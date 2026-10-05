"""Halaman 1: Dashboard — ringkasan harian & alarm selisih yang belum ditangani."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.shortcuts import render

from apps.core.enums import BANK_CHANNELS, Channel, MatchStatus, OtomaxCategory
from apps.ingest.models import BankMutation, OtomaxEntry
from apps.recon.models import Adjustment, Discrepancy, Match
from apps.recon.reports import get_daily_summary, get_reconciliation_bridge
from apps.recon.selectors import alarm_discrepancies, cumulative_open_total, day_overview

from ._shared import _parse_date


@login_required
def day_view(request):
    book_date = _parse_date(request.GET.get("d"))
    ctx = day_overview(book_date)
    summary = ctx["summary"] = get_daily_summary(book_date)
    ctx["bridge"] = get_reconciliation_bridge(book_date)
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
    ctx["bank_count"] = BankMutation.objects.filter(book_date=book_date).count()
    ctx["otomax_count"] = OtomaxEntry.objects.filter(book_date=book_date).count()
    ctx["bank_rows"] = _bank_rows(summary)
    ctx["matched_rate"] = _pct(ctx["matched_total_amount"], summary["total_bank"])
    # Alur kerja harian (bilah "Aksi Rekonsiliasi"): langkah mana yang sudah selesai.
    ctx["engine_ran"] = bool(
        ctx["matched_total_count"] or Discrepancy.objects.filter(origin_book_date=book_date).exists()
    )
    ctx["remaining_count"] = (
        summary["unmatched_bank_count"]
        + ctx["pending_settle_count"]
        + summary["amount_diff_count"]
        + ctx["proposal_count"]
    )
    ctx["can_reopen"] = bool(
        day
        and day.locked
        and not Adjustment.objects.filter(book_date=book_date).exists()
        and not Discrepancy.objects.filter(origin_book_date=book_date, status__in=["RESOLVED", "WRITTEN_OFF"]).exists()
    )

    return render(request, "dashboard/day.html", ctx)


# Warna tiap bank di bilah porsi "Uang masuk bank" & tabel rekap.
_BANK_COLORS = {
    Channel.BRI: "#2563eb",
    Channel.BCA: "#0ea5e9",
    Channel.MERCHANT_BCA: "#14b8a6",
    Channel.MANDIRI: "#f59e0b",
}


def _pct(part, whole) -> int | None:
    if not whole:
        return None
    return max(0, min(100, round(part / whole * 100)))


def _bank_rows(summary) -> list[dict]:
    rows = []
    for ch in BANK_CHANNELS:
        d = summary["per_bank"].get(ch)
        if d is None:
            continue
        matched = d["matched_auto"] + d["matched_manual"]
        rows.append(
            {
                **d,
                "channel": ch,
                "label": Channel(ch).label,
                "color": _BANK_COLORS[ch],
                "share": _pct(d["total"], summary["total_bank"]) or 0,
                "rate": _pct(matched, d["total"]),
            }
        )
    return rows
