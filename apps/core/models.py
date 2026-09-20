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
