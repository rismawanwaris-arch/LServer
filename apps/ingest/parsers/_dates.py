from __future__ import annotations

import re
from datetime import datetime

_ID_MONTHS = {
    "januari": 1,
    "februari": 2,
    "maret": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "agustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "desember": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "agu": 8,
    "agt": 8,
    "sep": 9,
    "okt": 10,
    "nov": 11,
    "des": 12,
}

_LONG = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})(?:\s+(\d{2}):(\d{2}):(\d{2}))?")
_SLASH = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def parse_id_datetime(text: str) -> datetime | None:
    """'5 September 2026 21:40:31' atau '05/09/2026' -> datetime (naive)."""
    text = text.strip()
    m = _LONG.search(text)
    if m:
        day, mon_raw, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        month = _ID_MONTHS.get(mon_raw)
        if month:
            hh, mm, ss = (int(x) if x else 0 for x in (m.group(4), m.group(5), m.group(6)))
            return datetime(year, month, day, hh, mm, ss)
    m = _SLASH.search(text)
    if m:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    try:
        return datetime.fromisoformat(text.replace("Z", ""))
    except ValueError:
        return None
