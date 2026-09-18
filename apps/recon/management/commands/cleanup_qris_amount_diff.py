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
            if not group:
                group = self._reconstruct_group(m)
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
                    self.style.WARNING(f"  LEWATI (grup Otomax tak bisa direkonstruksi ulang): {label} — {bank_desc}")
                )
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

    def _reconstruct_group(self, m: Match) -> list[OtomaxEntry] | None:
        """Rekonstruksi grup Otomax untuk Match AGGREGATE lama yang dibuat sebelum
        otomax_entries (M2M) ada. Cuma dipakai kalau jumlahnya PERSIS sama dengan
        amount_otomax yang tercatat di Match — kalau ambigu, dilewati (return None)."""
        primary = m.otomax_entry
        if primary is None:
            return None

        # Persis filter yang dipakai _match_qris() waktu grup ini pertama kali dibentuk
        # (engine.py _match_qris): channel QRIS/BULK + jendela tanggal H-1..H+2 ATAU teks
        # "TGL dd-Mon-yyyy" di keterangan. Tanpa filter channel ini, reseller yang juga
        # bertransaksi lewat BRI/BCA ikut kesedot dan jumlahnya tidak akan pernah pas.
        window = (m.book_date - timedelta(days=1), m.book_date + timedelta(days=2))
        d_str = m.book_date.strftime("%d-%b-%Y").upper()
        d_str_short = m.book_date.strftime("%d-%b").upper()
        candidates_qs = (
            OtomaxEntry.objects.filter(
                category=OtomaxCategory.TOPUP_TARTUN,
                match_status=MatchStatus.MATCHED,
            )
            .filter(
                Q(channel_hint=Channel.MERCHANT_BCA)
                | Q(description_raw__icontains="BULK")
                | Q(description_raw__icontains="TARTUN QR")
            )
            .filter(
                Q(book_date__range=window)
                | Q(description_raw__icontains=d_str)
                | Q(description_raw__icontains=d_str_short)
            )
        )
        if primary.reseller_id:
            candidates_qs = candidates_qs.filter(reseller_id=primary.reseller_id)
        else:
            candidates_qs = candidates_qs.filter(reseller_id__isnull=True, reseller_name_raw=primary.reseller_name_raw)

        candidates = list(candidates_qs)
        if primary.id not in {c.id for c in candidates}:
            return None
        total = sum((c.amount for c in candidates), Decimal("0.00"))
        if total != m.amount_otomax:
            return None
        return candidates
