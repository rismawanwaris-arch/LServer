"""Bersihkan selisih hantu lama: OTOMAX_ONLY yang masih OPEN padahal entrinya sudah
dinetralkan (netting otomatis lintas hari / netting manual sebelum netralkan ikut menutup
selisihnya)."""

import hashlib
import io
from datetime import date
from decimal import Decimal

import pytest
from django.core.management import call_command

from apps.core.enums import Channel, DiscrepancyKind, DiscrepancyStatus, MatchStatus, OtomaxCategory
from apps.ingest.models import ImportBatch, OtomaxEntry
from apps.recon.engine.helpers import _make_discrepancy
from apps.recon.models import Adjustment, Discrepancy, ReconDay

D1 = date(2026, 9, 15)
D2 = date(2026, 9, 16)


def _h(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _entry(desc, amount, book_date, category=OtomaxCategory.TOPUP_TARTUN, status=MatchStatus.PENDING_SETTLE):
    batch = ImportBatch.objects.create(
        channel=Channel.OTOMAX, book_date=book_date, source_filename="t", file_hash=_h(desc, book_date)
    )
    return OtomaxEntry.objects.create(
        import_batch=batch,
        book_date=book_date,
        reseller_name_raw="PLC ALFA",
        amount=Decimal(amount),
        description_raw=desc,
        category=category,
        ref_normalized=desc,
        match_status=status,
        row_hash=_h("o", desc, amount, book_date),
    )


def _ghost_pair():
    """Kondisi data lama: entri asli tgl 15 sudah tercatat OTOMAX_ONLY, lalu dinetralkan
    otomatis oleh REV tgl 16 tanpa selisihnya ikut ditutup."""
    original = _entry("TARTUN EDC BRI 123", "300000", D1)
    ghost = _make_discrepancy(D1, Channel.BRI, DiscrepancyKind.OTOMAX_ONLY, amount=-original.amount, otomax=original)
    rev = _entry("REV TARTUN EDC BRI 123", "-300000", D2, category=OtomaxCategory.REVERSAL)
    for a, b in ((original, rev), (rev, original)):
        a.match_status = MatchStatus.IGNORED
        a.net_pair = b
        a.save(update_fields=["match_status", "net_pair"])
    return original, rev, ghost


def _run(*args):
    out = io.StringIO()
    call_command("cleanup_netted_discrepancies", *args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
def test_dry_run_hanya_melaporkan():
    _original, _rev, ghost = _ghost_pair()

    out = _run()

    assert ghost.code in out
    assert "DRY-RUN" in out
    ghost.refresh_from_db()
    assert ghost.status == DiscrepancyStatus.OPEN


@pytest.mark.django_db
def test_apply_menghapus_selisih_hantu_di_hari_yang_belum_ditutup():
    original, _rev, ghost = _ghost_pair()
    masih_terbuka = _entry("TARTUN EDC BRI 999", "150000", D1)
    asli = _make_discrepancy(
        D1, Channel.BRI, DiscrepancyKind.OTOMAX_ONLY, amount=Decimal("-150000"), otomax=masih_terbuka
    )

    _run("--apply")

    assert not Discrepancy.objects.filter(pk=ghost.pk).exists()
    # Selisih entri yang memang masih outstanding tidak ikut tersentuh.
    asli.refresh_from_db()
    assert asli.status == DiscrepancyStatus.OPEN


@pytest.mark.django_db
def test_apply_di_hari_yang_sudah_ditutup_lewat_adjustment():
    _original, _rev, ghost = _ghost_pair()
    ReconDay.objects.create(book_date=D1, locked=True)

    _run("--apply")

    ghost.refresh_from_db()
    assert ghost.status == DiscrepancyStatus.RESOLVED
    assert Adjustment.objects.filter(discrepancy=ghost).count() == 1


@pytest.mark.django_db
def test_tidak_ada_yang_perlu_dibersihkan():
    assert "Tidak ada selisih hantu" in _run("--apply")
