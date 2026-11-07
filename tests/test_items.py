from datetime import datetime, timedelta, timezone

import pytest

from src.items import (
    after_reminder, due_choice, fingerprint, in_quiet_hours, next_reminder, out_of_quiet, parse_due, rank_focus,
    snooze_until,
)
from src.models import Item
from src.schedule import tz_of

UTC = timezone.utc
JKT = tz_of("Asia/Jakarta")  # UTC+7, quiet hours 21:00-08:00 local = 14:00-01:00 UTC


def at(*a):
    return datetime(*a, tzinfo=UTC)


def item(**kw):
    base = dict(user_id=1, kind="promise", title="Send report", created_at=at(2025, 1, 1))
    base.update(kw)
    return Item(**base)


def step(it: Item, now: datetime) -> Item:
    """Simulate sending a reminder at ``now``."""
    return it.model_copy(update=after_reminder(it, now, JKT))


# ------------------------------------------------------------------ parse_due
def test_parse_due_variants():
    assert parse_due("2025-01-10T15:00:00+07:00", JKT) == at(2025, 1, 10, 8)
    assert parse_due("2025-01-10T15:00:00Z", JKT) == at(2025, 1, 10, 15)
    assert parse_due("2025-01-10T15:00:00", JKT) == at(2025, 1, 10, 8)  # naive = user's local time
    assert parse_due("2025-01-10", JKT) == at(2025, 1, 10, 11)  # date only = 18:00 local
    for bad in (None, "", "null", "next week-ish", 42):
        assert parse_due(bad, JKT) is None


# ------------------------------------------------------------------ reminders
def test_promise_reminder_lifecycle():
    due = at(2025, 1, 10, 10)  # 17:00 Jakarta
    it = item(due_at=due)
    now = at(2025, 1, 9, 2)
    assert next_reminder(it, now, JKT) == due - timedelta(hours=2)  # heads-up

    it = step(it, due - timedelta(hours=2))
    assert it.remind_stage == 1 and it.next_remind_at == due  # "now overdue" at due time

    it = step(it, due)
    assert it.remind_stage == 2
    assert it.next_remind_at == at(2025, 1, 11, 2)  # daily 09:00 Jakarta

    for expected_day in (12, 13):
        it = step(it, it.next_remind_at)
        assert it.next_remind_at == at(2025, 1, expected_day, 2)
    it = step(it, it.next_remind_at)
    assert it.next_remind_at is None  # gave up; stays in Focus / summaries


def test_waiting_reminds_only_when_late():
    due = at(2025, 1, 10, 5)
    it = item(kind="waiting", due_at=due)
    assert next_reminder(it, at(2025, 1, 9), JKT) == due  # no heads-up
    it = step(it, due)
    assert it.remind_stage == 2 and it.next_remind_at == at(2025, 1, 11, 2)


def test_overdue_at_creation_reminds_now_and_no_due_no_reminder():
    now = at(2025, 1, 10, 5)
    assert next_reminder(item(due_at=now - timedelta(days=1)), now, JKT) == now
    assert next_reminder(item(), now, JKT) is None
    assert next_reminder(item(due_at=now, status="done"), now, JKT) is None


def test_quiet_hours():
    assert in_quiet_hours(at(2025, 1, 10, 15), JKT)  # 22:00 local
    assert not in_quiet_hours(at(2025, 1, 10, 5), JKT)  # 12:00 local
    assert out_of_quiet(at(2025, 1, 10, 15), JKT) == at(2025, 1, 11, 1)  # → 08:00 local next day
    assert out_of_quiet(at(2025, 1, 10, 23), JKT) == at(2025, 1, 11, 1)  # 06:00 local → 08:00 same day
    # a due time at night is reminded at 08:00
    due = at(2025, 1, 10, 16)  # 23:00 local
    assert next_reminder(item(kind="waiting", due_at=due), at(2025, 1, 10), JKT) == at(2025, 1, 11, 1)


def test_snooze_then_policy_continues():
    now = at(2025, 1, 10, 5)
    it = item(due_at=now - timedelta(hours=1), remind_stage=2, snoozed_until=now + timedelta(hours=4))
    assert next_reminder(it, now, JKT) == now + timedelta(hours=4)
    it = step(it, now + timedelta(hours=4))
    assert it.snoozed_until is None and it.remind_stage == 2  # snooze end doesn't consume an overdue reminder
    assert it.next_remind_at == at(2025, 1, 11, 2)


def test_snooze_and_due_choices():
    now = at(2025, 1, 10, 5)  # Friday 12:00 Jakarta
    assert snooze_until("4", now, JKT) == now + timedelta(hours=4)
    assert snooze_until("tm", now, JKT) == at(2025, 1, 11, 2)  # tomorrow 09:00
    assert due_choice("today", now, JKT) == at(2025, 1, 10, 11)  # 18:00 today
    assert due_choice("tm", now, JKT) == at(2025, 1, 11, 11)
    assert due_choice("mon", now, JKT) == at(2025, 1, 13, 11)
    assert due_choice("none", now, JKT) is None
    late = at(2025, 1, 10, 12)  # 19:00 local: "today 18:00" already passed
    assert due_choice("today", late, JKT) == late + timedelta(hours=2)


def test_fingerprint_normalizes():
    assert fingerprint("promise", "Send the report!", "Alex") == fingerprint("promise", "send the  report", "alex")
    assert fingerprint("promise", "Send report", "Alex") != fingerprint("waiting", "Send report", "Alex")


# ---------------------------------------------------------------------- focus
def test_focus_ranking():
    now = at(2025, 1, 10, 5)
    overdue_promise = item(id="a", due_at=now - timedelta(days=1), priority="medium")
    due_today = item(id="b", kind="todo", due_at=now + timedelta(hours=5), priority="medium")
    low_no_due = item(id="c", kind="todo", priority="low", created_at=now)
    waiting_not_late = item(id="d", kind="waiting", due_at=now + timedelta(days=1), priority="high")
    waiting_late = item(id="e", kind="waiting", due_at=now - timedelta(hours=3), priority="medium")
    snoozed = item(id="f", kind="reply", priority="high", snoozed_until=now + timedelta(hours=2))
    draft = {"id": "d1", "status": "pending", "priority": "high", "created_at": now}

    ranked = rank_focus([low_no_due, waiting_not_late, due_today, overdue_promise, waiting_late, snoozed], [draft], now)
    order = [e.ref.id if e.type == "item" else e.ref["id"] for e in ranked]
    assert order[0] == "a"  # overdue promise first
    assert "d" not in order and "f" not in order  # not late yet / snoozed
    assert order.index("d1") < order.index("c")  # high-priority ready draft beats low to-do
    assert order[-1] == "c"
    assert len(rank_focus([low_no_due] * 10, [], now, limit=5)) == 5
