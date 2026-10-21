"""Usulan Pencocokan: pasangan dari mesin yang rawan jadi pencocokan "hantu" -- gabungan
beberapa tiket QRIS (kasus nyata 819.000 + 181.000 dipasangkan ke 1.000.000), cocok nominal
saja tanpa cek nama outlet, kemiripan teks (fuzzy), dan susulan lintas hari yang teksnya
cuma mirip -- dibuat sebagai usulan (needs_review) yang wajib disetujui/ditolak operator,
dan tutup buku ditolak selama masih ada."""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.catalog.models import MerchantMap, Reseller
from apps.core.enums import Channel, DiscrepancyKind, DiscrepancyStatus, MatchStatus
from apps.ingest.models import BankMutation
from apps.recon.carry import carry_forward
from apps.recon.close import DayNotReady, close_day
from apps.recon.engine import run_match
from apps.recon.models import Adjustment, Discrepancy, Match
from apps.recon.resolve import approve_match, unpair_match

from .test_bayar_telat_koreksi import AUG30, SAHIDIN_BANK, SEP3, _payment
from .test_engine import BD, _bank, _batch, _otomax


@pytest.fixture
def operator(db):
    return User.objects.create_superuser(username="op_usulan", password="password123")


@pytest.fixture
def auth_client(operator):
    client = Client()
    client.force_login(operator)
    return client


def _qris_bank(amount, outlet="QRIS CIKADUT 2 CELL", merchant_id="004769148"):
    return BankMutation.objects.create(
        import_batch=_batch(Channel.MERCHANT_BCA),
        channel=Channel.MERCHANT_BCA,
        book_date=BD,
        description_raw=outlet,
        ref_normalized="QRIS",
        outlet_name=outlet,
        amount=Decimal(amount),
        external_ref=merchant_id,
        row_hash=f"qb-{outlet}-{amount}",
    )


def _active(bank):
    return Match.objects.get(bank_mutation=bank, voided_at__isnull=True)


# --- Fuzzy ---------------------------------------------------------------------------


def _fuzzy_case():
    b = _bank("TRANSFER DARI SITI AMINAH VIA BRIMO", "750000")
    _otomax("TARTUN TF BRI TRANSFER DARI SITI AMINA VIA BRIMO", "750000")
    run_match(BD)
    return b


@pytest.mark.django_db
def test_fuzzy_match_is_a_proposal_that_blocks_day_close(operator):
    b = _fuzzy_case()
    m = _active(b)
    assert m.needs_review
    assert "fuzzy" in m.review_reason.lower()

    with pytest.raises(DayNotReady):
        close_day(BD)

    approve_match(m, user=operator)
    m.refresh_from_db()
    assert not m.needs_review
    assert m.reviewed_by == operator
    assert m.reviewed_at is not None
    close_day(BD)  # tidak ada lagi usulan yang menunggu


@pytest.mark.django_db
def test_rejected_proposal_is_not_proposed_again():
    b = _fuzzy_case()
    unpair_match(_active(b))  # operator menolak usulan

    run_match(BD)

    b.refresh_from_db()
    assert b.match_status == MatchStatus.UNMATCHED
    assert not Match.objects.filter(bank_mutation=b, voided_at__isnull=True).exists()


# --- QRIS ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_qris_single_ticket_identified_by_merchant_map_stays_final():
    r = Reseller.objects.create(code="CKD2", name="Cikadut 2")
    MerchantMap.objects.create(merchant_id="004769148", reseller=r)
    b = _qris_bank("1000000")
    _otomax("TARTUN QR BULK TGL 05-SEP-2026", "1000000", channel=Channel.MERCHANT_BCA, reseller=r)

    run_match(BD)

    assert not _active(b).needs_review


