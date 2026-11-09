import json
from datetime import datetime, timedelta, timezone

import pytest

from src import config, runner
from src.models import Account, User, UserLLMKey
from tests.conftest import make_settings

UTC = timezone.utc


@pytest.fixture
def no_chat(monkeypatch):
    monkeypatch.setattr(runner, "create_draft", lambda **kw: "g")
    monkeypatch.setattr(runner, "list_spaces", lambda **kw: [])


@pytest.fixture
def quiet_llm(fake_llm):
    """Email analysis returns nothing to store; we only care which emails were sent to the LLM."""
    fake_llm.canned["Analyze these emails received by"] = json.dumps({"drafts": [], "items": [], "updates": []})
    return fake_llm


def analyzed_ids(fake_llm):
    ids = []
    for prompt in fake_llm.prompts("Analyze these emails received by"):
        ids += [line.split(": ", 1)[1] for line in prompt.splitlines() if line.startswith("Message ID: ")]
    return ids


async def add_user(store, uid=1, email="a@x.com", **account_fields):
    await store.save_user(User(id=uid, chat_id=uid))
    await store.link_account(Account(email=email, user_id=uid, refresh_token="t", **account_fields))


async def test_first_run_last_week_capped_at_200_oldest_first(store, outbox, mailbox, quiet_llm, no_chat):
    now = datetime.now(UTC)
    mailbox.add("a@x.com", at=now - timedelta(days=9), count=30)  # older than a week: ignored
    week = []
    for n in range(250):
        week += mailbox.add("a@x.com", at=now - timedelta(days=6) + timedelta(minutes=n))
    await add_user(store)
    await runner.run_all_users(force=True)

    ids = analyzed_ids(quiet_llm)
    assert ids == [m["id"] for m in week[-200:]]  # newest 200, processed oldest first
    assert len(quiet_llm.prompts("Analyze these emails received by")) == 10  # batches of 20
    assert mailbox.list_calls[0]["since"] < now - timedelta(days=6, hours=23)
    assert "more than 200 new emails" in outbox.text(1) and "/setkey" in outbox.text(1)


async def test_later_runs_capped_at_100(store, outbox, mailbox, quiet_llm, no_chat):
    now = datetime.now(UTC)
    await add_user(store, provider_last_run={"gmail": now - timedelta(hours=12)}, last_run=now - timedelta(hours=12))
    new = []
    for n in range(130):
        new += mailbox.add("a@x.com", at=now - timedelta(hours=11) + timedelta(minutes=n))
    await runner.run_all_users(force=True)
    assert analyzed_ids(quiet_llm) == [m["id"] for m in new[-100:]]
    assert "more than 100 new emails" in outbox.text(1)


async def test_own_key_has_no_count_limit(store, outbox, mailbox, quiet_llm, no_chat):
    now = datetime.now(UTC)
    await add_user(store)
    await store.set_llm_key(1, UserLLMKey(provider="gemini", api_key="k"))
    msgs = []
    for n in range(300):
        msgs += mailbox.add("a@x.com", at=now - timedelta(days=5) + timedelta(minutes=n))
    await runner.run_all_users(force=True)
    assert analyzed_ids(quiet_llm) == [m["id"] for m in msgs]
    assert mailbox.list_calls[0]["cap"] is None
    assert "more than" not in outbox.text(1)


async def test_deadline_leaves_backlog_that_continues_quietly(store, outbox, mailbox, quiet_llm, no_chat, monkeypatch):
    now = datetime.now(UTC)
    await add_user(store)
    await store.set_llm_key(1, UserLLMKey(provider="gemini", api_key="k"))
    msgs = []
    for n in range(100):
        msgs += mailbox.add("a@x.com", at=now - timedelta(days=2) + timedelta(minutes=n))

    clock = {"t": 0.0}

    class FakeTime:
        @staticmethod
        def monotonic():
            return clock["t"]

    real_get = mailbox.get_messages

    def slow_get(*a, **kw):
        clock["t"] += 100  # every batch "takes" 100s; soft deadline is 240s
        return real_get(*a, **kw)

    monkeypatch.setattr(runner, "time", FakeTime)
    monkeypatch.setattr(runner, "get_messages", slow_get)
    await runner.run_all_users(force=True)

    assert analyzed_ids(quiet_llm) == [m["id"] for m in msgs[:60]]  # 3 batches before the deadline
    acc = await store.get_account("a@x.com")
    assert acc.backlog is True
    assert acc.provider_last_run["gmail"] == datetime.fromtimestamp(msgs[59]["internal_ms"] / 1000, tz=UTC)
    assert "Still working through" in outbox.text(1)
    summaries = len(outbox.to(1))

    # next hourly run: not a summary slot, but the backlog is continued without a new summary
    clock["t"] = 0.0
    monkeypatch.setattr(runner, "get_messages", real_get)
    stats = await runner.run_all_users(now=now + timedelta(hours=1))
    assert stats["backlog"] == 1
    assert analyzed_ids(quiet_llm) == [m["id"] for m in msgs]  # nothing twice, nothing lost
    assert len(outbox.to(1)) == summaries
    assert (await store.get_account("a@x.com")).backlog is False


async def test_ai_limit_mid_mailbox_resumes_from_last_batch(store, outbox, mailbox, quiet_llm, no_chat, monkeypatch):
    monkeypatch.setattr(config, "settings", make_settings(user_token_limit=2500))
    now = datetime.now(UTC)
    await add_user(store)
    msgs = []
    for n in range(100):
        msgs += mailbox.add("a@x.com", at=now - timedelta(days=1) + timedelta(minutes=n))
    await runner.run_all_users(force=True)
    assert analyzed_ids(quiet_llm) == [m["id"] for m in msgs[:60]]  # 3 calls x 1000 tokens, then limit
    assert "AI limit reached" in outbox.text(1)

    await runner.run_all_users(force=True)  # fresh window at the next summary
    assert analyzed_ids(quiet_llm)[60:120] == [m["id"] for m in msgs[60:]]


async def test_same_timestamp_at_batch_boundary(store, mailbox, quiet_llm, no_chat, monkeypatch):
    monkeypatch.setattr(config, "settings", make_settings(email_batch_size=3))
    now = datetime.now(UTC)
    await add_user(store)
    same = mailbox.add("a@x.com", at=now - timedelta(hours=1), count=5)  # identical internalDate
    await runner.run_all_users(force=True)
    later = mailbox.add("a@x.com", at=now - timedelta(minutes=1), count=2)
    await runner.run_all_users(force=True)
    assert sorted(analyzed_ids(quiet_llm)) == sorted(m["id"] for m in same + later)
    assert len(analyzed_ids(quiet_llm)) == 7


async def test_cap_holds_when_boundary_ids_are_excluded(store, outbox, mailbox, quiet_llm, no_chat):
    now = datetime.now(UTC)
    boundary = now - timedelta(hours=12)
    await add_user(store, provider_last_run={"gmail": boundary}, last_run=boundary,
                   provider_seen_ids={"gmail": ["gone1", "gone2"]})  # seen ids no longer in the window
    new = []
    for n in range(110):
        new += mailbox.add("a@x.com", at=now - timedelta(hours=11) + timedelta(minutes=n))
    await runner.run_all_users(force=True)
    assert mailbox.list_calls[0]["cap"] == 102  # asks for 2 extra to make room for the seen ids
    assert analyzed_ids(quiet_llm) == [m["id"] for m in new[-100:]]  # but still at most 100
