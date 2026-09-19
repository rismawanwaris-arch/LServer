"""Mesin pencocokan per tanggal buku. Idempoten.

Tahapan tiap `run_match(book_date)`:
  1. Netralkan pasangan REVERSAL Otomax (reversal_netting) — lintas semua tanggal, bukan
     cuma book_date yang diminta.
  2. Cocokkan mutasi bank book_date per channel: QRIS lewat qris_match (agregat per
     reseller), sisanya (BRI/BCA/Mandiri) lewat ref_match (per-ref exact/token/fuzzy).
  3. Sisa yang masih terbuka setelah itu diklasifikasikan jadi Discrepancy (leftovers).

Lihat ARCHITECTURE.md di root repo untuk penjelasan aturan bisnis lengkap (kenapa
netting pakai ref_core, apa itu PENDING_SETTLE, dll).
"""

from __future__ import annotations

from datetime import date

from django.db import transaction

from apps.core.enums import BANK_CHANNELS, Channel

from .helpers import RunStats
from .leftovers import _classify_leftovers
from .qris_match import _match_qris
from .ref_match import _match_ref
from .reversal_netting import _net_reversals

__all__ = ["RunStats", "run_match"]


@transaction.atomic
def run_match(book_date: date) -> RunStats:
    stats = RunStats()
    stats.netted = _net_reversals()
    for channel in BANK_CHANNELS:
        if channel == Channel.MERCHANT_BCA:
            stats.merge(_match_qris(book_date))
        else:
            stats.merge(_match_ref(book_date, channel))
    stats.merge(_classify_leftovers(book_date))
    return stats
