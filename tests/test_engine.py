import hashlib
from datetime import date
from decimal import Decimal

import pytest

from apps.catalog.models import MerchantMap, Reseller
from apps.core.enums import Channel, MatchStatus, OtomaxCategory
from apps.core.normalize import norm_ref, ref_core
from apps.ingest.models import BankMutation, ImportBatch, OtomaxEntry
from apps.recon.carry import carry_forward
from apps.recon.close import close_day
from apps.recon.engine import run_match
from apps.recon.models import Adjustment, Discrepancy, Match, ReconDay

BD = date(2026, 9, 5)


def _h(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _batch(channel):
    return ImportBatch.objects.create(channel=channel, book_date=BD, source_filename="t", file_hash="h")


def _bank(desc, amount, channel=Channel.BRI, book_date=BD):
    from apps.core.normalize import extract_tokens

    return BankMutation.objects.create(
        import_batch=_batch(channel),
        channel=channel,
        book_date=book_date,
        description_raw=desc,
        ref_normalized=norm_ref(desc),
        ref_core=ref_core(desc),
        extracted_tokens=extract_tokens(desc),
        amount=Decimal(amount),
        row_hash=_h("b", desc, amount, book_date),
    )


def _otomax(desc, amount, channel=Channel.BRI, reseller=None, book_date=BD):
    from apps.core.normalize import extract_tokens, strip_otomax_prefix

    core = strip_otomax_prefix(desc)
    return OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=book_date,
        reseller_name_raw="X",
        reseller=reseller,
        amount=Decimal(amount),
        description_raw=desc,
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=channel,
        ref_normalized=norm_ref(core),
        ref_core=ref_core(core),
        extracted_tokens=extract_tokens(core),
        row_hash=_h("o", desc, amount, book_date),
    )


@pytest.mark.django_db
def test_exact_match():
    _bank("ATMLTRPRM 01884 000001039 21540100059656", "550000")
    _otomax("TARTUN EDC BRI ATMLTRPRM 01884 000001039 21540100059656", "550000")
    stats = run_match(BD)
    assert stats.matched == 1
    assert BankMutation.objects.get().match_status == MatchStatus.MATCHED


@pytest.mark.django_db
def test_core_match_leading_digit_dropped():
    _bank("5221845082525239#185996340008#EDC#TRFLA", "1000000")
    _otomax("TARTUN EDC BRI 221845082525239#185996340008#EDC#TRFLA", "1000000")
    stats = run_match(BD)
    assert stats.matched == 1


@pytest.mark.django_db
def test_bank_only_and_otomax_only_become_discrepancies():
    _bank("DANA20260905034895588601ASEPKURNIAWA", "1600000")
    _otomax("TARTUN TF BRI DANA20260905099999999999SOMEONE", "999000")
    run_match(BD)
    kinds = set(Discrepancy.objects.values_list("kind", flat=True))
    assert kinds == {"BANK_ONLY", "OTOMAX_ONLY"}


@pytest.mark.django_db
def test_qris_aggregate_and_amount_diff():
    r = Reseller.objects.create(code="ALFA2", name="Alfa 2")
    MerchantMap.objects.create(merchant_id="004767951", reseller=r)
    BankMutation.objects.create(
        import_batch=_batch(Channel.MERCHANT_BCA),
        channel=Channel.MERCHANT_BCA,
        book_date=BD,
        description_raw="QRIS ALFA 2 CELL",
        ref_normalized="QRIS",
        amount=Decimal("10760000"),
        external_ref="004767951",
        row_hash="qb1",
    )
    _otomax("TARTUN QR BULK TGL 05-SEP-2026", "10700000", channel=Channel.MERCHANT_BCA, reseller=r)
    stats = run_match(BD)
    assert stats.matched == 1
    assert Discrepancy.objects.filter(kind="AMOUNT_DIFF").count() == 1


