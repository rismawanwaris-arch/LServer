"""Helper bersama mesin pencocokan: statistik hasil run, status "masih terbuka",
penanda status, dan pembuatan Discrepancy. Tidak bergantung pada submodule lain."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from apps.core.enums import DiscrepancyKind, DiscrepancyStatus, MatchStatus, MatchType, OtomaxCategory

from ..models import Discrepancy, Match

ZERO = Decimal("0.00")

# Status yang dianggap "masih terbuka" untuk keperluan pencocokan/netting/carry-forward.
# PENDING_SETTLE diperlakukan sama seperti UNMATCHED karena keduanya berarti "belum ada
# pasangan aktif" — beda dari MATCHED/MANUAL/IGNORED yang statusnya sudah final.
_OPEN_STATUSES = [MatchStatus.UNMATCHED, MatchStatus.PENDING_SETTLE]

# Kategori OTOMAX yang punya uang masuk nyata di mutasi bank, jadi ikut dicocokkan mesin.
# PAYMENT ("BAYAR KE <bank>") sengaja TIDAK ikut dihitung di KPI total Otomax (yang
# tetap cuma TOPUP_TARTUN) -- ini murni soal kandidat pencocokan.
_MATCHABLE_CATEGORIES = [OtomaxCategory.TOPUP_TARTUN, OtomaxCategory.PAYMENT]


@dataclass
class RunStats:
    matched: int = 0
    discrepancies: int = 0
    netted: int = 0

    def merge(self, other: RunStats):
        self.matched += other.matched
        self.discrepancies += other.discrepancies
        self.netted += other.netted


def _mark(obj, status=MatchStatus.MATCHED):
    obj.match_status = status
    obj.save(update_fields=["match_status"])


def _proposal_rejected(bank) -> bool:
    """Operator pernah membatalkan usulan mesin untuk mutasi ini -- jangan usulkan lagi
    (kalau tidak, tiap "Jalankan Matching Engine" memunculkan usulan yang sama berulang).
    Pencocokan final yang pasti (ref/token persis) tetap boleh."""
    return Match.objects.filter(bank_mutation=bank, needs_review=True, voided_at__isnull=False).exists()


def _persist_match(book_date, channel, bank, otomax, mtype, note="", review_reason=""):
    m = Match.objects.create(
        book_date=book_date,
        channel=channel,
        bank_mutation=bank,
        otomax_entry=otomax,
        match_type=mtype,
        amount_bank=bank.amount,
        amount_otomax=otomax.amount,
        confidence=getattr(otomax, "_fuzzy_score", None),
        note=note,
        needs_review=bool(review_reason),
        review_reason=review_reason,
    )
    if mtype != MatchType.AGGREGATE:
        _mark(bank)
        _mark(otomax)

    # Bersihkan discrepancy leftover (BANK_ONLY / OTOMAX_ONLY) yang sempat dibuat pada
    # run sebelumnya untuk transaksi yang sekarang berhasil dicocokkan otomatis.
    if bank:
        Discrepancy.objects.filter(
            bank_mutation=bank,
            status=DiscrepancyStatus.OPEN,
            kind=DiscrepancyKind.BANK_ONLY,
        ).delete()
    if otomax:
        Discrepancy.objects.filter(
            otomax_entry=otomax,
            status=DiscrepancyStatus.OPEN,
            kind=DiscrepancyKind.OTOMAX_ONLY,
        ).delete()
    return m


def _next_code(book_date) -> str:
    prefix = f"SLS-{book_date:%Y%m%d}-"
    existing_codes = Discrepancy.objects.filter(code__startswith=prefix).values_list("code", flat=True)
    max_num = 0
    for code in existing_codes:
        suffix = code[len(prefix):]
        try:
            val = int(suffix)
            if val > max_num:
                max_num = val
        except ValueError:
            pass
    next_num = max_num + 1
    while Discrepancy.objects.filter(code=f"{prefix}{next_num:03d}").exists():
        next_num += 1
    return f"{prefix}{next_num:03d}"


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
