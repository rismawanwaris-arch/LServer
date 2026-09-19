"""Admin data: ringkasan per tanggal buku dan penghapusan data transaksi (purge). Khusus staff."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.ingest.models import BankMutation, ImportBatch, OtomaxEntry
from apps.recon.models import Discrepancy, Match, ReconDay
from apps.recon.purge import DayIsClosed, preview_all, purge_all_transactions, purge_day

from ._shared import _parse_date, _sync_inconsistent_batches, staff_only


@login_required
@staff_only
def data_admin(request):
    _sync_inconsistent_batches()

    dates = (
        set(ImportBatch.objects.values_list("book_date", flat=True))
        | set(ReconDay.objects.values_list("book_date", flat=True))
        | set(BankMutation.objects.values_list("book_date", flat=True))
        | set(OtomaxEntry.objects.values_list("book_date", flat=True))
    )
    rows = []
    for bd in sorted(dates, reverse=True):
        bank_count = BankMutation.objects.filter(book_date=bd).count()
        otomax_count = OtomaxEntry.objects.filter(book_date=bd).count()
        match_count = Match.objects.filter(book_date=bd).count()
        disc_count = Discrepancy.objects.filter(origin_book_date=bd).count()
        batch_count = ImportBatch.objects.filter(book_date=bd).count()
        day = ReconDay.objects.filter(book_date=bd).first()

        # Lewati tanggal yang benar-benar kosong (0 di semua metrik dan tidak dikunci)
        if (
            bank_count == 0
            and otomax_count == 0
            and match_count == 0
            and disc_count == 0
            and batch_count == 0
        ):
            if day and not day.locked:
                day.delete()
            continue

        rows.append(
            {
                "book_date": bd,
                "locked": bool(day and day.locked),
                "status": day.get_status_display() if day else "belum ada",
                "bank": bank_count,
                "otomax": otomax_count,
                "match": match_count,
                "discrepancy": disc_count,
            }
        )
    return render(request, "dashboard/data.html", {"rows": rows, "totals": preview_all()})


@login_required
@staff_only
@require_POST
def purge_day_view(request):
    book_date = _parse_date(request.POST.get("book_date"))
    include_closed = request.POST.get("include_closed") == "1"
    try:
        counts = purge_day(book_date, include_closed=include_closed)
    except DayIsClosed as exc:
        messages.error(request, str(exc))
        return redirect("data-admin")
    total = sum(counts.values())
    messages.success(request, f"Data tanggal {book_date} dihapus ({total} baris).")
    return redirect("data-admin")


@login_required
@staff_only
@require_POST
def purge_all_view(request):
    if request.POST.get("confirm") != "HAPUS SEMUA":
        messages.error(request, 'Ketik persis "HAPUS SEMUA" untuk konfirmasi.')
        return redirect("data-admin")
    counts = purge_all_transactions(include_history=request.POST.get("include_history") == "1")
    messages.success(
        request,
        f"Semua data transaksi dihapus ({sum(counts.values())} baris). " "Reseller & mapping merchant tetap.",
    )
    return redirect("data-admin")
