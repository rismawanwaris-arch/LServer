"""Aturan kerja yang disepakati dengan operator (review 7 poin):
- Mesin cuma memasangkan final yang identitasnya pasti; "Tartun PLC ↔ Auto Deposit"
  (nominal + kata DEPOSIT saja) jadi usulan.
- Semua outstanding di Pending Settle ikut tercatat di Daftar Selisih, bukan cuma
  kategori tarik tunai.
- Selisih "nominal beda" di Daftar Selisih menunjukkan arahnya.
- Dashboard: kartu Selisih Otomax = isi halaman Pending Settle.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.core.enums import Channel, DiscrepancyKind, MatchStatus, OtomaxCategory
from apps.ingest.models import BankMutation, OtomaxEntry
from apps.recon.carry import carry_forward
from apps.recon.engine import run_match
from apps.recon.models import Discrepancy, Match
from apps.recon.resolve import manual_pair_transactions

from .test_bayar_telat_koreksi import _payment
from .test_engine import BD, _bank, _batch, _otomax


@pytest.fixture
def auth_client(db):
    client = Client()
    client.force_login(User.objects.create_superuser(username="op_aturan", password="password123"))
    return client


@pytest.mark.django_db
def test_tartun_plc_auto_deposit_fallback_is_only_a_proposal():
    b = BankMutation.objects.create(
        import_batch=_batch(Channel.BCA),
        channel=Channel.BCA,
        book_date=BD,
        description_raw="TRSF E-BANKING CR 0309/FTSCY/WS95271 3190000.00  Tartun PLC9999 DEDE SUMPENA B.",
        ref_normalized="TRSF E BANKING CR TARTUN PLC9999 DEDE SUMPENA B",
        ref_core="PLC9999",
        extracted_tokens=["PLC9999"],
        amount=Decimal("3190000"),
        row_hash="bca_plc9999",
    )
    OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=BD,
        reseller_name_raw="PLC BUNISARI",
        amount=Decimal("3190000"),
        description_raw="Auto Deposit BCA 4373433015",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.BCA,
        ref_normalized="AUTO DEPOSIT BCA 4373433015",
        row_hash="oto_bunisari",
    )

    run_match(BD)

    m = Match.objects.get(bank_mutation=b, voided_at__isnull=True)
    assert m.needs_review
    assert "Auto Deposit" in m.review_reason


@pytest.mark.django_db
def test_every_pending_settle_entry_is_listed_in_daftar_selisih():
    lain = _payment("BAYAR QR 260829 04 0166244 TGL 24/AGS/2026", "24000", BD, reseller="PLC BUNISARI")
    stor = _payment("STOR ANDRI TGL 05-SEP-2026", "5000000", BD, reseller="ANDRI")
    admin = _payment("ADMIN TARTUN TGL 05-SEP-2026", "-6500", BD, reseller="PLC X")
    assert lain.category == OtomaxCategory.OTHER and stor.category == OtomaxCategory.STOR_IN

    run_match(BD)

    for o in (lain, stor):
        disc = Discrepancy.objects.get(otomax_entry=o, kind=DiscrepancyKind.OTOMAX_ONLY)
        assert disc.channel == Channel.OTOMAX  # tidak jelas bank mana -> bukan dilabeli BRI
    assert not Discrepancy.objects.filter(otomax_entry=admin).exists()  # potongan admin bukan outstanding


@pytest.mark.django_db
def test_non_reconcilable_entries_are_never_paired_by_carry_forward():
    stor = _payment("STOR ANDRI TRANSFER BANK", "5000000", BD, reseller="ANDRI")
    run_match(BD)
    later = BD.replace(day=BD.day + 1)
    bank = _bank("STOR ANDRI TRANSFER BANK", "5000000", channel=Channel.BRI, book_date=later)
    run_match(later)

    carry_forward(later)

    bank.refresh_from_db()
    stor.refresh_from_db()
    assert bank.match_status == MatchStatus.UNMATCHED
    assert stor.match_status == MatchStatus.PENDING_SETTLE


@pytest.mark.django_db
def test_amount_diff_shows_direction_in_daftar_selisih(auth_client):
    bank = _bank("QRIS CILENGKRANG 3 CELL 004769151", "2778000", book_date=BD)
    manual_pair_transactions(bank, _otomax("TARTUN TF BRI CILENGKRANG 3", "2788000", book_date=BD))

    html = auth_client.get("/selisih/", {"d": BD.isoformat()}).content.decode()

    assert "nominal beda · Otomax lebih" in html


@pytest.mark.django_db
def test_dashboard_selisih_otomax_card_matches_pending_settle_page(auth_client):
    _otomax("TARTUN TF BRI TANPA PASANGAN", "100000", book_date=BD)
    _payment("BAYAR QR 260905 04 0000001 TGL 05/SEP/2026", "24000", BD, reseller="PLC BUNISARI")
    _payment("ADMIN TARTUN TGL 05-SEP-2026", "-6500", BD, reseller="PLC X")

    res = auth_client.get("/", {"d": BD.isoformat()})

    assert res.context["pending_settle_count"] == 2
    assert res.context["pending_settle_amount"] == Decimal("124000.00")
    assert len(auth_client.get("/pending-settle/", {"d": BD.isoformat()}).context["items"]) == 2
    html = res.content.decode()
    assert "Belum Cocok — Perlu Tindakan" in html and "Aksi Rekonsiliasi" in html
