import hashlib
from datetime import date, datetime
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.catalog.models import MerchantMap, Reseller
from apps.core.enums import Channel, MatchStatus, OtomaxCategory
from apps.core.normalize import classify_otomax, extract_tokens, norm_ref, ref_core, strip_otomax_prefix
from apps.ingest.models import BankMutation, ImportBatch, OtomaxEntry
from apps.recon.carry import carry_forward
from apps.recon.close import close_day, compute_totals
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


def _otomax_row(desc, amount, reseller_name, book_date=BD, entry_datetime=None, match_status=MatchStatus.UNMATCHED):
    """Bangun OtomaxEntry persis seperti pipeline import (classify + strip prefix)."""
    category, hint = classify_otomax(desc)
    embedded = strip_otomax_prefix(desc)
    return OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=book_date,
        entry_datetime=entry_datetime,
        reseller_name_raw=reseller_name,
        amount=Decimal(amount),
        description_raw=desc,
        category=category,
        channel_hint=hint or "",
        ref_normalized=norm_ref(embedded),
        ref_core=ref_core(embedded),
        extracted_tokens=extract_tokens(embedded),
        match_status=match_status,
        row_hash=_h("o", desc, amount, reseller_name, book_date, match_status),
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
def test_leftover_otomax_channel_attribution():
    """Otomax leftover dengan channel_hint BCA/Mandiri harus mencatat Discrepancy sesuai channel-nya."""
    o_bca = _otomax("TARTUN TF BCA 12345678", "250000", channel=Channel.BCA)
    o_man = _otomax("TARTUN TF MANDIRI 87654321", "350000", channel=Channel.MANDIRI)
    run_match(BD)
    d_bca = Discrepancy.objects.get(otomax_entry=o_bca)
    d_man = Discrepancy.objects.get(otomax_entry=o_man)
    assert d_bca.channel == Channel.BCA
    assert d_man.channel == Channel.MANDIRI


@pytest.mark.django_db
def test_qris_amount_diff_beyond_tolerance_stays_unmatched():
    """Default MATCH_AMOUNT_TOLERANCE=0: nama outlet cocok tapi nominal beda besar ->
    JANGAN auto-match. Biarkan kedua sisi muncul terpisah untuk ditinjau manual."""
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
    o = _otomax("TARTUN QR BULK TGL 05-SEP-2026", "10700000", channel=Channel.MERCHANT_BCA, reseller=r)
    stats = run_match(BD)
    assert stats.matched == 0
    assert Discrepancy.objects.filter(kind="AMOUNT_DIFF").count() == 0
    assert set(Discrepancy.objects.values_list("kind", flat=True)) == {"BANK_ONLY", "OTOMAX_ONLY"}
    o.refresh_from_db()
    assert o.match_status == MatchStatus.PENDING_SETTLE


@pytest.mark.django_db
def test_qris_amount_diff_within_tolerance_auto_matches(settings):
    """Kalau MATCH_AMOUNT_TOLERANCE dinaikkan, selisih kecil di dalam ambang tetap
    boleh auto-match + ditandai AMOUNT_DIFF."""
    settings.MATCH_AMOUNT_TOLERANCE = 100000
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
def test_rerunning_qris_match_does_not_duplicate_discrepancy_for_same_bank_row():
    """Reproduksi bug: klik 'Jalankan Matching Engine' berkali-kali untuk book_date yang
    sama, padahal mutasi QRIS-nya tetap tidak ketemu pasangan -> Pass 4 dulu bikin
    Discrepancy BANK_ONLY baru SETIAP kali dijalankan (tanpa cek sudah ada atau belum),
    sehingga satu bank_mutation bisa punya 2+ Discrepancy OPEN. carry_forward di hari
    berikutnya lalu coba pasangkan keduanya -> Match kedua bentrok dengan unique
    constraint uniq_active_bank_match -> 500."""
    BankMutation.objects.create(
        import_batch=_batch(Channel.MERCHANT_BCA),
        channel=Channel.MERCHANT_BCA,
        book_date=BD,
        description_raw="QRIS TIDAK ADA PASANGAN SAMA SEKALI",
        ref_normalized="QRIS",
        amount=Decimal("777000"),
        external_ref="000000000",
        row_hash="qris-no-pair",
    )
    run_match(BD)
    run_match(BD)  # klik "Jalankan Matching Engine" lagi
    assert Discrepancy.objects.filter(kind="BANK_ONLY").count() == 1


