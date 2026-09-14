"""Parser mutasi Mandiri — Mendukung CSV delimiter ';' sesuai PRD & format 6 baris."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from apps.core.normalize import clean_mandiri_decimal

from ._dates import parse_id_datetime
from .base import ParsedBankRow, ParseResult, to_text

_DT = re.compile(r"^\d{2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2}:\d{2}")


def _parse_mandiri_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = raw.strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})\s+(\d{1,2})[.:](\d{1,2})(?:[.:](\d{1,2}))?", s)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        hour = int(m.group(4))
        minute = int(m.group(5))
        second = int(m.group(6) or 0)
        return datetime(year, month, day, hour, minute, second)
    return parse_id_datetime(s)


def _parse_mandiri_num(s: str | None) -> Decimal:
    if not s:
        return Decimal("0")
    cleaned = clean_mandiri_decimal(s)
    try:
        return Decimal(cleaned or "0")
    except InvalidOperation:
        return Decimal("0")


def _parse_csv(text: str, result: ParseResult) -> bool:
    first_few = text[:2000].upper()
    if ";" not in first_few and "DEBIT" not in first_few and "CREDIT" not in first_few:
        return False

    delimiter = ";" if ";" in first_few else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter, skipinitialspace=True)
    if not reader.fieldnames:
        return False

    for row in reader:
        row_upper = {k.strip().upper(): (v.strip() if v else "") for k, v in row.items() if k}
        tgl_raw = (
            row_upper.get("POSTDATE")
            or row_upper.get("POST DATE")
            or row_upper.get("POST_DATE")
            or row_upper.get("DATE")
            or row_upper.get("TANGGAL")
            or row_upper.get("TRANSACTION DATE")
            or ""
        )
        remark = (
            row_upper.get("REMARKS")
            or row_upper.get("REMARK")
            or row_upper.get("DESCRIPTION")
            or row_upper.get("KETERANGAN")
            or ""
        )
        add_desc = (
            row_upper.get("ADDITIONALDESC")
            or row_upper.get("ADDITIONAL DESC")
            or row_upper.get("ADDITIONAL_DESC")
            or ""
        )
        ref_no = (
            row_upper.get("REFERENCE NO.")
            or row_upper.get("REFERENCE NO")
            or row_upper.get("REF NO")
            or row_upper.get("REF NO.")
            or ""
        )
        credit_str = row_upper.get("CREDIT AMOUNT") or row_upper.get("CREDIT") or row_upper.get("KREDIT") or ""
        debit_str = row_upper.get("DEBIT AMOUNT") or row_upper.get("DEBIT") or row_upper.get("DEBET") or ""

        if not tgl_raw and not remark and not credit_str and not debit_str:
            continue

        credit = _parse_mandiri_num(credit_str)
        debit = _parse_mandiri_num(debit_str)
        amount = credit if credit > 0 else -debit

        dt = _parse_mandiri_date(tgl_raw)
        desc_parts = [remark]
        if add_desc and add_desc.strip() != remark.strip():
            desc_parts.append(add_desc)
        if ref_no and ref_no != "-":
            desc_parts.append(ref_no)
        desc = " ".join(desc_parts).strip()

        result.bank_rows.append(
            ParsedBankRow(
                description_raw=desc,
                amount=amount,
                txn_datetime=dt,
                external_ref=ref_no if ref_no != "-" else "",
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

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    i = 0
    while i < len(lines):
        if not _DT.match(lines[i]) or i + 5 >= len(lines):
            i += 1
            continue
        dt = parse_id_datetime(lines[i])
        remark = lines[i + 1]
        reference = lines[i + 2]
        debit = _parse_mandiri_num(lines[i + 3])
        credit = _parse_mandiri_num(lines[i + 4])
        amount = credit if credit > 0 else -debit
        desc = f"{remark} {reference}".replace(" -", "").strip()
        result.bank_rows.append(ParsedBankRow(description_raw=desc, amount=amount, txn_datetime=dt))
        i += 6
    if not result.bank_rows:
        result.warnings.append("Tidak ada transaksi Mandiri terbaca (cek format CSV ';' atau format 6 baris).")
    dates = [r.txn_datetime.date() for r in result.bank_rows if r.txn_datetime]
    if dates:
        result.book_date = max(set(dates), key=dates.count)
    return result
