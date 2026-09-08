from datetime import date

import pytest

from apps.catalog.models import Reseller
from apps.core.enums import Channel
from apps.ingest.models import BankMutation, ImportBatch
from apps.recon.close import close_day
from apps.recon.engine import run_match
from apps.recon.models import Discrepancy, ReconDay
from apps.recon.purge import DayIsClosed, purge_all_transactions, purge_day

from .test_engine import _bank, _otomax

BD = date(2026, 9, 5)


@pytest.mark.django_db
def test_purge_day_removes_txn_keeps_catalog():
    Reseller.objects.create(code="R1", name="R1")
    _bank("ATMLTRPRM 01884 000001039 21540100059656", "550000")
    _otomax("TARTUN EDC BRI ATMLTRPRM 01884 000001039 21540100059656", "550000")
    run_match(BD)

    purge_day(BD)

    assert BankMutation.objects.count() == 0
    assert ImportBatch.objects.count() == 0
    assert Reseller.objects.count() == 1  # katalog aman


@pytest.mark.django_db
def test_purge_day_blocked_when_closed_unless_flag():
    _bank("ATMLTRPRM 01884 000001039 21540100059656", "550000")
    run_match(BD)
    close_day(BD, force=True)

    with pytest.raises(DayIsClosed):
        purge_day(BD)

    purge_day(BD, include_closed=True)
    assert ReconDay.objects.count() == 0


@pytest.mark.django_db
def test_purge_all_transactions():
    _bank("DANA20260905034895588601ASEPKURNIAWA", "1600000", channel=Channel.BRI)
    run_match(BD)
    close_day(BD, force=True)
    assert Discrepancy.objects.exists()

    counts = purge_all_transactions()

    assert sum(counts.values()) > 0
    assert not Discrepancy.objects.exists()
    assert not ReconDay.objects.exists()
