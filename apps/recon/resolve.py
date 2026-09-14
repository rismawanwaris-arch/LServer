from __future__ import annotations

from datetime import date

from django.db import transaction
from django.utils import timezone

from apps.core.enums import MatchStatus, MatchType
from apps.ingest.models import BankMutation

from .models import Adjustment, Discrepancy, Match, ReconDay


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
    return resolve_discrepancy(discrepancy, resolution_type="WRITE_OFF", reason=reason, user=user)


@transaction.atomic
def tag_manual_mutation(
    bank_mutation: BankMutation,
    tag: str,
    note: str = "",
    user=None,
) -> BankMutation:
    """Beri tag manual untuk mutasi bank selisih (admin/tarik tunai/setor tunai/revisi/lainnya).

    Status mutasi berubah jadi MATCHED_MANUAL (MANUAL).
    Jika ada Discrepancy yang mengacu ke mutasi ini, selesaikan dengan keterangan tag manual.
    """
    from decimal import Decimal

    bm = BankMutation.objects.select_for_update().get(pk=bank_mutation.pk)
    bm.tag_manual = tag
    bm.manual_note = note
    bm.match_status = MatchStatus.MANUAL
    bm.save(update_fields=["tag_manual", "manual_note", "match_status", "updated_at"])

    # Buat record Match tipe MANUAL jika belum ada
    Match.objects.get_or_create(
        book_date=bm.book_date,
        channel=bm.channel,
        bank_mutation=bm,
        defaults=dict(
            match_type=MatchType.MANUAL,
            amount_bank=bm.amount,
            amount_otomax=Decimal("0.00"),
            note=f"Tag manual: {tag}. {note}".strip(),
            matched_by=user,
        ),
    )

    for disc in Discrepancy.objects.filter(bank_mutation=bm, status="OPEN"):
        resolve_discrepancy(
            disc,
            resolution_type="DATA_FIX",
            reason=f"Tag manual: {tag}. {note}".strip(),
            user=user,
        )

    return bm
