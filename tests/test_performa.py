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
