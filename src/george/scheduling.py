"""Cron/once schedule -> next UTC run time. `reminders.nextRunAt` is always
stored in UTC (so the timer trigger can compare it against `utcnow()` with a
plain string comparison); the schedule's own `timezone` field is only used
to interpret wall-clock times (e.g. "8pm every day" in America/Bogota).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from croniter import croniter

from george.util import utc_now

DEFAULT_TIMEZONE = "America/Bogota"


def _to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def compute_next_run(schedule: dict[str, Any], after: Optional[datetime] = None) -> str:
    """Returns the next occurrence (as a UTC ISO-8601 string) strictly after
    `after` (defaults to now). For schedule.type == 'once' this is simply
    schedule.runAt — callers must not invoke this again for a 'once' schedule
    after it has fired; use `next_state_after_fire` instead."""
    schedule_type = schedule.get("type")
    tz = ZoneInfo(schedule.get("timezone") or DEFAULT_TIMEZONE)

    if schedule_type == "cron":
        cron_expr = schedule.get("cron")
        if not cron_expr:
            raise ValueError("schedule.cron es requerido cuando schedule.type='cron'")
        if not croniter.is_valid(cron_expr):
            raise ValueError(f"Expresion cron invalida: {cron_expr!r}")
        base = (after or utc_now()).astimezone(tz)
        next_local = croniter(cron_expr, base).get_next(datetime)
        return _to_utc_iso(next_local)

    if schedule_type == "once":
        run_at = schedule.get("runAt")
        if not run_at:
            raise ValueError("schedule.runAt es requerido cuando schedule.type='once'")
        dt = datetime.fromisoformat(run_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return _to_utc_iso(dt)

    raise ValueError(f"schedule.type desconocido: {schedule_type!r} (usa 'cron' o 'once')")


def next_state_after_fire(reminder: dict[str, Any]) -> dict[str, Any]:
    """What to update on a reminder document right after it fires: a 'once'
    reminder is retired (status=completed); a 'cron' reminder gets its next
    occurrence computed from *now*, so a missed/delayed firing doesn't cause
    the next one to land early."""
    schedule = reminder["schedule"]
    if schedule.get("type") == "once":
        return {"status": "completed"}
    return {"nextRunAt": compute_next_run(schedule, after=utc_now())}
