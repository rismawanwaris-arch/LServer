from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


@dataclass
class ParsedBankRow:
    description_raw: str
    amount: Decimal  # bertanda; positif = kredit, negatif = debit
    txn_datetime: datetime | None = None
    external_ref: str = ""
    outlet_name: str = ""
    frequency: int | None = None
    review_flag: str = ""


@dataclass
class ParsedOtomaxRow:
    reseller_name_raw: str
    amount: Decimal  # bertanda
    description_raw: str
    entry_datetime: datetime | None = None


@dataclass
class ParseResult:
    book_date: date | None = None
    bank_rows: list[ParsedBankRow] = field(default_factory=list)
    otomax_rows: list[ParsedOtomaxRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class ParserError(ValueError):
    pass


def to_text(content: str | bytes) -> str:
    """Konversi bytes ke str dengan aman mendukung berbagai encoding CSV bank."""
    if isinstance(content, str):
        return content
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")
