"""Parser mutasi BRI — Mendukung Excel (.xlsx) & CSV Qlola Cash Statement terbaru
(Transaction Reference/Value Date/Remark/Debit/Credit), CSV Qlola lama
(TGL_TRAN/REMARK_CUSTOM), serta format teks 3 baris."""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import openpyxl

from apps.core.normalize import parse_rupiah

from ._dates import parse_id_datetime
from .base import ParsedBankRow, ParseResult, to_text

_AMOUNT_LINE = re.compile(r"^[+-]?\s*Rp[\d.]", re.IGNORECASE)


def _parse_num(val: object) -> Decimal:
    if val is None:
        return Decimal("0")
    if isinstance(val, Decimal):
        return val
    if isinstance(val, (int, float)):
        return Decimal(str(val))
    s = str(val).strip()
    if not s or s == "-":
        return Decimal("0")
    clean = re.sub(r"[^\d.]", "", s.replace(",", "."))
    if clean.count(".") > 1:
        parts = clean.split(".")
        clean = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return Decimal(clean or "0")
    except InvalidOperation:
        return Decimal("0")


def _parse_cell_datetime(val: object) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=None) if val.tzinfo else val
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day)
    s = str(val).strip()
    if not s or s == "-":
        return None
    return parse_id_datetime(s)


def _append_qlola_row(row_upper: dict[str, object], result: ParseResult) -> None:
    desc = str(row_upper.get("REMARK") or "").strip()
    val_date = row_upper.get("VALUE DATE")
    entry_date = row_upper.get("ENTRY DATE")
    dt = _parse_cell_datetime(val_date) or _parse_cell_datetime(entry_date)

    tx_ref = str(row_upper.get("TRANSACTION REFERENCE") or "").strip()
    ext_ref = tx_ref[:40] if tx_ref and tx_ref != "-" else ""

    debit = _parse_num(row_upper.get("DEBIT"))
    credit = _parse_num(row_upper.get("CREDIT"))

    if not desc and dt is None and credit == 0 and debit == 0:
        return
    if credit == 0 and debit == 0:
        return

    amount = credit if credit > 0 else -debit
    result.bank_rows.append(
        ParsedBankRow(
            description_raw=desc,
            amount=amount,
            txn_datetime=dt,
            external_ref=ext_ref,
        )
    )


def _parse_xlsx(file_bytes: bytes, result: ParseResult) -> bool:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    except Exception as exc:
        result.warnings.append(f"Gagal membaca format xlsx BRI: {exc}")
        return False

    sheets = [wb.active] + [s for s in wb.worksheets if s is not wb.active]
    for sheet in sheets:
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            continue

        header_idx = -1
        is_legacy = False
        for idx, r in enumerate(rows[:60]):
            if not r:
                continue
            cells = [str(c).strip().upper() for c in r if c is not None]
            joined = " | ".join(cells)
            if "REMARK" in joined and ("VALUE DATE" in joined or "ENTRY DATE" in joined) and (
                "DEBIT" in joined or "CREDIT" in joined
            ):
                header_idx = idx
                break
            if "TGL_TRAN" in joined or "REMARK_CUSTOM" in joined or "TRREMK" in joined:
                header_idx = idx
                is_legacy = True
                break

        if header_idx == -1:
            continue

        headers = [str(c).strip().upper() if c is not None else "" for c in rows[header_idx]]
        for r in rows[header_idx + 1 :]:
            if not r:
                continue
            row_upper = {headers[i]: r[i] for i in range(min(len(headers), len(r))) if headers[i]}
            if is_legacy:
                tgl_raw = str(row_upper.get("TGL_TRAN") or row_upper.get("TANGGAL") or "").strip()
                rc = str(row_upper.get("REMARK_CUSTOM") or "").strip()
                tr = str(row_upper.get("TRREMK") or "").strip()
                dt_txt = str(row_upper.get("DESK_TRAN") or "").strip()
                ket = str(row_upper.get("KETERANGAN") or "").strip()
                if rc and tr and rc.upper() != tr.upper():
                    desc = f"{rc} [{tr}]"
                else:
                    desc = rc or tr or dt_txt or ket
                glsign = str(row_upper.get("GLSIGN") or "").upper()
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
                result.bank_rows.append(
                    ParsedBankRow(
                        description_raw=desc,
                        amount=amount,
                        txn_datetime=_parse_cell_datetime(row_upper.get("TGL_TRAN") or row_upper.get("TANGGAL")),
                    )
                )
            else:
                _append_qlola_row(row_upper, result)

        if result.bank_rows:
            return True

    return False


def _parse_qlola_cash_statement_csv(text: str, result: ParseResult) -> bool:
    """Format export Qlola terbaru (CASH STATEMENT): beberapa baris metadata di atas,
    diikuti header tabel `Transaction Reference,Customer Reference,Value Date,Entry Date,Remark,...`."""
    lines = text.splitlines()
    header_idx = -1
    for idx, line in enumerate(lines[:60]):
        up = line.upper()
        if "REMARK" in up and ("VALUE DATE" in up or "ENTRY DATE" in up) and ("DEBIT" in up or "CREDIT" in up):
            header_idx = idx
            break

    if header_idx == -1:
        return False

    header_line = lines[header_idx]
    delimiter = "\t" if "\t" in header_line else (";" if ";" in header_line else ",")
    table_text = "\n".join(lines[header_idx:])

    reader = csv.DictReader(io.StringIO(table_text), delimiter=delimiter, skipinitialspace=True)
    if not reader.fieldnames:
        return False

    for row in reader:
        row_upper: dict[str, object] = {
            k.strip().upper(): (v.strip() if v is not None else "") for k, v in row.items() if k
        }
        _append_qlola_row(row_upper, result)
    return len(result.bank_rows) > 0


def _parse_csv(text: str, result: ParseResult) -> bool:
    if _parse_qlola_cash_statement_csv(text, result):
        return True

    first_few = text[:2000].upper()
    if (
        "TGL_TRAN" not in first_few
        and "REMARK_CUSTOM" not in first_few
        and "TRREMK" not in first_few
        and "GLSIGN" not in first_few
    ):
        return False

    reader = csv.DictReader(io.StringIO(text), delimiter=",", skipinitialspace=True)
    if not reader.fieldnames:
        return False

    for row in reader:
        row_upper = {k.strip().upper(): v.strip() for k, v in row.items() if k and v is not None}
        tgl_raw = row_upper.get("TGL_TRAN") or row_upper.get("TANGGAL") or ""
        rc = row_upper.get("REMARK_CUSTOM", "").strip()
        tr = row_upper.get("TRREMK", "").strip()
        dt = row_upper.get("DESK_TRAN", "").strip()
        ket = row_upper.get("KETERANGAN", "").strip()

        if rc and tr and rc.upper() != tr.upper():
            desc = f"{rc} [{tr}]"
        elif rc:
            desc = rc
        elif tr:
            desc = tr
        elif dt:
            desc = dt
        else:
            desc = ket

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
    result = ParseResult()
    if isinstance(content, bytes) and content[:2] == b"PK":
        if _parse_xlsx(content, result):
            dates = [r.txn_datetime.date() for r in result.bank_rows if r.txn_datetime]
            if dates:
                result.book_date = max(set(dates), key=dates.count)
            return result

    text = to_text(content)
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
