"""Usulan Pencocokan: pasangan dari mesin yang rawan jadi pencocokan "hantu" -- gabungan
beberapa tiket QRIS (kasus nyata 819.000 + 181.000 dipasangkan ke 1.000.000), cocok nominal
saja tanpa cek nama outlet, kemiripan teks (fuzzy), dan susulan lintas hari yang teksnya
cuma mirip -- dibuat sebagai usulan (needs_review) yang wajib disetujui/ditolak operator,
dan tutup buku ditolak selama masih ada."""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.catalog.models import MerchantMap, Reseller
from apps.core.enums import Channel, DiscrepancyKind, DiscrepancyStatus, MatchStatus
from apps.ingest.models import BankMutation
from apps.recon.carry import carry_forward
from apps.recon.close import DayNotReady, close_day
from apps.recon.engine import run_match
from apps.recon.models import Adjustment, Discrepancy, Match
from apps.recon.resolve import approve_match, unpair_match

from .test_bayar_telat_koreksi import AUG30, SAHIDIN_BANK, SEP3, _payment
from .test_engine import BD, _bank, _batch, _otomax


@pytest.fixture
def operator(db):
    return User.objects.create_superuser(username="op_usulan", password="password123")


@pytest.fixture
def auth_client(operator):
    client = Client()
    client.force_login(operator)
    return client


def _qris_bank(amount, outlet="QRIS CIKADUT 2 CELL", merchant_id="004769148"):
    return BankMutation.objects.create(
        import_batch=_batch(Channel.MERCHANT_BCA),
        channel=Channel.MERCHANT_BCA,
        book_date=BD,
        description_raw=outlet,
        ref_normalized="QRIS",
        outlet_name=outlet,
        amount=Decimal(amount),
        external_ref=merchant_id,
        row_hash=f"qb-{outlet}-{amount}",
    )


def _active(bank):
    return Match.objects.get(bank_mutation=bank, voided_at__isnull=True)


# --- Fuzzy ---------------------------------------------------------------------------


def _fuzzy_case():
    b = _bank("TRANSFER DARI SITI AMINAH VIA BRIMO", "750000")
    _otomax("TARTUN TF BRI TRANSFER DARI SITI AMINA VIA BRIMO", "750000")
    run_match(BD)
    return b


@pytest.mark.django_db
def test_fuzzy_match_is_a_proposal_that_blocks_day_close(operator):
    b = _fuzzy_case()
    m = _active(b)
    assert m.needs_review
    assert "fuzzy" in m.review_reason.lower()

    with pytest.raises(DayNotReady):
        close_day(BD)

    approve_match(m, user=operator)
    m.refresh_from_db()
    assert not m.needs_review
    assert m.reviewed_by == operator
    assert m.reviewed_at is not None
    close_day(BD)  # tidak ada lagi usulan yang menunggu


@pytest.mark.django_db
def test_rejected_proposal_is_not_proposed_again():
    b = _fuzzy_case()
    unpair_match(_active(b))  # operator menolak usulan

    run_match(BD)

    b.refresh_from_db()
    assert b.match_status == MatchStatus.UNMATCHED
    assert not Match.objects.filter(bank_mutation=b, voided_at__isnull=True).exists()


# --- QRIS ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_qris_single_ticket_identified_by_merchant_map_stays_final():
    r = Reseller.objects.create(code="CKD2", name="Cikadut 2")
    MerchantMap.objects.create(merchant_id="004769148", reseller=r)
    b = _qris_bank("1000000")
    _otomax("TARTUN QR BULK TGL 05-SEP-2026", "1000000", channel=Channel.MERCHANT_BCA, reseller=r)

    run_match(BD)

    assert not _active(b).needs_review


@pytest.mark.django_db
def test_qris_combining_two_tickets_is_a_proposal_and_learns_mapping_only_on_approval(operator):
    """Kasus nyata: 819.000 + 181.000 digabung otomatis ke settlement 1.000.000."""
    r = Reseller.objects.create(code="CKD2", name="Cikadut 2")
    b = _qris_bank("1000000", outlet="QRIS CIKADUT 2 CELL", merchant_id="004769148")
    _otomax("TARTUN QR BULK TGL 05-SEP-2026", "819000", channel=Channel.MERCHANT_BCA, reseller=r)
    _otomax("TARTUN QR BULK CIKADUT TGL 05-SEP-2026", "181000", channel=Channel.MERCHANT_BCA, reseller=r)

    run_match(BD)

    m = _active(b)
    assert m.needs_review
    assert "Gabungan 2 tiket" in m.review_reason
    assert m.otomax_entries.count() == 2
    assert not MerchantMap.objects.filter(merchant_id="004769148").exists()

    approve_match(m, user=operator)
    assert MerchantMap.objects.filter(merchant_id="004769148", reseller=r).exists()


