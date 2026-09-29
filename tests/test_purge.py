from datetime import date

import pytest

from apps.catalog.models import Reseller
from apps.core.enums import Channel, MatchStatus
from apps.ingest.models import BankMutation, ImportBatch, OtomaxEntry
from apps.recon.close import close_day
from apps.recon.engine import run_match
from apps.recon.models import Discrepancy, Match, ReconDay
from apps.recon.purge import DayIsClosed, delete_import_batch, purge_all_transactions, purge_day

from .test_engine import _bank, _otomax

BD = date(2026, 9, 5)


@pytest.mark.django_db
def test_purge_day_removes_txn_keeps_catalog():
    reseller_count_before = Reseller.objects.count()  # 41 baris bawaan dari seed migration
    Reseller.objects.create(code="R1", name="R1")
    _bank("ATMLTRPRM 01884 000001039 21540100059656", "550000")
    _otomax("TARTUN EDC BRI ATMLTRPRM 01884 000001039 21540100059656", "550000")
    run_match(BD)

    purge_day(BD)

    assert BankMutation.objects.count() == 0
    assert ImportBatch.objects.count() == 0
    assert Reseller.objects.count() == reseller_count_before + 1  # katalog aman


@pytest.mark.django_db
def test_purge_day_blocked_when_closed_unless_flag():
    _bank("ATMLTRPRM 01884 000001039 21540100059656", "550000")
    run_match(BD)
    close_day(BD, force=True)

    with pytest.raises(DayIsClosed):
        purge_day(BD)

    purge_day(BD, include_closed=True)
    assert ReconDay.objects.count() == 0


@pytest.mark.django_db
def test_purge_all_transactions():
    _bank("DANA20260905034895588601ASEPKURNIAWA", "1600000", channel=Channel.BRI)
    run_match(BD)
    close_day(BD, force=True)
    assert Discrepancy.objects.exists()

    counts = purge_all_transactions()

    assert sum(counts.values()) > 0
    assert not Discrepancy.objects.exists()
    assert not ReconDay.objects.exists()


@pytest.mark.django_db
def test_delete_import_batch():
    b = _bank("ATMLTRPRM 01884 000001039 21540100059656", "550000")
    batch = b.import_batch
    assert BankMutation.objects.filter(import_batch=batch).count() == 1

    res = delete_import_batch(batch.id)
    assert res["row_count"] == 1
    assert BankMutation.objects.filter(import_batch=batch).count() == 0
    assert not ImportBatch.objects.filter(id=batch.id).exists()


@pytest.mark.django_db
def test_delete_import_batch_resets_matched_partner():
    b = _bank("ATMLTRPRM 01884 000001039 21540100059656", "550000")
    o = _otomax("TARTUN EDC BRI ATMLTRPRM 01884 000001039 21540100059656", "550000")
    run_match(BD)

    assert Match.objects.count() == 1
    b.refresh_from_db()
    o.refresh_from_db()
    assert b.match_status == MatchStatus.MATCHED
    assert o.match_status == MatchStatus.MATCHED

    # Hapus batch bank saja
    delete_import_batch(b.import_batch.id)

    # Match terhapus
    assert Match.objects.count() == 0
    # Bank mutation hilang
    assert not BankMutation.objects.filter(id=b.id).exists()
    # Otomax tetap ada tapi status kembali ke UNMATCHED
    o.refresh_from_db()
    assert o.match_status == MatchStatus.UNMATCHED
    assert OtomaxEntry.objects.filter(id=o.id).exists()


@pytest.mark.django_db
def test_delete_import_batch_resets_aggregate_qris_match():
    from apps.catalog.models import MerchantMap

    r = Reseller.objects.create(code="QR1", name="Outlet 1")
    MerchantMap.objects.create(merchant_id="M001", reseller=r)
    b = BankMutation.objects.create(
        import_batch=ImportBatch.objects.create(
            channel=Channel.MERCHANT_BCA, book_date=BD, source_filename="b", file_hash="hb"
        ),
        channel=Channel.MERCHANT_BCA,
        book_date=BD,
        description_raw="QRIS OUTLET 1",
        ref_normalized="QRIS OUTLET 1",
        amount=200000,
        external_ref="M001",
        row_hash="q1",
    )
    batch_o = ImportBatch.objects.create(channel=Channel.OTOMAX, book_date=BD, source_filename="o", file_hash="ho")
    o1 = _otomax("TARTUN QR BULK 1", "100000", channel=Channel.MERCHANT_BCA, reseller=r)
    o2 = _otomax("TARTUN QR BULK 2", "100000", channel=Channel.MERCHANT_BCA, reseller=r)
    o1.import_batch = batch_o
    o1.save()
    o2.import_batch = batch_o
    o2.save()
    run_match(BD)

    b.refresh_from_db()
    assert b.match_status == MatchStatus.MATCHED
    assert Match.objects.filter(bank_mutation=b).count() == 1

    # Hapus batch otomax
    delete_import_batch(batch_o.id)

    # Match terhapus dan mutasi bank kembali UNMATCHED
    assert Match.objects.count() == 0
    b.refresh_from_db()
    assert b.match_status == MatchStatus.UNMATCHED



