import hashlib
from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.core.enums import Channel, OtomaxCategory
from apps.core.normalize import extract_tokens, norm_ref, ref_core
from apps.ingest.models import BankMutation, ImportBatch, OtomaxEntry
from apps.recon.engine import run_match
from apps.recon.reports import get_daily_summary, get_range_summary

BD = date(2026, 9, 11)
BD_NEXT = BD + timedelta(days=1)


def _h(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _batch(channel, book_date):
    return ImportBatch.objects.create(
        channel=channel, book_date=book_date, source_filename="t", file_hash=_h(channel, book_date)
    )


def _bank(desc, amount, channel=Channel.BRI, book_date=BD):
    return BankMutation.objects.create(
        import_batch=_batch(channel, book_date),
        channel=channel,
        book_date=book_date,
        description_raw=desc,
        ref_normalized=norm_ref(desc),
        ref_core=ref_core(desc),
        extracted_tokens=extract_tokens(desc),
        amount=Decimal(amount),
        row_hash=_h("b", desc, amount, book_date),
    )


def _otomax(desc, amount, channel=Channel.BRI, book_date=BD):
    return OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX, book_date),
        book_date=book_date,
        reseller_name_raw="X",
        amount=Decimal(amount),
        description_raw=desc,
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=channel,
        ref_normalized=norm_ref(desc),
        ref_core=ref_core(desc),
        extracted_tokens=extract_tokens(desc),
        row_hash=_h("o", desc, amount, book_date),
    )


@pytest.mark.django_db
def test_selisih_ignores_matches_recorded_on_a_later_otomax_book_date():
    """Reproduksi kasus nyata: 44 mutasi bank BRI di tanggal BD sudah matched ke entry
    Otomax yang diinput operator H+1 (lazim buat QRIS), plus 1 mutasi bank yang memang
    tidak ada pasangannya sama sekali. 'Selisih' harus cuma menghitung sisa PR riil
    (Rp 20.500), bukan total_bank - total_otomax yang meledak jadi besar gara-gara
    Otomax-nya "pindah tanggal" ke BD_NEXT."""
    _bank("REF-COCOK-001", "203670409", book_date=BD)
    _otomax("REF-COCOK-001", "203670409", book_date=BD_NEXT)
    _bank("REF-TANPA-PASANGAN", "20500", book_date=BD)

    run_match(BD)

    summary = get_daily_summary(BD)
    assert summary["total_otomax"] == Decimal("0.00")
    assert summary["matched_auto_count"] == 1
    assert summary["unmatched_bank_amount"] == Decimal("20500.00")
    assert summary["selisih"] == Decimal("20500.00")


@pytest.mark.django_db
def test_range_summary_selisih_matches_daily_summary_definition():
    _bank("REF-COCOK-002", "100000", book_date=BD)
    _otomax("REF-COCOK-002", "100000", book_date=BD_NEXT)
    _bank("REF-TANPA-PASANGAN-2", "5000", book_date=BD)

    run_match(BD)

    summary = get_range_summary(BD, BD_NEXT)
    assert summary["selisih"] == Decimal("5000.00")
