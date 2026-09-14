from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.core.enums import Channel, ManualTag, MatchStatus
from apps.ingest.models import BankMutation, ImportBatch, OtomaxEntry
from apps.recon.models import Match, ReconDay

User = get_user_model()


@pytest.fixture
def auth_client():
    client = Client()
    user = User.objects.create_superuser(username="admin", password="password123")
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_dashboard_day_view(auth_client):
    res = auth_client.get("/")
    assert res.status_code == 200
    assert "Rekonsiliasi" in res.content.decode()


@pytest.mark.django_db
def test_dashboard_upload_view(auth_client):
    res = auth_client.get("/upload/")
    assert res.status_code == 200
    assert "Upload Data Mentah" in res.content.decode()


@pytest.mark.django_db
def test_dashboard_matches_view(auth_client):
    d = date(2026, 9, 5)
    ReconDay.objects.create(book_date=d)
    batch_b = ImportBatch.objects.create(channel=Channel.BRI, book_date=d, source_filename="b.csv", file_hash="hb1")
    batch_o = ImportBatch.objects.create(channel=Channel.OTOMAX, book_date=d, source_filename="o.csv", file_hash="ho1")
    bm = BankMutation.objects.create(
        import_batch=batch_b,
        book_date=d,
        channel=Channel.BRI,
        amount=Decimal("1500000"),
        ref_normalized="dana123",
        description_raw="Transfer DANA",
        row_hash="bm1",
        match_status=MatchStatus.MATCHED,
    )
    oe = OtomaxEntry.objects.create(
        import_batch=batch_o,
        book_date=d,
        reseller_name_raw="PLC001",
        amount=Decimal("1500000"),
        description_raw="Topup",
        row_hash="oe1",
        match_status=MatchStatus.MATCHED,
    )
    Match.objects.create(
        book_date=d,
        channel=Channel.BRI,
        bank_mutation=bm,
        otomax_entry=oe,
        amount_bank=Decimal("1500000"),
        amount_otomax=Decimal("1500000"),
        match_type="AUTO_EXACT",
    )

    res = auth_client.get("/matches/", {"d": "2026-09-05"})
    assert res.status_code == 200
    content = res.content.decode()
    assert "Hasil Rekonsiliasi" in content
    assert "1.500.000" in content


@pytest.mark.django_db
def test_dashboard_manual_review_and_tag(auth_client):
    d = date(2026, 9, 5)
    batch_b = ImportBatch.objects.create(channel=Channel.BRI, book_date=d, source_filename="b.csv", file_hash="hb2")
    bm = BankMutation.objects.create(
        import_batch=batch_b,
        book_date=d,
        channel=Channel.BRI,
        amount=Decimal("2500"),
        ref_normalized="adm",
        description_raw="Biaya Admin Bulanan",
        row_hash="bm_adm",
        match_status=MatchStatus.UNMATCHED,
    )

    # View manual review page
    res = auth_client.get("/review-manual/", {"d": "2026-09-05"})
    assert res.status_code == 200
    assert "Antrean Review Manual" in res.content.decode()
    assert "Biaya Admin Bulanan" in res.content.decode()

    # Tag mutation as ADMIN
    tag_res = auth_client.post(
        f"/review-manual/tag/{bm.pk}/",
        {"tag": ManualTag.ADMIN, "note": "Biaya administrasi bank"},
    )
    assert tag_res.status_code in (200, 302)

    bm.refresh_from_db()
    assert bm.match_status == MatchStatus.MANUAL
    assert bm.tag_manual == ManualTag.ADMIN
    assert bm.manual_note == "Biaya administrasi bank"


@pytest.mark.django_db
def test_dashboard_pending_settle_view(auth_client):
    d = date(2026, 9, 5)
    batch_o = ImportBatch.objects.create(channel=Channel.OTOMAX, book_date=d, source_filename="o.csv", file_hash="ho2")
    OtomaxEntry.objects.create(
        import_batch=batch_o,
        book_date=d,
        reseller_name_raw="PLC999",
        amount=Decimal("500000"),
        description_raw="TARTUN EDC 500000",
        row_hash="oe_pend",
        match_status=MatchStatus.PENDING_SETTLE,
    )

    res = auth_client.get("/pending-settle/", {"d": "2026-09-05"})
    assert res.status_code == 200
    content = res.content.decode()
    assert "Pending Settle" in content
    assert "PLC999" in content
    assert "500.000" in content


