"""Parser mutasi Mandiri (Date / Remark / Reference No. / Debit / Credit / Balance).

Satu transaksi = 6 baris: datetime, remark, reference (atau '-'), debit, credit, balance.
Verifikasi terhadap file export asli.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from ._dates import parse_id_datetime
from .base import ParsedBankRow, ParseResult

_DT = re.compile(r"^\d{2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2}:\d{2}")


def _num(s: str) -> Decimal | None:
    try:
        return Decimal(re.sub(r"[^\d.]", "", s) or "0")
    except InvalidOperation:
        return None


def parse(text: str) -> ParseResult:
    result = ParseResult()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    i = 0
    while i < len(lines):
        if not _DT.match(lines[i]) or i + 5 >= len(lines):
            i += 1
            continue
        dt = parse_id_datetime(lines[i])
        remark = lines[i + 1]
        reference = lines[i + 2]
        debit = _num(lines[i + 3]) or Decimal("0")
        credit = _num(lines[i + 4]) or Decimal("0")
        amount = credit if credit > 0 else -debit
        desc = f"{remark} {reference}".replace(" -", "").strip()
        result.bank_rows.append(
            ParsedBankRow(description_raw=desc, amount=amount, txn_datetime=dt)
        )
        i += 6
    if not result.bank_rows:
        result.warnings.append("Tidak ada transaksi Mandiri terbaca (harap 6 baris/transaksi).")
    dates = [r.txn_datetime.date() for r in result.bank_rows if r.txn_datetime]
    if dates:
        result.book_date = max(set(dates), key=dates.count)
    return result
