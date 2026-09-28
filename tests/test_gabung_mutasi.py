"""Gabungan manual BEBERAPA mutasi bank <-> 1 entri Otomax. Kasus nyata: Tartun QR Bulk
KBB KALAPA Rp 3.329.000 (entri 4 Sep) menutup settlement dua outlet QRIS 3 Sep --
OJEG CELL QR Rp 2.488.000 + BANDAR KUOTA QR Rp 841.000."""

from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.core.enums import Channel, DiscrepancyKind, DiscrepancyStatus, MatchStatus
from apps.recon.engine import run_match
from apps.recon.models import Discrepancy, Match
from apps.recon.purge import delete_import_batch
from apps.recon.resolve import manual_pair_many, manual_pair_many_banks, manual_pair_transactions, unpair_match

from .test_engine import _bank, _otomax

SEP3, SEP4 = date(2026, 9, 3), date(2026, 9, 4)


@pytest.fixture
def auth_client(db):
    client = Client()
    client.force_login(User.objects.create_superuser(username="op_gabung", password="password123"))
    return client


def _kalapa_case(otomax_amount="3329000"):
    ojeg = _bank("QRIS OJEG CELL QR 001776725", "2488000", channel=Channel.MERCHANT_BCA, book_date=SEP3)
    bandar = _bank("QRIS BANDAR KUOTA QR 001776782", "841000", channel=Channel.MERCHANT_BCA, book_date=SEP3)
    oe = _otomax("TARTUN QR BULK TGL 03-SEP-2026", otomax_amount, channel=Channel.MERCHANT_BCA, book_date=SEP4)
    run_match(SEP3)
    run_match(SEP4)  # semuanya jadi leftover
    for row in (ojeg, bandar, oe):
        row.refresh_from_db()
    return ojeg, bandar, oe


def _open_discs(**filters):
    return Discrepancy.objects.filter(status=DiscrepancyStatus.OPEN, **filters)


@pytest.mark.django_db
def test_two_bank_mutations_combined_with_one_otomax_entry():
    ojeg, bandar, oe = _kalapa_case()
    assert _open_discs(bank_mutation__in=[ojeg, bandar]).count() == 2

    m = manual_pair_many_banks([ojeg, bandar], oe, note="KBB KALAPA 2 outlet")

    assert m.amount_bank == Decimal("3329000.00")
    assert m.amount_diff == Decimal("0.00")
    assert m.bank_mutation_id == ojeg.id  # nominal terbesar jadi mutasi utama tampilan
    assert set(m.bank_mutations.values_list("id", flat=True)) == {ojeg.id, bandar.id}
    for row in (ojeg, bandar, oe):
        row.refresh_from_db()
        assert row.match_status == MatchStatus.MANUAL
    assert not _open_discs(bank_mutation__in=[ojeg, bandar]).exists()
    assert not _open_discs(otomax_entry=oe).exists()


@pytest.mark.django_db
def test_combined_banks_with_remaining_diff_keeps_amount_diff_on_primary():
    ojeg, bandar, oe = _kalapa_case(otomax_amount="3330000")

    m = manual_pair_many_banks([ojeg, bandar], oe)

    assert m.amount_diff == Decimal("-1000.00")
    disc = Discrepancy.objects.get(kind=DiscrepancyKind.AMOUNT_DIFF, status=DiscrepancyStatus.OPEN)
    assert disc.bank_mutation_id == ojeg.id and disc.amount == Decimal("-1000.00")


@pytest.mark.django_db
def test_combined_banks_rejects_invalid_selection():
    ojeg, bandar, oe = _kalapa_case()
    with pytest.raises(ValueError):
        manual_pair_many_banks([ojeg], oe)  # minimal 2 mutasi

    manual_pair_many_banks([ojeg, bandar], oe)
    other = _otomax("TARTUN QR BULK LAIN", "100000", channel=Channel.MERCHANT_BCA, book_date=SEP4)
    other2 = _otomax("TARTUN QR BULK LAIN 2", "200000", channel=Channel.MERCHANT_BCA, book_date=SEP4)
    extra_bank = _bank("QRIS OUTLET LAIN", "5000", channel=Channel.MERCHANT_BCA, book_date=SEP3)
    with pytest.raises(ValueError):
        manual_pair_many_banks([bandar, extra_bank], other)  # bandar sudah anggota gabungan
    with pytest.raises(ValueError):
        manual_pair_many(bandar, [other, other2])  # anggota gabungan dianggap sudah berpasangan


@pytest.mark.django_db
def test_unpair_combined_banks_releases_every_mutation():
    ojeg, bandar, oe = _kalapa_case()
    m = manual_pair_many_banks([ojeg, bandar], oe)

    unpair_match(m)

    for bank in (ojeg, bandar):
        bank.refresh_from_db()
        assert bank.match_status == MatchStatus.UNMATCHED
    oe.refresh_from_db()
    assert oe.match_status == MatchStatus.PENDING_SETTLE


@pytest.mark.django_db
def test_deleting_batch_of_member_mutation_releases_the_rest():
    ojeg, bandar, oe = _kalapa_case()
    manual_pair_many_banks([ojeg, bandar], oe)

    delete_import_batch(bandar.import_batch_id)

    ojeg.refresh_from_db()
    oe.refresh_from_db()
    assert ojeg.match_status == MatchStatus.UNMATCHED
    assert oe.match_status == MatchStatus.UNMATCHED
    assert not Match.objects.filter(bank_mutation=ojeg, voided_at__isnull=True).exists()


@pytest.mark.django_db
def test_masih_selisih_merge_on_member_mutation_records_diff_on_primary():
    ojeg, bandar, oe = _kalapa_case(otomax_amount="3300000")  # Otomax kurang 29.000
    manual_pair_many_banks([ojeg, bandar], oe)
    top_up = _otomax("TARTUN QR BULK KALAPA SUSULAN", "29000", channel=Channel.MERCHANT_BCA, book_date=SEP4)

    m = manual_pair_transactions(bank_mutation=bandar, otomax_entry=top_up)

    assert m.amount_diff == Decimal("0.00")
    assert not _open_discs(kind=DiscrepancyKind.AMOUNT_DIFF).exists()


@pytest.mark.django_db
def test_pending_settle_view_combines_selected_bank_ids(auth_client):
    ojeg, bandar, oe = _kalapa_case()
    other = _otomax("TARTUN QR BULK LAIN", "100000", channel=Channel.MERCHANT_BCA, book_date=SEP4)

    # beberapa mutasi DAN beberapa entri sekaligus -> ditolak, tidak ada pasangan dibuat
    auth_client.post(
        "/manual-match/",
        {"bank_ids": [ojeg.pk, bandar.pk], "otomax_ids": [oe.pk, other.pk], "book_date": SEP4.isoformat()},
    )
    assert not Match.objects.filter(voided_at__isnull=True).exists()

    auth_client.post(
        "/manual-match/",
        {"bank_ids": [ojeg.pk, bandar.pk], "otomax_id": oe.pk, "book_date": SEP4.isoformat(), "next_url": "/"},
    )
    m = Match.objects.get(otomax_entry=oe, voided_at__isnull=True)
    assert m.bank_mutations.count() == 2

    matches_page = auth_client.get("/matches/", {"d": SEP3.isoformat()}).content.decode()
    assert "(+1 mutasi)" in matches_page
    assert "2 Mutasi Gabungan" in matches_page

    tagged = auth_client.get("/review-manual/", {"d": SEP3.isoformat(), "tab": "tagged"}).content.decode()
    assert tagged.count("Lawan: X") == 2  # mutasi utama & anggota gabungan sama-sama menampilkan lawannya
