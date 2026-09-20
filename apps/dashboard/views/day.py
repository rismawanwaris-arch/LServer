"""Halaman 1: Dashboard — ringkasan harian & alarm selisih yang belum ditangani."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.core.enums import Channel
from apps.core.models import AppSettings
from apps.core.period import business_month_range
from apps.recon.models import Adjustment, Discrepancy
from apps.recon.reports import get_daily_summary
from apps.recon.selectors import (
    alarm_discrepancies,
    alarm_discrepancies_this_period,
    cumulative_open_total,
    day_overview,
)

from ._shared import _parse_date


@login_required
def day_view(request):
    book_date = _parse_date(request.GET.get("d"))
    ctx = day_overview(book_date)
    ctx["summary"] = get_daily_summary(book_date)
    ctx["alarms"] = alarm_discrepancies(today=book_date)
    ctx["alarms_this_period"] = alarm_discrepancies_this_period(today=book_date)
    ctx["business_month_range"] = business_month_range(book_date, AppSettings.load().business_month_start_day)
    ctx["cumulative_open"] = cumulative_open_total()
    ctx["channels"] = Channel.choices
    day = ctx["day"]
    ctx["can_reopen"] = bool(
        day
        and day.locked
        and not Adjustment.objects.filter(book_date=book_date).exists()
        and not Discrepancy.objects.filter(origin_book_date=book_date, status__in=["RESOLVED", "WRITTEN_OFF"]).exists()
    )
    return render(request, "dashboard/day.html", ctx)
