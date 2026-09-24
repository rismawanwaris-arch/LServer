from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.catalog.models import ExclusionCategory, ExclusionRule, ExclusionTarget
from apps.core.enums import Channel, MatchStatus
from apps.ingest.models import BankMutation, ExcludedTransaction, ImportBatch
from apps.ingest.services import apply_exclusion_rules_retroactive, import_file
from apps.recon.engine import run_match
from apps.recon.models import Match

User = get_user_model()
BD = date(2026, 9, 12)


@pytest.fixture
def auth_client():
    client = Client()
    user = User.objects.create_superuser(username="admin_rules", password="password123")
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_exclusion_rule_matching_logic():
    rule = ExclusionRule.objects.create(
        name="Biaya Admin",
        keywords="BIAYA ADM, ADM BULANAN",
        target=ExclusionTarget.BANK,
        channel=Channel.BRI,
        category=ExclusionCategory.BIAYA_ADMIN,
        active=True,
    )
    assert rule.matches("BIAYA ADM BULANAN", channel=Channel.BRI, is_bank=True)
    assert rule.matches("potongan adm bulanan", channel=Channel.BRI, is_bank=True)
    # Channel beda
    assert not rule.matches("BIAYA ADM BULANAN", channel=Channel.BCA, is_bank=True)
    # Target beda
    assert not rule.matches("BIAYA ADM BULANAN", channel=Channel.BRI, is_bank=False)

    # Nonaktif
    rule.active = False
    assert not rule.matches("BIAYA ADM BULANAN", channel=Channel.BRI, is_bank=True)


@pytest.mark.django_db
def test_import_file_separates_excluded_transaction():
    ExclusionRule.objects.create(
        name="Simatech Transfer",
        keywords="SIMATECH",
        target=ExclusionTarget.BANK,
        channel=Channel.BRI,
        category=ExclusionCategory.TRANSFER_INTERNAL,
        active=True,
    )

    bri_csv = (
        '"ID","NOREK","TGL_TRAN","MUTASI_DEBET","MUTASI_KREDIT","GLSIGN","TRREMK","REMARK_CUSTOM"\n'
        '"1","215401000596563","2026-09-12 06:14:36","200000680.00","0.00","Db",'
        '"NBMB SYAIFUL TO PT SIMATECH SUMBE","Transfer Ke PT Simatech Sumbe via BRImo"\n'
        '"2","215401000596563","2026-09-12 06:27:53","0.00","200000.00","Cr",'
        '"ATMLTRPRM 01893 000001015 21540100059656","ATMLTRPRM 01893 000001015 21540100059656"\n'
    )

    batch = import_file(
        channel=Channel.BRI,
        content=bri_csv.encode("utf-8"),
        book_date=BD,
        filename="bri_test.csv",
    )

    # Baris 1 mengandung SIMATECH -> harus masuk ExcludedTransaction
    assert batch.excluded_count == 1
    assert batch.row_count == 2
    assert ExcludedTransaction.objects.filter(import_batch=batch).count() == 1
    ex = ExcludedTransaction.objects.get(import_batch=batch)
    assert "Simatech" in ex.description_raw
    assert ex.category == ExclusionCategory.TRANSFER_INTERNAL

    # Baris 2 normal -> masuk BankMutation
    assert BankMutation.objects.filter(import_batch=batch).count() == 1
    bm = BankMutation.objects.get(import_batch=batch)
    assert bm.amount == Decimal("200000.00")

    # Engine match tidak boleh menyentuh ExcludedTransaction
    run_match(BD)
    assert Match.objects.count() == 0
    assert not Match.objects.filter(bank_mutation_id=ex.id).exists()


