from datetime import datetime, timedelta, timezone

import pytest
from google.auth.exceptions import RefreshError

from src import config, runner
from src.llm_client import LLMError
from src.models import Account, User, UserLLMKey
from tests.conftest import make_settings


def now():
    return datetime.now(timezone.utc)


@pytest.fixture
def google(monkeypatch):
    """Stub Gmail / Chat APIs. Behaviour can be tweaked per account email."""
    calls = {"gmail_since": {}, "chat_since": {}, "gmail_fail": {}, "chat_fail": {}, "drafts": []}

    def fetch_recent_messages(account, client_id, client_secret, last_run, max_results=20):
        calls["gmail_since"][account.email] = last_run
        if account.email in calls["gmail_fail"]:
            raise calls["gmail_fail"][account.email]
        return [{"id": "m1", "thread_id": "t1", "from": "x", "to": account.email, "subject": "hi", "date": "", "body": "?"}]

    def create_draft(account, client_id, client_secret, to, subject, body, thread_id=None):
        calls["drafts"].append((account.email, subject))
        return f"gmail-draft-{len(calls['drafts'])}"

    def list_spaces(account, client_id, client_secret):
        if account.email in calls["chat_fail"]:
            raise calls["chat_fail"][account.email]
        return [{"name": "spaces/1", "displayName": "Team"}]

    def fetch_messages(account, client_id, client_secret, space, last_run, max_results=30):
        calls["chat_since"][account.email] = last_run
        return [{"sender": "a", "text": "can you?", "time": "2025-01-01T00:00", "space": "Team"}]

    for name, fn in dict(fetch_recent_messages=fetch_recent_messages, create_draft=create_draft,
                         list_spaces=list_spaces, fetch_messages=fetch_messages).items():
        monkeypatch.setattr(runner, name, fn)
    return calls


async def add_user(store, uid, emails, **kw):
    await store.save_user(User(id=uid, chat_id=uid, **kw))
    for e in emails:
        await store.link_account(Account(email=e, user_id=uid, refresh_token="t"))


async def test_each_user_gets_only_their_own_summary(store, outbox, google):
    await add_user(store, 1, ["a@x.com"], role="admin")
    await add_user(store, 2, ["b@x.com", "b2@x.com"])

    stats = await runner.run_all_users()
    assert stats == {"users": 2, "skipped": 0, "errors": 0, "deferred": 0}

    [(_, text1, _)] = outbox.to(1)
    [(_, text2, _)] = outbox.to(2)
    assert "a@x.com" in text1 and "b@x.com" not in text1
    assert "b@x.com" in text2 and "b2@x.com" in text2 and "a@x.com" not in text2

    drafts1 = await store.get_pending_drafts(1)
    assert [d["account"] for d in drafts1] == ["a@x.com"] and drafts1[0]["gmail_draft_id"]
    assert sorted(d["account"] for d in await store.get_pending_drafts(2)) == ["b2@x.com", "b@x.com"]
    assert [p["text"] for p in await store.get_open_promises(1)] == ["promise from a@x.com"]
    for email in ("a@x.com", "b@x.com"):
        acc = await store.get_account(email)
        assert acc.last_run and set(acc.provider_last_run) == {"gmail", "chat"}


async def test_new_account_looks_back_initial_window_then_uses_watermark(store, google, test_settings):
    await add_user(store, 1, ["a@x.com"])
    before = now()
    await runner.run_all_users()
    since = google["gmail_since"]["a@x.com"]
    expected = before - timedelta(hours=test_settings.initial_lookback_hours)
    assert abs((since - expected).total_seconds()) < 5

    watermark = (await store.get_account("a@x.com")).provider_last_run["gmail"]
    await runner.run_all_users()
    assert google["gmail_since"]["a@x.com"] == watermark


async def test_migrated_account_uses_legacy_last_run(store, google):
    await add_user(store, 1, ["a@x.com"])
    legacy = now() - timedelta(hours=3)
    await store.update_account("a@x.com", {"last_run": legacy})
    await runner.run_all_users()
    assert google["gmail_since"]["a@x.com"] == legacy


