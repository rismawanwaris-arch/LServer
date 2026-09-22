"""Halaman Daftar Selisih — daftar transaksional Discrepancy dengan aksi hapus buku.

Dipindahkan keluar dari Dashboard (lihat commit terkait): Dashboard seharusnya cuma
ringkasan/KPI satu layar, bukan tabel transaksi mentah. Berbeda dari Review Manual /
Pending Settle (antrean per-hari), halaman ini default menampilkan SEMUA tanggal dengan
status OPEN diurut dari yang paling lama — supaya selisih yang lewat SLA (carried-forward
dari hari-hari sebelumnya) langsung kelihatan di atas tanpa perlu gonta-ganti tanggal.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import render

from apps.core.enums import Channel, DiscrepancyStatus
from apps.recon.models import Discrepancy

from ._shared import _parse_date

ZERO = Decimal("0.00")


@login_required
def discrepancy_list_view(request):
    status = request.GET.get("status", DiscrepancyStatus.OPEN)
    start_date_raw = request.GET.get("start_date")
    end_date_raw = request.GET.get("end_date")
    channel = request.GET.get("channel", "")

    qs = Discrepancy.objects.select_related("bank_mutation", "otomax_entry")
    if start_date_raw:
        qs = qs.filter(origin_book_date__gte=_parse_date(start_date_raw))
    if end_date_raw:
        qs = qs.filter(origin_book_date__lte=_parse_date(end_date_raw))
    if channel:
        qs = qs.filter(channel=channel)

    # Ringkasan KPI dihitung dari filter tanggal/channel yang sama TAPI tanpa filter
    # status, supaya kartu di atas selalu menunjukkan gambaran lengkap (Open/Resolved/
    # Write-off) apa pun status yang sedang dipilih di tabel bawah.
    open_qs = qs.filter(status=DiscrepancyStatus.OPEN)
    open_count = open_qs.count()
    open_amount = open_qs.aggregate(s=Sum("amount"))["s"] or ZERO
    resolved_count = qs.filter(status=DiscrepancyStatus.RESOLVED).count()
    written_off_count = qs.filter(status=DiscrepancyStatus.WRITTEN_OFF).count()

    if status and status != "ALL":
        qs = qs.filter(status=status)
    items = list(qs.order_by("origin_book_date", "code")[:1000])

    return render(
        request,
        "dashboard/discrepancies.html",
        {
            "items": items,
            "selected_status": status,
            "start_date": start_date_raw or "",
            "end_date": end_date_raw or "",
            "selected_channel": channel,
            "channels": Channel.choices,
            "statuses": DiscrepancyStatus.choices,
            "open_count": open_count,
            "open_amount": open_amount,
            "resolved_count": resolved_count,
            "written_off_count": written_off_count,
        },
    )
