"""Kontrol mesin rekonsiliasi & buku: jalankan pencocokan, tutup/buka buku, hapus-buku selisih."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from apps.recon.carry import carry_forward
from apps.recon.close import DayHasDownstream, DayLocked, DayNotReady, close_day, reopen_day
from apps.recon.engine import run_match
from apps.recon.models import Discrepancy
from apps.recon.resolve import write_off

from ._shared import _parse_date


@login_required
@require_POST
def run_engine(request):
    book_date = _parse_date(request.POST.get("book_date"))
    stats = run_match(book_date)
    resolved = carry_forward(book_date, user=request.user)
    netted_msg = f" · revisi di-net: {stats.netted}" if stats.netted else ""
    messages.success(
        request,
        f"Cocok: {stats.matched} · discrepancy baru: {stats.discrepancies} · "
        f"selisih lama ditutup: {resolved}{netted_msg}.",
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
    fallback_url = f"/?d={request.POST.get('book_date')}"
    next_url = request.POST.get("next_url") or request.META.get("HTTP_REFERER") or fallback_url
    return redirect(next_url)
