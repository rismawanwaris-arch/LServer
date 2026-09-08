from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import TimeStampedModel


class Reseller(TimeStampedModel):
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=120)
    active = models.BooleanField(default=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"


class ResellerAlias(TimeStampedModel):
    """Setiap ejaan nama reseller yang muncul di export OTOMAX -> satu Reseller."""

    alias_norm = models.CharField(max_length=120, unique=True, db_index=True)
    alias_raw = models.CharField(max_length=120)
    reseller = models.ForeignKey(Reseller, on_delete=models.CASCADE, related_name="aliases")
    history = HistoricalRecords()

    def __str__(self) -> str:
        return f"{self.alias_raw} → {self.reseller.code}"


class MerchantMap(TimeStampedModel):
    """Merchant ID QRIS BCA -> Reseller. Kunci rekonsiliasi channel MERCHANT_BCA."""

    merchant_id = models.CharField(max_length=20, unique=True)
    merchant_name = models.CharField(max_length=120, blank=True)
    reseller = models.ForeignKey(Reseller, on_delete=models.PROTECT, related_name="merchants")
    active = models.BooleanField(default=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["merchant_name"]

    def __str__(self) -> str:
        return f"{self.merchant_id} · {self.merchant_name} → {self.reseller.code}"
