"""Klasifikasi ulang entri OTOMAX yang masih terbuka dengan aturan normalisasi terbaru.

Kategori, channel_hint, ref_normalized, ref_core & token dihitung SEKALI saat file
diimpor -- memperbaiki aturan di apps/core/normalize.py (mis. mengenali "BAYAR KE
MANDIRI", membuang akhiran "TGL 30/AGS/2026") tidak otomatis mengubah baris yang sudah
ada di database. Command ini menghitung ulang field-field itu dari description_raw.

Cuma menyentuh entri yang masih terbuka (UNMATCHED / PENDING_SETTLE) di tanggal yang
belum ditutup -- yang sudah dipasangkan atau hari yang sudah dikunci dibiarkan apa adanya.
Setelah --apply, jalankan "Jalankan Matching Engine" untuk tanggal yang terdampak.

Default DRY-RUN (cuma laporan, tidak mengubah apa pun). Pakai --apply untuk eksekusi.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.enums import MatchStatus
from apps.ingest.models import OtomaxEntry
from apps.ingest.services import otomax_derived_fields
from apps.recon.models import ReconDay

_FIELDS = ("category", "channel_hint", "ref_normalized", "ref_core", "extracted_tokens")


class Command(BaseCommand):
    help = (
        "Hitung ulang kategori/channel/ref entri OTOMAX yang masih terbuka dengan aturan "
        "normalisasi terbaru. Default dry-run — pakai --apply untuk eksekusi nyata."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Eksekusi nyata (default: dry-run).")
        parser.add_argument("--book-date", help="Batasi ke satu tanggal buku YYYY-MM-DD (opsional).")

    def handle(self, *args, **opts):
        qs = OtomaxEntry.objects.filter(
            match_status__in=[MatchStatus.UNMATCHED, MatchStatus.PENDING_SETTLE]
        ).order_by("book_date", "id")
        if opts.get("book_date"):
            try:
                qs = qs.filter(book_date=date.fromisoformat(opts["book_date"]))
            except ValueError as exc:
                raise CommandError("--book-date harus YYYY-MM-DD") from exc

        locked_dates = set(ReconDay.objects.filter(locked=True).values_list("book_date", flat=True))
        apply_mode = bool(opts["apply"])
        self.stdout.write(f"Mode: {'APPLY (eksekusi nyata)' if apply_mode else 'DRY-RUN (cuma laporan)'}\n")

        changes = []
        skipped_locked = 0
        for o in qs:
            if o.book_date in locked_dates:
                skipped_locked += 1
                continue
            new = otomax_derived_fields(o.description_raw)
            changed = {f: new[f] for f in _FIELDS if getattr(o, f) != new[f]}
            if changed:
                changes.append((o, changed))

        classification = [(o, c) for o, c in changes if "category" in c or "channel_hint" in c]
        category_moves = Counter((o.category, c.get("category", o.category)) for o, c in classification)
        for (old, new), n in sorted(category_moves.items()):
            self.stdout.write(f"  kategori {old} -> {new}: {n} entri")
        for o, c in classification:
            self.stdout.write(
                f"  [{o.book_date}] #{o.id} {o.reseller_name_raw} Rp{o.amount:,.0f} — {o.description_raw[:70]}\n"
                f"      kategori {o.category} -> {c.get('category', o.category)}, "
                f"channel '{o.channel_hint}' -> '{c.get('channel_hint', o.channel_hint)}'"
            )
        ref_only = len(changes) - len(classification)
        if ref_only:
            self.stdout.write(f"  {ref_only} entri lain cuma berubah ref/token (mis. akhiran TGL dibuang).")
        if skipped_locked:
            self.stdout.write(f"  {skipped_locked} entri dilewati karena tanggalnya sudah ditutup.")

        if not changes:
            self.stdout.write(self.style.SUCCESS("Tidak ada entri yang perlu diklasifikasi ulang."))
            return

        affected_dates = sorted({o.book_date for o, _ in changes})
        if apply_mode:
            with transaction.atomic():
                for o, c in changes:
                    for field, value in c.items():
                        setattr(o, field, value)
                    o.save(update_fields=[*c.keys(), "updated_at"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nSelesai: {len(changes)} entri diperbarui. Jalankan Matching Engine untuk tanggal: "
                    + ", ".join(d.isoformat() for d in affected_dates)
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nDry-run selesai: {len(changes)} entri AKAN diperbarui "
                    f"({len(classification)} berubah kategori/channel). "
                    "Jalankan lagi dengan --apply untuk benar-benar mengeksekusi."
                )
            )
