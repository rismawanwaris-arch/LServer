"""Optimasi load (lihat laporan Audit Kecepatan): respons HTML dikompres dan CSS dimuat
sebagai file statis, bukan dikompilasi di browser lewat Tailwind CDN."""

import gzip

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

User = get_user_model()


@pytest.fixture
def client_op():
    client = Client()
    client.force_login(User.objects.create_user(username="operator", password="password123"))
    return client


@pytest.mark.django_db
def test_html_dikompres_gzip_kalau_browser_mendukung(client_op):
    res = client_op.get("/", HTTP_ACCEPT_ENCODING="gzip, deflate, br")
    assert res.status_code == 200
    assert res["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in res["Vary"]
    html = gzip.decompress(res.content).decode()
    assert "Rekonsiliasi" in html  # isi utuh setelah dibuka kembali


@pytest.mark.django_db
def test_tanpa_accept_encoding_tetap_dikirim_mentah(client_op):
    res = client_op.get("/")
    assert not res.has_header("Content-Encoding")
    assert "Rekonsiliasi" in res.content.decode()


@pytest.mark.django_db
def test_css_dimuat_sebagai_file_statis_bukan_tailwind_cdn(client_op):
    html = client_op.get("/").content.decode()
    assert "cdn.tailwindcss.com" not in html
    assert "text/tailwindcss" not in html
    assert '<link rel="stylesheet" href="/static/css/tailwind.css">' in html


def test_konfigurasi_tailwind_build_memuat_token_dan_komponen():
    """Palet, mode gelap berbasis kelas, dan komponen yang dulu inline di base.html
    sekarang harus ada di sumber build -- kalau tidak, tampilan produksi berubah."""
    from pathlib import Path

    config = Path("tailwind.config.js").read_text()
    source = Path("apps/dashboard/static/src/input.css").read_text()
    assert 'darkMode: "class"' in config
    assert "'#2563eb'" in config  # indigo-600 = aksen biru
    assert "'#131926'" in config  # slate-900 netral
    for komponen in (".card", ".btn-primary", ".badge-good", ".seg-item-active", ".nav-link-active", ".callout-warn"):
        assert komponen in source, komponen


def _selisih(n, book_date, prefix="SLS"):
    from decimal import Decimal

    from apps.core.enums import Channel, DiscrepancyKind
    from apps.recon.models import Discrepancy

    for i in range(n):
        Discrepancy.objects.create(
            code=f"{prefix}-{i:03d}",
            origin_book_date=book_date,
            channel=Channel.BRI,
            kind=DiscrepancyKind.BANK_ONLY,
            amount=Decimal("1000") * (i + 1),
        )


@pytest.mark.django_db
def test_daftar_selisih_dimuat_bertahap_ringkasan_tetap_semua(client_op):
    """Langkah 4: 120 selisih -> halaman pertama 50 baris + pemicu muat berikutnya, tapi
    kartu ringkasan tetap menghitung SEMUA selisih (tidak boleh ikut terpotong)."""
    from datetime import date
    from decimal import Decimal

    bd = date(2026, 9, 12)
    _selisih(120, bd)

    res = client_op.get("/selisih/", {"d": bd.isoformat()})
    html = res.content.decode()
    assert len(res.context["items"]) == 50
    assert res.context["total_count"] == 120
    assert res.context["open_count"] == 120
    assert res.context["open_amount"] == sum(Decimal("1000") * (i + 1) for i in range(120))
    assert 'hx-trigger="revealed"' in html
    assert "page=2" in html
    # Detail tidak ikut dirender; cuma pemuatnya.
    assert html.count('hx-trigger="intersect once"') == 50

    more = client_op.get("/selisih/", {"d": bd.isoformat(), "page": 3}, HTTP_HX_REQUEST="true")
    assert "<html" not in more.content.decode()  # partial baris saja
    assert more.context["page_obj"].object_list[0].code == "SLS-100"
    assert "Semua 120 selisih sudah ditampilkan" in more.content.decode()

    # Tanpa header HTMX, ?page=3 tetap membuka daftar dari awal (link "hapus buku" dsb.).
    full = client_op.get("/selisih/", {"d": bd.isoformat(), "page": 3})
    assert full.context["items"][0].code == "SLS-000"


@pytest.mark.django_db
def test_detail_selisih_kembali_hanya_ke_daftar_selisih(client_op):
    from datetime import date

    from apps.recon.models import Discrepancy

    _selisih(1, date(2026, 9, 12))
    pk = Discrepancy.objects.get().pk
    html = client_op.get(f"/selisih/{pk}/detail/", {"next": "/selisih/?d=2026-09-12"}).content.decode()
    assert 'value="/selisih/?d=2026-09-12"' in html
    evil = client_op.get(f"/selisih/{pk}/detail/", {"next": "https://jahat.example/"}).content.decode()
    assert "jahat.example" not in evil


def _batch(channel, bd, tag):
    import hashlib

    from apps.ingest.models import ImportBatch

    return ImportBatch.objects.create(
        channel=channel, book_date=bd, source_filename="t", file_hash=hashlib.sha256(tag.encode()).hexdigest()
    )


@pytest.mark.django_db
def test_pending_settle_baris_dimuat_bertahap_total_tetap_semua(client_op):
    """Langkah 5: 70 entri -> 50 baris pertama + pemicu muat; jumlah & total tab tetap
    menghitung ke-70 entri, dan halaman 2 berisi 20 sisanya (lengkap dengan lawan netral)."""
    from datetime import date
    from decimal import Decimal

    from apps.core.enums import Channel, MatchStatus, OtomaxCategory
    from apps.ingest.models import OtomaxEntry

    bd = date(2026, 9, 12)
    batch = _batch(Channel.OTOMAX, bd, "otomax-70")
    for i in range(70):
        OtomaxEntry.objects.create(
            import_batch=batch,
            book_date=bd,
            reseller_name_raw=f"R{i}",
            amount=Decimal(100000 + i * 1000),
            description_raw=f"TARTUN TF BRI {i}",
            category=OtomaxCategory.TOPUP_TARTUN,
            match_status=MatchStatus.PENDING_SETTLE,
            row_hash=f"o{i}",
        )
    # Lawan netral untuk entri terkecil (muncul di halaman 2 karena urut nominal turun).
    OtomaxEntry.objects.create(
        import_batch=batch,
        book_date=bd,
        reseller_name_raw="REV",
        amount=Decimal("-100000"),
        description_raw="REV TARTUN",
        category=OtomaxCategory.REVERSAL,
        match_status=MatchStatus.PENDING_SETTLE,
        row_hash="rev",
    )

    res = client_op.get("/pending-settle/", {"d": bd.isoformat()})
    assert res.context["pending_count"] == 71
    assert len(res.context["items"]) == 50
    assert 'hx-trigger="revealed"' in res.content.decode()

    more = client_op.get("/pending-settle/", {"d": bd.isoformat(), "page": 2}, HTTP_HX_REQUEST="true")
    html = more.content.decode()
    assert "<html" not in html
    assert len(more.context["items"]) == 21
    smallest = next(o for o in more.context["items"] if o.amount == Decimal("100000"))
    assert [c.reseller_name_raw for c in smallest.net_candidates] == ["REV"]
    assert "Semua 71 entri sudah ditampilkan" in html


@pytest.mark.django_db
def test_review_manual_kartu_dimuat_bertahap(client_op):
    from datetime import date
    from decimal import Decimal

    from apps.core.enums import Channel
    from apps.ingest.models import BankMutation

    bd = date(2026, 9, 12)
    batch = _batch(Channel.BRI, bd, "bri-25")
    for i in range(25):
        BankMutation.objects.create(
            import_batch=batch,
            channel=Channel.BRI,
            book_date=bd,
            description_raw=f"TRF MASUK {i:02d}",
            ref_normalized=f"TRF {i}",
            amount=Decimal(50000 + i),
            row_hash=f"b{i}",
        )

    res = client_op.get("/review-manual/", {"d": bd.isoformat()})
    assert res.context["unmatched_count"] == 25
    assert len(res.context["items"]) == 20
    assert 'id="otomax-candidates"' in res.content.decode()  # kandidat dikirim sekali

    more = client_op.get("/review-manual/", {"d": bd.isoformat(), "page": 2}, HTTP_HX_REQUEST="true")
    html = more.content.decode()
    assert html.count('x-data="otomaxPicker(') == 5
    assert 'id="otomax-candidates"' not in html
    assert f'value="/review-manual/?d={bd.isoformat()}"' in html  # form kembali ke daftar, bukan ?page=2


@pytest.mark.django_db
def test_server_timing_untuk_pengguna_login(client_op):
    import re

    res = client_op.get("/")
    header = res["Server-Timing"]
    m = re.fullmatch(r'db;desc="(\d+) query";dur=[\d.]+, app;desc="Django";dur=[\d.]+', header)
    assert m, header
    assert int(m.group(1)) > 0


@pytest.mark.django_db
def test_server_timing_tidak_bocor_ke_pengunjung_anonim():
    res = Client().get("/login/")
    assert not res.has_header("Server-Timing")
