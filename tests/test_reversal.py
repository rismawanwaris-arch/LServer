import hashlib
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.core.enums import Channel, MatchStatus, OtomaxCategory
from apps.ingest.models import BankMutation, ImportBatch, OtomaxEntry
from apps.recon.resolve import manual_net_reversal

User = get_user_model()
BD = date(2026, 9, 13)


def _h(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _batch(channel=Channel.OTOMAX):
    return ImportBatch.objects.create(channel=channel, book_date=BD, source_filename="t", file_hash=_h(BD, channel))


def _bank(desc, amount, channel=Channel.BRI, book_date=BD, status=MatchStatus.UNMATCHED):
    return BankMutation.objects.create(
        import_batch=_batch(channel),
        channel=channel,
        book_date=book_date,
        description_raw=desc,
        ref_normalized=desc,
        ref_core="",
        extracted_tokens=[],
        amount=Decimal(amount),
        match_status=status,
        row_hash=_h("b", desc, amount, book_date),
    )


def _otomax_entry(
    desc, amount, reseller, category=OtomaxCategory.TOPUP_TARTUN, ref_core="", status=MatchStatus.UNMATCHED
):
    return OtomaxEntry.objects.create(
        import_batch=_batch(),
        book_date=BD,
        reseller_name_raw=reseller,
        amount=Decimal(amount),
        description_raw=desc,
        category=category,
        ref_normalized=desc,
        ref_core=ref_core,
        match_status=status,
        row_hash=_h(desc, amount, reseller, category),
    )


@pytest.fixture
def user_client():
    client = Client()
    user = User.objects.create_user(username="operator", password="password123")
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_manual_net_reversal_marks_both_ignored_and_links_net_pair():
    original = _otomax_entry("TARTUN PLC112 SALAH RESELLER", "100000", "PLC112 SALAH")
    rev = _otomax_entry("REV Transfer dari PLC112", "-100000", "Naufal Cell", category=OtomaxCategory.REVERSAL)

    manual_net_reversal(rev, original, note="verifikasi manual", user="admin")

    original.refresh_from_db()
    rev.refresh_from_db()
    assert original.match_status == MatchStatus.IGNORED
    assert rev.match_status == MatchStatus.IGNORED
    assert original.net_pair_id == rev.id
    assert rev.net_pair_id == original.id


@pytest.mark.django_db
def test_manual_net_reversal_rejects_non_opposite_amount():
    original = _otomax_entry("TARTUN X", "100000", "X")
    rev = _otomax_entry("REV Y", "-50000", "Y", category=OtomaxCategory.REVERSAL)

    with pytest.raises(ValueError, match="berlawanan"):
        manual_net_reversal(rev, original)


@pytest.mark.django_db
def test_manual_net_reversal_rejects_already_matched_original():
    original = _otomax_entry("TARTUN X", "100000", "X", status=MatchStatus.MATCHED)
    rev = _otomax_entry("REV X", "-100000", "X", category=OtomaxCategory.REVERSAL)

    with pytest.raises(ValueError, match="sudah tidak berstatus terbuka"):
        manual_net_reversal(rev, original)


@pytest.mark.django_db
def test_reversal_view_lists_unnetted_reversal_with_candidate(user_client):
    _otomax_entry("TARTUN PLC112 SALAH RESELLER", "100000", "PLC112 SALAH", ref_core="PLC112")
    _otomax_entry(
        "REV Transfer dari PLC112",
        "-100000",
        "Naufal Cell",
        category=OtomaxCategory.REVERSAL,
        ref_core="PLC112",
    )

    res = user_client.get("/reversal/")
    assert res.status_code == 200
    body = res.content.decode()
    assert "Naufal Cell" in body
    assert "Netkan Manual" in body


@pytest.mark.django_db
def test_manual_net_reversal_action_nets_via_post(user_client):
    original = _otomax_entry("TARTUN PLC112 SALAH RESELLER", "100000", "PLC112 SALAH", ref_core="PLC112")
    rev = _otomax_entry(
        "REV Transfer dari PLC112", "-100000", "Naufal Cell", category=OtomaxCategory.REVERSAL, ref_core="PLC112"
    )

    res = user_client.post(
        "/reversal/net/",
        {"rev_id": rev.id, "original_id": original.id, "book_date": str(BD), "note": "cek manual"},
    )
    assert res.status_code == 302

    original.refresh_from_db()
    rev.refresh_from_db()
    assert original.match_status == MatchStatus.IGNORED
    assert rev.match_status == MatchStatus.IGNORED


@pytest.mark.django_db
def test_reversal_view_shows_batalkan_pencocokan_for_already_matched_candidate(user_client):
    """Kandidat yang sudah MATCHED ke bank harus bisa dibatalkan langsung dari halaman
    Reversal (bukan disuruh pindah halaman) agar operator bisa koreksi kalau pencocokan
    itu keliru."""
    from apps.recon.resolve import manual_pair_transactions

    bank = _bank("TRF MASUK", "100000")
    original = _otomax_entry("TARTUN PLC112 SALAH RESELLER", "100000", "PLC112 SALAH", ref_core="PLC112")
    manual_pair_transactions(bank_mutation=bank, otomax_entry=original, note="test")
    rev = _otomax_entry(
        "REV Transfer dari PLC112", "-100000", "Naufal Cell", category=OtomaxCategory.REVERSAL, ref_core="PLC112"
    )

    res = user_client.get("/reversal/")
    body = res.content.decode()
    assert "Lihat Pencocokan" in body
    assert "Batalkan Pencocokan" in body
    assert "TRF MASUK" in body  # detail sisi bank ikut ditampilkan

    match = bank.matches.get()
    res2 = user_client.post(
        f"/matches/unpair/{match.id}/", {"next_url": "/reversal/"}
    )
    assert res2.status_code == 302

    original.refresh_from_db()
    rev.refresh_from_db()
    # unpair_match selalu mengembalikan entri Otomax ke PENDING_SETTLE (bukan UNMATCHED) —
    # keduanya tetap dianggap "terbuka" (_OPEN_STATUSES) jadi tetap bisa dinetralkan manual.
    assert original.match_status == MatchStatus.PENDING_SETTLE

    # Setelah dibatalkan, sekarang bisa dinetralkan manual dengan REV-nya.
    manual_net_reversal(rev, original, user="operator")
    original.refresh_from_db()
    assert original.match_status == MatchStatus.IGNORED


@pytest.mark.django_db
def test_reversal_manual_match_to_bank_pairs_reversal_directly(user_client):
    """Kalau sebuah REV ternyata BUKAN koreksi internal murni (ada uang bank yang memang
    bergerak), operator harus bisa memasangkannya langsung ke mutasi bank dari halaman
    Reversal, memakai endpoint manual-match yang sama seperti halaman lain."""
    bank = _bank("REFUND DARI BANK", "100000", book_date=BD)
    rev = _otomax_entry("REV Refund Nyata", "-100000", "Naufal Cell", category=OtomaxCategory.REVERSAL)

    res = user_client.get("/reversal/")
    assert "Pencocokan Manual ke Mutasi Bank" in res.content.decode()

    res2 = user_client.post(
        "/manual-match/",
        {"book_date": str(BD), "otomax_id": rev.id, "bank_id": bank.id, "next_url": "/reversal/"},
    )
    assert res2.status_code == 302

    rev.refresh_from_db()
    assert rev.match_status == MatchStatus.MANUAL

    res3 = user_client.get("/reversal/")
    assert res3.context["belum_count"] == 0
