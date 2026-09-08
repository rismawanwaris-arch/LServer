from datetime import date

from django.core.management.base import BaseCommand, CommandError

from apps.recon.close import DayLocked, DayNotReady, close_day


class Command(BaseCommand):
    help = "Tutup buku satu tanggal (snapshot + kunci)."

    def add_arguments(self, parser):
        parser.add_argument("book_date", help="YYYY-MM-DD")
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, **opts):
        try:
            bd = date.fromisoformat(opts["book_date"])
        except ValueError as exc:
            raise CommandError("book_date harus YYYY-MM-DD") from exc
        try:
            day = close_day(bd, force=opts["force"])
        except (DayLocked, DayNotReady) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"{bd} ditutup. masuk={day.total_in_bank} keluar={day.total_out_otomax} "
                f"selisih={day.selisih_initial}"
            )
        )
