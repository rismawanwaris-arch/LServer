from __future__ import annotations

from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.enums import Channel
from apps.ingest.services import ImportBlocked, import_file
from apps.recon.carry import carry_forward
from apps.recon.close import DayLocked, DayNotReady, close_day
from apps.recon.engine import run_match
from apps.recon.models import Discrepancy
from apps.recon.resolve import write_off
from apps.recon.selectors import alarm_discrepancies, cumulative_open_total, day_overview


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
    messages.success(request, f"Buku {day.book_date} ditutup. Selisih {day.selisih_initial}.")
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
