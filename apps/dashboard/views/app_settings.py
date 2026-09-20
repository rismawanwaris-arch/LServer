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
    raw = request.POST.get("business_month_start_day", "").strip()
    try:
        value = int(raw)
        if not (1 <= value <= 31):
            raise ValueError
    except ValueError:
        messages.error(request, "Tanggal mulai siklus harus angka 1-31.")
        return redirect("app-settings")

    obj = AppSettings.load()
    obj.business_month_start_day = value
    obj.save(update_fields=["business_month_start_day", "updated_at"])
    messages.success(request, f"Siklus bulan bisnis disimpan: mulai tanggal {value}.")
    return redirect("app-settings")
