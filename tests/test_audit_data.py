"""Menu Audit Data: semua transaksi mentah + status/alasan, filter detail, dan hapus per
baris mentah (bukan per pencocokan) dengan aturan tutup buku & jejak log."""

import hashlib
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.core.enums import Channel, DiscrepancyKind, DiscrepancyStatus, MatchStatus, MatchType, OtomaxCategory
from apps.core.normalize import extract_tokens, norm_ref, ref_core
from apps.ingest.models import BankMutation, DebitIgnored, ExcludedTransaction, ImportBatch, OtomaxEntry
from apps.recon import audit
from apps.recon.engine import run_match
from apps.recon.models import Discrepancy, Match, ReconDay
from apps.recon.raw_delete import delete_raw_rows, plan_delete
from apps.recon.resolve import manual_net_reversal, manual_pair_transactions, tag_manual_mutation

BD = date(2026, 9, 12)
User = get_user_model()


def _h(*p):
    return hashlib.sha256("|".join(map(str, p)).encode()).hexdigest()


def _batch(channel, bd=BD, name=None):
    name = name or f"{channel}-{bd}.csv"
    return ImportBatch.objects.create(channel=channel, book_date=bd, source_filename=name, file_hash=_h(name))


def _bank(desc, amount, channel=Channel.BRI, bd=BD, hour=9, batch=None):
    return BankMutation.objects.create(
        import_batch=batch or _batch(channel, bd),
        channel=channel,
        book_date=bd,
        txn_datetime=timezone.make_aware(datetime(bd.year, bd.month, bd.day, hour, 0)),
        description_raw=desc,
        ref_normalized=norm_ref(desc),
        ref_core=ref_core(desc),
        extracted_tokens=extract_tokens(desc),
        amount=Decimal(amount),
        row_hash=_h("b", desc, amount, bd),
    )


def _otomax(desc, amount, reseller="PLC X", category=OtomaxCategory.TOPUP_TARTUN, hint=Channel.BRI, bd=BD):
    return OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX, bd),
        book_date=bd,
        entry_datetime=timezone.make_aware(datetime(bd.year, bd.month, bd.day, 10, 0)),
        reseller_name_raw=reseller,
        amount=Decimal(amount),
        description_raw=desc,
        category=category,
        channel_hint=hint,
        ref_normalized=norm_ref(desc),
        ref_core=ref_core(desc),
        extracted_tokens=extract_tokens(desc),
        row_hash=_h("o", desc, amount, bd),
    )


def _rows(**kw):
    return {r.key: r for r in audit.collect(audit.AuditFilters(start=BD, end=BD, **kw)).rows}


@pytest.fixture
def skenario():
    s = {}
    s["bank_cocok"] = _bank("ATMLTRPRM 01884 000001039 21540100059656", "550000")
    s["oto_cocok"] = _otomax("TARTUN EDC BRI ATMLTRPRM 01884 000001039 21540100059656", "550000")
    run_match(BD)
    s["bank_belum"] = _bank("TRF MASUK TANPA PASANGAN 777", "125000", hour=11)
    s["oto_pending"] = _otomax("TARTUN TF BRI TIDAK ADA DI BANK 888", "300000")
    s["oto_admin"] = _otomax("ADMIN TARTUN", "-6500", category=OtomaxCategory.ADMIN, hint="")
    s["debit"] = DebitIgnored.objects.create(
        import_batch=_batch(Channel.BRI),
        channel=Channel.BRI,
        book_date=BD,
        description_raw="BIAYA ADM",
        amount=Decimal("6500"),
        row_hash=_h("d"),
    )
    s["excl"] = ExcludedTransaction.objects.create(
        import_batch=_batch(Channel.BRI),
        channel=Channel.BRI,
        source_type="BANK",
        book_date=BD,
        description_raw="TRANSFER INTERNAL SIMATECH",
        amount=Decimal("900000"),
        reason="Aturan: Simatech",
        row_hash=_h("x"),
    )
    b_diff = _bank("QRIS CILENGKRANG 3 CELL 004769151", "2778000", channel=Channel.BCA, hour=12)
    s["match_diff"] = manual_pair_transactions(
        b_diff, _otomax("TARTUN TF BCA CILENGKRANG", "2788000", hint=Channel.BCA)
    )
    s["bank_diff"] = b_diff
    s["bank_tag"] = tag_manual_mutation(_bank("SETOR TUNAI KASIR", "400000", hour=13), tag="setor_tunai")
    rev = _otomax(
        "REV Transfer dari PLC128 - PLC PD3", "-450000", reseller="DANI", category=OtomaxCategory.REVERSAL, hint=""
    )
    refund = _otomax("REFUND FROM OTO3386 - DANI", "450000", reseller="PLC PD3", category=OtomaxCategory.OTHER, hint="")
    manual_net_reversal(rev, refund, note="salah tembak")
    s["rev"], s["refund"] = rev, refund
    b_us = _bank("TRANSFER MIRIP SAJA", "75000", hour=14)
    o_us = _otomax("TARTUN TF BRI MIRIP", "75000")
    Match.objects.create(
        book_date=BD,
        channel=Channel.BRI,
        bank_mutation=b_us,
        otomax_entry=o_us,
        match_type=MatchType.AUTO_FUZZY,
        amount_bank=b_us.amount,
        amount_otomax=o_us.amount,
        needs_review=True,
        review_reason="Fuzzy kemiripan",
    )
    BankMutation.objects.filter(pk=b_us.pk).update(match_status=MatchStatus.MATCHED)
    OtomaxEntry.objects.filter(pk=o_us.pk).update(match_status=MatchStatus.MATCHED)
    s["bank_usulan"] = b_us
    return s


