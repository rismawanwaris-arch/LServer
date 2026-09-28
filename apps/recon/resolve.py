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


def retire_leftover_discrepancies(qs, match: Match, user, on_date: date) -> None:
    """Selisih BANK_ONLY/OTOMAX_ONLY yang usang begitu barisnya dapat pasangan (mis.
    OTOMAX_ONLY yang dibuat leftovers untuk entri Otomax 3 Sep sesaat sebelum carry_forward
    memasangkannya ke mutasi bank 30 Agu) -- tanpa ini ia tetap OPEN jadi selisih "hantu"
    di Daftar Selisih. Hari yang belum ditutup: hapus saja (sama seperti _persist_match).
    Hari yang sudah ditutup: selisihnya bagian dari snapshot beku, jadi diselesaikan lewat
    Adjustment, tidak dihapus."""
    for disc in qs.filter(
        status=DiscrepancyStatus.OPEN, kind__in=[DiscrepancyKind.BANK_ONLY, DiscrepancyKind.OTOMAX_ONLY]
    ):
        if ReconDay.objects.filter(book_date=disc.origin_book_date, locked=True).exists():
            resolve_discrepancy(disc, match=match, resolution_type="LATE_MATCH", user=user, on_date=on_date)
        else:
            disc.delete()


@transaction.atomic
def approve_match(match: Match, user=None) -> Match:
    """Setujui usulan pencocokan mesin -> jadi final. Efek yang sengaja DITUNDA selama
    masih usulan (karena tidak bisa dibatalkan kalau usulannya ternyata ditolak) dijalankan
    di sini: selisih lama dari hari sebelumnya diselesaikan lewat Adjustment (append-only),
    selisih sisa yang usang dibersihkan, dan mapping merchant QRIS dipelajari."""
    from .engine.qris_match import learn_merchant_map

    m = Match.objects.select_for_update().get(pk=match.pk)
    if m.voided_at:
        raise ValueError(f"Pencocokan #{m.id} sudah dibatalkan, tidak bisa disetujui.")
    if not m.needs_review:
        return m

    m.needs_review = False
    m.reviewed_by = user
    m.reviewed_at = timezone.now()
    m.save(update_fields=["needs_review", "reviewed_by", "reviewed_at", "updated_at"])

    otomax_ids = set(m.otomax_entries.values_list("id", flat=True))
    if m.otomax_entry_id:
        otomax_ids.add(m.otomax_entry_id)
    leftovers = Discrepancy.objects.filter(
        status=DiscrepancyStatus.OPEN, kind__in=[DiscrepancyKind.BANK_ONLY, DiscrepancyKind.OTOMAX_ONLY]
    ).filter(Q(bank_mutation_id=m.bank_mutation_id) | Q(otomax_entry_id__in=otomax_ids))
    for disc in leftovers.filter(origin_book_date__lt=m.book_date):
        resolve_discrepancy(disc, match=m, resolution_type="LATE_MATCH", user=user, on_date=m.book_date)
    retire_leftover_discrepancies(leftovers.filter(origin_book_date__gte=m.book_date), m, user, m.book_date)

    if m.channel == Channel.MERCHANT_BCA and m.bank_mutation:
        learn_merchant_map(m.bank_mutation, m.otomax_entry)
    return m


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


