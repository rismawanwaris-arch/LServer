"""Utilitas periode "bulan bisnis" dengan tanggal mulai siklus custom (mis. 29 -> 28),
bukan cuma kalender tanggal 1-31. Dipakai untuk laporan/alarm yang ingin dikelompokkan
per siklus tagihan reseller, bukan per bulan kalender. Tanpa Django/DB — murni fungsi
tanggal, gampang di-unit-test."""

from __future__ import annotations

import calendar
from datetime import date, timedelta


def _add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def _clamp_day(year: int, month: int, day: int) -> date:
    """mis. hari ke-29 di Februari tahun non-kabisat -> dipakukan ke tanggal 28."""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_day))


def business_month_range(today: date, start_day: int) -> tuple[date, date]:
    """Rentang tanggal (inklusif kedua ujung) siklus bulan berjalan yang mengandung
    `today`, dengan tanggal mulai siklus `start_day` (mis. start_day=29 -> siklus
    29 bulan lalu s/d 28 bulan ini). `start_day=1` sama persis dengan bulan kalender
    biasa (1 s/d akhir bulan) — jadi aman jadi default kalau belum dikonfigurasi.
    """
    start_day = max(1, min(start_day, 31))
    this_cycle_start = _clamp_day(today.year, today.month, start_day)
    if today >= this_cycle_start:
        period_start = this_cycle_start
        next_month = _add_months(today.replace(day=1), 1)
        period_end = _clamp_day(next_month.year, next_month.month, start_day) - timedelta(days=1)
    else:
        prev_month = _add_months(today.replace(day=1), -1)
        period_start = _clamp_day(prev_month.year, prev_month.month, start_day)
        period_end = this_cycle_start - timedelta(days=1)
    return period_start, period_end
