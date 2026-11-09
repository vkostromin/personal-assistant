import re

import pytest
from fastapi.testclient import TestClient

from src import config
from src.main import app
from src.models import Account
from tests.conftest import BASE_URL, BOT_USERNAME, WEBHOOK_HEADERS, make_settings

HEADERS = WEBHOOK_HEADERS
ADMIN, BOB, EVE = 1001, 2002, 3003


@pytest.fixture
def client():
    return TestClient(app)


def send(client, uid, text, chat_type="private", chat_id=None, username=""):
    body = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": uid, "first_name": f"U{uid}", "username": username},
            "chat": {"id": chat_id or uid, "type": chat_type},
            "text": text,
        },
    }
    r = client.post("/webhook", json=body, headers=HEADERS)
    assert r.status_code == 200 and r.json() == {"ok": True}


def invite_code(outbox, chat_id):
    text = outbox.last(chat_id)[1]
    m = re.search(rf"https://t\.me/{re.escape(BOT_USERNAME)}\?start=([\w-]+)", text)
    assert m, text
    return m.group(1)


def test_first_user_becomes_admin_and_gets_connect_button(client, outbox, store):
    send(client, ADMIN, "/start")
    chat_id, text, markup = outbox.last(ADMIN)
    assert "administrator" in text
    button = markup["inline_keyboard"][0][0]
    assert button["url"].startswith(f"{BASE_URL}/oauth/start?state=")


async def test_admin_claim_is_persisted(client, store):
    send(client, ADMIN, "/start")
    assert await store.get_admin_id() == ADMIN


def test_second_user_without_invite_is_rejected(client, outbox):
    send(client, ADMIN, "/start")
    send(client, EVE, "/start")
    assert "invitation" in outbox.last(EVE)[1]
    send(client, EVE, "/status")
    assert "invitation" in outbox.last(EVE)[1]


def test_invite_flow(client, outbox):
    send(client, ADMIN, "/start")
    send(client, ADMIN, "/invite for Bob")
    code = invite_code(outbox, ADMIN)

    send(client, BOB, f"/start {code}", username="bob")
    _, text, markup = outbox.last(BOB)
    assert "Welcome" in text and markup["inline_keyboard"][0][0]["url"].startswith(f"{BASE_URL}/oauth/start")
    assert "joined using your invitation" in outbox.last(ADMIN)[1]

    # the same link can't be reused by someone else
    send(client, EVE, f"/start {code}")
    assert "already been used" in outbox.last(EVE)[1]

    # Bob is a normal user: no admin commands
    send(client, BOB, "/invite")
    assert "Unknown command" in outbox.last(BOB)[1]
    send(client, BOB, "/help")
    assert "/invite" not in outbox.last(BOB)[1]
    send(client, ADMIN, "/help")
    assert "/invite" in outbox.last(ADMIN)[1]


def test_invalid_invite(client, outbox):
    send(client, ADMIN, "/start")
    send(client, EVE, "/start bogus-code")
    assert "Invalid invitation" in outbox.last(EVE)[1]


def test_admin_telegram_id_restricts_bootstrap(client, outbox, monkeypatch, store):
    monkeypatch.setattr(config, "settings", make_settings(admin_telegram_id=str(ADMIN)))
    send(client, EVE, "/start")
    assert "invitation" in outbox.last(EVE)[1]
    send(client, ADMIN, "/start")
    assert "administrator" in outbox.last(ADMIN)[1]


def test_group_chats_are_refused(client, outbox, store):
    send(client, ADMIN, "/start", chat_type="group", chat_id=-100)
    assert "private chat" in outbox.last(-100)[1]
    assert outbox.to(ADMIN) == []


async def test_disabled_user_is_blocked_and_can_be_enabled(client, outbox, store):
    send(client, ADMIN, "/start")
    send(client, ADMIN, "/invite")
    send(client, BOB, f"/start {invite_code(outbox, ADMIN)}")

    send(client, ADMIN, f"/disable {BOB}")
    send(client, BOB, "/status")
    assert "disabled" in outbox.last(BOB)[1]

    send(client, ADMIN, f"/enable {BOB}")
    send(client, BOB, "/status")
    assert "Status" in outbox.last(BOB)[1]

    send(client, ADMIN, f"/disable {ADMIN}")
    assert "yourself" in outbox.last(ADMIN)[1]


