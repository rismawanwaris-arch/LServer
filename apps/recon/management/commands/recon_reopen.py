from datetime import date

from django.core.management.base import BaseCommand, CommandError

from apps.recon.close import DayHasDownstream, reopen_day


class Command(BaseCommand):
    help = "Buka kembali tanggal buku yang keburu ditutup (kalau belum ada dampak lanjutan)."

    def add_arguments(self, parser):
        parser.add_argument("book_date", help="YYYY-MM-DD")
        parser.add_argument("--force", action="store_true", help="tembus pengaman (berisiko)")

    def handle(self, *args, **opts):
        try:
            bd = date.fromisoformat(opts["book_date"])
        except ValueError as exc:
            raise CommandError("book_date harus YYYY-MM-DD") from exc
        try:
            day = reopen_day(bd, force=opts["force"])
        except DayHasDownstream as exc:
            raise CommandError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"gagal: {exc}") from exc
        self.stdout.write(self.style.SUCCESS(f"{bd} dibuka kembali (status {day.status})."))
