"""Management command untuk mencocokkan ulang (re-match) data transaksi dengan mesin rekonsiliasi terbaru.

Berguna ketika:
- Mesin rekonsiliasi diperbarui (logika baru, penanganan selisih baru, perbaikan bug).
- File impor sudah ada di database, tapi hasil pencocokan lama masih menggunakan logika lama.

Command ini me-reset match otomatis (tanpa menyentuh file upload mentah dan tanpa
merusak match manual pengguna), lalu menjalankan kembali run_match() dan carry_forward().
"""

from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.enums import DayStatus, MatchStatus, MatchType
from apps.ingest.models import BankMutation, ImportBatch, OtomaxEntry
from apps.recon.carry import carry_forward
from apps.recon.close import reopen_day
from apps.recon.engine import run_match
from apps.recon.models import Discrepancy, Match, ReconDay


class Command(BaseCommand):
    help = "Reset match otomatis dan jalankan ulang mesin rekonsiliasi dengan logika terbaru."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Tanggal buku spesifik (YYYY-MM-DD).")
        parser.add_argument("--all", action="store_true", help="Jalankan untuk semua tanggal yang ada data.")
        parser.add_argument(
            "--reset-manual",
            action="store_true",
            help="Ikut me-reset match manual (default: HANYA me-reset auto-match).",
        )
        parser.add_argument(
            "--include-closed",
            action="store_true",
            help="Buka kembali (reopen) hari yang statusnya CLOSED secara otomatis.",
        )

    def handle(self, *args, **opts):
        target_date_raw = opts.get("date")
        run_all = opts.get("all")
        reset_manual = opts.get("reset_manual")
        include_closed = opts.get("include_closed")

        if not target_date_raw and not run_all:
            raise CommandError("Tentukan --date YYYY-MM-DD atau --all.")

        dates: list[date] = []
        if target_date_raw:
            try:
                dates = [date.fromisoformat(target_date_raw)]
            except ValueError as exc:
                raise CommandError("--date harus berformat YYYY-MM-DD") from exc
        else:
            # Kumpulkan semua tanggal dari ImportBatch & ReconDay berurutan dari yang paling lama
            b_dates = set(ImportBatch.objects.values_list("book_date", flat=True))
            r_dates = set(ReconDay.objects.values_list("book_date", flat=True))
            all_dates = sorted(b_dates | r_dates)
            if not all_dates:
                self.stdout.write(self.style.WARNING("Tidak ada tanggal transaksi ditemukan di database."))
                return
            dates = all_dates

        self.stdout.write(self.style.NOTICE(f"Memproses {len(dates)} tanggal buku..."))

        for d in dates:
            self._rematch_date(d, reset_manual=reset_manual, include_closed=include_closed)

        self.stdout.write(self.style.SUCCESS("Selesai menjalankan rekonsiliasi ulang dengan logika terbaru."))

    def _rematch_date(self, book_date: date, *, reset_manual: bool, include_closed: bool) -> None:
        day = ReconDay.objects.filter(book_date=book_date).first()
        if day and day.locked:
            if not include_closed:
                self.stdout.write(
                    self.style.WARNING(
                        f"[{book_date}] Hari berstatus CLOSED (terkunci). Dilewati. "
                        "Gunakan --include-closed jika ingin membuka ulang."
                    )
                )
                return
            # Buka kembali hari
            reopen_day(book_date)
            self.stdout.write(self.style.NOTICE(f"[{book_date}] Buku dibuka kembali."))

        with transaction.atomic():
            # 1. Pilih Match yang akan di-reset
            match_qs = Match.objects.filter(book_date=book_date)
            if not reset_manual:
                match_qs = match_qs.exclude(match_type=MatchType.MANUAL)

            # Kumpulkan ID mutasi bank & otomax yang terlibat
            bank_ids = set(match_qs.filter(bank_mutation__isnull=False).values_list("bank_mutation_id", flat=True))
            otomax_ids = set(match_qs.filter(otomax_entry__isnull=False).values_list("otomax_entry_id", flat=True))
            # Tambahkan juga relasi ManyToMany otomax_entries (misal AGGREGATE QRIS)
            for m in match_qs.prefetch_related("otomax_entries"):
                for oe in m.otomax_entries.all():
                    otomax_ids.add(oe.pk)

            # Hapus match lama
            deleted_count, _ = match_qs.delete()

            # 2. Kembalikan status mutasi bank ke UNMATCHED jika tidak ada match lain
            if bank_ids:
                BankMutation.objects.filter(id__in=bank_ids).filter(
                    matches__isnull=True
                ).update(
                    match_status=MatchStatus.UNMATCHED,
                    tag_manual="",
                    manual_note="",
                )

            # 3. Kembalikan status entri otomax ke PENDING_SETTLE jika tidak ada match lain
            if otomax_ids:
                OtomaxEntry.objects.filter(id__in=otomax_ids).filter(
                    matches__isnull=True,
                    aggregate_matches__isnull=True,
                ).update(
                    match_status=MatchStatus.PENDING_SETTLE
                )

            # 4. Hapus Discrepancy OPEN yang berasal dari tanggal ini
            deleted_disc, _ = Discrepancy.objects.filter(
                origin_book_date=book_date,
                status="OPEN",
            ).delete()

            # 5. Jalankan engine dengan logika terbaru
            stats = run_match(book_date)
            resolved = carry_forward(book_date)

            # 6. Hitung ulang ReconDay
            r_day, _ = ReconDay.objects.get_or_create(book_date=book_date)
            r_day.recompute_selisih()
            r_day.save()

            self.stdout.write(
                self.style.SUCCESS(
                    f"[{book_date}] Selesai: {deleted_count} match lama di-reset, "
                    f"{stats.matched} match baru terbentuk, {stats.discrepancies} selisih baru, "
                    f"{resolved} selisih lama ditutup."
                )
            )
