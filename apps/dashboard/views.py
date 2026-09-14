from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import models
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.catalog.models import ExclusionCategory, ExclusionRule, ExclusionTarget
from apps.core.enums import Channel, ManualTag, MatchStatus
from apps.ingest.models import BankMutation, ExcludedTransaction, ImportBatch, OtomaxEntry
from apps.ingest.services import (
    ImportBlocked,
    apply_exclusion_rules_retroactive,
    import_file,
    preview_file,
)
from apps.recon.carry import carry_forward
from apps.recon.close import DayHasDownstream, DayLocked, DayNotReady, close_day, reopen_day
from apps.recon.engine import run_match
from apps.recon.models import Adjustment, Discrepancy, Match, ReconDay
from apps.recon.purge import (
    DayIsClosed,
    delete_import_batch,
    preview_all,
    purge_all_transactions,
    purge_day,
)
from apps.recon.reports import generate_excel_report, get_daily_summary, get_range_summary
from apps.recon.resolve import (
    manual_pair_transactions,
    tag_manual_mutation,
    unpair_match,
    write_off,
)
from apps.recon.selectors import alarm_discrepancies, cumulative_open_total, day_overview

staff_only = user_passes_test(lambda u: u.is_superuser)


def _parse_date(raw: str | None, default=None) -> date:
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return default or timezone.localdate()


# --- Halaman 1: Dashboard ----------------------------------------------------


@login_required
def day_view(request):
    book_date = _parse_date(request.GET.get("d"))
    ctx = day_overview(book_date)
    ctx["summary"] = get_daily_summary(book_date)
    ctx["alarms"] = alarm_discrepancies(today=book_date)
    ctx["cumulative_open"] = cumulative_open_total()
    ctx["channels"] = Channel.choices
    day = ctx["day"]
    ctx["can_reopen"] = bool(
        day
        and day.locked
        and not Adjustment.objects.filter(book_date=book_date).exists()
        and not Discrepancy.objects.filter(origin_book_date=book_date, status__in=["RESOLVED", "WRITTEN_OFF"]).exists()
    )
    return render(request, "dashboard/day.html", ctx)


# --- Halaman 2: Upload Data & Preview ----------------------------------------


@login_required
def upload_view(request):
    book_date = _parse_date(request.GET.get("d") or request.POST.get("book_date"))
    batches = ImportBatch.objects.filter(book_date=book_date).order_by("-created_at")

    preview_data = None
    if request.method == "POST":
        action = request.POST.get("action")
        channel = request.POST.get("channel")
        upload_file = request.FILES.get("file")

        if not upload_file or channel not in Channel.values:
            messages.error(request, "Pilih channel dan file yang valid.")
            return redirect(f"/upload/?d={book_date}")

        content = upload_file.read()
        filename = upload_file.name

        if action == "preview":
            try:
                preview_data = preview_file(channel, content)
                preview_data["filename"] = filename
            except Exception as exc:
                messages.error(request, f"Gagal membaca preview: {exc}")
        else:
            # Action == 'import'
            try:
                batch = import_file(
                    channel=channel,
                    content=content,
                    book_date=book_date,
                    filename=filename,
                    user=request.user,
                )
                messages.success(
                    request,
                    f"Berhasil mengimpor {batch.channel}: {batch.row_count} baris "
                    f"({batch.quarantined_count} dikarantina).",
                )
                return redirect(f"/upload/?d={book_date}")
            except ImportBlocked as exc:
                messages.error(request, str(exc))
            except Exception as exc:
                messages.error(request, f"Gagal mengimpor file: {exc}")

    return render(
        request,
        "dashboard/upload.html",
        {
            "book_date": book_date,
            "channels": Channel.choices,
            "batches": batches,
            "preview": preview_data,
        },
    )


@login_required
@require_POST
def upload(request):
    """Legacy redirect handler for upload."""
    book_date = _parse_date(request.POST.get("book_date"))
    channel = request.POST.get("channel")
    upload_file = request.FILES.get("file")
    if not upload_file or channel not in Channel.values:
        messages.error(request, "Pilih channel dan file.")
        return redirect(f"/upload/?d={book_date}")
    try:
        batch = import_file(
            channel=channel,
            content=upload_file.read(),
            book_date=book_date,
            filename=upload_file.name,
            user=request.user,
        )
        messages.success(
            request,
            f"{batch.channel}: {batch.row_count} baris ({batch.quarantined_count} dikarantina).",
        )
    except ImportBlocked as exc:
        messages.error(request, str(exc))
    except Exception as exc:
        messages.error(request, f"Gagal: {exc}")
    return redirect(f"/upload/?d={book_date}")


