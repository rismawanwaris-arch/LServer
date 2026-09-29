from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.catalog.models import ExclusionRule, ResellerAlias
from apps.catalog.services import find_matching_rule, resolve_reseller
from apps.core.enums import Channel, DiscrepancyStatus, ImportStatus, MatchStatus
from apps.core.normalize import (
    classify_otomax,
    extract_tokens,
    norm_ref,
    ref_core,
    strip_otomax_prefix,
)
from apps.recon.models import ReconDay

from .models import BankMutation, DebitIgnored, ExcludedTransaction, ImportBatch, OtomaxEntry
from .parsers import ParseResult, parse_file


def _hash(*parts) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None or timezone.is_aware(dt):
        return dt
    return timezone.make_aware(dt)


def otomax_derived_fields(description_raw: str) -> dict:
    """Field OtomaxEntry yang diturunkan dari keterangan mentah -- satu sumber kebenaran
    untuk impor maupun klasifikasi ulang data lama (reclassify_otomax)."""
    category, hint = classify_otomax(description_raw)
    embedded = strip_otomax_prefix(description_raw)
    return {
        "category": category,
        "channel_hint": hint or "",
        "ref_normalized": norm_ref(embedded),
        "ref_core": ref_core(embedded),
        "extracted_tokens": extract_tokens(embedded),
    }


class ImportBlocked(Exception):
    pass


def preview_file(channel: str, content: str | bytes, book_date: date | None = None) -> dict:
    """Preview parsing file sebelum disimpan ke database."""
    result: ParseResult = parse_file(channel, content)
    if result.book_date and book_date and result.book_date != book_date:
        result.warnings.append(
            f"Perhatian: Tanggal transaksi di file ini terdeteksi {result.book_date.strftime('%d %B %Y')} "
            f"(berbeda dengan tanggal form {book_date.strftime('%d %B %Y')}). "
            f"Saat disimpan, sistem akan otomatis menyimpannya ke tanggal {result.book_date.strftime('%d %B %Y')}."
        )

    rows_preview = []
    active_rules = list(ExclusionRule.objects.filter(active=True))
    if channel == Channel.OTOMAX:
        for r in result.otomax_rows[:20]:
            category, hint = classify_otomax(r.description_raw)
            embedded = strip_otomax_prefix(r.description_raw)
            tokens = extract_tokens(embedded)
            rule = find_matching_rule(r.description_raw, channel="", is_bank=False, rules=active_rules)
            rows_preview.append(
                {
                    "datetime": r.entry_datetime.strftime("%Y-%m-%d %H:%M") if r.entry_datetime else "-",
                    "reseller": r.reseller_name_raw,
                    "amount": float(r.amount),
                    "description": r.description_raw,
                    "category": category,
                    "hint": hint or "-",
                    "tokens": tokens,
                    "excluded": bool(rule),
                    "rule_name": rule.name if rule else "",
                }
            )
        total_rows = len(result.otomax_rows)
    else:
        for r in result.bank_rows[:20]:
            tokens = extract_tokens(r.description_raw)
            rule = find_matching_rule(r.description_raw, channel=channel, is_bank=True, rules=active_rules)
            rows_preview.append(
                {
                    "datetime": r.txn_datetime.strftime("%Y-%m-%d %H:%M") if r.txn_datetime else "-",
                    "amount": float(r.amount),
                    "description": r.description_raw,
                    "outlet": r.outlet_name or "-",
                    "ref": r.external_ref or "-",
                    "tokens": tokens,
                    "excluded": bool(rule),
                    "rule_name": rule.name if rule else "",
                }
            )
        total_rows = len(result.bank_rows)

    return {
        "channel": channel,
        "book_date": result.book_date.isoformat() if result.book_date else None,
        "total_rows": total_rows,
        "warnings": result.warnings,
        "rows": rows_preview,
    }


class NothingToImport(ImportBlocked):
    """Tidak ada satu pun baris baru -- jangan membuat batch kosong."""


