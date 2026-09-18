"""Bulk-cleanup: batalkan match AGGREGATE (QRIS) yang selisih nominalnya di luar toleransi.

Dulu Pass 3 di _match_qris() auto-match berapa pun besar selisihnya, asal nama outlet
mirip. Sekarang dibatasi MATCH_AMOUNT_TOLERANCE (lihat apps/recon/engine.py) — tapi
Match yang sudah kadung dibuat sebelum perbaikan itu tidak otomatis ikut berubah.
Command ini mencari Match semacam itu dan membatalkannya lewat unpair_match(), supaya
kedua sisi (mutasi bank & baris Otomax) muncul lagi terpisah di antrean belum cocok
untuk ditinjau/dipasangkan manual.

Default DRY-RUN (cuma laporan, tidak mengubah apa pun). Pakai --apply untuk eksekusi.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from apps.core.enums import Channel, MatchStatus, MatchType, OtomaxCategory
from apps.ingest.models import OtomaxEntry
from apps.recon.models import Match
from apps.recon.resolve import unpair_match


class Command(BaseCommand):
    help = (
        "Batalkan match AGGREGATE (QRIS) yang selisih nominalnya melebihi "
        "MATCH_AMOUNT_TOLERANCE. Default dry-run — pakai --apply untuk eksekusi nyata."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Eksekusi nyata (default: dry-run).")
        parser.add_argument("--book-date", help="Batasi ke satu tanggal buku YYYY-MM-DD (opsional).")

    def handle(self, *args, **opts):
        tolerance = Decimal(str(settings.MATCH_AMOUNT_TOLERANCE))

        qs = (
            Match.objects.filter(
                channel=Channel.MERCHANT_BCA,
                match_type=MatchType.AGGREGATE,
                voided_at__isnull=True,
            )
            .select_related("bank_mutation", "otomax_entry")
            .order_by("book_date", "id")
        )

        if opts.get("book_date"):
            try:
                bd = date.fromisoformat(opts["book_date"])
            except ValueError as exc:
                raise CommandError("--book-date harus YYYY-MM-DD") from exc
            qs = qs.filter(book_date=bd)

        candidates = [m for m in qs if abs(m.amount_diff) > tolerance]

        if not candidates:
            self.stdout.write(
                self.style.SUCCESS("Tidak ada match AGGREGATE di luar toleransi. Tidak ada yang perlu dibersihkan.")
            )
            return

        apply_mode = bool(opts["apply"])
        mode_label = "APPLY (eksekusi nyata)" if apply_mode else "DRY-RUN (cuma laporan)"
        self.stdout.write(
            f"Mode: {mode_label} — toleransi Rp {tolerance:,.0f} — "
            f"ditemukan {len(candidates)} match AGGREGATE di luar toleransi.\n"
        )

        unpaired = skipped = 0
        for m in candidates:
            group = list(m.otomax_entries.all())
            reconstructed = False
            debug = ""
            if not group:
                group, debug = self._reconstruct_group(m)
                reconstructed = group is not None

            bank_desc = (m.bank_mutation.description_raw[:50] if m.bank_mutation else "-")
            reseller = m.otomax_entry.reseller_name_raw if m.otomax_entry else "-"
            label = (
                f"Match #{m.id} [{m.book_date}] {reseller} — "
                f"bank Rp{m.amount_bank:,.0f} vs otomax Rp{m.amount_otomax:,.0f} "
                f"(selisih Rp{m.amount_diff:,.0f})"
            )

            if group is None:
                self.stdout.write(
                    self.style.WARNING(
                        f"  LEWATI (grup Otomax tak bisa direkonstruksi ulang): {label} — {bank_desc}"
                    )
                )
                if debug:
                    self.stdout.write(f"      debug: {debug}")
                skipped += 1
                continue

            note = " [grup direkonstruksi ulang]" if reconstructed else ""
            action_word = "BATALKAN" if apply_mode else "AKAN DIBATALKAN"
            self.stdout.write(f"  {action_word}{note}: {label} — {bank_desc}")

            if apply_mode:
                with transaction.atomic():
                    if reconstructed:
                        m.otomax_entries.set(group)
                    unpair_match(m)
            unpaired += 1

        self.stdout.write("")
        if apply_mode:
            self.stdout.write(
                self.style.SUCCESS(f"Selesai: {unpaired} dibatalkan, {skipped} dilewati (perlu tinjauan manual).")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Dry-run selesai: {unpaired} AKAN dibatalkan, {skipped} akan dilewati. "
                    "Jalankan lagi dengan --apply untuk benar-benar mengeksekusi."
                )
            )

    def _reconstruct_group(self, m: Match) -> tuple[list[OtomaxEntry] | None, str]:
        """Rekonstruksi grup Otomax untuk Match AGGREGATE lama yang dibuat sebelum
        otomax_entries (M2M) ada. Cuma dipakai kalau jumlahnya PERSIS sama dengan
        amount_otomax yang tercatat di Match — kalau ambigu, dilewati (None).

        Untuk QRIS, entri Otomax-nya sering baru "ditembak" (dientri) H+1 dari tanggal
        transaksi bank aslinya — jadi tanggal buku Otomax & tanggal buku mutasi bank BISA
        beda satu hari secara konsisten, bukan cuma variasi acak. Coba tanggal TUNGGAL
        (bukan gabungan rentang) satu per satu — sama persis, lalu H+1, lalu H-1 — supaya
        tidak pernah mencampur total dua hari berbeda untuk reseller yang sama. Jendela
        lebar H-1..H+2 cuma jadi upaya terakhir kalau semua tanggal tunggal di atas gagal."""
        primary = m.otomax_entry
        if primary is None:
            return None, "otomax_entry utama kosong, tidak ada jangkar untuk rekonstruksi"

        base_filters = Q(category=OtomaxCategory.TOPUP_TARTUN, match_status=MatchStatus.MATCHED) & (
            Q(channel_hint=Channel.MERCHANT_BCA)
            | Q(description_raw__icontains="BULK")
            | Q(description_raw__icontains="TARTUN QR")
        )
        if primary.reseller_id:
            base_filters &= Q(reseller_id=primary.reseller_id)
        else:
            base_filters &= Q(reseller_id__isnull=True, reseller_name_raw=primary.reseller_name_raw)

        d_str = m.book_date.strftime("%d-%b-%Y").upper()
        d_str_short = m.book_date.strftime("%d-%b").upper()
        tgl_filter = Q(description_raw__icontains=d_str) | Q(description_raw__icontains=d_str_short)

        debug_parts = []

        single_day_attempts = [
            ("tanggal sama dgn bank", m.book_date),
            ("otomax H+1 dari bank (lazim utk QRIS)", m.book_date + timedelta(days=1)),
            ("otomax H-1 dari bank", m.book_date - timedelta(days=1)),
        ]
        for label, target_date in single_day_attempts:
            candidates = list(OtomaxEntry.objects.filter(base_filters & (Q(book_date=target_date) | tgl_filter)))
            total = sum((c.amount for c in candidates), Decimal("0.00"))
            debug_parts.append(f"{label} ({target_date}): {len(candidates)} baris, total Rp{total:,.0f}")
            if primary.id in {c.id for c in candidates} and total == m.amount_otomax:
                return candidates, ""

        window = (m.book_date - timedelta(days=1), m.book_date + timedelta(days=2))
        window_filter = Q(book_date__range=window) | tgl_filter
        candidates = list(OtomaxEntry.objects.filter(base_filters & window_filter))
        total = sum((c.amount for c in candidates), Decimal("0.00"))
        debug_parts.append(f"jendela H-1..H+2: {len(candidates)} baris, total Rp{total:,.0f}")
        if primary.id in {c.id for c in candidates} and total == m.amount_otomax:
            return candidates, ""

        debug_parts.append(f"target Rp{m.amount_otomax:,.0f}")
        return None, "; ".join(debug_parts)
