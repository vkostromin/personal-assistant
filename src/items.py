"""Items (promises / waiting-for / replies / to-dos): due dates, reminders, focus ranking.

Reminder policy (all times in the user's timezone, never during QUIET_HOURS):
  promise / todo / reply with a due date:
      stage 0 → heads-up 2h before due
      stage 1 → at due ("now overdue")
      stage 2.. → daily at 09:00 while overdue, MAX_OVERDUE_REMINDERS times, then only in summaries
  waiting (someone owes me):
      stage 1 → at due ("X hasn't answered") — no heads-up
      stage 2.. → daily at 09:00, MAX_OVERDUE_REMINDERS times
  snoozed: one reminder when the snooze ends, then the normal policy continues.
  no due date: no proactive reminders — shown in Focus / summaries by priority.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone, tzinfo

from src import config
from src.models import Item

HEADS_UP = timedelta(hours=2)
MAX_OVERDUE_REMINDERS = 3
DAILY_REMINDER_HOUR = 9
END_OF_DAY_HOUR = 18

KIND_LABEL = {
    "promise": "🫡 I promised",
    "waiting": "⏳ Waiting for",
    "reply": "💬 Needs my reply",
    "todo": "📌 To do",
}
PRIORITY_ICON = {"high": "🔴", "medium": "🟡", "low": "⚪"}


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ------------------------------------------------------------------ due dates
def parse_due(value, tz: tzinfo) -> datetime | None:
    """LLM/ISO due string → aware UTC datetime. Date-only values mean end of that local day."""
    if not value or not isinstance(value, str) or value.strip().lower() in ("null", "none", ""):
        return None
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        dt = datetime.combine(dt.date(), time(END_OF_DAY_HOUR), tzinfo=tz)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def at_local(now: datetime, tz: tzinfo, hour: int, days: int = 0) -> datetime:
    """``hour``:00 local time, ``days`` days after today's local date (UTC result)."""
    day = now.astimezone(tz).date() + timedelta(days=days)
    return datetime.combine(day, time(hour), tzinfo=tz).astimezone(timezone.utc)


def next_local(now: datetime, tz: tzinfo, hour: int) -> datetime:
    """The next ``hour``:00 local time strictly after ``now``."""
    t = at_local(now, tz, hour)
    return t if t > now else at_local(now, tz, hour, days=1)


def quiet_range() -> tuple[int, int]:
    try:
        start, end = (int(x) for x in config.settings.quiet_hours.split("-"))
        return start % 24, end % 24
    except ValueError:
        return 21, 8


def in_quiet_hours(t: datetime, tz: tzinfo) -> bool:
    start, end = quiet_range()
    hour = t.astimezone(tz).hour
    if start == end:
        return False
    return hour >= start or hour < end if start > end else start <= hour < end


def out_of_quiet(t: datetime, tz: tzinfo) -> datetime:
    """Move a reminder time out of quiet hours to the end of the quiet period."""
    if not in_quiet_hours(t, tz):
        return t
    _, end = quiet_range()
    candidate = at_local(t, tz, end)
    return candidate if candidate > t else at_local(t, tz, end, days=1)


# ------------------------------------------------------------------ reminders
def next_reminder(item: Item, now: datetime, tz: tzinfo) -> datetime | None:
    """When the next proactive reminder for ``item`` is due (None = no reminder)."""
    if item.status != "open":
        return None
    snoozed = _aware(item.snoozed_until)
    if snoozed and snoozed > now:
        return snoozed  # snooze end is explicit: not shifted by quiet hours
    due = _aware(item.due_at)
    if due is None:
        return None
    stage = item.remind_stage
    if item.kind == "waiting" and stage == 0:
        stage = 1
    if stage == 0:
        t = max(due - HEADS_UP, now)
    elif stage == 1:
        t = max(due, now)
    elif stage - 1 <= MAX_OVERDUE_REMINDERS:
        last = _aware(item.last_reminded_at) or now
        # next 09:00 at least half a day after the last reminder (a reminder sent at 08:00
        # when quiet hours end must not be repeated at 09:00 the same morning)
        t = max(next_local(last + timedelta(hours=12), tz, DAILY_REMINDER_HOUR), now)
    else:
        return None
    return out_of_quiet(t, tz)


