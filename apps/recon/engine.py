"""Mesin pencocokan per tanggal buku. Idempoten."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from rapidfuzz import fuzz

from apps.catalog.models import MerchantMap
from apps.core.enums import (
    BANK_CHANNELS,
    Channel,
    DiscrepancyKind,
    MatchStatus,
    MatchType,
    OtomaxCategory,
)
from apps.ingest.models import BankMutation, OtomaxEntry

from .models import Discrepancy, Match

ZERO = Decimal("0.00")


@dataclass
class RunStats:
    matched: int = 0
    discrepancies: int = 0

    def merge(self, other: RunStats):
        self.matched += other.matched
        self.discrepancies += other.discrepancies


@transaction.atomic
def run_match(book_date: date) -> RunStats:
    stats = RunStats()
    for channel in BANK_CHANNELS:
        if channel == Channel.MERCHANT_BCA:
            stats.merge(_match_qris(book_date))
        else:
            stats.merge(_match_ref(book_date, channel))
    stats.merge(_classify_leftovers(book_date))
    return stats


# --- per-ref channels (BRI / BCA / MANDIRI) ---------------------------------

def _open_bank(book_date, channel):
    return BankMutation.objects.filter(
        book_date=book_date, channel=channel, match_status=MatchStatus.UNMATCHED
    )


def _open_otomax(book_date, channel):
    return OtomaxEntry.objects.filter(
        book_date=book_date,
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=channel,
        match_status=MatchStatus.UNMATCHED,
    )


def _match_ref(book_date: date, channel: str) -> RunStats:
    stats = RunStats()
    otomax = list(_open_otomax(book_date, channel))
    by_norm: dict[tuple[str, Decimal], list] = defaultdict(list)
    by_core: dict[tuple[str, Decimal], list] = defaultdict(list)
    for o in otomax:
        by_norm[(o.ref_normalized, o.amount)].append(o)
        if o.ref_core:
            by_core[(o.ref_core, o.amount)].append(o)

    used: set[int] = set()

    def take(bucket, key):
        for cand in bucket.get(key, []):
            if cand.id not in used:
                used.add(cand.id)
                return cand
        return None

    for b in _open_bank(book_date, channel):
        o = take(by_norm, (b.ref_normalized, b.amount))
        mtype = MatchType.AUTO_EXACT
        if o is None and b.ref_core:
            o = take(by_core, (b.ref_core, b.amount))
            mtype = MatchType.AUTO_CORE
        if o is None:
            o = _fuzzy_candidate(b, otomax, used)
            mtype = MatchType.AUTO_FUZZY
        if o is None:
            continue
        _persist_match(book_date, channel, b, o, mtype)
        stats.matched += 1
    return stats


def _fuzzy_candidate(bank, otomax, used):
    threshold = settings.MATCH_FUZZY_THRESHOLD
    best, best_score = None, threshold
    for o in otomax:
        if o.id in used or o.amount != bank.amount:
            continue
        score = fuzz.token_sort_ratio(bank.ref_normalized, o.ref_normalized)
        if score >= best_score:
            best, best_score = o, score
    if best:
        used.add(best.id)
        best._fuzzy_score = int(best_score)
    return best


# --- QRIS aggregate --------------------------------------------------------

def _match_qris(book_date: date) -> RunStats:
    stats = RunStats()
    bank_by_reseller: dict[int, list] = defaultdict(list)
    unmapped: list = []
    mmap = {m.merchant_id: m.reseller_id for m in MerchantMap.objects.filter(active=True)}
    for b in _open_bank(book_date, Channel.MERCHANT_BCA):
        rid = mmap.get(b.external_ref)
        if rid:
            bank_by_reseller[rid].append(b)
        else:
            unmapped.append(b)

    otomax_by_reseller: dict[int, list] = defaultdict(list)
    for o in _open_otomax(book_date, Channel.MERCHANT_BCA):
        if o.reseller_id:
            otomax_by_reseller[o.reseller_id].append(o)

    for rid, bank_rows in bank_by_reseller.items():
        o_rows = otomax_by_reseller.get(rid, [])
        if not o_rows:
            continue
        bank_total = sum((b.amount for b in bank_rows), ZERO)
        otomax_total = sum((o.amount for o in o_rows), ZERO)
        Match.objects.create(
            book_date=book_date, channel=Channel.MERCHANT_BCA, match_type=MatchType.AGGREGATE,
            amount_bank=bank_total, amount_otomax=otomax_total,
            note=f"agregat QRIS reseller#{rid}: {len(bank_rows)} merchant vs {len(o_rows)} baris QR BULK",
        )
        for row in (*bank_rows, *o_rows):
            _mark(row)
        stats.matched += 1
        if bank_total != otomax_total:
            _make_discrepancy(
                book_date, Channel.MERCHANT_BCA, DiscrepancyKind.AMOUNT_DIFF,
                amount=bank_total - otomax_total, bank=bank_rows[0], otomax=o_rows[0],
                note=f"selisih agregat QRIS reseller#{rid}",
            )
            stats.discrepancies += 1

    for b in unmapped:
        _make_discrepancy(book_date, Channel.MERCHANT_BCA, DiscrepancyKind.BANK_ONLY,
                          amount=b.amount, bank=b, note=f"merchant_id {b.external_ref} belum di-mapping")
        _mark(b, MatchStatus.IGNORED)
        stats.discrepancies += 1
    return stats


# --- leftovers -----------------------------------------------------------

def _classify_leftovers(book_date: date) -> RunStats:
    stats = RunStats()
    for channel in BANK_CHANNELS:
        for b in _open_bank(book_date, channel).filter(discrepancies__isnull=True):
            _make_discrepancy(book_date, channel, DiscrepancyKind.BANK_ONLY, amount=b.amount, bank=b)
            stats.discrepancies += 1
        for o in _open_otomax(book_date, channel).filter(discrepancies__isnull=True):
            _make_discrepancy(book_date, channel, DiscrepancyKind.OTOMAX_ONLY, amount=-o.amount, otomax=o)
            stats.discrepancies += 1
    return stats


# --- helpers -----------------------------------------------------------

def _mark(obj, status=MatchStatus.MATCHED):
    obj.match_status = status
    obj.save(update_fields=["match_status"])


def _persist_match(book_date, channel, bank, otomax, mtype, note=""):
    Match.objects.create(
        book_date=book_date, channel=channel, bank_mutation=bank, otomax_entry=otomax,
        match_type=mtype, amount_bank=bank.amount, amount_otomax=otomax.amount,
        confidence=getattr(otomax, "_fuzzy_score", None), note=note,
    )
    if mtype != MatchType.AGGREGATE:
        _mark(bank)
        _mark(otomax)


def _next_code(book_date) -> str:
    prefix = f"SLS-{book_date:%Y%m%d}-"
    n = Discrepancy.objects.filter(code__startswith=prefix).count() + 1
    return f"{prefix}{n:03d}"


def _make_discrepancy(book_date, channel, kind, *, amount, bank=None, otomax=None, note=""):
    return Discrepancy.objects.create(
        code=_next_code(book_date), origin_book_date=book_date, channel=channel, kind=kind,
        amount=amount, bank_mutation=bank, otomax_entry=otomax, note=note,
    )
