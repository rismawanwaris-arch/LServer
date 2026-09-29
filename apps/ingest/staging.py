"""Staging upload sementara untuk alur "Periksa Dulu Sebelum Simpan".

File ditahan sementara di server (media/temp_uploads/) bersama token aman
selama maksimal 1 jam, sehingga operator dapat memeriksa status seluruh baris
sebelum konfirmasi simpan tanpa perlu upload ulang file dari browser.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from django.conf import settings

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_MAX_AGE_SECONDS = 3600  # 1 jam


def _staging_dir() -> Path:
    dir_path = Path(settings.MEDIA_ROOT) / "temp_uploads"
    dir_path.mkdir(parents=True, exist_ok=True)
    return dir_path


@dataclass
class StagedUpload:
    token: str
    channel: str
    filename: str
    book_date: date
    content: bytes
    created_at: float
    user_id: int | None = None


def cleanup_staged_uploads(max_age_seconds: int = _MAX_AGE_SECONDS) -> int:
    """Hapus file staging yang sudah kedaluwarsa."""
    staged_dir = _staging_dir()
    now = time.time()
    cleaned = 0
    try:
        for p in staged_dir.iterdir():
            if p.is_file() and (now - p.stat().st_mtime) > max_age_seconds:
                p.unlink(missing_ok=True)
                cleaned += 1
    except Exception:
        pass
    return cleaned


def stage_upload(
    *,
    channel: str,
    book_date: date,
    filename: str,
    content: bytes,
    user_id: int | None = None,
) -> str:
    """Simpan isi file dan metadata sementara, kembalikan token unik."""
    cleanup_staged_uploads()

    token = secrets.token_urlsafe(24)
    staged_dir = _staging_dir()

    meta = {
        "channel": channel,
        "book_date": book_date.isoformat(),
        "filename": filename,
        "created_at": time.time(),
        "user_id": user_id,
    }

    meta_file = staged_dir / f"{token}.meta.json"
    bin_file = staged_dir / f"{token}.bin"

    meta_file.write_text(json.dumps(meta), encoding="utf-8")
    bin_file.write_bytes(content)

    return token


def get_staged_upload(token: str) -> StagedUpload | None:
    """Ambil data staging berdasarkan token. Kembalikan None jika tidak ada/kedaluwarsa."""
    if not token or not _TOKEN_RE.match(token):
        return None

    staged_dir = _staging_dir()
    meta_file = staged_dir / f"{token}.meta.json"
    bin_file = staged_dir / f"{token}.bin"

    if not meta_file.exists() or not bin_file.exists():
        return None

    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        created_at = meta.get("created_at", 0)
        if (time.time() - created_at) > _MAX_AGE_SECONDS:
            delete_staged_upload(token)
            return None

        content = bin_file.read_bytes()
        return StagedUpload(
            token=token,
            channel=meta["channel"],
            filename=meta.get("filename", ""),
            book_date=date.fromisoformat(meta["book_date"]),
            content=content,
            created_at=created_at,
            user_id=meta.get("user_id"),
        )
    except Exception:
        delete_staged_upload(token)
        return None


def delete_staged_upload(token: str) -> None:
    """Hapus file staging dan metadata token."""
    if not token or not _TOKEN_RE.match(token):
        return

    staged_dir = _staging_dir()
    (staged_dir / f"{token}.meta.json").unlink(missing_ok=True)
    (staged_dir / f"{token}.bin").unlink(missing_ok=True)
