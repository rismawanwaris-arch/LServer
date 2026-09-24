"""Halaman Kode Reseller — kelola pemetaan kode (mis. 'PLC131') ke nama reseller Otomax,
dipakai mesin pencocokan untuk menerjemahkan kode yang muncul di keterangan mutasi bank
jadi reseller spesifik (lihat apps/recon/engine/ref_match.py::_match_by_reseller_code)."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.catalog.models import Reseller


@login_required
def reseller_view(request):
    resellers = Reseller.objects.all().order_by("-active", "code")
    return render(request, "dashboard/resellers.html", {"resellers": resellers})


@login_required
@require_POST
def add_reseller_action(request):
    code = request.POST.get("code", "").strip().upper()
    name = request.POST.get("name", "").strip()

    if not code or not name:
        messages.error(request, "Kode dan nama reseller wajib diisi.")
        return redirect("reseller-list")

    try:
        with transaction.atomic():
            Reseller.objects.create(code=code, name=name, active=True)
    except IntegrityError:
        messages.error(request, f"Kode '{code}' sudah terdaftar.")
        return redirect("reseller-list")

    messages.success(request, f"Reseller '{code} — {name}' berhasil ditambahkan.")
    return redirect("reseller-list")


@login_required
@require_POST
def edit_reseller_action(request, pk: int):
    reseller = get_object_or_404(Reseller, pk=pk)
    code = request.POST.get("code", "").strip().upper()
    name = request.POST.get("name", "").strip()

    if not code or not name:
        messages.error(request, "Kode dan nama reseller wajib diisi.")
        return redirect("reseller-list")

    reseller.code = code
    reseller.name = name
    try:
        with transaction.atomic():
            reseller.save(update_fields=["code", "name", "updated_at"])
    except IntegrityError:
        messages.error(request, f"Kode '{code}' sudah dipakai reseller lain.")
        return redirect("reseller-list")

    messages.success(request, f"Reseller '{code}' berhasil diperbarui.")
    return redirect("reseller-list")


@login_required
@require_POST
def toggle_reseller_action(request, pk: int):
    reseller = get_object_or_404(Reseller, pk=pk)
    reseller.active = not reseller.active
    reseller.save(update_fields=["active", "updated_at"])
    status_str = "diaktifkan" if reseller.active else "dinonaktifkan"
    messages.success(request, f"Reseller '{reseller.code}' berhasil {status_str}.")
    return redirect("reseller-list")


@login_required
@require_POST
def delete_reseller_action(request, pk: int):
    reseller = get_object_or_404(Reseller, pk=pk)
    code = reseller.code
    try:
        with transaction.atomic():
            reseller.delete()
        messages.success(request, f"Reseller '{code}' berhasil dihapus.")
    except ProtectedError:
        messages.error(
            request,
            f"Reseller '{code}' tidak bisa dihapus karena masih dipakai di pemetaan Merchant QRIS. "
            "Nonaktifkan saja alih-alih menghapus.",
        )
    return redirect("reseller-list")
