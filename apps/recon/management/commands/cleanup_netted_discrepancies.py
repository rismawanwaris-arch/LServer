"""Bulk-cleanup: tutup selisih OTOMAX_ONLY "hantu" milik entri Otomax yang sudah dinetralkan.

Dulu netting (otomatis lintas hari maupun "Netralkan manual") cuma mengubah status kedua
entri jadi IGNORED tanpa menutup selisih OTOMAX_ONLY yang sudah kadung tercatat -- mis.
topup tgl 15 sudah masuk Daftar Selisih, REV-nya baru dinetralkan tgl 16. Sekarang
settle_netted_discrepancies() menutupnya saat netting terjadi, tapi data lama tidak
otomatis ikut berubah. Command ini menyusulkannya dengan aturan yang sama: hari yang
belum ditutup -> selisihnya dihapus, hari yang sudah ditutup -> lewat Adjustment.

Default DRY-RUN (cuma laporan, tidak mengubah apa pun). Pakai --apply untuk eksekusi.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.core.enums import DiscrepancyKind, DiscrepancyStatus, MatchStatus
from apps.recon.models import Discrepancy, ReconDay
from apps.recon.resolve import settle_netted_discrepancies


class Command(BaseCommand):
    help = (
        "Tutup selisih OTOMAX_ONLY yang masih OPEN padahal entri Otomax-nya sudah dinetralkan. "
        "Default dry-run — pakai --apply untuk eksekusi nyata."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Eksekusi nyata (default: dry-run).")

    def handle(self, *args, **opts):
        ghosts = list(
            Discrepancy.objects.filter(
                status=DiscrepancyStatus.OPEN,
                kind=DiscrepancyKind.OTOMAX_ONLY,
                otomax_entry__match_status=MatchStatus.IGNORED,
                otomax_entry__net_pair__isnull=False,
            )
            .select_related("otomax_entry", "otomax_entry__net_pair")
            .order_by("origin_book_date", "code")
        )
        if not ghosts:
            self.stdout.write(
                self.style.SUCCESS("Tidak ada selisih hantu dari netting. Tidak ada yang perlu dibersihkan.")
            )
            return

        apply_mode = bool(opts["apply"])
        mode_label = "APPLY (eksekusi nyata)" if apply_mode else "DRY-RUN (cuma laporan)"
        self.stdout.write(f"Mode: {mode_label} — ditemukan {len(ghosts)} selisih hantu.\n")

        locked_days = set(
            ReconDay.objects.filter(book_date__in={d.origin_book_date for d in ghosts}, locked=True).values_list(
                "book_date", flat=True
            )
        )
        for d in ghosts:
            o, pair = d.otomax_entry, d.otomax_entry.net_pair
            how = "ADJUSTMENT (hari sudah ditutup)" if d.origin_book_date in locked_days else "HAPUS"
            self.stdout.write(
                f"  {how}: {d.code} Rp{d.amount:,.0f} — #{o.id} {o.reseller_name_raw} Rp{o.amount:,.0f} "
                f"dinetralkan dgn #{pair.id} {pair.reseller_name_raw} Rp{pair.amount:,.0f} [{pair.book_date}]"
            )

        if not apply_mode:
            self.stdout.write("")
            self.stdout.write(
                self.style.SUCCESS(
                    f"Dry-run selesai: {len(ghosts)} AKAN ditutup. "
                    "Jalankan lagi dengan --apply untuk benar-benar mengeksekusi."
                )
            )
            return

        with transaction.atomic():
            entries = {d.otomax_entry for d in ghosts}
            settle_netted_discrepancies(
                list(entries), reason="Pembersihan selisih hantu: entri sudah dinetralkan dengan lawannya"
            )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Selesai: {len(ghosts)} selisih hantu ditutup."))
