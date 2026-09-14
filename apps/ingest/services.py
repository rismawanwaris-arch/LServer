from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.catalog.services import find_matching_rule, resolve_reseller
from apps.core.enums import Channel, ImportStatus, MatchStatus
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


class ImportBlocked(Exception):
    pass


def preview_file(channel: str, content: str | bytes) -> dict:
    """Preview parsing file sebelum disimpan ke database."""
    result: ParseResult = parse_file(channel, content)
    rows_preview = []
    if channel == Channel.OTOMAX:
        for r in result.otomax_rows[:20]:
            category, hint = classify_otomax(r.description_raw)
            embedded = strip_otomax_prefix(r.description_raw)
            tokens = extract_tokens(embedded)
            rule = find_matching_rule(r.description_raw, channel="", is_bank=False)
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
            rule = find_matching_rule(r.description_raw, channel=channel, is_bank=True)
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


@transaction.atomic
def import_file(
    *,
    channel: str,
    content: str | bytes = None,
    text: str = None,
    book_date: date,
    filename: str,
    user=None,
) -> ImportBatch:
    day = ReconDay.objects.filter(book_date=book_date).first()
    if day and day.locked:
        raise ImportBlocked(f"Tanggal {book_date} sudah ditutup — impor ditolak.")

    data = content if content is not None else text
    if data is None:
        raise ValueError("content or text is required")

    result = parse_file(channel, data)
    content_bytes = data if isinstance(data, bytes) else data.encode("utf-8")
    batch = ImportBatch.objects.create(
        channel=channel,
        book_date=book_date,
        source_filename=filename,
        file_hash=_hash(channel, hashlib.sha256(content_bytes).hexdigest()),
        uploaded_by=user,
    )

    quarantined = 0
    if channel == Channel.OTOMAX:
        quarantined = _persist_otomax(batch, result, book_date)
    else:
        quarantined = _persist_bank(batch, result, channel, book_date)

    batch.row_count = (
        batch.mutations.count()
        + batch.otomax.count()
        + batch.debits.count()
        + batch.excluded_transactions.count()
    )
    batch.quarantined_count = quarantined
    batch.excluded_count = batch.excluded_transactions.count()
    batch.status = ImportStatus.PARTIAL if quarantined else ImportStatus.PARSED
    if result.warnings:
        batch.notes = "\n".join(result.warnings)
    batch.save(update_fields=["row_count", "quarantined_count", "excluded_count", "status", "notes"])
    return batch


def _persist_bank(batch, result, channel, book_date) -> int:
    quarantined = 0
    for row in result.bank_rows:
        row_bdate = row.txn_datetime.date() if row.txn_datetime else book_date
        rh = _hash(channel, row_bdate, row.description_raw, row.amount, row.txn_datetime, row.external_ref)

        # Cek ExclusionRule
        rule = find_matching_rule(row.description_raw, channel=channel, is_bank=True)
        if rule:
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

        if row.amount is not None and row.amount < 0:
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


def _persist_otomax(batch, result, book_date) -> int:
    for row in result.otomax_rows:
        row_bdate = row.entry_datetime.date() if row.entry_datetime else book_date
        rh = _hash(Channel.OTOMAX, row.description_raw, row.amount, row.entry_datetime, row.reseller_name_raw)

        # Cek ExclusionRule
        rule = find_matching_rule(row.description_raw, channel="", is_bank=False)
        if rule:
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

        category, hint = classify_otomax(row.description_raw)
        embedded = strip_otomax_prefix(row.description_raw)
        tokens = extract_tokens(embedded)
        OtomaxEntry.objects.get_or_create(
            row_hash=rh,
            defaults=dict(
                import_batch=batch,
                book_date=row_bdate,
                entry_datetime=_aware(row.entry_datetime),
                reseller_name_raw=row.reseller_name_raw,
                reseller=resolve_reseller(row.reseller_name_raw),
                amount=row.amount,
                description_raw=row.description_raw,
                category=category,
                channel_hint=hint or "",
                ref_normalized=norm_ref(embedded),
                ref_core=ref_core(embedded),
                extracted_tokens=tokens,
                match_status=MatchStatus.UNMATCHED,
            ),
        )
    return 0


@transaction.atomic
def apply_exclusion_rules_retroactive(book_date: date | None = None) -> dict[str, int]:
    """Terapkan aturan pemisahan secara retrospektif pada transaksi yang belum cocok (UNMATCHED)."""
    bank_qs = BankMutation.objects.filter(match_status=MatchStatus.UNMATCHED)
    otomax_qs = OtomaxEntry.objects.filter(match_status=MatchStatus.UNMATCHED)

    if book_date:
        bank_qs = bank_qs.filter(book_date=book_date)
        otomax_qs = otomax_qs.filter(book_date=book_date)

    bank_moved = 0
    batches_to_update = set()
    for bm in bank_qs:
        rule = find_matching_rule(bm.description_raw, channel=bm.channel, is_bank=True)
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
            bm.delete()
            bank_moved += 1

    otomax_moved = 0
    for oe in otomax_qs:
        rule = find_matching_rule(oe.description_raw, channel="", is_bank=False)
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

    return {"bank_moved": bank_moved, "otomax_moved": otomax_moved, "total_moved": bank_moved + otomax_moved}

