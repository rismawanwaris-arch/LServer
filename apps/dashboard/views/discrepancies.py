"""Halaman Daftar Selisih — daftar transaksional Discrepancy dengan aksi hapus buku.

Dua mode:
- Per tanggal (`?d=YYYY-MM-DD`, dipakai menu sidebar): seperti Review Manual / Pending
  Settle, supaya operator menyelesaikan selisih satu tanggal buku demi satu. Deretan
  "tanggal yang masih ada selisih terbuka" membantu loncat ke tanggal berikutnya.
- Semua tanggal (tanpa `d`, dipakai banner SLA Alarm di Dashboard): semua status OPEN
  lintas tanggal diurut dari yang paling lama, supaya yang lewat SLA langsung kelihatan.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render

from apps.core.enums import Channel, DiscrepancyStatus
from apps.recon.models import Discrepancy
from apps.recon.reports import generate_excel_discrepancies

from ._shared import _parse_date

ZERO = Decimal("0.00")
# Baris dimuat bertahap (infinite scroll) dan panel detail diambil saat dibuka -- dulu
# semua baris + semua detail tersembunyi dirender sekaligus (211 selisih = 2 MB HTML,
# 17 ribu elemen DOM).
PAGE_SIZE = 50


def _filter_discrepancies(get_params) -> tuple[object, dict]:
    """Helper untuk menyaring queryset Discrepancy berdasarkan parameter GET."""
    status = get_params.get("status", DiscrepancyStatus.OPEN)
    book_date = _parse_date(get_params["d"]) if get_params.get("d") else None
    start_date_raw = get_params.get("start_date")
    end_date_raw = get_params.get("end_date")
    channel = get_params.get("channel", "")
    q = get_params.get("q", "").strip()

    qs = Discrepancy.objects.select_related(
        "bank_mutation",
        "otomax_entry",
        "otomax_entry__reseller",
    )
    if book_date:
        qs = qs.filter(origin_book_date=book_date)
    else:
        if start_date_raw:
            qs = qs.filter(origin_book_date__gte=_parse_date(start_date_raw))
        if end_date_raw:
            qs = qs.filter(origin_book_date__lte=_parse_date(end_date_raw))
    if channel:
        qs = qs.filter(channel=channel)
    if q:
        qs = qs.filter(
            Q(code__icontains=q)
            | Q(bank_mutation__outlet_name__icontains=q)
            | Q(bank_mutation__description_raw__icontains=q)
            | Q(bank_mutation__ref_normalized__icontains=q)
            | Q(bank_mutation__ref_core__icontains=q)
            | Q(bank_mutation__external_ref__icontains=q)
            | Q(otomax_entry__reseller_name_raw__icontains=q)
            | Q(otomax_entry__reseller__name__icontains=q)
            | Q(otomax_entry__reseller__code__icontains=q)
            | Q(otomax_entry__description_raw__icontains=q)
            | Q(otomax_entry__ref_normalized__icontains=q)
            | Q(otomax_entry__ref_core__icontains=q)
        )

    filters = {
        "status": status,
        "book_date": book_date,
        "start_date_raw": start_date_raw,
        "end_date_raw": end_date_raw,
        "channel": channel,
        "q": q,
    }
    return qs, filters


@login_required
def discrepancy_list_view(request):
    qs, f = _filter_discrepancies(request.GET)
    status = f["status"]
    book_date = f["book_date"]
    start_date_raw = f["start_date_raw"]
    end_date_raw = f["end_date_raw"]
    channel = f["channel"]
    q = f["q"]

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
    paginator = Paginator(
        qs.select_related("bank_mutation", "otomax_entry").order_by("origin_book_date", "code"), PAGE_SIZE
    )
    is_more = bool(request.headers.get("HX-Request") and request.GET.get("page"))
    page_obj = paginator.get_page(request.GET.get("page") if is_more else 1)
    items = list(page_obj.object_list)
    # URL daftar tanpa nomor halaman: tujuan kembali setelah "hapus buku", dan dasar
    # URL halaman berikutnya.
    params = request.GET.copy()
    params.pop("page", None)
    list_url = request.path + (f"?{params.urlencode()}" if params else "")
    if is_more:
        return render(request, "dashboard/_discrepancy_rows.html", {"page_obj": page_obj, "list_url": list_url})

    open_dates = list(
        Discrepancy.objects.filter(status=DiscrepancyStatus.OPEN)
        .values("origin_book_date")
        .annotate(n=Count("id"))
        .order_by("origin_book_date")
    )

    # Parameter query string untuk link Export Excel (menyertakan semua filter saat ini)
    export_params = request.GET.copy()
    export_params.pop("page", None)
    export_query = export_params.urlencode()

    return render(
        request,
        "dashboard/discrepancies.html",
        {
            "book_date": book_date,
            "open_dates": open_dates,
            "items": items,
            "page_obj": page_obj,
            "total_count": paginator.count,
            "list_url": list_url,
            "export_query": export_query,
            "selected_status": status,
            "start_date": start_date_raw or "",
            "end_date": end_date_raw or "",
            "selected_channel": channel,
            "q": q,
            "channels": Channel.choices,
            "statuses": DiscrepancyStatus.choices,
            "open_count": open_count,
            "open_amount": open_amount,
            "resolved_count": resolved_count,
            "written_off_count": written_off_count,
        },
    )


@login_required
def discrepancy_detail_view(request, pk: int):
    """Panel detail satu selisih (bank vs Otomax berdampingan), dimuat lewat HTMX saat
    barisnya dibuka."""
    d = get_object_or_404(
        Discrepancy.objects.select_related("bank_mutation", "otomax_entry", "otomax_entry__reseller"), pk=pk
    )
    next_url = request.GET.get("next", "")
    if not next_url.startswith("/selisih/"):  # cuma boleh kembali ke daftar ini
        next_url = "/selisih/"
    return render(request, "dashboard/_discrepancy_detail.html", {"d": d, "next_url": next_url})


@login_required
def discrepancy_export_action(request):
    """Ekspor daftar selisih hasil filter saat ini ke file Excel (.xlsx)."""
    qs, f = _filter_discrepancies(request.GET)
    status = f["status"]
    if status and status != "ALL":
        qs = qs.filter(status=status)

    qs = qs.order_by("origin_book_date", "code")

    notes = []
    if f["book_date"]:
        notes.append(f"Tanggal Buku: {f['book_date'].strftime('%d %b %Y')}")
    elif f["start_date_raw"] or f["end_date_raw"]:
        s = f["start_date_raw"] or "Awal"
        e = f["end_date_raw"] or "Sekarang"
        notes.append(f"Periode: {s} s/d {e}")
    else:
        notes.append("Semua Tanggal")

    if status and status != "ALL":
        notes.append(f"Status: {dict(DiscrepancyStatus.choices).get(status, status)}")
    if f["channel"]:
        notes.append(f"Channel: {f['channel']}")
    if f["q"]:
        notes.append(f"Pencarian: {f['q']}")

    title_note = " · ".join(notes)
    excel_bytes = generate_excel_discrepancies(qs, title_note=title_note)

    date_label = f["book_date"].strftime("%Y%m%d") if f["book_date"] else "semua_tanggal"
    filename = f"daftar_selisih_{date_label}.xlsx"

    response = HttpResponse(
        excel_bytes,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
