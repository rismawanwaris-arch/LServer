"""Upload "periksa dulu sebelum simpan" (Blueprint Upload & Anti Double-Upload, disesuaikan).

Dua test pertama mereplikasi bug nyata yang ditemukan di data 12 Sep & file multi-hari
(lihat laporan Audit Alur Upload) dan ditulis SEBELUM perbaikannya."""

import hashlib
import io
from datetime import date, datetime
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.utils import timezone

from apps.core.enums import Channel, MatchStatus
from apps.ingest.models import BankMutation, ImportBatch, OtomaxEntry
from apps.ingest.services import ImportBlocked, NothingToImport, _hash, import_file
from apps.ingest.staging import (
    delete_staged_upload,
    get_staged_upload,
    stage_upload,
)
from apps.recon.models import ReconDay

D12 = date(2026, 9, 12)
HEADER = '"ID","NOREK","TGL_TRAN","MUTASI_DEBET","MUTASI_KREDIT","GLSIGN","TRREMK","REMARK_CUSTOM"\n'


def _bri_line(i, tgl, kredit, trremk, remark, debet="0.00", sign="Cr"):
    return f'"{i}","215401000596563","{tgl}","{debet}","{kredit}","{sign}","{trremk}","{remark}"\n'


def _batch(channel=Channel.BRI, bd=D12, name="lama.csv"):
    return ImportBatch.objects.create(
        channel=channel, book_date=bd, source_filename=name, file_hash=hashlib.sha256(name.encode()).hexdigest()
    )


# ---------------------------------------------------------------- regresi bug nyata


@pytest.mark.django_db
def test_upload_ulang_bri_dengan_format_keterangan_baru_tidak_menggandakan_baris():
    """Bug 1: data 12 Sep tersimpan saat parser BRI masih memakai REMARK_CUSTOM saja.
    Sejak 14 Sep keterangan = 'REMARK_CUSTOM [TRREMK]' -> row_hash beda -> upload ulang
    file yang sama memasukkan baris itu dua kali (106 dari 442 di data nyata)."""
    dt = datetime(2026, 9, 12, 6, 14, 36)
    old_desc = "Transfer BI-Fast dari Bank Lain - 901040210687 - Wawa"
    BankMutation.objects.create(
        import_batch=_batch(),
        channel=Channel.BRI,
        book_date=D12,
        txn_datetime=timezone.make_aware(dt),
        description_raw=old_desc,
        ref_normalized=old_desc,
        amount=Decimal("5600000.00"),
        row_hash=_hash(Channel.BRI, D12, old_desc, Decimal("5600000.00"), dt, ""),
    )
    csv = HEADER + _bri_line(1, "2026-09-12 06:14:36", "5600000.00", "WBNKTRF368201008965536T", old_desc)

    with pytest.raises(ImportBlocked):  # tidak ada baris baru -> tidak ada batch kosong
        import_file(channel=Channel.BRI, content=csv.encode(), book_date=D12, filename="ulang.csv")

    assert BankMutation.objects.count() == 1


@pytest.mark.django_db
def test_file_multi_hari_tidak_memasukkan_baris_ke_hari_yang_sudah_ditutup():
    """Bug 2: penolakan tutup buku cuma mengecek tanggal dominan file; baris tanggal lain
    di file yang sama tetap masuk ke hari yang sudah terkunci."""
    ReconDay.objects.create(book_date=date(2026, 9, 3), locked=True)
    csv = (
        HEADER
        + _bri_line(1, "2026-09-03 10:00:00", "100000.00", "TRF A", "TRF A")
        + _bri_line(2, "2026-09-05 10:00:00", "200000.00", "TRF B", "TRF B")
        + _bri_line(3, "2026-09-05 11:00:00", "300000.00", "TRF C", "TRF C")
    )

    batch = import_file(channel=Channel.BRI, content=csv.encode(), book_date=date(2026, 9, 5), filename="multi.csv")

    assert not BankMutation.objects.filter(book_date=date(2026, 9, 3)).exists()
    assert BankMutation.objects.filter(book_date=date(2026, 9, 5)).count() == 2
    assert batch.review["locked"] == 1


# ---------------------------------------------------------------- staging & UI workflow


def test_staging_lifecycle():
    sample_content = b"sample,csv,data"
    token = stage_upload(
        channel=Channel.BRI,
        book_date=D12,
        filename="test.csv",
        content=sample_content,
        user_id=1,
    )
    assert token

    staged = get_staged_upload(token)
    assert staged is not None
    assert staged.channel == Channel.BRI
    assert staged.filename == "test.csv"
    assert staged.content == sample_content
    assert staged.book_date == D12
    assert staged.user_id == 1

    delete_staged_upload(token)
    assert get_staged_upload(token) is None


@pytest.mark.django_db
def test_upload_view_review_and_confirm(client, django_user_model):
    user = django_user_model.objects.create_user(username="operator", password="password123")
    client.force_login(user)

    csv_content = (
        HEADER
        + _bri_line(1, "2026-09-12 08:00:00", "150000.00", "REF001", "Setoran Tiket 1")
        + _bri_line(2, "2026-09-12 08:30:00", "250000.00", "REF002", "Setoran Tiket 2")
    ).encode()

    uploaded = SimpleUploadedFile("test_bri.csv", csv_content, content_type="text/csv")

    # 1. Step: Review file
    resp_review = client.post(
        "/upload/?d=2026-09-12",
        {
            "action": "review",
            "channel": Channel.BRI,
            "file": uploaded,
        },
    )
    assert resp_review.status_code == 200
    html = resp_review.content.decode()
    assert "Periksa File" in html
    assert "test_bri.csv" in html
    assert "Total baris:" in html
    assert "Akan disimpan:" in html

    token = resp_review.context.get("staging_token")
    assert token

    # 2. Step: Confirm save
    resp_confirm = client.post(
        "/upload/?d=2026-09-12",
        {
            "action": "confirm",
            "token": token,
        },
        follow=True,
    )
    assert resp_confirm.status_code == 200
    confirm_html = resp_confirm.content.decode()
    assert "Berhasil mengimpor BRI: 2 baris ditambahkan" in confirm_html

    # Verifikasi data tersimpan
    assert BankMutation.objects.filter(book_date=D12).count() == 2

    # Verifikasi staged token telah dibersihkan
    assert get_staged_upload(token) is None


@pytest.mark.django_db
def test_upload_view_cancel(client, django_user_model):
    user = django_user_model.objects.create_user(username="operator2", password="password123")
    client.force_login(user)

    token = stage_upload(
        channel=Channel.BRI,
        book_date=D12,
        filename="batal.csv",
        content=b"dummy",
    )
    resp = client.post(
        "/upload/?d=2026-09-12",
        {
            "action": "cancel",
            "token": token,
        },
        follow=True,
    )
    assert resp.status_code == 200
    assert "Pemeriksaan file dibatalkan" in resp.content.decode()
    assert get_staged_upload(token) is None


@pytest.mark.django_db
def test_audit_duplicate_rows_command(capsys):
    call_command("audit_duplicate_rows")
    captured = capsys.readouterr()
    assert "AUDIT TRANSAKSI GANDA HISTORIS" in captured.out
    assert "Mode baca-saja" in captured.out
