"""Halaman Pengaturan Aplikasi — konfigurasi yang bisa diubah lewat web, bukan cuma .env."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.models import AppSettings
from apps.core.period import business_month_range

from ._shared import staff_only


@login_required
@staff_only
def app_settings_view(request):
    obj = AppSettings.load()
    preview_range = business_month_range(timezone.localdate(), obj.business_month_start_day)
    return render(request, "dashboard/app_settings.html", {"settings": obj, "preview_range": preview_range})


@login_required
@staff_only
@require_POST
def update_app_settings_action(request):
    setting_type = request.POST.get("setting_type", "")
    obj = AppSettings.load()

    if setting_type == "tolerance":
        fields = [
            ("bri_date_tolerance_min", "Toleransi H-min BRI"),
            ("bri_date_tolerance_max", "Toleransi H+max BRI"),
            ("bca_date_tolerance_min", "Toleransi H-min BCA"),
            ("bca_date_tolerance_max", "Toleransi H+max BCA"),
            ("mandiri_date_tolerance_min", "Toleransi H-min Mandiri"),
            ("mandiri_date_tolerance_max", "Toleransi H+max Mandiri"),
            ("merchant_bca_date_tolerance_min", "Toleransi H-min Merchant BCA"),
            ("merchant_bca_date_tolerance_max", "Toleransi H+max Merchant BCA"),
        ]
        updates = []
        for field_name, label in fields:
            raw = request.POST.get(field_name, "").strip()
            try:
                val = int(raw)
                if not (0 <= val <= 30):
                    raise ValueError
                setattr(obj, field_name, val)
                updates.append(field_name)
            except ValueError:
                messages.error(request, f"{label} harus berupa angka antara 0 sampai 30 hari.")
                return redirect("app-settings")

        updates.append("updated_at")
        obj.save(update_fields=updates)
        messages.success(request, "Toleransi rentang tanggal pencocokan berhasil disimpan.")
        return redirect("app-settings")

    raw = request.POST.get("business_month_start_day", "").strip()
    try:
        value = int(raw)
        if not (1 <= value <= 31):
            raise ValueError
    except ValueError:
        messages.error(request, "Tanggal mulai siklus harus angka 1-31.")
        return redirect("app-settings")

    obj.business_month_start_day = value
    obj.save(update_fields=["business_month_start_day", "updated_at"])
    messages.success(request, f"Siklus bulan bisnis disimpan: mulai tanggal {value}.")
    return redirect("app-settings")