# --- Regresi: hapus batch vs lawan netral & tutup buku di tanggal lain -----------------


def _otomax_in(batch, desc, amount, category, reseller, book_date=BD):
    from apps.core.enums import OtomaxCategory
    from apps.core.normalize import norm_ref, ref_core

    return OtomaxEntry.objects.create(
        import_batch=batch,
        book_date=book_date,
        reseller_name_raw=reseller,
        amount=amount,
        description_raw=desc,
        category=getattr(OtomaxCategory, category),
        ref_normalized=norm_ref(desc),
        ref_core=ref_core(desc),
        row_hash=f"{desc}|{amount}|{book_date}",
    )


@pytest.mark.django_db
def test_delete_import_batch_restores_net_pair_partner_in_other_batch():
    """Dulu: lawan netral di batch lain tetap IGNORED dengan net_pair=None (SET_NULL) --
    hilang dari semua antrean selamanya."""
    from apps.core.enums import DiscrepancyKind, DiscrepancyStatus
    from apps.recon.resolve import manual_net_reversal

    batch_a = ImportBatch.objects.create(channel=Channel.OTOMAX, book_date=BD, source_filename="a", file_hash="ha")
    batch_b = ImportBatch.objects.create(channel=Channel.OTOMAX, book_date=BD, source_filename="b", file_hash="hb")
    rev = _otomax_in(batch_a, "REV Transfer dari PLC128 - PLC PD3", -450000, "REVERSAL", "DANI")
    refund = _otomax_in(batch_b, "REFUND FROM OTO3386 - DANI", 450000, "OTHER", "PLC PD3")
    manual_net_reversal(rev, refund, note="salah tembak")

    delete_import_batch(batch_a.id)

    refund.refresh_from_db()
    assert refund.net_pair_id is None
    assert refund.match_status == MatchStatus.PENDING_SETTLE
    assert refund.discrepancies.filter(status=DiscrepancyStatus.OPEN, kind=DiscrepancyKind.OTOMAX_ONLY).exists()


@pytest.mark.django_db
def test_delete_import_batch_blocked_when_partner_day_closed():
    """Dulu: hanya tanggal batch yang dicek -- pasangan di hari yang sudah tutup buku
    ikut berubah status (melanggar Day-Lock)."""
    from datetime import timedelta

    from apps.recon.resolve import manual_pair_transactions

    closed_day = BD - timedelta(days=1)
    b = _bank("TRANSFER SUSULAN 999", "500000", book_date=closed_day)
    o = _otomax("TARTUN TF BRI SUSULAN 999", "500000", book_date=BD)
    manual_pair_transactions(b, o)
    ReconDay.objects.update_or_create(book_date=closed_day, defaults={"locked": True})

    with pytest.raises(DayIsClosed, match="05 Sep 2026|04 Sep 2026"):
        delete_import_batch(o.import_batch.id)
    assert OtomaxEntry.objects.filter(id=o.id).exists()
    b.refresh_from_db()
    assert b.match_status == MatchStatus.MANUAL


@pytest.mark.django_db
def test_delete_import_batch_blocked_when_row_date_differs_from_batch_date():
    """File multi-hari: batch bertanggal 5 Sep tapi berisi baris 4 Sep yang sudah ditutup."""
    from datetime import timedelta

    closed_day = BD - timedelta(days=1)
    b = _bank("BARIS HARI SEBELUMNYA", "10000", book_date=closed_day)
    ImportBatch.objects.filter(id=b.import_batch_id).update(book_date=BD)
    ReconDay.objects.update_or_create(book_date=closed_day, defaults={"locked": True})

    with pytest.raises(DayIsClosed):
        delete_import_batch(b.import_batch_id)
    assert BankMutation.objects.filter(id=b.id).exists()
