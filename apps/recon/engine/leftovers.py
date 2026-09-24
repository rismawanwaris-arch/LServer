"""Klasifikasikan baris yang masih terbuka setelah semua pencocokan lain selesai jadi
Discrepancy (BANK_ONLY / OTOMAX_ONLY) — langkah terakhir tiap run_match()."""

from __future__ import annotations

from datetime import date

from apps.core.enums import BANK_CHANNELS, Channel, DiscrepancyKind, MatchStatus, OtomaxCategory
from apps.ingest.models import OtomaxEntry

from .helpers import RunStats, _make_discrepancy, _mark
from .ref_match import _open_bank


def _classify_leftovers(book_date: date) -> RunStats:
    stats = RunStats()
    for channel in BANK_CHANNELS:
        for b in _open_bank(book_date, channel).filter(discrepancies__isnull=True):
            _make_discrepancy(book_date, channel, DiscrepancyKind.BANK_ONLY, amount=b.amount, bank=b)
            stats.discrepancies += 1

    for o in OtomaxEntry.objects.filter(
        book_date=book_date, category=OtomaxCategory.TOPUP_TARTUN, match_status=MatchStatus.UNMATCHED
    ).filter(discrepancies__isnull=True):
        channel = o.channel_hint if o.channel_hint in BANK_CHANNELS else Channel.BRI
        _mark(o, MatchStatus.PENDING_SETTLE)
        _make_discrepancy(book_date, channel, DiscrepancyKind.OTOMAX_ONLY, amount=-o.amount, otomax=o)
        stats.discrepancies += 1
    return stats
