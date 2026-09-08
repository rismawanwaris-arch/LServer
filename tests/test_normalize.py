from decimal import Decimal

import pytest

from apps.core.enums import Channel, OtomaxCategory
from apps.core.normalize import (
    classify_otomax,
    match_key,
    norm_ref,
    parse_rupiah,
    ref_core,
    strip_otomax_prefix,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+ Rp1.600.000", Decimal("1600000")),
        ("Rp785.158", Decimal("785158")),
        ("- Rp2.500", Decimal("-2500")),
        ("+ Rp53.000", Decimal("53000")),
        ("", None),
        ("Rp999.999.999.999.999", None),  # > 1 triliun -> karantina
    ],
)
def test_parse_rupiah(raw, expected):
    assert parse_rupiah(raw) == expected


def test_norm_ref_collapses_space_and_uppercases():
    assert norm_ref("  bfst215401000596563 teguh  bima ") == "BFST215401000596563 TEGUH BIMA"


def test_ref_core_edc_middle_token():
    assert ref_core("6013013657767948#185693070008#EDC#TRFLA") == "185693070008"


def test_ref_core_survives_leading_digit_drop():
    bank = "5221845082525239#185996340008#EDC#TRFLA"
    otomax = "221845082525239#185996340008#EDC#TRFLA"  # digit depan hilang di OTOMAX
    assert ref_core(bank) == ref_core(otomax) != ""


def test_ref_core_dana():
    assert ref_core("DANA20260905034895588601ASEPKURNIAWA") == "20260905034895588601"


def test_match_key_tuple():
    assert match_key("DANA123456789012ABC") == ("DANA123456789012ABC", "123456789012")


@pytest.mark.parametrize(
    "desc,category,hint",
    [
        ("TARTUN EDC BRI ATMLTRPRM 01884 000001039 21540100059656", OtomaxCategory.TOPUP_TARTUN, Channel.BRI),
        ("TARTUN TF MANDIRI MCM InhouseTrf CS-CS DARI DADAN SONDARI", OtomaxCategory.TOPUP_TARTUN, Channel.MANDIRI),
        ("TARTUN QR BULK TGL 05-SEP-2026", OtomaxCategory.TOPUP_TARTUN, Channel.MERCHANT_BCA),
        ("ADMIN TARTUN TGL 02-SEP-2026", OtomaxCategory.ADMIN, None),
        ("AMBIL STOR. ANDRI TGL 05-SEP-2026", OtomaxCategory.STOR_OUT, None),
        ("STOR ANDRI TGL 05-SEP-2026", OtomaxCategory.STOR_IN, None),
        ("REV ADMIN TARTUN TGL 04-SEP-2026", OtomaxCategory.REVERSAL, None),
    ],
)
def test_classify_otomax(desc, category, hint):
    assert classify_otomax(desc) == (category, hint)


def test_strip_prefix_leaves_bank_ref():
    d = "TARTUN EDC BRI ATMLTRPRM 01884 000001039 21540100059656"
    assert strip_otomax_prefix(d) == "ATMLTRPRM 01884 000001039 21540100059656"
