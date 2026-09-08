from __future__ import annotations

from datetime import date

from django.db import transaction
from django.utils import timezone

from .models import Adjustment, Discrepancy, ReconDay


class DiscrepancyClosed(Exception):
    pass


@transaction.atomic
def resolve_discrepancy(
    discrepancy: Discrepancy,
    *,
    match=None,
    resolution_type: str = "LATE_MATCH",
    reason: str = "",
    user=None,
    on_date: date | None = None,
) -> Adjustment:
    """Tutup satu discrepancy dan posting Adjustment bertanggal ke hari asalnya.

    Tidak menyentuh baris tanggal asal. recon_day[asal] dihitung ulang.
    """
    disc = Discrepancy.objects.select_for_update().get(pk=discrepancy.pk)
    if disc.status != "OPEN":
        raise DiscrepancyClosed(f"{disc.code} sudah {disc.status}")

    today = on_date or timezone.localdate()
    disc.status = "RESOLVED" if resolution_type != "WRITE_OFF" else "WRITTEN_OFF"
    disc.resolved_book_date = today
    disc.resolution_type = resolution_type
    disc.resolution_match = match
    disc.resolved_by = user
    disc.resolved_at = timezone.now()
    if reason:
        disc.note = f"{disc.note}\n{reason}".strip()
    disc.save()

    adj = Adjustment(
        book_date=disc.origin_book_date,
        discrepancy=disc,
        amount=disc.amount,
        reason=reason or f"{resolution_type} pada {today}",
        created_by=user,
    )
    adj.save()

    day, _ = ReconDay.objects.select_for_update().get_or_create(book_date=disc.origin_book_date)
    day.recompute_selisih()
    day.save(update_fields=["selisih_adjustments", "selisih_current", "updated_at"])
    return adj


def write_off(discrepancy: Discrepancy, *, reason: str, user=None) -> Adjustment:
    return resolve_discrepancy(
        discrepancy, resolution_type="WRITE_OFF", reason=reason, user=user
    )
