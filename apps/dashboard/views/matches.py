"""Halaman 3: Hasil Rekonsiliasi — daftar Match yang sudah terbentuk untuk satu tanggal buku.

Daftar ini bisa berisi ratusan pasangan (mis. QRIS Merchant BCA per outlet), jadi
ditampilkan bertahap lewat infinite scroll HTMX (lihat _matches_rows.html) alih-alih
merender semua baris sekaligus -- baris berikutnya baru diminta ke server saat baris
"sentinel" di ujung tabel masuk ke viewport (hx-trigger="revealed")."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render

from apps.core.enums import Channel
from apps.recon.models import Match

from ._shared import _parse_date

PAGE_SIZE = 50


@login_required
def matches_view(request):
    book_date = _parse_date(request.GET.get("d"))
    channel = request.GET.get("channel", "")
    query = request.GET.get("q", "").strip()
    page_number = request.GET.get("page", 1)

    qs = Match.objects.filter(book_date=book_date, voided_at__isnull=True).select_related(
        "bank_mutation", "otomax_entry"
    ).prefetch_related("otomax_entries")
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
        "page_obj": page_obj,
        "total_count": paginator.count,
    }

    if request.headers.get("HX-Request"):
        return render(request, "dashboard/_matches_rows.html", context)

    return render(request, "dashboard/matches.html", context)
