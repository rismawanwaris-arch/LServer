import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.catalog.models import MerchantMap, Reseller

User = get_user_model()


@pytest.fixture
def auth_client():
    client = Client()
    user = User.objects.create_superuser(username="admin_reseller", password="password123")
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_seed_migration_populates_known_codes():
    """Migrasi catalog/0003_seed_reseller_codes harus sudah keisi otomatis di semua
    lingkungan test (dan produksi, lewat `migrate` otomatis saat deploy)."""
    assert Reseller.objects.count() == 41
    plc131 = Reseller.objects.get(code="PLC131")
    assert plc131.name == "PLC CIPADUNG 2"
    assert plc131.active is True


@pytest.mark.django_db
def test_reseller_list_view(auth_client):
    res = auth_client.get("/reseller/")
    assert res.status_code == 200
    content = res.content.decode()
    assert "Kode Reseller" in content
    assert "PLC131" in content
    assert "PLC CIPADUNG 2" in content


@pytest.mark.django_db
def test_add_reseller_action(auth_client):
    res = auth_client.post("/reseller/add/", {"code": "plc999", "name": "PLC Outlet Baru"})
    assert res.status_code == 302
    r = Reseller.objects.get(code="PLC999")  # kode disimpan uppercase
    assert r.name == "PLC Outlet Baru"
    assert r.active is True


@pytest.mark.django_db
def test_add_reseller_action_rejects_duplicate_code(auth_client):
    res = auth_client.post("/reseller/add/", {"code": "PLC131", "name": "Duplikat"})
    assert res.status_code == 302
    # Baris asli tidak berubah, tidak ada baris kedua dibuat
    assert Reseller.objects.filter(code="PLC131").count() == 1
    assert Reseller.objects.get(code="PLC131").name == "PLC CIPADUNG 2"


@pytest.mark.django_db
def test_edit_reseller_action(auth_client):
    r = Reseller.objects.get(code="PLC131")
    res = auth_client.post(f"/reseller/edit/{r.pk}/", {"code": "PLC131", "name": "PLC Cipadung Baru"})
    assert res.status_code == 302
    r.refresh_from_db()
    assert r.name == "PLC Cipadung Baru"


@pytest.mark.django_db
def test_toggle_reseller_action(auth_client):
    r = Reseller.objects.get(code="PLC131")
    assert r.active is True
    auth_client.post(f"/reseller/toggle/{r.pk}/")
    r.refresh_from_db()
    assert r.active is False
    auth_client.post(f"/reseller/toggle/{r.pk}/")
    r.refresh_from_db()
    assert r.active is True


@pytest.mark.django_db
def test_delete_reseller_action(auth_client):
    r = Reseller.objects.get(code="PLC131")
    res = auth_client.post(f"/reseller/delete/{r.pk}/")
    assert res.status_code == 302
    assert not Reseller.objects.filter(code="PLC131").exists()


@pytest.mark.django_db
def test_delete_reseller_action_blocked_when_used_by_merchant_map(auth_client):
    r = Reseller.objects.get(code="PLC131")
    MerchantMap.objects.create(merchant_id="123456789", merchant_name="Test QRIS", reseller=r)

    res = auth_client.post(f"/reseller/delete/{r.pk}/")
    assert res.status_code == 302
    assert Reseller.objects.filter(code="PLC131").exists()  # tidak jadi terhapus
