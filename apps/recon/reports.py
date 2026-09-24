"""Layanan laporan rekapitulasi dan export Excel (.xlsx) rekonsiliasi."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import openpyxl
from django.db.models import Sum
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from apps.core.enums import BANK_CHANNELS, DiscrepancyKind, DiscrepancyStatus, MatchStatus, MatchType, OtomaxCategory
from apps.ingest.models import BankMutation, OtomaxEntry

from .models import Discrepancy, Match, ReconDay

ZERO = Decimal("0.00")


def _per_bank_breakdown(bank_qs, otomax_all_qs) -> tuple[dict, dict]:
    """Rekapitulasi per channel bank, DITAMBAH sisi Otomax (semua kategori, bukan cuma
    TOPUP_TARTUN) yang channel_hint-nya cocok dengan channel bank itu. Entri Otomax tanpa
    channel_hint (mis. ADMIN, STOR_IN/STOR_OUT) tidak masuk channel manapun — supaya tidak
    "hilang" dari rekap, dikumpulkan terpisah sebagai bucket lain_lain."""
    per_bank = {}
    for ch in BANK_CHANNELS:
        ch_mut = bank_qs.filter(channel=ch)
        ch_otomax = otomax_all_qs.filter(channel_hint=ch)
        per_bank[ch] = {
            "total": ch_mut.aggregate(s=Sum("amount"))["s"] or ZERO,
            "matched_auto": ch_mut.filter(match_status=MatchStatus.MATCHED).aggregate(s=Sum("amount"))["s"] or ZERO,
            "matched_manual": ch_mut.filter(match_status=MatchStatus.MANUAL).aggregate(s=Sum("amount"))["s"] or ZERO,
            "unmatched": ch_mut.filter(match_status=MatchStatus.UNMATCHED).aggregate(s=Sum("amount"))["s"] or ZERO,
            "unmatched_count": ch_mut.filter(match_status=MatchStatus.UNMATCHED).count(),
            "otomax_total": ch_otomax.aggregate(s=Sum("amount"))["s"] or ZERO,
            "otomax_count": ch_otomax.count(),
        }

    otomax_lain_lain = otomax_all_qs.exclude(channel_hint__in=BANK_CHANNELS)
    lain_lain = {
        "otomax_total": otomax_lain_lain.aggregate(s=Sum("amount"))["s"] or ZERO,
        "otomax_count": otomax_lain_lain.count(),
    }
    return per_bank, lain_lain


def get_daily_summary(book_date: date) -> dict:
    """Ringkasan rekonsiliasi harian."""
    bank_qs = BankMutation.objects.filter(book_date=book_date)
    otomax_qs = OtomaxEntry.objects.filter(book_date=book_date, category=OtomaxCategory.TOPUP_TARTUN).exclude(
        match_status=MatchStatus.IGNORED
    )
    otomax_all_qs = OtomaxEntry.objects.filter(book_date=book_date).exclude(match_status=MatchStatus.IGNORED)

    total_bank = bank_qs.filter(amount__gt=0).aggregate(s=Sum("amount"))["s"] or ZERO
    total_otomax = otomax_qs.aggregate(s=Sum("amount"))["s"] or ZERO

    matches = Match.objects.filter(book_date=book_date, voided_at__isnull=True)
    matched_auto = matches.exclude(match_type=MatchType.MANUAL)
    matched_manual = bank_qs.filter(match_status=MatchStatus.MANUAL)
    unmatched_bank = bank_qs.filter(match_status=MatchStatus.UNMATCHED)
    pending_settle = OtomaxEntry.objects.filter(
        book_date=book_date,
        category=OtomaxCategory.TOPUP_TARTUN,
        match_status__in=[MatchStatus.PENDING_SETTLE, MatchStatus.UNMATCHED],
    )
    unmatched_bank_amount = unmatched_bank.aggregate(s=Sum("amount"))["s"] or ZERO
    pending_settle_amount = pending_settle.aggregate(s=Sum("amount"))["s"] or ZERO
    # Pasangan yang SUDAH matched tapi nominalnya beda (mis. cocok manual/QRIS toleransi
    # nominal) tidak lagi masuk unmatched_bank/pending_settle di atas -- selisihnya cuma
    # kelihatan lewat Discrepancy(AMOUNT_DIFF) yang dibuat khusus untuk pasangan itu
    # (lihat manual_pair_transactions & qris_match Pass 3), jadi harus ditambahkan manual
    # supaya tidak hilang dari total selisih.
    amount_diff_qs = Discrepancy.objects.filter(
        origin_book_date=book_date, kind=DiscrepancyKind.AMOUNT_DIFF, status=DiscrepancyStatus.OPEN
    )
    amount_diff_amount = amount_diff_qs.aggregate(s=Sum("amount"))["s"] or ZERO
    amount_diff_count = amount_diff_qs.count()
    # Selisih riil = sisa PR rekonsiliasi (bank tanpa pasangan - otomax tanpa pasangan),
    # BUKAN total_bank - total_otomax: itu naive per book_date yang sama, jadi salah besar
    # kalau Otomax-nya dicatat lintas tanggal (mis. QRIS diinput H+1) padahal sudah matched.
    selisih = unmatched_bank_amount - pending_settle_amount + amount_diff_amount

    day = ReconDay.objects.filter(book_date=book_date).first()

    per_bank, otomax_lain_lain = _per_bank_breakdown(bank_qs, otomax_all_qs)

    return {
        "book_date": book_date,
        "day_status": day.status if day else "DRAFT",
        "day_locked": day.locked if day else False,
        "total_bank": total_bank,
        "total_otomax": total_otomax,
        "selisih": selisih,
        "matched_auto_count": matched_auto.count(),
        "matched_auto_amount": matched_auto.aggregate(s=Sum("amount_bank"))["s"] or ZERO,
        "matched_manual_count": matched_manual.count(),
        "matched_manual_amount": matched_manual.aggregate(s=Sum("amount"))["s"] or ZERO,
        "unmatched_bank_count": unmatched_bank.count(),
        "unmatched_bank_amount": unmatched_bank_amount,
        "pending_settle_count": pending_settle.count(),
        "pending_settle_amount": pending_settle_amount,
        "amount_diff_count": amount_diff_count,
        "amount_diff_amount": amount_diff_amount,
        "per_bank": per_bank,
        "otomax_lain_lain": otomax_lain_lain,
    }


def get_range_summary(start_date: date, end_date: date) -> dict:
    """Ringkasan rekonsiliasi rentang tanggal."""
    bank_qs = BankMutation.objects.filter(book_date__range=(start_date, end_date))
    otomax_qs = OtomaxEntry.objects.filter(
        book_date__range=(start_date, end_date),
        category=OtomaxCategory.TOPUP_TARTUN,
    ).exclude(match_status=MatchStatus.IGNORED)
    otomax_all_qs = OtomaxEntry.objects.filter(book_date__range=(start_date, end_date)).exclude(
        match_status=MatchStatus.IGNORED
    )

    total_bank = bank_qs.filter(amount__gt=0).aggregate(s=Sum("amount"))["s"] or ZERO
    total_otomax = otomax_qs.aggregate(s=Sum("amount"))["s"] or ZERO

    matches = Match.objects.filter(book_date__range=(start_date, end_date), voided_at__isnull=True)
    matched_auto = matches.exclude(match_type=MatchType.MANUAL)
    matched_manual = bank_qs.filter(match_status=MatchStatus.MANUAL)
    unmatched_bank = bank_qs.filter(match_status=MatchStatus.UNMATCHED)
    pending_settle = OtomaxEntry.objects.filter(
        book_date__range=(start_date, end_date),
        category=OtomaxCategory.TOPUP_TARTUN,
        match_status__in=[MatchStatus.PENDING_SETTLE, MatchStatus.UNMATCHED],
    )
    unmatched_bank_amount = unmatched_bank.aggregate(s=Sum("amount"))["s"] or ZERO
    pending_settle_amount = pending_settle.aggregate(s=Sum("amount"))["s"] or ZERO
    # Lihat catatan yang sama di get_daily_summary soal kenapa selisih riil bukan total_bank
    # - total_otomax, dan soal kenapa Discrepancy(AMOUNT_DIFF) yang OPEN perlu ditambahkan manual.
    amount_diff_amount = (
        Discrepancy.objects.filter(
            origin_book_date__range=(start_date, end_date),
            kind=DiscrepancyKind.AMOUNT_DIFF,
            status=DiscrepancyStatus.OPEN,
        ).aggregate(s=Sum("amount"))["s"]
        or ZERO
    )
    selisih = unmatched_bank_amount - pending_settle_amount + amount_diff_amount

    per_bank, otomax_lain_lain = _per_bank_breakdown(bank_qs, otomax_all_qs)

    return {
        "start_date": start_date,
        "end_date": end_date,
        "total_bank": total_bank,
        "total_otomax": total_otomax,
        "selisih": selisih,
        "matched_auto_count": matched_auto.count(),
        "matched_auto_amount": matched_auto.aggregate(s=Sum("amount_bank"))["s"] or ZERO,
        "matched_manual_count": matched_manual.count(),
        "matched_manual_amount": matched_manual.aggregate(s=Sum("amount"))["s"] or ZERO,
        "unmatched_bank_count": unmatched_bank.count(),
        "unmatched_bank_amount": unmatched_bank_amount,
        "pending_settle_count": pending_settle.count(),
        "pending_settle_amount": pending_settle_amount,
        "per_bank": per_bank,
        "otomax_lain_lain": otomax_lain_lain,
    }


def generate_excel_report(start_date: date, end_date: date) -> bytes:
    """Generate multi-sheet Excel (.xlsx) report using openpyxl."""
    wb = openpyxl.Workbook()
    ws_summary = wb.active
    ws_summary.title = "Ringkasan"

    summary = get_range_summary(start_date, end_date)

    header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
    title_font = Font(name="Arial", size=14, bold=True)
    bold_font = Font(name="Arial", size=10, bold=True)
    regular_font = Font(name="Arial", size=10)
    money_format = "#,##0"

    primary_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    sub_fill = PatternFill(start_color="3B82F6", end_color="3B82F6", fill_type="solid")
    alt_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

    thin_border = Border(
        left=Side(style="thin", color="E2E8F0"),
        right=Side(style="thin", color="E2E8F0"),
        top=Side(style="thin", color="E2E8F0"),
        bottom=Side(style="thin", color="E2E8F0"),
    )

    ws_summary["A1"] = "LAPORAN REKONSILIASI MUTASI BANK VS OTOMAX"
    ws_summary["A1"].font = title_font
    ws_summary["A2"] = f"Periode: {start_date} s/d {end_date}"
    ws_summary["A2"].font = regular_font

    overview_headers = ["Indikator", "Nilai / Jumlah", "Total Nominal (Rp)"]
    for col_idx, h in enumerate(overview_headers, start=1):
        cell = ws_summary.cell(row=4, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = primary_fill
        cell.alignment = Alignment(horizontal="center" if col_idx > 1 else "left")

    overview_rows = [
        ("Total Uang Masuk Bank", "-", summary["total_bank"]),
        ("Total Penambahan Otomax", "-", summary["total_otomax"]),
        ("Selisih Bersih (Bank - Otomax)", "-", summary["selisih"]),
        ("Matched Otomatis", f"{summary['matched_auto_count']} transaksi", summary["matched_auto_amount"]),
        ("Matched Manual (Di-tag)", f"{summary['matched_manual_count']} transaksi", summary["matched_manual_amount"]),
        (
            "Mutasi Bank Belum Cocok (Antrean Manual)",
            f"{summary['unmatched_bank_count']} baris",
            summary["unmatched_bank_amount"],
        ),
        (
            "Otomax Belum Cocok (Pending Settle)",
            f"{summary['pending_settle_count']} baris",
            summary["pending_settle_amount"],
        ),
    ]

    for r_idx, (ind, cnt, amt) in enumerate(overview_rows, start=5):
        c1 = ws_summary.cell(row=r_idx, column=1, value=ind)
        c2 = ws_summary.cell(row=r_idx, column=2, value=cnt)
        c3 = ws_summary.cell(row=r_idx, column=3, value=float(amt))
        c3.number_format = money_format
        for c in (c1, c2, c3):
            c.font = regular_font
            c.border = thin_border
            if r_idx % 2 == 0:
                c.fill = alt_fill

    start_bank_row = len(overview_rows) + 7
    ws_summary.cell(row=start_bank_row - 1, column=1, value="Rekapitulasi Per Bank").font = bold_font

    bank_headers = [
        "Bank / Channel",
        "Total Masuk (Rp)",
        "Total Otomax (Rp)",
        "Matched Auto (Rp)",
        "Matched Manual (Rp)",
        "Selisih Unmatched (Rp)",
        "Jml Unmatched",
    ]
    for col_idx, h in enumerate(bank_headers, start=1):
        cell = ws_summary.cell(row=start_bank_row, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = sub_fill
        cell.alignment = Alignment(horizontal="center")

    curr_r = start_bank_row + 1
    for ch, data in summary["per_bank"].items():
        ws_summary.cell(row=curr_r, column=1, value=ch).font = bold_font
        ws_summary.cell(row=curr_r, column=2, value=float(data["total"])).number_format = money_format
        ws_summary.cell(row=curr_r, column=3, value=float(data["otomax_total"])).number_format = money_format
        ws_summary.cell(row=curr_r, column=4, value=float(data["matched_auto"])).number_format = money_format
        ws_summary.cell(row=curr_r, column=5, value=float(data["matched_manual"])).number_format = money_format
        ws_summary.cell(row=curr_r, column=6, value=float(data["unmatched"])).number_format = money_format
        ws_summary.cell(row=curr_r, column=7, value=data["unmatched_count"]).alignment = Alignment(horizontal="center")
        for c in range(1, 8):
            ws_summary.cell(row=curr_r, column=c).border = thin_border
        curr_r += 1

    lain_lain = summary["otomax_lain_lain"]
    if lain_lain["otomax_count"] > 0:
        ws_summary.cell(row=curr_r, column=1, value="Lain-lain (Tanpa Channel)").font = bold_font
        ws_summary.cell(row=curr_r, column=3, value=float(lain_lain["otomax_total"])).number_format = money_format
        ws_summary.cell(row=curr_r, column=7, value=lain_lain["otomax_count"]).alignment = Alignment(
            horizontal="center"
        )
        for c in range(1, 8):
            ws_summary.cell(row=curr_r, column=c).border = thin_border
        curr_r += 1

    ws_matched = wb.create_sheet(title="Matched Otomatis")
    matched_headers = [
        "Tanggal Buku",
        "Channel",
        "Tipe Match",
        "Keterangan Bank",
        "Nominal Bank (Rp)",
        "Reseller Otomax",
        "Keterangan Otomax",
        "Nominal Otomax (Rp)",
        "Selisih (Rp)",
    ]
    for col_idx, h in enumerate(matched_headers, start=1):
        cell = ws_matched.cell(row=1, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = primary_fill
        cell.alignment = Alignment(horizontal="center")

    matches_qs = (
        Match.objects.filter(book_date__range=(start_date, end_date), voided_at__isnull=True)
        .select_related("bank_mutation", "otomax_entry")
        .order_by("-book_date")
    )

    row_num = 2
    for m in matches_qs[:3000]:
        bm = m.bank_mutation
        oe = m.otomax_entry
        ws_matched.cell(row=row_num, column=1, value=str(m.book_date))
        ws_matched.cell(row=row_num, column=2, value=m.channel)
        ws_matched.cell(row=row_num, column=3, value=m.get_match_type_display())
        ws_matched.cell(row=row_num, column=4, value=bm.description_raw if bm else "-")
        c5 = ws_matched.cell(row=row_num, column=5, value=float(m.amount_bank))
        c5.number_format = money_format
        ws_matched.cell(row=row_num, column=6, value=oe.reseller_name_raw if oe else "-")
        ws_matched.cell(row=row_num, column=7, value=oe.description_raw if oe else "-")
        c8 = ws_matched.cell(row=row_num, column=8, value=float(m.amount_otomax))
        c8.number_format = money_format
        c9 = ws_matched.cell(row=row_num, column=9, value=float(m.amount_diff))
        c9.number_format = money_format
        for c in range(1, 10):
            ws_matched.cell(row=row_num, column=c).border = thin_border
        row_num += 1

    ws_manual = wb.create_sheet(title="Review Manual")
    manual_headers = [
        "Tanggal",
        "Channel",
        "Nominal (Rp)",
        "Keterangan Bank",
        "Token",
        "Outlet",
        "Status",
        "Tag Kategori",
        "Catatan",
    ]
    for col_idx, h in enumerate(manual_headers, start=1):
        cell = ws_manual.cell(row=1, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = primary_fill
        cell.alignment = Alignment(horizontal="center")

    bank_review_qs = BankMutation.objects.filter(
        book_date__range=(start_date, end_date),
        match_status__in=[MatchStatus.UNMATCHED, MatchStatus.MANUAL],
    ).order_by("-book_date")

    row_num = 2
    for b in bank_review_qs[:3000]:
        ws_manual.cell(row=row_num, column=1, value=str(b.book_date))
        ws_manual.cell(row=row_num, column=2, value=b.channel)
        c3 = ws_manual.cell(row=row_num, column=3, value=float(b.amount))
        c3.number_format = money_format
        ws_manual.cell(row=row_num, column=4, value=b.description_raw)
        ws_manual.cell(row=row_num, column=5, value=", ".join(b.extracted_tokens or []))
        ws_manual.cell(row=row_num, column=6, value=b.outlet_name or "-")
        ws_manual.cell(row=row_num, column=7, value=b.get_match_status_display())
        ws_manual.cell(row=row_num, column=8, value=b.get_tag_manual_display() if b.tag_manual else "-")
        ws_manual.cell(row=row_num, column=9, value=b.manual_note or "-")
        for c in range(1, 10):
            ws_manual.cell(row=row_num, column=c).border = thin_border
        row_num += 1

    ws_pending = wb.create_sheet(title="Pending Settle")
    pending_headers = ["Tanggal", "Reseller", "Nominal (Rp)", "Keterangan Otomax", "Perkiraan Bank", "Token", "Status"]
    for col_idx, h in enumerate(pending_headers, start=1):
        cell = ws_pending.cell(row=1, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = primary_fill
        cell.alignment = Alignment(horizontal="center")

    pending_qs = OtomaxEntry.objects.filter(
        book_date__range=(start_date, end_date),
        category=OtomaxCategory.TOPUP_TARTUN,
        match_status__in=[MatchStatus.PENDING_SETTLE, MatchStatus.UNMATCHED],
    ).order_by("-book_date")

    row_num = 2
    for o in pending_qs[:3000]:
        ws_pending.cell(row=row_num, column=1, value=str(o.book_date))
        ws_pending.cell(row=row_num, column=2, value=o.reseller_name_raw)
        c3 = ws_pending.cell(row=row_num, column=3, value=float(o.amount))
        c3.number_format = money_format
        ws_pending.cell(row=row_num, column=4, value=o.description_raw)
        ws_pending.cell(row=row_num, column=5, value=o.channel_hint or "-")
        ws_pending.cell(row=row_num, column=6, value=", ".join(o.extracted_tokens or []))
        ws_pending.cell(row=row_num, column=7, value=o.get_match_status_display())
        for c in range(1, 8):
            ws_pending.cell(row=row_num, column=c).border = thin_border
        row_num += 1

    for sheet in (ws_summary, ws_matched, ws_manual, ws_pending):
        for col in sheet.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                val = str(cell.value or "")
                if len(val) > max_len:
                    max_len = len(val)
            sheet.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 45)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
