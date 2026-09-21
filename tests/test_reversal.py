import hashlib
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.core.enums import Channel, MatchStatus, OtomaxCategory
from apps.ingest.models import ImportBatch, OtomaxEntry
from apps.recon.resolve import manual_net_reversal

User = get_user_model()
BD = date(2026, 9, 13)


def _h(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _batch():
    return ImportBatch.objects.create(channel=Channel.OTOMAX, book_date=BD, source_filename="t", file_hash=_h(BD))


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
