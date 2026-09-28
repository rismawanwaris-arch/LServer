from datetime import date
from decimal import Decimal

import pytest

from apps.core.enums import Channel, OtomaxCategory
from apps.core.normalize import (
    classify_otomax,
    match_key,
    norm_ref,
    parse_rupiah,
    parse_tgl_date,
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
        # Data nyata: "BAYAR KE <bank>" dulu cuma dikenali untuk BRI -- MANDIRI/BCA jatuh ke
        # OTHER tanpa channel, jadi tidak pernah ikut dicocokkan mesin sama sekali.
        (
            "BAYAR KE MANDIRI MCM InhouseTrf DARI SAHIDIN 99101 TGL 30/AGS/2026",
            OtomaxCategory.PAYMENT,
            Channel.MANDIRI,
        ),
        ("BAYAR KE BCA TRSF E-BANKING CR 2908/FTSCY/WS95031", OtomaxCategory.PAYMENT, Channel.BCA),
        ("BAYAR KE BRI Transfer dari BK CIJAMBE", OtomaxCategory.PAYMENT, Channel.BRI),
        ("BAYAR QR 260903 04 0983811 LIVIN", OtomaxCategory.PAYMENT, Channel.MANDIRI),
        # Tanpa penanda bank: sengaja dibiarkan OTHER (belum jelas bank mana -> manual).
        ("BAYAR QR 260829 04 0166244 TGL 24/AGS/2026", OtomaxCategory.OTHER, None),
    ],
)
def test_classify_otomax(desc, category, hint):
    assert classify_otomax(desc) == (category, hint)


def test_strip_prefix_leaves_bank_ref():
    d = "TARTUN EDC BRI ATMLTRPRM 01884 000001039 21540100059656"
    assert strip_otomax_prefix(d) == "ATMLTRPRM 01884 000001039 21540100059656"


def test_strip_prefix_and_tgl_suffix_leaves_exact_bank_text():
    """Kasus SAHIDIN: setelah prefix "BAYAR KE MANDIRI" & akhiran "TGL 30/AGS/2026" dibuang,
    keterangan Otomax harus PERSIS sama dengan keterangan mutasi bank-nya."""
    otomax = "BAYAR KE MANDIRI MCM InhouseTrf DARI SAHIDIN 99101 TGL 30/AGS/2026"
    bank = "MCM InhouseTrf DARI SAHIDIN 99101"
    assert strip_otomax_prefix(otomax) == norm_ref(bank)


def test_tgl_only_description_is_not_emptied():
    # Keterangan QRIS bulk isinya cuma "TGL ..." -- jangan sampai jadi string kosong.
    assert strip_otomax_prefix("TARTUN QR BULK TGL 05-SEP-2026") == "TGL 05-SEP-2026"


@pytest.mark.parametrize(
    "desc,expected",
    [
        ("BAYAR KE MANDIRI MCM InhouseTrf DARI SAHIDIN 99101 TGL 30/AGS/2026", date(2026, 8, 30)),
        ("TARTUN QR BULK TGL 27-AUG-2026", date(2026, 8, 27)),
        ("BAYAR QR BCA TGL 29-AGU-2026", date(2026, 8, 29)),
        ("TARTUN TF BCA X TGL 02/09/26", date(2026, 9, 2)),
        ("TARTUN TF BCA X TGL 31/02/2026", None),  # tanggal mustahil
        ("TARTUN TF BCA X", None),
    ],
)
def test_parse_tgl_date(desc, expected):
    assert parse_tgl_date(desc) == expected


def test_clean_mandiri_decimal():
    from apps.core.normalize import clean_mandiri_decimal

    assert clean_mandiri_decimal("6500.00.00") == "6500.00"
    assert clean_mandiri_decimal("150000.00") == "150000.00"
    assert clean_mandiri_decimal("2,500,000.00") == "2500000.00"


def test_extract_tokens():
    from apps.core.normalize import extract_tokens

    tokens = extract_tokens("TARTUN TF BRI DANA20260905034895588601ASEPKURNIAWA PLC134")
    assert "DANA20260905034895588601" in tokens
    assert "20260905034895588601" in tokens
    assert "PLC134" in tokens

    bfst_tokens = extract_tokens("TRSF BFST215401000596563 TEGUH BIMA")
    assert "BFST215401000596563" in bfst_tokens
    assert "215401000596563" in bfst_tokens
