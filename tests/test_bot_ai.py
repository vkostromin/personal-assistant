"""/rewrite, /setkey, /removekey, /usage and the per-user AI limit."""
import pytest
from fastapi.testclient import TestClient

from src import bot, config
from src.gmail_client import DraftGone
from src.main import app
from src.models import Account, User
from tests.conftest import make_settings

HEADERS = {"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"}
ADMIN, BOB = 1001, 2002


@pytest.fixture
def client():
    return TestClient(app)


def send(client, uid, text, message_id=55):
    body = {"message": {"message_id": message_id, "from": {"id": uid, "first_name": "U"},
                        "chat": {"id": uid, "type": "private"}, "text": text}}
    assert client.post("/webhook", json=body, headers=HEADERS).json() == {"ok": True}


@pytest.fixture
async def users(store):
    await store.claim_admin(User(id=ADMIN, chat_id=ADMIN))
    await store.save_user(User(id=BOB, chat_id=BOB, name="Bob"))
    await store.link_account(Account(email="bob@x.com", user_id=BOB, refresh_token="t"))


@pytest.fixture
def llm_calls(monkeypatch):
    """Fake LLM functions used by the bot; each call spends 1000 tokens."""
    calls = []

    async def rewrite_draft(to, subject, body, context, llm, signature):
        calls.append(("rewrite", llm.api_key))
        llm.tokens_used += 1000
        return {"subject": subject, "body": f"{body} + {context}"}

    async def answer_question(question, drafts_summary, promises_summary, last_activity, llm, signature):
        calls.append(("answer", llm.api_key))
        llm.tokens_used += 1000
        return "answer"

    monkeypatch.setattr(bot, "rewrite_draft", rewrite_draft)
    monkeypatch.setattr(bot, "answer_question", answer_question)
    return calls


@pytest.fixture
def gmail(monkeypatch):
    calls = {"update": [], "create": [], "update_raises": None}

    def update_draft(account, client_id, client_secret, draft_id, to, subject, body, thread_id=None):
        calls["update"].append((draft_id, body, thread_id))
        if calls["update_raises"]:
            raise calls["update_raises"]
        return draft_id

    def create_draft(account, client_id, client_secret, to, subject, body, thread_id=None):
        calls["create"].append((body, thread_id))
        return "new-gmail-id"

    monkeypatch.setattr(bot, "update_draft", update_draft)
    monkeypatch.setattr(bot, "create_draft", create_draft)
    return calls


async def bob_draft(store, **extra):
    data = {"account": "bob@x.com", "to": "boss@x.com", "subject": "Re: plan", "body": "Hi", "thread_id": "th1", "status": "pending"}
    data.update(extra)
    return await store.save_draft(BOB, data)


# --------------------------------------------------------------------- /rewrite
async def test_rewrite_updates_existing_gmail_draft_in_place(client, outbox, store, users, llm_calls, gmail):
    draft_id = await bob_draft(store, gmail_draft_id="g-123")
    send(client, BOB, "/rewrite 1 say yes")
    assert gmail["update"] == [("g-123", "Hi + say yes", "th1")]
    assert gmail["create"] == []  # no duplicate draft
    assert "updated in Gmail" in outbox.last(BOB)[1]
    [d] = await store.get_pending_drafts(BOB)
    assert d["id"] == draft_id and d["body"] == "Hi + say yes" and d["gmail_draft_id"] == "g-123"

    send(client, BOB, "/rewrite 1 and thanks")  # second rewrite builds on the first, same draft
    assert gmail["update"][-1] == ("g-123", "Hi + say yes + and thanks", "th1")
    assert gmail["create"] == []


async def test_rewrite_recreates_draft_deleted_in_gmail(client, outbox, store, users, llm_calls, gmail):
    await bob_draft(store, gmail_draft_id="g-gone")
    gmail["update_raises"] = DraftGone("g-gone")
    send(client, BOB, "/rewrite 1 say yes")
    assert gmail["create"] == [("Hi + say yes", "th1")]
    assert "no longer in Gmail" in outbox.last(BOB)[1]
    [d] = await store.get_pending_drafts(BOB)
    assert d["gmail_draft_id"] == "new-gmail-id"


async def test_rewrite_without_gmail_id_creates_once_then_updates(client, outbox, store, users, llm_calls, gmail):
    await bob_draft(store)  # legacy draft: Gmail creation had failed
    send(client, BOB, "/rewrite 1 a")
    send(client, BOB, "/rewrite 1 b")
    assert len(gmail["create"]) == 1
    assert gmail["update"] == [("new-gmail-id", "Hi + a + b", "th1")]


async def test_rewrite_gmail_failure_keeps_stored_draft(client, outbox, store, users, llm_calls, gmail, monkeypatch):
    await bob_draft(store)
    monkeypatch.setattr(bot, "create_draft", lambda **kw: None)
    send(client, BOB, "/rewrite 1 a")
    assert "Could not save" in outbox.last(BOB)[1]
    [d] = await store.get_pending_drafts(BOB)
    assert d["body"] == "Hi"


