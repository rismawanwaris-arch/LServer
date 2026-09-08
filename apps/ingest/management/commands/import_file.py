from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.core.enums import Channel
from apps.ingest.services import import_file


class Command(BaseCommand):
    help = "Impor satu file mutasi/OTOMAX. Contoh: import_file BRI ~/Downloads/bri.txt 2026-09-05"

    def add_arguments(self, parser):
        parser.add_argument("channel", choices=[c.value for c in Channel])
        parser.add_argument("path")
        parser.add_argument("book_date", help="YYYY-MM-DD")

    def handle(self, *args, **opts):
        path = Path(opts["path"]).expanduser()
        if not path.exists():
            raise CommandError(f"file tidak ada: {path}")
        try:
            book_date = date.fromisoformat(opts["book_date"])
        except ValueError as exc:
            raise CommandError("book_date harus YYYY-MM-DD") from exc

        batch = import_file(
            channel=opts["channel"],
            text=path.read_text(encoding="utf-8", errors="replace"),
            book_date=book_date,
            filename=path.name,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"batch #{batch.id} {batch.channel} {batch.book_date}: "
                f"{batch.row_count} baris, {batch.quarantined_count} dikarantina ({batch.status})"
            )
        )
        if batch.notes:
            self.stdout.write(batch.notes)
