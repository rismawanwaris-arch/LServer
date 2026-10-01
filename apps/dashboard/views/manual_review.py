"""Halaman 4: Antrean Review Manual — mutasi bank yang belum cocok atau sudah di-tag manual."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.enums import Channel, ManualTag, MatchStatus, OtomaxCategory
from apps.core.normalize import parse_tgl_date
from apps.ingest.models import BankMutation, OtomaxEntry
from apps.recon.models import Match
from apps.recon.resolve import (
    manual_pair_many,
    manual_pair_many_banks,
    manual_pair_transactions,
    tag_manual_mutation,
    unpair_match,
)

from ._shared import _CANDIDATE_WINDOW, _EXTENDED_WINDOW_DAYS, _find_auto_pairs, _paginate_rows, _parse_date


def _otomax_candidate(o: OtomaxEntry, book_date, *, extended: bool) -> dict:
    return {
        "id": o.id,
        "reseller": o.reseller_name_raw,
        "cents": int(o.amount * 100),
        "desc": o.description_raw,
        "when": (o.entry_datetime and timezone.localtime(o.entry_datetime).strftime("%d %b %H:%M"))
        or o.book_date.strftime("%d %b"),
        "day_gap": (o.book_date - book_date).days,
        "tgl_match": parse_tgl_date(o.description_raw) == book_date,
        "extended": extended,
        "channel": o.channel_hint or "",
        "tokens": o.extracted_tokens or [],
        "ref_core": o.ref_core or "",
    }


REVIEW_PAGE_SIZE = 20


@login_required
def manual_review_view(request):
    book_date = _parse_date(request.GET.get("d"))
    tab = request.GET.get("tab", "unmatched")  # 'unmatched' or 'tagged'
    channel = request.GET.get("channel", "")

    qs = BankMutation.objects.filter(book_date=book_date)
    if channel and channel in Channel.values:
        qs = qs.filter(channel=channel)

    unmatched_qs = qs.filter(match_status=MatchStatus.UNMATCHED)
    active = Match.objects.filter(voided_at__isnull=True).select_related("otomax_entry")
    tagged_qs = qs.filter(match_status=MatchStatus.MANUAL).prefetch_related(
        models.Prefetch("matches", queryset=active, to_attr="active_matches"),
        # Mutasi anggota gabungan beberapa mutasi (bukan mutasi utamanya).
        models.Prefetch("aggregate_bank_matches", queryset=active, to_attr="active_aggregate_matches"),
    )

    unmatched_count = unmatched_qs.count()
    unmatched_total = unmatched_qs.aggregate(t=models.Sum("amount"))["t"] or Decimal("0.00")

    tagged_count = tagged_qs.count()
    tagged_total = tagged_qs.aggregate(t=models.Sum("amount"))["t"] or Decimal("0.00")

    # Daftar entri Otomax belum cocok untuk opsi pencocokan manual
    # (Hanya transaksi yang belum selesai, bukan potongan admin)
    open_otomax = OtomaxEntry.objects.filter(
        match_status__in=[MatchStatus.PENDING_SETTLE, MatchStatus.UNMATCHED],
    ).exclude(category=OtomaxCategory.ADMIN)
    window = (book_date + timedelta(days=_CANDIDATE_WINDOW[0]), book_date + timedelta(days=_CANDIDATE_WINDOW[1]))
    unmatched_otomax = list(open_otomax.filter(book_date__range=window).order_by("-amount"))

    if tab == "tagged":
        items = list(tagged_qs.order_by("-updated_at"))
        for b in items:
            b.active_match = next(iter(b.active_matches or b.active_aggregate_matches), None)
    else:
        items = list(unmatched_qs.order_by("-amount"))

    # Pencocokan Cepat sengaja tetap cuma memakai jendela normal (perilaku lama).
    auto_pairs = _find_auto_pairs(items if tab != "tagged" else [], unmatched_otomax)
    auto_pairable_count = len(auto_pairs)

    otomax_candidates = []
    if tab != "tagged":
        bank_amounts = {b.amount for b in items}
        wide = (book_date - timedelta(days=_EXTENDED_WINDOW_DAYS), book_date + timedelta(days=_EXTENDED_WINDOW_DAYS))
        extended = [
            o
            for o in open_otomax.filter(book_date__range=wide).exclude(book_date__range=window).order_by("-amount")
            if o.amount in bank_amounts or parse_tgl_date(o.description_raw) == book_date
        ]
        otomax_candidates = [_otomax_candidate(o, book_date, extended=False) for o in unmatched_otomax] + [
            _otomax_candidate(o, book_date, extended=True) for o in extended
        ]
    for b in items:
        b.amount_cents = int(b.amount * 100)

    # Kartu dirender bertahap (20 per halaman) -- tiap kartu ~9 KB berisi komponen
    # pencari lawan Otomax. Jumlah, total, Pencocokan Cepat & kandidat tetap dari SEMUA.
    page_obj, is_more, list_url = _paginate_rows(request, items, REVIEW_PAGE_SIZE)
    if is_more and tab != "tagged":
        return render(
            request,
            "dashboard/_review_cards.html",
            {"page_obj": page_obj, "list_url": list_url, "book_date": book_date, "manual_tags": ManualTag.choices},
        )

    return render(
        request,
        "dashboard/manual_review.html",
        {
            "book_date": book_date,
            "tab": tab,
            "selected_channel": channel,
            "channels": Channel.choices,
            "items": items if tab == "tagged" else list(page_obj.object_list),
            "page_obj": page_obj,
            "list_url": list_url,
            "unmatched_count": unmatched_count,
            "unmatched_total": unmatched_total,
            "tagged_count": tagged_count,
            "tagged_total": tagged_total,
            "manual_tags": ManualTag.choices,
            "otomax_candidates": otomax_candidates,
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


def _post_ids(request, many: str, single: str) -> list[int]:
    raw = [i for i in request.POST.getlist(many) if i] or [i for i in [request.POST.get(single)] if i]
    try:
        return [int(i) for i in raw]
    except ValueError:
        return []


@login_required
@require_POST
def manual_match_action(request):
    # otomax_ids (daftar centang Review Manual) / bank_ids (daftar centang Pending Settle)
    # bisa >1; otomax_id / bank_id tunggal tetap diterima (Reversal, form lama).
    otomax_ids = _post_ids(request, "otomax_ids", "otomax_id")
    bank_ids = _post_ids(request, "bank_ids", "bank_id")
    note = request.POST.get("note", "").strip()
    book_date = request.POST.get("book_date")
    fallback_url = f"/pending-settle/?d={book_date}" if book_date else "/"
    next_url = request.POST.get("next_url") or request.META.get("HTTP_REFERER") or fallback_url

    if not otomax_ids or not bank_ids:
        messages.error(request, "Pilih transaksi Otomax dan mutasi Bank yang akan dicocokkan.")
        return redirect(next_url)
    if len(otomax_ids) > 1 and len(bank_ids) > 1:
        messages.error(request, "Pilih beberapa entri Otomax ATAU beberapa mutasi bank, tidak keduanya sekaligus.")
        return redirect(next_url)

    if len(bank_ids) > 1:
        o = get_object_or_404(OtomaxEntry, pk=otomax_ids[0])
        try:
            banks = list(BankMutation.objects.filter(pk__in=bank_ids))
            match = manual_pair_many_banks(banks, o, note=note, user=request.user)
            msg = (
                f"Berhasil menggabungkan {len(banks)} mutasi bank (total Rp {match.amount_bank:,.0f}) "
                f"dengan Otomax '{o.reseller_name_raw}' (Rp {o.amount:,.0f})."
            )
            if match.amount_diff:
                messages.warning(
                    request, f"{msg} Sisa selisih Rp {match.amount_diff:,.0f} tercatat di Daftar Selisih."
                )
            else:
                messages.success(request, msg)
        except ValueError as e:
            messages.error(request, str(e))
        return redirect(next_url)

    bm = get_object_or_404(BankMutation, pk=bank_ids[0])

    try:
        if len(otomax_ids) == 1:
            o = get_object_or_404(OtomaxEntry, pk=otomax_ids[0])
            manual_pair_transactions(bank_mutation=bm, otomax_entry=o, note=note, user=request.user)
            messages.success(
                request,
                f"Berhasil mencocokkan Otomax '{o.reseller_name_raw}' (Rp {o.amount:,.0f}) "
                f"dengan mutasi {bm.channel} (Rp {bm.amount:,.0f}).",
            )
        else:
            entries = list(OtomaxEntry.objects.filter(pk__in=otomax_ids))
            match = manual_pair_many(bm, entries, note=note, user=request.user)
            msg = (
                f"Berhasil menggabungkan {len(entries)} entri Otomax (total Rp {match.amount_otomax:,.0f}) "
                f"dengan mutasi {bm.channel} (Rp {bm.amount:,.0f})."
            )
            if match.amount_diff:
                messages.warning(
                    request, f"{msg} Sisa selisih Rp {match.amount_diff:,.0f} tercatat di Daftar Selisih."
                )
            else:
                messages.success(request, msg)
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
