import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.core.models import AppSettings

User = get_user_model()


@pytest.fixture
def staff_client():
    client = Client()
    user = User.objects.create_superuser(username="admin", password="password123")
    client.force_login(user)
    return client


@pytest.fixture
def operator_client():
    client = Client()
    user = User.objects.create_user(username="operator", password="password123")
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_app_settings_load_seeds_from_django_setting_on_first_call(settings):
    settings.BUSINESS_MONTH_START_DAY = 29
    obj = AppSettings.load()
    assert obj.business_month_start_day == 29
    assert AppSettings.objects.count() == 1


@pytest.mark.django_db
def test_app_settings_is_a_singleton():
    first = AppSettings.load()
    first.business_month_start_day = 15
    first.save(update_fields=["business_month_start_day"])

    second = AppSettings.load()
    assert second.pk == first.pk == 1
    assert second.business_month_start_day == 15
    assert AppSettings.objects.count() == 1


@pytest.mark.django_db
def test_app_settings_page_requires_staff(operator_client):
    res = operator_client.get("/pengaturan/")
    # user_passes_test redirects non-staff instead of 200
    assert res.status_code in (302, 403)


@pytest.mark.django_db
def test_app_settings_page_renders_for_staff(staff_client):
    res = staff_client.get("/pengaturan/")
    assert res.status_code == 200
    assert "Siklus Bulan Bisnis" in res.content.decode()


@pytest.mark.django_db
def test_update_app_settings_action_saves_valid_value(staff_client):
    res = staff_client.post("/pengaturan/simpan/", {"business_month_start_day": "29"})
    assert res.status_code == 302
    assert AppSettings.load().business_month_start_day == 29


@pytest.mark.django_db
def test_update_app_settings_action_rejects_out_of_range_value(staff_client):
    AppSettings.load()  # pastikan baris awal ada dgn nilai default 1
    res = staff_client.post("/pengaturan/simpan/", {"business_month_start_day": "40"})
    assert res.status_code == 302
    assert AppSettings.load().business_month_start_day == 1  # tidak berubah


@pytest.mark.django_db
def test_app_settings_date_tolerance_defaults():
    from apps.core.enums import Channel

    obj = AppSettings.load()
    assert obj.bri_date_tolerance_min == 1
    assert obj.bri_date_tolerance_max == 1
    assert obj.merchant_bca_date_tolerance_min == 1
    assert obj.merchant_bca_date_tolerance_max == 2

    tol = obj.get_bank_date_tolerance()
    assert tol[Channel.BRI] == (-1, 1)
    assert tol[Channel.BCA] == (-1, 1)
    assert tol[Channel.MANDIRI] == (-1, 1)
    assert tol[Channel.MERCHANT_BCA] == (-1, 2)


@pytest.mark.django_db
def test_update_tolerance_settings_action_saves_valid_values(staff_client):
    from apps.core.enums import Channel

    payload = {
        "setting_type": "tolerance",
        "bri_date_tolerance_min": "2",
        "bri_date_tolerance_max": "3",
        "bca_date_tolerance_min": "0",
        "bca_date_tolerance_max": "1",
        "mandiri_date_tolerance_min": "1",
        "mandiri_date_tolerance_max": "2",
        "merchant_bca_date_tolerance_min": "3",
        "merchant_bca_date_tolerance_max": "4",
    }
    res = staff_client.post("/pengaturan/simpan/", payload)
    assert res.status_code == 302

    updated = AppSettings.load()
    assert updated.bri_date_tolerance_min == 2
    assert updated.bri_date_tolerance_max == 3
    assert updated.get_bank_date_tolerance()[Channel.BRI] == (-2, 3)
    assert updated.get_bank_date_tolerance()[Channel.MERCHANT_BCA] == (-3, 4)


@pytest.mark.django_db
def test_update_tolerance_settings_action_rejects_out_of_range(staff_client):
    payload = {
        "setting_type": "tolerance",
        "bri_date_tolerance_min": "35",  # max 30
        "bri_date_tolerance_max": "1",
        "bca_date_tolerance_min": "1",
        "bca_date_tolerance_max": "1",
        "mandiri_date_tolerance_min": "1",
        "mandiri_date_tolerance_max": "1",
        "merchant_bca_date_tolerance_min": "1",
        "merchant_bca_date_tolerance_max": "2",
    }
    res = staff_client.post("/pengaturan/simpan/", payload)
    assert res.status_code == 302
    assert AppSettings.load().bri_date_tolerance_min == 1  # tidak berubah


@pytest.mark.django_db
def test_ref_match_uses_custom_tolerance_from_app_settings():
    from datetime import date
    from decimal import Decimal

    from apps.core.enums import Channel, MatchStatus, OtomaxCategory
    from apps.ingest.models import BankMutation, ImportBatch, OtomaxEntry
    from apps.recon.engine.ref_match import _match_ref

    bd = date(2026, 9, 10)
    batch = ImportBatch.objects.create(
        channel=Channel.BRI, book_date=bd, source_filename="b", file_hash="h"
    )
    b = BankMutation.objects.create(
        import_batch=batch,
        channel=Channel.BRI,
        book_date=bd,
        description_raw="TRF REF123",
        ref_normalized="REF123",
        ref_core="REF123",
        amount=Decimal("100000"),
        row_hash="b1",
    )
    # Otomax di H-2 (tanggal 8 Sep)
    o = OtomaxEntry.objects.create(
        import_batch=batch,
        book_date=date(2026, 9, 8),
        channel_hint=Channel.BRI,
        category=OtomaxCategory.TOPUP_TARTUN,
        description_raw="TARTUN REF123",
        ref_normalized="REF123",
        ref_core="REF123",
        amount=Decimal("100000"),
        row_hash="o1",
    )

    # 1. Dengan default tolerance (H-1 s/d H+1), Otomax di H-2 tidak boleh kena match
    stats = _match_ref(bd, Channel.BRI)
    assert stats.matched == 0
    b.refresh_from_db()
    assert b.match_status == MatchStatus.UNMATCHED

    # 2. Ubah AppSettings jadi H-2
    app_settings = AppSettings.load()
    app_settings.bri_date_tolerance_min = 2
    app_settings.save()

    # Sekarang harus cocok (dan karena beda tanggal, berstatus usulan)
    stats2 = _match_ref(bd, Channel.BRI)
    assert stats2.matched == 1
    b.refresh_from_db()
    o.refresh_from_db()
    assert b.match_status == MatchStatus.MATCHED
    assert o.match_status == MatchStatus.MATCHED

