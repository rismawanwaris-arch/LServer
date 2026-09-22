import hashlib
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.core.enums import Channel, DiscrepancyKind, ManualTag, MatchStatus, OtomaxCategory
from apps.ingest.models import BankMutation, ImportBatch, OtomaxEntry
from apps.recon.models import Discrepancy, Match, ReconDay

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
def test_dashboard_day_view_alarm_expand_scoped_to_business_month(auth_client, settings):
    """Kotak Alarm SLA: "10 Terlama" tetap lihat semua selisih basi, tapi expand
    "1 Bulan Berjalan" cuma yang origin_book_date-nya masuk siklus bulan bisnis
    berjalan (BUSINESS_MONTH_START_DAY)."""
    settings.BUSINESS_MONTH_START_DAY = 29
    today = date(2026, 9, 20)  # siklus berjalan: 29 Agu - 28 Sep

    in_period = Discrepancy.objects.create(
        code="SLS-IN-PERIOD",
        origin_book_date=date(2026, 9, 1),
        channel=Channel.BRI,
        kind=DiscrepancyKind.BANK_ONLY,
        amount=Decimal("100000"),
    )
    out_of_period = Discrepancy.objects.create(
        code="SLS-OUT-PERIOD",
        origin_book_date=date(2026, 7, 1),
        channel=Channel.BRI,
        kind=DiscrepancyKind.BANK_ONLY,
        amount=Decimal("200000"),
    )

    res = auth_client.get(f"/?d={today.isoformat()}")
    assert res.status_code == 200
    content = res.content.decode()

    # Keduanya basi (jauh lebih dari DISCREPANCY_ALARM_DAYS) -> muncul di "10 Terlama".
    assert in_period.code in content
    assert out_of_period.code in content

    # Cuma yang di dalam siklus 29 Agu-28 Sep yang ada di alarms_this_period.
    assert res.context["alarms_this_period"].filter(pk=in_period.pk).exists()
    assert not res.context["alarms_this_period"].filter(pk=out_of_period.pk).exists()
    assert res.context["business_month_range"] == (date(2026, 8, 29), date(2026, 9, 28))


@pytest.mark.django_db
def test_dashboard_upload_view(auth_client):
    res = auth_client.get("/upload/")
    assert res.status_code == 200
    assert "Upload Data Mentah" in res.content.decode()


@pytest.mark.django_db
def test_upload_auto_syncs_date_from_file(auth_client):
    from django.core.files.uploadedfile import SimpleUploadedFile

    csv_data = (
        b"TGL_TRAN,MUTASI_DEBET,MUTASI_KREDIT,GLSIGN,REMARK_CUSTOM,DESK_TRAN\n"
        b"2026-09-12 10:15:00,0,500000,Cr,DANA123,TRANSFER DANA\n"
    )
    upfile = SimpleUploadedFile("bri_12sep.csv", csv_data, content_type="text/csv")

    # Upload while form date is set to 2026-09-14
    res = auth_client.post(
        "/upload/",
        {
            "action": "import",
            "channel": Channel.BRI,
            "book_date": "2026-09-14",
            "file": upfile,
        },
    )
    assert res.status_code == 302
    # Verify redirect automatically goes to 2026-09-12!
    assert "d=2026-09-12" in res.url

    # Verify both batch and mutation are synced to 2026-09-12
    batch = ImportBatch.objects.filter(source_filename="bri_12sep.csv").first()
    assert batch is not None
    assert batch.book_date == date(2026, 9, 12)

    bm = BankMutation.objects.filter(import_batch=batch).first()
    assert bm is not None
    assert bm.book_date == date(2026, 9, 12)


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


@pytest.mark.django_db
def test_pending_settle_tabs_and_tag_otomax(auth_client):
    d = date(2026, 9, 5)
    batch_o = ImportBatch.objects.create(
        channel=Channel.OTOMAX, book_date=d, source_filename="o.csv", file_hash="ho_tab"
    )
    oe = OtomaxEntry.objects.create(
        import_batch=batch_o,
        book_date=d,
        reseller_name_raw="PLC REV",
        amount=Decimal("-905000"),
        description_raw="REVISI TIKET CS DEBI",
        row_hash="oe_rev_1",
        match_status=MatchStatus.PENDING_SETTLE,
    )

    # 1. Check Belum Settle tab
    res = auth_client.get("/pending-settle/", {"d": "2026-09-05", "tab": "pending"})
    assert res.status_code == 200
    assert "REVISI TIKET CS DEBI" in res.content.decode()

    # 2. Tag Otomax
    tag_res = auth_client.post(
        f"/pending-settle/tag/{oe.pk}/",
        {"tag": "revisi", "note": "Koreksi CS", "book_date": "2026-09-05"},
    )
    assert tag_res.status_code in (200, 302)

    oe.refresh_from_db()
    assert oe.match_status == MatchStatus.MANUAL

    # 3. Check Sudah Selesai tab
    res_resolved = auth_client.get("/pending-settle/", {"d": "2026-09-05", "tab": "resolved"})
    assert res_resolved.status_code == 200
    content = res_resolved.content.decode()
    assert "REVISI TIKET CS DEBI" in content
    assert "Koreksi CS" in content


