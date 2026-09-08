"""Parser export OTOMAX — TSV: <datetime>\\t<reseller>\\t<jumlah>\\t<keterangan>

Baris header 'Tanggal\\tNama Reseller\\tJumlah\\tKeterangan' diabaikan.
book_date diambil dari 'TGL dd-MMM-yyyy' di keterangan kalau ada, jika tidak
dari tanggal entri.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from ._dates import parse_id_datetime
from .base import ParsedOtomaxRow, ParseResult

_TGL = re.compile(r"TGL\s+(\d{1,2})-([A-Z]{3})-(\d{4})", re.IGNORECASE)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "mei": 5, "jun": 6, "jul": 7,
    "aug": 8, "agu": 8, "sep": 9, "oct": 10, "okt": 10, "nov": 11, "dec": 12, "des": 12,
}


def _book_date_from_note(note: str) -> date | None:
    m = _TGL.search(note)
    if not m:
        return None
    month = _MONTHS.get(m.group(2).lower())
    if not month:
        return None
    return date(int(m.group(3)), month, int(m.group(1)))


def parse(text: str) -> ParseResult:
    result = ParseResult()
    counts: dict[date, int] = {}
    for line in text.splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4:
            continue
        dt_raw, reseller, amount_raw, note = parts[0], parts[1], parts[2], "\t".join(parts[3:])
        if dt_raw.strip().lower() in {"tanggal", "date"}:
            continue
        try:
            amount = Decimal(re.sub(r"[^\d-]", "", amount_raw))
        except InvalidOperation:
            result.warnings.append(f"nominal OTOMAX dilewati: {amount_raw!r}")
            continue
        entry_dt = parse_id_datetime(dt_raw)
        result.otomax_rows.append(
            ParsedOtomaxRow(
                reseller_name_raw=reseller.strip(),
                amount=amount,
                description_raw=note.strip(),
                entry_datetime=entry_dt,
            )
        )
        bd = _book_date_from_note(note) or (entry_dt.date() if entry_dt else None)
        if bd:
            counts[bd] = counts.get(bd, 0) + 1
    if counts:
        result.book_date = max(counts, key=counts.get)
    if not result.otomax_rows:
        result.warnings.append("Tidak ada baris OTOMAX terbaca — cek pemisah TAB.")
    return result