# --------------------------------------------------------------------- limits
async def test_interactive_commands_count_and_stop_at_limit(client, outbox, store, users, llm_calls, gmail, monkeypatch):
    monkeypatch.setattr(config, "settings", make_settings(user_token_limit=2000))
    await bob_draft(store, gmail_draft_id="g1")
    send(client, BOB, "/chat what's up?")
    send(client, BOB, "/raw some text")
    assert (await store.get_usage(BOB))["tokens"] == 2000
    assert len(llm_calls) == 2

    send(client, BOB, "/chat again?")
    send(client, BOB, "/rewrite 1 x")
    send(client, BOB, "/raw more")
    assert len(llm_calls) == 2  # all blocked
    assert gmail["update"] == []
    assert "/setkey" in outbox.last(BOB)[1] and "paused" in outbox.last(BOB)[1]

    send(client, BOB, "/usage")
    text = outbox.last(BOB)[1]
    assert "2,000 / 2,000" in text and "Limit reached" in text

    # admin on the shared key is not limited
    await store.add_usage(ADMIN, tokens=10**6)
    send(client, ADMIN, "/chat hi")
    assert llm_calls[-1] == ("answer", "server-gemini-key")


async def test_limit_applies_to_admin_when_configured(client, outbox, store, users, llm_calls, monkeypatch):
    monkeypatch.setattr(config, "settings", make_settings(user_token_limit=1000, limit_admins=True))
    await store.add_usage(ADMIN, tokens=1000)
    send(client, ADMIN, "/chat hi")
    assert llm_calls == [] and "paused" in outbox.last(ADMIN)[1]


async def test_window_expiry_restores_access(client, outbox, store, users, llm_calls, fake_db, monkeypatch):
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(config, "settings", make_settings(user_token_limit=1000))
    await store.add_usage(BOB, tokens=5000)
    fake_db.data["usage"][str(BOB)]["window_start"] = datetime.now(timezone.utc) - timedelta(hours=13)
    send(client, BOB, "/chat hi")
    assert len(llm_calls) == 1


# --------------------------------------------------------------------- own key
@pytest.fixture
def key_check(monkeypatch):
    result = {"value": (True, ""), "checked": []}

    async def check_key(llm):
        result["checked"].append((llm.provider, llm.api_key, llm.model))
        return result["value"]

    monkeypatch.setattr(bot, "check_key", check_key)
    return result


async def test_setkey_validates_stores_encrypted_and_deletes_message(client, outbox, store, users, key_check, llm_calls, fake_db, monkeypatch):
    monkeypatch.setattr(config, "settings", make_settings(user_token_limit=1000))
    await store.add_usage(BOB, tokens=5000)  # over the shared limit

    send(client, BOB, "/setkey gemini AIzaSECRET gemini-2.5-flash", message_id=77)
    assert (BOB, 77) in outbox.deleted
    assert key_check["checked"] == [("gemini", "AIzaSECRET", "gemini-2.5-flash")]
    assert "Saved" in outbox.last(BOB)[1]
    assert "AIzaSECRET" not in str(fake_db.data["llm_keys"])  # encrypted at rest
    assert (await store.get_user_llm(BOB))["api_key"] == "AIzaSECRET"

    send(client, BOB, "/chat hi")  # own key: no limit, uses Bob's key
    assert llm_calls[-1] == ("answer", "AIzaSECRET")
    send(client, BOB, "/usage")
    assert "your own key" in outbox.last(BOB)[1] and "No AI limits" in outbox.last(BOB)[1]

    send(client, BOB, "/removekey")
    assert "deleted" in outbox.last(BOB)[1]
    assert await store.get_user_llm(BOB) is None
    send(client, BOB, "/chat hi")
    assert "paused" in outbox.last(BOB)[1]  # back to the shared, exhausted limit
    send(client, BOB, "/removekey")
    assert "don't have" in outbox.last(BOB)[1]


async def test_setkey_default_model_and_rejections(client, outbox, store, users, key_check):
    send(client, BOB, "/setkey openai sk-abc")
    assert key_check["checked"][-1] == ("openai", "sk-abc", "gpt-4.1-mini")

    send(client, BOB, "/setkey mistral k", message_id=9)
    assert "Unknown provider" in outbox.last(BOB)[1] and (BOB, 9) in outbox.deleted

    key_check["value"] = (False, "the key was rejected")
    send(client, BOB, "/setkey gemini bad")
    assert "not saved" in outbox.last(BOB)[1]
    assert (await store.get_user_llm(BOB))["api_key"] == "sk-abc"  # previous key kept

    send(client, BOB, "/setkey")
    text = outbox.last(BOB)[1]
    assert "gemini" in text and "deepseek" in text and "openai" in text


async def test_remove_user_also_removes_key_and_usage(client, outbox, store, users, key_check, fake_db):
    send(client, BOB, "/setkey gemini k")
    await store.add_usage(BOB, tokens=5)
    send(client, ADMIN, f"/remove {BOB}")
    assert str(BOB) not in fake_db.data.get("llm_keys", {})
    assert str(BOB) not in fake_db.data.get("usage", {})
