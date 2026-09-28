"""Regresi untuk tiga kasus nyata dari produksi:

1. SAHIDIN -- mutasi Mandiri 30 Agu "MCM InhouseTrf DARI SAHIDIN 99101" Rp 500.000, entri
   OTOMAX-nya baru ditembak 3 Sep sebagai "BAYAR KE MANDIRI ... TGL 30/AGS/2026". Dulu
   "BAYAR KE MANDIRI" jatuh ke kategori OTHER tanpa channel -> tidak pernah dicocokkan
   mesin, dan dropdown manual di kedua halaman juga tidak saling menampilkan (beda 4 hari).
2. Koreksi operator yang ditembak sebagai selisih: salah input +3.540.000 lalu dikoreksi
   -90.000 (bukan dibalik lalu dientri ulang 3.450.000) -> operator perlu menggabungkan dua
   entri Otomax untuk satu mutasi bank secara manual.
3. Selisih "hantu": carry_forward memasangkan susulan tapi OTOMAX_ONLY/BANK_ONLY milik baris
   "baru"-nya tetap OPEN di Daftar Selisih.
"""

import hashlib
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client

from apps.core.enums import Channel, DiscrepancyKind, DiscrepancyStatus, MatchStatus, MatchType, OtomaxCategory
from apps.core.normalize import norm_ref
from apps.ingest.models import ImportBatch, OtomaxEntry
from apps.ingest.services import otomax_derived_fields
from apps.recon.carry import carry_forward
from apps.recon.close import close_day
from apps.recon.engine import run_match
from apps.recon.models import Adjustment, Discrepancy, Match, ReconDay
from apps.recon.resolve import manual_pair_many, manual_pair_transactions, unpair_match

from .test_engine import _bank, _otomax

AUG30 = date(2026, 8, 30)
SEP3 = date(2026, 9, 3)
SAHIDIN_BANK = "MCM InhouseTrf DARI SAHIDIN 99101"
SAHIDIN_OTOMAX = "BAYAR KE MANDIRI MCM InhouseTrf DARI SAHIDIN 99101 TGL 30/AGS/2026"


@pytest.fixture
def auth_client():
    client = Client()
    user = User.objects.create_superuser(username="admin_bayar", password="password123")
    client.force_login(user)
    return client


def _payment(desc, amount, book_date, reseller="PLC PC5 CIGENDING", **overrides):
    """Entri OTOMAX dengan field turunan persis seperti hasil impor sungguhan."""
    fields = {**otomax_derived_fields(desc), **overrides}
    return OtomaxEntry.objects.create(
        import_batch=ImportBatch.objects.create(
            channel=Channel.OTOMAX, book_date=book_date, source_filename="o", file_hash="h"
        ),
        book_date=book_date,
        reseller_name_raw=reseller,
        amount=Decimal(amount),
        description_raw=desc,
        row_hash=hashlib.sha256(f"p|{desc}|{amount}|{book_date}".encode()).hexdigest(),
        **fields,
    )


def _open_discs(**filters):
    return Discrepancy.objects.filter(status=DiscrepancyStatus.OPEN, **filters)


# --- 1. SAHIDIN: entri OTOMAX telat 4 hari -------------------------------------------


@pytest.mark.django_db
def test_sahidin_otomax_entered_4_days_late_is_matched_by_carry_forward():
    bm = _bank(SAHIDIN_BANK, "500000", channel=Channel.MANDIRI, book_date=AUG30)
    run_match(AUG30)
    stale = Discrepancy.objects.get(bank_mutation=bm, kind=DiscrepancyKind.BANK_ONLY, status=DiscrepancyStatus.OPEN)

    oe = _payment(SAHIDIN_OTOMAX, "500000", SEP3)
    assert (oe.category, oe.channel_hint) == (OtomaxCategory.PAYMENT, Channel.MANDIRI)
    run_match(SEP3)
    # leftovers sempat mencatat OTOMAX_ONLY untuk entri 3 Sep -- inilah calon selisih "hantu".
    assert _open_discs(otomax_entry=oe, kind=DiscrepancyKind.OTOMAX_ONLY).exists()

    assert carry_forward(SEP3) == 1

    bm.refresh_from_db()
    oe.refresh_from_db()
    assert bm.match_status == MatchStatus.MATCHED
    assert oe.match_status == MatchStatus.MATCHED
    match = Match.objects.get(bank_mutation=bm, voided_at__isnull=True)
    assert match.otomax_entry_id == oe.id
    assert match.amount_diff == Decimal("0.00")

    stale.refresh_from_db()
    assert stale.status == DiscrepancyStatus.RESOLVED
    assert stale.resolution_type == "LATE_MATCH"
    assert Adjustment.objects.filter(discrepancy=stale, book_date=AUG30).exists()
    assert not _open_discs(otomax_entry=oe).exists()
    assert not _open_discs(bank_mutation=bm).exists()