def _active_matches_for_bank(bm):
    """Match aktif yang melibatkan mutasi ini -- sebagai mutasi utama maupun anggota
    gabungan beberapa mutasi (bank_mutations)."""
    return Match.objects.filter(Q(bank_mutation=bm) | Q(bank_mutations=bm), voided_at__isnull=True).distinct()


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

    # Validasi: pastikan entri otomax belum dipasangkan dalam match aktif
    if Match.objects.filter(Q(otomax_entry=o) | Q(otomax_entries=o), voided_at__isnull=True).exists():
        raise ValueError(f"Entri Otomax #{o.id} sudah memiliki pasangan aktif.")

    # Cek apakah mutasi bank ini sudah memiliki match aktif
    existing_id = _active_matches_for_bank(bm).values_list("id", flat=True).first()
    existing_match = Match.objects.select_for_update().get(pk=existing_id) if existing_id else None

    if existing_match:
        # KASUS: Menggabungkan transaksi Otomax ke mutasi bank yang sudah cocok tapi masih ada selisih.
        # Selisih & ReconDay selalu dicatat di mutasi UTAMA pasangan itu (bisa beda dari `bm`
        # kalau bm cuma anggota gabungan beberapa mutasi).
        bm = existing_match.bank_mutation
        o.match_status = MatchStatus.MANUAL
        o.save(update_fields=["match_status", "updated_at"])

        # Pastikan otomax_entry awal juga masuk ke relasi ManyToMany otomax_entries
        orig_id = existing_match.otomax_entry_id
        if orig_id and not existing_match.otomax_entries.filter(pk=orig_id).exists():
            existing_match.otomax_entries.add(existing_match.otomax_entry)
        existing_match.otomax_entries.add(o)

        existing_match.amount_otomax += o.amount
        existing_match.amount_diff = (existing_match.amount_bank or ZERO) - existing_match.amount_otomax
        existing_match.match_type = MatchType.AGGREGATE
        match_note = note.strip()
        if match_note:
            existing_match.note = f"{existing_match.note} | {match_note}".strip(" |")
        existing_match.save()

        # Bersihkan Discrepancy OPEN milik entri Otomax ini
        Discrepancy.objects.filter(otomax_entry=o, status=DiscrepancyStatus.OPEN).delete()

        # Update atau hapus Discrepancy AMOUNT_DIFF mutasi bank
        diff = existing_match.amount_diff
        existing_disc = Discrepancy.objects.filter(
            bank_mutation=bm,
            kind=DiscrepancyKind.AMOUNT_DIFF,
            status=DiscrepancyStatus.OPEN,
        ).first()

        if diff == ZERO:
            if existing_disc:
                existing_disc.delete()
        else:
            if existing_disc:
                existing_disc.amount = diff
                existing_disc.note = (
                    f"Selisih nominal gabungan: Bank Rp {existing_match.amount_bank:,.2f} vs "
                    f"Otomax Rp {existing_match.amount_otomax:,.2f}"
                )
                existing_disc.save(update_fields=["amount", "note", "updated_at"])
            else:
                _make_discrepancy(
                    bm.book_date,
                    bm.channel,
                    DiscrepancyKind.AMOUNT_DIFF,
                    amount=diff,
                    bank=bm,
                    otomax=o,
                    note=(
                        f"Selisih nominal gabungan: Bank Rp {existing_match.amount_bank:,.2f} vs "
                        f"Otomax Rp {existing_match.amount_otomax:,.2f}"
                    ),
                )

        day, _ = ReconDay.objects.select_for_update().get_or_create(book_date=bm.book_date)
        day.recompute_selisih()
        day.save(update_fields=["selisih_adjustments", "selisih_current", "updated_at"])

        return existing_match

    # Update match_status mutasi bank baru
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
    _settle_manual_pair([bm], [o], match, match_note, user)
    return match