@pytest.mark.django_db
def test_qris_combining_two_tickets_is_a_proposal_and_learns_mapping_only_on_approval(operator):
    """Kasus nyata: 819.000 + 181.000 digabung otomatis ke settlement 1.000.000."""
    r = Reseller.objects.create(code="CKD2", name="Cikadut 2")
    b = _qris_bank("1000000", outlet="QRIS CIKADUT 2 CELL", merchant_id="004769148")
    _otomax("TARTUN QR BULK TGL 05-SEP-2026", "819000", channel=Channel.MERCHANT_BCA, reseller=r)
    _otomax("TARTUN QR BULK CIKADUT TGL 05-SEP-2026", "181000", channel=Channel.MERCHANT_BCA, reseller=r)

    run_match(BD)

    m = _active(b)
    assert m.needs_review
    assert "Gabungan 2 tiket" in m.review_reason
    assert m.otomax_entries.count() == 2
    assert not MerchantMap.objects.filter(merchant_id="004769148").exists()

    approve_match(m, user=operator)
    assert MerchantMap.objects.filter(merchant_id="004769148", reseller=r).exists()


@pytest.mark.django_db
def test_qris_matched_on_amount_only_is_a_proposal():
    other = Reseller.objects.create(code="ZZ", name="Outlet Lain Sama Sekali")
    b = _qris_bank("2409500", outlet="QRIS ALFA 5 CELL", merchant_id="004767954")
    _otomax("TARTUN QR BULK TGL 05-SEP-2026", "2409500", channel=Channel.MERCHANT_BCA, reseller=other)

    run_match(BD)

    m = _active(b)
    assert m.needs_review
    assert "nominal saja" in m.review_reason


# --- Susulan lintas hari dengan teks mirip --------------------------------------------

SAHIDIN_OTOMAX_TANPA_NOMOR = "BAYAR KE MANDIRI MCM InhouseTrf DARI SAHIDIN TGL 30/AGS/2026"


def _similar_carry_case():
    bm = _bank(SAHIDIN_BANK, "500000", channel=Channel.MANDIRI, book_date=AUG30)
    run_match(AUG30)
    stale = Discrepancy.objects.get(bank_mutation=bm, kind=DiscrepancyKind.BANK_ONLY)
    oe = _payment(SAHIDIN_OTOMAX_TANPA_NOMOR, "500000", SEP3)
    run_match(SEP3)
    resolved = carry_forward(SEP3)
    return bm, oe, stale, resolved


@pytest.mark.django_db
def test_similar_late_entry_becomes_proposal_without_touching_old_discrepancy():
    bm, oe, stale, resolved = _similar_carry_case()

    assert resolved == 0  # usulan tidak dihitung "selisih lama ditutup"
    m = _active(bm)
    assert m.needs_review
    assert m.otomax_entry_id == oe.id
    stale.refresh_from_db()
    assert stale.status == DiscrepancyStatus.OPEN  # Adjustment belum boleh dibuat
    assert not Adjustment.objects.filter(discrepancy=stale).exists()


@pytest.mark.django_db
def test_approving_similar_late_entry_settles_old_and_new_discrepancies(operator):
    bm, oe, stale, _ = _similar_carry_case()

    approve_match(_active(bm), user=operator)

    stale.refresh_from_db()
    assert stale.status == DiscrepancyStatus.RESOLVED
    assert Adjustment.objects.filter(discrepancy=stale, book_date=AUG30).exists()
    assert not Discrepancy.objects.filter(otomax_entry=oe, status=DiscrepancyStatus.OPEN).exists()


@pytest.mark.django_db
def test_rejecting_similar_late_entry_keeps_old_discrepancy_open_and_is_not_reproposed():
    bm, oe, stale, _ = _similar_carry_case()

    unpair_match(_active(bm))
    run_match(SEP3)
    carry_forward(SEP3)

    stale.refresh_from_db()
    assert stale.status == DiscrepancyStatus.OPEN
    assert not Match.objects.filter(bank_mutation=bm, voided_at__isnull=True).exists()


@pytest.mark.django_db
def test_ambiguous_similar_candidates_are_not_proposed():
    bm = _bank(SAHIDIN_BANK, "500000", channel=Channel.MANDIRI, book_date=AUG30)
    run_match(AUG30)
    _payment(SAHIDIN_OTOMAX_TANPA_NOMOR, "500000", SEP3, reseller="PLC A")
    _payment("BAYAR KE MANDIRI MCM InhouseTrf DARI SAHIDIN 991", "500000", SEP3, reseller="PLC B")

    carry_forward(SEP3)

    assert not Match.objects.filter(bank_mutation=bm, voided_at__isnull=True).exists()


