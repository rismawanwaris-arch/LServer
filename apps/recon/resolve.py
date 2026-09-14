from __future__ import annotations

from datetime import date

from django.db import transaction
from django.utils import timezone

from apps.core.enums import MatchStatus, MatchType
from apps.ingest.models import BankMutation, OtomaxEntry

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

@transaction.atomic
def manual_pair_transactions(
    bank_mutation: BankMutation,
    otomax_entry: OtomaxEntry,
    note: str = "",
    user=None,
) -> Match:
    """Pasangkan mutasi bank dan entri otomax secara manual."""
    bm = BankMutation.objects.select_for_update().get(pk=bank_mutation.pk)
    o = OtomaxEntry.objects.select_for_update().get(pk=otomax_entry.pk)

    # Validasi: pastikan belum dipasangkan dalam match aktif
    if Match.objects.filter(bank_mutation=bm, voided_at__isnull=True).exists():
        raise ValueError(f"Mutasi bank #{bm.id} sudah memiliki pasangan aktif.")
    if Match.objects.filter(otomax_entry=o, voided_at__isnull=True).exists():
        raise ValueError(f"Entri Otomax #{o.id} sudah memiliki pasangan aktif.")

    # Update match_status
    bm.match_status = MatchStatus.MANUAL
    bm.save(update_fields=["match_status", "updated_at"])

    o.match_status = MatchStatus.MANUAL
    o.save(update_fields=["match_status", "updated_at"])

    match_note = note.strip() or f"Pencocokan manual {bm.channel} vs Otomax ({o.reseller_name_raw})"
    match = Match.objects.create(
        book_date=bm.book_date,
        channel=bm.channel,
        bank_mutation=bm,
        otomax_entry=o,
        match_type=MatchType.MANUAL,
        amount_bank=bm.amount,
        amount_otomax=o.amount,
        confidence=100,
        note=match_note,
        matched_by=user,
    )

    # Selesaikan discrepancy terbuka jika ada
    for disc in Discrepancy.objects.filter(bank_mutation=bm, status="OPEN"):
        resolve_discrepancy(
            disc,
            match=match,
            resolution_type="DATA_FIX",
            reason=f"Cocok manual dengan Otomax #{o.id}: {match_note}",
            user=user,
        )

    for disc in Discrepancy.objects.filter(otomax_entry=o, status="OPEN"):
        resolve_discrepancy(
            disc,
            match=match,
            resolution_type="DATA_FIX",
            reason=f"Cocok manual dengan Mutasi Bank #{bm.id}: {match_note}",
            user=user,
        )

    day, _ = ReconDay.objects.select_for_update().get_or_create(book_date=bm.book_date)
    day.recompute_selisih()
    day.save(update_fields=["selisih_adjustments", "selisih_current", "updated_at"])

    return match


@transaction.atomic
def unpair_match(match: Match, user=None) -> None:
    """Batalkan pencocokan manual atau void match."""
    m = Match.objects.select_for_update().get(pk=match.pk)
    if m.voided_at:
        return

    m.voided_at = timezone.now()
    m.voided_by = user
    m.save(update_fields=["voided_at", "voided_by", "updated_at"])

    if m.bank_mutation:
        bm = BankMutation.objects.select_for_update().get(pk=m.bank_mutation_id)
        if not Match.objects.filter(bank_mutation=bm, voided_at__isnull=True).exists():
            bm.match_status = MatchStatus.UNMATCHED
            bm.tag_manual = ""
            bm.manual_note = ""
            bm.save(update_fields=["match_status", "tag_manual", "manual_note", "updated_at"])

    if m.otomax_entry:
        o = OtomaxEntry.objects.select_for_update().get(pk=m.otomax_entry_id)
        if not Match.objects.filter(otomax_entry=o, voided_at__isnull=True).exists():
            o.match_status = MatchStatus.PENDING_SETTLE
            o.save(update_fields=["match_status", "updated_at"])

    day, _ = ReconDay.objects.select_for_update().get_or_create(book_date=m.book_date)
    day.recompute_selisih()
    day.save(update_fields=["selisih_adjustments", "selisih_current", "updated_at"])


