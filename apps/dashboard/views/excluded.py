"""Halaman: Data Dikecualikan — melihat dan memulihkan transaksi non-engine (manual upload / aturan filter)."""

from __future__ import annotations

from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.enums import Channel
from apps.ingest.models import ExcludedTransaction
from apps.ingest.services import restore_excluded_transaction
from apps.recon.models import ReconDay

from ._shared import _parse_date

PAGE_SIZE = 50
ZERO = Decimal("0.00")


@login_required
def excluded_transactions_view(request):
    book_date = _parse_date(request.GET.get("d"))
    selected_channel = request.GET.get("channel", "")
    source_filter = request.GET.get("source", "all")  # 'all', 'manual', 'rule'
    query = request.GET.get("q", "").strip()
    page_number = request.GET.get("page", 1)

    base_qs = ExcludedTransaction.objects.filter(book_date=book_date).select_related("import_batch", "rule")

    # Tab counts per channel untuk tanggal ini
    channel_counts = {}
    channel_totals = {}
    for ch_val in ["ALL", Channel.BRI, Channel.BCA, Channel.MANDIRI, Channel.MERCHANT_BCA, Channel.OTOMAX]:
        qs_ch = base_qs if ch_val == "ALL" else base_qs.filter(channel=ch_val)
        c = qs_ch.count()
        t = qs_ch.aggregate(s=Sum("amount"))["s"] or ZERO
        channel_counts[ch_val] = c
        channel_totals[ch_val] = t

    filtered_qs = base_qs
    if selected_channel and selected_channel in Channel.values:
        filtered_qs = filtered_qs.filter(channel=selected_channel)

    if source_filter == "manual":
        filtered_qs = filtered_qs.filter(category="MANUAL_UPLOAD")
    elif source_filter == "rule":
        filtered_qs = filtered_qs.filter(rule__isnull=False)

    if query:
        filtered_qs = filtered_qs.filter(
            Q(party_raw__icontains=query)
            | Q(description_raw__icontains=query)
            | Q(reason__icontains=query)
            | Q(category__icontains=query)
        )

    filtered_qs = filtered_qs.order_by("-txn_datetime", "-id")

    paginator = Paginator(filtered_qs, PAGE_SIZE)
    page_obj = paginator.get_page(page_number)
    items = list(page_obj.object_list)

    # Status hari (apakah locked)
    day = ReconDay.objects.filter(book_date=book_date).first()
    is_locked = bool(day and day.locked)

    context = {
        "book_date": book_date,
        "selected_channel": selected_channel,
        "channels": Channel.choices,
        "source_filter": source_filter,
        "query": query,
        "channel_counts": channel_counts,
        "channel_totals": channel_totals,
        "total_count": paginator.count,
        "total_amount": filtered_qs.aggregate(s=Sum("amount"))["s"] or ZERO,
        "items": items,
        "page_obj": page_obj,
        "is_locked": is_locked,
    }

    if request.headers.get("HX-Request"):
        return render(request, "dashboard/_excluded_rows.html", context)

    return render(request, "dashboard/excluded_transactions.html", context)


@login_required
@require_POST
def restore_excluded_transaction_action(request, pk: int):
    excluded_tx = get_object_or_404(ExcludedTransaction, pk=pk)
    fallback_url = f"/dikecualikan/?d={excluded_tx.book_date}"
    next_url = request.POST.get("next_url") or request.META.get("HTTP_REFERER") or fallback_url
    try:
        obj = restore_excluded_transaction(excluded_tx, user=request.user)
        channel_label = obj.channel if hasattr(obj, "channel") else "Otomax"
        messages.success(
            request,
            f"Transaksi Rp {obj.amount:,.0f} ({channel_label}) berhasil dipulihkan ke antrean rekonsiliasi aktif.",
        )
    except ValueError as exc:
        messages.error(request, str(exc))
    except Exception as exc:
        messages.error(request, f"Gagal memulihkan transaksi: {exc}")

    return redirect(next_url)


@login_required
@require_POST
def restore_excluded_bulk_action(request):
    selected_ids = request.POST.getlist("selected_ids")
    next_url = request.POST.get("next_url") or request.META.get("HTTP_REFERER") or "/dikecualikan/"
    if not selected_ids:
        messages.warning(request, "Tidak ada transaksi yang dipilih untuk dipulihkan.")
        return redirect(next_url)

    restored = 0
    errors = []
    for tx_id in selected_ids:
        try:
            tx = ExcludedTransaction.objects.filter(pk=tx_id).first()
            if tx:
                restore_excluded_transaction(tx, user=request.user)
                restored += 1
        except Exception as exc:
            errors.append(str(exc))

    if restored > 0:
        messages.success(request, f"Berhasil memulihkan {restored} transaksi ke antrean rekonsiliasi aktif.")
    if errors:
        messages.error(request, f"Sebagian gagal dipulihkan: {errors[0]}")

    return redirect(next_url)