@pytest.mark.django_db
def test_bidirectional_sync_between_review_and_pending_settle(auth_client):
    d = date(2026, 9, 5)
    batch_b = ImportBatch.objects.create(
        channel=Channel.BRI, book_date=d, source_filename="b.csv", file_hash="hb_sync"
    )
    batch_o = ImportBatch.objects.create(
        channel=Channel.OTOMAX, book_date=d, source_filename="o.csv", file_hash="ho_sync"
    )

    bm = BankMutation.objects.create(
        import_batch=batch_b,
        book_date=d,
        channel=Channel.BRI,
        amount=Decimal("996000"),
        description_raw="Transfer BI-Fast DEBI RIZKI ADITYA",
        row_hash="bm_sync_1",
        match_status=MatchStatus.UNMATCHED,
    )
    oe = OtomaxEntry.objects.create(
        import_batch=batch_o,
        book_date=d,
        reseller_name_raw="PLC DEBI",
        amount=Decimal("996000"),
        description_raw="BFST590209540026DEBI RIZKI ADITYA",
        row_hash="oe_sync_1",
        match_status=MatchStatus.PENDING_SETTLE,
    )

    # Before pairing:
    # Manual Review: bm is in 'unmatched', oe is in dropdown
    res_mr = auth_client.get("/review-manual/", {"d": "2026-09-05", "tab": "unmatched"})
    assert "Transfer BI-Fast DEBI RIZKI ADITYA" in res_mr.content.decode()
    assert "PLC DEBI" in res_mr.content.decode()

    # Pending Settle: oe is in 'pending'
    res_ps = auth_client.get("/pending-settle/", {"d": "2026-09-05", "tab": "pending"})
    assert "BFST590209540026DEBI RIZKI ADITYA" in res_ps.content.decode()

    # Pair them
    match_res = auth_client.post(
        "/manual-match/",
        {"bank_id": bm.pk, "otomax_id": oe.pk, "note": "Sync pair test", "book_date": "2026-09-05"},
    )
    assert match_res.status_code in (200, 302)

    bm.refresh_from_db()
    oe.refresh_from_db()
    assert bm.match_status == MatchStatus.MANUAL
    assert oe.match_status == MatchStatus.MANUAL

    # After pairing:
    # 1. Manual Review: bm NOT in 'unmatched' items, IS in 'tagged', oe NOT in dropdown
    res_mr_unm = auth_client.get("/review-manual/", {"d": "2026-09-05", "tab": "unmatched"})
    assert len(res_mr_unm.context["items"]) == 0
    assert len(res_mr_unm.context["unmatched_otomax"]) == 0

    res_mr_tag = auth_client.get("/review-manual/", {"d": "2026-09-05", "tab": "tagged"})
    assert len(res_mr_tag.context["items"]) == 1
    assert "Transfer BI-Fast DEBI RIZKI ADITYA" in res_mr_tag.content.decode()
    assert "Lawan: PLC DEBI" in res_mr_tag.content.decode()

    # 2. Pending Settle: oe NOT in 'pending', IS in 'resolved' with partner bank info
    res_ps_unm = auth_client.get("/pending-settle/", {"d": "2026-09-05", "tab": "pending"})
    assert len(res_ps_unm.context["items"]) == 0

    res_ps_res = auth_client.get("/pending-settle/", {"d": "2026-09-05", "tab": "resolved"})
    assert len(res_ps_res.context["items"]) == 1
    assert "BFST590209540026DEBI RIZKI ADITYA" in res_ps_res.content.decode()
    assert "Lawan: BRI" in res_ps_res.content.decode()

    # 3. Unpair from Pending Settle
    match = Match.objects.filter(bank_mutation=bm, otomax_entry=oe, voided_at__isnull=True).first()
    assert match is not None
    unpair_res = auth_client.post(
        f"/matches/unpair/{match.pk}/", {"next_url": "/pending-settle/?d=2026-09-05&tab=resolved"}
    )
    assert unpair_res.status_code in (200, 302)

    bm.refresh_from_db()
    oe.refresh_from_db()
    assert bm.match_status == MatchStatus.UNMATCHED
    assert oe.match_status == MatchStatus.PENDING_SETTLE

    # Re-check both are back in their respective queues!
    res_mr_back = auth_client.get("/review-manual/", {"d": "2026-09-05", "tab": "unmatched"})
    assert "Transfer BI-Fast DEBI RIZKI ADITYA" in res_mr_back.content.decode()
    assert "PLC DEBI" in res_mr_back.content.decode()

    res_ps_back = auth_client.get("/pending-settle/", {"d": "2026-09-05", "tab": "pending"})
    assert "BFST590209540026DEBI RIZKI ADITYA" in res_ps_back.content.decode()


