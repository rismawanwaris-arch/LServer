from __future__ import annotations

from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.enums import Channel
from apps.ingest.models import BankMutation, ImportBatch, OtomaxEntry
from apps.ingest.services import ImportBlocked, import_file
from apps.recon.carry import carry_forward
from apps.recon.close import DayHasDownstream, DayLocked, DayNotReady, close_day, reopen_day
from apps.recon.engine import run_match
from apps.recon.models import Adjustment, Discrepancy, ReconDay
from apps.recon.purge import (
    DayIsClosed,
    preview_all,
    purge_all_transactions,
    purge_day,
)
from apps.recon.resolve import write_off
from apps.recon.selectors import alarm_discrepancies, cumulative_open_total, day_overview

staff_only = user_passes_test(lambda u: u.is_superuser)


def _parse_date(raw: str | None) -> date:
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return timezone.localdate()


@login_required
def day_view(request):
    book_date = _parse_date(request.GET.get("d"))
    ctx = day_overview(book_date)
    ctx["alarms"] = alarm_discrepancies()
    ctx["cumulative_open"] = cumulative_open_total()
    ctx["channels"] = Channel.choices
    day = ctx["day"]
    ctx["can_reopen"] = bool(
        day
        and day.locked
        and not Adjustment.objects.filter(book_date=book_date).exists()
        and not Discrepancy.objects.filter(
            origin_book_date=book_date, status__in=["RESOLVED", "WRITTEN_OFF"]
        ).exists()
    )
    return render(request, "dashboard/day.html", ctx)


@login_required
@require_POST
def upload(request):
    book_date = _parse_date(request.POST.get("book_date"))
    channel = request.POST.get("channel")
    upload_file = request.FILES.get("file")
    if not upload_file or channel not in Channel.values:
        messages.error(request, "Pilih channel dan file.")
        return redirect(f"/?d={book_date}")
    try:
        batch = import_file(
            channel=channel,
            text=upload_file.read().decode("utf-8", errors="replace"),
            book_date=book_date,
            filename=upload_file.name,
            user=request.user,
        )
    except ImportBlocked as exc:
        messages.error(request, str(exc))
        return redirect(f"/?d={book_date}")
    messages.success(
        request,
        f"{batch.channel}: {batch.row_count} baris ({batch.quarantined_count} dikarantina).",
    )
    return redirect(f"/?d={book_date}")


@login_required
@require_POST
def run_engine(request):
    book_date = _parse_date(request.POST.get("book_date"))
    stats = run_match(book_date)
    resolved = carry_forward(book_date, user=request.user)
    messages.success(
        request,
        f"Cocok: {stats.matched} · discrepancy baru: {stats.discrepancies} · "
        f"selisih lama ditutup: {resolved}.",
    )
    return redirect(f"/?d={book_date}")


@login_required
@require_POST
def close_view(request):
    book_date = _parse_date(request.POST.get("book_date"))
    force = request.POST.get("force") == "1"
    try:
        day = close_day(book_date, user=request.user, force=force)
    except (DayLocked, DayNotReady) as exc:
        messages.error(request, str(exc))
        return redirect(f"/?d={book_date}")
    messages.success(request, f"Buku {day.book_date} ditutup. Selisih Rp {day.selisih_initial:,.0f}.")
    return redirect(f"/?d={book_date}")


@login_required
@require_POST
def reopen_view(request):
    book_date = _parse_date(request.POST.get("book_date"))
    try:
        reopen_day(book_date, user=request.user)
    except DayHasDownstream as exc:
        messages.error(request, str(exc))
        return redirect(f"/?d={book_date}")
    messages.success(request, f"Buku {book_date} dibuka kembali. Jalankan pencocokan lalu tutup lagi.")
    return redirect(f"/?d={book_date}")


@login_required
@require_POST
def resolve_view(request, pk: int):
    disc = Discrepancy.objects.get(pk=pk)
    reason = request.POST.get("reason", "")
    if request.POST.get("action") == "write_off":
        write_off(disc, reason=reason or "hapus buku manual", user=request.user)
        messages.success(request, f"{disc.code} dihapusbukukan.")
    else:
        messages.info(request, "Gunakan tombol Jalankan Pencocokan untuk mencari pasangan otomatis.")
    return redirect(f"/?d={request.POST.get('book_date')}")


# --- Hapus data --------------------------------------------------------------

@login_required
@staff_only
def data_admin(request):
    from apps.recon.models import Match

    dates = set(ImportBatch.objects.values_list("book_date", flat=True)) | set(
        ReconDay.objects.values_list("book_date", flat=True)
    )
    rows = []
    for bd in sorted(dates, reverse=True):
        day = ReconDay.objects.filter(book_date=bd).first()
        rows.append({
            "book_date": bd,
            "locked": bool(day and day.locked),
            "status": day.get_status_display() if day else "belum ada",
            "bank": BankMutation.objects.filter(book_date=bd).count(),
            "otomax": OtomaxEntry.objects.filter(book_date=bd).count(),
            "match": Match.objects.filter(book_date=bd).count(),
            "discrepancy": Discrepancy.objects.filter(origin_book_date=bd).count(),
        })
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
        f"Semua data transaksi dihapus ({sum(counts.values())} baris). "
        "Reseller & mapping merchant tetap.",
    )
    return redirect("data-admin")