@pytest.mark.django_db
def test_rerunning_qris_match_does_not_duplicate_discrepancy_for_same_otomax_row():
    r = Reseller.objects.create(code="NOPAIR", name="Tanpa Pasangan")
    _otomax(
        "TARTUN QR BULK TGL 05-SEP-2026",
        "555000",
        channel=Channel.MERCHANT_BCA,
        reseller=r,
    )
    run_match(BD)
    run_match(BD)
    assert Discrepancy.objects.filter(kind="OTOMAX_ONLY").count() == 1


@pytest.mark.django_db
def test_carry_forward_skips_duplicate_discrepancy_instead_of_crashing():
    """Kalau data lama SUDAH kadung punya 2 Discrepancy OPEN yang menunjuk bank_mutation
    yang sama (dari bug di atas, sebelum diperbaiki), carry_forward tidak boleh crash
    IntegrityError -- cukup pasangkan salah satu, biarkan duplikatnya tetap OPEN untuk
    ditinjau/dihapusbukukan manual."""
    bank = _bank("DANA20260905034895588601ASEPKURNIAWA", "1600000")
    disc1 = Discrepancy.objects.create(
        code="SLS-DUP-001",
        origin_book_date=BD,
        channel=Channel.BRI,
        kind="BANK_ONLY",
        bank_mutation=bank,
        amount=bank.amount,
    )
    disc2 = Discrepancy.objects.create(
        code="SLS-DUP-002",
        origin_book_date=BD,
        channel=Channel.BRI,
        kind="BANK_ONLY",
        bank_mutation=bank,
        amount=bank.amount,
    )

    bd6 = date(2026, 9, 6)
    o1 = _otomax("TARTUN TF BRI DANA20260905034895588601ASEPKURNIAWA", "1600000", book_date=bd6)
    # Kandidat KEDUA dengan ref & nominal identik ke o1, supaya disc2 tetap punya kandidat
    # untuk ditemukan meski disc1 sudah lebih dulu memakai o1 -- ini yang benar-benar
    # memicu bentrok constraint kalau _find_otomax_for tidak dijaga.
    o2 = OtomaxEntry.objects.create(
        import_batch=o1.import_batch,
        book_date=bd6,
        reseller_name_raw="X",
        amount=Decimal("1600000"),
        description_raw=o1.description_raw,
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.BRI,
        ref_normalized=o1.ref_normalized,
        ref_core=o1.ref_core,
        row_hash=_h("o2", "dup-candidate"),
    )

    resolved = carry_forward(bd6)

    assert resolved == 1
    o1.refresh_from_db()
    o2.refresh_from_db()
    matched_otomax = {o1.match_status, o2.match_status}
    assert MatchStatus.MATCHED in matched_otomax
    disc1.refresh_from_db()
    disc2.refresh_from_db()
    statuses = {disc1.status, disc2.status}
    assert statuses == {"OPEN", "RESOLVED"}
    assert Match.objects.filter(bank_mutation=bank, voided_at__isnull=True).count() == 1


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
    """Kode PLC di sini SENGAJA bukan salah satu dari 41 kode bawaan (migrasi
    catalog/0003_seed_reseller_codes) supaya pass 'Kode Reseller' (2b) tidak ikut
    campur -- test ini murni menguji fallback nominal pass 4 untuk kode yang BELUM
    terdaftar di menu Kode Reseller. Skenario kode yang SUDAH terdaftar diuji di
    test_tartun_plc_matches_via_reseller_code_before_auto_deposit_fallback."""
    # BCA Bank mutation with Tartun PLC
    b = BankMutation.objects.create(
        import_batch=_batch(Channel.BCA),
        channel=Channel.BCA,
        book_date=BD,
        description_raw="TRSF E-BANKING CR 0309/FTSCY/WS95271 3190000.00  Tartun PLC9999 DEDE SUMPENA B.",
        ref_normalized="TRSF E BANKING CR TARTUN PLC9999 DEDE SUMPENA B",
        ref_core="PLC9999",
        extracted_tokens=["PLC9999"],
        amount=Decimal("3190000"),
        row_hash="bca_plc9999",
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


@pytest.mark.django_db
def test_tartun_plc_matches_via_reseller_code_before_auto_deposit_fallback():
    """Reproduksi kasus nyata yang memicu fitur Kode Reseller: mutasi bank Mandiri
    berisi token 'PLC131' (terdaftar di menu Kode Reseller -> 'PLC CIPADUNG 2'
    lewat migrasi seed), dan ada BEBERAPA entri Otomax lain dengan nominal SAMA
    PERSIS dari reseller yang BERBEDA -- tanpa fitur ini, operator harus pilih manual
    di antara kandidat yang membingungkan. Dengan kode terdaftar, harus langsung
    auto-match ke reseller yang benar tanpa menyentuh Pass 4 (fallback generik)."""
    b = BankMutation.objects.create(
        import_batch=_batch(Channel.MANDIRI),
        channel=Channel.MANDIRI,
        book_date=BD,
        description_raw="MCM InhouseTrf DARI TAMIM MUSLIH Tartun PLC131",
        ref_normalized="MCM INHOUSETRF DARI TAMIM MUSLIH TARTUN PLC131",
        ref_core="PLC131",
        extracted_tokens=["PLC131"],
        amount=Decimal("1500000"),
        row_hash="mandiri_plc131",
    )
    correct = OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=BD,
        reseller_name_raw="PLC CIPADUNG 2",
        amount=Decimal("1500000"),
        description_raw="TARTUN TF MANDIRI MCM InhouseTrf DARI TAMIM MUSLIH",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.MANDIRI,
        ref_normalized="TARTUN TF MANDIRI MCM INHOUSETRF DARI TAMIM MUSLIH",
        row_hash="oto_cipadung2",
    )
    # Kandidat pengecoh: reseller LAIN, kebetulan nominal sama persis.
    decoy = OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=BD,
        reseller_name_raw="PLC BK7 NAGROG2",
        amount=Decimal("1500000"),
        description_raw="Auto Deposit BCA 4373433015",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.MANDIRI,
        ref_normalized="AUTO DEPOSIT BCA 4373433015",
        row_hash="oto_decoy",
    )

    run_match(BD)

    b.refresh_from_db()
    correct.refresh_from_db()
    decoy.refresh_from_db()
    assert b.match_status == MatchStatus.MATCHED
    assert correct.match_status == MatchStatus.MATCHED
    # Pengecoh tidak ikut kepasangkan -- jadi leftover PENDING_SETTLE seperti biasa,
    # bukan MATCHED ke bank mutation yang salah.
    assert decoy.match_status == MatchStatus.PENDING_SETTLE

    m = Match.objects.filter(bank_mutation=b).first()
    assert m is not None
    assert m.otomax_entry_id == correct.id
    assert "Kode Reseller" in m.note


