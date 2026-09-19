"""Halaman 3: Hasil Rekonsiliasi — daftar Match yang sudah terbentuk untuk satu tanggal buku."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.core.enums import Channel
from apps.recon.models import Match

from ._shared import _parse_date


@login_required
def matches_view(request):
    book_date = _parse_date(request.GET.get("d"))
    channel = request.GET.get("channel", "")
    query = request.GET.get("q", "").strip()

    qs = Match.objects.filter(book_date=book_date, voided_at__isnull=True).select_related(
        "bank_mutation", "otomax_entry"
    )
    if channel and channel in Channel.values:
        qs = qs.filter(channel=channel)
    if query:
        qs = (
            qs.filter(bank_mutation__description_raw__icontains=query)
            | qs.filter(otomax_entry__description_raw__icontains=query)
            | qs.filter(otomax_entry__reseller_name_raw__icontains=query)
        )

    matches = qs.order_by("-match_type", "-amount_bank")

    return render(
        request,
        "dashboard/matches.html",
        {
            "book_date": book_date,
            "selected_channel": channel,
            "channels": Channel.choices,
            "query": query,
            "matches": matches,
            "total_count": matches.count(),
        },
    )