async def test_revoked_token_marks_reauth_and_tells_the_user(store, outbox, google):
    await add_user(store, 1, ["a@x.com", "ok@x.com"])
    google["gmail_fail"]["a@x.com"] = RefreshError("invalid_grant: Token has been expired or revoked.")
    await runner.run_all_users()

    assert (await store.get_account("a@x.com")).needs_reauth is True
    text = outbox.last(1)[1]
    assert "/connect" in text and "SUMMARY" in text

    google["gmail_since"].clear()
    await runner.run_all_users()
    assert "a@x.com" not in google["gmail_since"]


async def test_token_from_old_oauth_client_asks_to_reconnect(store, outbox, google):
    await add_user(store, 1, ["a@x.com"])
    google["gmail_fail"]["a@x.com"] = RefreshError("unauthorized_client: Unauthorized")
    await runner.run_all_users()
    assert (await store.get_account("a@x.com")).needs_reauth is True
    assert "/connect" in outbox.last(1)[1]


async def test_only_revoked_accounts_skip_llm_summary(store, outbox, google, fake_llm):
    await add_user(store, 1, ["a@x.com"])
    await store.update_account("a@x.com", {"needs_reauth": True})
    await runner.run_all_users()
    text = outbox.last(1)[1]
    assert "SUMMARY" not in text and "/connect" in text
    assert fake_llm.calls == []


async def test_chat_failure_does_not_reprocess_gmail(store, outbox, google):
    await add_user(store, 1, ["a@x.com"])
    google["chat_fail"]["a@x.com"] = RuntimeError("chat API 500")
    await runner.run_all_users()

    acc = await store.get_account("a@x.com")
    assert "gmail" in acc.provider_last_run and "chat" not in acc.provider_last_run
    assert "retry" in outbox.last(1)[1]

    del google["chat_fail"]["a@x.com"]
    await runner.run_all_users()
    assert google["gmail_since"]["a@x.com"] == acc.provider_last_run["gmail"]


async def test_one_user_failure_does_not_stop_others(store, outbox, google, fake_llm, monkeypatch):
    await add_user(store, 1, ["a@x.com"])
    await add_user(store, 2, ["b@x.com"])
    real = fake_llm.__call__

    async def flaky(cfg, messages, max_tokens=8000):
        prompt = messages[-1]["content"]
        if "Compose a concise daily summary" in prompt and "a@x.com" in prompt:
            raise RuntimeError("unexpected bug")  # not an LLMError: the user's run fails
        return await real(cfg, messages, max_tokens)

    monkeypatch.setattr("src.llm_client.chat_completion", flaky)
    stats = await runner.run_all_users()
    assert stats["errors"] == 1 and stats["users"] == 1
    assert outbox.to(2) and not outbox.to(1)


async def test_disabled_and_accountless_users_are_skipped(store, outbox, google):
    await add_user(store, 1, [])
    await add_user(store, 2, ["b@x.com"], status="disabled")
    stats = await runner.run_all_users()
    assert stats == {"users": 0, "skipped": 1, "errors": 0, "deferred": 0}
    assert list(outbox) == []


# ------------------------------------------------------------------ LLM limits
async def test_usage_is_counted_and_window_restarts_every_run(store, google, fake_llm):
    await add_user(store, 1, ["a@x.com"])
    await runner.run_all_users()
    # gmail analysis + 1 chat space + summary = 3 calls x 1000 tokens
    assert (await store.get_usage(1))["tokens"] == 3000
    await runner.run_all_users()
    assert (await store.get_usage(1))["tokens"] == 3000  # fresh window, not 6000


