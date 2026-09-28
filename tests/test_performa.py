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
