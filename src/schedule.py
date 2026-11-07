"""Per-user summary schedule.

Cloud Scheduler calls /run every hour. A user is *due* when the latest summary
slot (08:00 or 20:00 in the user's timezone, see SUMMARY_HOURS) hasn't been
processed yet. Missed slots (outage, run deadline) are caught up at the next
hourly run; a user who never had a run is picked up within FIRST_RUN_GRACE of a
slot (so joining at 14:00 doesn't trigger a "morning" summary in the afternoon).
"""

import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from src import config
from src.models import User

FIRST_RUN_GRACE = timedelta(hours=3)

_OFFSET = re.compile(r"^(?:utc|gmt)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def parse_timezone(text: str) -> str | None:
    """Normalize user input to a storable timezone string, or None if unknown.

    Accepts IANA names ("Europe/Berlin"), city names ("jakarta"), "UTC",
    and offsets ("+7", "UTC+5:30", "-03:00").
    """
    value = text.strip()
    if not value:
        return None
    if value.upper() in ("UTC", "GMT", "Z"):
        return "UTC"
    m = _OFFSET.match(value.replace(" ", ""))
    if m:
        sign, hours, minutes = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        if hours > 14 or minutes >= 60:
            return None
        return f"{sign}{hours:02d}:{minutes:02d}"
    try:
        ZoneInfo(value)
        return value
    except (ZoneInfoNotFoundError, ValueError):
        pass
    # case-insensitive match on full name or city part ("new york" -> America/New_York)
    wanted = value.lower().replace(" ", "_")
    matches = sorted(
        tz for tz in available_timezones()
        if tz.lower() == wanted or tz.lower().rsplit("/", 1)[-1] == wanted
    )
    return matches[0] if matches else None


def tz_of(name: str) -> tzinfo:
    m = re.match(r"^([+-])(\d{2}):(\d{2})$", name or "")
    if m:
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        return timezone(delta if m.group(1) == "+" else -delta)
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def user_tz_name(user: User) -> str:
    return user.timezone or config.settings.default_timezone or "UTC"


def user_tz(user: User) -> tzinfo:
    return tz_of(user_tz_name(user))


def summary_hours() -> list[int]:
    hours = sorted({int(h) for h in config.settings.summary_hours.split(",") if h.strip().isdigit() and int(h) < 24})
    return hours or [8, 20]


def _slots_around(now: datetime, tz: tzinfo) -> list[datetime]:
    local_day = now.astimezone(tz).date()
    slots = []
    for offset in (-1, 0, 1):
        day = local_day + timedelta(days=offset)
        for h in summary_hours():
            slots.append(datetime.combine(day, time(h), tzinfo=tz).astimezone(timezone.utc))
    return sorted(slots)


def current_slot(now: datetime, tz: tzinfo) -> datetime:
    """Latest slot start <= now (UTC)."""
    return max(s for s in _slots_around(now, tz) if s <= now)


def next_slot(now: datetime, tz: tzinfo) -> datetime:
    """Earliest slot start > now (UTC)."""
    return min(s for s in _slots_around(now, tz) if s > now)


def due_slot(user: User, now: datetime) -> datetime | None:
    """The slot to process for ``user`` now, or None if not due."""
    slot = current_slot(now, user_tz(user))
    last = user.last_run_slot
    if last is None:
        return slot if now - slot < FIRST_RUN_GRACE else None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return slot if last < slot else None


def fmt_local(dt: datetime | None, user: User, fmt: str = "%b %d %H:%M") -> str:
    """Format a UTC datetime in the user's timezone."""
    if dt is None:
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(user_tz(user)).strftime(fmt)


def local_date(now: datetime, user: User) -> date:
    return now.astimezone(user_tz(user)).date()