@pytest.mark.django_db
def test_setiap_jenis_baris_punya_status_dan_alasan(skenario):
    s = skenario
    rows = _rows()
    st = {k: r.status for k, r in rows.items()}

    assert st[f"B:{s['bank_cocok'].id}"] == audit.COCOK_OTOMATIS
    assert st[f"O:{s['oto_cocok'].id}"] == audit.COCOK_OTOMATIS
    assert st[f"B:{s['bank_belum'].id}"] == audit.BELUM_COCOK
    assert st[f"O:{s['oto_pending'].id}"] == audit.PENDING
    assert st[f"O:{s['oto_admin'].id}"] == audit.NON_REKON
    assert st[f"D:{s['debit'].id}"] == audit.DEBIT
    assert st[f"X:{s['excl'].id}"] == audit.DIKECUALIKAN
    assert st[f"B:{s['bank_diff'].id}"] == audit.SELISIH_NOMINAL
    assert st[f"B:{s['bank_tag'].id}"] == audit.TAG_MANUAL
    assert st[f"O:{s['rev'].id}"] == st[f"O:{s['refund'].id}"] == audit.DINETRALKAN
    assert st[f"B:{s['bank_usulan'].id}"] == audit.USULAN

    assert rows[f"D:{s['debit'].id}"].amount == Decimal("-6500")  # debit tampil negatif
    assert f"Otomax #{s['oto_cocok'].id}" in rows[f"B:{s['bank_cocok'].id}"].reason
    assert "PLC PD3" in rows[f"O:{s['rev'].id}"].reason
    assert "Setor Tunai" in rows[f"B:{s['bank_tag'].id}"].reason
    assert rows[f"B:{s['bank_cocok'].id}"].filename  # file asal ikut tampil
    # Semua baris mentah hari itu muncul: 7 mutasi+debit+dikecualikan, 9 Otomax.
    assert len(rows) == BankMutation.objects.count() + 2 + OtomaxEntry.objects.count()


@pytest.mark.django_db
def test_filter_sisi_sumber_status_dan_pencarian(skenario):
    s = skenario
    assert all(r.side == audit.SIDE_BANK for r in _rows(side="bank").values())
    assert all(r.side == audit.SIDE_OTOMAX for r in _rows(side="otomax").values())
    assert {r.channel for r in _rows(source="BCA").values()} == {Channel.BCA}
    assert set(_rows(status=audit.PENDING)) == {f"O:{s['oto_pending'].id}"}
    # Cari nominal (format Indonesia, tanda diabaikan) dan teks keterangan / pihak.
    assert f"O:{s['rev'].id}" in _rows(q="450.000") and f"O:{s['refund'].id}" in _rows(q="Rp 450.000")
    assert set(_rows(q="cilengkrang")) >= {f"B:{s['bank_diff'].id}"}
    assert set(_rows(q="PLC PD3")) >= {f"O:{s['refund'].id}"}


@pytest.mark.django_db
def test_ringkasan_tetap_lengkap_walau_difilter_status(skenario):
    res = audit.collect(audit.AuditFilters(start=BD, end=BD, status=audit.PENDING))
    assert len(res.rows) == 1
    assert res.summary["bank"][audit.BELUM_COCOK]["n"] == 1
    assert res.summary["otomax"][audit.PENDING]["amount"] == Decimal("300000")


