from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ingest', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='bankmutation',
            name='extracted_tokens',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='bankmutation',
            name='outlet_name',
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name='bankmutation',
            name='tag_manual',
            field=models.CharField(blank=True, choices=[('admin', 'Biaya Admin'), ('tarik_tunai', 'Tarik Tunai'), ('setor_tunai', 'Setor Tunai'), ('revisi', 'Revisi'), ('lainnya', 'Lainnya')], db_index=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name='bankmutation',
            name='manual_note',
            field=models.TextField(blank=True),
        ),
        migrations.AlterField(
            model_name='bankmutation',
            name='match_status',
            field=models.CharField(choices=[('UNMATCHED', 'Belum cocok'), ('MATCHED', 'Cocok (otomatis)'), ('MANUAL', 'Cocok (manual)'), ('PENDING_SETTLE', 'Pending Settle'), ('IGNORED', 'Diabaikan')], db_index=True, default='UNMATCHED', max_length=16),
        ),
        migrations.AddField(
            model_name='otomaxentry',
            name='extracted_tokens',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AlterField(
            model_name='otomaxentry',
            name='match_status',
            field=models.CharField(choices=[('UNMATCHED', 'Belum cocok'), ('MATCHED', 'Cocok (otomatis)'), ('MANUAL', 'Cocok (manual)'), ('PENDING_SETTLE', 'Pending Settle'), ('IGNORED', 'Diabaikan')], db_index=True, default='UNMATCHED', max_length=16),
        ),
        migrations.AddIndex(
            model_name='bankmutation',
            index=models.Index(fields=['match_status', 'tag_manual'], name='ingest_bank_match_tag_idx'),
        ),
    ]
