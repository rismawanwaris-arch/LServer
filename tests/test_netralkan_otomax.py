"""Netralkan Otomax <-> Otomax: koreksi internal operator (salah tembak saldo lalu langsung
dibalik ke reseller lain) yang TIDAK boleh dinetralkan otomatis (reseller beda), tapi harus
bisa diselesaikan operator tanpa meninggalkan selisih hantu, dan bisa dibatalkan lagi."""

import hashlib
from datetime import date, datetime
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.core.enums import Channel, DiscrepancyKind, DiscrepancyStatus, MatchStatus, OtomaxCategory
from apps.core.normalize import classify_otomax, extract_tokens, norm_ref, ref_core, strip_otomax_prefix
from apps.ingest.models import BankMutation, ImportBatch, OtomaxEntry
from apps.recon.engine import run_match
from apps.recon.models import Adjustment, Discrepancy, Match, ReconDay
from apps.recon.resolve import manual_net_reversal, unnet_otomax_pair

User = get_user_model()
BD = date(2026, 9, 16)


def _h(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _batch(channel, book_date=BD):
    return ImportBatch.objects.create(
        channel=channel, book_date=book_date, source_filename="t", file_hash=_h(book_date, channel)
    )


def _row(desc, amount, reseller, book_date=BD, at=None, status=MatchStatus.UNMATCHED):
    """OtomaxEntry persis seperti pipeline import (classify + strip prefix)."""
    category, hint = classify_otomax(desc)
    embedded = strip_otomax_prefix(desc)
    return OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX, book_date),
        book_date=book_date,
        entry_datetime=timezone.make_aware(at) if at else None,
        reseller_name_raw=reseller,
        amount=Decimal(amount),
        description_raw=desc,
        category=category,
        channel_hint=hint or "",
        ref_normalized=norm_ref(embedded),
        ref_core=ref_core(embedded),
        extracted_tokens=extract_tokens(embedded),
        match_status=status,
        row_hash=_h("o", desc, amount, reseller, book_date, status),
    )


def _salah_tembak():
    """Kasus nyata 16 Sep: saldo 450.000 salah ditembak, dibalik lewat REV dari DANI lalu
    di-REFUND ke PLC PD3 semenit kemudian."""
    rev = _row("REV Transfer dari PLC128 - PLC PD3", "-450000", "DANI", at=datetime(2026, 9, 16, 19, 34))
    refund = _row("REFUND FROM OTO3386 - DANI", "450000", "PLC PD3", at=datetime(2026, 9, 16, 19, 35))
    return rev, refund


@pytest.fixture
def client_op():
    client = Client()
    client.force_login(User.objects.create_user(username="operator", password="password123"))
    return client


def _open_discs(*entries):
    return Discrepancy.objects.filter(otomax_entry__in=entries, status=DiscrepancyStatus.OPEN)


@pytest.mark.django_db
def test_mesin_tidak_menetralkan_otomatis_koreksi_beda_reseller():
    rev, refund = _salah_tembak()
    assert rev.category == OtomaxCategory.REVERSAL
    assert refund.category == OtomaxCategory.OTHER

    run_match(BD)

    rev.refresh_from_db()
    refund.refresh_from_db()
    assert rev.match_status == MatchStatus.PENDING_SETTLE
    assert refund.match_status == MatchStatus.PENDING_SETTLE
    assert _open_discs(rev, refund).filter(kind=DiscrepancyKind.OTOMAX_ONLY).count() == 2


@pytest.mark.django_db
def test_pending_settle_menawarkan_lawan_otomax_berlawanan_persis(client_op):
    rev, refund = _salah_tembak()
    _row("TARTUN EDC BRI BEDA NOMINAL", "-400000", "X")  # nominal beda: bukan kandidat
    _row("TARTUN EDC BRI SUDAH COCOK", "450000", "Y", status=MatchStatus.MATCHED)  # sudah tertutup
    run_match(BD)

    res = client_op.get(f"/pending-settle/?d={BD}")
    items = {o.id: o for o in res.context["items"]}
    assert [c.id for c in items[rev.id].net_candidates] == [refund.id]
    assert [c.id for c in items[refund.id].net_candidates] == [rev.id]
    assert "Netralkan dgn Otomax" in res.content.decode()


@pytest.mark.django_db
def test_netralkan_dari_pending_settle_menutup_selisih_tanpa_menyentuh_bank(client_op):
    rev, refund = _salah_tembak()
    bank = BankMutation.objects.create(
        import_batch=_batch(Channel.BRI),
        channel=Channel.BRI,
        book_date=BD,
        description_raw="TRF LAIN",
        ref_normalized="TRF LAIN",
        amount=Decimal("450000"),
        row_hash=_h("b"),
    )
    run_match(BD)
    assert _open_discs(rev, refund).count() == 2

    res = client_op.post(
        "/reversal/net/",
        {"rev_id": rev.id, "original_id": refund.id, "note": "salah tembak", "next_url": f"/pending-settle/?d={BD}"},
    )
    assert res.status_code == 302

    rev.refresh_from_db()
    refund.refresh_from_db()
    bank.refresh_from_db()
    assert rev.match_status == refund.match_status == MatchStatus.IGNORED
    assert rev.net_pair_id == refund.id and refund.net_pair_id == rev.id
    assert "salah tembak" in rev.note
    # Selisih hantu hilang dari Daftar Selisih; bank & Match tidak disentuh sama sekali.
    assert not Discrepancy.objects.filter(otomax_entry__in=[rev, refund]).exists()
    assert bank.match_status == MatchStatus.UNMATCHED
    assert not Match.objects.exists()

    resolved = client_op.get(f"/pending-settle/?d={BD}&tab=resolved")
    assert {o.id for o in resolved.context["items"]} == {rev.id, refund.id}
    assert "Batalkan Netralkan" in resolved.content.decode()


