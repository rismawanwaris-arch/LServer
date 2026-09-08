"""Hapus data transaksi — untuk mulai ulang saat dev / salah impor.

Katalog (Reseller, ResellerAlias, MerchantMap) dan user TIDAK tersentuh.
"""

from __future__ import annotations

from datetime import date

from django.db import transaction

from apps.ingest.models import BankMutation, DebitIgnored, ImportBatch, OtomaxEntry

from .models import Adjustment, Discrepancy, Match, ReconDay

# Urutan hapus: anak dulu (hormati FK PROTECT).
_TXN_MODELS = (Adjustment, Discrepancy, Match, OtomaxEntry, BankMutation, DebitIgnored, ImportBatch, ReconDay)
_HISTORY_MODELS = (Adjustment, Discrepancy, Match, ReconDay)


class DayIsClosed(Exception):
    pass


def preview_day(book_date: date) -> dict[str, int]:
    return {
        "import_batch": ImportBatch.objects.filter(book_date=book_date).count(),
        "bank_mutation": BankMutation.objects.filter(book_date=book_date).count(),
        "otomax_entry": OtomaxEntry.objects.filter(book_date=book_date).count(),
        "debit_ignored": DebitIgnored.objects.filter(book_date=book_date).count(),
        "match": Match.objects.filter(book_date=book_date).count(),
        "discrepancy": Discrepancy.objects.filter(origin_book_date=book_date).count(),
        "adjustment": Adjustment.objects.filter(book_date=book_date).count(),
        "recon_day": ReconDay.objects.filter(book_date=book_date).count(),
    }


def preview_all() -> dict[str, int]:
    return {m._meta.model_name: m.objects.count() for m in _TXN_MODELS}


@transaction.atomic
def purge_day(book_date: date, *, include_closed: bool = False) -> dict[str, int]:
    day = ReconDay.objects.select_for_update().filter(book_date=book_date).first()
    if day and day.locked and not include_closed:
        raise DayIsClosed(f"{book_date} sudah ditutup. Centang 'termasuk hari yang sudah ditutup'.")

    counts = preview_day(book_date)
    Adjustment.objects.filter(book_date=book_date).delete()
    Discrepancy.objects.filter(origin_book_date=book_date).delete()
    Match.objects.filter(book_date=book_date).delete()
    OtomaxEntry.objects.filter(book_date=book_date).delete()
    BankMutation.objects.filter(book_date=book_date).delete()
    DebitIgnored.objects.filter(book_date=book_date).delete()
    ImportBatch.objects.filter(book_date=book_date).delete()
    ReconDay.objects.filter(book_date=book_date).delete()
    return counts


@transaction.atomic
def purge_all_transactions(*, include_history: bool = False) -> dict[str, int]:
    counts = preview_all()
    for model in _TXN_MODELS:
        model.objects.all().delete()
    if include_history:
        for model in _HISTORY_MODELS:
            model.history.all().delete()
    return counts
