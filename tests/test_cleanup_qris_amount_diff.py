import io
from datetime import date
from decimal import Decimal

import pytest
from django.core.management import call_command

from apps.catalog.models import MerchantMap, Reseller
from apps.core.enums import Channel, MatchStatus, MatchType, OtomaxCategory
from apps.ingest.models import BankMutation, ImportBatch, OtomaxEntry
from apps.recon.engine import run_match
from apps.recon.models import Match

BD = date(2026, 9, 5)


def _batch(channel):
    return ImportBatch.objects.create(channel=channel, book_date=BD, source_filename="t", file_hash="h")


def _run_cleanup(*args):
    out = io.StringIO()
    call_command("cleanup_qris_amount_diff", *args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
def test_dry_run_reports_but_does_not_change_anything():
    r = Reseller.objects.create(code="ALFA9", name="Alfa 9")
    b = BankMutation.objects.create(
        import_batch=_batch(Channel.MERCHANT_BCA),
        channel=Channel.MERCHANT_BCA,
        book_date=BD,
        description_raw="QRIS ALFA 9 CELL",
        ref_normalized="QRIS",
        amount=Decimal("9995000"),
        external_ref="004767951",
        row_hash="qb1",
    )
    o = OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=BD,
        reseller_name_raw="PLC ALFA9",
        reseller=r,
        amount=Decimal("5000000"),
        description_raw="TARTUN QR BULK TGL 05-SEP-2026",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.MERCHANT_BCA,
        ref_normalized="TARTUN",
        row_hash="oto1",
        match_status=MatchStatus.MATCHED,
    )
    m = Match.objects.create(
        book_date=BD,
        channel=Channel.MERCHANT_BCA,
        bank_mutation=b,
        otomax_entry=o,
        match_type=MatchType.AGGREGATE,
        amount_bank=b.amount,
        amount_otomax=o.amount,
        note="QRIS lama, selisih besar",
    )
    m.otomax_entries.set([o])
    b.match_status = MatchStatus.MATCHED
    b.save(update_fields=["match_status"])

    output = _run_cleanup()
    assert "DRY-RUN" in output
    assert "AKAN DIBATALKAN" in output

    m.refresh_from_db()
    b.refresh_from_db()
    o.refresh_from_db()
    assert m.voided_at is None
    assert b.match_status == MatchStatus.MATCHED
    assert o.match_status == MatchStatus.MATCHED


@pytest.mark.django_db
def test_apply_unpairs_out_of_tolerance_and_reconstructs_legacy_group():
    """Match lama (dibuat tanpa otomax_entries terisi, seolah dari sebelum field ini ada)
    harus berhasil direkonstruksi ulang lalu dibatalkan dengan benar."""
    r = Reseller.objects.create(code="ALFA9", name="Alfa 9")
    b = BankMutation.objects.create(
        import_batch=_batch(Channel.MERCHANT_BCA),
        channel=Channel.MERCHANT_BCA,
        book_date=BD,
        description_raw="QRIS ALFA 9 CELL",
        ref_normalized="QRIS",
        amount=Decimal("9995000"),
        external_ref="004767951",
        row_hash="qb1",
    )
    o1 = OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=BD,
        reseller_name_raw="PLC ALFA9",
        reseller=r,
        amount=Decimal("5000000"),
        description_raw="TARTUN QR BULK TGL 05-SEP-2026",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.MERCHANT_BCA,
        ref_normalized="TARTUN",
        row_hash="oto1",
        match_status=MatchStatus.MATCHED,
    )
    o2 = OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=BD,
        reseller_name_raw="PLC ALFA9",
        reseller=r,
        amount=Decimal("2000000"),
        description_raw="TARTUN QR BULK TGL 05-SEP-2026",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.MERCHANT_BCA,
        ref_normalized="TARTUN",
        row_hash="oto2",
        match_status=MatchStatus.MATCHED,
    )
    # Pengecoh: reseller yang sama juga punya TOPUP_TARTUN via channel BRI (mis. transfer
    # individual). Ini TIDAK boleh ikut kesedot rekonstruksi grup QRIS — kalau ikut,
    # jumlahnya akan meleset dari amount_otomax dan seharusnya bikin rekonstruksi gagal.
    OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=BD,
        reseller_name_raw="PLC ALFA9",
        reseller=r,
        amount=Decimal("300000"),
        description_raw="TARTUN TF BRI DANA20260905034895588601SOMEONE",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.BRI,
        ref_normalized="DANA20260905034895588601SOMEONE",
        row_hash="oto_decoy_bri",
        match_status=MatchStatus.MATCHED,
    )
    # otomax_entries SENGAJA tidak diisi -> mensimulasikan Match yang dibuat sebelum
    # field M2M ini ada, sehingga command harus merekonstruksinya dari amount_otomax.
    m = Match.objects.create(
        book_date=BD,
        channel=Channel.MERCHANT_BCA,
        bank_mutation=b,
        otomax_entry=o1,
        match_type=MatchType.AGGREGATE,
        amount_bank=b.amount,
        amount_otomax=Decimal("7000000"),
        note="QRIS lama, selisih besar",
    )
    b.match_status = MatchStatus.MATCHED
    b.save(update_fields=["match_status"])

    output = _run_cleanup("--apply")
    assert "APPLY" in output
    assert "grup direkonstruksi ulang" in output
    assert "dibatalkan" in output

    m.refresh_from_db()
    b.refresh_from_db()
    o1.refresh_from_db()
    o2.refresh_from_db()
    assert m.voided_at is not None
    assert b.match_status == MatchStatus.UNMATCHED
    assert o1.match_status == MatchStatus.PENDING_SETTLE
    assert o2.match_status == MatchStatus.PENDING_SETTLE
    assert set(m.otomax_entries.values_list("id", flat=True)) == {o1.id, o2.id}