@pytest.mark.django_db
def test_rentang_tanggal():
    _bank("HARI SATU", "10000", bd=BD)
    _bank("HARI DUA", "20000", bd=BD + timedelta(days=1))
    _bank("DI LUAR RENTANG", "30000", bd=BD + timedelta(days=5))
    res = audit.collect(audit.AuditFilters(start=BD, end=BD + timedelta(days=1)))
    assert [r.description for r in res.rows] == ["HARI SATU", "HARI DUA"]


# ------------------------------------------------------------------------------ hapus


@pytest.fixture
def op():
    return User.objects.create_user(username="operator", password="password123")


@pytest.mark.django_db
def test_hapus_mutasi_yang_sudah_cocok_lawannya_tetap_ada_dan_kembali_ke_antrean(skenario, op):
    s = skenario
    res = delete_raw_rows([f"B:{s['bank_cocok'].id}"], user=op)

    assert res["deleted"] == 1 and res["unpaired"] == 1
    assert not BankMutation.objects.filter(pk=s["bank_cocok"].pk).exists()
    oto = OtomaxEntry.objects.get(pk=s["oto_cocok"].pk)  # lawan TIDAK ikut terhapus
    assert oto.match_status == MatchStatus.PENDING_SETTLE
    assert not Match.objects.filter(otomax_entry=oto, voided_at__isnull=True).exists()
    log = LogEntry.objects.get()
    assert log.user == op and "Dihapus dari Audit Data" in log.change_message and "Rp 550.000" in log.change_message


@pytest.mark.django_db
def test_hapus_mutasi_dengan_selisih_nominal_membersihkan_selisihnya(skenario, op):
    s = skenario
    oto_id = s["match_diff"].otomax_entry_id
    delete_raw_rows([f"B:{s['bank_diff'].id}"], user=op)
    assert not Discrepancy.objects.filter(kind=DiscrepancyKind.AMOUNT_DIFF, status=DiscrepancyStatus.OPEN).exists()
    assert OtomaxEntry.objects.get(pk=oto_id).match_status == MatchStatus.PENDING_SETTLE


@pytest.mark.django_db
def test_hapus_rev_yang_dinetralkan_lawannya_kembali_ke_pending_settle(skenario, op):
    s = skenario
    delete_raw_rows([f"O:{s['rev'].id}"], user=op)
    refund = OtomaxEntry.objects.get(pk=s["refund"].pk)
    assert refund.match_status == MatchStatus.PENDING_SETTLE and refund.net_pair_id is None
    assert refund.discrepancies.filter(status=DiscrepancyStatus.OPEN, kind=DiscrepancyKind.OTOMAX_ONLY).exists()


@pytest.mark.django_db
def test_hapus_ditolak_di_hari_tutup_buku_termasuk_hari_pasangannya(op):
    o = _otomax("TARTUN TF BRI SUSULAN 999", "500000", bd=BD + timedelta(days=3))
    b = _bank("TRANSFER SUSULAN 999", "500000", bd=BD)
    manual_pair_transactions(b, o)
    ReconDay.objects.update_or_create(book_date=BD, defaults={"locked": True})

    plan = plan_delete([f"O:{o.id}"])  # hari entri Otomax terbuka, tapi pasangannya di hari tertutup
    assert plan.total == 0 and len(plan.blocked) == 1 and "sudah tutup buku" in plan.blocked[0][2]
    res = delete_raw_rows([f"O:{o.id}", f"B:{b.id}"], user=op)
    assert res == {"deleted": 0, "blocked": 2, "missing": 0, "unpaired": 0, "unnetted": 0}
    assert OtomaxEntry.objects.filter(pk=o.pk).exists() and BankMutation.objects.filter(pk=b.pk).exists()


@pytest.mark.django_db
def test_hapus_anggota_gabungan_qris_anggota_lain_kembali_terbuka(op):
    b = _bank("QRIS BULK OUTLET A", "300000", channel=Channel.MERCHANT_BCA)
    o1 = _otomax("TARTUN QR BULK 1", "100000", hint=Channel.MERCHANT_BCA)
    o2 = _otomax("TARTUN QR BULK 2", "200000", hint=Channel.MERCHANT_BCA)
    from apps.recon.resolve import manual_pair_many

    manual_pair_many(b, [o1, o2])
    delete_raw_rows([f"O:{o1.id}"], user=op)
    assert BankMutation.objects.get(pk=b.pk).match_status == MatchStatus.UNMATCHED
    assert OtomaxEntry.objects.get(pk=o2.pk).match_status == MatchStatus.PENDING_SETTLE


