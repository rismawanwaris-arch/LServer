from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.core.enums import Channel
from apps.ingest.models import BankMutation, ExcludedTransaction, OtomaxEntry
from apps.ingest.review import analyze_upload
from apps.ingest.services import import_file, restore_excluded_transaction
from apps.recon.models import ReconDay

D12 = date(2026, 9, 12)
HEADER = '"ID","NOREK","TGL_TRAN","MUTASI_DEBET","MUTASI_KREDIT","GLSIGN","TRREMK","REMARK_CUSTOM"\n'


def _bri_line(i, tgl, kredit, trremk, remark, debet="0.00", sign="Cr"):
    return f'"{i}","215401000596563","{tgl}","{debet}","{kredit}","{sign}","{trremk}","{remark}"\n'


@pytest.fixture
def auth_client(client):
    user = get_user_model().objects.create_user(username="admin_test", password="password123", is_staff=True)
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_import_with_selected_hashes_and_manual_exclusion():
    csv_content = (
        HEADER
        + _bri_line(1, "2026-09-12 08:00:00", "1000000.00", "TRX001", "Transfer Masuk 1")
        + _bri_line(2, "2026-09-12 09:00:00", "2000000.00", "TRX002", "Transfer Masuk 2")
    )
    review = analyze_upload(Channel.BRI, csv_content.encode("utf-8"), D12, "test_bri.csv")
    assert len(review.rows) == 2
    hash_1 = review.rows[0].row_hash
    hash_2 = review.rows[1].row_hash

    # Operator hanya mencentang hash_1, sedangkan hash_2 di-uncheck
    batch = import_file(
        channel=Channel.BRI,
        content=csv_content.encode("utf-8"),
        book_date=D12,
        filename="test_bri.csv",
        selected_hashes={hash_1},
    )

    # 1 baris masuk BankMutation aktif
    assert BankMutation.objects.filter(import_batch=batch).count() == 1
    bm = BankMutation.objects.get(import_batch=batch)
    assert bm.row_hash == hash_1
    assert bm.amount == Decimal("1000000.00")

    # 1 baris yang tidak dicentang masuk ke ExcludedTransaction
    assert ExcludedTransaction.objects.filter(import_batch=batch).count() == 1
    ex = ExcludedTransaction.objects.get(import_batch=batch)
    assert ex.row_hash == hash_2
    assert ex.amount == Decimal("2000000.00")
    assert ex.category == "MANUAL_UPLOAD"
    assert ex.reason == "Dikecualikan manual saat upload"
    assert batch.excluded_count == 1
    assert batch.row_count == 2


@pytest.mark.django_db
def test_restore_excluded_bank_mutation():
    csv_content = (
        HEADER
        + _bri_line(1, "2026-09-12 08:00:00", "500000.00", "TRX001", "Transfer Masuk 500k")
    )
    review = analyze_upload(Channel.BRI, csv_content.encode("utf-8"), D12, "test_bri.csv")
    hash_1 = review.rows[0].row_hash

    # Import dengan uncheck (tidak disimpan ke rekon)
    batch = import_file(
        channel=Channel.BRI,
        content=csv_content.encode("utf-8"),
        book_date=D12,
        filename="test_bri.csv",
        selected_hashes=set(),
    )
    assert BankMutation.objects.filter(import_batch=batch).count() == 0
    assert ExcludedTransaction.objects.filter(import_batch=batch).count() == 1

    ex = ExcludedTransaction.objects.get(import_batch=batch)
    # Pulihkan transaksi
    bm = restore_excluded_transaction(ex)
    assert isinstance(bm, BankMutation)
    assert bm.row_hash == hash_1
    assert bm.amount == Decimal("500000.00")
    assert ExcludedTransaction.objects.filter(import_batch=batch).count() == 0
    batch.refresh_from_db()
    assert batch.excluded_count == 0
    assert batch.mutations.count() == 1


