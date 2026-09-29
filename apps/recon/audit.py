"""Audit Data: SEMUA transaksi mentah (mutasi bank, debit, entri Otomax, baris yang
dikecualikan aturan) untuk satu tanggal atau rentang, masing-masing dengan status
rekonsiliasi + alasan -- supaya operator bisa mengecek semuanya dari satu halaman.

Modul ini hanya MEMBACA. Status diturunkan dari data yang sudah ada (match_status,
Match aktif, net_pair, tag manual); tidak ada aturan pencocokan baru di sini.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db.models import Q
from django.utils import timezone

from apps.core.enums import BANK_CHANNELS, Channel, MatchStatus, MatchType, OtomaxCategory
from apps.ingest.models import BankMutation, DebitIgnored, ExcludedTransaction, OtomaxEntry

from .models import Match

MAX_RANGE_DAYS = 62  # rentang lebih panjang terlalu berat untuk dirender per halaman

# Sumber data (bagian dari kunci baris "<sumber>:<id>")
SRC_BANK, SRC_DEBIT, SRC_OTOMAX, SRC_EXCLUDED = "B", "D", "O", "X"
SOURCE_MODELS = {
    SRC_BANK: BankMutation,
    SRC_DEBIT: DebitIgnored,
    SRC_OTOMAX: OtomaxEntry,
    SRC_EXCLUDED: ExcludedTransaction,
}

# Status gabungan (urutan = urutan kartu ringkasan)
COCOK_OTOMATIS = "cocok_otomatis"
COCOK_MANUAL = "cocok_manual"
USULAN = "usulan"
SELISIH_NOMINAL = "selisih_nominal"
TAG_MANUAL = "tag_manual"
DINETRALKAN = "dinetralkan"
BELUM_COCOK = "belum_cocok"
PENDING = "pending"
DIKECUALIKAN = "dikecualikan"
DEBIT = "debit"
NON_REKON = "non_rekon"
LAINNYA = "lainnya"

STATUS_LABELS = {
    COCOK_OTOMATIS: "Cocok otomatis",
    COCOK_MANUAL: "Cocok manual",
    USULAN: "Usulan, perlu konfirmasi",
    SELISIH_NOMINAL: "Cocok, selisih nominal",
    TAG_MANUAL: "Di-tag manual",
    DINETRALKAN: "Dinetralkan",
    BELUM_COCOK: "Belum cocok",
    PENDING: "Pending settle",
    DIKECUALIKAN: "Dikecualikan aturan",
    DEBIT: "Debit (uang keluar)",
    NON_REKON: "Non-rekon",
    LAINNYA: "Lainnya",
}
# Nada badge per status (kelas .badge-* di input.css)
STATUS_TONES = {
    COCOK_OTOMATIS: "good",
    COCOK_MANUAL: "good",
    USULAN: "warn",
    SELISIH_NOMINAL: "crit",
    TAG_MANUAL: "info",
    DINETRALKAN: "info",
    BELUM_COCOK: "warn",
    PENDING: "warn",
    DIKECUALIKAN: "mute",
    DEBIT: "mute",
    NON_REKON: "mute",
    LAINNYA: "mute",
}
SIDE_BANK, SIDE_OTOMAX = "bank", "otomax"


@dataclass
class AuditFilters:
    start: date
    end: date
    side: str = ""  # "" | bank | otomax
    source: str = ""  # channel: BRI/BCA/MANDIRI/MERCHANT_BCA/OTOMAX
    status: str = ""
    q: str = ""

    @property
    def is_range(self) -> bool:
        return self.start != self.end


@dataclass
class AuditRow:
    key: str
    src: str
    pk: int
    side: str
    channel: str
    book_date: date
    when: object
    party: str
    description: str
    amount: Decimal
    status: str
    reason: str
    filename: str
    uploaded_at: object
    link: str = ""
    category: str = ""

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def tone(self) -> str:
        return STATUS_TONES[self.status]

    @property
    def channel_label(self) -> str:
        try:
            return Channel(self.channel).label
        except ValueError:
            return self.channel


@dataclass
class AuditResult:
    rows: list[AuditRow]
    summary: dict = field(default_factory=dict)  # {side: {status: {"n", "amount"}}}


def _rp(v) -> str:
    return f"{v:,.0f}".replace(",", ".")


def parse_amount_query(q: str) -> Decimal | None:
    """'500.000', '500000', 'Rp 1.250.000', '-6.500' -> Decimal; teks biasa -> None."""
    raw = q.strip().lower().replace("rp", "").replace(" ", "").replace(".", "").replace(",", ".")
    if not raw or not any(ch.isdigit() for ch in raw):
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _active_matches(bank_ids: list[int], otomax_ids: list[int]):
    """Match aktif yang melibatkan baris-baris ini (lewat FK utama maupun gabungan M2M)."""
    by_bank: dict[int, Match] = {}
    by_otomax: dict[int, Match] = {}
    if not bank_ids and not otomax_ids:
        return by_bank, by_otomax
    qs = (
        Match.objects.filter(voided_at__isnull=True)
        .filter(
            Q(bank_mutation_id__in=bank_ids)
            | Q(bank_mutations__in=bank_ids)
            | Q(otomax_entry_id__in=otomax_ids)
            | Q(otomax_entries__in=otomax_ids)
        )
        .distinct()
        .select_related("bank_mutation", "otomax_entry")
        .prefetch_related("bank_mutations", "otomax_entries")
    )
    for m in qs:
        for b in [m.bank_mutation_id, *(x.id for x in m.bank_mutations.all())]:
            if b:
                by_bank.setdefault(b, m)
        for o in [m.otomax_entry_id, *(x.id for x in m.otomax_entries.all())]:
            if o:
                by_otomax.setdefault(o, m)
    return by_bank, by_otomax


def _match_status(m: Match) -> tuple[str, str]:
    if m.needs_review:
        return USULAN, f"Usulan mesin, belum final: {m.review_reason or m.get_match_type_display()}"
    if m.amount_diff != 0:
        return (
            SELISIH_NOMINAL,
            f"Bank Rp {_rp(m.amount_bank)} vs Otomax Rp {_rp(m.amount_otomax)} (selisih Rp {_rp(m.amount_diff)})",
        )
    if m.match_type == MatchType.MANUAL:
        return COCOK_MANUAL, f"Dipasangkan operator{': ' + m.note if m.note else ''}"
    return COCOK_OTOMATIS, m.get_match_type_display()


def _bank_status(b: BankMutation, m: Match | None, today: date) -> tuple[str, str, str]:
    """(status, alasan, link)"""
    d = b.book_date.isoformat()
    if b.match_status == MatchStatus.MANUAL and b.tag_manual and (m is None or m.otomax_entry_id is None):
        note = f" · {b.manual_note}" if b.manual_note else ""
        return TAG_MANUAL, f"Tag: {b.get_tag_manual_display()}{note}", f"/review-manual/?d={d}&tab=tagged"
    if m is not None:
        status, reason = _match_status(m)
        o = m.otomax_entry
        extra = m.otomax_entries.count() - 1 if o else 0
        partner = f"Otomax #{o.id} {o.reseller_name_raw} Rp {_rp(o.amount)}" if o else "tanpa pasangan Otomax"
        if extra > 0:
            partner += f" (+{extra} entri)"
        return status, f"{partner} · {reason}", f"/matches/?d={m.book_date.isoformat()}"
    if b.match_status == MatchStatus.UNMATCHED:
        age = (today - b.book_date).days
        return (
            BELUM_COCOK,
            "Belum ada pasangan Otomax" + (f" · terbuka {age} hari" if age > 0 else ""),
            f"/review-manual/?d={d}",
        )
    return LAINNYA, b.get_match_status_display(), ""


def _otomax_status(o: OtomaxEntry, m: Match | None, today: date) -> tuple[str, str, str]:
    d = o.book_date.isoformat()
    if o.match_status == MatchStatus.IGNORED:
        if o.net_pair_id:
            p = o.net_pair
            return (
                DINETRALKAN,
                f"Dinetralkan dengan #{p.id} {p.reseller_name_raw} Rp {_rp(p.amount)}",
                f"/reversal/?d={d}&tab=netted",
            )
        return LAINNYA, o.note or "Diabaikan", ""
    if m is not None:
        status, reason = _match_status(m)
        b = m.bank_mutation
        if b is None:
            return TAG_MANUAL, m.note or "Diselesaikan manual tanpa mutasi bank", f"/pending-settle/?d={d}&tab=resolved"
        extra = m.bank_mutations.count() - 1
        partner = f"{b.get_channel_display()} #{b.id} Rp {_rp(b.amount)}" + (f" (+{extra} mutasi)" if extra > 0 else "")
        return status, f"{partner} · {reason}", f"/matches/?d={m.book_date.isoformat()}"
    if o.category == OtomaxCategory.ADMIN:
        return NON_REKON, "Potongan admin, tidak direkonsiliasi", ""
    if o.match_status in (MatchStatus.UNMATCHED, MatchStatus.PENDING_SETTLE):
        age = (today - o.book_date).days
        cat = "" if o.category == OtomaxCategory.TOPUP_TARTUN else f" · {o.get_category_display()}"
        return (
            PENDING,
            "Uang belum ditemukan di mutasi bank" + cat + (f" · terbuka {age} hari" if age > 0 else ""),
            f"/pending-settle/?d={d}",
        )
    return LAINNYA, o.get_match_status_display(), ""


def _batch_info(obj):
    b = obj.import_batch
    return (b.source_filename, b.created_at) if b else ("", None)


def collect(filters: AuditFilters) -> AuditResult:
    """Semua baris sesuai filter, urut waktu. Ringkasan dihitung dari baris SEBELUM filter
    status (supaya kartu menunjukkan gambaran lengkap), tapi sesudah filter lain."""
    rng = (filters.start, filters.end)
    today = timezone.localdate()
    want_bank = filters.side in ("", SIDE_BANK) and filters.source != Channel.OTOMAX
    want_otomax = filters.side in ("", SIDE_OTOMAX) and filters.source in ("", Channel.OTOMAX)
    bank_channel = filters.source if filters.source in BANK_CHANNELS else ""

    amount_q = parse_amount_query(filters.q) if filters.q else None
    text_q = filters.q.strip() if filters.q and amount_q is None else ""

    def _narrow(qs, desc_fields):
        if amount_q is not None:
            qs = qs.filter(Q(amount=amount_q) | Q(amount=-amount_q))
        elif text_q:
            cond = Q(import_batch__source_filename__icontains=text_q)
            for f in desc_fields:
                cond |= Q(**{f"{f}__icontains": text_q})
            qs = qs.filter(cond)
        return qs

    rows: list[AuditRow] = []

    if want_bank:
        bqs = BankMutation.objects.filter(book_date__range=rng).select_related("import_batch")
        dqs = DebitIgnored.objects.filter(book_date__range=rng).select_related("import_batch")
        xqs = ExcludedTransaction.objects.filter(book_date__range=rng, source_type="BANK").select_related(
            "import_batch"
        )
        if bank_channel:
            bqs, dqs, xqs = (
                bqs.filter(channel=bank_channel),
                dqs.filter(channel=bank_channel),
                xqs.filter(channel=bank_channel),
            )
        bqs = list(_narrow(bqs, ["description_raw", "outlet_name", "external_ref"]))
        by_bank, _ = _active_matches([b.id for b in bqs], [])
        for b in bqs:
            status, reason, link = _bank_status(b, by_bank.get(b.id), today)
            fn, up = _batch_info(b)
            rows.append(
                AuditRow(
                    f"{SRC_BANK}:{b.id}",
                    SRC_BANK,
                    b.id,
                    SIDE_BANK,
                    b.channel,
                    b.book_date,
                    b.txn_datetime,
                    b.outlet_name or b.external_ref,
                    b.description_raw,
                    b.amount,
                    status,
                    reason,
                    fn,
                    up,
                    link,
                )
            )
        for d in _narrow(dqs, ["description_raw"]):
            fn, up = _batch_info(d)
            rows.append(
                AuditRow(
                    f"{SRC_DEBIT}:{d.id}",
                    SRC_DEBIT,
                    d.id,
                    SIDE_BANK,
                    d.channel,
                    d.book_date,
                    d.txn_datetime,
                    "",
                    d.description_raw,
                    -d.amount,
                    DEBIT,
                    "Uang keluar, dicatat terpisah (tidak dicocokkan)",
                    fn,
                    up,
                )
            )
        for x in _narrow(xqs, ["description_raw", "reason"]):
            fn, up = _batch_info(x)
            rows.append(
                AuditRow(
                    f"{SRC_EXCLUDED}:{x.id}",
                    SRC_EXCLUDED,
                    x.id,
                    SIDE_BANK,
                    x.channel,
                    x.book_date,
                    x.txn_datetime,
                    "",
                    x.description_raw,
                    x.amount,
                    DIKECUALIKAN,
                    x.reason or "Dipisah oleh aturan filter",
                    fn,
                    up,
                    f"/rules/?d={x.book_date.isoformat()}",
                    x.category,
                )
            )

    if want_otomax:
        oqs = OtomaxEntry.objects.filter(book_date__range=rng).select_related("import_batch", "net_pair")
        xqs = ExcludedTransaction.objects.filter(book_date__range=rng, source_type="OTOMAX").select_related(
            "import_batch"
        )
        oqs = list(_narrow(oqs, ["description_raw", "reseller_name_raw"]))
        _, by_otomax = _active_matches([], [o.id for o in oqs])
        for o in oqs:
            status, reason, link = _otomax_status(o, by_otomax.get(o.id), today)
            fn, up = _batch_info(o)
            rows.append(
                AuditRow(
                    f"{SRC_OTOMAX}:{o.id}",
                    SRC_OTOMAX,
                    o.id,
                    SIDE_OTOMAX,
                    Channel.OTOMAX,
                    o.book_date,
                    o.entry_datetime,
                    o.reseller_name_raw,
                    o.description_raw,
                    o.amount,
                    status,
                    reason,
                    fn,
                    up,
                    link,
                    o.get_category_display(),
                )
            )
        for x in _narrow(xqs, ["description_raw", "reason"]):
            fn, up = _batch_info(x)
            rows.append(
                AuditRow(
                    f"{SRC_EXCLUDED}:{x.id}",
                    SRC_EXCLUDED,
                    x.id,
                    SIDE_OTOMAX,
                    Channel.OTOMAX,
                    x.book_date,
                    x.txn_datetime,
                    "",
                    x.description_raw,
                    x.amount,
                    DIKECUALIKAN,
                    x.reason or "Dipisah oleh aturan filter",
                    fn,
                    up,
                    f"/rules/?d={x.book_date.isoformat()}",
                    x.category,
                )
            )

    summary = defaultdict(lambda: defaultdict(lambda: {"n": 0, "amount": Decimal("0")}))
    for r in rows:
        s = summary[r.side][r.status]
        s["n"] += 1
        s["amount"] += r.amount

    if filters.status:
        rows = [r for r in rows if r.status == filters.status]
    rows.sort(key=lambda r: (r.book_date, r.when.timestamp() if r.when else 0.0, r.key))
    return AuditResult(rows=rows, summary={side: dict(v) for side, v in summary.items()})


def status_counts(result: AuditResult) -> Counter:
    c = Counter()
    for side in result.summary.values():
        for status, v in side.items():
            c[status] += v["n"]
    return c
