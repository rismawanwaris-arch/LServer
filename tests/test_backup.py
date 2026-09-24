from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.catalog.models import ExclusionRule, MerchantMap, Reseller
from apps.core.enums import Channel
from apps.core.models import AppSettings
from apps.ingest.models import OtomaxEntry
from apps.recon.backup import (
    RestoreError,
    generate_backup_json,
    list_safety_backups,
    preview_backup_file,
    restore_from_json,
)
from apps.recon.close import close_day
from apps.recon.engine import run_match
from apps.recon.models import Adjustment, Discrepancy, Match

from .test_engine import _bank, _otomax

User = get_user_model()
BD = date(2026, 9, 5)


@pytest.fixture(autouse=True)
def _isolated_media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


@pytest.fixture
def auth_client():
    client = Client()
    user = User.objects.create_superuser(username="admin_backup", password="password123")
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_generate_and_restore_round_trip():
    reseller = Reseller.objects.create(code="RX1", name="Reseller X")
    MerchantMap.objects.create(merchant_id="999999999", merchant_name="Toko X", reseller=reseller)
    ExclusionRule.objects.create(name="Biaya Admin", keywords="BIAYA ADM")
    AppSettings.load()
    _bank("ATMLTRPRM 01884 000001039 21540100059656", "550000")
    _otomax("TARTUN EDC BRI ATMLTRPRM 01884 000001039 21540100059656", "550000")
    _bank("DANA20260905034895588601ASEPKURNIAWA", "1600000", channel=Channel.BRI)
    run_match(BD)
    close_day(BD, force=True)
    assert Match.objects.count() == 1
    assert Discrepancy.objects.exists()
    match = Match.objects.first()
    disc = Discrepancy.objects.first()
    Adjustment.objects.create(book_date=BD, discrepancy=disc, amount=100, reason="koreksi manual")

    content = generate_backup_json()

    before_match_pk = match.pk
    before_reseller_pk = reseller.pk

    # Ubah data SETELAH backup diambil, untuk membuktikan restore benar-benar
    # menimpanya kembali ke isi file, bukan cuma no-op.
    reseller.name = "Nama Salah Setelah Backup"
    reseller.save(update_fields=["name"])

    result = restore_from_json(content, make_safety_backup=False)

    assert Reseller.objects.filter(pk=before_reseller_pk, code="RX1", name="Reseller X").exists()
    assert MerchantMap.objects.filter(merchant_id="999999999").exists()
    assert ExclusionRule.objects.filter(name="Biaya Admin").exists()
    assert AppSettings.objects.filter(pk=1).exists()
    restored_match = Match.objects.get(pk=before_match_pk)
    assert restored_match.amount_bank == match.amount_bank
    assert Adjustment.objects.filter(discrepancy=disc, amount=100).exists()
    assert result["counts"]["Match"] == 1
    assert result["safety_backup_path"] is None


@pytest.mark.django_db
def test_restore_handles_self_referencing_net_pair():
    """OtomaxEntry.net_pair menunjuk baris lain di tabel yang sama (dua arah) --
    restore harus tetap benar walau baris pasangannya belum ada saat baris pertama
    diproses (lihat 2-pass di apps/recon/backup.py::restore_from_json)."""
    original = _otomax("TARTUN EDC BRI ABC", "100000", channel=Channel.BRI)
    reversal = _otomax("TARTUN EDC BRI ABC REV", "-100000", channel=Channel.BRI)
    original.net_pair = reversal
    original.save(update_fields=["net_pair"])
    reversal.net_pair = original
    reversal.save(update_fields=["net_pair"])

    content = generate_backup_json()
    OtomaxEntry.objects.all().delete()
    restore_from_json(content, make_safety_backup=False)

    restored_original = OtomaxEntry.objects.get(pk=original.pk)
    restored_reversal = OtomaxEntry.objects.get(pk=reversal.pk)
    assert restored_original.net_pair_id == reversal.pk
    assert restored_reversal.net_pair_id == original.pk


@pytest.mark.django_db
def test_restore_creates_safety_backup_before_wipe():
    Reseller.objects.create(code="RX2", name="Reseller Sebelum Restore")
    content = generate_backup_json()

    assert list_safety_backups() == []
    result = restore_from_json(content, make_safety_backup=True)

    assert result["safety_backup_path"] is not None
    backups = list_safety_backups()
    assert len(backups) == 1
    saved_content = backups[0].read_text(encoding="utf-8")
    assert "RX2" in saved_content


@pytest.mark.django_db
def test_preview_backup_file_counts_without_touching_db():
    Reseller.objects.create(code="RX3", name="Preview Test")
    content = generate_backup_json()

    reseller_count_before = Reseller.objects.count()
    counts = preview_backup_file(content)

    assert counts["Reseller"] == reseller_count_before
    # Preview tidak mengubah apa pun di database.
    assert Reseller.objects.count() == reseller_count_before


@pytest.mark.django_db
def test_preview_backup_file_rejects_garbage():
    with pytest.raises(RestoreError):
        preview_backup_file("bukan json valid {{{")


@pytest.mark.django_db
def test_preview_backup_file_rejects_empty_list():
    with pytest.raises(RestoreError):
        preview_backup_file("[]")


@pytest.mark.django_db
def test_backup_page_requires_staff(auth_client):
    User.objects.create_user(username="biasa", password="password123")
    client = Client()
    client.force_login(User.objects.get(username="biasa"))
    res = client.get("/backup/")
    assert res.status_code == 302  # ditolak staff_only, redirect ke login


@pytest.mark.django_db
def test_download_backup_action(auth_client):
    Reseller.objects.create(code="RX4", name="Download Test")
    res = auth_client.get("/backup/download/")
    assert res.status_code == 200
    assert res["Content-Type"] == "application/json"
    assert b"RX4" in res.content


@pytest.mark.django_db
def test_restore_full_web_flow_preview_then_confirm(auth_client):
    Reseller.objects.create(code="RX5", name="Web Flow Test")
    content = generate_backup_json()
    from io import BytesIO

    from django.core.files.uploadedfile import InMemoryUploadedFile

    upload = InMemoryUploadedFile(
        BytesIO(content.encode()), "file", "backup.json", "application/json", len(content), None
    )
    res = auth_client.post("/backup/restore/preview/", {"file": upload})
    assert res.status_code == 200
    assert "token" in res.context
    token = res.context["token"]
    assert res.context["total"] > 0

    Reseller.objects.all().delete()
    res = auth_client.post("/backup/restore/confirm/", {"token": token})
    assert res.status_code == 302
    assert Reseller.objects.filter(code="RX5").exists()


@pytest.mark.django_db
def test_restore_confirm_rejects_unknown_token(auth_client):
    res = auth_client.post("/backup/restore/confirm/", {"token": "does-not-exist"})
    assert res.status_code == 302
