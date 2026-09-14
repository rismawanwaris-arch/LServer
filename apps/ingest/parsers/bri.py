"""Parser mutasi BRI — Mendukung CSV (koma, quoted) sesuai PRD & format 3 baris."""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal, InvalidOperation

from apps.core.normalize import parse_rupiah

from ._dates import parse_id_datetime
from .base import ParsedBankRow, ParseResult, to_text

_AMOUNT_LINE = re.compile(r"^[+-]?\s*Rp[\d.]", re.IGNORECASE)


def _parse_num(val: str | None) -> Decimal:
    if not val:
        return Decimal("0")
    clean = re.sub(r"[^\d.]", "", val.replace(",", "."))
    if clean.count(".") > 1:
        parts = clean.split(".")
        clean = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return Decimal(clean or "0")
    except InvalidOperation:
        return Decimal("0")


def _parse_csv(text: str, result: ParseResult) -> bool:
    first_few = text[:2000].upper()
    if "TGL_TRAN" not in first_few and "REMARK_CUSTOM" not in first_few and "GLSIGN" not in first_few:
        return False

    reader = csv.DictReader(io.StringIO(text), delimiter=",", skipinitialspace=True)
    if not reader.fieldnames:
        return False

    for row in reader:
        row_upper = {k.strip().upper(): v.strip() for k, v in row.items() if k and v is not None}
        tgl_raw = row_upper.get("TGL_TRAN") or row_upper.get("TANGGAL") or ""
        desc = row_upper.get("REMARK_CUSTOM") or row_upper.get("DESK_TRAN") or row_upper.get("KETERANGAN") or ""
        glsign = row_upper.get("GLSIGN", "").upper()
        kredit = _parse_num(row_upper.get("MUTASI_KREDIT"))
        debet = _parse_num(row_upper.get("MUTASI_DEBET"))

        if not desc and not tgl_raw and kredit == 0 and debet == 0:
            continue

        if glsign == "CR" or (kredit > 0 and glsign != "DB"):
            amount = kredit if kredit > 0 else _parse_num(row_upper.get("NOMINAL"))
        elif glsign == "DB" or debet > 0:
            amount = -(debet if debet > 0 else _parse_num(row_upper.get("NOMINAL")))
        else:
            amount = kredit if kredit > 0 else -debet

        dt = parse_id_datetime(tgl_raw) if tgl_raw else None
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

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
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
        result.warnings.append("Tidak ada transaksi terbaca — cek format CSV BRI atau format 3 baris.")
    dates = [r.txn_datetime.date() for r in result.bank_rows if r.txn_datetime]
    if dates:
        result.book_date = max(set(dates), key=dates.count)
    return result
