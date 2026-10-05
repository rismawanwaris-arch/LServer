"""Klasifikasikan baris yang masih terbuka setelah semua pencocokan lain selesai jadi
Discrepancy (BANK_ONLY / OTOMAX_ONLY) — langkah terakhir tiap run_match()."""

from __future__ import annotations

from datetime import date

from apps.core.enums import BANK_CHANNELS, Channel, DiscrepancyKind, DiscrepancyStatus, MatchStatus, OtomaxCategory
from apps.ingest.models import OtomaxEntry
from apps.recon.models import ReconDay

from .helpers import _MATCHABLE_CATEGORIES, _OPEN_STATUSES, RunStats, _make_discrepancy, _mark
from .ref_match import _open_bank


def _reopen_stale_discrepancy(disc) -> bool:
    """Jika ada discrepancy yang berstatus RESOLVED tapi barisnya kembali terbuka (UNMATCHED/PENDING_SETTLE),
    pulihkan kembali jadi OPEN kecuali hari bukunya sudah dikunci."""
    if ReconDay.objects.filter(book_date=disc.origin_book_date, locked=True).exists():
        return False
    disc.status = DiscrepancyStatus.OPEN
    disc.resolved_book_date = None
    disc.resolution_type = ""
    disc.resolution_match = None
    disc.resolved_by = None
    disc.resolved_at = None
    disc.adjustments.all().delete()
    disc.save()
    return True


def _classify_leftovers(book_date: date) -> RunStats:
    stats = RunStats()
    for channel in BANK_CHANNELS:
        for b in _open_bank(book_date, channel):
            if b.discrepancies.filter(status=DiscrepancyStatus.OPEN).exists():
                continue
            stale = b.discrepancies.filter(
                status=DiscrepancyStatus.RESOLVED,
                kind=DiscrepancyKind.BANK_ONLY,
            ).first()
            if stale and _reopen_stale_discrepancy(stale):
                stats.discrepancies += 1
            elif not b.discrepancies.filter(status=DiscrepancyStatus.OPEN).exists():
                _make_discrepancy(book_date, channel, DiscrepancyKind.BANK_ONLY, amount=b.amount, bank=b)
                stats.discrepancies += 1

    # Sisi Otomax: SEMUA entri yang masih terbuka selain potongan admin -- definisi yang sama
    # persis dengan isi halaman Pending Settle, supaya setiap outstanding di sana juga muncul
    # di Daftar Selisih. _OPEN_STATUSES (bukan UNMATCHED saja): entri yang baru di-unpair jadi
    # PENDING_SETTLE tanpa Discrepancy -- tanpa ini carry_forward tidak pernah melihatnya.
    for o in (
        OtomaxEntry.objects.filter(book_date=book_date, match_status__in=_OPEN_STATUSES)
        .exclude(category=OtomaxCategory.ADMIN)
    ):
        if o.discrepancies.filter(status=DiscrepancyStatus.OPEN).exists():
            continue
        stale = o.discrepancies.filter(
            status=DiscrepancyStatus.RESOLVED,
            kind=DiscrepancyKind.OTOMAX_ONLY,
        ).first()
        if stale and _reopen_stale_discrepancy(stale):
            stats.discrepancies += 1
        elif not o.discrepancies.filter(status=DiscrepancyStatus.OPEN).exists():
            make_otomax_only(o)
            stats.discrepancies += 1
    return stats


def make_otomax_only(o: OtomaxEntry):
    """Catat entri Otomax yang masih terbuka sebagai selisih OTOMAX_ONLY di tanggal bukunya
    (juga dipakai saat netralkan dibatalkan, supaya entrinya muncul lagi di Daftar Selisih)."""
    if o.channel_hint in BANK_CHANNELS:
        channel = o.channel_hint
    elif o.category in _MATCHABLE_CATEGORIES:
        channel = Channel.BRI
    else:
        channel = Channel.OTOMAX  # mis. STOR/REV/lain-lain: tidak jelas bank mana
    _mark(o, MatchStatus.PENDING_SETTLE)
    return _make_discrepancy(o.book_date, channel, DiscrepancyKind.OTOMAX_ONLY, amount=-o.amount, otomax=o)
