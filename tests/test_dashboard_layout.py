"""Dashboard (tahap 2 redesign): alur kerja harian dan rekap per bank."""

import hashlib
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.core.enums import Channel, MatchStatus
from apps.ingest.models import BankMutation, ImportBatch
from apps.recon.models import ReconDay

User = get_user_model()
BD = date(2026, 9, 16)


def _h(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _bank(desc, amount, channel=Channel.BRI, status=MatchStatus.UNMATCHED):
    batch = ImportBatch.objects.create(channel=channel, book_date=BD, source_filename="t", file_hash=_h(desc))
    return BankMutation.objects.create(
        import_batch=batch,
        channel=channel,
        book_date=BD,
        description_raw=desc,
        ref_normalized=desc,
        amount=Decimal(amount),
        match_status=status,
        row_hash=_h("b", desc),
    )


@pytest.fixture
def client_op():
    client = Client()
    client.force_login(User.objects.create_user(username="operator", password="password123"))
    return client


@pytest.mark.django_db
def test_hari_kosong_langkah_pertama_upload(client_op):
    res = client_op.get("/", {"d": BD.isoformat()})
    html = res.content.decode()
    assert res.context["engine_ran"] is False
    assert res.context["matched_rate"] is None
    assert "Upload Mutasi / Otomax" in html
    assert "Jalankan Matching Engine" in html
    assert "Belum dijalankan" in html


@pytest.mark.django_db
def test_rekap_per_bank_tingkat_cocok_dan_porsi(client_op):
    _bank("A", "750000", status=MatchStatus.MATCHED)
    _bank("B", "250000")  # BRI: 75% cocok
    _bank("C", "1000000", channel=Channel.BCA, status=MatchStatus.MANUAL)  # BCA: 100%

    res = client_op.get("/", {"d": BD.isoformat()})
    rows = {r["channel"]: r for r in res.context["bank_rows"]}

    assert rows[Channel.BRI]["rate"] == 75
    assert rows[Channel.BRI]["share"] == 50
    assert rows[Channel.BCA]["rate"] == 100
    assert rows[Channel.MANDIRI]["rate"] is None  # tanpa uang masuk -> tanpa persen
    assert res.context["bank_count"] == 3
    assert res.context["remaining_count"] == 1  # 1 mutasi BRI belum cocok
    assert "Merchant BCA (QRIS)" in res.content.decode()


@pytest.mark.django_db
def test_hari_ditutup_menawarkan_buka_kembali(client_op):
    ReconDay.objects.create(book_date=BD, locked=True)
    html = client_op.get("/", {"d": BD.isoformat()}).content.decode()
    assert "Buka Kembali Buku" in html
    assert "Tutup Buku Harian" not in html
