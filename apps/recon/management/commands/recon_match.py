from datetime import date

from django.core.management.base import BaseCommand, CommandError

from apps.recon.carry import carry_forward
from apps.recon.engine import run_match


class Command(BaseCommand):
    help = "Jalankan pencocokan + carry-forward untuk satu tanggal buku."

    def add_arguments(self, parser):
        parser.add_argument("book_date", help="YYYY-MM-DD")
        parser.add_argument("--no-carry", action="store_true")

    def handle(self, *args, **opts):
        try:
            bd = date.fromisoformat(opts["book_date"])
        except ValueError as exc:
            raise CommandError("book_date harus YYYY-MM-DD") from exc

        stats = run_match(bd)
        self.stdout.write(self.style.SUCCESS(f"{bd}: {stats.matched} match, {stats.discrepancies} discrepancy"))
        if not opts["no_carry"]:
            resolved = carry_forward(bd)
            self.stdout.write(self.style.SUCCESS(f"carry-forward: {resolved} discrepancy lama ditutup"))
