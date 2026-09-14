"""Hapus data transaksi — untuk mulai ulang saat dev / salah impor.

Katalog (Reseller, ResellerAlias, MerchantMap) dan user TIDAK tersentuh.
"""

from __future__ import annotations

from datetime import date

from django.db import models, transaction

from apps.core.enums import MatchStatus
from apps.ingest.models import BankMutation, DebitIgnored, ExcludedTransaction, ImportBatch, OtomaxEntry

from .models import Adjustment, Discrepancy, Match, ReconDay

# Urutan hapus: anak dulu (hormati FK PROTECT).
_TXN_MODELS = (
    Adjustment,
    Discrepancy,
    Match,
    OtomaxEntry,
    BankMutation,
    DebitIgnored,
    ExcludedTransaction,
    ImportBatch,
    ReconDay,
)
_HISTORY_MODELS = (Adjustment, Discrepancy, Match, ReconDay)


class DayIsClosed(Exception):
    pass


def preview_day(book_date: date) -> dict[str, int]:
    return {
        "import_batch": ImportBatch.objects.filter(book_date=book_date).count(),
        "bank_mutation": BankMutation.objects.filter(book_date=book_date).count(),
        "otomax_entry": OtomaxEntry.objects.filter(book_date=book_date).count(),
        "debit_ignored": DebitIgnored.objects.filter(book_date=book_date).count(),
        "excluded_transaction": ExcludedTransaction.objects.filter(book_date=book_date).count(),
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
    ExcludedTransaction.objects.filter(book_date=book_date).delete()
    ImportBatch.objects.filter(book_date=book_date).delete()
    ReconDay.objects.filter(book_date=book_date).delete()

    # Bersihkan juga batch kosong tanpa data transaksi (orphan) jika ada
    for b in ImportBatch.objects.all():
        if (
            b.mutations.count() == 0
            and b.otomax.count() == 0
            and b.debits.count() == 0
            and b.excluded_transactions.count() == 0
        ):
            b.delete()

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


@transaction.atomic
def delete_import_batch(batch_id: int, *, include_closed: bool = False) -> dict:
    batch = ImportBatch.objects.select_for_update().get(id=batch_id)
    day = ReconDay.objects.select_for_update().filter(book_date=batch.book_date).first()
    if day and day.locked and not include_closed:
        raise DayIsClosed(f"Tanggal {batch.book_date} sudah ditutup — penghapusan batch ditolak.")

    # 1. Kumpulkan ID bank mutation dan otomax entry milik batch ini
    bank_ids = list(batch.mutations.values_list("id", flat=True))
    otomax_ids = list(batch.otomax.values_list("id", flat=True))

    # 2. Cari semua pasangan Match yang melibatkan mutasi / entri di batch ini
    matches = Match.objects.filter(
        models.Q(bank_mutation_id__in=bank_ids) | models.Q(otomax_entry_id__in=otomax_ids)
    )

    # Catat ID pasangan dari batch lain yang selamat agar statusnya direset ke UNMATCHED
    surviving_bank_ids = set()
    surviving_otomax_ids = set()
    for m in matches:
        if m.bank_mutation_id and m.bank_mutation_id not in bank_ids:
            surviving_bank_ids.add(m.bank_mutation_id)
        if m.otomax_entry_id and m.otomax_entry_id not in otomax_ids:
            surviving_otomax_ids.add(m.otomax_entry_id)

    # 3. Cari selisih (Discrepancy) yang melibatkan mutasi / entri di batch ini
    discrepancies = Discrepancy.objects.filter(
        models.Q(bank_mutation_id__in=bank_ids) | models.Q(otomax_entry_id__in=otomax_ids)
    )
    discrepancy_ids = list(discrepancies.values_list("id", flat=True))

    # 4. Hapus Adjustment yang mengacu pada discrepancy tersebut
    Adjustment.objects.filter(discrepancy_id__in=discrepancy_ids).delete()

    # 5. Hapus Discrepancy & Match
    discrepancies.delete()
    matches.delete()

    # 6. Reset status pasangan yang masih ada ke UNMATCHED
    if surviving_bank_ids:
        BankMutation.objects.filter(id__in=surviving_bank_ids).update(match_status=MatchStatus.UNMATCHED)
    if surviving_otomax_ids:
        OtomaxEntry.objects.filter(id__in=surviving_otomax_ids).update(match_status=MatchStatus.UNMATCHED)

    # 7. Hapus batch (cascade ke BankMutation, OtomaxEntry, DebitIgnored)
    actual_rows = len(bank_ids) + len(otomax_ids) + batch.debits.count()
    res = {
        "channel": batch.channel,
        "filename": batch.source_filename,
        "book_date": batch.book_date,
        "row_count": batch.row_count or actual_rows,
        "matches_unlinked": len(surviving_bank_ids) + len(surviving_otomax_ids),
    }
    batch.delete()
    return res

