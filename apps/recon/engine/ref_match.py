"""Pencocokan per-ref untuk channel BRI/BCA/Mandiri: exact ref -> token inti -> fuzzy,
lalu fallback nominal khusus Tartun PLC vs Auto Deposit."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models
from rapidfuzz import fuzz

from apps.core.enums import Channel, MatchStatus, MatchType, OtomaxCategory
from apps.ingest.models import BankMutation, OtomaxEntry

from .helpers import _OPEN_STATUSES, RunStats, _persist_match

# Toleransi hari (H-min, H+max) per bank
BANK_DATE_TOLERANCE = {
    Channel.BRI: (-1, 1),
    Channel.BCA: (-1, 1),
    Channel.MANDIRI: (-1, 1),
    Channel.MERCHANT_BCA: (-1, 2),
}


def _open_bank(book_date, channel):
    return BankMutation.objects.filter(book_date=book_date, channel=channel, match_status=MatchStatus.UNMATCHED)


def _open_otomax(book_date: date, channel: str, tolerance: tuple[int, int] = (-1, 1)):
    start_d = book_date + timedelta(days=tolerance[0])
    end_d = book_date + timedelta(days=tolerance[1])
    return OtomaxEntry.objects.filter(
        book_date__range=(start_d, end_d),
        category=OtomaxCategory.TOPUP_TARTUN,
        match_status__in=_OPEN_STATUSES,
    ).filter(models.Q(channel_hint=channel) | models.Q(channel_hint=""))


def _match_ref(book_date: date, channel: str) -> RunStats:
    stats = RunStats()
    tolerance = BANK_DATE_TOLERANCE.get(channel, (-1, 1))
    otomax = list(_open_otomax(book_date, channel, tolerance))

    by_norm: dict[tuple[str, Decimal], list] = defaultdict(list)
    by_core: dict[tuple[str, Decimal], list] = defaultdict(list)
    by_token: dict[tuple[str, Decimal], list] = defaultdict(list)

    for o in otomax:
        by_norm[(o.ref_normalized, o.amount)].append(o)
        if o.ref_core:
            by_core[(o.ref_core, o.amount)].append(o)
        for tk in o.extracted_tokens or []:
            if tk:
                by_token[(tk, o.amount)].append(o)

    used: set[int] = set()

    def take(bucket, key):
        for cand in bucket.get(key, []):
            if cand.id not in used:
                used.add(cand.id)
                return cand
        return None

    for b in _open_bank(book_date, channel):
        # 1. Exact norm match
        o = take(by_norm, (b.ref_normalized, b.amount))
        mtype = MatchType.AUTO_EXACT

        # 1b. Subparts match jika keterangan menggabungkan beberapa sumber (misal REMARK_CUSTOM [TRREMK])
        if o is None and "[" in b.ref_normalized and "]" in b.ref_normalized:
            parts = b.ref_normalized.split("[", 1)
            p1 = parts[0].strip()
            p2 = parts[1].split("]", 1)[0].strip()
            if p1:
                o = take(by_norm, (p1, b.amount))
            if o is None and p2:
                o = take(by_norm, (p2, b.amount))
            if o is not None:
                mtype = MatchType.AUTO_EXACT

        # 2. Token match (Priority 1)
        if o is None:
            # Check ref_core first
            if b.ref_core:
                o = take(by_core, (b.ref_core, b.amount))
                if o is None:
                    o = take(by_token, (b.ref_core, b.amount))
            # Check all extracted tokens
            if o is None and b.extracted_tokens:
                for tk in b.extracted_tokens:
                    o = take(by_token, (tk, b.amount))
                    if o:
                        break
            if o is not None:
                mtype = MatchType.AUTO_CORE

        # 3. Fuzzy match fallback (Priority 2)
        if o is None:
            o = _fuzzy_candidate(b, otomax, used)
            mtype = MatchType.AUTO_FUZZY

        # 4. Tartun PLC vs Auto Deposit (Pencocokan Nominal Sesuai Instruksi)
        match_note = ""
        if o is None and _is_tartun_plc(b):
            o = _match_auto_deposit_candidate(b, otomax, used)
            if o is not None:
                mtype = MatchType.AUTO_EXACT
                match_note = f"Tartun PLC cocok nominal dengan Auto Deposit ({o.reseller_name_raw})"
                o._fuzzy_score = 100

        if o is None:
            continue

        _persist_match(book_date, channel, b, o, mtype, note=match_note)
        stats.matched += 1
    return stats


def _is_tartun_plc(b: BankMutation) -> bool:
    raw = (b.description_raw or "").upper()
    if "TARTUN" in raw and "PLC" in raw:
        return True
    if re.search(r"\bPLC\d+\b", raw):
        return True
    if any("PLC" in (tk or "").upper() for tk in (b.extracted_tokens or [])):
        return True
    return False


def _match_auto_deposit_candidate(bank: BankMutation, otomax: list[OtomaxEntry], used: set[int]) -> OtomaxEntry | None:
    cands = [
        o
        for o in otomax
        if o.id not in used
        and o.amount == bank.amount
        and ("AUTO DEPOSIT" in (o.description_raw or "").upper() or "DEPOSIT" in (o.description_raw or "").upper())
    ]
    if not cands:
        return None

    if bank.txn_datetime:

        def time_diff(o: OtomaxEntry):
            if not o.entry_datetime:
                return timedelta(days=999)
            return abs(o.entry_datetime - bank.txn_datetime)

        cands.sort(key=time_diff)

    chosen = cands[0]
    used.add(chosen.id)
    return chosen


def _fuzzy_candidate(bank, otomax, used):
    threshold = settings.MATCH_FUZZY_THRESHOLD
    best, best_score = None, threshold
    bank_norms = [bank.ref_normalized]
    if "[" in bank.ref_normalized and "]" in bank.ref_normalized:
        parts = bank.ref_normalized.split("[", 1)
        p1 = parts[0].strip()
        p2 = parts[1].split("]", 1)[0].strip()
        if p1:
            bank_norms.append(p1)
        if p2:
            bank_norms.append(p2)

    for o in otomax:
        if o.id in used or o.amount != bank.amount:
            continue
        for bn in bank_norms:
            score = fuzz.token_sort_ratio(bn, o.ref_normalized)
            if score >= best_score:
                best, best_score = o, score
    if best:
        used.add(best.id)
        best._fuzzy_score = int(best_score)
    return best