@pytest.mark.django_db
def test_match_bri_combined_descriptions():
    # Case 1: Otomax matches the TRREMK part (inside brackets) - e.g. BI-Fast
    b1 = _bank(
        desc="Transfer BI-Fast - Ujang Wawan [BFST215401000596563UJANG WAWAN :BMRIIDJA]",
        amount="4090000",
        channel=Channel.BRI,
    )
    o1 = _otomax(
        desc="TARTUN TF BRI BFST215401000596563UJANG WAWAN :BMRIIDJA",
        amount="4090000",
        channel=Channel.BRI,
    )

    # Case 2: Otomax matches the REMARK_CUSTOM part (before brackets) - e.g. BRImo
    b2 = _bank(
        desc="Transfer Dari Budiharno via BRImo [NBMB BUDIHARNO TO SYAIFUL]",
        amount="1005000",
        channel=Channel.BRI,
    )
    o2 = _otomax(
        desc="TARTUN TF BRI Transfer Dari Budiharno via BRImo",
        amount="1005000",
        channel=Channel.BRI,
    )

    stats = run_match(BD)
    assert stats.matched >= 2

    b1.refresh_from_db()
    o1.refresh_from_db()
    assert b1.match_status == MatchStatus.MATCHED
    assert o1.match_status == MatchStatus.MATCHED

    b2.refresh_from_db()
    o2.refresh_from_db()
    assert b2.match_status == MatchStatus.MATCHED
    assert o2.match_status == MatchStatus.MATCHED


