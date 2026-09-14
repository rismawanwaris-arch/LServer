from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from apps.core.enums import DiscrepancyStatus

from .close import compute_totals
from .models import Discrepancy, Match, ReconDay


def day_overview(book_date: date) -> dict:
    day = ReconDay.objects.filter(book_date=book_date).first()
    totals = (
        compute_totals(book_date)
        if not day or not day.locked
        else {
            "bri": day.total_in_bri,
            "bca": day.total_in_bca,
            "merchant_bca": day.total_in_merchant_bca,
            "mandiri": day.total_in_mandiri,
            "bank": day.total_in_bank,
            "otomax": day.total_out_otomax,
            "selisih": day.selisih_initial,
        }
    )
    return {
        "book_date": book_date,
        "day": day,
        "totals": totals,
        "matches": Match.objects.filter(book_date=book_date, voided_at__isnull=True)
        .select_related("bank_mutation", "otomax_entry")
        .order_by("-match_type"),
        "discrepancies": Discrepancy.objects.filter(origin_book_date=book_date)
        .select_related("bank_mutation", "otomax_entry")
        .order_by("status", "channel"),
        "carried_open": open_discrepancies_before(book_date),
    }


def open_discrepancies_before(book_date: date):
    return Discrepancy.objects.filter(status=DiscrepancyStatus.OPEN, origin_book_date__lt=book_date).order_by(
        "origin_book_date"
    )


def alarm_discrepancies(today: date | None = None):
    today = today or timezone.localdate()
    cutoff = today - timedelta(days=settings.DISCREPANCY_ALARM_DAYS)
    return Discrepancy.objects.filter(status=DiscrepancyStatus.OPEN, origin_book_date__lte=cutoff).order_by(
        "origin_book_date"
    )


def cumulative_open_total():
    return Discrepancy.objects.filter(status=DiscrepancyStatus.OPEN).aggregate(s=Sum("amount"))["s"] or 0
