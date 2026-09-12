from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from prunebot.domain import windows as w


def test_day_of_is_utc_midnight_aligned():
    assert w.day_of(datetime(1970, 1, 1, 0, 0, tzinfo=UTC)) == 0
    assert w.day_of(datetime(1970, 1, 1, 23, 59, 59, tzinfo=UTC)) == 0
    assert w.day_of(datetime(1970, 1, 2, 0, 0, tzinfo=UTC)) == 1


def test_naive_datetimes_are_treated_as_utc():
    naive = datetime(2026, 6, 15, 12, 0)
    aware = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    assert w.day_of(naive) == w.day_of(aware)


def test_window_covers_exactly_window_days_buckets():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    today = w.day_of(now)
    start = w.window_start_day(now, 30)
    # Inclusive of both ends: today plus the previous 29 days.
    assert today - start + 1 == 30


@pytest.mark.parametrize("window_days", [1, 2, 7, 30, 365])
def test_window_size_is_exact_for_any_window(window_days):
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    start = w.window_start_day(now, window_days)
    assert w.day_of(now) - start + 1 == window_days


def test_window_of_one_day_is_today_only():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    assert w.window_start_day(now, 1) == w.day_of(now)


def test_window_days_below_one_is_rejected():
    with pytest.raises(ValueError):
        w.window_start_day(datetime.now(tz=UTC), 0)


def test_sum_window_ignores_buckets_before_the_start():
    buckets = {100: 5, 101: 3, 102: 7}
    assert w.sum_window(buckets, 100) == 15
    assert w.sum_window(buckets, 101) == 10
    assert w.sum_window(buckets, 103) == 0


def test_retention_cutoff_keeps_exactly_retention_days():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    cutoff = w.retention_cutoff_day(now, 120)
    assert w.day_of(now) - cutoff + 1 == 120


def test_has_elapsed_boundary_is_inclusive():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert not w.has_elapsed(start, 90, start + timedelta(days=89, hours=23))
    assert w.has_elapsed(start, 90, start + timedelta(days=90))
    assert w.has_elapsed(start, 90, start + timedelta(days=91))


def test_has_elapsed_with_no_start_never_elapses():
    # Load-bearing: a missing warned_at must never start a kick clock.
    assert w.has_elapsed(None, 0, datetime.now(tz=UTC)) is False
    assert w.has_elapsed(None, 90, datetime.now(tz=UTC)) is False


def test_epoch_roundtrip():
    moment = datetime(2026, 6, 15, 12, 34, 56, tzinfo=UTC)
    assert w.from_epoch(w.to_epoch(moment)) == moment
    assert w.from_epoch(None) is None



# ------------------------------------------------------------- flag countdown


def test_days_until_inactive_counts_down_from_the_oldest_post_keeping_them_over():
    today = 1000
    assert w.days_until_inactive({today: 1}, today, 30, 1) == 30
    # the only message was 29 days ago, so it leaves the window tomorrow
    assert w.days_until_inactive({today - 29: 1}, today, 30, 1) == 1


def test_days_until_inactive_is_zero_when_already_under_the_line():
    assert w.days_until_inactive({}, 1000, 30, 1) == 0
    assert w.days_until_inactive({1000: 9}, 1000, 30, 10) == 0


def test_days_until_inactive_ignores_buckets_outside_the_window():
    assert w.days_until_inactive({1000 - 30: 50}, 1000, 30, 1) == 0


def test_days_until_inactive_with_a_higher_threshold():
    today = 1000
    # 6 today and 4 ten days ago: the 10th most recent message is 10 days old
    assert w.days_until_inactive({today: 6, today - 10: 4}, today, 30, 10) == 20
