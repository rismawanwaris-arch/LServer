"""Pencocokan manual dengan nominal beda (mis. operator salah input Otomax) harus
menyisakan selisihnya sebagai Discrepancy(AMOUNT_DIFF) yang tetap kelihatan -- bukan
menghilang begitu saja dari Daftar Selisih & Dashboard. Lihat diskusi kasus:
mutasi Merchant BCA Rp 2.504.500 dicocokkan manual dengan entri Otomax Rp 2.409.500."""

from datetime import date
from decimal import Decimal

import pytest

from apps.core.enums import Channel, DiscrepancyKind, DiscrepancyStatus, MatchStatus, MatchType
from apps.recon.engine import run_match
from apps.recon.models import Discrepancy, Match
from apps.recon.reports import get_daily_summary
from apps.recon.resolve import manual_pair_transactions, unpair_match

from .test_engine import _bank, _otomax

BD = date(2026, 8, 29)


@pytest.mark.django_db
def test_manual_pair_with_amount_diff_creates_open_discrepancy():
    bm = _bank("QRIS CIKADUT 2 CELL 004769148", "2504500", channel=Channel.MERCHANT_BCA, book_date=BD)
    oe = _otomax("TARTUN QR BULK CIKADUT 2", "2409500", channel=Channel.MERCHANT_BCA, book_date=BD)
    run_match(BD)  # keduanya jadi leftover -> BANK_ONLY & OTOMAX_ONLY OPEN
    bm.refresh_from_db()
    oe.refresh_from_db()
    assert Discrepancy.objects.filter(bank_mutation=bm, status="OPEN", kind=DiscrepancyKind.BANK_ONLY).exists()
    assert Discrepancy.objects.filter(otomax_entry=oe, status="OPEN", kind=DiscrepancyKind.OTOMAX_ONLY).exists()

    match = manual_pair_transactions(bank_mutation=bm, otomax_entry=oe, note="Pencocokan manual")

    assert match.amount_diff == Decimal("95000.00")

    # Discrepancy leftover lama sudah dibersihkan (usang, sudah dapat pasangan).
    assert not Discrepancy.objects.filter(bank_mutation=bm, kind=DiscrepancyKind.BANK_ONLY).exists()
    assert not Discrepancy.objects.filter(otomax_entry=oe, kind=DiscrepancyKind.OTOMAX_ONLY).exists()

    # Diganti SATU discrepancy AMOUNT_DIFF baru, tetap OPEN, senilai sisa selisih riil.
    disc = Discrepancy.objects.get(bank_mutation=bm, otomax_entry=oe, kind=DiscrepancyKind.AMOUNT_DIFF)
    assert disc.status == DiscrepancyStatus.OPEN
    assert disc.amount == Decimal("95000.00")


@pytest.mark.django_db
def test_manual_pair_with_amount_diff_shows_in_daily_summary_selisih():
    bm = _bank("QRIS CIKADUT 2 CELL 004769148", "2504500", channel=Channel.MERCHANT_BCA, book_date=BD)
    oe = _otomax("TARTUN QR BULK CIKADUT 2", "2409500", channel=Channel.MERCHANT_BCA, book_date=BD)
    run_match(BD)
    bm.refresh_from_db()
    oe.refresh_from_db()

    manual_pair_transactions(bank_mutation=bm, otomax_entry=oe, note="Pencocokan manual")

    summary = get_daily_summary(BD)
    # Sebelum perbaikan ini, selisih_nya hilang jadi 0 begitu keduanya matched manual
    # walau nominalnya beda -- sekarang harus tetap kelihatan di Dashboard.
    assert summary["selisih"] == Decimal("95000.00")


@pytest.mark.django_db
def test_manual_pair_exact_amount_still_resolves_normally():
    """Regresi: nominal pas sama harus tetap berperilaku seperti sebelumnya -- discrepancy
    leftover diselesaikan penuh (DATA_FIX), TIDAK ada AMOUNT_DIFF baru dibuat."""
    bm = _bank("ATMLTRPRM 01884 000001039 21540100059656", "550000", channel=Channel.BRI, book_date=BD)
    oe = _otomax("TARTUN EDC BRI ATMLTRPRM 01884 999999999999", "550000", channel=Channel.BRI, book_date=BD)
    run_match(BD)
    bm.refresh_from_db()
    oe.refresh_from_db()

    match = manual_pair_transactions(bank_mutation=bm, otomax_entry=oe, note="Cocok manual persis")

    assert match.amount_diff == Decimal("0.00")
    assert not Discrepancy.objects.filter(kind=DiscrepancyKind.AMOUNT_DIFF).exists()
    assert not Discrepancy.objects.filter(status="OPEN").exists()

    summary = get_daily_summary(BD)
    assert summary["selisih"] == Decimal("0.00")


@pytest.mark.django_db
def test_unpair_removes_stale_amount_diff_discrepancy():
    bm = _bank("QRIS CIKADUT 2 CELL 004769148", "2504500", channel=Channel.MERCHANT_BCA, book_date=BD)
    oe = _otomax("TARTUN QR BULK CIKADUT 2", "2409500", channel=Channel.MERCHANT_BCA, book_date=BD)
    run_match(BD)
    bm.refresh_from_db()
    oe.refresh_from_db()
    match = manual_pair_transactions(bank_mutation=bm, otomax_entry=oe, note="Pencocokan manual")
    assert Discrepancy.objects.filter(bank_mutation=bm, otomax_entry=oe, status="OPEN").exists()

    unpair_match(match)

    assert not Discrepancy.objects.filter(bank_mutation=bm, otomax_entry=oe, status="OPEN").exists()
    bm.refresh_from_db()
    oe.refresh_from_db()
    assert bm.match_status == MatchStatus.UNMATCHED
    assert oe.match_status == MatchStatus.PENDING_SETTLE

    # Menjalankan engine lagi tidak meledak / tidak menumpuk discrepancy ganda untuk bm/o ini.
    run_match(BD)
    assert Discrepancy.objects.filter(bank_mutation=bm, status="OPEN").count() == 1
    assert Discrepancy.objects.filter(otomax_entry=oe, status="OPEN").count() == 1


