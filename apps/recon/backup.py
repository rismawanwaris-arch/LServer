"""Backup & restore data rekonsiliasi (mutasi, OTOMAX, pasangan, selisih, katalog reseller,
aturan filter, pengaturan aplikasi) lewat file JSON, pakai serializer bawaan Django --
TIDAK menyentuh akun user (auth.User) atau riwayat perubahan (simple_history), supaya
restore tidak pernah mengubah siapa yang bisa login dan tetap ringkas.

BACKUP_DIR sengaja ditaruh di bawah MEDIA_ROOT (bukan BASE_DIR) supaya ikut volume
`./data/media:/app/media` yang sudah di-bind-mount di compose.zima.yml -- kalau ditaruh
di luar itu, file safety-backup akan hilang saat container di-rebuild."""

from __future__ import annotations

import json
from datetime import datetime
from itertools import chain
from pathlib import Path

from django.conf import settings
from django.core import serializers
from django.core.management.color import no_style
from django.core.serializers.base import DeserializationError
from django.db import connection, transaction

from apps.catalog.models import ExclusionRule, MerchantMap, Reseller, ResellerAlias
from apps.core.models import AppSettings
from apps.ingest.models import BankMutation, DebitIgnored, ExcludedTransaction, ImportBatch, OtomaxEntry
from apps.recon.models import Adjustment, Discrepancy, Match, ReconDay

SAFETY_BACKUP_PREFIX = "auto-sebelum-restore"


def backup_dir() -> Path:
    # Dihitung tiap kali dipanggil (bukan konstanta modul) supaya menghormati
    # override settings.MEDIA_ROOT saat test, dan perubahan setting saat runtime.
    return Path(settings.MEDIA_ROOT) / "backups"


# Urutan INSERT saat restore -- parent dulu, supaya FK selalu ketemu barisnya.
# (OtomaxEntry.net_pair dikecualikan dari urutan ini: dia FK ke dirinya sendiri,
# ditangani terpisah lewat dua pass di restore_from_json.)
_RESTORE_ORDER = (
    Reseller,
    ResellerAlias,
    MerchantMap,
    ExclusionRule,
    AppSettings,
    ImportBatch,
    BankMutation,
    OtomaxEntry,
    DebitIgnored,
    ExcludedTransaction,
    ReconDay,
    Match,
    Discrepancy,
    Adjustment,
)

# Urutan HAPUS saat restore -- anak dulu (kebalikan dari _RESTORE_ORDER), supaya tidak
# kena FK PROTECT (mis. MerchantMap.reseller, Adjustment.discrepancy).
_DELETE_ORDER = tuple(reversed(_RESTORE_ORDER))


class RestoreError(Exception):
    pass


def _ordered_querysets():
    for model in _RESTORE_ORDER:
        yield model.objects.all().order_by("pk")


def generate_backup_json() -> str:
    return serializers.serialize("json", chain.from_iterable(_ordered_querysets()), indent=2)


def backup_filename() -> str:
    return f"rekonmutasi-backup-{datetime.now():%Y%m%d-%H%M%S}.json"


def save_safety_backup() -> Path:
    """Snapshot data SEKARANG sebelum restore menghapusnya, supaya bisa dipulihkan
    lagi kalau file yang diupload ternyata salah."""
    target_dir = backup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{SAFETY_BACKUP_PREFIX}-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(generate_backup_json(), encoding="utf-8")
    return path


def list_safety_backups() -> list[Path]:
    target_dir = backup_dir()
    if not target_dir.exists():
        return []
    return sorted(target_dir.glob(f"{SAFETY_BACKUP_PREFIX}-*.json"), reverse=True)


def preview_backup_file(content: str) -> dict[str, int]:
    """Parse & validasi file backup TANPA menyentuh database sama sekali."""
    try:
        deserialized = list(serializers.deserialize("json", content))
    except (DeserializationError, json.JSONDecodeError) as exc:
        raise RestoreError(f"File backup tidak valid atau rusak: {exc}") from exc

    if not deserialized:
        raise RestoreError("File backup kosong -- tidak ada data untuk direstore.")

    counts: dict[str, int] = {}
    for obj in deserialized:
        label = obj.object.__class__.__name__
        counts[label] = counts.get(label, 0) + 1
    return counts


@transaction.atomic
def restore_from_json(content: str, *, make_safety_backup: bool = True) -> dict:
    """Ganti TOTAL isi tabel dalam scope backup dengan isi file. Selalu bikin safety
    backup dulu (kecuali make_safety_backup=False) supaya bisa dibatalkan kalau file
    yang diupload salah. Dibungkus transaction.atomic supaya kalau gagal di tengah
    jalan, database kembali ke kondisi semula (tidak ada restore setengah jalan)."""
    counts = preview_backup_file(content)  # validasi & parse dulu, sebelum apa pun dihapus

    safety_backup_path = save_safety_backup() if make_safety_backup else None

    for model in _DELETE_ORDER:
        model.objects.all().delete()

    deserialized = list(serializers.deserialize("json", content))
    pending_net_pairs: list[tuple[int, int]] = []
    for obj in deserialized:
        instance = obj.object
        if isinstance(instance, OtomaxEntry) and instance.net_pair_id:
            pending_net_pairs.append((instance.pk, instance.net_pair_id))
            instance.net_pair_id = None
        obj.save()

    for pk, net_pair_id in pending_net_pairs:
        OtomaxEntry.objects.filter(pk=pk).update(net_pair_id=net_pair_id)

    # Reset sequence PK (Postgres) supaya baris baru setelah restore tidak tabrakan
    # dengan PK yang baru saja di-restore -- sama seperti yang dilakukan `loaddata`.
    reset_sql = connection.ops.sequence_reset_sql(no_style(), _RESTORE_ORDER)
    if reset_sql:
        with connection.cursor() as cursor:
            for statement in reset_sql:
                cursor.execute(statement)

    return {
        "counts": counts,
        "safety_backup_path": str(safety_backup_path) if safety_backup_path else None,
    }
