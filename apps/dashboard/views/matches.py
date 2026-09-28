"""Halaman 3: Hasil Rekonsiliasi — daftar Match yang sudah terbentuk untuk satu tanggal buku.

Daftar ini bisa berisi ratusan pasangan (mis. QRIS Merchant BCA per outlet), jadi
ditampilkan bertahap lewat infinite scroll HTMX (lihat _matches_rows.html) alih-alih
merender semua baris sekaligus -- baris berikutnya baru diminta ke server saat baris
"sentinel" di ujung tabel masuk ke viewport (hx-trigger="revealed")."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.enums import Channel
from apps.recon.models import Match
from apps.recon.resolve import approve_match

from ._shared import _parse_date

PAGE_SIZE = 50


@login_required
def matches_view(request):
    book_date = _parse_date(request.GET.get("d"))
    channel = request.GET.get("channel", "")
    query = request.GET.get("q", "").strip()
    tab = "review" if request.GET.get("tab") == "review" else "all"
    page_number = request.GET.get("page", 1)

    qs = Match.objects.filter(book_date=book_date, voided_at__isnull=True).select_related(
        "bank_mutation", "otomax_entry"
    ).prefetch_related("otomax_entries")
    review_count = qs.filter(needs_review=True).count()
    if tab == "review":
        qs = qs.filter(needs_review=True)
    if channel and channel in Channel.values:
        qs = qs.filter(channel=channel)
    if query:
        qs = (
            qs.filter(bank_mutation__description_raw__icontains=query)
            | qs.filter(otomax_entry__description_raw__icontains=query)
            | qs.filter(otomax_entry__reseller_name_raw__icontains=query)
        )

    matches = qs.order_by("-match_type", "-amount_bank")
    paginator = Paginator(matches, PAGE_SIZE)
    page_obj = paginator.get_page(page_number)

    context = {
        "book_date": book_date,
        "selected_channel": channel,
        "channels": Channel.choices,
        "query": query,
        "tab": tab,
        "review_count": review_count,
        "page_obj": page_obj,
        "total_count": paginator.count,
    }

    if request.headers.get("HX-Request"):
        return render(request, "dashboard/_matches_rows.html", context)

    return render(request, "dashboard/matches.html", context)


@login_required
@require_POST
def approve_match_action(request, pk: int):
    match = get_object_or_404(Match, pk=pk)
    next_url = request.POST.get("next_url") or request.META.get("HTTP_REFERER") or f"/matches/?d={match.book_date}"
    try:
        approve_match(match, user=request.user)
        messages.success(request, f"Usulan pencocokan #{match.id} disetujui dan sekarang final.")
    except ValueError as e:
        messages.error(request, str(e))
    return redirect(next_url)
