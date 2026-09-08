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
    norm_ref,
    ref_core,
    strip_otomax_prefix,
)
from apps.recon.models import ReconDay

from .models import BankMutation, DebitIgnored, ImportBatch, OtomaxEntry
from .parsers import parse_file


def _hash(*parts) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None or timezone.is_aware(dt):
        return dt
    return timezone.make_aware(dt)


class ImportBlocked(Exception):
    pass


@transaction.atomic
def import_file(*, channel: str, text: str, book_date: date, filename: str, user=None) -> ImportBatch:
    day = ReconDay.objects.filter(book_date=book_date).first()
    if day and day.locked:
        raise ImportBlocked(f"Tanggal {book_date} sudah ditutup — impor ditolak.")

    result = parse_file(channel, text)
    batch = ImportBatch.objects.create(
        channel=channel,
        book_date=book_date,
        source_filename=filename,
        file_hash=_hash(channel, hashlib.sha256(text.encode()).hexdigest()),
        uploaded_by=user,
    )

    quarantined = 0
    if channel == Channel.OTOMAX:
        quarantined = _persist_otomax(batch, result, book_date)
    else:
        quarantined = _persist_bank(batch, result, channel, book_date)

    batch.row_count = (
        batch.mutations.count() + batch.otomax.count() + batch.debits.count()
    )
    batch.quarantined_count = quarantined
    batch.status = ImportStatus.PARTIAL if quarantined else ImportStatus.PARSED
    if result.warnings:
        batch.notes = "\n".join(result.warnings)
    batch.save(update_fields=["row_count", "quarantined_count", "status", "notes"])
    return batch


def _persist_bank(batch, result, channel, book_date) -> int:
    quarantined = 0
    for row in result.bank_rows:
        rh = _hash(channel, book_date, row.description_raw, row.amount, row.txn_datetime, row.external_ref)
        if row.amount is not None and row.amount < 0:
            DebitIgnored.objects.get_or_create(
                row_hash=rh,
                defaults=dict(
                    import_batch=batch, channel=channel, book_date=book_date,
                    txn_datetime=_aware(row.txn_datetime), description_raw=row.description_raw,
                    amount=abs(row.amount),
                ),
            )
            continue
        nr = norm_ref(row.description_raw)
        obj, created = BankMutation.objects.get_or_create(
            row_hash=rh,
            defaults=dict(
                import_batch=batch, channel=channel, book_date=book_date,
                txn_datetime=_aware(row.txn_datetime), description_raw=row.description_raw,
                ref_normalized=nr, ref_core=ref_core(row.description_raw),
                amount=row.amount or Decimal("0"), external_ref=row.external_ref,
                frequency=row.frequency, review_flag=row.review_flag,
            ),
        )
        if created and obj.review_flag:
            quarantined += 1
    return quarantined


def _persist_otomax(batch, result, book_date) -> int:
    for row in result.otomax_rows:
        rh = _hash(Channel.OTOMAX, row.description_raw, row.amount, row.entry_datetime, row.reseller_name_raw)
        category, hint = classify_otomax(row.description_raw)
        embedded = strip_otomax_prefix(row.description_raw)
        OtomaxEntry.objects.get_or_create(
            row_hash=rh,
            defaults=dict(
                import_batch=batch, book_date=book_date, entry_datetime=_aware(row.entry_datetime),
                reseller_name_raw=row.reseller_name_raw,
                reseller=resolve_reseller(row.reseller_name_raw),
                amount=row.amount, description_raw=row.description_raw,
                category=category, channel_hint=hint or "",
                ref_normalized=norm_ref(embedded), ref_core=ref_core(embedded),
                match_status=MatchStatus.UNMATCHED,
            ),
        )
    return 0