@login_required
@require_POST
def delete_batch_action(request, pk: int):
    book_date_raw = request.POST.get("book_date")
    try:
        res = delete_import_batch(pk)
        bdate = res["book_date"]
        unlinked_msg = (
            f" ({res['matches_unlinked']} pasangan terkait direset ke belum cocok)."
            if res["matches_unlinked"] > 0
            else ""
        )
        messages.success(
            request,
            f"Batch {res['channel']} ({res['filename']}) berhasil dihapus. "
            f"{res['row_count']} baris data telah dibersihkan{unlinked_msg}",
        )
        return redirect(f"/upload/?d={bdate}")
    except DayIsClosed as exc:
        messages.error(request, str(exc))
    except Exception as exc:
        messages.error(request, f"Gagal menghapus batch: {exc}")

    target_d = book_date_raw or ""
    return redirect(f"/upload/?d={target_d}" if target_d else "/upload/")



# --- Halaman 3: Hasil Rekonsiliasi ------------------------------------------


@login_required
def matches_view(request):
    book_date = _parse_date(request.GET.get("d"))
    channel = request.GET.get("channel", "")
    query = request.GET.get("q", "").strip()

    qs = Match.objects.filter(book_date=book_date, voided_at__isnull=True).select_related(
        "bank_mutation", "otomax_entry"
    )
    if channel and channel in Channel.values:
        qs = qs.filter(channel=channel)
    if query:
        qs = (
            qs.filter(bank_mutation__description_raw__icontains=query)
            | qs.filter(otomax_entry__description_raw__icontains=query)
            | qs.filter(otomax_entry__reseller_name_raw__icontains=query)
        )

    matches = qs.order_by("-match_type", "-amount_bank")

    return render(
        request,
        "dashboard/matches.html",
        {
            "book_date": book_date,
            "selected_channel": channel,
            "channels": Channel.choices,
            "query": query,
            "matches": matches,
            "total_count": matches.count(),
        },
    )


def _find_auto_pairs(unmatched_banks, pending_otomax):
    import re
    from collections import defaultdict

    def get_name_from_bank(desc):
        parts = (desc or "").split(" - ")
        return parts[-1].strip().upper() if len(parts) >= 2 else ""

    def get_name_from_otomax(desc):
        m = re.search(r"BFST\d+([A-Z\s]+?)(?::|$)", (desc or "").upper())
        if m:
            return m.group(1).strip()
        return ""

    used_b = set()
    used_o = set()
    pairs = []

    # Tahap 1: Nama pengirim + nominal persis
    for o in pending_otomax:
        o_name = get_name_from_otomax(o.description_raw or "")
        if not o_name or len(o_name) < 3:
            continue
        for b in unmatched_banks:
            if b.id in used_b:
                continue
            if b.amount != o.amount:
                continue
            b_name = get_name_from_bank(b.description_raw or "")
            if b_name and len(b_name) >= 3 and (o_name in b_name or b_name in o_name):
                pairs.append((b, o, f"Cocok referensi nama ({b_name}) & nominal persis"))
                used_b.add(b.id)
                used_o.add(o.id)
                break

    # Tahap 2: Nominal 1-ke-1 unik
    rem_banks = [b for b in unmatched_banks if b.id not in used_b]
    rem_otomax = [o for o in pending_otomax if o.id not in used_o]

    banks_by_amt = defaultdict(list)
    for b in rem_banks:
        banks_by_amt[b.amount].append(b)

    otomax_by_amt = defaultdict(list)
    for o in rem_otomax:
        otomax_by_amt[o.amount].append(o)

    for amt, b_list in banks_by_amt.items():
        o_list = otomax_by_amt.get(amt, [])
        if len(b_list) == 1 and len(o_list) == 1:
            bm = b_list[0]
            oe = o_list[0]
            pairs.append((bm, oe, f"Cocok referensi nominal unik persis (Rp {amt:,.0f})"))
            used_b.add(bm.id)
            used_o.add(oe.id)

    return pairs


# --- Halaman 4: Antrean Review Manual ----------------------------------------