@pytest.mark.django_db
def test_apply_skips_when_group_cannot_be_reconstructed():
    """Kalau jumlah kandidat tidak persis sama dengan amount_otomax, jangan menebak —
    lewati dan laporkan untuk ditinjau manual."""
    r = Reseller.objects.create(code="ALFA9", name="Alfa 9")
    b = BankMutation.objects.create(
        import_batch=_batch(Channel.MERCHANT_BCA),
        channel=Channel.MERCHANT_BCA,
        book_date=BD,
        description_raw="QRIS ALFA 9 CELL",
        ref_normalized="QRIS",
        amount=Decimal("9995000"),
        external_ref="004767951",
        row_hash="qb1",
    )
    o1 = OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=BD,
        reseller_name_raw="PLC ALFA9",
        reseller=r,
        amount=Decimal("5000000"),
        description_raw="TARTUN QR BULK TGL 05-SEP-2026",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.MERCHANT_BCA,
        ref_normalized="TARTUN",
        row_hash="oto1",
        match_status=MatchStatus.MATCHED,
    )
    m = Match.objects.create(
        book_date=BD,
        channel=Channel.MERCHANT_BCA,
        bank_mutation=b,
        otomax_entry=o1,
        match_type=MatchType.AGGREGATE,
        amount_bank=b.amount,
        # amount_otomax sengaja tidak sama dengan o1.amount ATAU jumlah kandidat apa pun
        # yang bisa direkonstruksi -> harus dilewati, bukan ditebak.
        amount_otomax=Decimal("6123456"),
        note="QRIS lama, ambigu",
    )

    output = _run_cleanup("--apply")
    assert "LEWATI" in output

    m.refresh_from_db()
    assert m.voided_at is None


@pytest.mark.django_db
def test_within_tolerance_match_is_not_touched(settings):
    settings.MATCH_AMOUNT_TOLERANCE = 100000
    r = Reseller.objects.create(code="ALFA9", name="Alfa 9")
    MerchantMap.objects.create(merchant_id="004767951", reseller=r)
    BankMutation.objects.create(
        import_batch=_batch(Channel.MERCHANT_BCA),
        channel=Channel.MERCHANT_BCA,
        book_date=BD,
        description_raw="QRIS ALFA 9 CELL",
        ref_normalized="QRIS",
        amount=Decimal("5050000"),
        external_ref="004767951",
        row_hash="qb1",
    )
    OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=BD,
        reseller_name_raw="PLC ALFA9",
        reseller=r,
        amount=Decimal("5000000"),
        description_raw="TARTUN QR BULK TGL 05-SEP-2026",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.MERCHANT_BCA,
        ref_normalized="TARTUN",
        row_hash="oto1",
    )
    run_match(BD)
    m = Match.objects.get(voided_at__isnull=True)
    assert abs(m.amount_diff) <= Decimal("100000")

    output = _run_cleanup("--apply")
    assert "Tidak ada match AGGREGATE di luar toleransi" in output

    m.refresh_from_db()
    assert m.voided_at is None


