"""Halaman Backup & Restore Data -- download snapshot data rekonsiliasi, dan restore
dari file lewat alur upload -> preview -> konfirmasi (supaya operator lihat dulu isi
file sebelum data sekarang ditimpa). Khusus staff."""

from __future__ import annotations

import secrets
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.recon.backup import (
    RestoreError,
    backup_filename,
    generate_backup_json,
    list_safety_backups,
    preview_backup_file,
    restore_from_json,
)

from ._shared import staff_only

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024


def _restore_tmp_dir() -> Path:
    return Path(settings.MEDIA_ROOT) / "backups" / "tmp-restore"


@login_required
@staff_only
def backup_view(request):
    return render(
        request,
        "dashboard/backup.html",
        {"safety_backups": [p.name for p in list_safety_backups()]},
    )


@login_required
@staff_only
def download_backup_action(request):
    content = generate_backup_json()
    response = HttpResponse(content, content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename="{backup_filename()}"'
    return response


@login_required
@staff_only
@require_POST
def restore_preview_action(request):
    upload = request.FILES.get("file")
    if not upload:
        messages.error(request, "Pilih file backup (.json) dulu.")
        return redirect("backup")
    if upload.size > _MAX_UPLOAD_BYTES:
        messages.error(request, "File terlalu besar (maks 100MB).")
        return redirect("backup")

    content = upload.read().decode("utf-8", errors="replace")
    try:
        counts = preview_backup_file(content)
    except RestoreError as exc:
        messages.error(request, str(exc))
        return redirect("backup")

    tmp_dir = _restore_tmp_dir()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    (tmp_dir / f"{token}.json").write_text(content, encoding="utf-8")

    return render(
        request,
        "dashboard/backup_confirm.html",
        {"counts": sorted(counts.items()), "total": sum(counts.values()), "token": token},
    )


@login_required
@staff_only
@require_POST
def restore_confirm_action(request):
    token = request.POST.get("token", "")
    if not token or "/" in token or "\\" in token:
        messages.error(request, "Token restore tidak valid. Ulangi upload file.")
        return redirect("backup")

    tmp_path = _restore_tmp_dir() / f"{token}.json"
    if not tmp_path.exists():
        messages.error(request, "Sesi restore sudah kedaluwarsa. Ulangi upload file.")
        return redirect("backup")

    content = tmp_path.read_text(encoding="utf-8")
    try:
        result = restore_from_json(content)
    except RestoreError as exc:
        messages.error(request, str(exc))
        return redirect("backup")
    finally:
        tmp_path.unlink(missing_ok=True)

    total = sum(result["counts"].values())
    messages.success(
        request,
        f"Restore selesai: {total} baris dipulihkan. "
        f"Safety backup data sebelumnya tersimpan di {result['safety_backup_path']}.",
    )
    return redirect("backup")
