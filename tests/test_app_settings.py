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
