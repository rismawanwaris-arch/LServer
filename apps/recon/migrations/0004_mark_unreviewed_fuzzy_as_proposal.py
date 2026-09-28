"""AUTO_FUZZY lama yang belum direview (note kosong) dulu memblokir tutup buku lewat cek
note=="" di close_day. Cek itu sekarang diganti needs_review -- tandai yang lama juga
supaya perilaku blokirnya tidak hilang diam-diam. Hari yang sudah ditutup dibiarkan."""

from django.db import migrations


def mark(apps, schema_editor):
    Match = apps.get_model("recon", "Match")
    ReconDay = apps.get_model("recon", "ReconDay")
    locked = ReconDay.objects.filter(locked=True).values_list("book_date", flat=True)
    Match.objects.filter(match_type="AUTO_FUZZY", voided_at__isnull=True, note="").exclude(
        book_date__in=locked
    ).update(needs_review=True, review_reason="Kemiripan teks (fuzzy)")


class Migration(migrations.Migration):
    dependencies = [("recon", "0003_match_review_proposal")]

    operations = [migrations.RunPython(mark, migrations.RunPython.noop)]
