from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.enums import Channel, DayStatus, DiscrepancyStatus, MatchStatus, OtomaxCategory
from apps.ingest.models import BankMutation, DebitIgnored, OtomaxEntry

from .models import Adjustment, Discrepancy, Match, ReconDay

ZERO = Decimal("0.00")


class DayLocked(Exception):
    pass


class DayNotReady(Exception):
    pass


class DayHasDownstream(Exception):
    pass


def compute_totals(book_date: date) -> dict:
    def bank_sum(channel):
        return BankMutation.objects.filter(book_date=book_date, channel=channel).aggregate(s=Sum("amount"))["s"] or ZERO

    totals = {
        "bri": bank_sum(Channel.BRI),
        "bca": bank_sum(Channel.BCA),
        "merchant_bca": bank_sum(Channel.MERCHANT_BCA),
        "mandiri": bank_sum(Channel.MANDIRI),
    }
    totals["bank"] = sum(totals.values(), ZERO)
    totals["otomax"] = (
        OtomaxEntry.objects.filter(book_date=book_date, category=OtomaxCategory.TOPUP_TARTUN).aggregate(
            s=Sum("amount")
        )["s"]
        or ZERO
    )
    totals["selisih"] = totals["bank"] - totals["otomax"]
    return totals


def build_snapshot(book_date: date) -> dict:
    totals = compute_totals(book_date)
    per_reseller = list(
        OtomaxEntry.objects.filter(book_date=book_date, category=OtomaxCategory.TOPUP_TARTUN)
        .values("reseller__code")
        .annotate(total=Sum("amount"))
        .order_by("reseller__code")
    )
    discrepancies = list(
        Discrepancy.objects.filter(origin_book_date=book_date).values("code", "kind", "channel", "amount", "status")
    )
    return {
        "book_date": book_date.isoformat(),
        "totals": {k: str(v) for k, v in totals.items()},
        "per_reseller": [{"reseller": r["reseller__code"], "total": str(r["total"])} for r in per_reseller],
        "discrepancies": [{**d, "amount": str(d["amount"])} for d in discrepancies],
        "debit_ignored_total": str(
            DebitIgnored.objects.filter(book_date=book_date).aggregate(s=Sum("amount"))["s"] or ZERO
        ),
        "frozen_at": timezone.now().isoformat(),
    }


@transaction.atomic
def close_day(book_date: date, *, user=None, force: bool = False) -> ReconDay:
    day, _ = ReconDay.objects.select_for_update().get_or_create(book_date=book_date)
    if day.locked:
        raise DayLocked(f"{book_date} sudah ditutup.")

    fuzzy_pending = Match.objects.filter(
        book_date=book_date, match_type="AUTO_FUZZY", voided_at__isnull=True, note=""
    ).exists()
    unmapped = Discrepancy.objects.filter(
        origin_book_date=book_date, note__contains="belum di-mapping", status="OPEN"
    ).exists()
    if not force and (fuzzy_pending or unmapped):
        raise DayNotReady(
            "Masih ada AUTO_FUZZY belum di-review atau merchant QRIS belum di-mapping. "
            "Pakai force=True untuk tetap menutup."
        )

    totals = compute_totals(book_date)
    matched = Match.objects.filter(book_date=book_date, voided_at__isnull=True).count()
    unmatched = (
        BankMutation.objects.filter(book_date=book_date, match_status=MatchStatus.UNMATCHED).count()
        + OtomaxEntry.objects.filter(
            book_date=book_date, category=OtomaxCategory.TOPUP_TARTUN, match_status=MatchStatus.UNMATCHED
        ).count()
    )

    day.total_in_bri = totals["bri"]
    day.total_in_bca = totals["bca"]
    day.total_in_merchant_bca = totals["merchant_bca"]
    day.total_in_mandiri = totals["mandiri"]
    day.total_in_bank = totals["bank"]
    day.total_out_otomax = totals["otomax"]
    day.selisih_initial = totals["selisih"]
    day.matched_count = matched
    day.unmatched_count = unmatched
    day.snapshot = build_snapshot(book_date)
    day.status = DayStatus.CLOSED
    day.locked = True
    day.closed_by = user
    day.closed_at = timezone.now()
    day.recompute_selisih()
    day.save()
    return day


@transaction.atomic
def reopen_day(book_date: date, *, user=None, force: bool = False) -> ReconDay:
    """Buka kembali hari yang keburu ditutup — hanya kalau belum ada dampak lanjutan.

    Ditolak kalau sudah ada Adjustment bertanggal hari itu, atau ada Discrepancy
    asal hari itu yang sudah RESOLVED/WRITTEN_OFF di hari lain. Pakai force untuk
    menembus (berisiko desync — hanya untuk perbaikan darurat).
    """
    day = ReconDay.objects.select_for_update().get(book_date=book_date)
    if not day.locked:
        return day
    if not force:
        if Adjustment.objects.filter(book_date=book_date).exists():
            raise DayHasDownstream(f"{book_date} sudah menerima penyesuaian bertanggal — tidak bisa dibuka.")
        resolved = Discrepancy.objects.filter(
            origin_book_date=book_date,
            status__in=[DiscrepancyStatus.RESOLVED, DiscrepancyStatus.WRITTEN_OFF],
        ).exists()
        if resolved:
            raise DayHasDownstream(
                f"Ada selisih asal {book_date} yang sudah diselesaikan di hari lain — tidak bisa dibuka."
            )
    day.status = DayStatus.IN_REVIEW
    day.locked = False
    day.closed_by = None
    day.closed_at = None
    day.save()
    return day
