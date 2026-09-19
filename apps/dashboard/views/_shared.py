"""Helper lintas-halaman dashboard: parsing tanggal, guard staff-only, pencarian
kandidat pencocokan manual otomatis, dan sinkronisasi tanggal batch yang tidak konsisten."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from django.contrib.auth.decorators import user_passes_test
from django.utils import timezone

from apps.ingest.models import ImportBatch

staff_only = user_passes_test(lambda u: u.is_superuser)


def _parse_date(raw: str | None, default=None) -> date:
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return default or timezone.localdate()


def _find_auto_pairs(unmatched_banks, pending_otomax):
    def get_name_from_bank(desc):
        parts = (desc or "").split(" - ")
        return parts[-1].strip().upper() if len(parts) >= 2 else ""

    def get_name_from_otomax(desc):
        m = re.search(r"BFST\d+([A-Z\s]+?)(?::|$)", (desc or "").upper())
        if m:
            return m.group(1).strip()
        return ""

    used_b = set()
    used_o = set()
    pairs = []

    # Tahap 1: Nama pengirim + nominal persis
    for o in pending_otomax:
        o_name = get_name_from_otomax(o.description_raw or "")
        if not o_name or len(o_name) < 3:
            continue
        for b in unmatched_banks:
            if b.id in used_b:
                continue
            if b.amount != o.amount:
                continue
            b_name = get_name_from_bank(b.description_raw or "")
            if b_name and len(b_name) >= 3 and (o_name in b_name or b_name in o_name):
                pairs.append((b, o, f"Cocok referensi nama ({b_name}) & nominal persis"))
                used_b.add(b.id)
                used_o.add(o.id)
                break

    # Tahap 2: Nominal 1-ke-1 unik
    rem_banks = [b for b in unmatched_banks if b.id not in used_b]
    rem_otomax = [o for o in pending_otomax if o.id not in used_o]

    banks_by_amt = defaultdict(list)
    for b in rem_banks:
        banks_by_amt[b.amount].append(b)

    otomax_by_amt = defaultdict(list)
    for o in rem_otomax:
        otomax_by_amt[o.amount].append(o)

    for amt, b_list in banks_by_amt.items():
        o_list = otomax_by_amt.get(amt, [])
        if len(b_list) == 1 and len(o_list) == 1:
            bm = b_list[0]
            oe = o_list[0]
            pairs.append((bm, oe, f"Cocok referensi nominal unik persis (Rp {amt:,.0f})"))
            used_b.add(bm.id)
            used_o.add(oe.id)

    return pairs


def _sync_inconsistent_batches():
    """Otomatis selaraskan tanggal batch jika berbeda dengan tanggal transaksi di dalamnya,
    dan bersihkan batch kosong tanpa transaksi."""
    for b in ImportBatch.objects.all():
        mut_dates = list(b.mutations.values_list("book_date", flat=True))
        oto_dates = list(b.otomax.values_list("book_date", flat=True))
        all_dates = mut_dates + oto_dates
        if all_dates:
            target_date = max(set(all_dates), key=all_dates.count)
            if b.book_date != target_date:
                b.book_date = target_date
                b.save(update_fields=["book_date", "updated_at"])
        elif (
            b.mutations.count() == 0
            and b.otomax.count() == 0
            and b.debits.count() == 0
            and b.excluded_transactions.count() == 0
        ):
            b.delete()
