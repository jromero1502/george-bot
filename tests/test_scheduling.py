from datetime import datetime, timezone

import pytest

from george.scheduling import compute_next_run, next_state_after_fire


def test_cron_daily_bogota_evening_to_utc():
    # 8pm every day, America/Bogota (UTC-5, no DST) -> 01:00 UTC the next day.
    schedule = {"type": "cron", "cron": "0 20 * * *", "timezone": "America/Bogota"}
    after = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)  # 07:00 Bogota
    result = compute_next_run(schedule, after=after)
    assert result == "2026-03-11T01:00:00Z"


def test_cron_weekly_specific_day():
    # Every Monday at 07:00 Bogota. day-of-week 1 = Monday in standard cron.
    schedule = {"type": "cron", "cron": "0 7 * * 1", "timezone": "America/Bogota"}
    # 2026-03-10 is a Tuesday -> next Monday is 2026-03-16.
    after = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    result = compute_next_run(schedule, after=after)
    assert result == "2026-03-16T12:00:00Z"


def test_once_schedule_returns_run_at_regardless_of_after():
    schedule = {"type": "once", "runAt": "2026-12-01T09:30:00", "timezone": "America/Bogota"}
    result = compute_next_run(schedule)
    assert result == "2026-12-01T14:30:00Z"


def test_once_schedule_with_explicit_tz_offset_is_respected():
    schedule = {"type": "once", "runAt": "2026-12-01T09:30:00-03:00", "timezone": "America/Bogota"}
    result = compute_next_run(schedule)
    assert result == "2026-12-01T12:30:00Z"


def test_dst_crossing_handled_correctly():
    # New York DST ends 2026-11-01 at 2am local. A daily 09:00 cron just
    # before and after the transition should both land on 09:00 local time,
    # which means a different UTC offset (13:00Z before, 14:00Z after).
    schedule = {"type": "cron", "cron": "0 9 * * *", "timezone": "America/New_York"}

    before_dst_end = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)
    assert compute_next_run(schedule, after=before_dst_end) == "2026-10-30T13:00:00Z"

    after_dst_end = datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)
    assert compute_next_run(schedule, after=after_dst_end) == "2026-11-02T14:00:00Z"


def test_invalid_cron_expression_raises():
    schedule = {"type": "cron", "cron": "not a cron", "timezone": "America/Bogota"}
    with pytest.raises(ValueError):
        compute_next_run(schedule)


def test_missing_cron_field_raises():
    schedule = {"type": "cron", "timezone": "America/Bogota"}
    with pytest.raises(ValueError):
        compute_next_run(schedule)


def test_missing_run_at_field_raises():
    schedule = {"type": "once", "timezone": "America/Bogota"}
    with pytest.raises(ValueError):
        compute_next_run(schedule)


def test_unknown_schedule_type_raises():
    with pytest.raises(ValueError):
        compute_next_run({"type": "monthly"})


def test_next_state_after_fire_once_completes():
    reminder = {"schedule": {"type": "once", "runAt": "2026-01-01T00:00:00", "timezone": "America/Bogota"}}
    assert next_state_after_fire(reminder) == {"status": "completed"}


def test_next_state_after_fire_cron_advances():
    reminder = {"schedule": {"type": "cron", "cron": "0 20 * * *", "timezone": "America/Bogota"}}
    updates = next_state_after_fire(reminder)
    assert "nextRunAt" in updates
    assert updates["nextRunAt"].endswith("Z")
