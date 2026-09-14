"""Parser mutasi BCA — Mendukung CSV (skip 4 header baris info rekening) & format teks."""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal, InvalidOperation

from ._dates import parse_id_datetime
from .base import ParsedBankRow, ParseResult, to_text

_DATE = re.compile(r"^\s*(\d{2}/\d{2}/\d{4})")
_AMOUNT_DIR = re.compile(r"([\d,.]+)\s*(CR|DB)\b", re.IGNORECASE)


def _parse_bca_amount(raw: str | None) -> Decimal | None:
    if not raw:
        return None
    m = _AMOUNT_DIR.search(raw)
    if not m:
        clean = re.sub(r"[^\d.]", "", raw.replace(",", ""))
        try:
            return Decimal(clean or "0")
        except InvalidOperation:
            return None
    num_str = m.group(1).replace(",", "")
    sign = m.group(2).upper()
    try:
        val = Decimal(num_str)
        return -val if sign == "DB" else val
    except InvalidOperation:
        return None


def _parse_csv(text: str, result: ParseResult) -> bool:
    lines = text.splitlines()
    header_idx = -1
    for idx, line in enumerate(lines[:15]):
        line_up = line.upper()
        has_tgl = "TANGGAL" in line_up
        has_detail = "KETERANGAN" in line_up or "JUMLAH" in line_up
        if "TANGGAL TRANSAKSI" in line_up or (has_tgl and has_detail):
            header_idx = idx
            break

    if header_idx == -1:
        return False

    table_text = "\n".join(lines[header_idx:])
    first_row = lines[header_idx]
    delimiter = "\t" if "\t" in first_row else (";" if ";" in first_row else ",")

    reader = csv.DictReader(io.StringIO(table_text), delimiter=delimiter, skipinitialspace=True)
    if not reader.fieldnames:
        return False

    for row in reader:
        row_upper = {k.strip().upper(): (v.strip() if v else "") for k, v in row.items() if k}
        tgl_raw = row_upper.get("TANGGAL TRANSAKSI") or row_upper.get("TANGGAL") or ""
        desc = row_upper.get("KETERANGAN") or row_upper.get("URAIAN") or ""
        amt_str = row_upper.get("JUMLAH") or row_upper.get("MUTASI") or ""

        if not tgl_raw or "SALDO AWAL" in desc.upper() or "SALDO AKHIR" in desc.upper():
            continue

        amount = _parse_bca_amount(amt_str)
        if amount is None:
            continue

        dt = parse_id_datetime(tgl_raw)
        result.bank_rows.append(
            ParsedBankRow(
                description_raw=desc,
                amount=amount,
                txn_datetime=dt,
            )
        )
    return len(result.bank_rows) > 0


def parse(content: str | bytes) -> ParseResult:
    text = to_text(content)
    result = ParseResult()
    if _parse_csv(text, result):
        dates = [r.txn_datetime.date() for r in result.bank_rows if r.txn_datetime]
        if dates:
            result.book_date = max(set(dates), key=dates.count)
        return result

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
        if m.group(2).upper() == "DB":
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
            buf, cur_date = [ln[m.end() :].strip()], m.group(1)
        else:
            buf.append(ln.strip())
        if _AMOUNT_DIR.search(ln):
            flush(buf, cur_date)
            buf = []
    if buf:
        flush(buf, cur_date)

    if not result.bank_rows:
        result.warnings.append("Tidak ada transaksi BCA terbaca — periksa format CSV atau teks BCA.")
    dates = [r.txn_datetime.date() for r in result.bank_rows if r.txn_datetime]
    if dates:
        result.book_date = max(set(dates), key=dates.count)
    return result
