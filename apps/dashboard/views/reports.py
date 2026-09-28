"""Halaman 6: Riwayat & Laporan — ringkasan rentang tanggal dan export Excel."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from apps.recon.reports import generate_excel_report, get_daily_rows, get_range_summary

from ._shared import _parse_date


@login_required
def reports_view(request):
    today = timezone.localdate()
    start_date = _parse_date(request.GET.get("start_date"), default=today - timedelta(days=6))
    end_date = _parse_date(request.GET.get("end_date"), default=today)

    if start_date > end_date:
        start_date, end_date = end_date, start_date

    summary = get_range_summary(start_date, end_date)

    # Rincian per tanggal: satu set query GROUP BY untuk seluruh rentang (dulu
    # get_daily_summary() dipanggil per tanggal -> 1.381 query untuk sebulan).
    daily_rows = get_daily_rows(start_date, end_date)

    return render(
        request,
        "dashboard/reports.html",
        {
            "start_date": start_date,
            "end_date": end_date,
            "summary": summary,
            "daily_rows": daily_rows,
        },
    )


@login_required
def reports_export_action(request):
    start_date = _parse_date(request.GET.get("start_date"))
    end_date = _parse_date(request.GET.get("end_date"))
    if "start_date" not in request.GET and "end_date" not in request.GET and "d" in request.GET:
        start_date = end_date = _parse_date(request.GET.get("d"))

    excel_bytes = generate_excel_report(start_date, end_date)
    response = HttpResponse(
        excel_bytes,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="rekonsiliasi_{start_date}_{end_date}.xlsx"'
    return response
