"""Fungsi murni untuk normalisasi & klasifikasi. Tanpa Django, tanpa DB — diuji unit.

Semua pencocokan bertumpu pada dua kunci deterministik dari satu string keterangan:
  - norm_ref(s): uppercase + rapikan spasi. Kunci utama.
  - ref_core(s): token angka paling stabil (RRN/ID inti). Menangkap kasus
    "digit depan hilang" dan beda format prefix antara sisi bank vs OTOMAX.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .enums import Channel, OtomaxCategory

# --- Nominal -------------------------------------------------------------------

_MAX_PLAUSIBLE = Decimal("1000000000000")  # 1 triliun; di atas ini = karantina


def parse_rupiah(raw: str | None) -> Decimal | None:
    """'+ Rp1.600.000' -> Decimal('1600000'); '- Rp2.500' -> Decimal('-2500').

    Kembalikan None kalau tak ada digit atau nilainya mustahil (dikarantina).
    """
    if raw is None:
        return None
    negative = "-" in raw or "DB" in raw.upper()
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        return None
    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None
    if value >= _MAX_PLAUSIBLE:
        return None
    return -value if negative else value


def clean_mandiri_decimal(raw: str | None) -> str:
    """Bersihkan anomali angka Mandiri seperti '6500.00.00' -> '6500.00'."""
    if not raw:
        return "0"
    s = raw.strip().replace(",", "")
    # Jika ada pola dobel desimal seperti 6500.00.00, ambil satu desimal saja
    s = re.sub(r"(\.\d{2})\.\d{2}$", r"\1", s)
    return s


# --- Referensi ----------------------------------------------------------------

_WS = re.compile(r"\s+")
_EDC_CORE = re.compile(r"#(\d{6,})#")
_DANA = re.compile(r"DANA(\d{12,})")
_ATM = re.compile(r"ATM[LS]TRPRM\s+([0-9A-Z]+)\s+(\d+)")
_BIFAST = re.compile(r"BFST(\d{10,})")
_WBNK = re.compile(r"WBNK(\d{10,})")
_GOPAY = re.compile(r"GOPAY BANK TRANSFER ID([0-9A-Z]+)")
_BRILINK = re.compile(r"TRANSFER DARI (\d{10,}) VIA BRILINK")
_OVERBOOK = re.compile(r"(\d{6,}_OB_\d{6,})")
_PLC = re.compile(r"\b(PLC\w+)\b")
_DIGITS_LONG = re.compile(r"\b\d{10,}\b")

_OTOMAX_PREFIXES = (
    "REV TARTUN EDC BRI ",
    "REV TARTUN EDC BCA ",
    "REV TARTUN TF BRI ",
    "TARTUN EDC BRI ",
    "TARTUN TF BRI ",
    "TARTUN EDC BCA ",
    "TARTUN TF BCA ",
    "TARTUN TF MANDIRI ",
    "TARTUN QR BULK ",
    "TARTUN QR ",
    "BAYAR KE BRI ",
    "TIKET DEPOSIT BRI ",
)


def norm_ref(s: str | None) -> str:
    return _WS.sub(" ", (s or "").upper()).strip()


def strip_otomax_prefix(s: str | None) -> str:
    """Buang prefix keterangan OTOMAX supaya menyisakan ref bank mentah."""
    t = norm_ref(s)
    for p in _OTOMAX_PREFIXES:
        if t.startswith(p):
            return t[len(p) :].strip()
    return t


def ref_core(s: str | None) -> str:
    """Token angka paling stabil dari sebuah keterangan. '' kalau tak ketemu."""
    t = norm_ref(s)
    for pattern in (_EDC_CORE, _DANA, _BIFAST, _WBNK, _GOPAY, _BRILINK, _OVERBOOK):
        m = pattern.search(t)
        if m:
            return m.group(1)
    m = _ATM.search(t)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = _PLC.search(t)
    if m:
        return m.group(1)
    return ""


def extract_tokens(s: str | None) -> list[str]:
    """Ekstrak semua token referensi dari keterangan untuk pencocokan deterministik."""
    if not s:
        return []
    text = norm_ref(s)
    tokens: list[str] = []

    def add(t: str):
        cleaned = re.sub(r"^[#\s]+|[#\s]+$", "", t).strip()
        if cleaned and cleaned not in tokens:
            tokens.append(cleaned)

    # 1. Pola token spesifik
    for m in re.finditer(r"DANA\d{10,}", text):
        add(m.group(0))
        add(m.group(0)[4:])
    for m in re.finditer(r"BFST\d{10,}", text):
        add(m.group(0))
        add(m.group(0)[4:])
    for m in re.finditer(r"WBNK\d{10,}", text):
        add(m.group(0))
        add(m.group(0)[4:])
    for m in _ATM.finditer(text):
        add(f"{m.group(1)}-{m.group(2)}")
        add(m.group(2))
    for m in _PLC.finditer(text):
        add(m.group(0))
    for m in _EDC_CORE.finditer(text):
        add(m.group(1))
    for m in _BRILINK.finditer(text):
        add(m.group(1))
    for m in _OVERBOOK.finditer(text):
        add(m.group(0))
    for m in _GOPAY.finditer(text):
        add(m.group(1))

    # 2. Sequence angka panjang (>= 10 digit) yang belum tertangkap
    for m in _DIGITS_LONG.finditer(text):
        add(m.group(0))

    # 3. Masukkan juga ref_core jika ada
    rc = ref_core(text)
    if rc:
        add(rc)

    return tokens


def match_key(s: str | None) -> tuple[str, str]:
    """(norm_ref, ref_core) untuk satu keterangan bank mentah."""
    return norm_ref(s), ref_core(s)


# --- Klasifikasi baris OTOMAX ------------------------------------------------

_TARTUN_CHANNEL = [
    (re.compile(r"\bTARTUN (?:EDC|TF) BRI\b"), Channel.BRI),
    (re.compile(r"\bTARTUN (?:EDC|TF) BCA\b"), Channel.BCA),
    (re.compile(r"\bTARTUN TF MANDIRI\b"), Channel.MANDIRI),
    (re.compile(r"\bTARTUN QR\b"), Channel.MERCHANT_BCA),
    (re.compile(r"\bBAYAR KE BRI\b"), Channel.BRI),
    (re.compile(r"\bTIKET DEPOSIT BRI\b"), Channel.BRI),
    (re.compile(r"\b(?:AUTO|TIKET)?\s*DEPOSIT BCA\b"), Channel.BCA),
    (re.compile(r"\b(?:AUTO|TIKET)?\s*DEPOSIT BRI\b"), Channel.BRI),
    (re.compile(r"\b(?:AUTO|TIKET)?\s*DEPOSIT MANDIRI\b"), Channel.MANDIRI),
    (re.compile(r"\bDEPOSIT -? ?BCA\b"), Channel.BCA),
    (re.compile(r"\bDEPOSIT -? ?BRI\b"), Channel.BRI),
    (re.compile(r"\bDEPOSIT -? ?MANDIRI\b"), Channel.MANDIRI),
]


def classify_otomax(description: str | None) -> tuple[str, str | None]:
    """-> (OtomaxCategory, channel_hint|None) dari kolom Keterangan OTOMAX."""
    t = norm_ref(description)
    if t.startswith("REV "):
        return OtomaxCategory.REVERSAL, _channel_hint(t)
    if t.startswith("ADMIN "):
        return OtomaxCategory.ADMIN, None
    if t.startswith("AMBIL STOR"):
        return OtomaxCategory.STOR_OUT, None
    if t.startswith("STOR "):
        return OtomaxCategory.STOR_IN, None
    if t.startswith("AUTO DEPOSIT") or t.startswith("TIKET DEPOSIT") or t.startswith("DEPOSIT"):
        return OtomaxCategory.TOPUP_TARTUN, _channel_hint(t)
    if t.startswith("BAYAR KE BRI"):
        return OtomaxCategory.PAYMENT, Channel.BRI
    if t.startswith("TARTUN "):
        return OtomaxCategory.TOPUP_TARTUN, _channel_hint(t)
    return OtomaxCategory.OTHER, _channel_hint(t)


def _channel_hint(t: str) -> str | None:
    for pattern, channel in _TARTUN_CHANNEL:
        if pattern.search(t):
            return channel
    return None
