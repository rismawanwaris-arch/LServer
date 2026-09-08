from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    """Aktifkan pg_trgm untuk fuzzy match di level DB (opsional, dipakai nanti).

    CreateExtension otomatis dilewati kalau DB bukan PostgreSQL (mis. SQLite di dev awal).
    """

    initial = True
    dependencies = []
    operations = [TrigramExtension()]