@pytest.mark.django_db
def test_carry_forward_resolves_and_posts_adjustment():
    # Hari 5: uang masuk di bank, tidak ada di OTOMAX -> BANK_ONLY
    _bank("DANA20260905034895588601ASEPKURNIAWA", "1600000")
    run_match(BD)
    close_day(BD, force=True)
    disc = Discrepancy.objects.get()
    assert disc.status == "OPEN"

    # Hari 6: OTOMAX-nya baru masuk
    bd6 = date(2026, 9, 6)
    _otomax("TARTUN TF BRI DANA20260905034895588601ASEPKURNIAWA", "1600000", book_date=bd6)
    resolved = carry_forward(bd6)
    assert resolved == 1

    disc.refresh_from_db()
    assert disc.status == "RESOLVED"
    assert disc.resolved_book_date == bd6

    adj = Adjustment.objects.get()
    assert adj.book_date == BD  # penyesuaian bertanggal hari asal
    day = ReconDay.objects.get(book_date=BD)
    assert day.selisih_current == day.selisih_initial - adj.amount


@pytest.mark.django_db
def test_reopen_day_when_no_downstream():
    from apps.recon.close import reopen_day

    _bank("ATMLTRPRM 01884 000001039 21540100059656", "550000")
    close_day(BD, force=True)  # keburu tutup sebelum match
    assert ReconDay.objects.get(book_date=BD).locked is True

    reopen_day(BD)
    day = ReconDay.objects.get(book_date=BD)
    assert day.locked is False and day.status == "IN_REVIEW"

    # sekarang bisa match + tutup lagi
    _otomax("TARTUN EDC BRI ATMLTRPRM 01884 000001039 21540100059656", "550000")
    assert run_match(BD).matched == 1
    close_day(BD, force=True)
    assert ReconDay.objects.get(book_date=BD).matched_count == 1


@pytest.mark.django_db
def test_reopen_blocked_after_adjustment_posted():
    from apps.recon.close import DayHasDownstream, reopen_day

    _bank("DANA20260905034895588601ASEPKURNIAWA", "1600000")
    run_match(BD)
    close_day(BD, force=True)
    bd6 = date(2026, 9, 6)
    _otomax("TARTUN TF BRI DANA20260905034895588601ASEPKURNIAWA", "1600000", book_date=bd6)
    carry_forward(bd6)  # posting adjustment bertanggal BD
    with pytest.raises(DayHasDownstream):
        reopen_day(BD)


@pytest.mark.django_db
def test_closed_day_blocks_import():
    from apps.ingest.services import ImportBlocked, import_file

    ReconDay.objects.create(book_date=BD, locked=True)
    with pytest.raises(ImportBlocked):
        import_file(channel=Channel.BRI, text="x", book_date=BD, filename="f")


@pytest.mark.django_db
def test_adjustment_is_append_only():
    from django.core.exceptions import ValidationError

    _bank("DANA20260905034895588601ASEPKURNIAWA", "1600000")
    run_match(BD)
    close_day(BD, force=True)
    bd6 = date(2026, 9, 6)
    _otomax("TARTUN TF BRI DANA20260905034895588601ASEPKURNIAWA", "1600000", book_date=bd6)
    carry_forward(bd6)
    adj = Adjustment.objects.get()
    adj.amount = Decimal("1")
    with pytest.raises(ValidationError):
        adj.save()


@pytest.mark.django_db
def test_tag_manual_mutation():
    from apps.recon.resolve import tag_manual_mutation

    bm = _bank("BIAYA ADM BULANAN", "-2500")
    run_match(BD)
    assert Discrepancy.objects.filter(bank_mutation=bm).count() == 1

    tag_manual_mutation(bm, tag="admin", note="Potongan bank rutin")
    bm.refresh_from_db()
    assert bm.match_status == MatchStatus.MANUAL
    assert bm.tag_manual == "admin"
    assert bm.manual_note == "Potongan bank rutin"

    disc = Discrepancy.objects.get(bank_mutation=bm)
    assert disc.status == "RESOLVED"