@transaction.atomic
def import_file(
    *,
    channel: str,
    content: str | bytes = None,
    text: str = None,
    book_date: date,
    filename: str,
    user=None,
    include_maybe: set[str] | frozenset[str] = frozenset(),
) -> ImportBatch:
    """Simpan isi file. Aturan pilih baris SAMA dengan layar Periksa (review.analyze_upload),
    dihitung ulang di sini (server hakim terakhir): yang disimpan hanya baris berstatus
    "baru", ditambah baris "kemungkinan sudah ada" yang dipilih operator (include_maybe =
    row_hash-nya). Baris kembar/sudah ada/di hari yang sudah ditutup dilewati & dihitung
    di batch.review. Tanpa baris baru -> NothingToImport (tidak ada batch kosong)."""
    from .review import BARU, DEBIT, DIKECUALIKAN, MUNGKIN_ADA, OTOMAX, analyze_upload, file_fingerprint

    data = content if content is not None else text
    if data is None:
        raise ValueError("content or text is required")
    content_bytes = data if isinstance(data, bytes) else data.encode("utf-8")

    review = analyze_upload(channel, content_bytes, book_date, filename)
    # Otomatis deteksi tanggal buku dari isi file: jika parser mendeteksi tanggal dominan
    # di file, gunakan tanggal tersebut agar batch dan transaksi selalu sinkron.
    effective_book_date = review.book_date

    day = ReconDay.objects.filter(book_date=effective_book_date).first()
    if day and day.locked:
        raise ImportBlocked(f"Tanggal {effective_book_date} sudah ditutup — impor ditolak.")

    to_save = [r for r in review.rows if r.status == BARU or (r.status == MUNGKIN_ADA and r.row_hash in include_maybe)]
    counts = review.counts
    summary = {
        "saved": len(to_save),
        "existing": counts["sudah_ada"],
        "maybe_skipped": counts["mungkin_ada"] - sum(1 for r in to_save if r.status == MUNGKIN_ADA),
        "dup_file": counts["kembar_file"],
        "locked": counts["ditutup"],
    }
    if not to_save:
        raise NothingToImport(
            f"Tidak ada baris baru untuk disimpan dari {filename}: "
            f"{summary['existing']} sudah ada, {summary['maybe_skipped']} kemungkinan sudah ada, "
            f"{summary['dup_file']} kembar di file, {summary['locked']} di hari yang sudah ditutup."
        )

    batch = ImportBatch.objects.create(
        channel=channel,
        book_date=effective_book_date,
        source_filename=filename,
        file_hash=file_fingerprint(channel, content_bytes),
        uploaded_by=user,
    )

    quarantined = 0
    if channel == Channel.OTOMAX:
        _persist_otomax(batch, [r for r in to_save if r.kind in (OTOMAX, DIKECUALIKAN)])
    else:
        quarantined = _persist_bank(batch, to_save, channel, debit_kind=DEBIT, excluded_kind=DIKECUALIKAN)

    batch.row_count = (
        batch.mutations.count()
        + batch.otomax.count()
        + batch.debits.count()
        + batch.excluded_transactions.count()
    )
    batch.quarantined_count = quarantined
    batch.excluded_count = batch.excluded_transactions.count()
    batch.status = ImportStatus.PARTIAL if quarantined else ImportStatus.PARSED
    notes = list(review.result.warnings)
    skipped = {k: v for k, v in summary.items() if k != "saved" and v}
    if skipped:
        notes.append(
            "Dilewati saat simpan: "
            + ", ".join(
                f"{v} {label}"
                for k, v in skipped.items()
                for label in [
                    {
                        "existing": "sudah ada",
                        "maybe_skipped": "kemungkinan sudah ada",
                        "dup_file": "kembar di file",
                        "locked": "hari sudah ditutup",
                    }[k]
                ]
            )
        )
    if notes:
        batch.notes = "\n".join(notes)
    batch.save(update_fields=["row_count", "quarantined_count", "excluded_count", "status", "notes"])
    batch.review = summary  # bukan kolom DB -- untuk pesan hasil ke operator
    return batch


def _persist_bank(batch, rows, channel, *, debit_kind, excluded_kind) -> int:
    quarantined = 0
    for r in rows:
        row, rh, row_bdate = r.parsed, r.row_hash, r.row_bdate
        if r.kind == excluded_kind:
            rule = r.rule
            ExcludedTransaction.objects.get_or_create(
                row_hash=rh,
                defaults=dict(
                    import_batch=batch,
                    channel=channel,
                    source_type="BANK",
                    book_date=row_bdate,
                    txn_datetime=_aware(row.txn_datetime),
                    description_raw=row.description_raw,
                    amount=row.amount or Decimal("0"),
                    rule=rule,
                    category=rule.category,
                    reason=f"Aturan: {rule.name} ({rule.keywords})",
                ),
            )
            continue

        if r.kind == debit_kind:
            DebitIgnored.objects.get_or_create(
                row_hash=rh,
                defaults=dict(
                    import_batch=batch,
                    channel=channel,
                    book_date=row_bdate,
                    txn_datetime=_aware(row.txn_datetime),
                    description_raw=row.description_raw,
                    amount=abs(row.amount),
                ),
            )
            continue
        nr = norm_ref(row.description_raw)
        tokens = extract_tokens(row.description_raw)
        obj, created = BankMutation.objects.get_or_create(
            row_hash=rh,
            defaults=dict(
                import_batch=batch,
                channel=channel,
                book_date=row_bdate,
                txn_datetime=_aware(row.txn_datetime),
                description_raw=row.description_raw,
                ref_normalized=nr,
                ref_core=ref_core(row.description_raw),
                extracted_tokens=tokens,
                outlet_name=row.outlet_name,
                amount=row.amount or Decimal("0"),
                external_ref=row.external_ref,
                frequency=row.frequency,
                review_flag=row.review_flag,
            ),
        )
        if created and obj.review_flag:
            quarantined += 1
    return quarantined


