"""Parser rekap QRIS Merchant BCA — TSV agregat harian per merchant:

    Merchant Name\\tMerchant ID\\tTotal Frequency\\tTotal Amount
    ALFA 1 CELL\\t004767950\\t25\\tRp4,374,000

Baris 'Total' di akhir diabaikan. book_date harus diisi manual saat upload.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .base import ParsedBankRow, ParseResult


def parse(text: str) -> ParseResult:
    result = ParseResult()
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) < 4:
            continue
        name, mid, freq_raw, amount_raw = parts[0], parts[1], parts[2], parts[3]
        if name.lower() in {"merchant name", "total"} or not re.fullmatch(r"\d{6,}", mid):
            continue
        try:
            amount = Decimal(re.sub(r"[^\d]", "", amount_raw))
            freq = int(re.sub(r"[^\d]", "", freq_raw) or 0)
        except (InvalidOperation, ValueError):
            result.warnings.append(f"baris merchant dilewati: {line!r}")
            continue
        result.bank_rows.append(
            ParsedBankRow(
                description_raw=f"QRIS {name} {mid}",
                amount=amount,
                external_ref=mid,
                frequency=freq,
            )
        )
    if not result.bank_rows:
        result.warnings.append("Tidak ada merchant terbaca — cek pemisah TAB.")
    return result