@transaction.atomic
def manual_pair_many(
    bank_mutation: BankMutation,
    otomax_entries: list[OtomaxEntry],
    note: str = "",
    user=None,
) -> Match:
    """Pasangkan SATU mutasi bank dengan BEBERAPA entri Otomax sekaligus -- untuk koreksi
    operator yang ditembak sebagai selisih, bukan dibalik lalu dientri ulang (mis. +3.540.000
    lalu -90.000 untuk transfer 3.450.000). Dipilih operator, jadi langsung final."""
    ids = sorted({o.pk for o in otomax_entries})
    if len(ids) < 2:
        raise ValueError("Pilih minimal 2 entri Otomax untuk digabungkan.")

    bm = BankMutation.objects.select_for_update().get(pk=bank_mutation.pk)
    entries = list(OtomaxEntry.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
    if len(entries) != len(ids):
        raise ValueError("Sebagian entri Otomax yang dipilih tidak ditemukan.")

    if _active_matches_for_bank(bm).exists():
        raise ValueError(
            f"Mutasi bank #{bm.id} sudah punya pasangan aktif. Batalkan dulu pasangannya, atau "
            "tambahkan entri lewat daftar 'Masih Selisih' di Pending Settle."
        )
    open_statuses = {MatchStatus.UNMATCHED, MatchStatus.PENDING_SETTLE}
    for o in entries:
        already_paired = Match.objects.filter(Q(otomax_entry=o) | Q(otomax_entries=o), voided_at__isnull=True).exists()
        if o.match_status not in open_statuses or already_paired:
            raise ValueError(
                f"Entri Otomax #{o.id} ({o.reseller_name_raw}) sudah tidak terbuka atau sudah punya pasangan aktif."
            )

    bm.match_status = MatchStatus.MANUAL
    bm.save(update_fields=["match_status", "updated_at"])
    for o in entries:
        o.match_status = MatchStatus.MANUAL
        o.save(update_fields=["match_status", "updated_at"])

    primary = max(entries, key=lambda o: abs(o.amount))
    match_note = note.strip() or (
        f"Pencocokan manual gabungan {len(entries)} entri Otomax vs {bm.channel} "
        f"({', '.join(sorted({o.reseller_name_raw for o in entries}))})"
    )
    match = Match.objects.create(
        book_date=bm.book_date,
        channel=bm.channel,
        bank_mutation=bm,
        otomax_entry=primary,
        match_type=MatchType.MANUAL,
        amount_bank=bm.amount,
        amount_otomax=sum((o.amount for o in entries), ZERO),
        confidence=100,
        note=match_note,
        matched_by=user,
    )
    match.otomax_entries.set(entries)
    _settle_manual_pair([bm], entries, match, match_note, user)
    return match


@transaction.atomic
def manual_pair_many_banks(
    bank_mutations: list[BankMutation],
    otomax_entry: OtomaxEntry,
    note: str = "",
    user=None,
) -> Match:
    """Pasangkan BEBERAPA mutasi bank dengan SATU entri Otomax -- mis. satu Tartun QR Bulk
    Rp 3.329.000 yang menutup settlement dua outlet QRIS (2.488.000 + 841.000), boleh beda
    tanggal. Dipilih operator, jadi langsung final."""
    ids = sorted({b.pk for b in bank_mutations})
    if len(ids) < 2:
        raise ValueError("Pilih minimal 2 mutasi bank untuk digabungkan.")

    banks = list(BankMutation.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
    if len(banks) != len(ids):
        raise ValueError("Sebagian mutasi bank yang dipilih tidak ditemukan.")
    o = OtomaxEntry.objects.select_for_update().get(pk=otomax_entry.pk)

    open_statuses = {MatchStatus.UNMATCHED, MatchStatus.PENDING_SETTLE}
    if o.match_status not in open_statuses or Match.objects.filter(
        Q(otomax_entry=o) | Q(otomax_entries=o), voided_at__isnull=True
    ).exists():
        raise ValueError(f"Entri Otomax #{o.id} sudah tidak terbuka atau sudah punya pasangan aktif.")
    for b in banks:
        if b.match_status != MatchStatus.UNMATCHED or _active_matches_for_bank(b).exists():
            raise ValueError(
                f"Mutasi bank #{b.id} (Rp {b.amount:,.0f}) sudah tidak terbuka atau sudah punya pasangan aktif."
            )

    for b in banks:
        b.match_status = MatchStatus.MANUAL
        b.save(update_fields=["match_status", "updated_at"])
    o.match_status = MatchStatus.MANUAL
    o.save(update_fields=["match_status", "updated_at"])

    primary = max(banks, key=lambda b: abs(b.amount))
    match_note = note.strip() or (
        f"Pencocokan manual gabungan {len(banks)} mutasi bank vs Otomax ({o.reseller_name_raw})"
    )
    match = Match.objects.create(
        book_date=primary.book_date,
        channel=primary.channel,
        bank_mutation=primary,
        otomax_entry=o,
        match_type=MatchType.MANUAL,
        amount_bank=sum((b.amount for b in banks), ZERO),
        amount_otomax=o.amount,
        confidence=100,
        note=match_note,
        matched_by=user,
    )
    match.bank_mutations.set(banks)
    _settle_manual_pair(banks, [o], match, match_note, user)
    return match


def _settle_manual_pair(
    banks: list[BankMutation], entries: list[OtomaxEntry], match: Match, match_note: str, user
) -> None:
    """Rapikan Discrepancy setelah pencocokan manual baru (1:1 maupun gabungan dua arah)."""
    diff = match.amount_diff  # = amount_bank - amount_otomax, dihitung otomatis di Match.save()
    otomax_refs = ", ".join(f"#{o.id}" for o in entries)
    bank_refs = ", ".join(f"#{b.id}" for b in banks)
    primary_bank = match.bank_mutation

    if diff == ZERO:
        # Nominal pas sama -- kedua sisi benar-benar sudah sepenuhnya terjelaskan,
        # selesaikan discrepancy leftover asal (BANK_ONLY/OTOMAX_ONLY) seperti biasa.
        for disc in Discrepancy.objects.filter(bank_mutation__in=banks, status=DiscrepancyStatus.OPEN):
            resolve_discrepancy(
                disc,
                match=match,
                resolution_type="DATA_FIX",
                reason=f"Cocok manual dengan Otomax {otomax_refs}: {match_note}",
                user=user,
            )
        for disc in Discrepancy.objects.filter(otomax_entry__in=entries, status=DiscrepancyStatus.OPEN):
            resolve_discrepancy(
                disc,
                match=match,
                resolution_type="DATA_FIX",
                reason=f"Cocok manual dengan Mutasi Bank {bank_refs}: {match_note}",
                user=user,
            )
    else:
        # Nominal beda -- JANGAN tutup penuh & adjust seakan sudah sepenuhnya
        # terjelaskan (itu akan menghapus selisih riilnya dari total selisih hari itu,
        # lihat diskusi soal kasus ini). Discrepancy leftover lama (BANK_ONLY/OTOMAX_ONLY)
        # sudah usang begitu kedua sisi dapat pasangan -- ganti dengan SATU discrepancy
        # AMOUNT_DIFF baru senilai sisa selisih riil, tetap OPEN, supaya kelihatan di
        # Daftar Selisih & Dashboard sampai ada yang menyelesaikan/write-off terpisah.
        Discrepancy.objects.filter(bank_mutation__in=banks, status=DiscrepancyStatus.OPEN).delete()
        Discrepancy.objects.filter(otomax_entry__in=entries, status=DiscrepancyStatus.OPEN).delete()
        diff_note = (
            f"Selisih nominal pencocokan manual: Bank Rp {match.amount_bank:,.2f} vs "
            f"Otomax Rp {match.amount_otomax:,.2f} ({match_note})"
        )
        _make_discrepancy(
            primary_bank.book_date,
            primary_bank.channel,
            DiscrepancyKind.AMOUNT_DIFF,
            amount=diff,
            bank=primary_bank,
            otomax=match.otomax_entry,
            note=diff_note,
        )

    for book_date in {b.book_date for b in banks}:
        day, _ = ReconDay.objects.select_for_update().get_or_create(book_date=book_date)
        day.recompute_selisih()
        day.save(update_fields=["selisih_adjustments", "selisih_current", "updated_at"])


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
    bank_ids = set(m.bank_mutations.values_list("id", flat=True))
    if m.bank_mutation_id:
        bank_ids.add(m.bank_mutation_id)

    m.voided_at = timezone.now()
    m.voided_by = user
    m.save(update_fields=["voided_at", "voided_by", "updated_at"])

    for bm in BankMutation.objects.select_for_update().filter(pk__in=bank_ids):
        if not _active_matches_for_bank(bm).exists():
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