@pytest.mark.django_db
def test_reversal_netting_cross_day_matches_correction_not_original():
    """Revisian beda hari: asli (12/9) dibatalkan REV (13/9), lalu direvisi (13/9) -> yg dicocokkan hanya revisian."""
    day1 = date(2026, 9, 12)
    day2 = date(2026, 9, 13)
    ref_desc = "TARTUN EDC BRI 6013013636952876#192240520005#EDC#TRFLA TGL 12/SEP/2026"
    rev_desc = "REV TARTUN EDC BRI 6013013636952876#192240520005#EDC#TRFLA TGL 12/SEP/2026"

    original = _otomax_row(
        ref_desc, "1150000", "PLC ALFA3 SINJAY1", book_date=day1,
        entry_datetime=timezone.make_aware(datetime(2026, 9, 12, 21, 44, 50)),
    )
    rev = _otomax_row(
        rev_desc, "-1150000", "PLC ALFA3 SINJAY1", book_date=day2,
        entry_datetime=timezone.make_aware(datetime(2026, 9, 13, 9, 31, 45)),
    )
    correction = _otomax_row(
        ref_desc, "1150000", "PLC SA", book_date=day2,
        entry_datetime=timezone.make_aware(datetime(2026, 9, 13, 9, 32, 9)),
    )
    b = _bank("6013013636952876#192240520005#EDC#TRFLA", "1150000", channel=Channel.BRI, book_date=day2)

    stats = run_match(day2)
    assert stats.netted == 1
    assert stats.matched == 1

    original.refresh_from_db()
    rev.refresh_from_db()
    correction.refresh_from_db()
    b.refresh_from_db()

    assert original.match_status == MatchStatus.IGNORED
    assert rev.match_status == MatchStatus.IGNORED
    assert original.net_pair_id == rev.id
    assert rev.net_pair_id == original.id
    assert correction.match_status == MatchStatus.MATCHED
    assert b.match_status == MatchStatus.MATCHED

    m = Match.objects.get(bank_mutation=b)
    assert m.otomax_entry_id == correction.id

    # Total OTOMAX hari asal (12/9) tidak lagi kelebihan hitung akibat entri yang dibatalkan
    assert compute_totals(day1)["otomax"] == Decimal("0.00")
    assert compute_totals(day2)["otomax"] == Decimal("1150000.00")


@pytest.mark.django_db
def test_reversal_netting_matches_via_ref_core_when_original_lacks_tgl_suffix():
    """Data nyata: entri asli tidak punya akhiran "TGL ...", REV & revisian punya —
    ref_normalized jadi beda persis, jadi netting harus jatuh ke ref_core."""
    day1 = date(2026, 9, 12)
    day2 = date(2026, 9, 13)
    original_desc = "TARTUN EDC BRI 6013013636952876#192240520005#EDC#TRFLA"
    rev_desc = "REV TARTUN EDC BRI 6013013636952876#192240520005#EDC#TRFLA TGL 12/SEP/2026"
    correction_desc = "TARTUN EDC BRI 6013013636952876#192240520005#EDC#TRFLA TGL 12/SEP/2026"

    original = _otomax_row(
        original_desc, "1150000", "PLC ALFA3 SINJAY1", book_date=day1,
        entry_datetime=timezone.make_aware(datetime(2026, 9, 12, 21, 44, 50)),
    )
    rev = _otomax_row(
        rev_desc, "-1150000", "PLC ALFA3 SINJAY1", book_date=day2,
        entry_datetime=timezone.make_aware(datetime(2026, 9, 13, 9, 31, 45)),
    )
    correction = _otomax_row(
        correction_desc, "1150000", "PLC SA", book_date=day2,
        entry_datetime=timezone.make_aware(datetime(2026, 9, 13, 9, 32, 9)),
    )
    assert original.ref_normalized != rev.ref_normalized  # persis skenario yang gagal di produksi
    assert original.ref_core == rev.ref_core == correction.ref_core

    b = _bank("6013013636952876#192240520005#EDC#TRFLA", "1150000", channel=Channel.BRI, book_date=day1)

    stats = run_match(day1)
    assert stats.netted == 1
    assert stats.matched == 1

    original.refresh_from_db()
    rev.refresh_from_db()
    correction.refresh_from_db()
    b.refresh_from_db()

    assert original.match_status == MatchStatus.IGNORED
    assert rev.match_status == MatchStatus.IGNORED
    assert correction.match_status == MatchStatus.MATCHED
    assert b.match_status == MatchStatus.MATCHED

    m = Match.objects.get(bank_mutation=b)
    assert m.otomax_entry_id == correction.id