# --- Dashboard ------------------------------------------------------------------------


@pytest.mark.django_db
def test_review_tab_lists_only_proposals_and_approve_endpoint(auth_client):
    fuzzy_bank = _fuzzy_case()
    exact = _bank("ATMLTRPRM 01884 000001039 21540100059656", "550000")
    _otomax("TARTUN EDC BRI ATMLTRPRM 01884 000001039 21540100059656", "550000")
    run_match(BD)
    assert not _active(exact).needs_review

    res = auth_client.get("/matches/", {"d": BD.isoformat(), "tab": "review"})
    assert res.context["review_count"] == 1
    assert [m.bank_mutation_id for m in res.context["page_obj"].object_list] == [fuzzy_bank.id]

    day = auth_client.get("/", {"d": BD.isoformat()})
    assert day.context["proposal_count"] == 1

    m = _active(fuzzy_bank)
    auth_client.post(f"/matches/approve/{m.id}/", {"next_url": "/"})
    m.refresh_from_db()
    assert not m.needs_review


# --- Anti-Mismatch & Name Conflict Guards --------------------------------------------


def test_name_similarity_rejects_different_base_names_sharing_digit():
    from apps.recon.engine.qris_match import _name_similarity

    # Kasus bug: BAKSAR 2 CELL vs PLC ASBER2 sama-sama punya digit 2 tapi nama dasar beda
    assert _name_similarity("BAKSAR 2 CELL", "PLC ASBER2") == 0.0

    # Nama dasar sama persis dengan angka cabang sama
    assert _name_similarity("BAKSAR 2 CELL", "BAKSAR2") == 100.0
    assert _name_similarity("BAKSAR 2 CELL", "PLC BAKSAR 2") == 100.0

    # Angka cabang beda
    assert _name_similarity("BAKSAR 1 CELL", "BAKSAR 2") == 0.0


@pytest.mark.django_db
def test_qris_mapped_reseller_not_matched_to_different_reseller():
    """Engine tidak boleh memasangkan outlet QRIS ke reseller berbeda jika sudah terpetakan di MerchantMap."""
    r_baksar = Reseller.objects.create(code="BKS2", name="BAKSAR 2")
    r_asber = Reseller.objects.create(code="ASB2", name="ASBER 2")
    MerchantMap.objects.create(merchant_id="004769148", reseller=r_baksar)

    b = _qris_bank("3836000", outlet="QRIS BAKSAR 2 CELL", merchant_id="004769148")
    o = _otomax("TARTUN QR BULK ASBER 2 TGL 05-SEP-2026", "3836000", channel=Channel.MERCHANT_BCA, reseller=r_asber)
    o.reseller_name_raw = "PLC ASBER2"
    o.save(update_fields=["reseller_name_raw"])

    run_match(BD)

    b.refresh_from_db()
    assert b.match_status == MatchStatus.UNMATCHED
    assert not Match.objects.filter(bank_mutation=b, voided_at__isnull=True).exists()


@pytest.mark.django_db
def test_find_auto_pairs_rejects_conflicting_names_for_qris():
    """_find_auto_pairs tidak boleh memasangkan BAKSAR 2 CELL dengan PLC ASBER2."""
    from apps.dashboard.views._shared import _find_auto_pairs

    reseller = Reseller.objects.create(code="ASB2", name="ASBER 2")
    b = _qris_bank("3836000", outlet="QRIS BAKSAR 2 CELL", merchant_id="004769148")
    o = _otomax("TARTUN QR BULK ASBER 2 TGL 05-SEP-2026", "3836000", channel=Channel.MERCHANT_BCA, reseller=reseller)
    o.reseller_name_raw = "PLC ASBER2"
    o.save(update_fields=["reseller_name_raw"])

    pairs = _find_auto_pairs([b], [o])
    assert len(pairs) == 0


