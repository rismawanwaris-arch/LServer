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


def _retire_stale_open_discrepancies(book_date: date) -> None:
    """Bersihkan selisih BANK_ONLY/OTOMAX_ONLY yang masih OPEN di book_date padahal
    transaksinya sudah punya Match aktif FINAL (needs_review=False) atau status MANUAL/IGNORED."""
    from apps.core.enums import DiscrepancyKind, DiscrepancyStatus
    from ..models import Discrepancy, Match, ReconDay
    from ..resolve import resolve_discrepancy

    is_locked = ReconDay.objects.filter(book_date=book_date, locked=True).exists()
    open_discs = Discrepancy.objects.filter(
        origin_book_date=book_date,
        status=DiscrepancyStatus.OPEN,
        kind__in=[DiscrepancyKind.BANK_ONLY, DiscrepancyKind.OTOMAX_ONLY],
    ).select_related("bank_mutation", "otomax_entry")

    for disc in open_discs:
        final_match = None
        is_closed_status = False
        if disc.kind == DiscrepancyKind.BANK_ONLY and disc.bank_mutation:
            final_match = (
                Match.objects.filter(
                    models.Q(bank_mutation=disc.bank_mutation) | models.Q(bank_mutations=disc.bank_mutation),
                    voided_at__isnull=True,
                    needs_review=False,
                )
                .order_by("-book_date")
                .first()
            )
            is_closed_status = disc.bank_mutation.match_status in (MatchStatus.MANUAL, MatchStatus.IGNORED)
        elif disc.kind == DiscrepancyKind.OTOMAX_ONLY and disc.otomax_entry:
            final_match = (
                Match.objects.filter(
                    models.Q(otomax_entry=disc.otomax_entry) | models.Q(otomax_entries=disc.otomax_entry),
                    voided_at__isnull=True,
                    needs_review=False,
                )
                .order_by("-book_date")
                .first()
            )
            is_closed_status = disc.otomax_entry.match_status in (MatchStatus.MANUAL, MatchStatus.IGNORED)

        if not final_match and not is_closed_status:
            continue

        if is_locked:
            resolve_discrepancy(
                disc,
                match=final_match,
                resolution_type="LATE_MATCH" if final_match else "DATA_FIX",
                on_date=final_match.book_date if final_match else book_date,
            )
        else:
            disc.delete()


@transaction.atomic
def run_match(book_date: date) -> RunStats:
    _heal_stale_match_statuses()
    _retire_stale_open_discrepancies(book_date)
    stats = RunStats()
    stats.netted = _net_reversals(book_date)
    for channel in BANK_CHANNELS:
        if channel == Channel.MERCHANT_BCA:
            stats.merge(_match_qris(book_date))
        else:
            stats.merge(_match_ref(book_date, channel))
    stats.merge(_classify_leftovers(book_date))
    return stats
