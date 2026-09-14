from django.conf import settings
from django.db import models

from apps.catalog.models import Reseller
from apps.core.enums import Channel, ImportStatus, ManualTag, MatchStatus, OtomaxCategory
from apps.core.models import TimeStampedModel, money_field


class ImportBatch(TimeStampedModel):
    channel = models.CharField(max_length=16, choices=Channel.choices)
    book_date = models.DateField()
    source_filename = models.CharField(max_length=255)
    file_hash = models.CharField(max_length=64, db_index=True)
    row_count = models.PositiveIntegerField(default=0)
    quarantined_count = models.PositiveIntegerField(default=0)
    excluded_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=12, choices=ImportStatus.choices, default=ImportStatus.PARSED)
    notes = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.channel} {self.book_date} ({self.row_count} baris)"


class BankMutation(TimeStampedModel):
    """Baris kredit (uang masuk) dari mutasi bank, sudah dinormalisasi."""

    import_batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="mutations")
    channel = models.CharField(max_length=16, choices=Channel.choices, db_index=True)
    book_date = models.DateField(db_index=True)
    txn_datetime = models.DateTimeField(null=True, blank=True)

    description_raw = models.TextField()
    ref_normalized = models.TextField(db_index=True)
    ref_core = models.CharField(max_length=80, blank=True, db_index=True)
    extracted_tokens = models.JSONField(default=list, blank=True)
    outlet_name = models.CharField(max_length=150, blank=True)
    amount = money_field()
    external_ref = models.CharField(max_length=40, blank=True, db_index=True)
    frequency = models.PositiveIntegerField(null=True, blank=True)

    row_hash = models.CharField(max_length=64, unique=True)
    match_status = models.CharField(
        max_length=16, choices=MatchStatus.choices, default=MatchStatus.UNMATCHED, db_index=True
    )
    tag_manual = models.CharField(max_length=20, choices=ManualTag.choices, blank=True, default="", db_index=True)
    manual_note = models.TextField(blank=True)
    review_flag = models.CharField(max_length=120, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["book_date", "channel", "match_status"]),
            models.Index(fields=["match_status", "tag_manual"]),
        ]

    def __str__(self) -> str:
        return f"{self.channel} {self.amount} — {self.ref_normalized[:40]}"


class OtomaxEntry(TimeStampedModel):
    """Semua baris export OTOMAX (bertanda; bisa negatif)."""

    import_batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="otomax")
    book_date = models.DateField(db_index=True)
    entry_datetime = models.DateTimeField(null=True, blank=True)

    reseller_name_raw = models.CharField(max_length=120)
    reseller = models.ForeignKey(
        Reseller, null=True, blank=True, on_delete=models.SET_NULL, related_name="otomax_entries"
    )
    amount = money_field()
    description_raw = models.TextField()

    category = models.CharField(
        max_length=16, choices=OtomaxCategory.choices, default=OtomaxCategory.OTHER, db_index=True
    )
    channel_hint = models.CharField(max_length=16, choices=Channel.choices, blank=True, db_index=True)
    ref_normalized = models.TextField(blank=True, db_index=True)
    ref_core = models.CharField(max_length=80, blank=True, db_index=True)
    extracted_tokens = models.JSONField(default=list, blank=True)

    row_hash = models.CharField(max_length=64, unique=True)
    match_status = models.CharField(
        max_length=16, choices=MatchStatus.choices, default=MatchStatus.UNMATCHED, db_index=True
    )

    class Meta:
        indexes = [models.Index(fields=["book_date", "category", "match_status"])]

    def __str__(self) -> str:
        return f"{self.reseller_name_raw} {self.amount} — {self.description_raw[:40]}"


class DebitIgnored(TimeStampedModel):
    """Baris debit mutasi bank (sweep, QRIS keluar). Disimpan agar Σ bisa dicek balik."""

    import_batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="debits")
    channel = models.CharField(max_length=16, choices=Channel.choices)
    book_date = models.DateField(db_index=True)
    txn_datetime = models.DateTimeField(null=True, blank=True)
    description_raw = models.TextField()
    amount = money_field(help_text="Positif; arah keluar tersirat.")
    row_hash = models.CharField(max_length=64, unique=True)


class ExcludedTransaction(TimeStampedModel):
    """Baris mutasi bank atau otomax yang dipisahkan dari mesin rekonsiliasi (non-engine)."""

    import_batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="excluded_transactions")
    channel = models.CharField(max_length=16, choices=Channel.choices, db_index=True)
    source_type = models.CharField(max_length=10, choices=[("BANK", "Bank"), ("OTOMAX", "Otomax")], default="BANK")
    book_date = models.DateField(db_index=True)
    txn_datetime = models.DateTimeField(null=True, blank=True)

    description_raw = models.TextField()
    amount = money_field()
    rule = models.ForeignKey(
        "catalog.ExclusionRule",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="excluded_transactions",
    )
    category = models.CharField(max_length=30, blank=True)
    reason = models.CharField(max_length=150, blank=True)
    row_hash = models.CharField(max_length=64, unique=True)

    class Meta:
        indexes = [
            models.Index(fields=["book_date", "channel"]),
            models.Index(fields=["source_type", "book_date"]),
        ]
        ordering = ["-txn_datetime", "-id"]

    def __str__(self) -> str:
        return f"[EXCLUDED] {self.channel} {self.amount} — {self.description_raw[:40]}"

