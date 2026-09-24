import json
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from apps.core.enums import Channel, MatchStatus, OtomaxCategory
from apps.ingest.models import BankMutation, ImportBatch, OtomaxEntry

BD = date(2026, 9, 5)


@pytest.fixture
def client(settings):
    settings.API_KEY = "test-key"
    c = Client()
    c.defaults["HTTP_X_API_KEY"] = "test-key"
    return c


@pytest.fixture
def sample_data():
    batch_b = ImportBatch.objects.create(channel=Channel.BRI, book_date=BD, source_filename="b.csv", file_hash="h1")
    bm1 = BankMutation.objects.create(
        import_batch=batch_b,
        channel=Channel.BRI,
        book_date=BD,
        description_raw="TRSF DANA20260905034895588601 ASEP",
        ref_normalized="TRSF DANA20260905034895588601 ASEP",
        extracted_tokens=["DANA20260905034895588601", "20260905034895588601"],
        amount=Decimal("1600000.00"),
        row_hash="h_bm1",
    )
    bm2 = BankMutation.objects.create(
        import_batch=batch_b,
        channel=Channel.BRI,
        book_date=BD,
        description_raw="BIAYA ADM BULANAN",
        ref_normalized="BIAYA ADM BULANAN",
        extracted_tokens=[],
        amount=Decimal("-2500.00"),
        row_hash="h_bm2",
    )
    batch_o = ImportBatch.objects.create(channel=Channel.OTOMAX, book_date=BD, source_filename="o.csv", file_hash="h2")
    oe1 = OtomaxEntry.objects.create(
        import_batch=batch_o,
        book_date=BD,
        reseller_name_raw="PLC134",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.BRI,
        description_raw="TARTUN TF BRI DANA20260905034895588601 ASEP",
        ref_normalized="TARTUN TF BRI DANA20260905034895588601 ASEP",
        extracted_tokens=["DANA20260905034895588601", "20260905034895588601"],
        amount=Decimal("1600000.00"),
        row_hash="h_oe1",
    )
    oe2 = OtomaxEntry.objects.create(
        import_batch=batch_o,
        book_date=BD,
        reseller_name_raw="PLC001",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.BRI,
        description_raw="TARTUN EDC BRI 500000",
        ref_normalized="TARTUN EDC BRI 500000",
        extracted_tokens=["500000"],
        amount=Decimal("500000.00"),
        row_hash="h_oe2",
    )
    return bm1, bm2, oe1, oe2


@pytest.mark.django_db
def test_api_reconcile_and_summary(client, sample_data):
    # Run reconciliation
    res = client.post("/api/reconcile/run", {"book_date": "2026-09-05"})
    assert res.status_code == 200
    data = res.json()
    assert data["total_matched"] >= 1

    # Get summary
    res = client.get("/api/reconcile/summary?d=2026-09-05")
    assert res.status_code == 200
    s_data = res.json()
    assert s_data["matched_auto_count"] >= 1
    assert "per_bank" in s_data


@pytest.mark.django_db
def test_api_manual_tag(client, sample_data):
    bm1, bm2, oe1, oe2 = sample_data
    # Create an unmatched bank mutation
    bm_unmatched = BankMutation.objects.create(
        import_batch=bm1.import_batch,
        channel=Channel.BCA,
        book_date=BD,
        description_raw="TARIK TUNAI ATM",
        ref_normalized="TARIK TUNAI ATM",
        amount=Decimal("100000.00"),
        row_hash="h_unmatched",
    )

    res = client.post(
        "/api/reconcile/manual-tag",
        json.dumps({"mutation_id": bm_unmatched.id, "tag": "tarik_tunai", "note": "Uang kas toko"}),
        content_type="application/json",
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["tag_manual"] == "tarik_tunai"

    bm_unmatched.refresh_from_db()
    assert bm_unmatched.match_status == MatchStatus.MANUAL
    assert bm_unmatched.manual_note == "Uang kas toko"


@pytest.mark.django_db
def test_api_reports_export(client, sample_data):
    res = client.get("/api/reports/export?start_date=2026-09-05&end_date=2026-09-05")
    assert res.status_code == 200
    assert "spreadsheetml" in res["Content-Type"]
    assert len(res.content) > 1000


@pytest.mark.django_db
def test_api_security_enforces_api_key(settings, sample_data):
    settings.API_KEY = "super-secret-token"
    c = Client()

    # 1. Tanpa API Key -> 401
    res = c.get("/api/reconcile/summary?d=2026-09-05")
    assert res.status_code == 401
    assert "Autentikasi gagal" in res.json()["error"]

    # 2. API Key salah -> 401
    res = c.get("/api/reconcile/summary?d=2026-09-05", HTTP_X_API_KEY="wrong-key")
    assert res.status_code == 401

    # 3. API Key benar via X-API-KEY -> 200
    res = c.get("/api/reconcile/summary?d=2026-09-05", HTTP_X_API_KEY="super-secret-token")
    assert res.status_code == 200

    # 4. API Key benar via Authorization Bearer -> 200
    res = c.get("/api/reconcile/summary?d=2026-09-05", HTTP_AUTHORIZATION="Bearer super-secret-token")
    assert res.status_code == 200


@pytest.mark.django_db
def test_api_security_allows_session_user(settings, sample_data):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(username="api_user", password="password")
    settings.API_KEY = "super-secret-token"

    c = Client()
    c.force_login(user)
    # Walau API_KEY disetel dan request tanpa header API Key, user login tetap diizinkan
    res = c.get("/api/reconcile/summary?d=2026-09-05")
    assert res.status_code == 200


@pytest.mark.django_db
def test_api_security_rejects_in_production_without_key_or_login(settings, sample_data):
    settings.DEBUG = False
    settings.API_KEY = ""

    c = Client()
    res = c.get("/api/reconcile/summary?d=2026-09-05")
    assert res.status_code == 401
    assert "Akses ditolak" in res.json()["error"]