@pytest.mark.django_db
def test_reversal_netting_and_matching_also_pick_up_pending_settle_entries():
    """unpair_match() mengembalikan entri Otomax ke PENDING_SETTLE (bukan UNMATCHED) —
    netting & pencocokan ulang harus tetap jalan tanpa perlu direset manual dulu."""
    day1 = date(2026, 9, 12)
    day2 = date(2026, 9, 13)
    original_desc = "TARTUN EDC BRI 6013013636952876#192240520005#EDC#TRFLA"
    rev_desc = "REV TARTUN EDC BRI 6013013636952876#192240520005#EDC#TRFLA TGL 12/SEP/2026"
    correction_desc = "TARTUN EDC BRI 6013013636952876#192240520005#EDC#TRFLA TGL 12/SEP/2026"

    original = _otomax_row(
        original_desc, "1150000", "PLC ALFA3 SINJAY1", book_date=day1,
        entry_datetime=timezone.make_aware(datetime(2026, 9, 12, 21, 44, 50)),
        match_status=MatchStatus.PENDING_SETTLE,
    )
    rev = _otomax_row(
        rev_desc, "-1150000", "PLC ALFA3 SINJAY1", book_date=day2,
        entry_datetime=timezone.make_aware(datetime(2026, 9, 13, 9, 31, 45)),
        match_status=MatchStatus.PENDING_SETTLE,
    )
    correction = _otomax_row(
        correction_desc, "1150000", "PLC SA", book_date=day2,
        entry_datetime=timezone.make_aware(datetime(2026, 9, 13, 9, 32, 9)),
        match_status=MatchStatus.PENDING_SETTLE,
    )
    b = _bank("6013013636952876#192240520005#EDC#TRFLA", "1150000", channel=Channel.BRI, book_date=day1)

    stats = run_match(day1)
    assert stats.netted == 1
    assert stats.matched == 1

    original.refresh_from_db()
    rev.refresh_from_db()
    correction.refresh_from_db()
    b.refresh_from_db()

    assert original.match_status == MatchStatus.IGNORED
    assert rev.match_status == MatchStatus.IGNORED
    assert correction.match_status == MatchStatus.MATCHED
    assert b.match_status == MatchStatus.MATCHED


@pytest.mark.django_db
def test_reversal_netting_same_day_matches_correction_not_original():
    """Revisian sehari: asli, REV, dan revisian semuanya di hari yang sama."""
    bd = date(2026, 9, 16)
    ref_desc = "TARTUN TF BRI BFST215401000596563ANGGIAT HISA:SSPIIDJA"
    rev_desc = "REV TARTUN TF BRI BFST215401000596563ANGGIAT HISA:SSPIIDJA"

    original = _otomax_row(
        ref_desc, "1105000", "PLC BAKSAR1", book_date=bd,
        entry_datetime=timezone.make_aware(datetime(2026, 9, 16, 20, 8, 39)),
    )
    rev = _otomax_row(
        rev_desc, "-1105000", "PLC BAKSAR1", book_date=bd,
        entry_datetime=timezone.make_aware(datetime(2026, 9, 16, 20, 8, 56)),
    )
    correction = _otomax_row(
        ref_desc, "1105000", "PLC JH2", book_date=bd,
        entry_datetime=timezone.make_aware(datetime(2026, 9, 16, 20, 9, 3)),
    )
    b = _bank("BFST215401000596563ANGGIAT HISA:SSPIIDJA", "1105000", channel=Channel.BRI, book_date=bd)

    stats = run_match(bd)
    assert stats.netted == 1
    assert stats.matched == 1

    original.refresh_from_db()
    rev.refresh_from_db()
    correction.refresh_from_db()
    b.refresh_from_db()

    assert original.match_status == MatchStatus.IGNORED
    assert rev.match_status == MatchStatus.IGNORED
    assert correction.match_status == MatchStatus.MATCHED
    assert b.match_status == MatchStatus.MATCHED

    m = Match.objects.get(bank_mutation=b)
    assert m.otomax_entry_id == correction.id
    assert compute_totals(bd)["otomax"] == Decimal("1105000.00")


