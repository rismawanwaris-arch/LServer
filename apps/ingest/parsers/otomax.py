"""Parser export OTOMAX — Mendukung Excel (.xlsx), CSV, dan TSV sesuai PRD."""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import openpyxl

from ._dates import parse_id_datetime
from .base import ParsedOtomaxRow, ParseResult, to_text

_TGL = re.compile(r"TGL\s+(\d{1,2})-([A-Z]{3})-(\d{4})", re.IGNORECASE)
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "mei": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "agu": 8,
    "sep": 9,
    "oct": 10,
    "okt": 10,
    "nov": 11,
    "dec": 12,
    "des": 12,
}


def _book_date_from_note(note: str) -> date | None:
    m = _TGL.search(note)
    if not m:
        return None
    month = _MONTHS.get(m.group(2).lower())
    if not month:
        return None
    return date(int(m.group(3)), month, int(m.group(1)))


def _parse_xlsx(file_bytes: bytes, result: ParseResult) -> bool:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    except Exception as exc:
        result.warnings.append(f"Gagal membaca format xlsx: {exc}")
        return False

    sheet = wb.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return False

    header_idx = -1
    tgl_col, reseller_col, amt_col, desc_col = 0, 1, 2, 3
    for idx, r in enumerate(rows[:10]):
        if not r:
            continue
        cells = [str(c).strip().upper() for c in r if c is not None]
        has_date = any("TANGGAL" in c or "DATE" in c for c in cells)
        has_detail = any("RESELLER" in c or "JUMLAH" in c or "KETERANGAN" in c for c in cells)
        if has_date and has_detail:
            header_idx = idx
            break

    if header_idx != -1:
        h_row = [str(c).strip().upper() if c is not None else "" for c in rows[header_idx]]
        for c_idx, val in enumerate(h_row):
            if "TANGGAL" in val or "DATE" in val:
                tgl_col = c_idx
            elif "RESELLER" in val or "NAMA" in val:
                reseller_col = c_idx
            elif "JUMLAH" in val or "NOMINAL" in val or "AMOUNT" in val:
                amt_col = c_idx
            elif "KETERANGAN" in val or "NOTE" in val or "DESC" in val:
                desc_col = c_idx
        start_row = header_idx + 1
    else:
        start_row = 0

    counts: dict[date, int] = {}
    for r in rows[start_row:]:
        if not r or len(r) <= max(tgl_col, reseller_col, amt_col):
            continue
        tgl_val = r[tgl_col]
        reseller_val = str(r[reseller_col] or "").strip()
        amt_val = r[amt_col]
        desc_val = str(r[desc_col] if len(r) > desc_col and r[desc_col] is not None else "").strip()

        if not reseller_val and not desc_val and amt_val is None:
            continue

        try:
            if isinstance(amt_val, int | float | Decimal):
                amount = Decimal(str(amt_val))
            else:
                amount = Decimal(re.sub(r"[^\d-]", "", str(amt_val or "0")))
        except (InvalidOperation, ValueError):
            continue

        entry_dt: datetime | None = None
        if isinstance(tgl_val, datetime):
            entry_dt = tgl_val
        elif isinstance(tgl_val, date):
            entry_dt = datetime.combine(tgl_val, datetime.min.time())
        elif tgl_val:
            entry_dt = parse_id_datetime(str(tgl_val))

        result.otomax_rows.append(
            ParsedOtomaxRow(
                reseller_name_raw=reseller_val,
                amount=amount,
                description_raw=desc_val,
                entry_datetime=entry_dt,
            )
        )

        bd = _book_date_from_note(desc_val) or (entry_dt.date() if entry_dt else None)
        if bd:
            counts[bd] = counts.get(bd, 0) + 1

    if counts:
        result.book_date = max(counts, key=counts.get)
    return len(result.otomax_rows) > 0


def parse(data: str | bytes) -> ParseResult:
    result = ParseResult()

    if isinstance(data, bytes) and data.startswith(b"PK"):
        if _parse_xlsx(data, result):
            return result

    text = to_text(data)
    counts: dict[date, int] = {}
    lines = [ln for ln in text.splitlines() if ln.strip()]

    first_line = lines[0] if lines else ""
    delimiter = "\t" if "\t" in first_line else (";" if ";" in first_line else ",")

    reader = csv.reader(lines, delimiter=delimiter)
    for parts in reader:
        if len(parts) < 3:
            continue
        dt_raw = parts[0].strip()
        if dt_raw.lower() in {"tanggal", "date", "waktu"}:
            continue
        reseller = parts[1].strip() if len(parts) > 1 else ""
        amount_raw = parts[2].strip() if len(parts) > 2 else "0"
        note = delimiter.join(parts[3:]).strip() if len(parts) > 3 else ""

        try:
            amount = Decimal(re.sub(r"[^\d-]", "", amount_raw))
        except (InvalidOperation, ValueError):
            result.warnings.append(f"nominal OTOMAX dilewati: {amount_raw!r}")
            continue

        entry_dt = parse_id_datetime(dt_raw)
        result.otomax_rows.append(
            ParsedOtomaxRow(
                reseller_name_raw=reseller,
                amount=amount,
                description_raw=note,
                entry_datetime=entry_dt,
            )
        )
        bd = _book_date_from_note(note) or (entry_dt.date() if entry_dt else None)
        if bd:
            counts[bd] = counts.get(bd, 0) + 1

    if counts:
        result.book_date = max(counts, key=counts.get)
    if not result.otomax_rows:
        result.warnings.append("Tidak ada baris OTOMAX terbaca — periksa file Excel atau pemisah kolom CSV/TSV.")
    return result