@pytest.mark.django_db
def test_unpair_keeps_resolved_amount_diff_discrepancy():
    """Kalau discrepancy AMOUNT_DIFF-nya SUDAH diselesaikan (bukan lagi OPEN) sebelum match
    dibatalkan, jangan dihapus -- itu riwayat penyelesaian yang sah, harus tetap ada."""
    bm = _bank("QRIS CIKADUT 2 CELL 004769148", "2504500", channel=Channel.MERCHANT_BCA, book_date=BD)
    oe = _otomax("TARTUN QR BULK CIKADUT 2", "2409500", channel=Channel.MERCHANT_BCA, book_date=BD)
    run_match(BD)
    bm.refresh_from_db()
    oe.refresh_from_db()
    match = manual_pair_transactions(bank_mutation=bm, otomax_entry=oe, note="Pencocokan manual")

    disc = Discrepancy.objects.get(bank_mutation=bm, otomax_entry=oe, kind=DiscrepancyKind.AMOUNT_DIFF)
    disc.status = DiscrepancyStatus.WRITTEN_OFF
    disc.save(update_fields=["status"])

    unpair_match(match)

    assert Discrepancy.objects.filter(pk=disc.pk, status=DiscrepancyStatus.WRITTEN_OFF).exists()


@pytest.mark.django_db
def test_manual_match_view_with_amount_diff(client, django_user_model):
    user = django_user_model.objects.create_superuser(username="admin_resolve", password="password123")
    client.force_login(user)

    bm = _bank("QRIS CIKADUT 2 CELL 004769148", "2504500", channel=Channel.MERCHANT_BCA, book_date=BD)
    oe = _otomax("TARTUN QR BULK CIKADUT 2", "2409500", channel=Channel.MERCHANT_BCA, book_date=BD)
    run_match(BD)

    res = client.post(
        "/manual-match/",
        {"bank_id": bm.pk, "otomax_id": oe.pk, "note": "Cocok manual", "book_date": BD.isoformat()},
    )
    assert res.status_code in (200, 302)

    match = Match.objects.get(bank_mutation=bm, otomax_entry=oe, voided_at__isnull=True)
    assert match.amount_diff == Decimal("95000.00")

    res = client.get("/selisih/")
    assert res.status_code == 200
    content = res.content.decode()
    assert "nominal beda" in content
    assert "95.000" in content


@pytest.mark.django_db
def test_manual_match_merge_into_existing_diff_match(client, django_user_model):
    user = django_user_model.objects.create_superuser(username="admin_merge", password="password123")
    client.force_login(user)

    # Bank mutation: Rp 2.577.000
    bm = _bank("QRIS RAWA CELL 004767960", "2577000", channel=Channel.MERCHANT_BCA, book_date=BD)
    # Otomax 1: Rp 2.399.000 (diff: Rp 178.000)
    oe1 = _otomax("TARTUN QR RAWA CELL 1", "2399000", channel=Channel.MERCHANT_BCA, book_date=BD)
    # Otomax 2: Rp 178.000 (pending settle)
    oe2 = _otomax("TARTUN QR RAWA CELL 2", "178000", channel=Channel.MERCHANT_BCA, book_date=BD)

    # 1. Pasangkan bm dan oe1
    res1 = client.post(
        "/manual-match/",
        {"bank_id": bm.pk, "otomax_id": oe1.pk, "note": "Match 1", "book_date": BD.isoformat()},
    )
    assert res1.status_code in (200, 302)

    match = Match.objects.get(bank_mutation=bm, voided_at__isnull=True)
    assert match.amount_diff == Decimal("178000.00")
    assert Discrepancy.objects.filter(
        bank_mutation=bm, kind=DiscrepancyKind.AMOUNT_DIFF, status=DiscrepancyStatus.OPEN
    ).exists()

    # 2. Pasangkan bm yang sama dengan oe2 (menyerap sisa selisih Rp 178.000)
    res2 = client.post(
        "/manual-match/",
        {"bank_id": bm.pk, "otomax_id": oe2.pk, "note": "Match 2 gabung", "book_date": BD.isoformat()},
    )
    assert res2.status_code in (200, 302)

    match.refresh_from_db()
    assert match.amount_otomax == Decimal("2577000.00")
    assert match.amount_diff == Decimal("0.00")
    assert match.match_type == MatchType.AGGREGATE
    assert set(match.otomax_entries.values_list("id", flat=True)) == {oe1.id, oe2.id}

    oe2.refresh_from_db()
    assert oe2.match_status == MatchStatus.MANUAL

    # Discrepancy AMOUNT_DIFF harus sudah bersih karena selisihnya sudah 0
    assert not Discrepancy.objects.filter(
        bank_mutation=bm, kind=DiscrepancyKind.AMOUNT_DIFF, status=DiscrepancyStatus.OPEN
    ).exists()

    # Cek di /matches/
    res_matches = client.get("/matches/", {"d": BD.isoformat()})
    assert res_matches.status_code == 200
    m_content = res_matches.content.decode()
    assert "2 Tiket Gabungan" in m_content
    assert "QRIS RAWA CELL" in m_content