@pytest.mark.django_db
def test_reversal_netting_handles_multi_step_correction_chain():
    """Data nyata: 3 kali percobaan koreksi (asli, REV +, REV -, REV REV -) sebelum entri
    final ke reseller yang benar. Dua REV yang saling berlawanan harus saling ternetralkan,
    bukan cuma REV vs TOPUP_TARTUN, supaya tidak ada sisa yang nyangkut di Pending Settle."""
    bd = date(2026, 9, 13)
    shared = "BFST215401000596563ASEP MUHAMAD:SSPIIDJA"
    original_desc = f"TARTUN TF BRI {shared}"
    rev_desc = f"REV TARTUN TF BRI {shared}"
    rev_rev_desc = f"REV REV TARTUN TF BRI {shared}"
    final_desc = f"TARTUN TF BRI {shared}"

    original = _otomax_row(
        original_desc, "550000", "PLC CL 3", book_date=bd,
        entry_datetime=timezone.make_aware(datetime(2026, 9, 13, 11, 16, 4)),
    )
    rev_plus = _otomax_row(
        rev_desc, "550000", "PLC CL 3", book_date=bd,
        entry_datetime=timezone.make_aware(datetime(2026, 9, 13, 14, 25, 16)),
    )
    rev_minus = _otomax_row(
        rev_desc, "-550000", "PLC CL 3", book_date=bd,
        entry_datetime=timezone.make_aware(datetime(2026, 9, 13, 14, 25, 32)),
    )
    rev_rev_minus = _otomax_row(
        rev_rev_desc, "-550000", "PLC CL 3", book_date=bd,
        entry_datetime=timezone.make_aware(datetime(2026, 9, 13, 14, 25, 40)),
    )
    final = _otomax_row(
        final_desc, "550000", "PLC PC4 OJEG", book_date=bd,
        entry_datetime=timezone.make_aware(datetime(2026, 9, 13, 14, 25, 59)),
    )
    b = _bank(shared, "550000", channel=Channel.BRI, book_date=bd)

    stats = run_match(bd)
    assert stats.netted == 2
    assert stats.matched == 1

    for row in (original, rev_plus, rev_minus, rev_rev_minus):
        row.refresh_from_db()
        assert row.match_status == MatchStatus.IGNORED, row.description_raw

    final.refresh_from_db()
    b.refresh_from_db()
    assert final.match_status == MatchStatus.MATCHED
    assert b.match_status == MatchStatus.MATCHED

    m = Match.objects.get(bank_mutation=b)
    assert m.otomax_entry_id == final.id
    assert compute_totals(bd)["otomax"] == Decimal("550000.00")


