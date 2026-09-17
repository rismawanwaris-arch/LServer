"""Coba selesaikan discrepancy OPEN dari hari-hari sebelumnya dengan data hari ini."""

from __future__ import annotations

from datetime import date

from django.db import transaction

from apps.core.enums import Channel, DiscrepancyKind, MatchStatus, MatchType, OtomaxCategory
from apps.ingest.models import BankMutation, OtomaxEntry

from .models import Discrepancy, Match
from .resolve import resolve_discrepancy


@transaction.atomic
def carry_forward(book_date: date, user=None) -> int:
    """Return jumlah discrepancy lama yang berhasil ditutup oleh data book_date."""
    resolved = 0
    stale = Discrepancy.objects.filter(status="OPEN", origin_book_date__lt=book_date).select_related(
        "bank_mutation", "otomax_entry"
    )

    for disc in stale:
        match = None
        if disc.kind == DiscrepancyKind.BANK_ONLY and disc.bank_mutation:
            match = _find_otomax_for(disc.bank_mutation, book_date)
        elif disc.kind == DiscrepancyKind.OTOMAX_ONLY and disc.otomax_entry:
            match = _find_bank_for(disc.otomax_entry, book_date)
        if match:
            resolve_discrepancy(disc, match=match, resolution_type="LATE_MATCH", user=user, on_date=book_date)
            resolved += 1
    return resolved


_OTOMAX_OPEN_STATUSES = [MatchStatus.UNMATCHED, MatchStatus.PENDING_SETTLE]


def _find_otomax_for(bank: BankMutation, book_date: date) -> Match | None:
    o = (
        OtomaxEntry.objects.filter(
            book_date=book_date,
            category=OtomaxCategory.TOPUP_TARTUN,
            channel_hint=bank.channel,
            amount=bank.amount,
            match_status__in=_OTOMAX_OPEN_STATUSES,
        )
        .filter(ref_core=bank.ref_core)
        .first()
        if bank.ref_core
        else None
    )
    o = (
        o
        or OtomaxEntry.objects.filter(
            book_date=book_date,
            category=OtomaxCategory.TOPUP_TARTUN,
            channel_hint=bank.channel,
            amount=bank.amount,
            ref_normalized=bank.ref_normalized,
            match_status__in=_OTOMAX_OPEN_STATUSES,
        ).first()
    )
    if not o:
        return None
    return _persist(book_date, bank.channel, bank, o)


def _find_bank_for(otomax: OtomaxEntry, book_date: date) -> Match | None:
    channel = otomax.channel_hint or Channel.BRI
    b = (
        BankMutation.objects.filter(
            book_date=book_date,
            channel=channel,
            amount=otomax.amount,
            match_status=MatchStatus.UNMATCHED,
        )
        .filter(ref_normalized=otomax.ref_normalized)
        .first()
    )
    if not b and otomax.ref_core:
        b = BankMutation.objects.filter(
            book_date=book_date,
            channel=channel,
            amount=otomax.amount,
            ref_core=otomax.ref_core,
            match_status=MatchStatus.UNMATCHED,
        ).first()
    if not b:
        return None
    return _persist(book_date, channel, b, otomax)


def _persist(book_date, channel, bank, otomax) -> Match:
    match = Match.objects.create(
        book_date=book_date,
        channel=channel,
        bank_mutation=bank,
        otomax_entry=otomax,
        match_type=MatchType.MANUAL,
        amount_bank=bank.amount,
        amount_otomax=otomax.amount,
        note="carry-forward",
    )
    for row in (bank, otomax):
        row.match_status = MatchStatus.MATCHED
        row.save(update_fields=["match_status"])
    return match
