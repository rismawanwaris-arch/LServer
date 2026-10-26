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

from django.db import models, transaction

from apps.core.enums import BANK_CHANNELS, Channel, MatchStatus
from apps.ingest.models import BankMutation, OtomaxEntry

from .helpers import _OPEN_STATUSES, RunStats
from .leftovers import _classify_leftovers
from .qris_match import _match_qris
from .ref_match import _match_ref
from .reversal_netting import _net_reversals

__all__ = ["RunStats", "run_match"]


def _heal_stale_match_statuses() -> None:
    """Sinkronkan match_status transaksi yang masih tercatat UNMATCHED/PENDING_SETTLE padahal
    sudah memiliki Match aktif (voided_at__isnull=True) agar tidak memicu IntegrityError
    uniq_active_bank_match / uniq_active_otomax_match."""
    from ..models import Match

    active_bank_subq = Match.objects.filter(
        models.Q(bank_mutation=models.OuterRef("pk")) | models.Q(bank_mutations=models.OuterRef("pk")),
        voided_at__isnull=True,
    )
    BankMutation.objects.filter(
        match_status=MatchStatus.UNMATCHED,
    ).filter(models.Exists(active_bank_subq)).update(match_status=MatchStatus.MATCHED)

    active_otomax_subq = Match.objects.filter(
        models.Q(otomax_entry=models.OuterRef("pk")) | models.Q(otomax_entries=models.OuterRef("pk")),
        voided_at__isnull=True,
    )
    OtomaxEntry.objects.filter(
        match_status__in=_OPEN_STATUSES,
    ).filter(models.Exists(active_otomax_subq)).update(match_status=MatchStatus.MATCHED)


@transaction.atomic
def run_match(book_date: date) -> RunStats:
    _heal_stale_match_statuses()
    stats = RunStats()
    stats.netted = _net_reversals(book_date)
    for channel in BANK_CHANNELS:
        if channel == Channel.MERCHANT_BCA:
            stats.merge(_match_qris(book_date))
        else:
            stats.merge(_match_ref(book_date, channel))
    stats.merge(_classify_leftovers(book_date))
    return stats
