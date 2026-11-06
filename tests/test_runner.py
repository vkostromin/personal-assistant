from datetime import datetime, timedelta, timezone

import pytest
from google.auth.exceptions import RefreshError

from src import config, runner
from src.llm_client import LLMError
from src.models import Account, ChatSummary, Draft, EmailSummary, Promise, User
from tests.conftest import make_settings


def now():
    return datetime.now(timezone.utc)


@pytest.fixture
def fakes(monkeypatch):
    """Stub Google + LLM calls. Behaviour can be tweaked per account email."""
    calls = {
        "gmail_since": {}, "chat_since": {}, "gmail_fail": {}, "chat_fail": {}, "drafts": [],
        "inbox": {},            # email -> number of new messages (default 1)
        "with_drafts": [],      # with_drafts flag of each analyze_emails call
        "batches": [],          # batch sizes
        "llm_keys": [],         # api key used per LLM call
        "tokens_per_call": 1000,
        "spaces": None,         # override list_spaces result
        "chat_spaces_read": [],
    }

    def fetch_recent_messages(account, client_id, client_secret, last_run, max_results=20):
        calls["gmail_since"][account.email] = last_run
        if account.email in calls["gmail_fail"]:
            raise calls["gmail_fail"][account.email]
        n = calls["inbox"].get(account.email, 1)
        take = min(n, max_results)
        msgs = [{"id": f"m{i}", "thread_id": f"t{i}", "from": "x", "to": account.email,
                 "subject": f"hi {account.email}", "date": "", "body": "?"} for i in range(take)]
        return msgs, n - take

    def create_draft(account, client_id, client_secret, to, subject, body, thread_id=None):
        calls["drafts"].append((account.email, subject))
        return "gmail-draft-id"

    def list_spaces(account, client_id, client_secret):
        if account.email in calls["chat_fail"]:
            raise calls["chat_fail"][account.email]
        if calls["spaces"] is not None:
            return calls["spaces"]
        return [{"name": "spaces/1", "displayName": "Team"}]

    def fetch_messages(account, client_id, client_secret, space, last_run, max_results=30):
        calls["chat_since"][account.email] = last_run
        calls["chat_spaces_read"].append(space["name"])
        return [{"sender": "a", "text": "can you?", "time": "2025-01-01T00:00", "space": "Team"}]

    def spend(llm):
        calls["llm_keys"].append(llm.api_key)
        llm.tokens_used += calls["tokens_per_call"]

    async def analyze_emails(messages, account, llm, signature, with_drafts=True):
        spend(llm)
        calls["with_drafts"].append(with_drafts)
        calls["batches"].append(len(messages))
        t = now()
        return EmailSummary(
            drafts=[Draft(account=account, to="x", subject=f"Re: {account}", body="b", created_at=t)] if with_drafts else [],
            promises=[Promise(text=f"promise from {account}", source="email", created_at=t)],
            summary_items=[f"email item for {account}"],
        )

    async def analyze_chat(messages, space_name, llm):
        spend(llm)
        return ChatSummary(space_name=space_name, missed_questions=["q?"], promises=[], summary_items=["chat item"])

    async def compose_summary(email_summary, chat_summary, promises, date, llm, signature):
        spend(llm)
        return f"SUMMARY\n{email_summary}\n{chat_summary}"

    for name, fn in dict(fetch_recent_messages=fetch_recent_messages, create_draft=create_draft, list_spaces=list_spaces,
                         fetch_messages=fetch_messages, analyze_emails=analyze_emails, analyze_chat=analyze_chat,
                         compose_summary=compose_summary).items():
        monkeypatch.setattr(runner, name, fn)
    return calls


async def add_user(store, uid, emails, **kw):
    await store.save_user(User(id=uid, chat_id=uid, **kw))
    for e in emails:
        await store.link_account(Account(email=e, user_id=uid, refresh_token="t"))


def use(monkeypatch, **kw):
    monkeypatch.setattr(config, "settings", make_settings(**kw))


# ------------------------------------------------------------------ isolation
async def test_each_user_gets_only_their_own_summary(store, outbox, fakes):
    await add_user(store, 1, ["a@x.com"], role="admin")
    await add_user(store, 2, ["b@x.com", "b2@x.com"])

    stats = await runner.run_all_users()
    assert stats == {"users": 2, "skipped": 0, "errors": 0, "deferred": 0}

    [(_, text1, _)] = outbox.to(1)
    [(_, text2, _)] = outbox.to(2)
    assert "a@x.com" in text1 and "b@x.com" not in text1
    assert "b@x.com" in text2 and "b2@x.com" in text2 and "a@x.com" not in text2

    assert [d["account"] for d in await store.get_pending_drafts(1)] == ["a@x.com"]
    assert sorted(d["account"] for d in await store.get_pending_drafts(2)) == ["b2@x.com", "b@x.com"]
    assert [p["text"] for p in await store.get_open_promises(1)] == ["promise from a@x.com"]
    for email in ("a@x.com", "b@x.com"):
        acc = await store.get_account(email)
        assert acc.last_run and set(acc.provider_last_run) == {"gmail", "chat"}


