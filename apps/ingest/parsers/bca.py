"""Parser mutasi BCA (Tgl / Keterangan / Cabang / Jumlah / Saldo).

Satu transaksi = blok baris yang diawali tanggal dd/mm/yyyy dan diakhiri baris
berisi '<nominal> CR' atau '<nominal> DB'. Baris di antaranya = keterangan.
Format export bervariasi — verifikasi terhadap file asli.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from ._dates import parse_id_datetime
from .base import ParsedBankRow, ParseResult

_DATE = re.compile(r"^\s*(\d{2}/\d{2}/\d{4})")
_AMOUNT_DIR = re.compile(r"([\d,]+\.\d{2})\s*(CR|DB)\b")


def parse(text: str) -> ParseResult:
    result = ParseResult()
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    buf: list[str] = []
    cur_date = None

    def flush(block: list[str], bdate):
        joined = " ".join(block)
        m = _AMOUNT_DIR.search(joined)
        if not m:
            return
        try:
            amount = Decimal(m.group(1).replace(",", ""))
        except InvalidOperation:
            result.warnings.append(f"nominal BCA dilewati: {joined[:60]!r}")
            return
        if m.group(2) == "DB":
            amount = -amount
        desc = _AMOUNT_DIR.sub("", joined)
        desc = re.sub(r"\s+0000\s+", " ", desc)
        desc = re.sub(r"[\d,]+\.\d{2}\s*$", "", desc).strip()
        result.bank_rows.append(
            ParsedBankRow(
                description_raw=desc,
                amount=amount,
                txn_datetime=parse_id_datetime(bdate) if bdate else None,
            )
        )

    for ln in lines:
        m = _DATE.match(ln)
        if m:
            if buf:
                flush(buf, cur_date)
            buf, cur_date = [ln[m.end():].strip()], m.group(1)
        else:
            buf.append(ln.strip())
        if _AMOUNT_DIR.search(ln):
            flush(buf, cur_date)
            buf = []
    if buf:
        flush(buf, cur_date)

    if not result.bank_rows:
        result.warnings.append("Tidak ada transaksi BCA terbaca.")
    dates = [r.txn_datetime.date() for r in result.bank_rows if r.txn_datetime]
    if dates:
        result.book_date = max(set(dates), key=dates.count)
    return result