@pytest.mark.django_db
def test_restore_excluded_otomax_entry():
    batch = import_file(
        channel=Channel.BRI,
        content=(HEADER + _bri_line(1, "2026-09-12 08:00:00", "100000.00", "T1", "R1")).encode("utf-8"),
        book_date=D12,
        filename="b.csv",
    )
    ex = ExcludedTransaction.objects.create(
        import_batch=batch,
        channel=Channel.OTOMAX,
        source_type="OTOMAX",
        book_date=D12,
        party_raw="RS_WARIS",
        description_raw="Tiket 100.000 RS_WARIS",
        amount=Decimal("100000.00"),
        category="MANUAL_UPLOAD",
        reason="Dikecualikan manual saat upload",
        row_hash="test_otomax_hash_unique",
    )

    oe = restore_excluded_transaction(ex)
    assert isinstance(oe, OtomaxEntry)
    assert oe.row_hash == "test_otomax_hash_unique"
    assert oe.reseller_name_raw == "RS_WARIS"
    assert oe.amount == Decimal("100000.00")
    assert ExcludedTransaction.objects.filter(pk=ex.pk).count() == 0


@pytest.mark.django_db
def test_restore_locked_day_fails():
    batch = import_file(
        channel=Channel.BRI,
        content=(HEADER + _bri_line(1, "2026-09-12 08:00:00", "100000.00", "T1", "R1")).encode("utf-8"),
        book_date=D12,
        filename="b.csv",
    )
    ex = ExcludedTransaction.objects.create(
        import_batch=batch,
        channel=Channel.BRI,
        source_type="BANK",
        book_date=D12,
        description_raw="Transfer 100k",
        amount=Decimal("100000.00"),
        row_hash="test_locked_hash",
    )
    ReconDay.objects.create(book_date=D12, locked=True)

    with pytest.raises(ValueError, match="sudah ditutup"):
        restore_excluded_transaction(ex)


@pytest.mark.django_db
def test_excluded_transactions_views_and_restore(auth_client):
    csv_content = (
        HEADER
        + _bri_line(1, "2026-09-12 08:00:00", "1500000.00", "TRX001", "Transfer Masuk 1.5M")
        + _bri_line(2, "2026-09-12 09:00:00", "2500000.00", "TRX002", "Transfer Masuk 2.5M")
    )
    batch = import_file(
        channel=Channel.BRI,
        content=csv_content.encode("utf-8"),
        book_date=D12,
        filename="test_bri.csv",
        selected_hashes=set(),  # Uncheck all
    )
    assert ExcludedTransaction.objects.filter(import_batch=batch).count() == 2

    # 1. Halaman Data Dikecualikan
    url = f"{reverse('excluded-transactions')}?d={D12.isoformat()}"
    resp = auth_client.get(url)
    assert resp.status_code == 200
    assert "Data Dikecualikan" in resp.content.decode("utf-8")
    assert "Rp 4.000.000" in resp.content.decode("utf-8")

    # 2. Pulihkan single via POST action
    first_ex = ExcludedTransaction.objects.filter(import_batch=batch).first()
    restore_url = reverse("excluded-restore", args=[first_ex.pk])
    resp = auth_client.post(restore_url)
    assert resp.status_code == 302
    assert ExcludedTransaction.objects.filter(import_batch=batch).count() == 1
    assert BankMutation.objects.filter(import_batch=batch).count() == 1

    # 3. Pulihkan bulk via POST action
    second_ex = ExcludedTransaction.objects.filter(import_batch=batch).first()
    bulk_url = reverse("excluded-restore-bulk")
    resp = auth_client.post(bulk_url, {"selected_ids": [second_ex.pk]})
    assert resp.status_code == 302
    assert ExcludedTransaction.objects.filter(import_batch=batch).count() == 0
    assert BankMutation.objects.filter(import_batch=batch).count() == 2
