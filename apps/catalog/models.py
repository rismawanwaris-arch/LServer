from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.enums import Channel
from apps.core.models import TimeStampedModel


class ExclusionTarget(models.TextChoices):
    BANK = "BANK", "Mutasi Bank"
    OTOMAX = "OTOMAX", "Data Otomax"
    ALL = "ALL", "Semua Data (Bank & Otomax)"


class ExclusionCategory(models.TextChoices):
    BIAYA_ADMIN = "BIAYA_ADMIN", "Biaya Administrasi Bank"
    TRANSFER_INTERNAL = "TRANSFER_INTERNAL", "Transfer Internal / Pemindahan Dana"
    PAJAK_BUNGA = "PAJAK_BUNGA", "Pajak & Bunga"
    NON_OPERASIONAL = "NON_OPERASIONAL", "Pengeluaran Non-Operasional"
    LAINNYA = "LAINNYA", "Lainnya"


class ExclusionRule(TimeStampedModel):
    """Aturan deteksi kata kunci untuk memisahkan data dari engine rekonsiliasi."""

    name = models.CharField(max_length=120, help_text="Nama atau label aturan")
    keywords = models.CharField(
        max_length=255,
        help_text="Kata kunci dalam keterangan (pisahkan koma jika banyak, case-insensitive)",
    )
    target = models.CharField(
        max_length=16,
        choices=ExclusionTarget.choices,
        default=ExclusionTarget.BANK,
    )
    channel = models.CharField(
        max_length=16,
        choices=Channel.choices,
        blank=True,
        default="",
        help_text="Kosongkan jika berlaku untuk semua channel",
    )
    category = models.CharField(
        max_length=30,
        choices=ExclusionCategory.choices,
        default=ExclusionCategory.BIAYA_ADMIN,
    )
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        ch = f" [{self.channel}]" if self.channel else ""
        return f"{self.name} ({self.keywords}){ch}"

    def matches(self, desc: str, channel: str | None = None, is_bank: bool = True) -> bool:
        if not self.active:
            return False
        if self.target == ExclusionTarget.BANK and not is_bank:
            return False
        if self.target == ExclusionTarget.OTOMAX and is_bank:
            return False
        if self.channel and channel and self.channel != channel:
            return False

        desc_upper = (desc or "").upper()
        for kw in self.keywords.split(","):
            kw_clean = kw.strip().upper()
            if kw_clean and kw_clean in desc_upper:
                return True
        return False



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