@pytest.mark.django_db
def test_reconstruction_does_not_leak_across_adjacent_days_same_reseller():
    """Reseller yang sama punya grup QRIS di dua hari berdekatan (hal biasa untuk
    reseller aktif harian). Rekonstruksi utk Match hari pertama TIDAK BOLEH ikut
    menyedot baris milik grup hari kedua walau keduanya masuk jendela H-1..H+2."""
    day1 = BD
    day2 = date(2026, 9, 6)
    r = Reseller.objects.create(code="ALFA9", name="Alfa 9")

    b1 = BankMutation.objects.create(
        import_batch=_batch(Channel.MERCHANT_BCA),
        channel=Channel.MERCHANT_BCA,
        book_date=day1,
        description_raw="QRIS ALFA 9 CELL",
        ref_normalized="QRIS",
        amount=Decimal("5100000"),
        external_ref="004767951",
        row_hash="qb_day1",
    )
    o1 = OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=day1,
        reseller_name_raw="PLC ALFA9",
        reseller=r,
        amount=Decimal("5000000"),
        description_raw="TARTUN QR BULK TGL 05-SEP-2026",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.MERCHANT_BCA,
        ref_normalized="TARTUN",
        row_hash="oto_day1",
        match_status=MatchStatus.MATCHED,
    )
    m1 = Match.objects.create(
        book_date=day1,
        channel=Channel.MERCHANT_BCA,
        bank_mutation=b1,
        otomax_entry=o1,
        match_type=MatchType.AGGREGATE,
        amount_bank=b1.amount,
        amount_otomax=o1.amount,
        note="hari 1",
    )
    b1.match_status = MatchStatus.MATCHED
    b1.save(update_fields=["match_status"])

    # Grup reseller yang SAMA di hari berikutnya — total 8 juta, beda dari hari 1.
    o2a = OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=day2,
        reseller_name_raw="PLC ALFA9",
        reseller=r,
        amount=Decimal("6000000"),
        description_raw="TARTUN QR BULK TGL 06-SEP-2026",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.MERCHANT_BCA,
        ref_normalized="TARTUN",
        row_hash="oto_day2a",
        match_status=MatchStatus.MATCHED,
    )
    o2b = OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=day2,
        reseller_name_raw="PLC ALFA9",
        reseller=r,
        amount=Decimal("2000000"),
        description_raw="TARTUN QR BULK TGL 06-SEP-2026",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.MERCHANT_BCA,
        ref_normalized="TARTUN",
        row_hash="oto_day2b",
        match_status=MatchStatus.MATCHED,
    )
    BankMutation.objects.create(
        import_batch=_batch(Channel.MERCHANT_BCA),
        channel=Channel.MERCHANT_BCA,
        book_date=day2,
        description_raw="QRIS ALFA 9 CELL",
        ref_normalized="QRIS",
        amount=Decimal("8100000"),
        external_ref="004767951",
        row_hash="qb_day2",
        match_status=MatchStatus.MATCHED,
    )
    m2 = Match.objects.create(
        book_date=day2,
        channel=Channel.MERCHANT_BCA,
        bank_mutation=None,
        otomax_entry=o2a,
        match_type=MatchType.AGGREGATE,
        amount_bank=Decimal("8100000"),
        amount_otomax=Decimal("8000000"),
        note="hari 2",
    )

    output = _run_cleanup("--apply")
    # m2 (hari 2) JUGA di luar toleransi (selisih 100rb) -> ikut dibersihkan di run yang
    # sama. Yang penting dibuktikan bukan "m2 tidak tersentuh", tapi grupnya tidak tercampur.
    assert output.count("grup direkonstruksi ulang") == 2

    m1.refresh_from_db()
    o1.refresh_from_db()
    b1.refresh_from_db()
    assert m1.voided_at is not None
    assert set(m1.otomax_entries.values_list("id", flat=True)) == {o1.id}
    assert o1.match_status == MatchStatus.PENDING_SETTLE
    assert b1.match_status == MatchStatus.UNMATCHED

    # Grup hari 2 harus direkonstruksi PERSIS ke {o2a, o2b} -> tidak ikut kebawa o1,
    # dan sebaliknya grup hari 1 di atas tidak ikut kebawa o2a/o2b.
    m2.refresh_from_db()
    o2a.refresh_from_db()
    o2b.refresh_from_db()
    assert m2.voided_at is not None
    assert set(m2.otomax_entries.values_list("id", flat=True)) == {o2a.id, o2b.id}
    assert o2a.match_status == MatchStatus.PENDING_SETTLE
    assert o2b.match_status == MatchStatus.PENDING_SETTLE
