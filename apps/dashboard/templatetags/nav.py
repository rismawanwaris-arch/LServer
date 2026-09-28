"""Sidebar & bilah atas base.html: daftar menu, jumlah pekerjaan per menu, pemilih tanggal
‹ › dan status hari. Dibuat sebagai inclusion tag (bukan context processor) supaya
(1) ikut membaca `book_date` milik halaman yang sedang dirender, dan (2) query hitungannya
cuma jalan saat base.html dirender -- bukan di tiap partial HTMX."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from django import template
from django.urls import reverse

from apps.core.enums import DiscrepancyStatus, MatchStatus, OtomaxCategory
from apps.ingest.models import BankMutation, OtomaxEntry
from apps.recon.models import Discrepancy, Match, ReconDay

register = template.Library()

_OPEN = [MatchStatus.UNMATCHED, MatchStatus.PENDING_SETTLE]

# Halaman yang membaca ?d= -- link sidebar-nya membawa tanggal kerja, dan bilah atasnya
# menampilkan pemilih tanggal ‹ ›.
_DATED = {"day", "upload", "manual-review", "pending-settle", "reversal", "discrepancy-list", "matches", "reports"}
_STEPPER = _DATED - {"reports"}
# Halaman yang juga punya mode "semua tanggal" (tanpa ?d=): bilah atasnya harus bilang
# "Semua tanggal", bukan tanggal terakhir dari sesi -- kalau tidak, judul dan isi rancu.
_OPTIONAL_DATE = {"reversal", "discrepancy-list"}


@dataclass
class NavItem:
    url_name: str
    label: str
    icon: str
    count: int = 0
    tone: str = "mute"  # mute | warn | crit | info
    danger: bool = False
    external: str = ""


def _menu(user) -> list[tuple[str, list[NavItem]]]:
    sistem = [
        NavItem("exclusion-rules", "Aturan Filter", "filter"),
        NavItem("reseller-list", "Kode Reseller", "users"),
    ]
    if user.is_superuser:
        sistem += [
            NavItem("app-settings", "Pengaturan", "sliders"),
            NavItem("data-admin", "Reset & Purge Data", "trash", danger=True),
            NavItem("backup", "Backup & Restore", "db"),
        ]
    sistem.append(NavItem("", "Django Admin", "external", external="/admin/"))
    return [
        ("Mulai di sini", [NavItem("day", "Dashboard", "dash"), NavItem("upload", "Upload Data", "upload")]),
        (
            "Tinjau & selesaikan",
            [
                NavItem("manual-review", "Review Manual", "search", tone="warn"),
                NavItem("pending-settle", "Pending Settle", "clock", tone="warn"),
                NavItem("reversal", "Reversal Otomax", "swap"),
                NavItem("discrepancy-list", "Daftar Selisih", "alert", tone="crit"),
                NavItem("matches", "Hasil Cocok", "checks", tone="info"),
            ],
        ),
        ("Laporan", [NavItem("reports", "Laporan Rekonsiliasi", "file")]),
        ("Sistem", sistem),
    ]


def _work_date(context) -> date | None:
    """Tanggal kerja: book_date milik halaman (bisa date, string, atau kosong), lalu
    tanggal terakhir yang diingat sesi (context processor last_book_date)."""
    for raw in (context.get("book_date"), context.get("nav_book_date")):
        if isinstance(raw, date):
            return raw
        if raw:
            try:
                return date.fromisoformat(str(raw))
            except ValueError:
                continue
    return None


def _parse(raw) -> date | None:
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _counts(d: date) -> dict[str, int]:
    return {
        "manual-review": BankMutation.objects.filter(book_date=d, match_status=MatchStatus.UNMATCHED).count(),
        "pending-settle": OtomaxEntry.objects.filter(book_date=d, match_status__in=_OPEN)
        .exclude(category=OtomaxCategory.ADMIN)
        .count(),
        "reversal": OtomaxEntry.objects.filter(
            book_date=d, category=OtomaxCategory.REVERSAL, match_status__in=_OPEN
        ).count(),
        "discrepancy-list": Discrepancy.objects.filter(origin_book_date=d, status=DiscrepancyStatus.OPEN).count(),
        # Hasil Cocok: usulan mesin yang menunggu persetujuan operator.
        "matches": Match.objects.filter(book_date=d, needs_review=True, voided_at__isnull=True).count(),
    }


def _with_date(request, d: date) -> str:
    params = request.GET.copy()
    params["d"] = d.isoformat()
    params.pop("page", None)
    return f"{request.path}?{params.urlencode()}"


@register.inclusion_tag("dashboard/_sidebar.html", takes_context=True)
def sidebar_nav(context):
    request = context["request"]
    current = getattr(request.resolver_match, "url_name", "")
    d = _work_date(context)
    counts = _counts(d) if d else {}
    groups = []
    for title, items in _menu(context["user"]):
        rows = []
        for it in items:
            if it.external:
                href = it.external
            else:
                href = reverse(it.url_name)
                if d and it.url_name in _DATED:
                    href += f"?d={d.isoformat()}"
            it.count = counts.get(it.url_name, 0)
            rows.append({"item": it, "href": href, "active": it.url_name == current})
        groups.append((title, rows))
    return {
        "groups": groups,
        "user": context["user"],
        "home": reverse("day") + (f"?d={d.isoformat()}" if d else ""),
        "csrf_token": context.get("csrf_token"),
    }


@register.inclusion_tag("dashboard/_topbar.html", takes_context=True)
def topbar(context):
    request = context["request"]
    current = getattr(request.resolver_match, "url_name", "")
    section, label = "", ""
    for title, items in _menu(context["user"]):
        for it in items:
            if it.url_name == current:
                section, label = title, it.label
    all_dates = current in _OPTIONAL_DATE and not request.GET.get("d")
    d = _work_date(context) if current in _STEPPER and not all_dates else None
    day = ReconDay.objects.filter(book_date=d).first() if d else None
    return {
        "section": section,
        "label": label,
        "all_dates": all_dates,
        "range_start": _parse(request.GET.get("start_date")) if all_dates else None,
        "range_end": _parse(request.GET.get("end_date")) if all_dates else None,
        "work_date": d,
        "prev_url": _with_date(request, d - timedelta(days=1)) if d else "",
        "next_url": _with_date(request, d + timedelta(days=1)) if d else "",
        "locked": bool(day and day.locked),
    }
