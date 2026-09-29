"""Posting slots like "mon 17:00" or "daily 09:30", used to auto-pick a time."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
GROUPS = {
    "daily": set(range(7)),
    "weekdays": set(range(5)),
    "weekends": {5, 6},
}


def parse_slot(slot: str) -> tuple[set[int], time]:
    try:
        day_part, time_part = slot.lower().split()
        hour, minute = (int(x) for x in time_part.split(":"))
        at = time(hour, minute)
    except ValueError:
        raise ValueError(f"Bad slot {slot!r}; use e.g. 'mon 17:00', 'daily 09:30', 'weekdays 12:00'") from None
    days: set[int] = set()
    for token in day_part.split(","):
        token = token[:3] if token[:3] in DAYS else token
        if token in GROUPS:
            days |= GROUPS[token]
        elif token in DAYS:
            days.add(DAYS[token])
        else:
            raise ValueError(f"Unknown day {token!r} in slot {slot!r}")
    return days, at


def next_free_slot(
    slots: Iterable[str],
    taken: Iterable[datetime],
    now: datetime,
    tz: ZoneInfo,
    horizon_days: int = 120,
) -> datetime:
    parsed = [parse_slot(s) for s in slots]
    if not parsed:
        raise ValueError("No slots configured in config.yaml; pass --at instead")
    taken_set = {t.astimezone(tz).replace(second=0, microsecond=0) for t in taken}
    local_now = now.astimezone(tz)
    for offset in range(horizon_days):
        day = local_now.date() + timedelta(days=offset)
        candidates = sorted(
            datetime.combine(day, at, tzinfo=tz) for days, at in parsed if day.weekday() in days
        )
        for candidate in candidates:
            if candidate > local_now and candidate not in taken_set:
                return candidate
    raise ValueError(f"No free slot in the next {horizon_days} days")
