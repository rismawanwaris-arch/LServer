from apps.core.enums import Channel

from . import bca, bri, mandiri, merchant_bca, otomax
from .base import ParsedBankRow, ParsedOtomaxRow, ParserError, ParseResult

_BANK = {
    Channel.BRI: bri.parse,
    Channel.BCA: bca.parse,
    Channel.MERCHANT_BCA: merchant_bca.parse,
    Channel.MANDIRI: mandiri.parse,
}


def parse_file(channel: str, text: str) -> ParseResult:
    if channel == Channel.OTOMAX:
        return otomax.parse(text)
    try:
        return _BANK[channel](text)
    except KeyError as exc:
        raise ParserError(f"channel tidak dikenal: {channel}") from exc


__all__ = [
    "parse_file",
    "ParseResult",
    "ParsedBankRow",
    "ParsedOtomaxRow",
    "ParserError",
]