async def test_new_account_looks_back_initial_window_then_uses_watermark(store, fakes, test_settings):
    await add_user(store, 1, ["a@x.com"])
    before = now()
    await runner.run_all_users()
    since = fakes["gmail_since"]["a@x.com"]
    expected = before - timedelta(hours=test_settings.initial_lookback_hours)
    assert abs((since - expected).total_seconds()) < 5

    watermark = (await store.get_account("a@x.com")).provider_last_run["gmail"]
    await runner.run_all_users()
    assert fakes["gmail_since"]["a@x.com"] == watermark


async def test_migrated_account_uses_legacy_last_run(store, fakes):
    await add_user(store, 1, ["a@x.com"])
    legacy = now() - timedelta(hours=3)
    await store.update_account("a@x.com", {"last_run": legacy})
    await runner.run_all_users()
    assert fakes["gmail_since"]["a@x.com"] == legacy


# ------------------------------------------------------------------ reauth
async def test_revoked_token_marks_reauth_and_tells_the_user(store, outbox, fakes):
    await add_user(store, 1, ["a@x.com", "ok@x.com"])
    fakes["gmail_fail"]["a@x.com"] = RefreshError("invalid_grant: Token has been expired or revoked.")
    await runner.run_all_users()

    assert (await store.get_account("a@x.com")).needs_reauth is True
    text = outbox.last(1)[1]
    assert "/connect" in text and "SUMMARY" in text

    fakes["gmail_since"].clear()
    await runner.run_all_users()
    assert "a@x.com" not in fakes["gmail_since"]


async def test_token_from_old_oauth_client_asks_to_reconnect(store, outbox, fakes):
    await add_user(store, 1, ["a@x.com"])
    fakes["gmail_fail"]["a@x.com"] = RefreshError("unauthorized_client: Unauthorized")
    await runner.run_all_users()
    assert (await store.get_account("a@x.com")).needs_reauth is True
    assert "/connect" in outbox.last(1)[1]


async def test_only_revoked_accounts_skip_llm_summary(store, outbox, fakes):
    await add_user(store, 1, ["a@x.com"])
    await store.update_account("a@x.com", {"needs_reauth": True})
    await runner.run_all_users()
    text = outbox.last(1)[1]
    assert "SUMMARY" not in text and "/connect" in text
    assert fakes["llm_keys"] == []


# ------------------------------------------------------------------ robustness
async def test_chat_failure_does_not_reprocess_gmail(store, outbox, fakes):
    await add_user(store, 1, ["a@x.com"])
    fakes["chat_fail"]["a@x.com"] = RuntimeError("chat API 500")
    await runner.run_all_users()

    acc = await store.get_account("a@x.com")
    assert "gmail" in acc.provider_last_run and "chat" not in acc.provider_last_run
    assert "retry" in outbox.last(1)[1]

    del fakes["chat_fail"]["a@x.com"]
    await runner.run_all_users()
    assert fakes["gmail_since"]["a@x.com"] == acc.provider_last_run["gmail"]


async def test_one_user_failure_does_not_stop_others(store, outbox, fakes, monkeypatch):
    await add_user(store, 1, ["a@x.com"])
    await add_user(store, 2, ["b@x.com"])
    original = runner.compose_summary

    async def flaky(**kw):
        if "a@x.com" in kw["email_summary"]:
            raise RuntimeError("boom")
        return await original(**kw)

    monkeypatch.setattr(runner, "compose_summary", flaky)
    stats = await runner.run_all_users()
    assert stats["errors"] == 1 and stats["users"] == 1
    assert outbox.to(2) and not outbox.to(1)


async def test_llm_summary_failure_falls_back_to_plain_digest(store, outbox, fakes, monkeypatch):
    await add_user(store, 1, ["a@x.com"])

    async def down(**kw):
        raise LLMError("LLM API error 503", 503)

    monkeypatch.setattr(runner, "compose_summary", down)
    await runner.run_all_users()
    text = outbox.last(1)[1]
    assert "Summary" in text and "email item for a@x.com" in text


async def test_disabled_and_accountless_users_are_skipped(store, outbox, fakes):
    await add_user(store, 1, [])
    await add_user(store, 2, ["b@x.com"], status="disabled")
    stats = await runner.run_all_users()
    assert stats == {"users": 0, "skipped": 1, "errors": 0, "deferred": 0}
    assert list(outbox) == []


# ------------------------------------------------------------------ limits
async def test_email_limit_per_window_for_shared_key(store, outbox, fakes, test_settings):
    await add_user(store, 2, ["b@x.com", "b2@x.com"])
    fakes["inbox"] = {"b2@x.com": 70, "b@x.com": 60}  # b2 is processed first (sorted)
    await runner.run_all_users()

    assert sum(fakes["batches"]) == 100
    assert max(fakes["batches"]) <= test_settings.email_batch_size
    text = outbox.last(2)[1]
    assert "30 more new email(s) were not analysed" in text and "100 emails per 12 hours" in text
    usage = await store.get_usage(2)
    assert usage["emails"] == 100 and usage["tokens"] > 0


