"""REST API views sesuai PRD Bagian 8."""

from __future__ import annotations

import functools
import hmac
import json
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.core.enums import Channel, MatchStatus
from apps.ingest.models import BankMutation, OtomaxEntry
from apps.ingest.services import ImportBlocked, import_file, preview_file
from apps.recon.engine import run_match
from apps.recon.reports import generate_excel_report, get_daily_summary
from apps.recon.resolve import tag_manual_mutation

BANK_MAP = {
    "bca": Channel.BCA,
    "bri": Channel.BRI,
    "mandiri": Channel.MANDIRI,
    "merchant_bca": Channel.MERCHANT_BCA,
    "merchant-bca": Channel.MERCHANT_BCA,
}


def _parse_date(val: str | None) -> date:
    if val:
        try:
            return date.fromisoformat(val)
        except ValueError:
            pass
    return timezone.localdate()


def api_auth_required(view_func):
    """Decorator untuk memproteksi endpoint REST API.

    Akses diizinkan jika:
    1. Pengguna terautentikasi via sesi Django (request.user.is_authenticated), ATAU
    2. Request menyertakan header 'X-API-KEY' atau 'Authorization: Bearer <key>'
       yang cocok dengan settings.API_KEY, ATAU
    3. settings.API_KEY belum disetel DAN settings.DEBUG bernilai True (lingkungan dev lokal).
    """

    @functools.wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if request.user and request.user.is_authenticated:
            return view_func(request, *args, **kwargs)

        configured_key = getattr(settings, "API_KEY", "").strip()
        auth_header = request.headers.get("Authorization", "").strip()
        api_key_header = request.headers.get("X-API-KEY", "").strip()
        provided_key = ""
        if auth_header.startswith("Bearer "):
            provided_key = auth_header[7:].strip()
        elif api_key_header:
            provided_key = api_key_header

        if configured_key:
            if provided_key and hmac.compare_digest(provided_key, configured_key):
                return view_func(request, *args, **kwargs)
            return JsonResponse(
                {"error": "Autentikasi gagal: API Key tidak valid atau tidak disertakan."},
                status=401,
            )

        if not getattr(settings, "DEBUG", False):
            return JsonResponse(
                {"error": "Akses ditolak: Autentikasi login diperlukan atau API_KEY belum disetel di server."},
                status=401,
            )

        return view_func(request, *args, **kwargs)

    return _wrapped


@csrf_exempt
@require_POST
@api_auth_required
def upload_otomax(request):
    """POST /api/upload/otomax"""
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"error": "File diperlukan (field 'file')"}, status=400)
    bd = _parse_date(request.POST.get("book_date"))
    try:
        batch = import_file(
            channel=Channel.OTOMAX,
            content=f.read(),
            book_date=bd,
            filename=f.name,
            user=request.user if request.user.is_authenticated else None,
        )
    except ImportBlocked as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"error": f"Gagal memproses file: {exc}"}, status=500)

    return JsonResponse(
        {
            "status": "ok",
            "channel": batch.channel,
            "book_date": batch.book_date.isoformat(),
            "row_count": batch.row_count,
            "quarantined_count": batch.quarantined_count,
            "notes": batch.notes,
        }
    )


@csrf_exempt
@require_POST
@api_auth_required
def upload_mutasi(request, bank: str):
    """POST /api/upload/mutasi/{bank}"""
    channel = BANK_MAP.get(bank.lower())
    if not channel:
        return JsonResponse({"error": f"Bank '{bank}' tidak dikenal. Pilihan: {list(BANK_MAP.keys())}"}, status=400)

    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"error": "File diperlukan (field 'file')"}, status=400)

    bd = _parse_date(request.POST.get("book_date"))
    try:
        batch = import_file(
            channel=channel,
            content=f.read(),
            book_date=bd,
            filename=f.name,
            user=request.user if request.user.is_authenticated else None,
        )
    except ImportBlocked as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"error": f"Gagal memproses file: {exc}"}, status=500)

    return JsonResponse(
        {
            "status": "ok",
            "channel": batch.channel,
            "book_date": batch.book_date.isoformat(),
            "row_count": batch.row_count,
            "quarantined_count": batch.quarantined_count,
            "notes": batch.notes,
        }
    )


@csrf_exempt
@require_POST
@api_auth_required
def upload_preview(request):
    """POST /api/upload/preview"""
    f = request.FILES.get("file")
    channel = request.POST.get("channel")
    if not f or not channel:
        return JsonResponse({"error": "File dan channel diperlukan"}, status=400)

    if channel.lower() in BANK_MAP:
        channel = BANK_MAP[channel.lower()]
    elif channel.upper() == "OTOMAX":
        channel = Channel.OTOMAX

    try:
        preview_data = preview_file(channel, f.read())
        return JsonResponse({"status": "ok", "preview": preview_data})
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)