@pytest.mark.django_db
def test_hapus_debit_dan_dikecualikan_merapikan_batch_asal(op):
    batch = _batch(Channel.BRI, name="satu.csv")
    d = DebitIgnored.objects.create(
        import_batch=batch,
        channel=Channel.BRI,
        book_date=BD,
        description_raw="BIAYA",
        amount=Decimal("6500"),
        row_hash=_h("d1"),
    )
    keep = _bank("TETAP ADA", "10000", batch=batch)
    batch.row_count = 2
    batch.save()
    delete_raw_rows([f"D:{d.id}"], user=op)
    batch.refresh_from_db()
    assert batch.row_count == 1
    delete_raw_rows([f"B:{keep.id}"], user=op)
    assert not ImportBatch.objects.filter(pk=batch.pk).exists()  # batch kosong ikut hilang


# ------------------------------------------------------------------------------ halaman


@pytest.fixture
def client_op(client, op):
    client.force_login(op)
    return client


@pytest.mark.django_db
def test_halaman_wajib_login(client):
    resp = client.get(f"/audit/?d={BD}")
    assert resp.status_code == 302 and "/login/" in resp["Location"]


@pytest.mark.django_db
def test_halaman_tanpa_tanggal_dialihkan_ke_tanggal_eksplisit(client_op):
    resp = client_op.get("/audit/?side=bank")
    assert resp.status_code == 302 and "d=" in resp["Location"] and "side=bank" in resp["Location"]


@pytest.mark.django_db
def test_halaman_menampilkan_semua_baris_tanpa_menulis(skenario, client_op):
    before = (BankMutation.objects.count(), OtomaxEntry.objects.count(), Match.objects.count())
    resp = client_op.get(f"/audit/?d={BD}")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "ATMLTRPRM 01884" in html and "TIDAK ADA DI BANK 888" in html and "BIAYA ADM" in html
    assert "TRANSFER INTERNAL SIMATECH" in html and "Dinetralkan" in html
    assert (BankMutation.objects.count(), OtomaxEntry.objects.count(), Match.objects.count()) == before

    resp = client_op.get(f"/audit/?start_date={BD}&end_date={BD + timedelta(days=1)}&side=otomax&q=450.000")
    html = resp.content.decode()
    assert "REFUND FROM OTO3386" in html and "ATMLTRPRM" not in html


@pytest.mark.django_db
def test_detail_baris(skenario, client_op):
    s = skenario
    resp = client_op.get(f"/audit/detail/B/{s['bank_cocok'].id}/")
    assert resp.status_code == 200 and "Riwayat pencocokan" in resp.content.decode()
    assert client_op.get("/audit/detail/Z/1/").status_code == 404


@pytest.mark.django_db
def test_hapus_lewat_halaman_wajib_ketik_hapus(skenario, client_op):
    s = skenario
    key = f"B:{s['bank_belum'].id}"
    preview = client_op.post("/audit/hapus/periksa/", {"keys": key, "next": f"/audit/?d={BD}"})
    assert preview.status_code == 200 and "Ketik" in preview.content.decode()
    assert BankMutation.objects.filter(pk=s["bank_belum"].pk).exists()  # periksa tidak menghapus

    client_op.post("/audit/hapus/", {"keys": key, "confirm": "hapus", "next": f"/audit/?d={BD}"})
    assert BankMutation.objects.filter(pk=s["bank_belum"].pk).exists()

    resp = client_op.post("/audit/hapus/", {"keys": key, "confirm": "HAPUS", "next": "https://jahat.example/"})
    assert resp.status_code == 302 and resp["Location"] == "/audit/"  # next hanya boleh ke Audit Data
    assert not BankMutation.objects.filter(pk=s["bank_belum"].pk).exists()


@pytest.mark.django_db
def test_periksa_semua_hasil_filter_dihitung_ulang_di_server(skenario, client_op):
    resp = client_op.post("/audit/hapus/periksa/", {"all": "1", "d": str(BD), "status": audit.PENDING})
    html = resp.content.decode()
    assert f'value="O:{skenario["oto_pending"].id}"' in html and "Hapus 1 baris" in html


@pytest.mark.django_db
def test_export_excel_mengikuti_filter(skenario, client_op):
    import io

    import openpyxl

    resp = client_op.get(f"/audit/export/?d={BD}&side=bank")
    assert resp.status_code == 200 and resp["Content-Disposition"].endswith(f'audit_{BD}.xlsx"')
    ws = openpyxl.load_workbook(io.BytesIO(resp.content)).active
    sides = {ws.cell(row=r, column=3).value for r in range(5, ws.max_row + 1)}
    assert sides == {"Bank"}
    amounts = [ws.cell(row=r, column=8).value for r in range(5, ws.max_row + 1)]
    assert -6500 in amounts  # debit negatif