def after_reminder(item: Item, now: datetime, tz: tzinfo) -> dict:
    """Firestore updates after a reminder card for ``item`` was sent."""
    snoozed = _aware(item.snoozed_until)
    due = _aware(item.due_at)
    updates: dict = {"last_reminded_at": now}
    if snoozed and snoozed <= now:
        updates["snoozed_until"] = None  # snooze ended; policy stage unchanged
        stage = item.remind_stage
    elif due and now < due:
        stage = 1  # heads-up sent
    else:
        stage = max(item.remind_stage, 1) + 1  # overdue reminder sent
    updates["remind_stage"] = stage
    following = item.model_copy(update={"remind_stage": stage, "last_reminded_at": now, "snoozed_until": None if "snoozed_until" in updates else item.snoozed_until})
    updates["next_remind_at"] = next_reminder(following, now, tz)
    return updates


def reschedule(item: Item, now: datetime, tz: tzinfo) -> dict:
    """Updates after due date / snooze / status changed by the user."""
    return {"next_remind_at": next_reminder(item, now, tz)}


# ----------------------------------------------------------------- snooze / due
def snooze_until(choice: str, now: datetime, tz: tzinfo) -> datetime:
    """'2' / '4' / '8' hours, or 'tm' = tomorrow 09:00 local."""
    if choice == "tm":
        return at_local(now, tz, DAILY_REMINDER_HOUR, days=1)
    return now + timedelta(hours=int(choice))


def due_choice(choice: str, now: datetime, tz: tzinfo) -> datetime | None:
    """Preset due dates from the card menu."""
    if choice == "today":
        t = at_local(now, tz, END_OF_DAY_HOUR)
        return t if t > now else now + timedelta(hours=2)
    if choice == "tm":
        return at_local(now, tz, END_OF_DAY_HOUR, days=1)
    if choice == "3d":
        return at_local(now, tz, END_OF_DAY_HOUR, days=3)
    if choice == "mon":
        local = now.astimezone(tz)
        days = (7 - local.weekday()) % 7 or 7
        return at_local(now, tz, END_OF_DAY_HOUR, days=days)
    return None  # "none": clear due date


# -------------------------------------------------------------------- dedupe
def fingerprint(kind: str, title: str, counterparty: str) -> str:
    norm = re.sub(r"[^a-z0-9а-яё]+", " ", f"{title} {counterparty}".lower()).strip()
    return hashlib.sha1(f"{kind}|{norm[:120]}".encode()).hexdigest()[:16]


# --------------------------------------------------------------------- focus
@dataclass
class FocusEntry:
    type: str  # "item" | "draft"
    ref: dict | Item
    score: float


PRIORITY_SCORE = {"high": 40, "medium": 20, "low": 5}
KIND_SCORE = {"reply": 15, "promise": 15, "todo": 10, "waiting": 0}
DRAFT_SCORE = 20  # a reply ready to send is a quick win


def _due_score(due: datetime | None, now: datetime) -> float:
    if due is None:
        return 0
    hours = (due - now).total_seconds() / 3600
    if hours < 0:
        return 60 + min(-hours, 72) / 3
    if hours < 24:
        return 45
    if hours < 72:
        return 20
    return 0


def item_score(item: Item, now: datetime) -> float | None:
    """Higher = do it sooner. None = nothing to do right now."""
    if item.status != "open":
        return None
    snoozed = _aware(item.snoozed_until)
    if snoozed and snoozed > now:
        return None
    due = _aware(item.due_at)
    if item.kind == "waiting" and (due is None or due > now):
        return None  # nothing to do until they're late
    age_days = (now - _aware(item.created_at)).total_seconds() / 86400
    return PRIORITY_SCORE[item.priority] + KIND_SCORE[item.kind] + _due_score(due, now) + min(age_days * 2, 10)


def draft_score(draft: dict, now: datetime) -> float | None:
    snoozed = _aware(draft.get("snoozed_until"))
    if draft.get("status") != "pending" or (snoozed and snoozed > now):
        return None
    created = _aware(draft.get("created_at")) or now
    age_days = (now - created).total_seconds() / 86400
    return PRIORITY_SCORE.get(draft.get("priority", "medium"), 20) + DRAFT_SCORE + min(age_days * 2, 10)


def rank_focus(items: list[Item], drafts: list[dict], now: datetime, limit: int = 5) -> list[FocusEntry]:
    entries = []
    for it in items:
        score = item_score(it, now)
        if score is not None:
            entries.append(FocusEntry("item", it, score))
    for d in drafts:
        score = draft_score(d, now)
        if score is not None:
            entries.append(FocusEntry("draft", d, score))
    entries.sort(key=lambda e: e.score, reverse=True)
    return entries[:limit]
