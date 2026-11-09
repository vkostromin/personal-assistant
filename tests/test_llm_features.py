import pytest
from fastapi.testclient import TestClient

from src import config
from src.llm_client import LLMError
from src.main import app
from src.models import Account, User, UserLLMKey
from src.providers import server_llm_config
from tests.conftest import WEBHOOK_HEADERS, make_settings

HEADERS = WEBHOOK_HEADERS
ADMIN, BOB = 1001, 2002


@pytest.fixture
def client():
    return TestClient(app)


def send(client, uid, text, message_id=77):
    body = {"message": {"message_id": message_id, "from": {"id": uid, "first_name": f"U{uid}"},
                        "chat": {"id": uid, "type": "private"}, "text": text}}
    assert client.post("/webhook", json=body, headers=HEADERS).json() == {"ok": True}


@pytest.fixture
async def users(store):
    await store.claim_admin(User(id=ADMIN, chat_id=ADMIN, name="Admin"))
    await store.save_user(User(id=BOB, chat_id=BOB, name="Bob"))
    await store.link_account(Account(email="bob@x.com", user_id=BOB, refresh_token="t"))


@pytest.fixture
def gmail(monkeypatch):
    calls = []

    def fake_update(account, client_id, client_secret, draft_id, to, subject, body, thread_id=None, in_reply_to=None):
        calls.append(dict(account=account.email, draft_id=draft_id, subject=subject, body=body, thread_id=thread_id))
        return (draft_id or "created-id"), draft_id is None

    monkeypatch.setattr("src.bot.update_gmail_draft", fake_update)
    return calls


async def add_draft(store, gmail_draft_id="g-123"):
    data = {"account": "bob@x.com", "to": "boss@x.com", "subject": "Re: plan", "body": "old body",
            "thread_id": "t9", "status": "pending"}
    if gmail_draft_id:
        data["gmail_draft_id"] = gmail_draft_id
    return await store.save_draft(BOB, data)


# ------------------------------------------------------------------ /rewrite
async def test_rewrite_updates_existing_gmail_draft_instead_of_creating(client, outbox, store, users, gmail):
    draft_id = await add_draft(store)
    send(client, BOB, "/rewrite 1 say I'll be late")
    assert gmail == [dict(account="bob@x.com", draft_id="g-123", subject="Rewritten subject",
                          body="Rewritten body", thread_id="t9")]
    [d] = await store.get_pending_drafts(BOB)
    assert d["id"] == draft_id and d["body"] == "Rewritten body" and d["gmail_draft_id"] == "g-123"
    assert "updated in Gmail" in outbox.last(BOB)[1] and "new draft" not in outbox.last(BOB)[1]

    send(client, BOB, "/rewrite 1 more changes")  # second rewrite edits the same Gmail draft again
    assert [c["draft_id"] for c in gmail] == ["g-123", "g-123"]


async def test_rewrite_recreated_draft_id_is_stored(client, outbox, store, users, monkeypatch):
    await add_draft(store, gmail_draft_id="deleted-in-gmail")
    monkeypatch.setattr("src.bot.update_gmail_draft", lambda **kw: ("fresh-id", True))
    send(client, BOB, "/rewrite 1 ctx")
    [d] = await store.get_pending_drafts(BOB)
    assert d["gmail_draft_id"] == "fresh-id"
    assert "new draft was created" in outbox.last(BOB)[1]


async def test_rewrite_gmail_failure_keeps_store_unchanged(client, outbox, store, users, monkeypatch):
    await add_draft(store)
    monkeypatch.setattr("src.bot.update_gmail_draft", lambda **kw: (None, False))
    send(client, BOB, "/rewrite 1 ctx")
    [d] = await store.get_pending_drafts(BOB)
    assert d["body"] == "old body"
    assert "rejected" in outbox.last(BOB)[1]


# ------------------------------------------------------------- usage limits
async def test_interactive_features_pause_when_limit_used(client, outbox, store, users, gmail, fake_llm, monkeypatch):
    monkeypatch.setattr(config, "settings", make_settings(user_token_limit=1500))
    await add_draft(store)
    send(client, BOB, "/chat what's up?")  # 1000 tokens, under the limit
    assert outbox.last(BOB)[1] == "ANSWER"
    send(client, BOB, "/raw some text")  # 2000 tokens, over the limit
    n_calls = len(fake_llm.calls)

    send(client, BOB, "/rewrite 1 ctx")
    assert "AI limit" in outbox.last(BOB)[1] and "/setkey" in outbox.last(BOB)[1]
    send(client, BOB, "/chat again?")
    assert "AI limit" in outbox.last(BOB)[1]
    assert len(fake_llm.calls) == n_calls and gmail == []  # nothing called

    send(client, BOB, "/status")
    assert "limit reached" in outbox.last(BOB)[1]

    # admin uses the same shared key but isn't limited
    send(client, ADMIN, "/chat hi")
    assert outbox.last(ADMIN)[1] == "ANSWER"


async def test_expired_window_lifts_the_limit(client, outbox, store, users, fake_llm, monkeypatch, fake_db):
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(config, "settings", make_settings(user_token_limit=1500))
    await store.reset_usage(BOB, datetime.now(timezone.utc) - timedelta(hours=13), tokens=99_999)
    send(client, BOB, "/chat hi")
    assert outbox.last(BOB)[1] == "ANSWER"
    assert fake_db.data["usage"][str(BOB)]["tokens"] == 1000


