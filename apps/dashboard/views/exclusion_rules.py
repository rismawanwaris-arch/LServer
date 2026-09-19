"""Halaman 7: Pengaturan Aturan Filter (Exclusion Rules) — pemisahan transaksi non-engine."""

from __future__ import annotations

from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.catalog.models import ExclusionCategory, ExclusionRule, ExclusionTarget
from apps.core.enums import Channel
from apps.ingest.models import ExcludedTransaction
from apps.ingest.services import apply_exclusion_rules_retroactive

from ._shared import _parse_date


@login_required
def exclusion_rules_view(request):
    book_date = _parse_date(request.GET.get("d"))
    rules = ExclusionRule.objects.all().order_by("-active", "name")
    excluded_txns = (
        ExcludedTransaction.objects.filter(book_date=book_date)
        .select_related("rule", "import_batch")
        .order_by("-txn_datetime", "-id")
    )
    total_excluded_amount = sum((t.amount for t in excluded_txns), Decimal("0"))
    total_excluded_count = excluded_txns.count()

    return render(
        request,
        "dashboard/exclusion_rules.html",
        {
            "book_date": book_date,
            "rules": rules,
            "excluded_txns": excluded_txns,
            "total_excluded_amount": total_excluded_amount,
            "total_excluded_count": total_excluded_count,
            "target_choices": ExclusionTarget.choices,
            "channel_choices": Channel.choices,
            "category_choices": ExclusionCategory.choices,
        },
    )


@login_required
@require_POST
def add_exclusion_rule_action(request):
    book_date = _parse_date(request.POST.get("book_date"))
    name = request.POST.get("name", "").strip()
    keywords = request.POST.get("keywords", "").strip()
    target = request.POST.get("target", ExclusionTarget.BANK)
    channel = request.POST.get("channel", "").strip()
    category = request.POST.get("category", ExclusionCategory.BIAYA_ADMIN)
    apply_now = request.POST.get("apply_now") == "1"

    if not name or not keywords:
        messages.error(request, "Nama aturan dan kata kunci wajib diisi.")
        return redirect(f"/rules/?d={book_date}")

    rule = ExclusionRule.objects.create(
        name=name,
        keywords=keywords,
        target=target,
        channel=channel,
        category=category,
        active=True,
    )

    moved_msg = ""
    if apply_now:
        res = apply_exclusion_rules_retroactive(book_date)
        if res["total_moved"] > 0:
            moved_msg = f" dan langsung memindahkan {res['total_moved']} transaksi belum cocok."

    messages.success(request, f"Aturan '{rule.name}' berhasil disimpan{moved_msg}")
    return redirect(f"/rules/?d={book_date}")


@login_required
@require_POST
def toggle_exclusion_rule_action(request, pk: int):
    book_date = _parse_date(request.POST.get("book_date"))
    rule = get_object_or_404(ExclusionRule, pk=pk)
    rule.active = not rule.active
    rule.save(update_fields=["active"])
    status_str = "diaktifkan" if rule.active else "dinonaktifkan"
    messages.success(request, f"Aturan '{rule.name}' berhasil {status_str}.")
    return redirect(f"/rules/?d={book_date}")


@login_required
@require_POST
def delete_exclusion_rule_action(request, pk: int):
    book_date = _parse_date(request.POST.get("book_date"))
    rule = get_object_or_404(ExclusionRule, pk=pk)
    name = rule.name
    rule.delete()
    messages.success(request, f"Aturan '{name}' berhasil dihapus.")
    return redirect(f"/rules/?d={book_date}")


@login_required
@require_POST
def apply_exclusion_rules_action(request):
    book_date = _parse_date(request.POST.get("book_date"))
    res = apply_exclusion_rules_retroactive(book_date)
    if res["total_moved"] > 0:
        messages.success(
            request,
            f"Berhasil menerapkan aturan: {res['total_moved']} transaksi dipindahkan ke data dikecualikan "
            f"({res['bank_moved']} mutasi bank, {res['otomax_moved']} data otomax).",
        )
    else:
        messages.info(request, "Tidak ada data belum cocok yang cocok dengan aturan pemisahan aktif.")
    return redirect(f"/rules/?d={book_date}")