@pytest.mark.django_db
def test_netralkan_di_hari_yang_sudah_ditutup_lewat_adjustment_bukan_dihapus():
    rev, refund = _salah_tembak()
    run_match(BD)
    ReconDay.objects.update_or_create(book_date=BD, defaults={"locked": True})

    manual_net_reversal(rev, refund, note="koreksi", user=None)

    discs = Discrepancy.objects.filter(otomax_entry__in=[rev, refund])
    assert discs.count() == 2
    assert set(discs.values_list("status", flat=True)) == {DiscrepancyStatus.RESOLVED}
    assert Adjustment.objects.filter(discrepancy__in=discs).count() == 2


@pytest.mark.django_db
def test_halaman_reversal_menawarkan_refund_kategori_lain_sebagai_kandidat(client_op):
    # Entri lama bernominal sama yang sudah cocok ke bank tetap ditampilkan, tapi di bawah
    # (sengaja dibuat duluan supaya id-nya lebih kecil).
    lama = _row("TARTUN EDC BRI LAMA", "450000", "LAIN", book_date=date(2026, 9, 12), status=MatchStatus.MATCHED)
    rev, refund = _salah_tembak()
    admin = _row("POTONG ADMIN", "450000", "DANI")
    admin.category = OtomaxCategory.ADMIN
    admin.save(update_fields=["category"])

    res = client_op.get("/reversal/")
    (item_rev, candidates, _banks) = next(i for i in res.context["items"] if i[0].id == rev.id)
    assert [c.id for c, _m in candidates] == [refund.id, lama.id]


@pytest.mark.django_db
def test_batalkan_netralkan_mengembalikan_keduanya_ke_pending_settle(client_op):
    rev, refund = _salah_tembak()
    run_match(BD)
    manual_net_reversal(rev, refund, note="salah pilih")

    res = client_op.post(f"/reversal/unnet/{refund.id}/", {"next_url": f"/pending-settle/?d={BD}&tab=resolved"})
    assert res.status_code == 302

    rev.refresh_from_db()
    refund.refresh_from_db()
    assert rev.match_status == refund.match_status == MatchStatus.PENDING_SETTLE
    assert rev.net_pair_id is None and refund.net_pair_id is None
    # Kembali tercatat di Daftar Selisih seperti sebelum dinetralkan.
    assert _open_discs(rev, refund).filter(kind=DiscrepancyKind.OTOMAX_ONLY).count() == 2

    pending = client_op.get(f"/pending-settle/?d={BD}")
    assert {o.id for o in pending.context["items"]} == {rev.id, refund.id}


@pytest.mark.django_db
def test_batalkan_netralkan_ditolak_kalau_hari_sudah_ditutup():
    rev, refund = _salah_tembak()
    manual_net_reversal(rev, refund)
    ReconDay.objects.update_or_create(book_date=BD, defaults={"locked": True})

    with pytest.raises(ValueError, match="sudah ditutup"):
        unnet_otomax_pair(rev)

    rev.refresh_from_db()
    assert rev.match_status == MatchStatus.IGNORED


@pytest.mark.django_db
def test_batalkan_netralkan_ditolak_untuk_entri_yang_tidak_dinetralkan():
    rev, _refund = _salah_tembak()
    with pytest.raises(ValueError, match="tidak sedang dinetralkan"):
        unnet_otomax_pair(rev)


@pytest.mark.django_db
def test_netting_otomatis_lintas_hari_menutup_selisih_lama_entri_asli():
    """Regression: entri asli tgl 15 sudah tercatat OTOMAX_ONLY (run tgl 15), REV-nya baru
    datang tgl 16 dan dinetralkan otomatis -- selisih tgl 15 tidak boleh tertinggal OPEN."""
    day1 = date(2026, 9, 15)
    desc = "TARTUN EDC BRI 6013013636952876#192240520005#EDC#TRFLA"
    original = _row(desc, "300000", "PLC ALFA", book_date=day1, at=datetime(2026, 9, 15, 21, 0))
    run_match(day1)
    assert _open_discs(original).count() == 1

    rev = _row("REV " + desc, "-300000", "PLC ALFA", at=datetime(2026, 9, 16, 9, 0))
    run_match(BD)

    original.refresh_from_db()
    rev.refresh_from_db()
    assert original.match_status == rev.match_status == MatchStatus.IGNORED
    assert not _open_discs(original, rev).exists()
