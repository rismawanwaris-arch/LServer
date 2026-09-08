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