# ---------------------------------------------------------------- own keys
async def test_setkey_validates_stores_encrypted_and_deletes_message(client, outbox, store, users, fake_llm, fake_db):
    send(client, BOB, "/setkey gemini AIzaSECRET123 gemini-2.5-flash", message_id=555)
    assert (BOB, 555) in outbox.deleted
    cfg, _ = fake_llm.calls[-1]
    assert cfg.api_key == "AIzaSECRET123" and cfg.model == "gemini-2.5-flash" and cfg.own_key
    text = outbox.last(BOB)[1]
    assert "✅" in text and "AIzaSECRET123" not in text

    stored = fake_db.data["llm_keys"][str(BOB)]
    assert stored["api_key"] != "AIzaSECRET123"  # encrypted at rest
    assert (await store.get_llm_key(BOB)).api_key == "AIzaSECRET123"

    send(client, BOB, "/llm")
    assert "your own" in outbox.last(BOB)[1]
    send(client, BOB, "/removekey")
    assert "deleted" in outbox.last(BOB)[1]
    assert await store.get_llm_key(BOB) is None
    send(client, BOB, "/llm")
    assert "shared assistant" in outbox.last(BOB)[1] and "150k" in outbox.last(BOB)[1]


async def test_setkey_rejected_key_is_not_saved(client, outbox, store, users, fake_llm):
    fake_llm.fail_with = LLMError("401", 401)
    send(client, BOB, "/setkey openai sk-bad", message_id=9)
    assert (BOB, 9) in outbox.deleted  # deleted even if invalid
    assert "rejected" in outbox.last(BOB)[1]
    assert await store.get_llm_key(BOB) is None


async def test_setkey_usage_and_unknown_provider(client, outbox, store, users):
    send(client, BOB, "/setkey")
    assert "Usage" in outbox.last(BOB)[1] and "gemini" in outbox.last(BOB)[1]
    send(client, BOB, "/setkey mistral key123")
    assert "Unknown provider" in outbox.last(BOB)[1]
    send(client, BOB, "/removekey")
    assert "don't have" in outbox.last(BOB)[1]


async def test_own_key_bypasses_limit_in_interactive_commands(client, outbox, store, users, fake_llm, monkeypatch):
    monkeypatch.setattr(config, "settings", make_settings(user_token_limit=1))
    await store.reset_usage(BOB, __import__("src.models", fromlist=["utcnow"]).utcnow(), tokens=10)
    await store.set_llm_key(BOB, UserLLMKey(provider="deepseek", api_key="ds-key"))
    send(client, BOB, "/chat hi")
    assert outbox.last(BOB)[1] == "ANSWER"
    cfg, _ = fake_llm.calls[-1]
    assert cfg.api_key == "ds-key" and cfg.url == "https://api.deepseek.com/chat/completions"


async def test_own_key_error_is_explained(client, outbox, store, users, fake_llm):
    await store.set_llm_key(BOB, UserLLMKey(provider="openai", api_key="sk"))
    fake_llm.fail_with = LLMError("openai API error 429: quota", 429)
    send(client, BOB, "/chat hi")
    assert "Your openai API key" in outbox.last(BOB)[1]


# ---------------------------------------------------------------- /setlimit
async def test_admin_setlimit(client, outbox, store, users):
    send(client, ADMIN, f"/setlimit {BOB} 300k")
    assert (await store.get_user(BOB)).token_limit == 300_000
    send(client, ADMIN, f"/setlimit {BOB} unlimited")
    assert (await store.get_user(BOB)).token_limit == 0
    assert "unlimited" in outbox.last(ADMIN)[1]
    send(client, ADMIN, f"/setlimit {BOB} default")
    assert (await store.get_user(BOB)).token_limit is None
    send(client, ADMIN, f"/setlimit {BOB} lots")
    assert "Usage" in outbox.last(ADMIN)[1]
    send(client, ADMIN, "/users")
    assert "AI 0/150k" in outbox.last(ADMIN)[1]

    send(client, BOB, f"/setlimit {BOB} unlimited")
    assert "Unknown command" in outbox.last(BOB)[1]


# ------------------------------------------------------------ provider order
def test_server_provider_is_first_configured(monkeypatch):
    monkeypatch.setattr(config, "settings", make_settings(gemini_api_key="g", deepseek_api_key="d"))
    cfg = server_llm_config()
    assert cfg.provider == "gemini" and cfg.model == "gemini-2.5-flash-lite" and not cfg.own_key
    assert cfg.url == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

    monkeypatch.setattr(config, "settings", make_settings(gemini_api_key="", deepseek_api_key="d"))
    cfg = server_llm_config()
    assert cfg.provider == "deepseek" and cfg.model == "deepseek-v4-flash"
    assert cfg.url == "https://opencode.ai/zen/go/v1/chat/completions"  # existing deployments keep working

    monkeypatch.setattr(config, "settings", make_settings(gemini_api_key="g", openai_api_key="o", llm_provider_order="openai,gemini"))
    assert server_llm_config().provider == "openai"

    monkeypatch.setattr(config, "settings", make_settings(gemini_api_key=""))
    assert server_llm_config() is None
