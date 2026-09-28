"""Klasifikasikan baris yang masih terbuka setelah semua pencocokan lain selesai jadi
Discrepancy (BANK_ONLY / OTOMAX_ONLY) — langkah terakhir tiap run_match()."""

from __future__ import annotations

from datetime import date

from apps.core.enums import BANK_CHANNELS, Channel, DiscrepancyKind, MatchStatus, OtomaxCategory
from apps.ingest.models import OtomaxEntry

from .helpers import _MATCHABLE_CATEGORIES, _OPEN_STATUSES, RunStats, _make_discrepancy, _mark
from .ref_match import _open_bank


def _classify_leftovers(book_date: date) -> RunStats:
    stats = RunStats()
    for channel in BANK_CHANNELS:
        for b in _open_bank(book_date, channel).filter(discrepancies__isnull=True):
            _make_discrepancy(book_date, channel, DiscrepancyKind.BANK_ONLY, amount=b.amount, bank=b)
            stats.discrepancies += 1

    # Sisi Otomax: SEMUA entri yang masih terbuka selain potongan admin -- definisi yang sama
    # persis dengan isi halaman Pending Settle, supaya setiap outstanding di sana juga muncul
    # di Daftar Selisih. _OPEN_STATUSES (bukan UNMATCHED saja): entri yang baru di-unpair jadi
    # PENDING_SETTLE tanpa Discrepancy -- tanpa ini carry_forward tidak pernah melihatnya.
    for o in (
        OtomaxEntry.objects.filter(book_date=book_date, match_status__in=_OPEN_STATUSES)
        .exclude(category=OtomaxCategory.ADMIN)
        .filter(discrepancies__isnull=True)
    ):
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
