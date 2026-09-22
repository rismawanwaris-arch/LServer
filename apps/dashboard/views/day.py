"""Halaman 1: Dashboard — ringkasan harian & alarm selisih yang belum ditangani."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import reverse

from apps.core.enums import Channel, MatchStatus, OtomaxCategory
from apps.ingest.models import ImportBatch, OtomaxEntry
from apps.recon.models import Adjustment, Discrepancy
from apps.recon.reports import get_daily_summary
from apps.recon.selectors import alarm_discrepancies, cumulative_open_total, day_overview

from ._shared import _parse_date

_REVERSAL_OPEN_STATUSES = [MatchStatus.UNMATCHED, MatchStatus.PENDING_SETTLE]


def _today_steps(book_date, day, summary, has_import_batch, engine_has_run, reversal_open_count):
    """Checklist 'Langkah Hari Ini' — urutan kerja harian yang sebenarnya, dengan status
    dihitung dari data riil (bukan checklist statis), supaya operator baru tidak perlu
    hafal struktur menu untuk tahu harus mulai dari mana."""
    d = book_date.isoformat()
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


@login_required
def day_view(request):
    book_date = _parse_date(request.GET.get("d"))
    ctx = day_overview(book_date)
    ctx["summary"] = get_daily_summary(book_date)
    ctx["alarms"] = alarm_discrepancies(today=book_date)
    ctx["cumulative_open"] = cumulative_open_total()
    ctx["channels"] = Channel.choices
    day = ctx["day"]
    ctx["can_reopen"] = bool(
        day
        and day.locked
        and not Adjustment.objects.filter(book_date=book_date).exists()
        and not Discrepancy.objects.filter(origin_book_date=book_date, status__in=["RESOLVED", "WRITTEN_OFF"]).exists()
    )

    has_import_batch = ImportBatch.objects.filter(book_date=book_date).exists()
    engine_has_run = ctx["matches"].exists() or ctx["discrepancies"].exists()
    reversal_open_count = OtomaxEntry.objects.filter(
        book_date=book_date, category=OtomaxCategory.REVERSAL, match_status__in=_REVERSAL_OPEN_STATUSES
    ).count()
    ctx["today_steps"] = _today_steps(
        book_date, day, ctx["summary"], has_import_batch, engine_has_run, reversal_open_count
    )

    return render(request, "dashboard/day.html", ctx)