@pytest.mark.django_db
def test_unpair_aggregate_match_frees_all_group_members():
    """unpair_match() pada match AGGREGATE (QRIS multi-baris) harus melepas SEMUA
    baris Otomax dalam grup, bukan cuma otomax_entry utama yang tersimpan di Match."""
    from apps.recon.resolve import unpair_match

    r = Reseller.objects.create(code="ALFA9", name="Alfa 9")
    b = BankMutation.objects.create(
        import_batch=_batch(Channel.MERCHANT_BCA),
        channel=Channel.MERCHANT_BCA,
        book_date=BD,
        description_raw="QRIS ALFA 9 CELL",
        ref_normalized="QRIS",
        amount=Decimal("1500000"),
        external_ref="009999999",
        row_hash="qb_agg1",
    )
    o1 = OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=BD,
        reseller_name_raw="PLC ALFA9",
        reseller=r,
        amount=Decimal("1000000"),
        description_raw="TARTUN QR BULK TGL 05-SEP-2026",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.MERCHANT_BCA,
        ref_normalized="TARTUN",
        row_hash="oto_agg1",
    )
    o2 = OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=BD,
        reseller_name_raw="PLC ALFA9",
        reseller=r,
        amount=Decimal("500000"),
        description_raw="TARTUN QR BULK TGL 05-SEP-2026",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.MERCHANT_BCA,
        ref_normalized="TARTUN",
        row_hash="oto_agg2",
    )
    stats = run_match(BD)
    assert stats.matched == 1

    m = Match.objects.get(bank_mutation=b, voided_at__isnull=True)
    assert m.match_type == "AGGREGATE"
    assert set(m.otomax_entries.values_list("id", flat=True)) == {o1.id, o2.id}

    unpair_match(m)

    b.refresh_from_db()
    o1.refresh_from_db()
    o2.refresh_from_db()
    assert b.match_status == MatchStatus.UNMATCHED
    assert o1.match_status == MatchStatus.PENDING_SETTLE
    assert o2.match_status == MatchStatus.PENDING_SETTLE
    m.refresh_from_db()
    assert m.voided_at is not None


@pytest.mark.django_db
def test_qris_matches_otomax_entry_left_pending_settle_by_earlier_cleanup():
    """Entri Otomax QRIS yang statusnya PENDING_SETTLE (mis. sisa dari unpair_match
    atau leftover harian) harus tetap terlihat oleh _match_qris, bukan cuma UNMATCHED —
    sama seperti sudah dibenarkan untuk BRI/BCA/Mandiri & netting REV."""
    r = Reseller.objects.create(code="ASBER2", name="Asber 2")
    b = BankMutation.objects.create(
        import_batch=_batch(Channel.MERCHANT_BCA),
        channel=Channel.MERCHANT_BCA,
        book_date=BD,
        description_raw="QRIS ASBER 2 CELL 004767942",
        ref_normalized="QRIS",
        outlet_name="ASBER 2 CELL",
        amount=Decimal("3000000"),
        external_ref="004767942",
        row_hash="qb_asber2",
    )
    o = OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=BD,
        reseller_name_raw="PLC ASBER2",
        reseller=r,
        amount=Decimal("3000000"),
        description_raw="TARTUN QR BULK TGL 05-SEP-2026",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.MERCHANT_BCA,
        ref_normalized="TARTUN",
        row_hash="oto_asber2",
        match_status=MatchStatus.PENDING_SETTLE,
    )
    stats = run_match(BD)
    assert stats.matched == 1

    b.refresh_from_db()
    o.refresh_from_db()
    m = Match.objects.get(bank_mutation=b, voided_at__isnull=True)
    assert m.otomax_entry_id == o.id


@pytest.mark.django_db
def test_recon_rematch_command():
    from io import StringIO
    from django.core.management import call_command

    d = date(2026, 9, 7)
    b = BankMutation.objects.create(
        import_batch=_batch(Channel.BRI),
        channel=Channel.BRI,
        book_date=d,
        description_raw="TRSF DANA TOK99",
        ref_normalized="TRSF DANA TOK99",
        ref_core="TOK99",
        extracted_tokens=["TOK99"],
        amount=Decimal("150000"),
        row_hash="rematch_b1",
    )
    o = OtomaxEntry.objects.create(
        import_batch=_batch(Channel.OTOMAX),
        book_date=d,
        reseller_name_raw="PLC TOK99",
        amount=Decimal("150000"),
        description_raw="Tiket TOK99",
        category=OtomaxCategory.TOPUP_TARTUN,
        channel_hint=Channel.BRI,
        ref_normalized="TOK99",
        ref_core="TOK99",
        extracted_tokens=["TOK99"],
        row_hash="rematch_o1",
    )

    out = StringIO()
    call_command("recon_rematch", date=str(d), stdout=out)
    output = out.getvalue()
    assert "1 match baru terbentuk" in output

    b.refresh_from_db()
    o.refresh_from_db()
    assert b.match_status == MatchStatus.MATCHED
    assert o.match_status == MatchStatus.MATCHED
    assert Match.objects.filter(book_date=d, bank_mutation=b, otomax_entry=o).exists()


