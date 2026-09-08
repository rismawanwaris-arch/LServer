from datetime import date

from django.core.management.base import BaseCommand, CommandError

from apps.recon.purge import DayIsClosed, preview_all, purge_all_transactions, purge_day


class Command(BaseCommand):
    help = "Hapus data transaksi. `recon_purge 2026-09-08` atau `recon_purge --all`."

    def add_arguments(self, parser):
        parser.add_argument("book_date", nargs="?", help="YYYY-MM-DD")
        parser.add_argument("--all", action="store_true", help="hapus SEMUA tanggal")
        parser.add_argument("--include-closed", action="store_true")
        parser.add_argument("--include-history", action="store_true")
        parser.add_argument("--yes", action="store_true", help="lewati konfirmasi")

    def handle(self, *args, **opts):
        if opts["all"]:
            if not opts["yes"]:
                self.stdout.write(f"Akan menghapus: {preview_all()}")
                if input('Ketik "HAPUS SEMUA": ') != "HAPUS SEMUA":
                    raise CommandError("dibatalkan")
            counts = purge_all_transactions(include_history=opts["include_history"])
            self.stdout.write(self.style.SUCCESS(f"dihapus: {counts}"))
            return

        if not opts["book_date"]:
            raise CommandError("beri tanggal (YYYY-MM-DD) atau --all")
        try:
            bd = date.fromisoformat(opts["book_date"])
        except ValueError as exc:
            raise CommandError("book_date harus YYYY-MM-DD") from exc
        try:
            counts = purge_day(bd, include_closed=opts["include_closed"])
        except DayIsClosed as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"{bd} dihapus: {counts}"))
