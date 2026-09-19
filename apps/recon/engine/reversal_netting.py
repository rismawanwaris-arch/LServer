"""Netralkan baris REVERSAL Otomax dengan entri lawan yang dibatalkannya, sebelum
pencocokan ref/nominal biasa berjalan."""

from __future__ import annotations

from django.db import models

from apps.core.enums import MatchStatus, OtomaxCategory
from apps.ingest.models import OtomaxEntry

from .helpers import _OPEN_STATUSES


def _net_reversals() -> int:
    """Netkan baris REVERSAL dgn entri lawan (TOPUP_TARTUN atau REVERSAL lain) yang dibatalkannya.

    OTOMAX kadang mencatat topup ke reseller yang salah, membalikkannya lewat baris
    "REV ..." (ref & nominal sama, tanda berlawanan, nama reseller sama persis dengan
    yang dibatalkan), lalu mengentri ulang nominal yang sama ke reseller yang benar.
    Kadang koreksinya berlapis (REV dibalas REV lagi sebelum entri final) — dua REV yang
    saling berlawanan itu juga perlu dinetralkan satu sama lain, bukan cuma REV vs
    TOPUP_TARTUN. Pasangan yang bernilai nol itu tidak boleh ikut bersaing memperebutkan
    satu mutasi bank dengan entri revisiannya — jadi dikeluarkan dari kandidat pencocokan
    (match_status=IGNORED) sebelum pencocokan ref/nominal berjalan.
    """
    netted = 0
    reversals = OtomaxEntry.objects.filter(category=OtomaxCategory.REVERSAL, match_status__in=_OPEN_STATUSES).order_by(
        "entry_datetime"
    )
    for rev in reversals:
        base = OtomaxEntry.objects.filter(
            category__in=[OtomaxCategory.TOPUP_TARTUN, OtomaxCategory.REVERSAL],
            match_status__in=_OPEN_STATUSES,
            amount=-rev.amount,
            reseller_name_raw=rev.reseller_name_raw,
        ).exclude(pk=rev.pk)
        if rev.entry_datetime:
            base = base.filter(models.Q(entry_datetime__lte=rev.entry_datetime) | models.Q(entry_datetime__isnull=True))

        # ref_core (token angka inti) lebih tahan terhadap variasi teks — mis. OTOMAX
        # kadang menambah akhiran "TGL dd/bln/yyyy" hanya pada baris REV/revisian,
        # tidak pada entri aslinya, sehingga ref_normalized-nya jadi tidak identik.
        original = None
        if rev.ref_core:
            original = base.filter(ref_core=rev.ref_core).order_by("-entry_datetime").first()
        if original is None and rev.ref_normalized:
            original = base.filter(ref_normalized=rev.ref_normalized).order_by("-entry_datetime").first()
        if original is None:
            continue

        rev.match_status = MatchStatus.IGNORED
        rev.net_pair = original
        rev.note = f"Menetralkan OtomaxEntry #{original.id} ({original.amount})"
        rev.save(update_fields=["match_status", "net_pair", "note"])

        original.match_status = MatchStatus.IGNORED
        original.net_pair = rev
        original.note = f"Dibatalkan oleh REV OtomaxEntry #{rev.id}"
        original.save(update_fields=["match_status", "net_pair", "note"])
        netted += 1
    return netted