@pytest.mark.django_db
def test_otomax_first_then_bank_later_is_matched_without_ghost_bank_discrepancy():
    oe = _payment("BAYAR KE MANDIRI MCM InhouseTrf DARI SAHIDIN 99101", "500000", AUG30)
    run_match(AUG30)
    stale = Discrepancy.objects.get(otomax_entry=oe, kind=DiscrepancyKind.OTOMAX_ONLY)

    bm = _bank(SAHIDIN_BANK, "500000", channel=Channel.MANDIRI, book_date=SEP3)
    run_match(SEP3)
    assert _open_discs(bank_mutation=bm, kind=DiscrepancyKind.BANK_ONLY).exists()

    assert carry_forward(SEP3) == 1

    stale.refresh_from_db()
    assert stale.status == DiscrepancyStatus.RESOLVED
    assert Match.objects.filter(bank_mutation=bm, otomax_entry=oe, voided_at__isnull=True).exists()
    assert not _open_discs(bank_mutation=bm).exists()


@pytest.mark.django_db
def test_counterpart_discrepancy_on_closed_day_is_resolved_not_deleted():
    bm = _bank(SAHIDIN_BANK, "500000", channel=Channel.MANDIRI, book_date=AUG30)
    run_match(AUG30)
    oe = _payment(SAHIDIN_OTOMAX, "500000", SEP3)
    run_match(SEP3)
    counterpart = Discrepancy.objects.get(otomax_entry=oe, kind=DiscrepancyKind.OTOMAX_ONLY)
    close_day(SEP3, force=True)  # selisihnya sudah jadi bagian snapshot beku

    carry_forward(SEP3)

    counterpart.refresh_from_db()
    assert counterpart.status == DiscrepancyStatus.RESOLVED
    assert Adjustment.objects.filter(discrepancy=counterpart, book_date=SEP3).exists()
    assert Match.objects.filter(bank_mutation=bm, otomax_entry=oe, voided_at__isnull=True).exists()


# --- 2. Gabungkan beberapa entri OTOMAX secara manual (koreksi -90.000) ---------------

D = date(2026, 9, 10)


def _koreksi_case():
    bm = _bank("TRANSFER DARI BUDI SANTOSO VIA BRIMO", "3450000", channel=Channel.BRI, book_date=D)
    wrong = _otomax("TARTUN TF BRI TRANSFER DARI BUDI SANTOSO", "3540000", channel=Channel.BRI, book_date=D)
    fix = _otomax("TARTUN TF BRI KOREKSI BUDI SANTOSO", "-90000", channel=Channel.BRI, book_date=D)
    run_match(D)  # ketiganya jadi leftover
    for row in (bm, wrong, fix):
        row.refresh_from_db()
    return bm, wrong, fix


@pytest.mark.django_db
def test_manual_pair_many_combines_wrong_entry_and_correction():
    bm, wrong, fix = _koreksi_case()

    match = manual_pair_many(bm, [wrong, fix], note="Koreksi -90rb")

    assert match.match_type == MatchType.MANUAL
    assert match.amount_otomax == Decimal("3450000.00")
    assert match.amount_diff == Decimal("0.00")
    assert match.otomax_entry_id == wrong.id  # nominal terbesar jadi baris utama tampilan
    assert set(match.otomax_entries.values_list("id", flat=True)) == {wrong.id, fix.id}
    for row in (bm, wrong, fix):
        row.refresh_from_db()
        assert row.match_status == MatchStatus.MANUAL
    assert not _open_discs(bank_mutation=bm).exists()
    assert not _open_discs(otomax_entry__in=[wrong, fix]).exists()


@pytest.mark.django_db
def test_manual_pair_many_with_remaining_diff_keeps_amount_diff_open():
    bm, wrong, _ = _koreksi_case()
    partial = _otomax("TARTUN TF BRI KOREKSI SEBAGIAN", "-80000", channel=Channel.BRI, book_date=D)

    match = manual_pair_many(bm, [wrong, partial])

    assert match.amount_diff == Decimal("-10000.00")
    disc = Discrepancy.objects.get(bank_mutation=bm, kind=DiscrepancyKind.AMOUNT_DIFF)
    assert disc.status == DiscrepancyStatus.OPEN
    assert disc.amount == Decimal("-10000.00")


@pytest.mark.django_db
def test_manual_pair_many_rejects_invalid_selection():
    bm, wrong, fix = _koreksi_case()
    with pytest.raises(ValueError):
        manual_pair_many(bm, [wrong])  # minimal 2 entri

    other_bank = _bank("TRANSFER LAIN", "3540000", channel=Channel.BRI, book_date=D)
    manual_pair_transactions(bank_mutation=other_bank, otomax_entry=wrong)
    with pytest.raises(ValueError):
        manual_pair_many(bm, [wrong, fix])  # wrong sudah punya pasangan aktif

    third = _otomax("TARTUN TF BRI LAIN", "100000", channel=Channel.BRI, book_date=D)
    fourth = _otomax("TARTUN TF BRI LAIN 2", "200000", channel=Channel.BRI, book_date=D)
    with pytest.raises(ValueError):
        manual_pair_many(other_bank, [third, fourth])  # mutasi bank sudah punya pasangan aktif


