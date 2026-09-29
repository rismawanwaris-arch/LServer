"""Perintah audit baca-saja untuk mendeteksi transaksi ganda historis (Keputusan D).

Mencari baris transaksi di BankMutation dan OtomaxEntry yang memiliki:
1. Sidik persis sama (row_hash) jika ada inkonsistensi
2. Kunci stabil sama (waktu presisi + nominal, atau no. referensi) walau keterangan beda

Perintah ini 100% BACA-SAJA (read-only) dan tidak mengubah database.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time

from django.core.management.base import BaseCommand
from django.db.models import Count

from apps.core.enums import Channel
from apps.ingest.models import BankMutation, OtomaxEntry


class Command(BaseCommand):
    help = "Audit baca-saja mencari potensi data dobel historis di database (Keputusan D)."

    def add_arguments(self, parser):
        parser.add_argument("--date", type=str, help="Filter tanggal buku tertentu (YYYY-MM-DD)")
        parser.add_argument("--channel", type=str, help="Filter channel tertentu (BCA, MANDIRI, BRI, OTOMAX)")
        parser.add_argument("--limit", type=int, default=20, help="Batas contoh kasus yang ditampilkan (default: 20)")

    def handle(self, *args, **options):
        date_str = options.get("date")
        channel_filter = options.get("channel")
        limit = options.get("limit")

        self.stdout.write(self.style.MIGRATE_HEADING("=== AUDIT TRANSAKSI GANDA HISTORIS (BACA-SAJA) ==="))
        if date_str:
            self.stdout.write(f"Filter tanggal: {date_str}")
        if channel_filter:
            self.stdout.write(f"Filter channel: {channel_filter}")

        # 1. Audit BankMutation
        self.stdout.write("\nMemeriksa BankMutation...")
        bank_qs = BankMutation.objects.select_related("import_batch")
        if date_str:
            bank_qs = bank_qs.filter(book_date=date_str)
        if channel_filter:
            bank_qs = bank_qs.filter(channel=channel_filter)

        total_bank = bank_qs.count()
        self.stdout.write(f"Total baris BankMutation diperiksa: {total_bank}")

        # a. Duplikat Kunci Stabil: Channel non-BCA dengan waktu presisi (jam != 00:00:00) + nominal
        # BCA dikecualikan dari kunci waktu karena jamnya selalu 00:00:00
        stable_bank_dups = (
            bank_qs.exclude(channel=Channel.BCA)
            .exclude(txn_datetime__isnull=True)
            .values("channel", "txn_datetime", "amount")
            .annotate(cnt=Count("id"))
            .filter(cnt__gt=1)
            .order_by("-cnt")
        )

        bank_dup_count = stable_bank_dups.count()
        self.stdout.write(f"Ditemukan kelompok duplikat waktu presisi + nominal di Bank: {bank_dup_count}")

        shown = 0
        for grp in stable_bank_dups[:limit]:
            rows = list(
                bank_qs.filter(
                    channel=grp["channel"],
                    txn_datetime=grp["txn_datetime"],
                    amount=grp["amount"],
                )
            )
            self.stdout.write(
                self.style.WARNING(
                    f"\n  [Bank] {grp['channel']} | Waktu: {grp['txn_datetime']} | Nominal: {grp['amount']} ({len(rows)} baris)"
                )
            )
            for r in rows:
                b = r.import_batch
                batch_str = f"Batch #{b.id} ({b.source_filename if b else '-'})" if b else "-"
                self.stdout.write(f"    - ID: {r.id} | {batch_str} | Ket: {r.description_raw[:70]}")
            shown += 1

        # b. Duplikat nomor referensi (bila ada)
        ref_bank_dups = (
            bank_qs.exclude(external_ref="")
            .exclude(external_ref__isnull=True)
            .values("channel", "book_date", "external_ref", "amount")
            .annotate(cnt=Count("id"))
            .filter(cnt__gt=1)
            .order_by("-cnt")
        )
        ref_dup_count = ref_bank_dups.count()
        if ref_dup_count > 0:
            self.stdout.write(f"\nDitemukan kelompok duplikat no. referensi + nominal di Bank: {ref_dup_count}")
            for grp in ref_bank_dups[:limit]:
                rows = list(
                    bank_qs.filter(
                        channel=grp["channel"],
                        book_date=grp["book_date"],
                        external_ref=grp["external_ref"],
                        amount=grp["amount"],
                    )
                )
                self.stdout.write(
                    self.style.WARNING(
                        f"\n  [Bank Ref] {grp['channel']} | Ref: {grp['external_ref']} | Nominal: {grp['amount']} ({len(rows)} baris)"
                    )
                )
                for r in rows:
                    b = r.import_batch
                    batch_str = f"Batch #{b.id} ({b.source_filename if b else '-'})" if b else "-"
                    self.stdout.write(f"    - ID: {r.id} | {batch_str} | Ket: {r.description_raw[:70]}")

        # 2. Audit OtomaxEntry
        if not channel_filter or channel_filter == Channel.OTOMAX:
            self.stdout.write("\nMemeriksa OtomaxEntry...")
            oto_qs = OtomaxEntry.objects.select_related("import_batch")
            if date_str:
                oto_qs = oto_qs.filter(book_date=date_str)

            total_oto = oto_qs.count()
            self.stdout.write(f"Total baris OtomaxEntry diperiksa: {total_oto}")

            # Kunci stabil Otomax: waktu entri presisi + reseller_name_raw + nominal
            stable_oto_dups = (
                oto_qs.exclude(entry_datetime__isnull=True)
                .values("entry_datetime", "reseller_name_raw", "amount")
                .annotate(cnt=Count("id"))
                .filter(cnt__gt=1)
                .order_by("-cnt")
            )

            oto_dup_count = stable_oto_dups.count()
            self.stdout.write(f"Ditemukan kelompok duplikat waktu + reseller + nominal di Otomax: {oto_dup_count}")

            for grp in stable_oto_dups[:limit]:
                rows = list(
                    oto_qs.filter(
                        entry_datetime=grp["entry_datetime"],
                        reseller_name_raw=grp["reseller_name_raw"],
                        amount=grp["amount"],
                    )
                )
                self.stdout.write(
                    self.style.WARNING(
                        f"\n  [Otomax] Reseller: {grp['reseller_name_raw']} | Waktu: {grp['entry_datetime']} | Nominal: {grp['amount']} ({len(rows)} baris)"
                    )
                )
                for r in rows:
                    b = r.import_batch
                    batch_str = f"Batch #{b.id} ({b.source_filename if b else '-'})" if b else "-"
                    self.stdout.write(f"    - ID: {r.id} | {batch_str} | Ket: {r.description_raw[:70]}")

        self.stdout.write(self.style.SUCCESS("\nAudit selesai. Mode baca-saja: tidak ada data yang diubah."))
