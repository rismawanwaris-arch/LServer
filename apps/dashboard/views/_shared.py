"""Helper lintas-halaman dashboard: parsing tanggal, guard staff-only, pencarian
kandidat pencocokan manual otomatis, sinkronisasi tanggal batch yang tidak konsisten,
dan checklist 'Langkah Hari Ini' (dipakai di halaman Upload Data)."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from django.contrib.auth.decorators import user_passes_test
from django.db import models
from django.urls import reverse
from django.utils import timezone

from apps.core.enums import MatchStatus, OtomaxCategory
from apps.ingest.models import ImportBatch, OtomaxEntry
from apps.recon.models import Discrepancy, Match, ReconDay
from apps.recon.reports import get_daily_summary

staff_only = user_passes_test(lambda u: u.is_superuser)

_REVERSAL_OPEN_STATUSES = [MatchStatus.UNMATCHED, MatchStatus.PENDING_SETTLE]


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
    # Bersihkan batch kosong tanpa data transaksi secara bulk
    ImportBatch.objects.filter(
        mutations__isnull=True,
        otomax__isnull=True,
        debits__isnull=True,
        excluded_transactions__isnull=True,
    ).delete()

    # Hanya periksa batch yang memiliki transaksi dengan tanggal berbeda dari tanggal batch
    inconsistent_batch_ids = set(
        ImportBatch.objects.filter(
            models.Q(mutations__isnull=False) & ~models.Q(mutations__book_date=models.F("book_date"))
        ).values_list("id", flat=True)
    ) | set(
        ImportBatch.objects.filter(
            models.Q(otomax__isnull=False) & ~models.Q(otomax__book_date=models.F("book_date"))
        ).values_list("id", flat=True)
    )

    if inconsistent_batch_ids:
        for b in ImportBatch.objects.filter(id__in=inconsistent_batch_ids):
            mut_dates = list(b.mutations.values_list("book_date", flat=True))
            oto_dates = list(b.otomax.values_list("book_date", flat=True))
            all_dates = mut_dates + oto_dates
            if all_dates:
                target_date = max(set(all_dates), key=all_dates.count)
                if b.book_date != target_date:
                    b.book_date = target_date
                    b.save(update_fields=["book_date", "updated_at"])


def build_today_steps(book_date: date) -> list[dict]:
    """Checklist 'Langkah Hari Ini' — urutan kerja harian yang sebenarnya, dengan status
    dihitung dari data riil (bukan checklist statis), supaya operator baru tidak perlu
    hafal struktur menu untuk tahu harus mulai dari mana. Ditampilkan di halaman Upload
    Data (pintu masuk alur kerja harian), sengaja BUKAN di Dashboard supaya Dashboard
    tetap ringkasan/KPI satu layar."""
    d = book_date.isoformat()
    day = ReconDay.objects.filter(book_date=book_date).first()
    summary = get_daily_summary(book_date)
    has_import_batch = ImportBatch.objects.filter(book_date=book_date).exists()
    engine_has_run = (
        Match.objects.filter(book_date=book_date, voided_at__isnull=True).exists()
        or Discrepancy.objects.filter(origin_book_date=book_date).exists()
    )
    reversal_open_count = OtomaxEntry.objects.filter(
        book_date=book_date, category=OtomaxCategory.REVERSAL, match_status__in=_REVERSAL_OPEN_STATUSES
    ).count()

    steps = [
        {
            "title": "Upload Data",
            "desc": "Import mutasi bank & data Otomax untuk tanggal ini."
            if not has_import_batch
            else "Data sudah diimport.",
            "done": has_import_batch,
            "url": f"{reverse('upload')}?d={d}",
        },
        {
            "title": "Jalankan Matching Engine",
            "desc": (
                "Cocokkan otomatis mutasi bank vs Otomax." if not engine_has_run else "Pencocokan sudah dijalankan."
            ),
            "done": engine_has_run,
            "url": f"{reverse('day')}?d={d}#aksi-rekonsiliasi",
        },
        {
            "title": "Selesaikan Review Manual",
            "desc": (
                "Jalankan matching engine dulu."
                if not engine_has_run
                else f"{summary['unmatched_bank_count']} mutasi bank belum ada pasangan."
                if summary["unmatched_bank_count"]
                else "Semua mutasi bank sudah tertangani."
            ),
            # Cuma dianggap "selesai" kalau engine-nya SUDAH dijalankan dan hitungannya
            # nol -- di hari kosong (belum ada data sama sekali) hitungannya juga
            # trivially nol, tapi itu bukan "sudah selesai", cuma "belum mulai".
            "done": engine_has_run and summary["unmatched_bank_count"] == 0,
            "url": f"{reverse('manual-review')}?d={d}",
        },
        {
            "title": "Selesaikan Pending Settle",
            "desc": (
                "Jalankan matching engine dulu."
                if not engine_has_run
                else f"{summary['pending_settle_count']} entri Otomax belum ada pasangan."
                if summary["pending_settle_count"]
                else "Semua entri Otomax sudah tertangani."
            ),
            "done": engine_has_run and summary["pending_settle_count"] == 0,
            "url": f"{reverse('pending-settle')}?d={d}",
        },
        {
            "title": "Cek Reversal Otomax",
            "desc": (
                "Jalankan matching engine dulu."
                if not engine_has_run
                else f"{reversal_open_count} baris REV belum netted."
                if reversal_open_count
                else "Semua REV sudah netted."
            ),
            "done": engine_has_run and reversal_open_count == 0,
            "url": f"{reverse('reversal')}?d={d}",
        },
        {
            "title": "Tutup Buku Harian",
            "desc": "Kunci data tanggal ini setelah semua selisih di atas ditangani.",
            "done": bool(day and day.locked),
            "url": f"{reverse('day')}?d={d}#aksi-rekonsiliasi",
        },
    ]
    next_index = next((i for i, s in enumerate(steps) if not s["done"]), None)
    for i, s in enumerate(steps):
        s["number"] = i + 1
        s["is_next"] = i == next_index
    return steps