@pytest.mark.django_db
def test_find_auto_pairs_accepts_matching_names_for_qris():
    """_find_auto_pairs harus memasangkan BAKSAR 2 CELL dengan PLC BAKSAR2."""
    from apps.dashboard.views._shared import _find_auto_pairs

    reseller = Reseller.objects.create(code="BKS2", name="BAKSAR 2")
    b = _qris_bank("3836000", outlet="QRIS BAKSAR 2 CELL", merchant_id="004769148")
    o = _otomax("TARTUN QR BULK BAKSAR 2 TGL 05-SEP-2026", "3836000", channel=Channel.MERCHANT_BCA, reseller=reseller)
    o.reseller_name_raw = "PLC BAKSAR2"
    o.save(update_fields=["reseller_name_raw"])

    pairs = _find_auto_pairs([b], [o])
    assert len(pairs) == 1
    bm, oe, note = pairs[0]
    assert bm.id == b.id
    assert oe.id == o.id


@pytest.mark.django_db
def test_find_auto_pairs_rejects_channel_mismatch():
    """_find_auto_pairs tidak boleh memasangkan mutasi QRIS dengan entri bank transfer."""
    from apps.dashboard.views._shared import _find_auto_pairs

    b = _qris_bank("1500000", outlet="QRIS TOKO ABC", merchant_id="004769148")
    o = _otomax("BAYAR KE BRI TRANSFER TOKO ABC", "1500000", channel=Channel.BRI)

    pairs = _find_auto_pairs([b], [o])
    assert len(pairs) == 0


@pytest.mark.django_db
def test_find_auto_pairs_rejects_conflicting_parties_in_bank_transfer():
    """_find_auto_pairs tidak boleh memasangkan dua transfer beda pihak walau nominal unik."""
    from apps.dashboard.views._shared import _find_auto_pairs

    b = _bank("TRSF E-BANKING CR 1709/FTSCY/WS95011 500000.00 EDI SUHARTO", "500000", channel=Channel.BCA)
    o = _otomax("BFST215401000596563BUDI SANTOSO:CENAIDJA", "500000", channel=Channel.BCA)

    pairs = _find_auto_pairs([b], [o])
    assert len(pairs) == 0


@pytest.mark.django_db
def test_ref_match_multipass_deterministic_beats_auto_deposit_fallback():
    """Mutasi dengan kecocokan deterministik (exact/token) di urutan belakangan tidak boleh
    dicuri kandidat Otomax-nya oleh mutasi urutan awal yang jatuh ke fallback Auto Deposit."""
    # Bank #1 dibuat lebih dulu: merupakan Tartun PLC tanpa pasangan exact
    b_fallback = _bank("TARTUN PLC9999 SETORAN", "1250000")
    # Bank #2 dibuat belakangan: punya kecocokan exact dengan entri Otomax
    b_exact = _bank("AUTO DEPOSIT REF889900", "1250000")
    o = _otomax("AUTO DEPOSIT REF889900", "1250000")

    run_match(BD)

    b_exact.refresh_from_db()
    b_fallback.refresh_from_db()
    assert b_exact.match_status == MatchStatus.MATCHED
    assert b_fallback.match_status == MatchStatus.UNMATCHED
    m = _active(b_exact)
    assert m.otomax_entry_id == o.id
    assert m.needs_review is False


@pytest.mark.django_db
def test_tartun_plc_auto_deposit_rejects_conflicting_reseller_code():
    """Jika mutasi bank memiliki kode reseller terdaftar (misal PLC131 -> PLC CIPADUNG 2),
    fallback Auto Deposit tidak boleh memasangkannya ke Auto Deposit milik reseller lain."""
    Reseller.objects.get_or_create(code="PLC131", defaults={"name": "PLC CIPADUNG 2"})
    b = _bank("TARTUN PLC131 SETOR TUNAI", "2000000")
    o = _otomax("AUTO DEPOSIT", "2000000")
    o.reseller_name_raw = "PLC ASBER 2"
    o.save(update_fields=["reseller_name_raw"])

    run_match(BD)

    b.refresh_from_db()
    assert b.match_status == MatchStatus.UNMATCHED
    assert not Match.objects.filter(bank_mutation=b, voided_at__isnull=True).exists()


