"""Tahap 3 redesign: Pending Settle & Review Manual pakai tab segmen, status yang
informatif, dan tanpa pemilih tanggal dobel (sudah ada di bilah atas)."""

import hashlib
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.core.enums import Channel, MatchStatus, OtomaxCategory
from apps.ingest.models import BankMutation, ImportBatch, OtomaxEntry

User = get_user_model()
BD = date(2026, 9, 16)


def _h(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _batch(channel):
    return ImportBatch.objects.create(channel=channel, book_date=BD, source_filename="t", file_hash=_h(channel))


def _otomax(desc, amount, reseller, category=OtomaxCategory.OTHER):
    return OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=BD,
        reseller_name_raw=reseller,
        amount=Decimal(amount),
        description_raw=desc,
        category=category,
        match_status=MatchStatus.PENDING_SETTLE,
        row_hash=_h(desc),
    )


@pytest.fixture
def client_op():
    client = Client()
    client.force_login(User.objects.create_user(username="operator", password="password123"))
    return client


@pytest.mark.django_db
def test_pending_settle_status_menunjukkan_lawan_otomax(client_op):
    _otomax("REV Transfer dari PLC128 - PLC PD3", "-450000", "DANI", category=OtomaxCategory.REVERSAL)
    _otomax("REFUND FROM OTO3386 - DANI", "450000", "PLC PD3")
    _otomax("TARTUN EDC BRI TANPA LAWAN", "300000", "PLC X", category=OtomaxCategory.TOPUP_TARTUN)

    html = client_op.get(f"/pending-settle/?d={BD}").content.decode()

    assert html.count("1 lawan Otomax") == 2
    assert "Pending settle" in html  # entri tanpa lawan
    assert 'aria-current="page">\n      Belum Settle' in html
    assert "Ganti Tanggal" not in html
    assert "Total terbuka" in html


@pytest.mark.django_db
def test_review_manual_tab_segmen_tanpa_pemilih_tanggal_dobel(client_op):
    BankMutation.objects.create(
        import_batch=_batch(Channel.BCA),
        channel=Channel.BCA,
        book_date=BD,
        description_raw="TRF MASUK BELUM COCOK",
        ref_normalized="TRF",
        amount=Decimal("125000"),
        row_hash=_h("b"),
    )

    html = client_op.get(f"/review-manual/?d={BD}").content.decode()

    assert "Antrean Review Manual" in html
    assert "Perlu Review" in html and "Sudah Di-tag" in html
    assert "TRF MASUK BELUM COCOK" in html
    assert "Total menggantung" in html
    assert "Ganti Tanggal" not in html
