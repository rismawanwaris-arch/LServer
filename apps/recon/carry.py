"""Coba selesaikan discrepancy OPEN dari hari-hari sebelumnya dengan data hari ini."""

from __future__ import annotations

from datetime import date

from django.db import transaction

from apps.core.enums import Channel, DiscrepancyKind, DiscrepancyStatus, MatchStatus, MatchType
from apps.ingest.models import BankMutation, OtomaxEntry

from .engine.helpers import _MATCHABLE_CATEGORIES, _proposal_rejected
from .models import Discrepancy, Match
from .resolve import resolve_discrepancy, retire_leftover_discrepancies

# Teks yang lebih pendek dari ini terlalu umum untuk dianggap "mirip" (mis. "TRANSFER").
_MIN_SIMILAR_TEXT = 12
_SIMILAR_REASON = "Susulan lintas hari, teks mirip tapi tidak persis"


@transaction.atomic
def carry_forward(book_date: date, user=None) -> int:
    """Return jumlah discrepancy lama yang berhasil ditutup oleh data book_date.

    Pasangan susulan yang teksnya cuma mirip dibuat sebagai USULAN: discrepancy lamanya
    sengaja belum diselesaikan (Adjustment append-only, tidak bisa dibatalkan kalau usulan
    ditolak) -- itu dilakukan approve_match() saat operator menyetujui."""
    resolved = 0
    stale = Discrepancy.objects.filter(status="OPEN", origin_book_date__lt=book_date).select_related(
        "bank_mutation", "otomax_entry"
    )

    for disc in stale:
        # Iterasi sebelumnya bisa saja sudah menutup/menghapus disc ini sebagai selisih sisi
        # lawan (lihat retire_leftover_discrepancies) -- queryset `stale` sudah dievaluasi.
        if not Discrepancy.objects.filter(pk=disc.pk, status=DiscrepancyStatus.OPEN).exists():
            continue
        match = None
        counterpart = None
        if disc.kind == DiscrepancyKind.BANK_ONLY and disc.bank_mutation:
            match = _find_otomax_for(disc.bank_mutation, book_date)
            if match:
                counterpart = Discrepancy.objects.filter(otomax_entry=match.otomax_entry)
        elif disc.kind == DiscrepancyKind.OTOMAX_ONLY and disc.otomax_entry:
            match = _find_bank_for(disc.otomax_entry, book_date)
            if match:
                counterpart = Discrepancy.objects.filter(bank_mutation=match.bank_mutation)
        if match and not match.needs_review:
            retire_leftover_discrepancies(counterpart, match, user, book_date)
            resolve_discrepancy(disc, match=match, resolution_type="LATE_MATCH", user=user, on_date=book_date)
            resolved += 1
    return resolved


def _similar(a: str, b: str) -> bool:
    """Teks satu terkandung utuh di teks lain (mis. operator tidak menyalin nomor di ujung
    keterangan bank), dan cukup panjang untuk tidak kebetulan sama."""
    shorter, longer = sorted((a or "", b or ""), key=len)
    return len(shorter) >= _MIN_SIMILAR_TEXT and shorter in longer


_OTOMAX_OPEN_STATUSES = [MatchStatus.UNMATCHED, MatchStatus.PENDING_SETTLE]


def _find_otomax_for(bank: BankMutation, book_date: date) -> Match | None:
    # Cek status terbaru langsung dari DB, bukan percaya field di objek Python `bank` yang
    # mungkin sudah basi (mis. kalau ada dua Discrepancy duplikat menunjuk bank_mutation yang
    # sama - baris ini bisa saja SUDAH dipasangkan oleh iterasi lain dalam loop carry_forward
    # yang sama). Tanpa cek ini, Match.objects.create() di bawah bisa bentrok dengan constraint
    # uniq_active_bank_match dan bikin seluruh "Jalankan Matching Engine" gagal 500.
    if not BankMutation.objects.filter(pk=bank.pk, match_status=MatchStatus.UNMATCHED).exists():
        return None
    o = (
        OtomaxEntry.objects.filter(
            book_date=book_date,
            category__in=_MATCHABLE_CATEGORIES,
            channel_hint=bank.channel,
            amount=bank.amount,
            match_status__in=_OTOMAX_OPEN_STATUSES,
        )
        .filter(ref_core=bank.ref_core)
        .first()
        if bank.ref_core
        else None
    )
    same_amount = OtomaxEntry.objects.filter(
        book_date=book_date,
        category__in=_MATCHABLE_CATEGORIES,
        channel_hint=bank.channel,
        amount=bank.amount,
        match_status__in=_OTOMAX_OPEN_STATUSES,
    )
    o = o or same_amount.filter(ref_normalized=bank.ref_normalized).first()
    if o:
        return _persist(book_date, bank.channel, bank, o)

    # Tidak persis: usulkan kalau ada TEPAT SATU kandidat yang teksnya mirip -- lebih dari
    # satu berarti ambigu, jangan ditebak.
    if _proposal_rejected(bank):
        return None
    similar = [c for c in same_amount if _similar(c.ref_normalized, bank.ref_normalized)]
    if len(similar) != 1:
        return None
    return _persist(book_date, bank.channel, bank, similar[0], review_reason=_SIMILAR_REASON)


def _find_bank_for(otomax: OtomaxEntry, book_date: date) -> Match | None:
    # Lihat catatan di _find_otomax_for soal kenapa ini harus dicek fresh dari DB.
    if not OtomaxEntry.objects.filter(pk=otomax.pk, match_status__in=_OTOMAX_OPEN_STATUSES).exists():
        return None
    channel = otomax.channel_hint or Channel.BRI
    same_amount = BankMutation.objects.filter(
        book_date=book_date,
        channel=channel,
        amount=otomax.amount,
        match_status=MatchStatus.UNMATCHED,
    )
    b = same_amount.filter(ref_normalized=otomax.ref_normalized).first()
    if not b and otomax.ref_core:
        b = same_amount.filter(ref_core=otomax.ref_core).first()
    if b:
        return _persist(book_date, channel, b, otomax)

    similar = [c for c in same_amount if _similar(otomax.ref_normalized, c.ref_normalized)]
    if len(similar) != 1 or _proposal_rejected(similar[0]):
        return None
    return _persist(book_date, channel, similar[0], otomax, review_reason=_SIMILAR_REASON)


def _persist(book_date, channel, bank, otomax, review_reason: str = "") -> Match:
    match = Match.objects.create(
        book_date=book_date,
        channel=channel,
        bank_mutation=bank,
        otomax_entry=otomax,
        match_type=MatchType.MANUAL,
        amount_bank=bank.amount,
        amount_otomax=otomax.amount,
        note="carry-forward",
        needs_review=bool(review_reason),
        review_reason=review_reason,
    )
    for row in (bank, otomax):
        row.match_status = MatchStatus.MATCHED
        row.save(update_fields=["match_status"])
    return match
