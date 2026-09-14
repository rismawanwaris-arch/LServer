from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.catalog.services import resolve_reseller
from apps.core.enums import Channel, ImportStatus, MatchStatus
from apps.core.normalize import (
    classify_otomax,
    extract_tokens,
    norm_ref,
    ref_core,
    strip_otomax_prefix,
)
from apps.recon.models import ReconDay

from .models import BankMutation, DebitIgnored, ImportBatch, OtomaxEntry
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
            rows_preview.append(
                {
                    "datetime": r.entry_datetime.strftime("%Y-%m-%d %H:%M") if r.entry_datetime else "-",
                    "reseller": r.reseller_name_raw,
                    "amount": float(r.amount),
                    "description": r.description_raw,
                    "category": category,
                    "hint": hint or "-",
                    "tokens": tokens,
                }
            )
        total_rows = len(result.otomax_rows)
    else:
        for r in result.bank_rows[:20]:
            tokens = extract_tokens(r.description_raw)
            rows_preview.append(
                {
                    "datetime": r.txn_datetime.strftime("%Y-%m-%d %H:%M") if r.txn_datetime else "-",
                    "amount": float(r.amount),
                    "description": r.description_raw,
                    "outlet": r.outlet_name or "-",
                    "ref": r.external_ref or "-",
                    "tokens": tokens,
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

    batch.row_count = batch.mutations.count() + batch.otomax.count() + batch.debits.count()
    batch.quarantined_count = quarantined
    batch.status = ImportStatus.PARTIAL if quarantined else ImportStatus.PARSED
    if result.warnings:
        batch.notes = "\n".join(result.warnings)
    batch.save(update_fields=["row_count", "quarantined_count", "status", "notes"])
    return batch


def _persist_bank(batch, result, channel, book_date) -> int:
    quarantined = 0
    for row in result.bank_rows:
        row_bdate = row.txn_datetime.date() if row.txn_datetime else book_date
        rh = _hash(channel, row_bdate, row.description_raw, row.amount, row.txn_datetime, row.external_ref)
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