_STEPS_BD = date(2026, 9, 21)


def _h(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _steps_batch(channel):
    return ImportBatch.objects.create(
        channel=channel, book_date=_STEPS_BD, source_filename="t", file_hash=_h("steps", channel)
    )


@pytest.mark.django_db
def test_today_steps_all_pending_when_no_data(auth_client):
    """Tanggal kosong sama sekali: langkah 1 (Upload Data) yang harus jadi 'Selanjutnya',
    bukan langkah lain -- operator baru langsung tahu harus mulai dari mana."""
    res = auth_client.get("/", {"d": _STEPS_BD.isoformat()})
    steps = res.context["today_steps"]
    assert [s["done"] for s in steps] == [False, False, False, False, False, False]
    assert steps[0]["is_next"] is True
    assert all(not s["is_next"] for s in steps[1:])


@pytest.mark.django_db
def test_today_steps_reflect_real_outstanding_counts(auth_client):
    """Setelah data diimport & engine dijalankan, langkah 3-5 harus menunjukkan angka
    riil yang match dengan KPI card di halaman yang sama (bukan hitungan terpisah)."""
    bank = BankMutation.objects.create(
        import_batch=_steps_batch(Channel.BRI),
        channel=Channel.BRI,
        book_date=_STEPS_BD,
        description_raw="MUTASI TANPA PASANGAN",
        ref_normalized="MUTASI TANPA PASANGAN",
        amount=Decimal("100000"),
        match_status=MatchStatus.UNMATCHED,
        row_hash=_h("bank", "unpaired"),
    )
    Discrepancy.objects.create(
        code="SLS-STEPS-001",
        origin_book_date=_STEPS_BD,
        channel=Channel.BRI,
        kind=DiscrepancyKind.BANK_ONLY,
        bank_mutation=bank,
        amount=bank.amount,
    )
    OtomaxEntry.objects.create(
        import_batch=_steps_batch(Channel.OTOMAX),
        book_date=_STEPS_BD,
        reseller_name_raw="X",
        amount=Decimal("50000"),
        description_raw="TARTUN TANPA PASANGAN",
        category=OtomaxCategory.TOPUP_TARTUN,
        match_status=MatchStatus.PENDING_SETTLE,
        row_hash=_h("otomax", "pending"),
    )
    OtomaxEntry.objects.create(
        import_batch=_steps_batch(Channel.OTOMAX),
        book_date=_STEPS_BD,
        reseller_name_raw="Y",
        amount=Decimal("-20000"),
        description_raw="REV BELUM NETTED",
        category=OtomaxCategory.REVERSAL,
        match_status=MatchStatus.UNMATCHED,
        row_hash=_h("otomax", "rev"),
    )

    res = auth_client.get("/", {"d": _STEPS_BD.isoformat()})
    steps = res.context["today_steps"]
    by_title = {s["title"]: s for s in steps}

    assert by_title["Upload Data"]["done"] is True
    assert by_title["Jalankan Matching Engine"]["done"] is True
    assert by_title["Selesaikan Review Manual"]["done"] is False
    assert "1 mutasi bank" in by_title["Selesaikan Review Manual"]["desc"]
    assert by_title["Selesaikan Pending Settle"]["done"] is False
    assert "1 entri Otomax" in by_title["Selesaikan Pending Settle"]["desc"]
    assert by_title["Cek Reversal Otomax"]["done"] is False
    assert "1 baris REV" in by_title["Cek Reversal Otomax"]["desc"]
    assert by_title["Tutup Buku Harian"]["done"] is False

    # "Selanjutnya" harus jatuh ke langkah OUTSTANDING pertama (Review Manual),
    # bukan ke langkah yang sudah selesai di depannya.
    assert by_title["Selesaikan Review Manual"]["is_next"] is True
    assert by_title["Upload Data"]["is_next"] is False
    assert by_title["Jalankan Matching Engine"]["is_next"] is False


@pytest.mark.django_db
def test_today_steps_close_day_marks_last_step_done(auth_client):
    ReconDay.objects.create(book_date=_STEPS_BD, locked=True)
    res = auth_client.get("/", {"d": _STEPS_BD.isoformat()})
    steps = res.context["today_steps"]
    assert steps[-1]["title"] == "Tutup Buku Harian"
    assert steps[-1]["done"] is True




