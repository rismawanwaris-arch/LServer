"""Layout base.html: sidebar dengan jumlah pekerjaan per menu dan bilah atas dengan
pemilih tanggal ‹ ›, keduanya mengikuti tanggal kerja halaman yang sedang dibuka."""

import hashlib
import re
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.core.enums import Channel, MatchStatus, OtomaxCategory
from apps.ingest.models import BankMutation, ImportBatch, OtomaxEntry
from apps.recon.models import ReconDay

User = get_user_model()
BD = date(2026, 9, 16)


def _h(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _batch(channel):
    return ImportBatch.objects.create(channel=channel, book_date=BD, source_filename="t", file_hash=_h(channel))


@pytest.fixture
def client_op():
    client = Client()
    client.force_login(User.objects.create_user(username="operator", password="password123"))
    return client


def _count_for(html: str, label: str) -> str | None:
    m = re.search(rf">{re.escape(label)}</span>\s*(?:<span[^>]*nav-count[^>]*>(\d+)</span>)?", html)
    assert m, f"menu {label} tidak ditemukan"
    return m.group(1)


@pytest.mark.django_db
def test_sidebar_menampilkan_jumlah_pekerjaan_tanggal_kerja(client_op):
    BankMutation.objects.create(
        import_batch=_batch(Channel.BRI),
        channel=Channel.BRI,
        book_date=BD,
        description_raw="TRF",
        ref_normalized="TRF",
        amount=Decimal("100000"),
        row_hash=_h("b"),
    )
    for i, (cat, status) in enumerate(
        [
            (OtomaxCategory.REVERSAL, MatchStatus.PENDING_SETTLE),
            (OtomaxCategory.OTHER, MatchStatus.UNMATCHED),
            (OtomaxCategory.ADMIN, MatchStatus.UNMATCHED),  # potongan admin tidak dihitung
            (OtomaxCategory.TOPUP_TARTUN, MatchStatus.MATCHED),  # sudah selesai
        ]
    ):
        OtomaxEntry.objects.create(
            import_batch=_batch(Channel.OTOMAX),
            book_date=BD,
            reseller_name_raw="X",
            amount=Decimal("1000") * (i + 1),
            description_raw=f"row {i}",
            category=cat,
            match_status=status,
            row_hash=_h("o", i),
        )

    html = client_op.get(f"/pending-settle/?d={BD}").content.decode()

    assert _count_for(html, "Review Manual") == "1"
    assert _count_for(html, "Pending Settle") == "2"
    assert _count_for(html, "Reversal Otomax") == "1"
    assert _count_for(html, "Hasil Cocok") is None  # tidak ada usulan -> tanpa angka
    # Link sidebar membawa tanggal kerja, dan menu aktif ditandai.
    assert f'href="/review-manual/?d={BD}"' in html
    assert re.search(r'href="/pending-settle/\?d=2026-09-16"\s+aria-current="page"', html)


@pytest.mark.django_db
def test_bilah_atas_hari_sebelum_sesudah_mempertahankan_filter(client_op):
    html = client_op.get(f"/pending-settle/?d={BD}&tab=resolved").content.decode()

    assert 'href="/pending-settle/?d=2026-09-15&amp;tab=resolved"' in html
    assert 'href="/pending-settle/?d=2026-09-17&amp;tab=resolved"' in html
    assert "Hari masih terbuka" in html


@pytest.mark.django_db
def test_bilah_atas_menandai_hari_yang_sudah_tutup_buku(client_op):
    ReconDay.objects.create(book_date=BD, locked=True)
    html = client_op.get(f"/?d={BD}").content.decode()
    assert "Sudah tutup buku" in html


@pytest.mark.django_db
def test_halaman_tanpa_tanggal_tidak_menampilkan_pemilih_tanggal(client_op):
    html = client_op.get("/reseller/").content.decode()
    assert "Hari sebelumnya" not in html
    assert "Kode Reseller" in html


@pytest.mark.django_db
def test_halaman_login_tanpa_sidebar():
    html = Client().get("/login/").content.decode()
    assert "Navigasi utama" not in html


@pytest.mark.django_db
def test_reversal_tanpa_tanggal_bilah_atas_bilang_semua_tanggal(client_op):
    """Regression (masukan operator 28 Sep): di mode semua tanggal, bilah atas dulu tetap
    menampilkan tanggal terakhir dari sesi + halaman punya form tanggal sendiri -> rancu."""
    client_op.get(f"/?d={BD}")  # sesi ingat 16 Sep
    html = client_op.get("/reversal/").content.decode()

    assert "Semua tanggal" in html
    assert "Hari sebelumnya" not in html  # tidak ada stepper tanggal sesi
    assert "Filter Tanggal" not in html
    # Tombol kembali ke per tanggal membawa tanggal kerja terakhir.
    assert f'href="?d={BD}&tab=belum"' in html


@pytest.mark.django_db
def test_daftar_selisih_rentang_tanggal_tampil_di_bilah_atas(client_op):
    html = client_op.get("/selisih/", {"start_date": "2026-09-01", "end_date": "2026-09-10"}).content.decode()
    assert "01 Sep 2026 – 10 Sep 2026" in html
    assert "Ganti Tanggal" not in html


@pytest.mark.django_db
def test_daftar_selisih_per_tanggal_pakai_stepper(client_op):
    html = client_op.get("/selisih/", {"d": BD.isoformat()}).content.decode()
    assert "Hari sebelumnya" in html
    assert 'aria-current="page">\n      <svg class="icon h-3.5 w-3.5"><use href="#i-cal"/></svg>Per tanggal' in html
