from datetime import date

from apps.core.period import business_month_range


def test_start_day_1_is_plain_calendar_month():
    assert business_month_range(date(2026, 9, 15), 1) == (date(2026, 9, 1), date(2026, 9, 30))


def test_custom_start_day_before_cycle_start_uses_previous_month():
    # 20 Sep, siklus mulai tgl 29 -> belum masuk siklus baru -> 29 Agu s/d 28 Sep
    assert business_month_range(date(2026, 9, 20), 29) == (date(2026, 8, 29), date(2026, 9, 28))


def test_custom_start_day_on_or_after_cycle_start_uses_current_month():
    # 29 Sep, siklus mulai tgl 29 -> pas mulai siklus baru -> 29 Sep s/d 28 Okt
    assert business_month_range(date(2026, 9, 29), 29) == (date(2026, 9, 29), date(2026, 10, 28))
    # 30 Sep -> masih di siklus yang sama (29 Sep s/d 28 Okt)
    assert business_month_range(date(2026, 9, 30), 29) == (date(2026, 9, 29), date(2026, 10, 28))


def test_start_day_clamped_on_short_month_february_non_leap_year():
    # start_day=29, tahun non-kabisat -> Februari cuma 28 hari. Tgl 28 Feb dipakukan
    # jadi awal siklus BERIKUTNYA (bukan akhir siklus Jan) -> siklus Jan berakhir 27 Feb,
    # supaya tidak ada tanggal yang bolong atau tumpang tindih antar siklus.
    assert business_month_range(date(2027, 2, 15), 29) == (date(2027, 1, 29), date(2027, 2, 27))
    assert business_month_range(date(2027, 2, 28), 29) == (date(2027, 2, 28), date(2027, 3, 28))


def test_year_rollover_at_december():
    assert business_month_range(date(2026, 12, 20), 29) == (date(2026, 11, 29), date(2026, 12, 28))
    assert business_month_range(date(2027, 1, 5), 29) == (date(2026, 12, 29), date(2027, 1, 28))