@pytest.mark.django_db
def test_apply_exclusion_rules_retroactive():
    d = date(2026, 9, 12)
    batch = ImportBatch.objects.create(channel=Channel.BRI, book_date=d, source_filename="b.csv", file_hash="h1")
    bm1 = BankMutation.objects.create(
        import_batch=batch,
        book_date=d,
        channel=Channel.BRI,
        amount=Decimal("15000"),
        description_raw="BIAYA ADM BULANAN",
        ref_normalized="BIAYA ADM",
        row_hash="bm_adm",
        match_status=MatchStatus.UNMATCHED,
    )
    bm2 = BankMutation.objects.create(
        import_batch=batch,
        book_date=d,
        channel=Channel.BRI,
        amount=Decimal("500000"),
        description_raw="TRANSFER BI-FAST CECEP",
        ref_normalized="TRANSFER BI-FAST",
        row_hash="bm_ok",
        match_status=MatchStatus.UNMATCHED,
    )

    ExclusionRule.objects.create(
        name="Biaya Admin",
        keywords="BIAYA ADM",
        target=ExclusionTarget.BANK,
        category=ExclusionCategory.BIAYA_ADMIN,
        active=True,
    )

    res = apply_exclusion_rules_retroactive(book_date=d)
    assert res["bank_moved"] == 1
    assert not BankMutation.objects.filter(id=bm1.id).exists()
    assert BankMutation.objects.filter(id=bm2.id).exists()

    assert ExcludedTransaction.objects.filter(row_hash="bm_adm").count() == 1
    batch.refresh_from_db()
    assert batch.excluded_count == 1


@pytest.mark.django_db
def test_exclusion_rules_dashboard_views(auth_client):
    res = auth_client.get("/rules/")
    assert res.status_code == 200
    assert "Pengaturan Aturan Filter" in res.content.decode()

    # Add rule
    res_add = auth_client.post(
        "/rules/add/",
        {
            "book_date": "2026-09-12",
            "name": "Pajak Bunga",
            "keywords": "PAJAK, BUNGA",
            "target": "BANK",
            "channel": "",
            "category": "PAJAK_BUNGA",
            "apply_now": "0",
        },
    )
    assert res_add.status_code == 302
    rule = ExclusionRule.objects.get(name="Pajak Bunga")
    assert rule.active is True

    # Toggle rule
    auth_client.post(f"/rules/toggle/{rule.id}/", {"book_date": "2026-09-12"})
    rule.refresh_from_db()
    assert rule.active is False

    # Delete rule
    auth_client.post(f"/rules/delete/{rule.id}/", {"book_date": "2026-09-12"})
    assert not ExclusionRule.objects.filter(id=rule.id).exists()


@pytest.mark.django_db
def test_apply_exclusion_rules_retroactive_with_discrepancy():
    """Jika transaksi belum cocok sudah kadung dicatat Discrepancy oleh run_match,
    apply_exclusion_rules_retroactive harus tetap sukses (menghapus Discrepancy tanpa ProtectedError)."""
    d = date(2026, 9, 12)
    batch = ImportBatch.objects.create(channel=Channel.BRI, book_date=d, source_filename="b.csv", file_hash="h_disc")
    bm = BankMutation.objects.create(
        import_batch=batch,
        book_date=d,
        channel=Channel.BRI,
        amount=Decimal("15000"),
        description_raw="BIAYA ADM BULANAN",
        ref_normalized="BIAYA ADM",
        row_hash="bm_with_disc",
        match_status=MatchStatus.UNMATCHED,
    )
    run_match(d)
    assert bm.discrepancies.filter(status="OPEN").exists()

    ExclusionRule.objects.create(
        name="Biaya Admin",
        keywords="BIAYA ADM",
        target=ExclusionTarget.BANK,
        category=ExclusionCategory.BIAYA_ADMIN,
        active=True,
    )

    res = apply_exclusion_rules_retroactive(book_date=d)
    assert res["bank_moved"] == 1
    assert not BankMutation.objects.filter(id=bm.id).exists()
    assert ExcludedTransaction.objects.filter(row_hash="bm_with_disc").exists()