async def test_users_only_see_their_own_data(client, outbox, store):
    send(client, ADMIN, "/start")
    send(client, ADMIN, "/invite")
    send(client, BOB, f"/start {invite_code(outbox, ADMIN)}")
    await store.link_account(Account(email="admin@x.com", user_id=ADMIN, refresh_token="t"))
    await store.link_account(Account(email="bob@x.com", user_id=BOB, refresh_token="t"))
    await store.save_draft(ADMIN, {"account": "admin@x.com", "to": "ceo@x.com", "subject": "Admin secret", "body": "b", "status": "pending"})
    await store.save_draft(BOB, {"account": "bob@x.com", "to": "mom@x.com", "subject": "Bob stuff", "body": "b", "status": "pending"})

    send(client, BOB, "/drafts")
    text = outbox.last(BOB)[1]
    assert "Bob stuff" in text and "Admin secret" not in text

    send(client, BOB, "/accounts")
    text = outbox.last(BOB)[1]
    assert "bob@x.com" in text and "admin@x.com" not in text

    # can't disconnect someone else's account
    send(client, BOB, "/disconnect admin@x.com")
    assert "No such account" in outbox.last(BOB)[1]
    assert await store.get_account("admin@x.com") is not None

    send(client, BOB, "/disconnect BOB@x.com")
    assert "disconnected" in outbox.last(BOB)[1]
    assert await store.get_account("bob@x.com") is None
    assert await store.get_pending_drafts(BOB) == []


async def test_admin_remove_user(client, outbox, store):
    send(client, ADMIN, "/start")
    send(client, ADMIN, "/invite")
    send(client, BOB, f"/start {invite_code(outbox, ADMIN)}")
    await store.link_account(Account(email="bob@x.com", user_id=BOB, refresh_token="t"))

    send(client, ADMIN, "/users")
    assert str(BOB) in outbox.last(ADMIN)[1]
    send(client, ADMIN, f"/remove {BOB}")
    assert "removed" in outbox.last(ADMIN)[1]
    assert await store.get_user(BOB) is None
    assert await store.get_account("bob@x.com") is None

    send(client, BOB, "/status")
    assert "invitation" in outbox.last(BOB)[1]


def test_invites_list_and_revoke(client, outbox):
    send(client, ADMIN, "/start")
    send(client, ADMIN, "/invite for Carol")
    code = invite_code(outbox, ADMIN)
    send(client, ADMIN, "/invites")
    assert code in outbox.last(ADMIN)[1] and "Carol" in outbox.last(ADMIN)[1]
    send(client, ADMIN, f"/revoke {code}")
    assert "revoked" in outbox.last(ADMIN)[1]
    send(client, BOB, f"/start {code}")
    assert "Invalid invitation" in outbox.last(BOB)[1]


def test_command_with_bot_suffix(client, outbox):
    send(client, ADMIN, "/start")
    send(client, ADMIN, f"/help@{BOT_USERNAME}")
    assert "/status" in outbox.last(ADMIN)[1]


def test_handler_error_is_reported_and_not_retried(client, outbox, monkeypatch):
    send(client, ADMIN, "/start")

    async def boom(ctx):
        raise RuntimeError("LLM down")

    monkeypatch.setitem(__import__("src.bot", fromlist=["USER_COMMANDS"]).USER_COMMANDS, "/status", boom)
    send(client, ADMIN, "/status")  # still 200 {"ok": True}
    assert "went wrong" in outbox.last(ADMIN)[1]


async def test_disconnect_legacy_mixed_case_account(client, outbox, store, fake_db):
    send(client, ADMIN, "/start")
    fake_db.data["accounts"] = {"Boss@Corp.com": {"refresh_token": "t", "enabled": True, "user_id": ADMIN}}
    send(client, ADMIN, "/disconnect boss@corp.com")
    assert "disconnected" in outbox.last(ADMIN)[1]
    assert fake_db.data["accounts"] == {}
