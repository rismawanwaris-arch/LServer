"""Halaman 3: Hasil Rekonsiliasi — daftar Match yang sudah terbentuk untuk satu tanggal buku.

Daftar ini bisa berisi ratusan pasangan (mis. QRIS Merchant BCA per outlet), jadi
ditampilkan bertahap lewat infinite scroll HTMX (lihat _matches_rows.html) alih-alih
merender semua baris sekaligus -- baris berikutnya baru diminta ke server saat baris
"sentinel" di ujung tabel masuk ke viewport (hx-trigger="revealed")."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.enums import Channel
from apps.recon.models import Match
from apps.recon.resolve import approve_match

from ._shared import _parse_date

PAGE_SIZE = 50

# amount_diff = nominal bank - nominal Otomax: plus = bank lebih besar, minus = Otomax lebih besar.
SELISIH_FILTERS = {
    "any": {"exclude": {"amount_diff": 0}},
    "plus": {"filter": {"amount_diff__gt": 0}},
    "minus": {"filter": {"amount_diff__lt": 0}},
}


@login_required
def matches_view(request):
    book_date = _parse_date(request.GET.get("d"))
    channel = request.GET.get("channel", "")
    query = request.GET.get("q", "").strip()
    tab = "review" if request.GET.get("tab") == "review" else "all"
    selisih = request.GET.get("selisih", "")
    if selisih not in SELISIH_FILTERS:
        selisih = ""
    page_number = request.GET.get("page", 1)

    qs = Match.objects.filter(book_date=book_date, voided_at__isnull=True).select_related(
        "bank_mutation", "otomax_entry"
    ).prefetch_related("otomax_entries", "bank_mutations")
    review_count = qs.filter(needs_review=True).count()
    if tab == "review":
        qs = qs.filter(needs_review=True)
    if selisih:
        rule = SELISIH_FILTERS[selisih]
        qs = qs.filter(**rule.get("filter", {})).exclude(**rule.get("exclude", {}))
    if channel and channel in Channel.values:
        qs = qs.filter(channel=channel)
    if query:
        qs = (
            qs.filter(bank_mutation__description_raw__icontains=query)
            | qs.filter(otomax_entry__description_raw__icontains=query)
            | qs.filter(otomax_entry__reseller_name_raw__icontains=query)
        )

    matches = qs.order_by("-match_type", "-amount_bank")
    paginator = Paginator(matches, PAGE_SIZE)
    page_obj = paginator.get_page(page_number)

    context = {
        "book_date": book_date,
        "selected_channel": channel,
        "channels": Channel.choices,
        "query": query,
        "tab": tab,
        "selisih": selisih,
        "review_count": review_count,
        "page_obj": page_obj,
        "total_count": paginator.count,
    }

    if request.headers.get("HX-Request"):
        return render(request, "dashboard/_matches_rows.html", context)

    return render(request, "dashboard/matches.html", context)


@login_required
@require_POST
def approve_match_action(request, pk: int):
    match = get_object_or_404(Match, pk=pk)
    next_url = request.POST.get("next_url") or request.META.get("HTTP_REFERER") or f"/matches/?d={match.book_date}"
    try:
        approve_match(match, user=request.user)
        messages.success(request, f"Usulan pencocokan #{match.id} disetujui dan sekarang final.")
    except ValueError as e:
        messages.error(request, str(e))
    return redirect(next_url)


@login_required
@require_POST
def approve_match_bulk_action(request):
    from django.utils import timezone

    raw_ids = request.POST.getlist("selected_ids")
    book_date_raw = request.POST.get("book_date") or ""
    fallback_url = f"/matches/?d={book_date_raw}" if book_date_raw else "/matches/"
    next_url = request.POST.get("next_url") or request.META.get("HTTP_REFERER") or fallback_url

    match_ids: list[int] = []
    for rid in raw_ids:
        for part in str(rid).split(","):
            part = part.strip()
            if part.isdigit():
                match_ids.append(int(part))

    if not match_ids:
        messages.warning(request, "Pilih minimal satu pasangan untuk dikonfirmasi.")
        return redirect(next_url)

    matches_qs = Match.objects.filter(pk__in=match_ids, voided_at__isnull=True)
    approved_count = 0
    already_final_count = 0
    errors: list[str] = []

    for m in matches_qs:
        try:
            if m.needs_review:
                approve_match(m, user=request.user)
                approved_count += 1
            else:
                if not m.reviewed_at:
                    m.reviewed_by = request.user
                    m.reviewed_at = timezone.now()
                    m.save(update_fields=["reviewed_by", "reviewed_at", "updated_at"])
                already_final_count += 1
        except ValueError as e:
            errors.append(str(e))

    if approved_count > 0 and already_final_count > 0:
        messages.success(
            request,
            f"Berhasil mengonfirmasi {approved_count + already_final_count} pasangan sekaligus "
            f"({approved_count} usulan disetujui menjadi final, {already_final_count} pasangan terkonfirmasi).",
        )
    elif approved_count > 0:
        messages.success(
            request,
            f"Berhasil mengonfirmasi {approved_count} usulan pencocokan sekaligus menjadi final.",
        )
    elif already_final_count > 0:
        messages.success(
            request,
            f"Berhasil mengonfirmasi {already_final_count} pasangan yang dipilih.",
        )
    if errors:
        messages.error(request, f"Sebagian gagal dikonfirmasi: {errors[0]}")

    return redirect(next_url)

