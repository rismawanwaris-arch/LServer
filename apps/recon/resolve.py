from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.enums import Channel, DiscrepancyKind, DiscrepancyStatus, MatchStatus, MatchType
from apps.ingest.models import BankMutation, OtomaxEntry

from .engine.helpers import _make_discrepancy
from .models import Adjustment, Discrepancy, Match, ReconDay

ZERO = Decimal("0.00")


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
def tag_manual_otomax(
    otomax_entry: OtomaxEntry,
    tag: str,
    note: str = "",
    user=None,
) -> OtomaxEntry:
    """Beri tag manual untuk transaksi Otomax selisih (revisi, retur, setor tunai langsung, dll).

    Status transaksi berubah jadi MATCHED_MANUAL (MANUAL).
    Jika ada Discrepancy yang mengacu ke transaksi ini, selesaikan dengan keterangan tag manual.
    """
    o = OtomaxEntry.objects.select_for_update().get(pk=otomax_entry.pk)
    o.match_status = MatchStatus.MANUAL
    o.save(update_fields=["match_status", "updated_at"])

    tag_note = f"Tag manual Otomax: {tag}. {note}".strip()
    match, _ = Match.objects.get_or_create(
        book_date=o.book_date,
        channel=o.channel_hint or Channel.OTOMAX,
        otomax_entry=o,
        defaults=dict(
            match_type=MatchType.MANUAL,
            amount_bank=Decimal("0.00"),
            amount_otomax=o.amount,
            note=tag_note,
            matched_by=user,
        ),
    )

    for disc in Discrepancy.objects.filter(otomax_entry=o, status="OPEN"):
        resolve_discrepancy(
            disc,
            match=match,
            resolution_type="DATA_FIX",
            reason=tag_note,
            user=user,
        )

    day, _ = ReconDay.objects.select_for_update().get_or_create(book_date=o.book_date)
    day.recompute_selisih()
    day.save(update_fields=["selisih_adjustments", "selisih_current", "updated_at"])

    return o


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

    diff = match.amount_diff  # = amount_bank - amount_otomax, dihitung otomatis di Match.save()

    if diff == ZERO:
        # Nominal pas sama -- kedua sisi benar-benar sudah sepenuhnya terjelaskan,
        # selesaikan discrepancy leftover asal (BANK_ONLY/OTOMAX_ONLY) seperti biasa.
        for disc in Discrepancy.objects.filter(bank_mutation=bm, status=DiscrepancyStatus.OPEN):
            resolve_discrepancy(
                disc,
                match=match,
                resolution_type="DATA_FIX",
                reason=f"Cocok manual dengan Otomax #{o.id}: {match_note}",
                user=user,
            )
        for disc in Discrepancy.objects.filter(otomax_entry=o, status=DiscrepancyStatus.OPEN):
            resolve_discrepancy(
                disc,
                match=match,
                resolution_type="DATA_FIX",
                reason=f"Cocok manual dengan Mutasi Bank #{bm.id}: {match_note}",
                user=user,
            )
    else:
        # Nominal beda -- JANGAN tutup penuh & adjust seakan sudah sepenuhnya
        # terjelaskan (itu akan menghapus selisih riilnya dari total selisih hari itu,
        # lihat diskusi soal kasus ini). Discrepancy leftover lama (BANK_ONLY/OTOMAX_ONLY)
        # sudah usang begitu kedua sisi dapat pasangan -- ganti dengan SATU discrepancy
        # AMOUNT_DIFF baru senilai sisa selisih riil, tetap OPEN, supaya kelihatan di
        # Daftar Selisih & Dashboard sampai ada yang menyelesaikan/write-off terpisah.
        Discrepancy.objects.filter(bank_mutation=bm, status=DiscrepancyStatus.OPEN).delete()
        Discrepancy.objects.filter(otomax_entry=o, status=DiscrepancyStatus.OPEN).delete()
        diff_note = (
            f"Selisih nominal pencocokan manual: Bank Rp {bm.amount:,.2f} vs "
            f"Otomax Rp {o.amount:,.2f} ({match_note})"
        )
        _make_discrepancy(
            bm.book_date,
            bm.channel,
            DiscrepancyKind.AMOUNT_DIFF,
            amount=diff,
            bank=bm,
            otomax=o,
            note=diff_note,
        )

    day, _ = ReconDay.objects.select_for_update().get_or_create(book_date=bm.book_date)
    day.recompute_selisih()
    day.save(update_fields=["selisih_adjustments", "selisih_current", "updated_at"])

    return match