@pytest.mark.django_db
def test_qris_pass2_rejects_conflicting_branch_outlets_without_merchant_map():
    """Tanpa MerchantMap sekalipun, Pass 2 QRIS tidak boleh mengusulkan BAKSAR 2 CELL dengan PLC ASBER2."""
    b = _qris_bank("3836000", outlet="QRIS BAKSAR 2 CELL", merchant_id="004769148")
    o = _otomax("TARTUN QR BULK ASBER 2 TGL 05-SEP-2026", "3836000", channel=Channel.MERCHANT_BCA)
    o.reseller_name_raw = "PLC ASBER2"
    o.save(update_fields=["reseller_name_raw"])

    run_match(BD)

    b.refresh_from_db()
    assert b.match_status == MatchStatus.UNMATCHED
    assert not Match.objects.filter(bank_mutation=b, voided_at__isnull=True).exists()


@pytest.mark.django_db
def test_learn_merchant_map_does_not_poison_on_conflicting_names():
    """learn_merchant_map tidak boleh menyimpan MerchantMap jika nama outlet & reseller bertolak belakang."""
    from apps.recon.engine.qris_match import learn_merchant_map

    r_asber = Reseller.objects.create(code="ASB2", name="PLC ASBER2")
    b = _qris_bank("3836000", outlet="QRIS BAKSAR 2 CELL", merchant_id="004769148")
    o = _otomax("TARTUN QR BULK ASBER 2", "3836000", channel=Channel.MERCHANT_BCA, reseller=r_asber)

    learn_merchant_map(b, o)
    assert not MerchantMap.objects.filter(merchant_id="004769148").exists()


@pytest.mark.django_db
def test_qris_matches_via_reseller_alias():
    """ResellerAlias di database harus dikenali oleh Pass 1 _match_qris meskipun ejaan outlet berbeda."""
    from apps.catalog.models import ResellerAlias
    from apps.core.normalize import norm_ref

    reseller = Reseller.objects.create(code="PLC777", name="PLC MITRA UTAMA")
    ResellerAlias.objects.create(
        alias_raw="WARUNG PULSA MANDIRI",
        alias_norm=norm_ref("WARUNG PULSA MANDIRI"),
        reseller=reseller,
    )
    b = _qris_bank("1750000", outlet="WARUNG PULSA MANDIRI", merchant_id="009988776")
    o = _otomax("TARTUN QR BULK MITRA UTAMA TGL 05-SEP-2026", "1750000", channel=Channel.MERCHANT_BCA, reseller=reseller)
    o.reseller_name_raw = "PLC MITRA UTAMA"
    o.save(update_fields=["reseller_name_raw"])

    run_match(BD)

    b.refresh_from_db()
    assert b.match_status == MatchStatus.MATCHED
    m = _active(b)
    assert m.needs_review is False


@pytest.mark.django_db
def test_carry_forward_supports_qris_multi_ticket_aggregate():
    """carry_forward harus bisa mengusulkan gabungan beberapa tiket QRIS susulan lintas hari."""
    from datetime import timedelta

    from apps.core.enums import MatchType
    from apps.ingest.models import OtomaxEntry

    b = _qris_bank("500000", outlet="QRIS CILENGKRANG 1", merchant_id="004760001")
    run_match(BD)
    b.refresh_from_db()
    assert b.match_status == MatchStatus.UNMATCHED
    disc = Discrepancy.objects.get(bank_mutation=b, status=DiscrepancyStatus.OPEN)

    late_date = BD + timedelta(days=4)
    batch_late = _batch(Channel.OTOMAX)
    o1 = OtomaxEntry.objects.create(
        import_batch=batch_late,
        book_date=late_date,
        reseller_name_raw="PLC CL1",
        amount=Decimal("300000"),
        description_raw="TARTUN QR BULK CL1 PART 1",
        channel_hint=Channel.MERCHANT_BCA,
        row_hash="late-qris-1",
    )
    o2 = OtomaxEntry.objects.create(
        import_batch=batch_late,
        book_date=late_date,
        reseller_name_raw="PLC CL1",
        amount=Decimal("200000"),
        description_raw="TARTUN QR BULK CL1 PART 2",
        channel_hint=Channel.MERCHANT_BCA,
        row_hash="late-qris-2",
    )

    carry_forward(late_date)

    m = _active(b)
    assert m.match_type == MatchType.AGGREGATE
    assert m.needs_review is True
    assert set(m.otomax_entries.values_list("id", flat=True)) == {o1.id, o2.id}

    # Saat operator menyetujui usulan, discrepancy asal otomatis RESOLVED
    approve_match(m)
    disc.refresh_from_db()
    assert disc.status == DiscrepancyStatus.RESOLVED


