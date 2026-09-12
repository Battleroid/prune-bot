"""Day-bucket arithmetic.

Activity is bucketed per UTC day, so every window calculation is integer maths on
"days since the Unix epoch" rather than timestamp comparison. All functions are
pure and take `now` explicitly so tests never need to freeze the clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

SECONDS_PER_DAY = 86400


def to_epoch(moment: datetime) -> int:
    """Unix seconds. Naive datetimes are treated as UTC rather than local time."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp())


def from_epoch(seconds: int | None) -> datetime | None:
    return None if seconds is None else datetime.fromtimestamp(seconds, tz=UTC)


def day_of(moment: datetime | int) -> int:
    """The UTC day bucket containing `moment`."""
    seconds = moment if isinstance(moment, int) else to_epoch(moment)
    return seconds // SECONDS_PER_DAY


def day_start(day: int) -> datetime:
    return datetime.fromtimestamp(day * SECONDS_PER_DAY, tz=UTC)


def window_start_day(now: datetime, window_days: int) -> int:
    """First day bucket inside the window.

    The window is `window_days` buckets ending with (and including) today, so a
    30-day window covers today plus the previous 29 days.
    """
    if window_days < 1:
        raise ValueError("window_days must be at least 1")
    return day_of(now) - window_days + 1


def retention_cutoff_day(now: datetime, retention_days: int) -> int:
    """Buckets strictly older than this are safe to delete."""
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")
    return day_of(now) - retention_days + 1


def days_between(earlier: datetime, later: datetime) -> float:
    return (later - earlier).total_seconds() / SECONDS_PER_DAY


def has_elapsed(since: datetime | None, days: int, now: datetime) -> bool:
    """True when at least `days` have passed since `since`.

    A `None` start never counts as elapsed -- callers rely on this so that a
    missing `warned_at` can never start a kick clock.
    """
    if since is None:
        return False
    return now >= since + timedelta(days=days)


def elapsed_days(since: datetime | None, now: datetime) -> float | None:
    return None if since is None else days_between(since, now)


def sum_window(buckets: dict[int, int], since_day: int) -> int:
    """Total messages in buckets at or after `since_day`."""
    return sum(count for day, count in buckets.items() if day >= since_day)


def days_until_inactive(
    buckets: dict[int, int], today: int, window_days: int, min_messages: int
) -> int:
    """Whole days until a member would first count as inactive if they never post
    again. 0 means they already do.

    Walk back from the most recent day adding up messages. The day on which the
    running total reaches `min_messages` is the oldest bucket still keeping them
    over the line, and they drop below it the day that bucket leaves the window.
    """
    start = today - window_days + 1
    total = 0
    for day in sorted((d for d in buckets if start <= d <= today), reverse=True):
        total += buckets[day]
        if total >= min_messages:
            return day + window_days - today
    return 0
