"""Hapus baris data mentah satu per satu dari menu Audit Data.

Yang dihapus adalah BARIS itu sendiri (mutasi bank, debit, entri Otomax, atau baris yang
dikecualikan aturan) -- bukan pasangan pencocokannya. Kalau baris itu sudah cocok /
dinetralkan, pasangannya dibatalkan dulu lewat fungsi yang sudah ada, sehingga lawannya
TETAP ADA dan kembali ke antrean belum cocok.

Aturan:
- Ditolak bila tanggal baris, tanggal pasangan aktifnya, atau tanggal lawan netralnya sudah
  tutup buku (aturan Day-Lock).
- Setiap baris yang dihapus dicatat di log Django Admin ("Recent actions") beserta
  pelakunya -- tabel data mentah tidak punya riwayat sendiri.
- Baris yang dihapus akan terbaca "Baru" lagi kalau file aslinya di-upload ulang.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.contrib.admin.models import DELETION, LogEntry
from django.db import transaction
from django.db.models import Q

from apps.core.enums import DiscrepancyStatus, MatchStatus
from apps.ingest.models import ImportBatch, OtomaxEntry

from .audit import SOURCE_MODELS, SRC_BANK, SRC_DEBIT, SRC_EXCLUDED, SRC_OTOMAX, _rp
from .models import Adjustment, Discrepancy, Match, ReconDay
from .purge import refresh_recon_day

SOURCE_LABELS = {
    SRC_BANK: "Mutasi bank",
    SRC_DEBIT: "Debit bank",
    SRC_OTOMAX: "Entri Otomax",
    SRC_EXCLUDED: "Dikecualikan aturan",
}


@dataclass
class DeletePlan:
    objects: list = field(default_factory=list)  # (src, obj) yang boleh dihapus
    blocked: list = field(default_factory=list)  # (src, obj, alasan)
    missing: int = 0

    @property
    def by_source(self) -> list[dict]:
        agg = defaultdict(lambda: {"n": 0, "amount": Decimal("0")})
        for src, obj in self.objects:
            agg[src]["n"] += 1
            agg[src]["amount"] += _signed_amount(src, obj)
        return [{"label": SOURCE_LABELS[s], **v} for s, v in agg.items()]

    @property
    def total(self) -> int:
        return len(self.objects)


def _signed_amount(src, obj) -> Decimal:
    return -obj.amount if src == SRC_DEBIT else obj.amount


def parse_keys(keys) -> dict[str, list[int]]:
    out: dict[str, list[int]] = defaultdict(list)
    for k in keys:
        src, _, pk = str(k).partition(":")
        if src in SOURCE_MODELS and pk.isdigit():
            out[src].append(int(pk))
    return out


def _matches_of(src, obj):
    if src == SRC_BANK:
        return Match.objects.filter(Q(bank_mutation=obj) | Q(bank_mutations=obj)).distinct()
    if src == SRC_OTOMAX:
        return Match.objects.filter(Q(otomax_entry=obj) | Q(otomax_entries=obj)).distinct()
    return Match.objects.none()


def _affected_dates(src, obj) -> set:
    dates = {obj.book_date}
    for m in _matches_of(src, obj).filter(voided_at__isnull=True).prefetch_related("bank_mutations", "otomax_entries"):
        dates.add(m.book_date)
        for x in (m.bank_mutation, m.otomax_entry, *m.bank_mutations.all(), *m.otomax_entries.all()):
            if x is not None:
                dates.add(x.book_date)
    if src == SRC_OTOMAX and obj.net_pair_id:
        dates.add(obj.net_pair.book_date)
    return dates


def _active_matches_of(src, ids):
    cond = (
        Q(bank_mutation_id__in=ids) | Q(bank_mutations__in=ids)
        if src == SRC_BANK
        else Q(otomax_entry_id__in=ids) | Q(otomax_entries__in=ids)
    )
    return (
        Match.objects.filter(cond, voided_at__isnull=True)
        .distinct()
        .prefetch_related("bank_mutations", "otomax_entries")
    )


def _bulk_affected_dates(src, objs) -> dict[int, set]:
    """Seperti _affected_dates, tapi untuk banyak baris sekaligus (beberapa query saja)."""
    dates = {o.pk: {o.book_date} for o in objs}
    if src in (SRC_BANK, SRC_OTOMAX) and objs:
        ids = list(dates)
        for m in _active_matches_of(src, ids).select_related("bank_mutation", "otomax_entry"):
            banks = [x for x in (m.bank_mutation, *m.bank_mutations.all()) if x is not None]
            otos = [x for x in (m.otomax_entry, *m.otomax_entries.all()) if x is not None]
            md = {m.book_date} | {x.book_date for x in banks} | {x.book_date for x in otos}
            for x in banks if src == SRC_BANK else otos:
                if x.pk in dates:
                    dates[x.pk] |= md
    if src == SRC_OTOMAX:
        for o in objs:
            if o.net_pair_id:
                dates[o.pk].add(o.net_pair.book_date)
    return dates


def plan_delete(keys) -> DeletePlan:
    """Baca-saja: baris mana yang akan dihapus dan mana yang ditolak (hari ditutup)."""
    plan = DeletePlan()
    wanted = parse_keys(keys)
    requested = sum(len(set(v)) for v in wanted.values())
    for src, ids in wanted.items():
        qs = SOURCE_MODELS[src].objects.filter(pk__in=set(ids))
        if src == SRC_OTOMAX:
            qs = qs.select_related("net_pair")
        objs = list(qs)
        dates = _bulk_affected_dates(src, objs)
        all_dates = set().union(*dates.values()) if dates else set()
        locked = set(ReconDay.objects.filter(book_date__in=all_dates, locked=True).values_list("book_date", flat=True))
        for o in objs:
            hit = sorted(dates[o.pk] & locked)
            if hit:
                tgl = ", ".join(d.strftime("%d %b %Y") for d in hit)
                plan.blocked.append((src, o, f"Tanggal {tgl} sudah tutup buku"))
            else:
                plan.objects.append((src, o))
    plan.missing = requested - len(plan.objects) - len(plan.blocked)
    return plan


def count_paired(plan: DeletePlan) -> int:
    """Berapa baris di rencana yang masih berpasangan (cocok / dinetralkan) -- pasangannya
    akan dibatalkan dan lawannya kembali ke antrean."""
    n = 0
    for src in (SRC_BANK, SRC_OTOMAX):
        objs = [o for s, o in plan.objects if s == src]
        if not objs:
            continue
        ids = [o.pk for o in objs]
        paired = set()
        for m in _active_matches_of(src, ids):
            if src == SRC_BANK:
                paired |= {m.bank_mutation_id, *(x.pk for x in m.bank_mutations.all())}
            else:
                paired |= {m.otomax_entry_id, *(x.pk for x in m.otomax_entries.all())}
        if src == SRC_OTOMAX:
            paired |= {o.pk for o in objs if o.net_pair_id}
        n += len(paired & set(ids))
    return n


@transaction.atomic
def delete_raw_rows(keys, user=None) -> dict:
    """Hapus baris-baris mentah yang lolos plan_delete(). Return ringkasan hitungan."""
    from .engine.leftovers import make_otomax_only
    from .resolve import unpair_match

    plan = plan_delete(keys)
    dates_to_refresh: set = set()
    batches: set[int] = set()
    unpaired = 0
    unnetted = 0

    for src, obj in plan.objects:
        model = SOURCE_MODELS[src]
        obj = model.objects.select_for_update().filter(pk=obj.pk).first()
        if obj is None:  # sudah terhapus oleh baris lain di permintaan yang sama
            continue
        dates_to_refresh |= _affected_dates(src, obj)
        batches.add(obj.import_batch_id)

        # 1. Pasangan aktif dibatalkan -> lawannya kembali ke antrean belum cocok.
        for m in _matches_of(src, obj).filter(voided_at__isnull=True):
            unpair_match(m, user=user if getattr(user, "pk", None) else None)
            unpaired += 1

        # 2. Lawan netral dikembalikan ke Pending Settle (dulu bisa tersembunyi selamanya).
        if src == SRC_OTOMAX and obj.net_pair_id:
            partner = OtomaxEntry.objects.select_for_update().get(pk=obj.net_pair_id)
            if partner.net_pair_id == obj.pk and partner.match_status == MatchStatus.IGNORED:
                partner.net_pair = None
                partner.match_status = MatchStatus.PENDING_SETTLE
                partner.note = "Lawan netralnya dihapus dari Audit Data"
                partner.save(update_fields=["net_pair", "match_status", "note", "updated_at"])
                if not partner.discrepancies.filter(status=DiscrepancyStatus.OPEN).exists():
                    make_otomax_only(partner)
                unnetted += 1

        # 3. Catatan pasangan lama (sudah dibatalkan) & selisih milik baris ini (FK PROTECT).
        if src in (SRC_BANK, SRC_OTOMAX):
            fk = "bank_mutation" if src == SRC_BANK else "otomax_entry"
            Match.objects.filter(**{fk: obj}).delete()
            discs = Discrepancy.objects.filter(**{fk: obj})
            Adjustment.objects.filter(discrepancy__in=discs).delete()
            discs.delete()

        # 4. Jejak audit, lalu hapus barisnya.
        if getattr(user, "pk", None):
            LogEntry.objects.log_actions(
                user.pk,
                [obj],
                DELETION,
                change_message=(
                    f"Dihapus dari Audit Data: {SOURCE_LABELS[src]} {obj.book_date} Rp {_rp(_signed_amount(src, obj))} "
                    f"— {obj.description_raw[:120]}"
                ),
                single_object=True,
            )
        obj.delete()

    # 5. Rapikan batch asal (hitungan baris) & angka hari yang terdampak.
    for batch in ImportBatch.objects.filter(pk__in=batches):
        remaining = batch.mutations.count() + batch.otomax.count() + batch.debits.count()
        excluded = batch.excluded_transactions.count()
        if remaining + excluded == 0:
            batch.delete()
            continue
        batch.row_count = remaining + excluded
        batch.excluded_count = excluded
        batch.save(update_fields=["row_count", "excluded_count", "updated_at"])
    for d in dates_to_refresh:
        refresh_recon_day(d)

    return {
        "deleted": len(plan.objects),
        "blocked": len(plan.blocked),
        "missing": plan.missing,
        "unpaired": unpaired,
        "unnetted": unnetted,
    }
