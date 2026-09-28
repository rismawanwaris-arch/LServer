"""Halaman 1: Dashboard — ringkasan harian & alarm selisih yang belum ditangani."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.shortcuts import render

from apps.core.enums import Channel, MatchStatus, OtomaxCategory
from apps.ingest.models import OtomaxEntry
from apps.recon.models import Adjustment, Discrepancy, Match
from apps.recon.reports import get_daily_summary
from apps.recon.selectors import alarm_discrepancies, cumulative_open_total, day_overview

from ._shared import _parse_date


@login_required
def day_view(request):
    book_date = _parse_date(request.GET.get("d"))
    ctx = day_overview(book_date)
    summary = ctx["summary"] = get_daily_summary(book_date)
    ctx["matched_total_count"] = summary["matched_auto_count"] + summary["matched_manual_count"]
    ctx["matched_total_amount"] = summary["matched_auto_amount"] + summary["matched_manual_amount"]
    # Kartu "Selisih Otomax" = isi persis halaman Pending Settle yang ditautkannya (semua
    # entri terbuka selain potongan admin), bukan cuma kategori tarik tunai.
    pending = (
        OtomaxEntry.objects.filter(
            book_date=book_date, match_status__in=[MatchStatus.UNMATCHED, MatchStatus.PENDING_SETTLE]
        )
        .exclude(category=OtomaxCategory.ADMIN)
        .aggregate(n=Count("id"), total=Sum("amount"))
    )
    ctx["pending_settle_count"] = pending["n"]
    ctx["pending_settle_amount"] = pending["total"] or Decimal("0.00")
    ctx["alarms"] = alarm_discrepancies(today=book_date)
    ctx["cumulative_open"] = cumulative_open_total()
    ctx["channels"] = Channel.choices
    ctx["proposal_count"] = Match.objects.filter(book_date=book_date, needs_review=True, voided_at__isnull=True).count()
    day = ctx["day"]
    ctx["can_reopen"] = bool(
        day
        and day.locked
        and not Adjustment.objects.filter(book_date=book_date).exists()
        and not Discrepancy.objects.filter(origin_book_date=book_date, status__in=["RESOLVED", "WRITTEN_OFF"]).exists()
    )

    return render(request, "dashboard/day.html", ctx)
