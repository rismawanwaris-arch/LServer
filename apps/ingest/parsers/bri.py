"""Parser mutasi BRI — format 3 baris per transaksi:

    <keterangan>
    + Rp1.600.000
    5 September 2026 21:40:31

Verifikasi terhadap file export asli sebelum produksi.
"""

from __future__ import annotations

import re
from decimal import Decimal

from apps.core.normalize import parse_rupiah

from ._dates import parse_id_datetime
from .base import ParsedBankRow, ParseResult

_AMOUNT_LINE = re.compile(r"^[+-]?\s*Rp[\d.]", re.IGNORECASE)


def parse(text: str) -> ParseResult:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    result = ParseResult()
    i = 0
    while i < len(lines):
        desc = lines[i]
        if i + 1 >= len(lines) or not _AMOUNT_LINE.match(lines[i + 1]):
            i += 1
            continue
        raw_amount = lines[i + 1]
        dt = parse_id_datetime(lines[i + 2]) if i + 2 < len(lines) else None
        amount = parse_rupiah(raw_amount)
        review = "" if amount is not None else f"nominal dikarantina: {raw_amount!r}"
        result.bank_rows.append(
            ParsedBankRow(
                description_raw=desc,
                amount=amount if amount is not None else Decimal("0"),
                txn_datetime=dt,
                review_flag=review,
            )
        )
        i += 3
    if not result.bank_rows:
        result.warnings.append("Tidak ada transaksi terbaca — cek format BRI (3 baris/transaksi).")
    dates = [r.txn_datetime.date() for r in result.bank_rows if r.txn_datetime]
    if dates:
        result.book_date = max(set(dates), key=dates.count)
    return result
