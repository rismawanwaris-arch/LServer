"""Halaman 1: Dashboard — ringkasan harian & alarm selisih yang belum ditangani."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.core.enums import Channel
from apps.recon.models import Adjustment, Discrepancy
from apps.recon.reports import get_daily_summary
from apps.recon.selectors import alarm_discrepancies, cumulative_open_total, day_overview

from ._shared import _parse_date


@login_required
def day_view(request):
    book_date = _parse_date(request.GET.get("d"))
    ctx = day_overview(book_date)
    ctx["summary"] = get_daily_summary(book_date)
    ctx["alarms"] = alarm_discrepancies(today=book_date)
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