def _persist_otomax(batch, rows) -> int:
    alias_map = {a.alias_norm: a.reseller for a in ResellerAlias.objects.select_related("reseller")}
    for r in rows:
        row, rh, row_bdate = r.parsed, r.row_hash, r.row_bdate
        if r.rule:
            rule = r.rule
            ExcludedTransaction.objects.get_or_create(
                row_hash=rh,
                defaults=dict(
                    import_batch=batch,
                    channel=Channel.OTOMAX,
                    source_type="OTOMAX",
                    book_date=row_bdate,
                    txn_datetime=_aware(row.entry_datetime),
                    description_raw=row.description_raw,
                    amount=row.amount or Decimal("0"),
                    rule=rule,
                    category=rule.category,
                    reason=f"Aturan: {rule.name} ({rule.keywords})",
                ),
            )
            continue

        OtomaxEntry.objects.get_or_create(
            row_hash=rh,
            defaults=dict(
                import_batch=batch,
                book_date=row_bdate,
                entry_datetime=_aware(row.entry_datetime),
                reseller_name_raw=row.reseller_name_raw,
                reseller=resolve_reseller(row.reseller_name_raw, alias_map=alias_map),
                amount=row.amount,
                description_raw=row.description_raw,
                match_status=MatchStatus.UNMATCHED,
                **otomax_derived_fields(row.description_raw),
            ),
        )
    return 0


@transaction.atomic
def apply_exclusion_rules_retroactive(book_date: date | None = None) -> dict[str, int]:
    """Terapkan aturan pemisahan secara retrospektif pada transaksi yang belum cocok (UNMATCHED / PENDING_SETTLE)."""
    bank_qs = BankMutation.objects.filter(match_status=MatchStatus.UNMATCHED)
    otomax_qs = OtomaxEntry.objects.filter(match_status__in=[MatchStatus.UNMATCHED, MatchStatus.PENDING_SETTLE])

    if book_date:
        bank_qs = bank_qs.filter(book_date=book_date)
        otomax_qs = otomax_qs.filter(book_date=book_date)

    active_rules = list(ExclusionRule.objects.filter(active=True))
    bank_moved = 0
    batches_to_update = set()
    for bm in bank_qs:
        rule = find_matching_rule(bm.description_raw, channel=bm.channel, is_bank=True, rules=active_rules)
        if rule:
            ExcludedTransaction.objects.get_or_create(
                row_hash=bm.row_hash,
                defaults=dict(
                    import_batch=bm.import_batch,
                    channel=bm.channel,
                    source_type="BANK",
                    book_date=bm.book_date,
                    txn_datetime=bm.txn_datetime,
                    description_raw=bm.description_raw,
                    amount=bm.amount,
                    rule=rule,
                    category=rule.category,
                    reason=f"Aturan: {rule.name} ({rule.keywords})",
                ),
            )
            batches_to_update.add(bm.import_batch)
            bm.discrepancies.filter(status=DiscrepancyStatus.OPEN).delete()
            bm.delete()
            bank_moved += 1

    otomax_moved = 0
    for oe in otomax_qs:
        rule = find_matching_rule(oe.description_raw, channel="", is_bank=False, rules=active_rules)
        if rule:
            ExcludedTransaction.objects.get_or_create(
                row_hash=oe.row_hash,
                defaults=dict(
                    import_batch=oe.import_batch,
                    channel=Channel.OTOMAX,
                    source_type="OTOMAX",
                    book_date=oe.book_date,
                    txn_datetime=oe.entry_datetime,
                    description_raw=oe.description_raw,
                    amount=oe.amount,
                    rule=rule,
                    category=rule.category,
                    reason=f"Aturan: {rule.name} ({rule.keywords})",
                ),
            )
            batches_to_update.add(oe.import_batch)
            oe.discrepancies.filter(status=DiscrepancyStatus.OPEN).delete()
            oe.delete()
            otomax_moved += 1

    for batch in batches_to_update:
        batch.excluded_count = batch.excluded_transactions.count()
        batch.row_count = (
            batch.mutations.count()
            + batch.otomax.count()
            + batch.debits.count()
            + batch.excluded_transactions.count()
        )
        batch.save(update_fields=["excluded_count", "row_count"])

    # Sinkronisasi ReconDay jika ada hari yang belum dikunci
    affected_dates = {b.book_date for b in batches_to_update}
    for d in affected_dates:
        day = ReconDay.objects.filter(book_date=d, locked=False).first()
        if day:
            from apps.recon.close import compute_totals

            totals = compute_totals(d)
            day.total_in_bri = totals["bri"]
            day.total_in_bca = totals["bca"]
            day.total_in_merchant_bca = totals["merchant_bca"]
            day.total_in_mandiri = totals["mandiri"]
            day.total_in_bank = totals["bank"]
            day.total_out_otomax = totals["otomax"]
            day.selisih_initial = totals["selisih"]
            day.recompute_selisih()
            day.save()

    return {"bank_moved": bank_moved, "otomax_moved": otomax_moved, "total_moved": bank_moved + otomax_moved}

