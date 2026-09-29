"""Perintah audit baca-saja untuk mendeteksi transaksi ganda historis (Keputusan D).

Mencari baris BankMutation / OtomaxEntry yang punya KUNCI STABIL sama -- aturan yang
PERSIS sama dengan pemeriksa upload (apps.ingest.review): waktu presisi (jam != 00:00:00)
+ nominal, atau no. referensi + tanggal + nominal. Bank yang cuma punya tanggal (BCA,
Merchant BCA tanpa ref) tidak dicek, karena tanggal+nominal kembar di sana sering SAH.

Perintah ini 100% BACA-SAJA (read-only) dan tidak mengubah database.
"""

from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand

from apps.core.enums import Channel
from apps.ingest.models import BankMutation, OtomaxEntry
from apps.ingest.review import bank_stable_key, otomax_stable_key


class Command(BaseCommand):
    help = "Audit baca-saja mencari potensi data dobel historis di database (Keputusan D)."

    def add_arguments(self, parser):
        parser.add_argument("--date", type=str, help="Filter tanggal buku tertentu (YYYY-MM-DD)")
        parser.add_argument(
            "--channel", type=str, help="Filter channel tertentu (BRI, BCA, MANDIRI, MERCHANT_BCA, OTOMAX)"
        )
        parser.add_argument(
            "--limit", type=int, default=20, help="Batas contoh kelompok yang ditampilkan (default: 20)"
        )

    def handle(self, *args, **options):
        date_str = options.get("date")
        channel_filter = options.get("channel")
        limit = options.get("limit")

        self.stdout.write(self.style.MIGRATE_HEADING("=== AUDIT TRANSAKSI GANDA HISTORIS (BACA-SAJA) ==="))
        if date_str:
            self.stdout.write(f"Filter tanggal: {date_str}")
        if channel_filter:
            self.stdout.write(f"Filter channel: {channel_filter}")

        if channel_filter != Channel.OTOMAX:
            bank_qs = BankMutation.objects.select_related("import_batch")
            if date_str:
                bank_qs = bank_qs.filter(book_date=date_str)
            if channel_filter:
                bank_qs = bank_qs.filter(channel=channel_filter)
            groups = defaultdict(list)
            for m in bank_qs.iterator():
                key = bank_stable_key(m.channel, m.txn_datetime, m.book_date, m.external_ref, m.amount)
                if key is not None:
                    groups[key].append(m)
            self._report("Bank", bank_qs.count(), groups, limit)

        if not channel_filter or channel_filter == Channel.OTOMAX:
            oto_qs = OtomaxEntry.objects.select_related("import_batch")
            if date_str:
                oto_qs = oto_qs.filter(book_date=date_str)
            groups = defaultdict(list)
            for o in oto_qs.iterator():
                key = otomax_stable_key(o.entry_datetime, o.reseller_name_raw, o.amount)
                if key is not None:
                    groups[key].append(o)
            self._report("Otomax", oto_qs.count(), groups, limit)

        self.stdout.write(self.style.SUCCESS("\nAudit selesai. Mode baca-saja: tidak ada data yang diubah."))

    def _report(self, label: str, total: int, groups: dict, limit: int) -> None:
        dups = sorted((rows for rows in groups.values() if len(rows) > 1), key=len, reverse=True)
        self.stdout.write(f"\nMemeriksa {label}: {total} baris")
        self.stdout.write(f"Kelompok kemungkinan dobel di {label}: {len(dups)}")
        for rows in dups[:limit]:
            first = rows[0]
            when = getattr(first, "txn_datetime", None) or getattr(first, "entry_datetime", None)
            who = getattr(first, "channel", "") or getattr(first, "reseller_name_raw", "")
            self.stdout.write(
                self.style.WARNING(f"\n  [{label}] {who} | Waktu: {when} | Nominal: {first.amount} ({len(rows)} baris)")
            )
            for r in rows:
                b = r.import_batch
                batch_str = f"Batch #{b.id} ({b.source_filename})" if b else "-"
                self.stdout.write(f"    - ID: {r.id} | {batch_str} | Ket: {r.description_raw[:70]}")
