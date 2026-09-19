"""Pencocokan agregat QRIS (Merchant BCA): satu settlement bank harian vs kumpulan
baris Otomax "Tartun Bulk" milik reseller yang sama. Beda dari ref_match — di sini
satu Match bisa mewakili banyak baris Otomax sekaligus (lihat Match.otomax_entries)."""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models
from rapidfuzz import fuzz

from apps.catalog.models import MerchantMap
from apps.core.enums import Channel, DiscrepancyKind, MatchStatus, MatchType, OtomaxCategory
from apps.ingest.models import BankMutation, OtomaxEntry

from ..models import Match
from .helpers import ZERO, RunStats, _make_discrepancy, _mark
from .ref_match import _open_bank

_ALIASES = {
    "CL": "CILENGKRANG",
    "PD": "PADASUKA",
    "CJM": "CIJAMBE",
    "CUKANG": "CICUKANG",
    "CISA": "CISARANTEN",
    "JH": "BK JH",
    "JH2": "BK JH 2",
    "KALAPA": "BANDAR KUOTA QR",
}


def _clean_outlet_name(s: str | None) -> str:
    s = (s or "").upper()
    s = re.sub(r"([A-Z])(\d+)", r"\1 \2", s)
    s = re.sub(r"(\d+)([A-Z])", r"\1 \2", s)
    s = re.sub(r"\b(PLC|KBB|CELL|QR|PULSA)\b", "", s)
    s = re.sub(r"[^A-Z0-9]", " ", s)
    tokens = s.split()
    expanded = [_ALIASES.get(t, t) for t in tokens]
    return " ".join(expanded).strip()


def _name_similarity(name1: str, name2: str) -> float:
    c1 = _clean_outlet_name(name1)
    c2 = _clean_outlet_name(name2)
    if not c1 or not c2:
        return 0.0
    if c1 == c2:
        return 100.0

    # Branch number check: e.g. ALFA 1 vs ALFA 3, CILENGKRANG 1 vs CILENGKRANG 2
    t1 = c1.split()
    t2 = c2.split()
    if t1 and t2 and t1[0] == t2[0]:
        if len(t1) > 1 and t1[1].isdigit() and len(t2) > 1 and t2[1].isdigit():
            if t1[1] != t2[1]:
                return 0.0

    # Digits check: if both contain digits, ensure they share at least one digit
    nums1 = set(re.findall(r"\d+", c1))
    nums2 = set(re.findall(r"\d+", c2))
    if nums1 and nums2 and not (nums1 & nums2):
        return 0.0

    return float(fuzz.token_set_ratio(c1, c2))


def _find_best_group(b: BankMutation, groups: list[dict], mmap: dict) -> tuple[dict | None, float]:
    best_grp, best_score = None, 0.0
    mapped_rid = mmap.get(b.external_ref)

    for grp in groups:
        if grp["used"]:
            continue
        if mapped_rid and grp.get("reseller_id") == mapped_rid:
            return grp, 100.0
        score = _name_similarity(b.outlet_name or b.description_raw, grp.get("raw_name", ""))
        if score > best_score:
            best_score = score
            best_grp = grp

    if best_score >= 80.0:
        return best_grp, best_score
    return None, 0.0


def _persist_qris_match(
    book_date: date,
    bank: BankMutation,
    otomax_rows: list[OtomaxEntry],
    mtype: str,
    note: str = "",
):
    otomax_total = sum((o.amount for o in otomax_rows), ZERO)
    primary_otomax = otomax_rows[0] if otomax_rows else None
    m = Match.objects.create(
        book_date=book_date,
        channel=Channel.MERCHANT_BCA,
        bank_mutation=bank,
        otomax_entry=primary_otomax,
        match_type=mtype,
        amount_bank=bank.amount,
        amount_otomax=otomax_total,
        confidence=100 if mtype == MatchType.AUTO_EXACT else 90,
        note=note,
    )
    m.otomax_entries.set(otomax_rows)
    _mark(bank, MatchStatus.MATCHED)
    for o in otomax_rows:
        _mark(o, MatchStatus.MATCHED)

    # Auto-learn mapping ke MerchantMap jika reseller sudah ada
    if bank.external_ref and primary_otomax and primary_otomax.reseller:
        MerchantMap.objects.get_or_create(
            merchant_id=bank.external_ref,
            defaults={
                "reseller": primary_otomax.reseller,
                "merchant_name": bank.outlet_name or bank.description_raw,
            },
        )
    return m


