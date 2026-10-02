"""Layanan pembuat teks format laporan harian WhatsApp.

Menyusun ringkasan angka rekonsiliasi hari ini, daftar selisih outstanding (baru),
serta update perkembangan selisih hari sebelumnya (yang sudah selesai & yang masih terbuka).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from apps.core.enums import DiscrepancyStatus
from apps.recon.models import Discrepancy
from apps.recon.reports import get_daily_summary

ZERO = Decimal("0.00")


def _format_rupiah(val: Decimal | int | float | None) -> str:
    if val is None:
        return "0"
    try:
        d = Decimal(str(val)).quantize(Decimal("1"))
    except (InvalidOperation, TypeError, ValueError):
        return str(val)
    return f"{d:,}".replace(",", ".")


def generate_whatsapp_recon_text(book_date: date) -> str:
    """Menghasilkan teks laporan rekonsiliasi siap kirim ke WhatsApp."""
    summary = get_daily_summary(book_date)

    # 1. Selisih terbuka di tanggal buku ini
    outstanding_qs = (
        Discrepancy.objects.filter(origin_book_date=book_date, status=DiscrepancyStatus.OPEN)
        .select_related("bank_mutation", "otomax_entry", "otomax_entry__reseller")
        .order_by("code")
    )
    outstanding_list = list(outstanding_qs)

    # 2. Tanggal buku sebelumnya yang relevan (cari tanggal terakhir sebelum book_date yang memiliki selisih)
    prev_discrepancy = (
        Discrepancy.objects.filter(origin_book_date__lt=book_date)
        .order_by("-origin_book_date")
        .values_list("origin_book_date", flat=True)
        .first()
    )
    prev_date = prev_discrepancy or (book_date - timedelta(days=1))

    # Selisih hari sebelumnya yang SUDAH selesai
    prev_resolved = list(
        Discrepancy.objects.filter(
            origin_book_date=prev_date,
            status__in=[DiscrepancyStatus.RESOLVED, DiscrepancyStatus.WRITTEN_OFF],
        )
        .select_related("bank_mutation", "otomax_entry")
        .order_by("code")
    )

    # Selisih hari sebelumnya yang MASIH terbuka
    prev_open = list(
        Discrepancy.objects.filter(
            origin_book_date=prev_date,
            status=DiscrepancyStatus.OPEN,
        )
        .select_related("bank_mutation", "otomax_entry")
        .order_by("code")
    )

    prev_open_total = sum((d.amount for d in prev_open), ZERO)

    lines = [
        "📊 *LAPORAN REKONSILIASI HARIAN*",
        f"📅 *Tanggal Rekon:* {book_date.strftime('%d %B %Y')}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "💰 *RINGKASAN REKONSILIASI:*",
        f"• Total Uang Masuk Bank : Rp {_format_rupiah(summary['total_bank'])}",
        f"• Total Penambahan Otomax : Rp {_format_rupiah(summary['total_otomax'])}",
        f"• Selisih Bersih Hari Ini : Rp {_format_rupiah(summary['selisih'])}",
        f"• Transaksi Cocok : {summary['matched_auto_count'] + summary['matched_manual_count']} transaksi",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # Bagian Selisih Outstanding Hari Ini
    if outstanding_list:
        lines.append(f"🔴 *OUTSTANDING HARI INI ({len(outstanding_list)} Selisih):*")
        for idx, d in enumerate(outstanding_list, start=1):
            bm = d.bank_mutation
            oe = d.otomax_entry
            party = "-"
            if oe and oe.reseller_name_raw:
                party = oe.reseller_name_raw
            elif bm and bm.outlet_name:
                party = bm.outlet_name

            channel_str = d.get_channel_display()
            kind_str = d.get_kind_display()
            lines.append(f"{idx}. {channel_str} | Rp {_format_rupiah(abs(d.amount))} ({kind_str})")
            if party != "-":
                lines.append(f"   • Pihak: {party}")

            desc = (bm.description_raw if bm else (oe.description_raw if oe else "")).strip()
            if desc:
                # Batasi panjang keterangan jika terlalu panjang
                short_desc = (desc[:65] + "...") if len(desc) > 65 else desc
                lines.append(f"   • Ket: {short_desc}")

            if d.note:
                lines.append(f"   👉 *Catatan:* {d.note}")
            lines.append("")
    else:
        lines.append("🟢 *OUTSTANDING HARI INI:*")
        lines.append("Alhamdulillah, tidak ada selisih terbuka (Semua klop! ✅)")
        lines.append("")

    # Bagian Update Hari Sebelumnya
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🔄 *UPDATE PROGRESS TGL SEBELUMNYA ({prev_date.strftime('%d %b %Y')}):*")
    total_prev = len(prev_resolved) + len(prev_open)
    if total_prev > 0:
        lines.append(
            f"Status: *{len(prev_resolved)} Selesai*, *{len(prev_open)} Masih Terbuka* "
            f"(Sisa Selisih: Rp {_format_rupiah(prev_open_total)})"
        )
        lines.append("")

        if prev_resolved:
            lines.append(f"✅ *Sudah Selesai / Klop ({len(prev_resolved)}):*")
            for d in prev_resolved:
                bm = d.bank_mutation
                oe = d.otomax_entry
                p = (oe.reseller_name_raw if oe else (bm.outlet_name if bm else "")).strip()
                p_str = f" | {p}" if p else ""
                lines.append(f" • {d.get_channel_display()} Rp {_format_rupiah(abs(d.amount))}{p_str} ({d.code})")
            lines.append("")

        if prev_open:
            lines.append(f"⏳ *Masih Belum Selesai ({len(prev_open)}):*")
            for idx, d in enumerate(prev_open, start=1):
                bm = d.bank_mutation
                oe = d.otomax_entry
                p = (oe.reseller_name_raw if oe else (bm.outlet_name if bm else "")).strip()
                p_str = f" | {p}" if p else ""
                lines.append(f" {idx}. {d.get_channel_display()} Rp {_format_rupiah(abs(d.amount))}{p_str}")
                if d.note:
                    lines.append(f"    👉 *Catatan:* {d.note}")
            lines.append("")
    else:
        lines.append("Tidak ada riwayat selisih terbuka di tanggal sebelumnya.")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines).strip()