@login_required
def manual_review_view(request):
    book_date = _parse_date(request.GET.get("d"))
    tab = request.GET.get("tab", "unmatched")  # 'unmatched' or 'tagged'
    channel = request.GET.get("channel", "")

    qs = BankMutation.objects.filter(book_date=book_date)
    if channel and channel in Channel.values:
        qs = qs.filter(channel=channel)

    if tab == "tagged":
        items = qs.filter(match_status=MatchStatus.MANUAL).order_by("-updated_at")
    else:
        items = qs.filter(match_status=MatchStatus.UNMATCHED).order_by("-amount")

    unmatched_qs = qs.filter(match_status=MatchStatus.UNMATCHED)
    tagged_qs = qs.filter(match_status=MatchStatus.MANUAL)

    unmatched_count = unmatched_qs.count()
    unmatched_total = unmatched_qs.aggregate(t=models.Sum("amount"))["t"] or Decimal("0.00")

    tagged_count = tagged_qs.count()
    tagged_total = tagged_qs.aggregate(t=models.Sum("amount"))["t"] or Decimal("0.00")

    # Daftar entri Otomax belum cocok untuk opsi pencocokan manual
    unmatched_otomax = list(
        OtomaxEntry.objects.filter(
            book_date__gte=book_date - timedelta(days=2),
            book_date__lte=book_date + timedelta(days=1),
            match_status__in=[MatchStatus.PENDING_SETTLE, MatchStatus.UNMATCHED],
        ).order_by("-amount")
    )

    items_list = list(items)
    auto_pairs = _find_auto_pairs(items_list, unmatched_otomax)
    auto_pairable_count = len(auto_pairs)

    return render(
        request,
        "dashboard/manual_review.html",
        {
            "book_date": book_date,
            "tab": tab,
            "selected_channel": channel,
            "channels": Channel.choices,
            "items": items_list,
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

    if not tag or tag not in ManualTag.values:
        messages.error(request, "Pilih tag kategori yang valid.")
        return redirect(f"/review-manual/?d={book_date}")

    tag_manual_mutation(bm, tag=tag, note=note, user=request.user)
    messages.success(request, f"Mutasi Rp {bm.amount:,.0f} berhasil di-tag sebagai '{bm.get_tag_manual_display()}'.")
    return redirect(f"/review-manual/?d={book_date}")


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


# --- Halaman 5: Pending Settle -----------------------------------------------


@login_required
def pending_settle_view(request):
    book_date = _parse_date(request.GET.get("d"))
    channel = request.GET.get("channel", "")

    qs = OtomaxEntry.objects.filter(
        book_date=book_date,
        match_status__in=[MatchStatus.PENDING_SETTLE, MatchStatus.UNMATCHED],
    )
    if channel and channel in Channel.values:
        qs = qs.filter(channel_hint=channel)

    items = list(qs.order_by("-amount"))
    total_amount = sum((i.amount for i in items), Decimal("0"))

    # Daftar mutasi bank yang belum cocok untuk kandidat pencocokan manual
    unmatched_banks = list(
        BankMutation.objects.filter(
            book_date__gte=book_date - timedelta(days=2),
            book_date__lte=book_date + timedelta(days=1),
            match_status=MatchStatus.UNMATCHED,
        ).order_by("-amount", "-txn_datetime")
    )

    auto_pairs = _find_auto_pairs(unmatched_banks, items)
    auto_pairable_count = len(auto_pairs)

    return render(
        request,
        "dashboard/pending_settle.html",
        {
            "book_date": book_date,
            "selected_channel": channel,
            "channels": Channel.choices,
            "items": items,
            "count": len(items),
            "total_amount": total_amount,
            "unmatched_banks": unmatched_banks,
            "auto_pairable_count": auto_pairable_count,
        },
    )


# --- Halaman 6: Riwayat & Laporan --------------------------------------------


@login_required
def reports_view(request):
    today = timezone.localdate()
    start_date = _parse_date(request.GET.get("start_date"), default=today - timedelta(days=6))
    end_date = _parse_date(request.GET.get("end_date"), default=today)

    if start_date > end_date:
        start_date, end_date = end_date, start_date

    summary = get_range_summary(start_date, end_date)

    # Breakdown per day in range
    curr = start_date
    daily_rows = []
    while curr <= end_date:
        d_sum = get_daily_summary(curr)
        daily_rows.append(d_sum)
        curr += timedelta(days=1)
    daily_rows.reverse()

    return render(
        request,
        "dashboard/reports.html",
        {
            "start_date": start_date,
            "end_date": end_date,
            "summary": summary,
            "daily_rows": daily_rows,
        },
    )


@login_required
def reports_export_action(request):
    start_date = _parse_date(request.GET.get("start_date"))
    end_date = _parse_date(request.GET.get("end_date"))
    if "start_date" not in request.GET and "end_date" not in request.GET and "d" in request.GET:
        start_date = end_date = _parse_date(request.GET.get("d"))

    excel_bytes = generate_excel_report(start_date, end_date)
    response = HttpResponse(
        excel_bytes,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="rekonsiliasi_{start_date}_{end_date}.xlsx"'
    return response


# --- Mesin Rekon & Kontrol Buku ----------------------------------------------


@login_required
@require_POST
def run_engine(request):
    book_date = _parse_date(request.POST.get("book_date"))
    stats = run_match(book_date)
    resolved = carry_forward(book_date, user=request.user)
    messages.success(
        request,
        f"Cocok: {stats.matched} · discrepancy baru: {stats.discrepancies} · " f"selisih lama ditutup: {resolved}.",
    )
    return redirect(f"/?d={book_date}")


@login_required
@require_POST
def close_view(request):
    book_date = _parse_date(request.POST.get("book_date"))
    force = request.POST.get("force") == "1"
    try:
        day = close_day(book_date, user=request.user, force=force)
    except (DayLocked, DayNotReady) as exc:
        messages.error(request, str(exc))
        return redirect(f"/?d={book_date}")
    messages.success(request, f"Buku {day.book_date} ditutup. Selisih Rp {day.selisih_initial:,.0f}.")
    return redirect(f"/?d={book_date}")


@login_required
@require_POST
def reopen_view(request):
    book_date = _parse_date(request.POST.get("book_date"))
    try:
        reopen_day(book_date, user=request.user)
    except DayHasDownstream as exc:
        messages.error(request, str(exc))
        return redirect(f"/?d={book_date}")
    messages.success(request, f"Buku {book_date} dibuka kembali. Jalankan pencocokan lalu tutup lagi.")
    return redirect(f"/?d={book_date}")


@login_required
@require_POST
def resolve_view(request, pk: int):
    disc = Discrepancy.objects.get(pk=pk)
    reason = request.POST.get("reason", "")
    if request.POST.get("action") == "write_off":
        write_off(disc, reason=reason or "hapus buku manual", user=request.user)
        messages.success(request, f"{disc.code} dihapusbukukan.")
    else:
        messages.info(request, "Gunakan tombol Jalankan Pencocokan untuk mencari pasangan otomatis.")
    return redirect(f"/?d={request.POST.get('book_date')}")


# --- Hapus data --------------------------------------------------------------


@login_required
@staff_only
def data_admin(request):
    dates = set(ImportBatch.objects.values_list("book_date", flat=True)) | set(
        ReconDay.objects.values_list("book_date", flat=True)
    )
    rows = []
    for bd in sorted(dates, reverse=True):
        day = ReconDay.objects.filter(book_date=bd).first()
        rows.append(
            {
                "book_date": bd,
                "locked": bool(day and day.locked),
                "status": day.get_status_display() if day else "belum ada",
                "bank": BankMutation.objects.filter(book_date=bd).count(),
                "otomax": OtomaxEntry.objects.filter(book_date=bd).count(),
                "match": Match.objects.filter(book_date=bd).count(),
                "discrepancy": Discrepancy.objects.filter(origin_book_date=bd).count(),
            }
        )
    return render(request, "dashboard/data.html", {"rows": rows, "totals": preview_all()})


@login_required
@staff_only
@require_POST
def purge_day_view(request):
    book_date = _parse_date(request.POST.get("book_date"))
    include_closed = request.POST.get("include_closed") == "1"
    try:
        counts = purge_day(book_date, include_closed=include_closed)
    except DayIsClosed as exc:
        messages.error(request, str(exc))
        return redirect("data-admin")
    total = sum(counts.values())
    messages.success(request, f"Data tanggal {book_date} dihapus ({total} baris).")
    return redirect("data-admin")


@login_required
@staff_only
@require_POST
def purge_all_view(request):
    if request.POST.get("confirm") != "HAPUS SEMUA":
        messages.error(request, 'Ketik persis "HAPUS SEMUA" untuk konfirmasi.')
        return redirect("data-admin")
    counts = purge_all_transactions(include_history=request.POST.get("include_history") == "1")
    messages.success(
        request,
        f"Semua data transaksi dihapus ({sum(counts.values())} baris). " "Reseller & mapping merchant tetap.",
    )
    return redirect("data-admin")


# --- Halaman 7: Pengaturan Aturan Filter (Exclusion Rules) ------------------


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

