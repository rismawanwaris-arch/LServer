"""Periksa isi file upload SEBELUM disimpan: status tiap baris + alasannya.

Satu aturan dipakai dua kali (Blueprint Upload: "server hakim terakhir"): untuk layar
Periksa, dan lagi oleh import_file() tepat sebelum menulis -- jadi yang tersimpan selalu
sesuai aturan terbaru, bukan sekadar percaya hasil pratinjau. Modul ini hanya MEMBACA DB.

Status (urutan cek):
  kembar_file  -- sidik baris sama dengan baris lain di file yang sama
  sudah_ada    -- sidik baris (row_hash) persis sudah tersimpan
  mungkin_ada  -- kunci stabil (waktu presisi + nominal, atau no. referensi) cocok dengan
                  baris tersimpan walau teks keterangannya beda format (mis. parser BRI
                  berubah 14 Sep). Bawaan TIDAK disimpan; operator bisa memilihnya.
  ditutup      -- tanggal baris sudah tutup buku
  baru         -- akan disimpan
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time

from django.utils import timezone

from apps.catalog.models import ExclusionRule
from apps.catalog.services import find_matching_rule
from apps.core.enums import Channel
from apps.recon.models import ReconDay

from .models import BankMutation, DebitIgnored, ExcludedTransaction, ImportBatch, OtomaxEntry
from .parsers import ParseResult, parse_file

BARU, SUDAH_ADA, MUNGKIN_ADA, KEMBAR_FILE, DITUTUP = "baru", "sudah_ada", "mungkin_ada", "kembar_file", "ditutup"
STATUS_LABELS = {
    BARU: "Baru",
    SUDAH_ADA: "Sudah ada",
    MUNGKIN_ADA: "Kemungkinan sudah ada",
    KEMBAR_FILE: "Kembar di file",
    DITUTUP: "Hari ditutup",
}
MUTASI, DEBIT, DIKECUALIKAN, OTOMAX = "mutasi", "debit", "dikecualikan", "otomax"


def _hash(*parts) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None or timezone.is_aware(dt):
        return dt
    return timezone.make_aware(dt)


def row_book_date(dt: datetime | None, fallback: date) -> date:
    return dt.date() if dt else fallback


def bank_row_hash(channel: str, row, row_bdate: date) -> str:
    """Sidik baris mutasi -- rumus TIDAK boleh berubah (data lama memakainya)."""
    return _hash(channel, row_bdate, row.description_raw, row.amount, row.txn_datetime, row.external_ref)


def otomax_row_hash(row) -> str:
    """Sidik baris Otomax -- rumus TIDAK boleh berubah (data lama memakainya)."""
    return _hash(Channel.OTOMAX, row.description_raw, row.amount, row.entry_datetime, row.reseller_name_raw)


def _precise(dt: datetime | None) -> bool:
    # BCA & Merchant BCA hanya punya tanggal (jam 00:00:00): tanggal+nominal sering kembar
    # untuk transaksi yang SAH berbeda, jadi tidak boleh dipakai sebagai kunci.
    return dt is not None and dt.time() != time(0, 0, 0)


def bank_stable_key(channel: str, txn_datetime, row_bdate: date, external_ref: str, signed_amount):
    """Kunci yang tidak bergantung pada teks keterangan, atau None bila tidak aman."""
    if _precise(txn_datetime):
        return ("T", channel, _aware(txn_datetime), signed_amount)
    if external_ref:
        return ("R", channel, row_bdate, external_ref, signed_amount)
    return None


def otomax_stable_key(entry_datetime, reseller_name_raw: str, amount):
    if _precise(entry_datetime):
        return ("O", _aware(entry_datetime), reseller_name_raw, amount)
    return None


@dataclass
class ReviewRow:
    no: int
    parsed: object
    kind: str
    row_hash: str
    row_bdate: date
    stable_key: tuple | None = None
    rule: ExclusionRule | None = None
    status: str = BARU
    reason: str = ""

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def amount(self):
        return self.parsed.amount

    @property
    def description(self) -> str:
        return self.parsed.description_raw

    @property
    def when(self):
        return getattr(self.parsed, "txn_datetime", None) or getattr(self.parsed, "entry_datetime", None)

    @property
    def party(self) -> str:
        p = self.parsed
        return getattr(p, "reseller_name_raw", "") or getattr(p, "outlet_name", "") or getattr(p, "external_ref", "")


@dataclass
class UploadReview:
    channel: str
    filename: str
    book_date: date
    result: ParseResult
    rows: list[ReviewRow] = field(default_factory=list)
    same_file_batches: list[ImportBatch] = field(default_factory=list)
    dates: list[dict] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        c = Counter(r.status for r in self.rows)
        return {s: c.get(s, 0) for s in STATUS_LABELS}

    @property
    def new_rows(self) -> list[ReviewRow]:
        return [r for r in self.rows if r.status == BARU]

    @property
    def total_new_amount(self):
        return sum((r.amount for r in self.new_rows if r.kind in (MUTASI, OTOMAX)), 0)


def file_fingerprint(channel: str, content: bytes) -> str:
    """Sama persis dengan ImportBatch.file_hash yang disimpan import_file()."""
    return _hash(channel, hashlib.sha256(content).hexdigest())


def _origin(obj, label: str) -> str:
    b = obj.import_batch
    when = timezone.localtime(b.created_at).strftime("%d %b %H:%M") if b and b.created_at else "-"
    return f"{label} di {b.source_filename if b else '-'} (upload {when})"


def analyze_upload(channel: str, content: bytes, book_date: date, filename: str = "") -> UploadReview:
    result = parse_file(channel, content)
    effective = result.book_date or book_date
    review = UploadReview(channel=channel, filename=filename, book_date=effective, result=result)
    rules = list(ExclusionRule.objects.filter(active=True))

    # 1. Bentuk baris + sidik + kunci stabil
    if channel == Channel.OTOMAX:
        for i, r in enumerate(result.otomax_rows, start=1):
            rule = find_matching_rule(r.description_raw, channel="", is_bank=False, rules=rules)
            review.rows.append(
                ReviewRow(
                    no=i,
                    parsed=r,
                    kind=DIKECUALIKAN if rule else OTOMAX,
                    row_hash=otomax_row_hash(r),
                    row_bdate=row_book_date(r.entry_datetime, effective),
                    stable_key=None if rule else otomax_stable_key(r.entry_datetime, r.reseller_name_raw, r.amount),
                    rule=rule,
                )
            )
    else:
        for i, r in enumerate(result.bank_rows, start=1):
            bdate = row_book_date(r.txn_datetime, effective)
            rule = find_matching_rule(r.description_raw, channel=channel, is_bank=True, rules=rules)
            kind = DIKECUALIKAN if rule else (DEBIT if r.amount is not None and r.amount < 0 else MUTASI)
            review.rows.append(
                ReviewRow(
                    no=i,
                    parsed=r,
                    kind=kind,
                    row_hash=bank_row_hash(channel, r, bdate),
                    row_bdate=bdate,
                    stable_key=bank_stable_key(channel, r.txn_datetime, bdate, r.external_ref, r.amount),
                    rule=rule,
                )
            )

    hashes = [r.row_hash for r in review.rows]
    days = {r.row_bdate for r in review.rows}

    # 2. Sidik persis yang sudah tersimpan (di tabel mana pun baris itu bisa berakhir)
    existing: dict[str, str] = {}
    tables = (
        [(OtomaxEntry, "Sudah tersimpan"), (ExcludedTransaction, "Sudah dikecualikan")]
        if channel == Channel.OTOMAX
        else [
            (BankMutation, "Sudah tersimpan"),
            (DebitIgnored, "Sudah dicatat sebagai debit"),
            (ExcludedTransaction, "Sudah dikecualikan"),
        ]
    )
    for i in range(0, len(hashes), 500):
        chunk = hashes[i : i + 500]
        for model, label in tables:
            for obj in model.objects.filter(row_hash__in=chunk).select_related("import_batch"):
                existing.setdefault(obj.row_hash, _origin(obj, label))

    # 3. Kunci stabil baris tersimpan di tanggal yang sama -- kecuali baris yang sudah
    #    cocok persis dengan baris file (satu baris tersimpan tidak boleh "dipakai" dua kali).
    stable_db: Counter = Counter()
    stable_origin: dict[tuple, str] = {}

    def _add(key, obj, label):
        if key is None or obj.row_hash in existing:
            return
        stable_db[key] += 1
        stable_origin.setdefault(key, _origin(obj, label))

    if days:
        if channel == Channel.OTOMAX:
            for o in OtomaxEntry.objects.filter(book_date__in=days).select_related("import_batch"):
                _add(
                    otomax_stable_key(o.entry_datetime, o.reseller_name_raw, o.amount),
                    o,
                    "Waktu, reseller & nominal sama dengan baris",
                )
        else:
            for m in BankMutation.objects.filter(channel=channel, book_date__in=days).select_related("import_batch"):
                _add(
                    bank_stable_key(channel, m.txn_datetime, m.book_date, m.external_ref, m.amount),
                    m,
                    "Waktu & nominal sama dengan baris",
                )
            for d in DebitIgnored.objects.filter(channel=channel, book_date__in=days).select_related("import_batch"):
                _add(
                    bank_stable_key(channel, d.txn_datetime, d.book_date, "", -d.amount),
                    d,
                    "Waktu & nominal sama dengan debit",
                )
            for e in ExcludedTransaction.objects.filter(
                channel=channel, source_type="BANK", book_date__in=days
            ).select_related("import_batch"):
                _add(
                    bank_stable_key(channel, e.txn_datetime, e.book_date, "", e.amount),
                    e,
                    "Waktu & nominal sama dengan baris dikecualikan",
                )

    locked = set(ReconDay.objects.filter(book_date__in=days, locked=True).values_list("book_date", flat=True))

    # 4. Status per baris
    seen: dict[str, int] = {}
    for r in review.rows:
        if r.row_hash in seen:
            r.status, r.reason = KEMBAR_FILE, f"Sama persis dengan baris #{seen[r.row_hash]} di file ini"
        elif r.row_hash in existing:
            r.status, r.reason = SUDAH_ADA, existing[r.row_hash]
        elif r.stable_key is not None and stable_db[r.stable_key] > 0:
            stable_db[r.stable_key] -= 1
            r.status = MUNGKIN_ADA
            r.reason = f"{stable_origin[r.stable_key]}; keterangan beda format"
        elif r.row_bdate in locked:
            r.status, r.reason = DITUTUP, f"Tanggal {r.row_bdate.strftime('%d %b %Y')} sudah tutup buku"
        else:
            r.reason = {
                DIKECUALIKAN: f"Dipisah oleh aturan: {r.rule.name}" if r.rule else "",
                DEBIT: "Uang keluar, dicatat terpisah",
            }.get(r.kind, "")
        seen.setdefault(r.row_hash, r.no)

    # 5. Ringkasan file
    review.same_file_batches = list(
        ImportBatch.objects.filter(file_hash=file_fingerprint(channel, content)).order_by("-created_at")
    )
    per_day = Counter(r.row_bdate for r in review.rows)
    review.dates = [{"date": d, "rows": n, "locked": d in locked} for d, n in sorted(per_day.items())]
    return review