@pytest.mark.django_db
def test_unpair_combined_match_releases_all_entries():
    bm, wrong, fix = _koreksi_case()
    match = manual_pair_many(bm, [wrong, fix])

    unpair_match(match)

    bm.refresh_from_db()
    assert bm.match_status == MatchStatus.UNMATCHED
    for row in (wrong, fix):
        row.refresh_from_db()
        assert row.match_status == MatchStatus.PENDING_SETTLE


@pytest.mark.django_db
def test_manual_match_view_accepts_multiple_otomax_ids(auth_client):
    bm, wrong, fix = _koreksi_case()

    res = auth_client.post(
        "/manual-match/",
        {"bank_id": bm.pk, "otomax_ids": [wrong.pk, fix.pk], "book_date": D.isoformat(), "next_url": "/"},
    )

    assert res.status_code == 302
    match = Match.objects.get(bank_mutation=bm, voided_at__isnull=True)
    assert match.amount_diff == Decimal("0.00")
    assert match.otomax_entries.count() == 2


@pytest.mark.django_db
def test_pending_settle_offers_match_where_otomax_is_larger_and_merges_correction(auth_client):
    bm, wrong, fix = _koreksi_case()
    first = manual_pair_transactions(bank_mutation=bm, otomax_entry=wrong)
    assert first.amount_diff == Decimal("-90000.00")  # Otomax lebih besar dari bank

    res = auth_client.get("/pending-settle/", {"d": D.isoformat()})
    assert first.id in [m.id for m in res.context["diff_matches"]]

    auth_client.post(
        "/manual-match/", {"bank_id": bm.pk, "otomax_id": fix.pk, "book_date": D.isoformat(), "next_url": "/"}
    )
    first.refresh_from_db()
    assert first.amount_diff == Decimal("0.00")


# --- Kandidat manual dari luar jendela ±2 hari ---------------------------------------


@pytest.mark.django_db
def test_review_manual_offers_late_otomax_with_same_amount_or_matching_tgl(auth_client):
    _bank(SAHIDIN_BANK, "500000", channel=Channel.MANDIRI, book_date=AUG30)
    sahidin = _payment(SAHIDIN_OTOMAX, "500000", SEP3)
    tgl_only = _payment("BAYAR KE MANDIRI TRANSFER LAIN TGL 30/AGS/2026", "777000", SEP3)
    unrelated = _payment("BAYAR KE MANDIRI TRANSFER LAIN LAGI", "123000", SEP3)

    res = auth_client.get("/review-manual/", {"d": AUG30.isoformat()})

    candidates = {c["id"]: c for c in res.context["otomax_candidates"]}
    assert candidates[sahidin.id]["extended"] is True
    assert candidates[sahidin.id]["day_gap"] == 4
    assert candidates[sahidin.id]["tgl_match"] is True
    assert candidates[sahidin.id]["cents"] == 50000000
    assert tgl_only.id in candidates
    assert unrelated.id not in candidates


@pytest.mark.django_db
def test_pending_settle_offers_bank_mutation_from_days_earlier(auth_client):
    bm = _bank(SAHIDIN_BANK, "500000", channel=Channel.MANDIRI, book_date=AUG30)
    _payment(SAHIDIN_OTOMAX, "500000", SEP3)

    res = auth_client.get("/pending-settle/", {"d": SEP3.isoformat()})

    banks = {b.id: b for b in res.context["unmatched_banks"]}
    assert bm.id in banks
    assert banks[bm.id].is_extended is True


# --- Klasifikasi ulang data lama -----------------------------------------------------


def _legacy(desc, amount, book_date, **kw):
    """Entri yang diimpor dengan aturan LAMA: OTHER, tanpa channel, prefix tidak dibuang."""
    return _payment(
        desc, amount, book_date, category=OtomaxCategory.OTHER, channel_hint="",
        ref_normalized=norm_ref(desc), ref_core="", extracted_tokens=[], **kw,
    )


@pytest.mark.django_db
def test_reclassify_otomax_dry_run_then_apply():
    open_row = _legacy(SAHIDIN_OTOMAX, "500000", SEP3, match_status=MatchStatus.PENDING_SETTLE)
    matched_row = _legacy("BAYAR KE MANDIRI SUDAH COCOK", "100000", SEP3, match_status=MatchStatus.MATCHED)
    locked_day = date(2026, 9, 1)
    ReconDay.objects.create(book_date=locked_day, locked=True)
    locked_row = _legacy("BAYAR KE MANDIRI HARI DITUTUP", "200000", locked_day)

    call_command("reclassify_otomax", stdout=StringIO())
    open_row.refresh_from_db()
    assert open_row.category == OtomaxCategory.OTHER  # dry-run tidak mengubah apa pun

    out = StringIO()
    call_command("reclassify_otomax", "--apply", stdout=out)

    open_row.refresh_from_db()
    assert open_row.category == OtomaxCategory.PAYMENT
    assert open_row.channel_hint == Channel.MANDIRI
    assert open_row.ref_normalized == norm_ref(SAHIDIN_BANK)
    for untouched in (matched_row, locked_row):
        untouched.refresh_from_db()
        assert untouched.category == OtomaxCategory.OTHER
    assert "2026-09-03" in out.getvalue()
