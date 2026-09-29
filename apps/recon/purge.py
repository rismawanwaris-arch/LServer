"""Hapus data transaksi — untuk mulai ulang saat dev / salah impor.

Katalog (Reseller, ResellerAlias, MerchantMap) dan user TIDAK tersentuh.
"""

from __future__ import annotations

from datetime import date

from django.db import models, transaction

from apps.core.enums import MatchStatus, OtomaxCategory
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
    from .raw_delete import SRC_BANK, SRC_OTOMAX, bulk_affected_dates, restore_net_partner

    batch = ImportBatch.objects.select_for_update().get(id=batch_id)
    batch_date = batch.book_date

    # 1. Kumpulkan baris milik batch ini
    bank_objs = list(batch.mutations.all())
    otomax_objs = list(batch.otomax.select_related("net_pair"))
    bank_ids = [b.id for b in bank_objs]
    otomax_ids = [o.id for o in otomax_objs]

    # Day-Lock: bukan cuma tanggal batch -- baris file multi-hari, pasangan cocoknya, dan
    # lawan netralnya bisa ada di tanggal lain yang sudah tutup buku.
    affected = {batch_date}
    affected |= set(batch.debits.values_list("book_date", flat=True))
    affected |= set(batch.excluded_transactions.values_list("book_date", flat=True))
    for src, objs in ((SRC_BANK, bank_objs), (SRC_OTOMAX, otomax_objs)):
        for dates in bulk_affected_dates(src, objs).values():
            affected |= dates
    locked = sorted(ReconDay.objects.filter(book_date__in=affected, locked=True).values_list("book_date", flat=True))
    if locked and not include_closed:
        tgl = ", ".join(d.strftime("%d %b %Y") for d in locked)
        raise DayIsClosed(f"Tanggal {tgl} sudah ditutup — penghapusan batch ditolak.")

    # 2. Cari semua pasangan Match yang melibatkan mutasi / entri di batch ini (termasuk M2M QRIS AGGREGATE)
    matches = Match.objects.filter(
        models.Q(bank_mutation_id__in=bank_ids)
        | models.Q(bank_mutations__in=bank_ids)
        | models.Q(otomax_entry_id__in=otomax_ids)
        | models.Q(otomax_entries__in=otomax_ids)
    ).distinct()

    # Catat ID pasangan dari batch lain yang selamat agar statusnya direset ke UNMATCHED
    surviving_bank_ids = set()
    surviving_otomax_ids = set()
    for m in matches.prefetch_related("otomax_entries", "bank_mutations"):
        if m.bank_mutation_id and m.bank_mutation_id not in bank_ids:
            surviving_bank_ids.add(m.bank_mutation_id)
        for b in m.bank_mutations.all():
            if b.id not in bank_ids:
                surviving_bank_ids.add(b.id)
        if m.otomax_entry_id and m.otomax_entry_id not in otomax_ids:
            surviving_otomax_ids.add(m.otomax_entry_id)
        for o in m.otomax_entries.all():
            if o.id not in otomax_ids:
                surviving_otomax_ids.add(o.id)

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

    # 6. Reset status pasangan yang masih ada ke UNMATCHED jika sudah tidak punya match aktif lain
    for b_id in surviving_bank_ids:
        if not Match.objects.filter(
            models.Q(bank_mutation_id=b_id) | models.Q(bank_mutations__id=b_id),
            voided_at__isnull=True,
        ).exists():
            BankMutation.objects.filter(id=b_id).update(match_status=MatchStatus.UNMATCHED)
    for o_id in surviving_otomax_ids:
        if not Match.objects.filter(
            models.Q(otomax_entry_id=o_id) | models.Q(otomax_entries__id=o_id),
            voided_at__isnull=True,
        ).exists():
            OtomaxEntry.objects.filter(id=o_id).update(match_status=MatchStatus.UNMATCHED)

    # 6b. Lawan netral di batch lain dikembalikan ke Pending Settle -- dulu tetap IGNORED
    #     (net_pair jadi NULL karena SET_NULL) dan hilang dari semua antrean.
    in_batch = set(otomax_ids)
    unnetted = 0
    for o in otomax_objs:
        if o.net_pair_id and o.net_pair_id not in in_batch:
            unnetted += restore_net_partner(o.net_pair_id, o.pk, "Lawan netralnya ikut terhapus bersama batch")

    # 7. Hapus batch (cascade ke BankMutation, OtomaxEntry, DebitIgnored, ExcludedTransaction)
    actual_rows = len(bank_ids) + len(otomax_ids) + batch.debits.count()
    res = {
        "channel": batch.channel,
        "filename": batch.source_filename,
        "book_date": batch_date,
        "row_count": batch.row_count or actual_rows,
        "matches_unlinked": len(surviving_bank_ids) + len(surviving_otomax_ids) + unnetted,
    }
    batch.delete()

    # 8. Hitung ulang semua hari terdampak yang belum dikunci
    for d in affected:
        refresh_recon_day(d)

    return res


def refresh_recon_day(book_date: date) -> None:
    """Hitung ulang total & selisih ReconDay yang BELUM dikunci setelah data mentah
    berkurang (hapus batch / hapus baris dari Audit Data). Hari terkunci tidak disentuh."""
    day = ReconDay.objects.select_for_update().filter(book_date=book_date, locked=False).first()
    if not day:
        return
    from .close import compute_totals

    totals = compute_totals(book_date)
    day.total_in_bri = totals["bri"]
    day.total_in_bca = totals["bca"]
    day.total_in_merchant_bca = totals["merchant_bca"]
    day.total_in_mandiri = totals["mandiri"]
    day.total_in_bank = totals["bank"]
    day.total_out_otomax = totals["otomax"]
    day.selisih_initial = totals["selisih"]
    day.matched_count = Match.objects.filter(book_date=book_date, voided_at__isnull=True).count()
    day.unmatched_count = (
        BankMutation.objects.filter(book_date=book_date, match_status=MatchStatus.UNMATCHED).count()
        + OtomaxEntry.objects.filter(
            book_date=book_date,
            category=OtomaxCategory.TOPUP_TARTUN,
            match_status__in=[MatchStatus.UNMATCHED, MatchStatus.PENDING_SETTLE],
        ).count()
    )
    day.recompute_selisih()
    day.save()
