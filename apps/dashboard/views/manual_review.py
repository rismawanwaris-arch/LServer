"""Halaman 4: Antrean Review Manual — mutasi bank yang belum cocok atau sudah di-tag manual."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.enums import Channel, ManualTag, MatchStatus, OtomaxCategory
from apps.ingest.models import BankMutation, OtomaxEntry
from apps.recon.models import Match
from apps.recon.resolve import manual_pair_transactions, tag_manual_mutation, unpair_match

from ._shared import _find_auto_pairs, _parse_date


@login_required
def manual_review_view(request):
    book_date = _parse_date(request.GET.get("d"))
    tab = request.GET.get("tab", "unmatched")  # 'unmatched' or 'tagged'
    channel = request.GET.get("channel", "")

    qs = BankMutation.objects.filter(book_date=book_date)
    if channel and channel in Channel.values:
        qs = qs.filter(channel=channel)

    unmatched_qs = qs.filter(match_status=MatchStatus.UNMATCHED)
    tagged_qs = qs.filter(match_status=MatchStatus.MANUAL).prefetch_related(
        models.Prefetch(
            "matches",
            queryset=Match.objects.filter(voided_at__isnull=True).select_related("otomax_entry"),
            to_attr="active_matches",
        )
    )

    unmatched_count = unmatched_qs.count()
    unmatched_total = unmatched_qs.aggregate(t=models.Sum("amount"))["t"] or Decimal("0.00")

    tagged_count = tagged_qs.count()
    tagged_total = tagged_qs.aggregate(t=models.Sum("amount"))["t"] or Decimal("0.00")

    # Daftar entri Otomax belum cocok untuk opsi pencocokan manual
    # (Hanya transaksi yang belum selesai, bukan potongan admin)
    unmatched_otomax = list(
        OtomaxEntry.objects.filter(
            book_date__gte=book_date - timedelta(days=2),
            book_date__lte=book_date + timedelta(days=1),
            match_status__in=[MatchStatus.PENDING_SETTLE, MatchStatus.UNMATCHED],
        )
        .exclude(category=OtomaxCategory.ADMIN)
        .order_by("-amount")
    )

    if tab == "tagged":
        items = list(tagged_qs.order_by("-updated_at"))
    else:
        items = list(unmatched_qs.order_by("-amount"))

    auto_pairs = _find_auto_pairs(items if tab != "tagged" else [], unmatched_otomax)
    auto_pairable_count = len(auto_pairs)

    return render(
        request,
        "dashboard/manual_review.html",
        {
            "book_date": book_date,
            "tab": tab,
            "selected_channel": channel,
            "channels": Channel.choices,
            "items": items,
            "unmatched_count": unmatched_count,
            "unmatched_total": unmatched_total,
            "tagged_count": tagged_count,
            "tagged_total": tagged_total,
            "manual_tags": ManualTag.choices,
            "unmatched_otomax": unmatched_otomax,
            "auto_pairable_count": auto_pairable_count,
        },
    )


@login_required
@require_POST
def manual_tag_action(request, pk: int):
    bm = get_object_or_404(BankMutation, pk=pk)
    tag = request.POST.get("tag")
    note = request.POST.get("note", "").strip()
    book_date = request.POST.get("book_date") or bm.book_date.isoformat()
    fallback_url = f"/review-manual/?d={book_date}"
    next_url = request.POST.get("next_url") or request.META.get("HTTP_REFERER") or fallback_url

    if not tag or tag not in ManualTag.values:
        messages.error(request, "Pilih tag kategori yang valid.")
        return redirect(next_url)

    tag_manual_mutation(bm, tag=tag, note=note, user=request.user)
    messages.success(request, f"Mutasi Rp {bm.amount:,.0f} berhasil di-tag sebagai '{bm.get_tag_manual_display()}'.")
    return redirect(next_url)


@login_required
@require_POST
def manual_match_action(request):
    otomax_id = request.POST.get("otomax_id")
    bank_id = request.POST.get("bank_id")
    note = request.POST.get("note", "").strip()
    book_date = request.POST.get("book_date")
    fallback_url = f"/pending-settle/?d={book_date}" if book_date else "/"
    next_url = request.POST.get("next_url") or request.META.get("HTTP_REFERER") or fallback_url

    if not otomax_id or not bank_id:
        messages.error(request, "Pilih transaksi Otomax dan mutasi Bank yang akan dicocokkan.")
        return redirect(next_url)

    bm = get_object_or_404(BankMutation, pk=bank_id)
    o = get_object_or_404(OtomaxEntry, pk=otomax_id)

    try:
        manual_pair_transactions(bank_mutation=bm, otomax_entry=o, note=note, user=request.user)
        messages.success(
            request,
            f"Berhasil mencocokkan Otomax '{o.reseller_name_raw}' (Rp {o.amount:,.0f}) "
            f"dengan mutasi {bm.channel} (Rp {bm.amount:,.0f}).",
        )
    except ValueError as e:
        messages.error(request, str(e))
    except Exception as e:
        messages.error(request, f"Gagal mencocokkan: {e}")

    return redirect(next_url)


@login_required
@require_POST
def unpair_match_action(request, pk: int):
    match = get_object_or_404(Match, pk=pk)
    next_url = request.POST.get("next_url") or request.META.get("HTTP_REFERER") or f"/matches/?d={match.book_date}"

    try:
        unpair_match(match, user=request.user)
        messages.success(request, f"Pencocokan #{match.id} berhasil dibatalkan.")
    except Exception as e:
        messages.error(request, f"Gagal membatalkan pencocokan: {e}")

    return redirect(next_url)


@login_required
@require_POST
def bulk_manual_match_action(request):
    book_date = _parse_date(request.POST.get("book_date"))
    next_url = request.POST.get("next_url") or request.META.get("HTTP_REFERER") or f"/pending-settle/?d={book_date}"

    unmatched_banks = list(
        BankMutation.objects.filter(
            book_date__gte=book_date - timedelta(days=2),
            book_date__lte=book_date + timedelta(days=1),
            match_status=MatchStatus.UNMATCHED,
        )
    )
    pending_otomax = list(
        OtomaxEntry.objects.filter(
            book_date__gte=book_date - timedelta(days=2),
            book_date__lte=book_date + timedelta(days=1),
            match_status__in=[MatchStatus.PENDING_SETTLE, MatchStatus.UNMATCHED],
        )
    )

    pairs = _find_auto_pairs(unmatched_banks, pending_otomax)
    matched_count = 0
    for bm, oe, note in pairs:
        try:
            manual_pair_transactions(
                bank_mutation=bm,
                otomax_entry=oe,
                note=note,
                user=request.user,
            )
            matched_count += 1
        except Exception:
            pass

    if matched_count > 0:
        messages.success(
            request,
            f"Berhasil memasangkan {matched_count} transaksi secara otomatis berdasarkan referensi yang cocok!",
        )
    else:
        messages.info(request, "Tidak ditemukan transaksi dengan referensi pasangan yang cocok.")

    return redirect(next_url)
