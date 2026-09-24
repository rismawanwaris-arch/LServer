"""Halaman 5: Pending Settle — entri Otomax yang belum (atau sudah, tab "resolved") ketemu
pasangan mutasi bank."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.enums import Channel, MatchStatus, OtomaxCategory
from apps.ingest.models import BankMutation, OtomaxEntry
from apps.recon.models import Match
from apps.recon.resolve import tag_manual_otomax

from ._shared import _find_auto_pairs, _parse_date


@login_required
def pending_settle_view(request):
    book_date = _parse_date(request.GET.get("d"))
    channel = request.GET.get("channel", "")
    tab = request.GET.get("tab", "pending")  # 'pending' or 'resolved'

    base_qs = OtomaxEntry.objects.filter(
        book_date=book_date,
    ).exclude(category=OtomaxCategory.ADMIN)
    if channel and channel in Channel.values:
        base_qs = base_qs.filter(channel_hint=channel)

    pending_qs = base_qs.filter(
        match_status__in=[MatchStatus.PENDING_SETTLE, MatchStatus.UNMATCHED],
    )
    resolved_qs = base_qs.filter(
        match_status=MatchStatus.MANUAL,
    ).prefetch_related(
        models.Prefetch(
            "matches",
            queryset=Match.objects.filter(voided_at__isnull=True).select_related("bank_mutation"),
            to_attr="active_matches",
        )
    )

    pending_count = pending_qs.count()
    pending_total = pending_qs.aggregate(t=models.Sum("amount"))["t"] or Decimal("0.00")

    resolved_count = resolved_qs.count()
    resolved_total = resolved_qs.aggregate(t=models.Sum("amount"))["t"] or Decimal("0.00")

    if tab == "resolved":
        items = list(resolved_qs.order_by("-updated_at"))
    else:
        items = list(pending_qs.order_by("-amount"))

    # Daftar mutasi bank yang belum cocok untuk kandidat pencocokan manual
    unmatched_banks = list(
        BankMutation.objects.filter(
            book_date__gte=book_date - timedelta(days=2),
            book_date__lte=book_date + timedelta(days=1),
            match_status=MatchStatus.UNMATCHED,
        ).order_by("-amount", "-txn_datetime")
    )

    # Mutasi bank dari hasil cocok yang masih ada sisa selisih (amount_diff > 0)
    diff_matches = list(
        Match.objects.filter(
            book_date__gte=book_date - timedelta(days=2),
            book_date__lte=book_date + timedelta(days=1),
            voided_at__isnull=True,
            bank_mutation__isnull=False,
            amount_diff__gt=Decimal("0.00"),
        )
        .select_related("bank_mutation", "otomax_entry")
        .order_by("-amount_diff")
    )

    auto_pairs = _find_auto_pairs(unmatched_banks, list(pending_qs) if tab != "resolved" else [])
    auto_pairable_count = len(auto_pairs)

    otomax_tags = [
        ("revisi", "Revisi / Koreksi Kasir"),
        ("retur", "Retur / Tarik Tunai"),
        ("setor_tunai", "Setor Tunai Langsung"),
        ("batal", "Dibatalkan / Void"),
        ("lainnya", "Lain-lain"),
    ]

    return render(
        request,
        "dashboard/pending_settle.html",
        {
            "book_date": book_date,
            "selected_channel": channel,
            "channels": Channel.choices,
            "tab": tab,
            "items": items,
            "pending_count": pending_count,
            "pending_total": pending_total,
            "resolved_count": resolved_count,
            "resolved_total": resolved_total,
            "unmatched_banks": unmatched_banks,
            "diff_matches": diff_matches,
            "auto_pairable_count": auto_pairable_count,
            "otomax_tags": otomax_tags,
        },
    )


@login_required
@require_POST
def manual_tag_otomax_action(request, pk: int):
    o = get_object_or_404(OtomaxEntry, pk=pk)
    tag = request.POST.get("tag")
    note = request.POST.get("note", "").strip()
    book_date = request.POST.get("book_date") or o.book_date.isoformat()
    fallback_url = f"/pending-settle/?d={book_date}"
    next_url = request.POST.get("next_url") or request.META.get("HTTP_REFERER") or fallback_url

    if not tag:
        messages.error(request, "Pilih tag kategori yang valid.")
        return redirect(next_url)

    tag_manual_otomax(o, tag=tag, note=note, user=request.user)
    messages.success(
        request,
        f"Transaksi Otomax '{o.reseller_name_raw}' (Rp {o.amount:,.0f}) berhasil di-tag sebagai '{tag}'.",
    )
    return redirect(next_url)