@pytest.mark.django_db
def test_dashboard_reports_and_export(auth_client):
    res = auth_client.get("/reports/")
    assert res.status_code == 200
    assert "Riwayat &amp; Laporan" in res.content.decode() or "Riwayat & Laporan" in res.content.decode()

    export_res = auth_client.get("/reports/export/", {"start_date": "2026-09-01", "end_date": "2026-09-10"})
    assert export_res.status_code == 200
    assert export_res["Content-Type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.mark.django_db
def test_manual_match_and_unpair_action(auth_client):
    d = date(2026, 9, 5)
    batch_b = ImportBatch.objects.create(channel=Channel.BRI, book_date=d, source_filename="b.csv", file_hash="hbm1")
    batch_o = ImportBatch.objects.create(channel=Channel.OTOMAX, book_date=d, source_filename="o.csv", file_hash="hom1")

    bm = BankMutation.objects.create(
        import_batch=batch_b,
        book_date=d,
        channel=Channel.BRI,
        amount=Decimal("1800000"),
        description_raw="Transfer BI-Fast dari BANK NEG",
        row_hash="b_unmatched_1",
        match_status=MatchStatus.UNMATCHED,
    )
    oe = OtomaxEntry.objects.create(
        import_batch=batch_o,
        book_date=d,
        reseller_name_raw="PLC CL 2",
        amount=Decimal("1800000"),
        description_raw="TARTUN TF BRI BFST215401000596",
        row_hash="o_pending_1",
        match_status=MatchStatus.PENDING_SETTLE,
    )

    # 1. Post to /manual-match/
    res = auth_client.post(
        "/manual-match/",
        {
            "bank_id": bm.pk,
            "otomax_id": oe.pk,
            "note": "Cocok transfer manual",
            "book_date": "2026-09-05",
        },
    )
    assert res.status_code in (200, 302)

    bm.refresh_from_db()
    oe.refresh_from_db()
    assert bm.match_status == MatchStatus.MANUAL
    assert oe.match_status == MatchStatus.MANUAL

    match = Match.objects.filter(bank_mutation=bm, otomax_entry=oe, voided_at__isnull=True).first()
    assert match is not None
    assert match.amount_bank == Decimal("1800000")
    assert match.amount_diff == Decimal("0.00")
    assert "Cocok transfer manual" in match.note

    # 2. Post to /matches/unpair/<id>/
    unpair_res = auth_client.post(f"/matches/unpair/{match.pk}/")
    assert unpair_res.status_code in (200, 302)

    match.refresh_from_db()
    assert match.voided_at is not None

    bm.refresh_from_db()
    oe.refresh_from_db()
    assert bm.match_status == MatchStatus.UNMATCHED
    assert oe.match_status == MatchStatus.PENDING_SETTLE


@pytest.mark.django_db
def test_bulk_manual_match_action(auth_client):
    d = date(2026, 9, 5)
    batch_b = ImportBatch.objects.create(channel=Channel.BRI, book_date=d, source_filename="b.csv", file_hash="hbm2")
    batch_o = ImportBatch.objects.create(channel=Channel.OTOMAX, book_date=d, source_filename="o.csv", file_hash="hom2")

    # Pair 1: Unik Rp 2.500.000
    bm1 = BankMutation.objects.create(
        import_batch=batch_b,
        book_date=d,
        channel=Channel.BRI,
        amount=Decimal("2500000"),
        description_raw="Transfer BI-Fast 2.5jt",
        row_hash="b_unmatched_2",
        match_status=MatchStatus.UNMATCHED,
    )
    oe1 = OtomaxEntry.objects.create(
        import_batch=batch_o,
        book_date=d,
        reseller_name_raw="PLC 2.5JT",
        amount=Decimal("2500000"),
        description_raw="TARTUN TF BRI 2.5jt",
        row_hash="o_pending_2",
        match_status=MatchStatus.PENDING_SETTLE,
    )

    # Pair 2: Ambigu Rp 100.000 (2 bank vs 1 otomax -> tidak boleh di-bulk)
    BankMutation.objects.create(
        import_batch=batch_b,
        book_date=d,
        channel=Channel.BRI,
        amount=Decimal("100000"),
        description_raw="Transfer 100rb A",
        row_hash="b_unmatched_3a",
        match_status=MatchStatus.UNMATCHED,
    )
    BankMutation.objects.create(
        import_batch=batch_b,
        book_date=d,
        channel=Channel.BRI,
        amount=Decimal("100000"),
        description_raw="Transfer 100rb B",
        row_hash="b_unmatched_3b",
        match_status=MatchStatus.UNMATCHED,
    )
    OtomaxEntry.objects.create(
        import_batch=batch_o,
        book_date=d,
        reseller_name_raw="PLC 100RB",
        amount=Decimal("100000"),
        description_raw="TARTUN 100rb",
        row_hash="o_pending_3",
        match_status=MatchStatus.PENDING_SETTLE,
    )

    res = auth_client.post("/manual-match/bulk/", {"book_date": "2026-09-05"})
    assert res.status_code in (200, 302)

    bm1.refresh_from_db()
    oe1.refresh_from_db()
    # Yang unik 2.5jt harus otomatis MATCHED (MANUAL)
    assert bm1.match_status == MatchStatus.MANUAL
    assert oe1.match_status == MatchStatus.MANUAL

    # Yang ambigu 100rb tetap UNMATCHED / PENDING_SETTLE
    assert BankMutation.objects.filter(amount=Decimal("100000"), match_status=MatchStatus.UNMATCHED).count() == 2


@pytest.mark.django_db
def test_dashboard_delete_batch_action(auth_client):
    d = date(2026, 9, 5)
    batch = ImportBatch.objects.create(channel=Channel.BCA, book_date=d, source_filename="bca.csv", file_hash="hbca")
    BankMutation.objects.create(
        import_batch=batch,
        book_date=d,
        channel=Channel.BCA,
        amount=Decimal("500000"),
        description_raw="Transfer BCA",
        row_hash="bm_del_test",
        match_status=MatchStatus.UNMATCHED,
    )
    assert ImportBatch.objects.filter(id=batch.id).exists()
    assert BankMutation.objects.filter(import_batch=batch).count() == 1

    res = auth_client.post(f"/upload/delete-batch/{batch.id}/", {"book_date": "2026-09-05"})
    assert res.status_code == 302
    assert "/upload/?d=2026-09-05" in res.url
    assert not ImportBatch.objects.filter(id=batch.id).exists()
    assert BankMutation.objects.filter(row_hash="bm_del_test").count() == 0



