from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

MONEY = {"max_digits": 15, "decimal_places": 2}


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


def money_field(**kwargs):
    return models.DecimalField(**MONEY, **kwargs)


class AppSettings(models.Model):
    """Pengaturan aplikasi yang bisa diubah lewat halaman web (bukan cuma .env) —
    singleton, selalu pakai AppSettings.load(), jangan query .objects langsung."""

    business_month_start_day = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(31)],
        help_text="Tanggal mulai siklus bulan bisnis (1-31) untuk expand Alarm SLA. "
        "1 = kalender biasa. Contoh: 29 -> siklus tgl 29 s/d 28.",
    )

    # Toleransi rentang tanggal pencocokan per channel bank (H-min s/d H+max)
    bri_date_tolerance_min = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(0), MaxValueValidator(30)],
        help_text="Toleransi hari mundur (H-min) untuk BRI.",
    )
    bri_date_tolerance_max = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(0), MaxValueValidator(30)],
        help_text="Toleransi hari maju (H+max) untuk BRI.",
    )
    bca_date_tolerance_min = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(0), MaxValueValidator(30)],
        help_text="Toleransi hari mundur (H-min) untuk BCA.",
    )
    bca_date_tolerance_max = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(0), MaxValueValidator(30)],
        help_text="Toleransi hari maju (H+max) untuk BCA.",
    )
    mandiri_date_tolerance_min = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(0), MaxValueValidator(30)],
        help_text="Toleransi hari mundur (H-min) untuk Mandiri.",
    )
    mandiri_date_tolerance_max = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(0), MaxValueValidator(30)],
        help_text="Toleransi hari maju (H+max) untuk Mandiri.",
    )
    merchant_bca_date_tolerance_min = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(0), MaxValueValidator(30)],
        help_text="Toleransi hari mundur (H-min) untuk Merchant BCA.",
    )
    merchant_bca_date_tolerance_max = models.PositiveSmallIntegerField(
        default=2,
        validators=[MinValueValidator(0), MaxValueValidator(30)],
        help_text="Toleransi hari maju (H+max) untuk Merchant BCA.",
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Pengaturan Aplikasi"
        verbose_name_plural = "Pengaturan Aplikasi"

    def __str__(self) -> str:
        return "Pengaturan Aplikasi"

    def save(self, *args, **kwargs):
        self.pk = 1  # singleton — selalu satu baris
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "AppSettings":
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={"business_month_start_day": getattr(settings, "BUSINESS_MONTH_START_DAY", 1)},
        )
        return obj

    def get_bank_date_tolerance(self) -> dict[str, tuple[int, int]]:
        from apps.core.enums import Channel

        return {
            Channel.BRI: (-int(self.bri_date_tolerance_min), int(self.bri_date_tolerance_max)),
            Channel.BCA: (-int(self.bca_date_tolerance_min), int(self.bca_date_tolerance_max)),
            Channel.MANDIRI: (-int(self.mandiri_date_tolerance_min), int(self.mandiri_date_tolerance_max)),
            Channel.MERCHANT_BCA: (
                -int(self.merchant_bca_date_tolerance_min),
                int(self.merchant_bca_date_tolerance_max),
            ),
        }