@pytest.mark.django_db
def test_tartun_bulk_outlet_name_and_nominal_match():
    # Merchant BCA row with outlet name ALFA 1 CELL
    b = BankMutation.objects.create(
        import_batch=_batch(Channel.MERCHANT_BCA),
        channel=Channel.MERCHANT_BCA,
        book_date=BD,
        description_raw="QRIS ALFA 1 CELL 004767950",
        ref_normalized="QRIS",
        outlet_name="ALFA 1 CELL",
        amount=Decimal("4374000"),
        external_ref="004767950",
        row_hash="bca_alfa1",
    )
    # Otomax tartun bulk with reseller PLC ALFA1 PASIR IMPUN
    o = OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=BD,
        reseller_name_raw="PLC ALFA1 PASIR IMPUN",
        amount=Decimal("4374000"),
        description_raw="TARTUN QR BULK TGL 05-SEP-2026",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.MERCHANT_BCA,
        ref_normalized="TARTUN",
        row_hash="oto_alfa1",
    )
    stats = run_match(BD)
    assert stats.matched == 1
    assert stats.discrepancies == 0

    b.refresh_from_db()
    o.refresh_from_db()
    assert b.match_status == MatchStatus.MATCHED
    assert o.match_status == MatchStatus.MATCHED


@pytest.mark.django_db
def test_tartun_bulk_exact_nominal_fallback():
    # If outlet name does not match, match by unambiguous exact nominal
    b = BankMutation.objects.create(
        import_batch=_batch(Channel.MERCHANT_BCA),
        channel=Channel.MERCHANT_BCA,
        book_date=BD,
        description_raw="QRIS OUTLET UNKNOWN 00999999",
        ref_normalized="QRIS",
        outlet_name="UNKNOWN OUTLET",
        amount=Decimal("1234567"),
        external_ref="00999999",
        row_hash="bca_unk",
    )
    o = OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=BD,
        reseller_name_raw="PLC SOME RESELLER",
        amount=Decimal("1234567"),
        description_raw="TARTUN QR BULK TGL 05-SEP-2026",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.MERCHANT_BCA,
        ref_normalized="TARTUN",
        row_hash="oto_some",
    )
    stats = run_match(BD)
    assert stats.matched == 1
    b.refresh_from_db()
    o.refresh_from_db()
    assert b.match_status == MatchStatus.MATCHED
    assert o.match_status == MatchStatus.MATCHED


@pytest.mark.django_db
def test_tartun_plc_auto_deposit_nominal_match():
    # BCA Bank mutation with Tartun PLC
    b = BankMutation.objects.create(
        import_batch=_batch(Channel.BCA),
        channel=Channel.BCA,
        book_date=BD,
        description_raw="TRSF E-BANKING CR 0309/FTSCY/WS95271 3190000.00  Tartun PLC111 DEDE SUMPENA B.",
        ref_normalized="TRSF E BANKING CR TARTUN PLC111 DEDE SUMPENA B",
        ref_core="PLC111",
        extracted_tokens=["PLC111"],
        amount=Decimal("3190000"),
        row_hash="bca_plc111",
    )
    # Otomax entry with Auto Deposit BCA
    o = OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=BD,
        reseller_name_raw="PLC BUNISARI",
        amount=Decimal("3190000"),
        description_raw="Auto Deposit BCA 4373433015",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.BCA,
        ref_normalized="AUTO DEPOSIT BCA 4373433015",
        row_hash="oto_bunisari",
    )
    stats = run_match(BD)
    assert stats.matched == 1

    b.refresh_from_db()
    o.refresh_from_db()
    assert b.match_status == MatchStatus.MATCHED
    assert o.match_status == MatchStatus.MATCHED

    m = Match.objects.filter(bank_mutation=b).first()
    assert m is not None
    assert m.amount_bank == Decimal("3190000")
    assert m.amount_otomax == Decimal("3190000")
    assert "Auto Deposit" in m.note