async def test_limit_reached_mid_run_pauses_analysis_but_digest_is_sent(store, outbox, google, fake_llm, monkeypatch):
    monkeypatch.setattr(config, "settings", make_settings(user_token_limit=2500))
    await add_user(store, 1, ["a@x.com", "b@x.com", "c@x.com"])
    await runner.run_all_users()

    # a: gmail(1000)+chat(1000); b: gmail(3000) -> chat blocked; c: gmail blocked
    assert len(fake_llm.prompts("Analyze these emails")) == 2
    assert len(fake_llm.prompts("Google Chat messages")) == 1
    text = outbox.last(1)[1]
    assert text.startswith("SUMMARY") and "AI limit reached" in text and "/setkey" in text

    a, b, c = [await store.get_account(e) for e in ("a@x.com", "b@x.com", "c@x.com")]
    assert set(a.provider_last_run) == {"gmail", "chat"}
    assert set(b.provider_last_run) == {"gmail"}  # chat left for next run
    assert c.provider_last_run == {}  # nothing lost, nothing drafted
    assert [d["account"] for d in await store.get_pending_drafts(1)] == ["a@x.com", "b@x.com"]


async def test_admin_is_not_limited(store, outbox, google, fake_llm, monkeypatch):
    monkeypatch.setattr(config, "settings", make_settings(user_token_limit=1))
    await add_user(store, 1, ["a@x.com", "b@x.com"], role="admin")
    await runner.run_all_users()
    assert "AI limit" not in outbox.last(1)[1]
    assert len(fake_llm.prompts("Analyze these emails")) == 2


async def test_per_user_limit_override(store, outbox, google, fake_llm, monkeypatch):
    monkeypatch.setattr(config, "settings", make_settings(user_token_limit=1))
    await add_user(store, 1, ["a@x.com"], token_limit=0)  # unlimited
    await add_user(store, 2, ["b@x.com"], token_limit=100_000)
    await runner.run_all_users()
    assert "AI limit" not in outbox.last(1)[1] and "AI limit" not in outbox.last(2)[1]


async def test_own_key_is_used_without_limits(store, outbox, google, fake_llm, monkeypatch):
    monkeypatch.setattr(config, "settings", make_settings(user_token_limit=1))
    await add_user(store, 1, ["a@x.com"])
    await store.set_llm_key(1, UserLLMKey(provider="openai", api_key="sk-user", model="gpt-4o-mini"))
    await runner.run_all_users()

    assert "AI limit" not in outbox.last(1)[1]
    cfgs = {cfg for cfg, _ in fake_llm.calls}
    assert len(cfgs) == 1
    cfg = cfgs.pop()
    assert cfg.api_key == "sk-user" and cfg.model == "gpt-4o-mini" and cfg.url.startswith("https://api.openai.com/")
    usage = await store.get_usage(1)
    assert usage["tokens"] == 0 and usage["own_tokens"] == 3000


async def test_own_key_failure_is_reported_with_plain_digest(store, outbox, google, fake_llm):
    await add_user(store, 1, ["a@x.com"])
    await store.set_llm_key(1, UserLLMKey(provider="gemini", api_key="bad"))
    fake_llm.fail_with = LLMError("gemini API error 401: invalid key", 401)
    await runner.run_all_users()
    text = outbox.last(1)[1]
    assert text.startswith("📋 Summary")  # fallback digest without LLM
    assert "Your own gemini API key failed" in text and "/removekey" in text
    assert "provider_last_run" not in (await store.get_account("a@x.com")).model_dump(exclude_defaults=True)


async def test_no_llm_configured_skips_users(store, outbox, google, monkeypatch):
    monkeypatch.setattr(config, "settings", make_settings(gemini_api_key=""))
    await add_user(store, 1, ["a@x.com"])
    stats = await runner.run_all_users()
    assert stats["skipped"] == 1 and not outbox.to(1)


async def test_soft_deadline_defers_users_without_losing_mail(store, outbox, google, monkeypatch):
    monkeypatch.setattr(config, "settings", make_settings(run_soft_deadline_seconds=-1))
    await add_user(store, 1, ["a@x.com"])
    stats = await runner.run_all_users()
    assert stats["deferred"] == 1 and not outbox.to(1)
    assert (await store.get_account("a@x.com")).provider_last_run == {}