@pytest.mark.django_db
def test_rbac_regular_operator_restricted_from_privileged_actions():
    """Operator biasa (non-staff, non-superuser) tidak boleh write-off selisih, buka buku,
    tutup buku paksa, atau menghapus batch milik user lain."""
    admin_user = User.objects.create_superuser(username="admin_rbac", password="password123")
    op_user = User.objects.create_user(username="operator_biasa", password="password123")

    client = Client()
    client.force_login(op_user)

    # 1. Tidak boleh hapus batch yang diunggah admin
    batch = _batch(Channel.BRI)
    batch.uploaded_by = admin_user
    batch.save(update_fields=["uploaded_by"])
    res_del = client.post(f"/upload/delete-batch/{batch.id}/", {"book_date": BD.isoformat()})
    assert res_del.status_code == 302
    assert batch.__class__.objects.filter(pk=batch.id).exists()

    # 2. Tidak boleh write-off selisih
    disc = Discrepancy.objects.create(
        code="SLS-RBAC-001",
        origin_book_date=BD,
        channel=Channel.BRI,
        kind=DiscrepancyKind.BANK_ONLY,
        amount=Decimal("100000"),
    )
    res_wo = client.post(f"/discrepancy/{disc.pk}/resolve/", {"action": "write_off", "book_date": BD.isoformat()})
    assert res_wo.status_code == 302
    disc.refresh_from_db()
    assert disc.status == DiscrepancyStatus.OPEN


@pytest.mark.django_db
def test_matches_checklist_bar_and_bulk_approve(auth_client, operator):
    """Menu Hasil Cocok merender checklist bar dan mendukung konfirmasi cocok bersamaan."""
    b1 = _fuzzy_case()
    b2 = _qris_bank("800000", outlet="QRIS ALFA 9 CELL", merchant_id="004769999")
    _otomax("TARTUN QR BULK ALFA 9 PART 1", "500000", channel=Channel.MERCHANT_BCA)
    _otomax("TARTUN QR BULK ALFA 9 PART 2", "300000", channel=Channel.MERCHANT_BCA)

    run_match(BD)

    m1 = _active(b1)
    m2 = _active(b2)
    assert m1.needs_review is True
    assert m2.needs_review is True

    # Pastikan halaman Hasil Cocok menampilkan checklist bar & checkbox untuk tiap baris
    res_page = auth_client.get("/matches/", {"d": BD.isoformat()})
    assert res_page.status_code == 200
    html = res_page.content.decode()
    assert "Konfirmasi Cocok" in html
    assert "Pilih Semua" in html
    assert f'value="{m1.id}"' in html
    assert f'value="{m2.id}"' in html

    # Konfirmasi keduanya sekaligus lewat endpoint approve-match-bulk
    res_bulk = auth_client.post(
        "/matches/approve-bulk/",
        {
            "book_date": BD.isoformat(),
            "selected_ids": [str(m1.id), str(m2.id)],
            "next_url": f"/matches/?d={BD.isoformat()}",
        },
    )
    assert res_bulk.status_code == 302

    m1.refresh_from_db()
    m2.refresh_from_db()
    assert m1.needs_review is False
    assert m2.needs_review is False
    assert m1.reviewed_by == operator
    assert m2.reviewed_by == operator