@csrf_exempt
@require_POST
@api_auth_required
def reconcile_run(request):
    """POST /api/reconcile/run"""
    bd_str = request.POST.get("book_date") or request.GET.get("book_date")
    start_str = request.POST.get("start_date") or request.GET.get("start_date")
    end_str = request.POST.get("end_date") or request.GET.get("end_date")

    if not bd_str and not start_str and request.body:
        try:
            body = json.loads(request.body)
            bd_str = body.get("book_date")
            start_str = body.get("start_date")
            end_str = body.get("end_date")
        except json.JSONDecodeError:
            pass

    dates = []
    if start_str and end_str:
        s = _parse_date(start_str)
        e = _parse_date(end_str)
        curr = s
        while curr <= e:
            dates.append(curr)
            curr += timedelta(days=1)
    else:
        dates.append(_parse_date(bd_str))

    total_matched = 0
    total_discrepancies = 0
    for d in dates:
        stats = run_match(d)
        total_matched += stats.matched
        total_discrepancies += stats.discrepancies

    return JsonResponse(
        {
            "status": "ok",
            "dates": [d.isoformat() for d in dates],
            "total_matched": total_matched,
            "total_discrepancies": total_discrepancies,
        }
    )


@require_GET
@api_auth_required
def reconcile_summary(request):
    """GET /api/reconcile/summary"""
    d = _parse_date(request.GET.get("d") or request.GET.get("date"))
    summary = get_daily_summary(d)

    # Serialize decimals
    def _dec(v):
        return float(v) if isinstance(v, Decimal) else v

    result = {
        "book_date": summary["book_date"].isoformat(),
        "day_status": summary["day_status"],
        "day_locked": summary["day_locked"],
        "total_bank": _dec(summary["total_bank"]),
        "total_otomax": _dec(summary["total_otomax"]),
        "selisih": _dec(summary["selisih"]),
        "matched_auto_count": summary["matched_auto_count"],
        "matched_auto_amount": _dec(summary["matched_auto_amount"]),
        "matched_manual_count": summary["matched_manual_count"],
        "matched_manual_amount": _dec(summary["matched_manual_amount"]),
        "unmatched_bank_count": summary["unmatched_bank_count"],
        "unmatched_bank_amount": _dec(summary["unmatched_bank_amount"]),
        "pending_settle_count": summary["pending_settle_count"],
        "pending_settle_amount": _dec(summary["pending_settle_amount"]),
        "amount_diff_count": summary["amount_diff_count"],
        "amount_diff_amount": _dec(summary["amount_diff_amount"]),
        "per_bank": {k: {sk: _dec(sv) for sk, sv in v.items()} for k, v in summary["per_bank"].items()},
    }
    return JsonResponse(result)


@require_GET
@api_auth_required
def reconcile_unmatched(request):
    """GET /api/reconcile/unmatched"""
    d_str = request.GET.get("d") or request.GET.get("date")
    channel = request.GET.get("channel")
    qs = BankMutation.objects.filter(match_status=MatchStatus.UNMATCHED)
    if d_str:
        qs = qs.filter(book_date=_parse_date(d_str))
    if channel:
        qs = qs.filter(channel=channel.upper())

    rows = []
    for b in qs.order_by("-book_date", "-id")[:500]:
        rows.append(
            {
                "id": b.id,
                "book_date": b.book_date.isoformat(),
                "channel": b.channel,
                "amount": float(b.amount),
                "description": b.description_raw,
                "tokens": b.extracted_tokens or [],
                "outlet_name": b.outlet_name or "",
                "match_status": b.match_status,
            }
        )
    return JsonResponse({"count": len(rows), "results": rows})


@require_GET
@api_auth_required
def reconcile_pending_settle(request):
    """GET /api/reconcile/pending-settle"""
    d_str = request.GET.get("d") or request.GET.get("date")
    qs = OtomaxEntry.objects.filter(match_status__in=[MatchStatus.PENDING_SETTLE, MatchStatus.UNMATCHED])
    if d_str:
        qs = qs.filter(book_date=_parse_date(d_str))

    rows = []
    for o in qs.order_by("-book_date", "-id")[:500]:
        rows.append(
            {
                "id": o.id,
                "book_date": o.book_date.isoformat(),
                "reseller": o.reseller_name_raw,
                "amount": float(o.amount),
                "description": o.description_raw,
                "channel_hint": o.channel_hint or "",
                "tokens": o.extracted_tokens or [],
                "match_status": o.match_status,
            }
        )
    return JsonResponse({"count": len(rows), "results": rows})


@csrf_exempt
@require_POST
@api_auth_required
def reconcile_manual_tag(request):
    """POST /api/reconcile/manual-tag"""
    mutation_id = request.POST.get("mutation_id")
    tag = request.POST.get("tag")
    note = request.POST.get("note", "")

    if not mutation_id or not tag:
        if request.body:
            try:
                body = json.loads(request.body)
                mutation_id = body.get("mutation_id")
                tag = body.get("tag")
                note = body.get("note", "")
            except json.JSONDecodeError:
                pass

    if not mutation_id or not tag:
        return JsonResponse({"error": "mutation_id dan tag diperlukan"}, status=400)

    try:
        bm = BankMutation.objects.get(pk=mutation_id)
    except BankMutation.DoesNotExist:
        return JsonResponse({"error": f"Mutasi bank #{mutation_id} tidak ditemukan"}, status=404)

    bm = tag_manual_mutation(
        bm,
        tag=tag,
        note=note,
        user=request.user if request.user.is_authenticated else None,
    )

    return JsonResponse(
        {
            "status": "ok",
            "mutation_id": bm.id,
            "match_status": bm.match_status,
            "tag_manual": bm.tag_manual,
            "manual_note": bm.manual_note,
        }
    )


@require_GET
@api_auth_required
def reports_daily(request):
    """GET /api/reports/daily"""
    return reconcile_summary(request)


@require_GET
@api_auth_required
def reports_export(request):
    """GET /api/reports/export"""
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