def _match_qris(book_date: date) -> RunStats:
    stats = RunStats()
    bank_rows = list(_open_bank(book_date, Channel.MERCHANT_BCA))
    if not bank_rows:
        return stats

    start_d = book_date + timedelta(days=-1)
    end_d = book_date + timedelta(days=2)
    d_str = book_date.strftime("%d-%b-%Y").upper()
    d_str_short = book_date.strftime("%d-%b").upper()

    oto_candidates = list(
        OtomaxEntry.objects.filter(match_status=MatchStatus.UNMATCHED)
        .exclude(category=OtomaxCategory.ADMIN)
        .filter(
            models.Q(channel_hint=Channel.MERCHANT_BCA)
            | models.Q(description_raw__icontains="BULK")
            | models.Q(description_raw__icontains="TARTUN QR")
        )
        .filter(
            models.Q(book_date__range=(start_d, end_d))
            | models.Q(description_raw__icontains=d_str)
            | models.Q(description_raw__icontains=d_str_short)
        )
    )

    mmap = {m.merchant_id: m.reseller_id for m in MerchantMap.objects.filter(active=True)}

    otomax_groups: dict[str, dict] = {}
    for o in oto_candidates:
        key = f"id:{o.reseller_id}" if o.reseller_id else f"name:{_clean_outlet_name(o.reseller_name_raw) or o.id}"
        if key not in otomax_groups:
            otomax_groups[key] = {
                "reseller_id": o.reseller_id,
                "raw_name": o.reseller_name_raw,
                "rows": [],
                "total": ZERO,
                "used": False,
            }
        otomax_groups[key]["rows"].append(o)
        otomax_groups[key]["total"] += o.amount

    unmatched_banks = list(bank_rows)

    # Pass 1: Nama / MerchantMap cocok DAN Nominal persis
    for b in list(unmatched_banks):
        grp, score = _find_best_group(b, list(otomax_groups.values()), mmap)
        if grp and b.amount == grp["total"]:
            mtype = MatchType.AUTO_EXACT if len(grp["rows"]) == 1 else MatchType.AGGREGATE
            b_name = b.outlet_name or b.external_ref
            _persist_qris_match(
                book_date,
                b,
                grp["rows"],
                mtype,
                note=f"QRIS {b_name} cocok dengan {grp['raw_name']}: nominal persis Rp {b.amount:,.2f}",
            )
            stats.matched += 1
            grp["used"] = True
            unmatched_banks.remove(b)

    # Pass 2: Cocokan nominal persis jika unik di antara sisa grup
    for b in list(unmatched_banks):
        matching_grps = [grp for grp in otomax_groups.values() if not grp["used"] and b.amount == grp["total"]]
        if len(matching_grps) == 1:
            grp = matching_grps[0]
            mtype = MatchType.AUTO_EXACT if len(grp["rows"]) == 1 else MatchType.AGGREGATE
            b_name = b.outlet_name or b.external_ref
            _persist_qris_match(
                book_date,
                b,
                grp["rows"],
                mtype,
                note=f"QRIS {b_name}: cocok nominal persis Rp {b.amount:,.2f} dengan {grp['raw_name']}",
            )
            stats.matched += 1
            grp["used"] = True
            unmatched_banks.remove(b)

    # Pass 3: Nama / MerchantMap cocok, selisih nominal masih dalam toleransi
    # (di luar toleransi -> jangan di-match otomatis, biarkan kedua sisi muncul
    # terpisah di antrean belum cocok supaya ditinjau & dipasangkan manual).
    amount_tolerance = Decimal(str(settings.MATCH_AMOUNT_TOLERANCE))
    for b in list(unmatched_banks):
        grp, score = _find_best_group(b, list(otomax_groups.values()), mmap)
        if grp:
            diff = b.amount - grp["total"]
            if abs(diff) > amount_tolerance:
                continue
            b_name = b.outlet_name or b.external_ref
            note_str = f"QRIS {b_name} selisih nominal: Bank {b.amount:,.2f} vs Otomax {grp['total']:,.2f}"
            _persist_qris_match(
                book_date,
                b,
                grp["rows"],
                MatchType.AGGREGATE,
                note=note_str,
            )
            _make_discrepancy(
                book_date,
                Channel.MERCHANT_BCA,
                DiscrepancyKind.AMOUNT_DIFF,
                amount=diff,
                bank=b,
                otomax=grp["rows"][0],
                note=note_str,
            )
            stats.matched += 1
            stats.discrepancies += 1
            grp["used"] = True
            unmatched_banks.remove(b)

    # Pass 4: Bank yang belum cocok tetap UNMATCHED (bukan IGNORED) + catat diskrepansi BANK_ONLY
    for b in unmatched_banks:
        _make_discrepancy(
            book_date,
            Channel.MERCHANT_BCA,
            DiscrepancyKind.BANK_ONLY,
            amount=b.amount,
            bank=b,
            note=f"QRIS {b.outlet_name or b.external_ref} belum cocok dengan Otomax Tartun Bulk",
        )
        stats.discrepancies += 1

    # Otomax tartun bulk yang belum cocok untuk book_date ini -> PENDING_SETTLE
    for grp in otomax_groups.values():
        if not grp["used"]:
            for o in grp["rows"]:
                if o.book_date == book_date or d_str in (o.description_raw or "").upper():
                    _mark(o, MatchStatus.PENDING_SETTLE)
                    _make_discrepancy(
                        book_date,
                        Channel.MERCHANT_BCA,
                        DiscrepancyKind.OTOMAX_ONLY,
                        amount=-o.amount,
                        otomax=o,
                        note=f"Tartun Bulk {o.reseller_name_raw} belum ada di Merchant BCA",
                    )
                    stats.discrepancies += 1

    return stats
