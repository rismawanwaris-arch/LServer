"""Helper lintas-halaman dashboard: parsing tanggal, guard staff-only, pencarian
kandidat pencocokan manual otomatis, sinkronisasi tanggal batch yang tidak konsisten,
dan checklist 'Langkah Hari Ini' (dipakai di halaman Upload Data)."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import user_passes_test
from django.db import models
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import MerchantMap
from apps.core.enums import Channel, MatchStatus, OtomaxCategory
from apps.ingest.models import ImportBatch, OtomaxEntry
from apps.recon.engine.qris_match import _clean_outlet_name, _name_similarity
from apps.recon.models import Discrepancy, Match, ReconDay
from apps.recon.reports import get_daily_summary

staff_only = user_passes_test(lambda u: u.is_superuser or u.is_staff)

LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300  # 5 menit


class RateLimitedLoginView(auth_views.LoginView):
    """LoginView dengan proteksi brute-force berbasis cache (maksimal 5 gagal dalam 5 menit)."""

    template_name = "dashboard/login.html"

    @staticmethod
    def _rate_key(request) -> str:
        xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
        ip = xff.split(",")[0].strip() if xff else (request.META.get("REMOTE_ADDR") or "unknown")
        username = (request.POST.get("username") or "").strip().lower()[:64]
        return f"login_fail:{ip}:{username}"

    def post(self, request, *args, **kwargs):
        from django.core.cache import cache

        key = self._rate_key(request)
        attempts = cache.get(key, 0)
        if attempts >= LOGIN_MAX_ATTEMPTS:
            form = self.get_form()
            form.add_error(
                None,
                "Terlalu banyak percobaan login gagal. Silakan tunggu 5 menit sebelum mencoba lagi.",
            )
            return self.render_to_response(self.get_context_data(form=form), status=429)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        from django.core.cache import cache

        key = self._rate_key(self.request)
        attempts = cache.get(key, 0) + 1
        cache.set(key, attempts, timeout=LOGIN_LOCKOUT_SECONDS)
        return super().form_invalid(form)

    def form_valid(self, form):
        from django.core.cache import cache

        cache.delete(self._rate_key(self.request))
        return super().form_valid(form)


_REVERSAL_OPEN_STATUSES = [MatchStatus.UNMATCHED, MatchStatus.PENDING_SETTLE]

# Kandidat pencocokan manual (Review Manual & Pending Settle): jendela "normal" dalam
# hari relatif ke tanggal halaman, plus jendela lebar yang cuma dipakai untuk kandidat
# bernominal persis sama / yang "TGL ..."-nya menunjuk tanggal ini -- supaya entri yang
# dientri operator beberapa hari telat tetap bisa dipasangkan manual.
_CANDIDATE_WINDOW = (-2, 1)
_EXTENDED_WINDOW_DAYS = 14


def _paginate_rows(request, rows, per_page: int):
    """Daftar panjang dirender bertahap (infinite scroll HTMX): halaman pertama ikut
    halaman penuh, halaman berikutnya diambil sebagai partial saat baris terakhir
    terlihat. Return (page_obj, is_more, list_url) -- list_url = URL daftar tanpa nomor
    halaman, dipakai sebagai tujuan kembali form di baris & dasar URL halaman berikut."""
    from django.core.paginator import Paginator

    paginator = Paginator(rows, per_page)
    is_more = bool(request.headers.get("HX-Request") and request.GET.get("page"))
    page_obj = paginator.get_page(request.GET.get("page") if is_more else 1)
    params = request.GET.copy()
    params.pop("page", None)
    list_url = request.path + (f"?{params.urlencode()}" if params else "")
    return page_obj, is_more, list_url


def _parse_date(raw: str | None, default=None) -> date:
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return default or timezone.localdate()


def _safe_next_url(request, fallback: str = "/") -> str:
    """Ambil next_url dari POST/GET/HTTP_REFERER dan pastikan hanya mengarah ke host internal
    (mencegah celah Open Redirect ke domain eksternal)."""
    from django.utils.http import url_has_allowed_host_and_scheme

    raw = (
        request.POST.get("next_url")
        or request.GET.get("next_url")
        or request.META.get("HTTP_REFERER")
        or ""
    ).strip()
    if raw and url_has_allowed_host_and_scheme(
        url=raw,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return raw
    return fallback



def _is_channel_compatible(b, o) -> bool:
    b_ch = getattr(b, "channel", None)
    o_ch = getattr(o, "channel_hint", None)
    o_cat = getattr(o, "category", None)
    o_desc = (getattr(o, "description_raw", "") or "").upper()

    is_b_qris = b_ch == Channel.MERCHANT_BCA
    is_o_qris = (
        o_ch == Channel.MERCHANT_BCA
        or o_cat == OtomaxCategory.TOPUP_TARTUN
        or "BULK" in o_desc
        or "TARTUN QR" in o_desc
    )

    # Settlement QRIS bank hanya boleh dipasangkan dengan QRIS / Tartun Bulk
    if is_b_qris != is_o_qris:
        return False

    # Untuk transfer bank non-QRIS (BCA, BRI, Mandiri, BNI, dll.)
    if not is_b_qris:
        if b_ch and o_ch and b_ch != Channel.UNKNOWN and o_ch not in (Channel.UNKNOWN, Channel.OTOMAX):
            if b_ch != o_ch:
                return False

    return True


def _get_bank_parties(b) -> list[str]:
    names = []
    if getattr(b, "outlet_name", None):
        names.append(b.outlet_name)
    desc = getattr(b, "description_raw", "") or ""
    parts = desc.split(" - ")
    if len(parts) >= 2:
        names.append(parts[-1].strip())
    m = re.search(r"(?:DARI|KE|TRANSFER|CR|TRSF)\s+(?:[A-Z0-9/.]+\s+)*(?:[0-9.,]+\s+)?([A-Z\s]{3,30})", desc.upper())
    if m:
        cand = m.group(1).strip()
        if len(cand) >= 3:
            names.append(cand)
    res = []
    for n in names:
        n_clean = n.strip().upper()
        if len(n_clean) >= 2 and n_clean not in res:
            res.append(n_clean)
    return res


def _get_otomax_parties(o) -> list[str]:
    names = []
    if getattr(o, "reseller_name_raw", None):
        names.append(o.reseller_name_raw)
    if getattr(o, "reseller", None) and o.reseller and getattr(o.reseller, "name", None):
        names.append(o.reseller.name)
    desc = getattr(o, "description_raw", "") or ""
    m_bfst = re.search(r"BFST\d+([A-Z\s]+?)(?::|$)", desc.upper())
    if m_bfst:
        names.append(m_bfst.group(1).strip())
    m_tartun = re.search(r"TARTUN\s+(?:BULK|QR|TF\s+\w+|EDC\s+\w+)?\s*([A-Z0-9\s]+?)(?:TGL|:|\d{2}[-/]|$|\.|\bRp\b)", desc.upper())
    if m_tartun:
        cand = m_tartun.group(1).strip()
        if len(cand) >= 3:
            names.append(cand)
    res = []
    for n in names:
        n_clean = n.strip().upper()
        if len(n_clean) >= 2 and n_clean not in res:
            res.append(n_clean)
    return res


def _calc_party_similarity(b_parties: list[str], o_parties: list[str]) -> tuple[float, str]:
    best_sim = 0.0
    best_name = ""
    for bp in b_parties:
        for op in o_parties:
            sim = _name_similarity(bp, op)
            c_bp = _clean_outlet_name(bp)
            c_op = _clean_outlet_name(op)
            if c_bp and c_op and (c_bp in c_op or c_op in c_bp):
                sim = max(sim, 85.0)
            if sim > best_sim:
                best_sim = sim
                best_name = bp
    return best_sim, best_name


def _find_auto_pairs(unmatched_banks, pending_otomax):
    used_b = set()
    used_o = set()
    pairs = []

    mmap = {m.merchant_id: m.reseller_id for m in MerchantMap.objects.filter(active=True)}

    # Tahap 1: Nama pengirim / outlet cocok DAN nominal persis
    for o in pending_otomax:
        if o.id in used_o:
            continue
        o_parties = _get_otomax_parties(o)
        for b in unmatched_banks:
            if b.id in used_b:
                continue
            if b.amount != o.amount:
                continue
            if not _is_channel_compatible(b, o):
                continue

            # MerchantMap check untuk QRIS
            if b.channel == Channel.MERCHANT_BCA and b.external_ref:
                mapped_rid = mmap.get(b.external_ref)
                if mapped_rid and o.reseller_id and o.reseller_id == mapped_rid:
                    pairs.append((b, o, "Cocok pemetaan merchant ID & nominal persis"))
                    used_b.add(b.id)
                    used_o.add(o.id)
                    break

            b_parties = _get_bank_parties(b)
            if not b_parties or not o_parties:
                continue

            best_sim, best_name = _calc_party_similarity(b_parties, o_parties)
            if best_sim >= 75.0:
                pairs.append((b, o, f"Cocok referensi nama ({best_name}) & nominal persis"))
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

            # 1. Pastikan channel kompatibel
            if not _is_channel_compatible(bm, oe):
                continue

            b_parties = _get_bank_parties(bm)
            o_parties = _get_otomax_parties(oe)

            # 2. Khusus QRIS (MERCHANT_BCA):
            # DILARANG pasang buta hanya karena nominal unik jika nama outlet & reseller tidak cocok / bertolak belakang!
            if bm.channel == Channel.MERCHANT_BCA:
                if bm.external_ref and mmap:
                    mapped_rid = mmap.get(bm.external_ref)
                    if mapped_rid and oe.reseller_id:
                        if mapped_rid != oe.reseller_id:
                            continue
                        pairs.append((bm, oe, f"Cocok referensi nominal unik & MerchantMap (Rp {amt:,.0f})"))
                        used_b.add(bm.id)
                        used_o.add(oe.id)
                        continue

                if not b_parties or not o_parties:
                    continue

                best_sim, _ = _calc_party_similarity(b_parties, o_parties)
                if best_sim < 75.0:
                    continue

            # 3. Bank Transfer Non-QRIS:
            # Jika kedua belah pihak memiliki nama pihak pengirim/reseller,
            # pastikan nama TIDAK bertentangan (misal EDI SUHARTO vs BUDI SANTOSO).
            if b_parties and o_parties:
                best_sim, _ = _calc_party_similarity(b_parties, o_parties)
                if best_sim < 50.0:
                    continue

            pairs.append((bm, oe, f"Cocok referensi nominal unik persis (Rp {amt:,.0f})"))
            used_b.add(bm.id)
            used_o.add(oe.id)

    return pairs


def _sync_inconsistent_batches():
    """Otomatis selaraskan tanggal batch jika berbeda dengan tanggal transaksi di dalamnya,
    dan bersihkan batch kosong tanpa transaksi."""
    # Bersihkan batch kosong tanpa data transaksi secara bulk
    ImportBatch.objects.filter(
        mutations__isnull=True,
        otomax__isnull=True,
        debits__isnull=True,
        excluded_transactions__isnull=True,
    ).delete()

    # Hanya periksa batch yang memiliki transaksi dengan tanggal berbeda dari tanggal batch
    inconsistent_batch_ids = set(
        ImportBatch.objects.filter(
            models.Q(mutations__isnull=False) & ~models.Q(mutations__book_date=models.F("book_date"))
        ).values_list("id", flat=True)
    ) | set(
        ImportBatch.objects.filter(
            models.Q(otomax__isnull=False) & ~models.Q(otomax__book_date=models.F("book_date"))
        ).values_list("id", flat=True)
    )

    if inconsistent_batch_ids:
        for b in ImportBatch.objects.filter(id__in=inconsistent_batch_ids):
            mut_dates = list(b.mutations.values_list("book_date", flat=True))
            oto_dates = list(b.otomax.values_list("book_date", flat=True))
            all_dates = mut_dates + oto_dates
            if all_dates:
                target_date = max(set(all_dates), key=all_dates.count)
                if b.book_date != target_date:
                    b.book_date = target_date
                    b.save(update_fields=["book_date", "updated_at"])


def build_today_steps(book_date: date) -> list[dict]:
    """Checklist 'Langkah Hari Ini' — urutan kerja harian yang sebenarnya, dengan status
    dihitung dari data riil (bukan checklist statis), supaya operator baru tidak perlu
    hafal struktur menu untuk tahu harus mulai dari mana. Ditampilkan di halaman Upload
    Data (pintu masuk alur kerja harian), sengaja BUKAN di Dashboard supaya Dashboard
    tetap ringkasan/KPI satu layar."""
    d = book_date.isoformat()
    day = ReconDay.objects.filter(book_date=book_date).first()
    summary = get_daily_summary(book_date)
    has_import_batch = ImportBatch.objects.filter(book_date=book_date).exists()
    engine_has_run = (
        Match.objects.filter(book_date=book_date, voided_at__isnull=True).exists()
        or Discrepancy.objects.filter(origin_book_date=book_date).exists()
    )
    reversal_open_count = OtomaxEntry.objects.filter(
        book_date=book_date, category=OtomaxCategory.REVERSAL, match_status__in=_REVERSAL_OPEN_STATUSES
    ).count()

    steps = [
        {
            "title": "Upload Data",
            "desc": "Import mutasi bank & data Otomax untuk tanggal ini."
            if not has_import_batch
            else "Data sudah diimport.",
            "done": has_import_batch,
            "url": f"{reverse('upload')}?d={d}",
        },
        {
            "title": "Jalankan Matching Engine",
            "desc": (
                "Cocokkan otomatis mutasi bank vs Otomax." if not engine_has_run else "Pencocokan sudah dijalankan."
            ),
            "done": engine_has_run,
            "url": f"{reverse('day')}?d={d}#aksi-rekonsiliasi",
        },
        {
            "title": "Selesaikan Review Manual",
            "desc": (
                "Jalankan matching engine dulu."
                if not engine_has_run
                else f"{summary['unmatched_bank_count']} mutasi bank belum ada pasangan."
                if summary["unmatched_bank_count"]
                else "Semua mutasi bank sudah tertangani."
            ),
            # Cuma dianggap "selesai" kalau engine-nya SUDAH dijalankan dan hitungannya
            # nol -- di hari kosong (belum ada data sama sekali) hitungannya juga
            # trivially nol, tapi itu bukan "sudah selesai", cuma "belum mulai".
            "done": engine_has_run and summary["unmatched_bank_count"] == 0,
            "url": f"{reverse('manual-review')}?d={d}",
        },
        {
            "title": "Selesaikan Pending Settle",
            "desc": (
                "Jalankan matching engine dulu."
                if not engine_has_run
                else f"{summary['pending_settle_count']} entri Otomax belum ada pasangan."
                if summary["pending_settle_count"]
                else "Semua entri Otomax sudah tertangani."
            ),
            "done": engine_has_run and summary["pending_settle_count"] == 0,
            "url": f"{reverse('pending-settle')}?d={d}",
        },
        {
            "title": "Cek Reversal Otomax",
            "desc": (
                "Jalankan matching engine dulu."
                if not engine_has_run
                else f"{reversal_open_count} baris REV belum netted."
                if reversal_open_count
                else "Semua REV sudah netted."
            ),
            "done": engine_has_run and reversal_open_count == 0,
            "url": f"{reverse('reversal')}?d={d}",
        },
        {
            "title": "Tutup Buku Harian",
            "desc": "Kunci data tanggal ini setelah semua selisih di atas ditangani.",
            "done": bool(day and day.locked),
            "url": f"{reverse('day')}?d={d}#aksi-rekonsiliasi",
        },
    ]
    next_index = next((i for i, s in enumerate(steps) if not s["done"]), None)
    for i, s in enumerate(steps):
        s["number"] = i + 1
        s["is_next"] = i == next_index
    return steps