@transaction.atomic
def manual_net_reversal(
    rev: OtomaxEntry,
    original: OtomaxEntry,
    note: str = "",
    user=None,
) -> None:
    """Netralkan manual sepasang entri Otomax (REV vs entri yang dibatalkannya) yang gagal
    dinetralkan otomatis oleh reversal_netting — mis. nama reseller beda teks persis, atau
    ref_core/ref_normalized-nya tidak identik. Efeknya sama seperti netting otomatis:
    keduanya jadi IGNORED dan saling menunjuk net_pair, TANPA membuat Match (tidak ada uang
    bank yang bergerak, murni koreksi internal Otomax).
    """
    r = OtomaxEntry.objects.select_for_update().get(pk=rev.pk)
    o = OtomaxEntry.objects.select_for_update().get(pk=original.pk)

    if r.pk == o.pk:
        raise ValueError("Tidak bisa menetralkan entri dengan dirinya sendiri.")
    if r.amount != -o.amount:
        raise ValueError("Nominal kedua entri harus persis saling meniadakan (berlawanan tanda, sama besar).")
    open_statuses = {MatchStatus.UNMATCHED, MatchStatus.PENDING_SETTLE}
    if r.match_status not in open_statuses or o.match_status not in open_statuses:
        raise ValueError(
            "Salah satu entri sudah tidak berstatus terbuka (sudah MATCHED/MANUAL/IGNORED). "
            "Batalkan pencocokan/tag-nya dulu sebelum menetralkan manual."
        )

    who = str(user) if user else "sistem"
    tag_note = f"Netting manual oleh {who}: {note}".strip().rstrip(":") if note else f"Netting manual oleh {who}"

    r.match_status = MatchStatus.IGNORED
    r.net_pair = o
    r.note = tag_note
    r.save(update_fields=["match_status", "net_pair", "note", "updated_at"])

    o.match_status = MatchStatus.IGNORED
    o.net_pair = r
    o.note = tag_note
    o.save(update_fields=["match_status", "net_pair", "note", "updated_at"])


@transaction.atomic
def unpair_match(match: Match, user=None) -> None:
    """Batalkan pencocokan manual/otomatis — termasuk match AGGREGATE (QRIS multi-baris)."""
    m = Match.objects.select_for_update().get(pk=match.pk)
    if m.voided_at:
        return

    # Kumpulkan semua id Otomax yang terlibat SEBELUM di-void, supaya "masih ada match aktif
    # lain?" di bawah tidak ikut menghitung match ini sendiri.
    otomax_ids = set(m.otomax_entries.values_list("id", flat=True))
    if m.otomax_entry_id:
        otomax_ids.add(m.otomax_entry_id)

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

    for o in OtomaxEntry.objects.select_for_update().filter(pk__in=otomax_ids):
        still_active = Match.objects.filter(Q(otomax_entry=o) | Q(otomax_entries=o), voided_at__isnull=True).exists()
        if not still_active:
            o.match_status = MatchStatus.PENDING_SETTLE
            o.save(update_fields=["match_status", "updated_at"])

    # Discrepancy AMOUNT_DIFF yang dibuat khusus untuk pasangan match ini (lihat
    # manual_pair_transactions / qris_match Pass 3) jadi usang begitu match dibatalkan --
    # kalau masih OPEN (belum diselesaikan/write-off manual), hapus supaya tidak nyangkut
    # dan menghalangi leftovers.py bikin BANK_ONLY/OTOMAX_ONLY baru untuk bm/o ini.
    if m.bank_mutation_id and m.otomax_entry_id:
        Discrepancy.objects.filter(
            bank_mutation_id=m.bank_mutation_id,
            otomax_entry_id=m.otomax_entry_id,
            status=DiscrepancyStatus.OPEN,
        ).delete()

    day, _ = ReconDay.objects.select_for_update().get_or_create(book_date=m.book_date)
    day.recompute_selisih()
    day.save(update_fields=["selisih_adjustments", "selisih_current", "updated_at"])