@pytest.mark.django_db
def test_qris_matched_on_amount_only_is_a_proposal():
    other = Reseller.objects.create(code="ZZ", name="Outlet Lain Sama Sekali")
    b = _qris_bank("2409500", outlet="QRIS ALFA 5 CELL", merchant_id="004767954")
    _otomax("TARTUN QR BULK TGL 05-SEP-2026", "2409500", channel=Channel.MERCHANT_BCA, reseller=other)

    run_match(BD)

    m = _active(b)
    assert m.needs_review
    assert "nominal saja" in m.review_reason


# --- Susulan lintas hari dengan teks mirip --------------------------------------------

SAHIDIN_OTOMAX_TANPA_NOMOR = "BAYAR KE MANDIRI MCM InhouseTrf DARI SAHIDIN TGL 30/AGS/2026"


def _similar_carry_case():
    bm = _bank(SAHIDIN_BANK, "500000", channel=Channel.MANDIRI, book_date=AUG30)
    run_match(AUG30)
    stale = Discrepancy.objects.get(bank_mutation=bm, kind=DiscrepancyKind.BANK_ONLY)
    oe = _payment(SAHIDIN_OTOMAX_TANPA_NOMOR, "500000", SEP3)
    run_match(SEP3)
    resolved = carry_forward(SEP3)
    return bm, oe, stale, resolved


@pytest.mark.django_db
def test_similar_late_entry_becomes_proposal_without_touching_old_discrepancy():
    bm, oe, stale, resolved = _similar_carry_case()

    assert resolved == 0  # usulan tidak dihitung "selisih lama ditutup"
    m = _active(bm)
    assert m.needs_review
    assert m.otomax_entry_id == oe.id
    stale.refresh_from_db()
    assert stale.status == DiscrepancyStatus.OPEN  # Adjustment belum boleh dibuat
    assert not Adjustment.objects.filter(discrepancy=stale).exists()


@pytest.mark.django_db
def test_approving_similar_late_entry_settles_old_and_new_discrepancies(operator):
    bm, oe, stale, _ = _similar_carry_case()

    approve_match(_active(bm), user=operator)

    stale.refresh_from_db()
    assert stale.status == DiscrepancyStatus.RESOLVED
    assert Adjustment.objects.filter(discrepancy=stale, book_date=AUG30).exists()
    assert not Discrepancy.objects.filter(otomax_entry=oe, status=DiscrepancyStatus.OPEN).exists()


@pytest.mark.django_db
def test_rejecting_similar_late_entry_keeps_old_discrepancy_open_and_is_not_reproposed():
    bm, oe, stale, _ = _similar_carry_case()

    unpair_match(_active(bm))
    run_match(SEP3)
    carry_forward(SEP3)

    stale.refresh_from_db()
    assert stale.status == DiscrepancyStatus.OPEN
    assert not Match.objects.filter(bank_mutation=bm, voided_at__isnull=True).exists()


@pytest.mark.django_db
def test_ambiguous_similar_candidates_are_not_proposed():
    bm = _bank(SAHIDIN_BANK, "500000", channel=Channel.MANDIRI, book_date=AUG30)
    run_match(AUG30)
    _payment(SAHIDIN_OTOMAX_TANPA_NOMOR, "500000", SEP3, reseller="PLC A")
    _payment("BAYAR KE MANDIRI MCM InhouseTrf DARI SAHIDIN 991", "500000", SEP3, reseller="PLC B")

    carry_forward(SEP3)

    assert not Match.objects.filter(bank_mutation=bm, voided_at__isnull=True).exists()


# --- Dashboard ------------------------------------------------------------------------


@pytest.mark.django_db
def test_review_tab_lists_only_proposals_and_approve_endpoint(auth_client):
    fuzzy_bank = _fuzzy_case()
    exact = _bank("ATMLTRPRM 01884 000001039 21540100059656", "550000")
    _otomax("TARTUN EDC BRI ATMLTRPRM 01884 000001039 21540100059656", "550000")
    run_match(BD)
    assert not _active(exact).needs_review

    res = auth_client.get("/matches/", {"d": BD.isoformat(), "tab": "review"})
    assert res.context["review_count"] == 1
    assert [m.bank_mutation_id for m in res.context["page_obj"].object_list] == [fuzzy_bank.id]

    day = auth_client.get("/", {"d": BD.isoformat()})
    assert day.context["proposal_count"] == 1

    m = _active(fuzzy_bank)
    auth_client.post(f"/matches/approve/{m.id}/", {"next_url": "/"})
    m.refresh_from_db()
    assert not m.needs_review