async def test_admin_and_own_key_users_use_technical_cap(store, outbox, fakes, test_settings):
    await add_user(store, 1, ["a@x.com"], role="admin")
    fakes["inbox"] = {"a@x.com": 150}
    await runner.run_all_users()
    assert sum(fakes["batches"]) == 150
    assert "not analysed" not in outbox.last(1)[1]


async def test_drafts_pause_when_token_budget_used_up(store, outbox, fakes, monkeypatch):
    use(monkeypatch, user_token_limit=2500)
    await add_user(store, 2, ["b@x.com"])
    fakes["inbox"] = {"b@x.com": 80}  # 4 batches of 20, 1000 tokens each
    await runner.run_all_users()

    assert fakes["with_drafts"] == [True, True, True, False]
    assert len(fakes["drafts"]) == 3
    assert "Draft replies were paused" in outbox.last(2)[1]
    assert "SUMMARY" in outbox.last(2)[1]  # summary still delivered


async def test_new_run_resets_the_window(store, outbox, fakes, monkeypatch):
    use(monkeypatch, user_token_limit=2500)
    await add_user(store, 2, ["b@x.com"])
    await store.add_usage(2, tokens=10_000)  # exhausted by interactive use
    await runner.run_all_users()
    assert fakes["with_drafts"] == [True]
    usage = await store.get_usage(2)
    assert usage["tokens"] < 10_000


async def test_run_time_budget(store, outbox, fakes, monkeypatch):
    use(monkeypatch, run_time_budget_seconds=0)
    await add_user(store, 1, ["a@x.com"])
    stats = await runner.run_all_users()
    assert stats["deferred"] == 1 and list(outbox) == []

    # deadline passes mid-run: remaining batches are reported, summary still sent
    use(monkeypatch, run_time_budget_seconds=1000)
    fakes["inbox"] = {"a@x.com": 50}
    clock = iter([0.0] + [0.0] * 3 + [5000.0] * 100)
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(clock))
    await runner.run_all_users()
    text = outbox.last(1)[1]
    assert "in time" in text and "not analysed" in text and "SUMMARY" in text
    assert sum(fakes["batches"]) < 50


async def test_only_recently_active_chat_spaces_are_read(store, fakes, monkeypatch):
    use(monkeypatch, max_chat_spaces=2)
    await add_user(store, 1, ["a@x.com"])
    t = now()
    iso = lambda d: d.isoformat().replace("+00:00", "Z")  # noqa: E731
    fakes["spaces"] = [
        {"name": "spaces/old", "lastActiveTime": iso(t - timedelta(days=3))},
        {"name": "spaces/recent", "lastActiveTime": iso(t - timedelta(hours=1))},
        {"name": "spaces/newest", "lastActiveTime": iso(t - timedelta(minutes=5))},
        {"name": "spaces/mid", "lastActiveTime": iso(t - timedelta(hours=2))},
    ]
    await runner.run_all_users()
    assert fakes["chat_spaces_read"] == ["spaces/newest", "spaces/recent"]


# ------------------------------------------------------------------ own keys
async def test_own_key_is_used_for_that_user_only(store, outbox, fakes):
    await add_user(store, 1, ["a@x.com"])
    await add_user(store, 2, ["b@x.com"])
    await store.set_user_llm(2, "openai", "sk-bob", "gpt-4.1-mini")
    await runner.run_all_users()
    keys = set(fakes["llm_keys"])
    assert keys == {"server-gemini-key", "sk-bob"}


async def test_rejected_own_key_is_reported(store, outbox, fakes, monkeypatch):
    await add_user(store, 2, ["b@x.com"])
    await store.set_user_llm(2, "gemini", "bad-key", "")

    async def rejected(**kw):
        raise LLMError("LLM API error 401", 401)

    monkeypatch.setattr(runner, "analyze_emails", rejected)
    monkeypatch.setattr(runner, "analyze_chat", rejected)
    monkeypatch.setattr(runner, "compose_summary", rejected)
    await runner.run_all_users()
    text = outbox.last(2)[1]
    assert "API key was rejected" in text and "/removekey" in text


async def test_no_llm_configured_skips_quietly(store, outbox, fakes, monkeypatch):
    use(monkeypatch, gemini_api_key="")
    await add_user(store, 1, ["a@x.com"])
    stats = await runner.run_all_users()
    assert stats["skipped"] == 1 and list(outbox) == []


async def test_sharding(store, fakes):
    for uid in (10, 11, 12, 13):
        await add_user(store, uid, [f"u{uid}@x.com"])
    s0 = await runner.run_all_users(shard=0, shards=2)
    assert s0["users"] == 2
    assert set(fakes["gmail_since"]) == {"u10@x.com", "u12@x.com"}
