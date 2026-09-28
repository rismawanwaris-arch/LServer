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
from apps.core.normalize import parse_tgl_date
from apps.ingest.models import BankMutation, OtomaxEntry
from apps.recon.models import Match
from apps.recon.resolve import tag_manual_otomax

from ._shared import _CANDIDATE_WINDOW, _EXTENDED_WINDOW_DAYS, _find_auto_pairs, _parse_date


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
    window = (book_date + timedelta(days=_CANDIDATE_WINDOW[0]), book_date + timedelta(days=_CANDIDATE_WINDOW[1]))
    unmatched_banks = list(
        BankMutation.objects.filter(
            book_date__range=window,
            match_status=MatchStatus.UNMATCHED,
        ).order_by("-amount", "-txn_datetime")
    )

    # Mutasi bank dari hasil cocok yang masih ada sisa selisih -- DUA arah: Otomax kurang
    # (diff > 0) maupun Otomax lebih (diff < 0, mis. operator mengoreksi 3.540.000 dengan
    # menembak -90.000 alih-alih membalik lalu mengentri ulang 3.450.000).
    diff_matches = list(
        Match.objects.filter(
            book_date__range=window,
            voided_at__isnull=True,
            bank_mutation__isnull=False,
        )
        .exclude(amount_diff=Decimal("0.00"))
        .select_related("bank_mutation", "otomax_entry")
        .order_by("-amount_diff")
    )

    # Pencocokan Cepat sengaja tetap cuma memakai jendela normal (perilaku lama).
    auto_pairs = _find_auto_pairs(unmatched_banks, list(pending_qs) if tab != "resolved" else [])
    auto_pairable_count = len(auto_pairs)

    for o in items:
        o.tgl_date = parse_tgl_date(o.description_raw)
    for b in unmatched_banks:
        b.is_extended = False
    if tab != "resolved" and items:
        # Kandidat dari luar jendela normal (entri Otomax yang dientri beberapa hari telat):
        # cuma yang nominalnya persis sama dengan salah satu entri di halaman ini, atau yang
        # tanggalnya disebut operator lewat "TGL ..." di keterangan Otomax.
        amounts = {o.amount for o in items} | {abs(o.amount) for o in items}
        tgl_dates = {o.tgl_date for o in items if o.tgl_date}
        wide = (book_date - timedelta(days=_EXTENDED_WINDOW_DAYS), book_date + timedelta(days=_EXTENDED_WINDOW_DAYS))
        extended = (
            BankMutation.objects.filter(book_date__range=wide, match_status=MatchStatus.UNMATCHED)
            .exclude(book_date__range=window)
            .filter(models.Q(amount__in=amounts) | models.Q(book_date__in=tgl_dates))
            .order_by("-amount", "-txn_datetime")
        )
        for b in extended:
            b.is_extended = True
            unmatched_banks.append(b)

        # "Masih Selisih" dari luar jendela normal: koreksi Otomax sering baru ditembak
        # beberapa hari kemudian (mis. -10.000 untuk pasangan 1 Sep yang salah nominal).
        # Cuma yang sisa selisihnya PERSIS sama dengan nominal entri di halaman ini --
        # yang kalau digabung, selisihnya jadi Rp 0.
        extended_diffs = (
            Match.objects.filter(
                book_date__range=wide,
                voided_at__isnull=True,
                bank_mutation__isnull=False,
                amount_diff__in={o.amount for o in items},
            )
            .exclude(book_date__range=window)
            .exclude(amount_diff=Decimal("0.00"))
            .select_related("bank_mutation", "otomax_entry")
            .order_by("-amount_diff")
        )
        for m in extended_diffs:
            m.is_extended = True
            diff_matches.append(m)

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
