from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from apps.core.enums import DiscrepancyStatus
from apps.core.models import AppSettings
from apps.core.period import business_month_range

from .models import Discrepancy, ReconDay


def day_overview(book_date: date) -> dict:
    """Data dasar halaman Dashboard. Angka ringkasan datang dari get_daily_summary();
    di sini cuma hal yang benar-benar ditampilkan (dulu ikut menghitung compute_totals()
    -- 5 query -- yang hasilnya tidak dipakai template mana pun)."""
    return {
        "book_date": book_date,
        "day": ReconDay.objects.filter(book_date=book_date).first(),
        # COUNT, bukan memuat semua baris selisih lama cuma untuk dihitung jumlahnya.
        "carried_open_count": open_discrepancies_before(book_date).count(),
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


def alarm_discrepancies_this_period(today: date | None = None):
    """Sama seperti alarm_discrepancies(), dibatasi ke siklus bulan bisnis berjalan
    (AppSettings.business_month_start_day, diatur lewat halaman Pengaturan) — dipakai
    saat expand "Lihat 1 bulan"."""
    today = today or timezone.localdate()
    cutoff = today - timedelta(days=settings.DISCREPANCY_ALARM_DAYS)
    period_start, _period_end = business_month_range(today, AppSettings.load().business_month_start_day)
    return Discrepancy.objects.filter(
        status=DiscrepancyStatus.OPEN,
        origin_book_date__lte=cutoff,
        origin_book_date__gte=period_start,
    ).order_by("origin_book_date")


def cumulative_open_total():
    return Discrepancy.objects.filter(status=DiscrepancyStatus.OPEN).aggregate(s=Sum("amount"))["s"] or 0
