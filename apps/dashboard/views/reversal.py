"""Halaman Monitor Reversal Otomax — baris REV yang harus saling menetralkan dengan
entri topup yang dibatalkannya, dan status pencocokannya (otomatis atau perlu dicek manual)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.enums import MatchStatus, OtomaxCategory
from apps.ingest.models import BankMutation, OtomaxEntry
from apps.recon.models import Match
from apps.recon.resolve import manual_net_reversal

from ._shared import _parse_date

_OPEN_STATUSES = [MatchStatus.UNMATCHED, MatchStatus.PENDING_SETTLE]
_MAX_CANDIDATES = 5
_MAX_NETTED_ROWS = 300
_BANK_CANDIDATE_WINDOW = (-2, 1)  # hari, sama seperti jendela di Pending Settle/Review Manual


def _active_match_for(otomax_entry: OtomaxEntry) -> Match | None:
    return (
        Match.objects.filter(
            models.Q(otomax_entry=otomax_entry) | models.Q(otomax_entries=otomax_entry), voided_at__isnull=True
        )
        .select_related("bank_mutation")
        .first()
    )


def _candidates_for_reversal(rev: OtomaxEntry) -> list[tuple[OtomaxEntry, Match | None]]:
    """Kandidat entri asli yang mungkin dibatalkan REV ini. Sengaja TIDAK dibatasi ke
    status terbuka saja — entri yang sudah MATCHED juga ditampilkan (lengkap dengan Match
    aktifnya) supaya kelihatan kenapa auto-netting gagal (mis. originalnya sudah lanjut
    dicocokkan ke bank duluan sebelum REV-nya diproses), dan bisa dibatalkan langsung dari
    halaman ini kalau memang pencocokannya keliru."""
    qs = (
        OtomaxEntry.objects.filter(
            category__in=[OtomaxCategory.TOPUP_TARTUN, OtomaxCategory.REVERSAL],
            amount=-rev.amount,
        )
        .exclude(pk=rev.pk)
        .exclude(match_status=MatchStatus.IGNORED)
    )

    def score(o: OtomaxEntry) -> int:
        if rev.ref_core and o.ref_core == rev.ref_core:
            return 3
        if rev.ref_normalized and o.ref_normalized == rev.ref_normalized:
            return 2
        if o.reseller_name_raw == rev.reseller_name_raw:
            return 1
        return 0

    scored = sorted(qs, key=lambda o: (-score(o), o.id))[:_MAX_CANDIDATES]
    matched_statuses = (MatchStatus.MATCHED, MatchStatus.MANUAL)
    return [(o, _active_match_for(o) if o.match_status in matched_statuses else None) for o in scored]


def _unmatched_banks_for(rev: OtomaxEntry) -> list[BankMutation]:
    """Mutasi bank UNMATCHED di sekitar tanggal REV ini, buat opsi 'Pencocokan Manual ke
    Bank' kalau REV-nya ternyata bukan koreksi internal murni tapi memang ada uang bank
    yang perlu dipasangkan langsung (mis. refund nyata dari bank)."""
    start_d = rev.book_date + timedelta(days=_BANK_CANDIDATE_WINDOW[0])
    end_d = rev.book_date + timedelta(days=_BANK_CANDIDATE_WINDOW[1])
    return list(
        BankMutation.objects.filter(
            book_date__range=(start_d, end_d), match_status=MatchStatus.UNMATCHED
        ).order_by("-amount")
    )


@login_required
def reversal_view(request):
    book_date = request.GET.get("d") or ""
    tab = request.GET.get("tab", "belum")  # 'belum' or 'netted'

    belum_qs = OtomaxEntry.objects.filter(category=OtomaxCategory.REVERSAL, match_status__in=_OPEN_STATUSES)
    netted_qs = OtomaxEntry.objects.filter(
        category=OtomaxCategory.REVERSAL, match_status=MatchStatus.IGNORED, net_pair__isnull=False
    ).select_related("net_pair")

    if book_date:
        parsed = _parse_date(book_date)
        belum_qs = belum_qs.filter(book_date=parsed)
        netted_qs = netted_qs.filter(book_date=parsed)

    belum_count = belum_qs.count()
    belum_total = belum_qs.aggregate(t=models.Sum("amount"))["t"] or Decimal("0.00")
    netted_count = netted_qs.count()
    netted_total = netted_qs.aggregate(t=models.Sum("amount"))["t"] or Decimal("0.00")

    if tab == "netted":
        items = list(netted_qs.order_by("-updated_at")[:_MAX_NETTED_ROWS])
    else:
        items = [
            (rev, _candidates_for_reversal(rev), _unmatched_banks_for(rev))
            for rev in belum_qs.order_by("-entry_datetime")
        ]

    return render(
        request,
        "dashboard/reversal.html",
        {
            "book_date": book_date,
            "tab": tab,
            "items": items,
            "belum_count": belum_count,
            "belum_total": belum_total,
            "netted_count": netted_count,
            "netted_total": netted_total,
        },
    )


@login_required
@require_POST
def manual_net_reversal_action(request):
    rev_id = request.POST.get("rev_id")
    original_id = request.POST.get("original_id")
    note = request.POST.get("note", "").strip()
    book_date = request.POST.get("book_date", "")
    fallback_url = f"/reversal/?d={book_date}" if book_date else "/reversal/"
    next_url = request.POST.get("next_url") or request.META.get("HTTP_REFERER") or fallback_url

    rev = get_object_or_404(OtomaxEntry, pk=rev_id)
    original = get_object_or_404(OtomaxEntry, pk=original_id)

    try:
        manual_net_reversal(rev, original, note=note, user=request.user)
        messages.success(
            request,
            f"Berhasil menetralkan REV '{rev.reseller_name_raw}' (Rp {rev.amount:,.0f}) "
            f"dengan entri #{original.id} (Rp {original.amount:,.0f}).",
        )
    except ValueError as e:
        messages.error(request, str(e))

    return redirect(next_url)
