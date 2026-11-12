from datetime import datetime, timedelta, timezone

import pytest

from src import config, runner
from src.models import Account, User
from src.schedule import current_slot, due_slot, next_slot, parse_timezone, tz_of
from tests.conftest import make_settings

UTC = timezone.utc


def at(*args):
    return datetime(*args, tzinfo=UTC)


@pytest.mark.parametrize("text,expected", [
    ("Europe/Berlin", "Europe/Berlin"),
    ("jakarta", "Asia/Jakarta"),
    ("new york", "America/New_York"),
    ("utc", "UTC"),
    ("+7", "+07:00"),
    ("UTC+5:30", "+05:30"),
    ("GMT-3", "-03:00"),
    ("Mars/Olympus", None),
    ("+15", None),
    ("", None),
])
def test_parse_timezone(text, expected):
    assert parse_timezone(text) == expected


def test_slots_in_user_timezone():
    jakarta = tz_of("Asia/Jakarta")  # UTC+7
    assert current_slot(at(2025, 1, 10, 1, 0), jakarta) == at(2025, 1, 10, 1, 0)  # 08:00 local
    assert current_slot(at(2025, 1, 10, 0, 59), jakarta) == at(2025, 1, 9, 13, 0)  # 20:00 local yesterday
    assert next_slot(at(2025, 1, 10, 1, 0), jakarta) == at(2025, 1, 10, 13, 0)


def test_slots_follow_daylight_saving():
    berlin = tz_of("Europe/Berlin")
    assert current_slot(at(2025, 1, 15, 7, 30), berlin) == at(2025, 1, 15, 7, 0)  # CET: 08:00 = 07:00 UTC
    assert current_slot(at(2025, 7, 15, 6, 30), berlin) == at(2025, 7, 15, 6, 0)  # CEST: 08:00 = 06:00 UTC


def test_half_hour_offset_is_picked_up_by_hourly_scheduler():
    user = User(id=1, chat_id=1, timezone="+05:30")
    # scheduler fires at 03:00 UTC = 08:30 local
    assert due_slot(user, at(2025, 1, 10, 3, 0)) == at(2025, 1, 10, 2, 30)


def test_due_rules():
    user = User(id=1, chat_id=1, timezone="Asia/Jakarta")
    slot = at(2025, 1, 10, 1, 0)  # 08:00 Jakarta
    assert due_slot(user, slot) == slot  # new user, slot hour
    assert due_slot(user, slot + timedelta(hours=5)) is None  # new user, long after the slot
    done = user.model_copy(update={"last_run_slot": slot})
    assert due_slot(done, slot + timedelta(minutes=30)) is None  # already processed
    missed = user.model_copy(update={"last_run_slot": slot - timedelta(hours=12)})
    assert due_slot(missed, slot + timedelta(hours=5)) == slot  # caught up


def test_default_timezone_setting(monkeypatch):
    monkeypatch.setattr(config, "settings", make_settings(default_timezone="Asia/Jakarta"))
    user = User(id=1, chat_id=1)
    assert due_slot(user, at(2025, 1, 10, 1, 0)) == at(2025, 1, 10, 1, 0)


# ---------------------------------------------------------------- runner
@pytest.fixture
def quiet_google(monkeypatch):
    monkeypatch.setattr(runner, "list_message_ids", lambda **kw: ([], False))  # empty mailbox
    monkeypatch.setattr(runner, "list_spaces", lambda **kw: [])


async def add(store, uid, tz):
    await store.save_user(User(id=uid, chat_id=uid, timezone=tz))
    await store.link_account(Account(email=f"u{uid}@x.com", user_id=uid, refresh_token="t"))


async def test_users_run_at_their_own_local_times(store, outbox, quiet_google):
    await add(store, 1, "Asia/Jakarta")   # 08:00 local = 01:00 UTC
    await add(store, 2, "Europe/Berlin")  # 08:00 local = 07:00 UTC (winter)

    stats = await runner.run_all_users(now=at(2025, 1, 10, 1, 0))
    assert stats["users"] == 1 and stats["not_due"] == 1
    assert outbox.to(1) and not outbox.to(2)

    # the next hourly call doesn't repeat Jakarta's summary
    stats = await runner.run_all_users(now=at(2025, 1, 10, 2, 0))
    assert stats["users"] == 0 and len(outbox.to(1)) == 1

    await runner.run_all_users(now=at(2025, 1, 10, 7, 0))
    assert outbox.to(2)
    await runner.run_all_users(now=at(2025, 1, 10, 13, 0))  # 20:00 Jakarta
    assert len(outbox.to(1)) == 2


async def test_deferred_user_is_caught_up_next_hour(store, outbox, quiet_google, monkeypatch):
    await add(store, 1, "Asia/Jakarta")
    monkeypatch.setattr(config, "settings", make_settings(run_soft_deadline_seconds=-1))
    stats = await runner.run_all_users(now=at(2025, 1, 10, 1, 0))
    assert stats["deferred"] == 1 and not outbox.to(1)

    monkeypatch.setattr(config, "settings", make_settings())
    stats = await runner.run_all_users(now=at(2025, 1, 10, 2, 0))
    assert stats["users"] == 1 and outbox.to(1)


async def test_overlapping_runs_process_a_user_once(store, outbox, quiet_google):
    import asyncio
    await add(store, 1, "Asia/Jakarta")
    now = at(2025, 1, 10, 1, 0)
    await asyncio.gather(runner.run_all_users(now=now), runner.run_all_users(now=now))
    assert len(outbox.to(1)) == 1


async def test_force_runs_everyone(store, outbox, quiet_google):
    await add(store, 1, "Asia/Jakarta")
    await add(store, 2, "Europe/Berlin")
    stats = await runner.run_all_users(now=at(2025, 1, 10, 4, 0), force=True)
    assert stats["users"] == 2


async def test_summary_date_is_local(store, outbox, quiet_google, fake_llm):
    await add(store, 1, "Pacific/Kiritimati")  # UTC+14
    await runner.run_all_users(now=at(2025, 1, 9, 18, 0))  # 08:00 on Jan 10 local
    [prompt] = fake_llm.prompts("Compose a concise summary")
    assert "Jan 10, 2025" in prompt
