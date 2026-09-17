from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.enums import (
    Channel,
    DayStatus,
    DiscrepancyKind,
    DiscrepancyStatus,
    MatchType,
    ResolutionType,
)
from apps.core.models import TimeStampedModel, money_field
from apps.ingest.models import BankMutation, OtomaxEntry

ZERO = Decimal("0.00")


class Match(TimeStampedModel):
    book_date = models.DateField(db_index=True)
    channel = models.CharField(max_length=16, choices=Channel.choices)
    bank_mutation = models.ForeignKey(
        BankMutation, null=True, blank=True, on_delete=models.PROTECT, related_name="matches"
    )
    otomax_entry = models.ForeignKey(
        OtomaxEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="matches"
    )
    otomax_entries = models.ManyToManyField(
        OtomaxEntry,
        blank=True,
        related_name="aggregate_matches",
        help_text="Semua baris Otomax dalam grup untuk match AGGREGATE (QRIS multi-baris). "
        "otomax_entry di atas tetap menunjuk baris utama untuk tampilan.",
    )
    match_type = models.CharField(max_length=12, choices=MatchType.choices)
    amount_bank = money_field(default=ZERO)
    amount_otomax = money_field(default=ZERO)
    amount_diff = money_field(default=ZERO)
    confidence = models.PositiveSmallIntegerField(null=True, blank=True)
    note = models.TextField(blank=True)
    matched_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="voided_matches",
    )
    history = HistoricalRecords()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["bank_mutation"],
                condition=models.Q(voided_at__isnull=True),
                name="uniq_active_bank_match",
            ),
            models.UniqueConstraint(
                fields=["otomax_entry"],
                condition=models.Q(voided_at__isnull=True),
                name="uniq_active_otomax_match",
            ),
        ]

    def save(self, *args, **kwargs):
        self.amount_diff = (self.amount_bank or ZERO) - (self.amount_otomax or ZERO)
        super().save(*args, **kwargs)


class Discrepancy(TimeStampedModel):
    code = models.CharField(max_length=24, unique=True)
    origin_book_date = models.DateField(db_index=True, help_text="Tidak pernah berubah.")
    channel = models.CharField(max_length=16, choices=Channel.choices)
    kind = models.CharField(max_length=12, choices=DiscrepancyKind.choices)
    bank_mutation = models.ForeignKey(
        BankMutation, null=True, blank=True, on_delete=models.PROTECT, related_name="discrepancies"
    )
    otomax_entry = models.ForeignKey(
        OtomaxEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="discrepancies"
    )
    amount = money_field(help_text="Bertanda: + = bank lebih, - = OTOMAX lebih.")
    status = models.CharField(
        max_length=12, choices=DiscrepancyStatus.choices, default=DiscrepancyStatus.OPEN, db_index=True
    )
    resolved_book_date = models.DateField(null=True, blank=True)
    resolution_type = models.CharField(max_length=12, choices=ResolutionType.choices, blank=True)
    resolution_match = models.ForeignKey(
        Match, null=True, blank=True, on_delete=models.SET_NULL, related_name="resolves"
    )
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    resolved_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name_plural = "discrepancies"
        ordering = ["origin_book_date", "code"]

    def __str__(self) -> str:
        return f"{self.code} · {self.kind} · {self.amount}"


class Adjustment(TimeStampedModel):
    """Penyesuaian bertanggal ke hari yang sudah ditutup. Append-only."""

    book_date = models.DateField(db_index=True, help_text="= discrepancy.origin_book_date")
    discrepancy = models.ForeignKey(Discrepancy, on_delete=models.PROTECT, related_name="adjustments")
    amount = money_field(help_text="Bertanda; menggerakkan selisih hari asal.")
    reason = models.TextField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    history = HistoricalRecords()

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Adjustment bersifat append-only, tidak boleh diubah.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Adjustment tidak boleh dihapus.")


class ReconDay(TimeStampedModel):
    book_date = models.DateField(unique=True)
    status = models.CharField(max_length=10, choices=DayStatus.choices, default=DayStatus.DRAFT)
    locked = models.BooleanField(default=False)

    total_in_bri = money_field(default=ZERO)
    total_in_bca = money_field(default=ZERO)
    total_in_merchant_bca = money_field(default=ZERO)
    total_in_mandiri = money_field(default=ZERO)
    total_in_bank = money_field(default=ZERO)
    total_out_otomax = money_field(default=ZERO)

    selisih_initial = money_field(default=ZERO)
    selisih_adjustments = money_field(default=ZERO)
    selisih_current = money_field(default=ZERO)

    matched_count = models.PositiveIntegerField(default=0)
    unmatched_count = models.PositiveIntegerField(default=0)

    snapshot = models.JSONField(default=dict, blank=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    closed_at = models.DateTimeField(null=True, blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-book_date"]

    def __str__(self) -> str:
        return f"{self.book_date} [{self.status}]"

    def recompute_selisih(self):
        agg = self.book_adjustments().aggregate(s=models.Sum("amount"))
        self.selisih_adjustments = agg["s"] or ZERO
        self.selisih_current = self.selisih_initial - self.selisih_adjustments

    def book_adjustments(self):
        return Adjustment.objects.filter(book_date=self.book_date)
