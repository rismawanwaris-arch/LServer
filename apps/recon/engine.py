"""Mesin pencocokan per tanggal buku. Idempoten."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
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

# Toleransi hari (H-min, H+max) per bank
BANK_DATE_TOLERANCE = {
    Channel.BRI: (-1, 1),
    Channel.BCA: (-1, 1),
    Channel.MANDIRI: (-1, 1),
    Channel.MERCHANT_BCA: (-1, 2),
}


@dataclass
class RunStats:
    matched: int = 0
    discrepancies: int = 0
    netted: int = 0

    def merge(self, other: RunStats):
        self.matched += other.matched
        self.discrepancies += other.discrepancies
        self.netted += other.netted


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


# --- per-ref channels (BRI / BCA / MANDIRI) ---------------------------------


def _open_bank(book_date, channel):
    return BankMutation.objects.filter(book_date=book_date, channel=channel, match_status=MatchStatus.UNMATCHED)


def _open_otomax(book_date: date, channel: str, tolerance: tuple[int, int] = (-1, 1)):
    start_d = book_date + timedelta(days=tolerance[0])
    end_d = book_date + timedelta(days=tolerance[1])
    return OtomaxEntry.objects.filter(
        book_date__range=(start_d, end_d),
        category=OtomaxCategory.TOPUP_TARTUN,
        match_status=MatchStatus.UNMATCHED,
    ).filter(models.Q(channel_hint=channel) | models.Q(channel_hint=""))


# --- reversal netting -------------------------------------------------------


def _net_reversals() -> int:
    """Netkan baris REVERSAL dgn TOPUP_TARTUN yang dibatalkannya.

    OTOMAX kadang mencatat topup ke reseller yang salah, membalikkannya lewat baris
    "REV ..." (ref & nominal sama, tanda berlawanan, nama reseller sama persis dengan
    yang dibatalkan), lalu mengentri ulang nominal yang sama ke reseller yang benar.
    Pasangan asli+REV itu bernilai nol dan tidak boleh ikut bersaing memperebutkan satu
    mutasi bank dengan entri revisiannya — jadi dikeluarkan dari kandidat pencocokan
    (match_status=IGNORED) sebelum pencocokan ref/nominal berjalan.
    """
    netted = 0
    reversals = OtomaxEntry.objects.filter(category=OtomaxCategory.REVERSAL, match_status=MatchStatus.UNMATCHED)
    for rev in reversals:
        base = OtomaxEntry.objects.filter(
            category=OtomaxCategory.TOPUP_TARTUN,
            match_status=MatchStatus.UNMATCHED,
            amount=-rev.amount,
            reseller_name_raw=rev.reseller_name_raw,
        )
        if rev.entry_datetime:
            base = base.filter(models.Q(entry_datetime__lte=rev.entry_datetime) | models.Q(entry_datetime__isnull=True))

        # ref_core (token angka inti) lebih tahan terhadap variasi teks — mis. OTOMAX
        # kadang menambah akhiran "TGL dd/bln/yyyy" hanya pada baris REV/revisian,
        # tidak pada entri aslinya, sehingga ref_normalized-nya jadi tidak identik.
        original = None
        if rev.ref_core:
            original = base.filter(ref_core=rev.ref_core).order_by("-entry_datetime").first()
        if original is None and rev.ref_normalized:
            original = base.filter(ref_normalized=rev.ref_normalized).order_by("-entry_datetime").first()
        if original is None:
            continue

        rev.match_status = MatchStatus.IGNORED
        rev.net_pair = original
        rev.note = f"Menetralkan OtomaxEntry #{original.id} ({original.amount})"
        rev.save(update_fields=["match_status", "net_pair", "note"])

        original.match_status = MatchStatus.IGNORED
        original.net_pair = rev
        original.note = f"Dibatalkan oleh REV OtomaxEntry #{rev.id}"
        original.save(update_fields=["match_status", "net_pair", "note"])
        netted += 1
    return netted


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


# --- QRIS aggregate --------------------------------------------------------

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

    # Pass 3: Nama / MerchantMap cocok tapi ada selisih nominal
    for b in list(unmatched_banks):
        grp, score = _find_best_group(b, list(otomax_groups.values()), mmap)
        if grp:
            diff = b.amount - grp["total"]
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


# --- leftovers -----------------------------------------------------------


def _classify_leftovers(book_date: date) -> RunStats:
    stats = RunStats()
    for channel in BANK_CHANNELS:
        for b in _open_bank(book_date, channel).filter(discrepancies__isnull=True):
            _make_discrepancy(book_date, channel, DiscrepancyKind.BANK_ONLY, amount=b.amount, bank=b)
            stats.discrepancies += 1
        for o in OtomaxEntry.objects.filter(
            book_date=book_date, category=OtomaxCategory.TOPUP_TARTUN, match_status=MatchStatus.UNMATCHED
        ).filter(discrepancies__isnull=True):
            _mark(o, MatchStatus.PENDING_SETTLE)
            _make_discrepancy(book_date, channel, DiscrepancyKind.OTOMAX_ONLY, amount=-o.amount, otomax=o)
            stats.discrepancies += 1
    return stats


# --- helpers -----------------------------------------------------------


def _mark(obj, status=MatchStatus.MATCHED):
    obj.match_status = status
    obj.save(update_fields=["match_status"])


def _persist_match(book_date, channel, bank, otomax, mtype, note=""):
    Match.objects.create(
        book_date=book_date,
        channel=channel,
        bank_mutation=bank,
        otomax_entry=otomax,
        match_type=mtype,
        amount_bank=bank.amount,
        amount_otomax=otomax.amount,
        confidence=getattr(otomax, "_fuzzy_score", None),
        note=note,
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
        code=_next_code(book_date),
        origin_book_date=book_date,
        channel=channel,
        kind=kind,
        amount=amount,
        bank_mutation=bank,
        otomax_entry=otomax,
        note=note,
    )
