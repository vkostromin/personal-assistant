from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src import runner
from src.main import app
from src.models import Account, Item, User, utcnow
from tests.conftest import WEBHOOK_HEADERS

BOB, EVE = 2002, 3003
UTC = timezone.utc


@pytest.fixture
def client():
    return TestClient(app)


def send(client, uid, text):
    msg = {"message_id": 1, "from": {"id": uid, "first_name": "U"}, "chat": {"id": uid, "type": "private"}, "text": text}
    assert client.post("/webhook", json={"message": msg}, headers=WEBHOOK_HEADERS).json() == {"ok": True}


def press(client, uid, data, message_id=700):
    cq = {"id": f"cq{message_id}", "from": {"id": uid}, "data": data,
          "message": {"message_id": message_id, "chat": {"id": uid, "type": "private"}}}
    assert client.post("/webhook", json={"callback_query": cq}, headers=WEBHOOK_HEADERS).json() == {"ok": True}


def buttons(markup):
    return [b.get("callback_data") or b.get("url") for row in (markup or {}).get("inline_keyboard", []) for b in row]


@pytest.fixture
async def bob(store):
    await store.save_user(User(id=BOB, chat_id=BOB, name="Bob", timezone="UTC"))
    await store.save_user(User(id=EVE, chat_id=EVE, name="Eve"))
    await store.link_account(Account(email="bob@x.com", user_id=BOB, refresh_token="t", chat_send=True))


async def item(store, **kw):
    base = dict(user_id=BOB, kind="reply", title="Answer Ann about the budget", counterparty="Ann",
                source="chat", account="bob@x.com", space="spaces/AAA", chat_thread="spaces/AAA/threads/T1",
                detail="Can we cut the budget by 10%?")
    base.update(kw)
    return await store.create_item(Item(**base))


# ------------------------------------------------------------- chat replies
@pytest.fixture
def chat_api(monkeypatch):
    sent = []

    def send_chat_message(account, client_id, client_secret, space, text, thread=None):
        sent.append(dict(account=account.email, space=space, text=text, thread=thread))
        return {"name": "spaces/AAA/messages/new"}

    monkeypatch.setattr("src.actions.send_chat_message", send_chat_message)
    return sent


async def test_reply_to_chat_from_telegram(client, outbox, store, bob, chat_api):
    a = await item(store)
    press(client, BOB, f"i:open:{a}")
    card, markup = outbox.last(BOB)[1], outbox.last(BOB)[2]
    assert "Can we cut the budget" in card and f"i:crep:{a}" in buttons(markup)

    press(client, BOB, f"i:crep:{a}")
    assert "Type your reply" in outbox.last(BOB)[1]
    send(client, BOB, "Yes, 10% works")
    preview, markup = outbox.last(BOB)[1], outbox.last(BOB)[2]
    assert "Yes, 10% works" in preview and f"i:csend:{a}" in buttons(markup)
    assert chat_api == []  # nothing posted before confirmation

    press(client, BOB, f"i:csend:{a}")
    assert chat_api == [dict(account="bob@x.com", space="spaces/AAA", text="Yes, 10% works", thread="spaces/AAA/threads/T1")]
    assert (await store.get_item(a)).status == "done"
    assert "Sent" in outbox.edits[-1][2]
    press(client, BOB, f"i:csend:{a}")  # double tap
    assert len(chat_api) == 1


async def test_lola_drafts_the_chat_reply(client, outbox, store, bob, chat_api, fake_llm):
    a = await item(store)
    fake_llm.canned["Write my reply to this Google Chat message"] = "Sure, let's do 10%."
    press(client, BOB, f"i:csug:{a}")
    [prompt] = fake_llm.prompts("Write my reply to this Google Chat message")
    assert "Can we cut the budget" in prompt
    assert "Sure, let's do 10%." in outbox.last(BOB)[1]
    press(client, BOB, f"i:csend:{a}")
    assert chat_api[0]["text"] == "Sure, let's do 10%."


async def test_chat_reply_cancel_and_expiry(client, outbox, store, bob, chat_api):
    a = await item(store)
    press(client, BOB, f"i:crep:{a}")
    send(client, BOB, "hello")
    press(client, BOB, f"i:cx:{a}")
    press(client, BOB, f"i:csend:{a}")
    assert chat_api == [] and "Expired" in outbox.answers[-1][1]


async def test_chat_reply_needs_new_permission(client, outbox, store, bob, chat_api):
    await store.update_account("bob@x.com", {"chat_send": False})
    a = await item(store)
    press(client, BOB, f"i:crep:{a}")
    assert "/connect" in outbox.last(BOB)[1] and "bob@x.com" in outbox.last(BOB)[1]
    assert (await store.get_user(BOB)).pending_action is None


async def test_other_user_cannot_send_chat_reply(client, store, bob, chat_api):
    a = await item(store)
    press(client, BOB, f"i:crep:{a}")
    send(client, BOB, "ok")
    press(client, EVE, f"i:csend:{a}")
    assert chat_api == []


async def test_reply_button_only_for_chat_items(client, outbox, store, bob):
    a = await item(store, source="email", space=None, chat_thread=None)
    press(client, BOB, f"i:open:{a}")
    assert f"i:crep:{a}" not in buttons(outbox.last(BOB)[2])


def test_send_chat_message_uses_thread_and_send_scope():
    from src.chat_client import CHAT_SEND_SCOPE, send_chat_message
    svc = MagicMock()
    svc.spaces.return_value.messages.return_value.create.return_value.execute.return_value = {"name": "m"}
    with patch("src.chat_client._get_service", return_value=svc) as get_service:
        assert send_chat_message(Account(email="a@x.com"), "c", "s", "spaces/AAA", "hi", "spaces/AAA/threads/T") == {"name": "m"}
    assert get_service.call_args.kwargs["scopes"] == [CHAT_SEND_SCOPE]
    kwargs = svc.spaces.return_value.messages.return_value.create.call_args.kwargs
    assert kwargs == {"parent": "spaces/AAA", "body": {"text": "hi", "thread": {"name": "spaces/AAA/threads/T"}},
                      "messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"}


async def test_chat_items_remember_message_and_thread(store, monkeypatch, fake_llm):
    import json
    await store.save_user(User(id=1, chat_id=1))
    await store.link_account(Account(email="a@x.com", user_id=1, refresh_token="t", providers=["chat"]))
    monkeypatch.setattr(runner, "list_spaces", lambda **kw: [{"name": "spaces/S", "displayName": "Team"}])
    monkeypatch.setattr(runner, "fetch_messages", lambda **kw: [
        {"sender": "Ann", "text": "Hi", "time": "2030-01-01T10:00", "name": "spaces/S/messages/1", "thread": "spaces/S/threads/1"},
        {"sender": "Ann", "text": "Can you review the doc?", "time": "2030-01-01T10:01",
         "name": "spaces/S/messages/2", "thread": "spaces/S/threads/2"},
    ])
    fake_llm.canned["Google Chat messages"] = json.dumps({"items": [
        {"kind": "reply", "title": "Review Ann's doc", "counterparty": "Ann", "message_ref": "c2"}]})
    await runner.run_all_users(force=True)
    [prompt] = fake_llm.prompts("Google Chat messages")
    assert "[c2 2030-01-01T10:01] Ann: Can you review the doc?" in prompt
    [it] = await store.list_open_items(1)
    assert (it.space, it.chat_thread, it.chat_message) == ("spaces/S", "spaces/S/threads/2", "spaces/S/messages/2")
    assert it.detail == "Can you review the doc?"


# --------------------------------------------------------------------- /who
async def test_who_shows_everything_with_a_person(client, outbox, store, bob):
    now = utcnow()
    a1 = await item(store, kind="promise", title="Send deck", counterparty="Alex Smith <alex@x.com>", source="email",
                    due_at=now - timedelta(hours=2))
    a2 = await item(store, kind="waiting", title="Get numbers", counterparty="alex@x.com", source="email")
    await item(store, kind="todo", title="Alexandra's visa", counterparty="Alexandra <alexandra@y.com>")
    await item(store, kind="promise", title="Call Bob", counterparty="Bob")
    d = await store.save_draft(BOB, {"account": "bob@x.com", "to": "Alex Smith <alex@x.com>", "subject": "Re: plan",
                                     "body": "b", "status": "pending"})

    send(client, BOB, "/who alex smith")
    text, markup = outbox.last(BOB)[1], outbox.last(BOB)[2]
    assert "You promised" in text and "Send deck" in text and "overdue" in text
    assert "They owe you" in text and "Get numbers" in text  # same email, no "Smith" in the name
    assert "Drafts to them" in text and "Re: plan" in text
    assert "Alexandra" not in text and "Call Bob" not in text
    assert [f"i:open:{a1}", f"i:open:{a2}", f"d:open:{d}"] == buttons(markup)

    send(client, BOB, "/who alex")  # two different people: lines are labelled
    text = outbox.last(BOB)[1]
    assert "Alexandra's visa" in text and "— Alexandra" in text

    send(client, BOB, "/who")
    text = outbox.last(BOB)[1]
    assert text.index("Alex Smith") < text.index("Bob")  # overdue first
    assert "3 open, ⚠️ 1 overdue" in text

    send(client, BOB, "/who nobody")
    assert "Nothing open" in outbox.last(BOB)[1]


async def test_who_ignores_case_and_accents(client, outbox, store, bob):
    await item(store, title="Send photos", counterparty="Zoë Müller")
    send(client, BOB, "/who zoe muller")
    assert "Send photos" in outbox.last(BOB)[1]


# ------------------------------------------------------------------ /review
async def review_items(store):
    now = utcnow()
    old = await item(store, kind="todo", title="Old task", created_at=now - timedelta(days=10), source="manual")
    gave_up = await item(store, kind="promise", title="Forgotten promise", created_at=now - timedelta(days=3),
                         due_at=now - timedelta(days=2), next_remind_at=None, remind_stage=5, source="manual")
    await item(store, kind="todo", title="Fresh", created_at=now - timedelta(days=1), source="manual")
    await item(store, kind="todo", title="Snoozed old", created_at=now - timedelta(days=20),
               snoozed_until=now + timedelta(days=1), source="manual")
    return old, gave_up


async def test_review_walks_through_old_items(client, outbox, store, bob):
    old, gave_up = await review_items(store)
    send(client, BOB, "/review")
    text, markup = outbox.last(BOB)[1], outbox.last(BOB)[2]
    assert "Old task" in text and "2 left" in text and f"r:done:{old}" in buttons(markup)

    press(client, BOB, f"r:done:{old}")
    assert (await store.get_item(old)).status == "done"
    text, markup = outbox.edits[-1][2], outbox.edits[-1][3]
    assert "Forgotten promise" in text and "reminders used up" in text and "1 left" in text

    press(client, BOB, f"r:again:{gave_up}")  # "reset the counter"
    it = await store.get_item(gave_up)
    assert it.remind_stage == 0 and it.next_remind_at is not None and it.reviewed_at is not None
    assert "Review finished" in outbox.edits[-1][2]

    send(client, BOB, "/review")
    assert "Nothing to review" in outbox.last(BOB)[1]


async def test_review_remind_again_without_deadline_asks_for_one(client, outbox, store, bob):
    old, _ = await review_items(store)
    send(client, BOB, "/review")
    press(client, BOB, f"r:again:{old}")
    assert f"r:due:{old}:tm" in buttons(outbox.edits[-1][3])
    press(client, BOB, f"r:due:{old}:tm")
    it = await store.get_item(old)
    assert it.due_at is not None and it.next_remind_at is not None and it.reviewed_at is not None
    assert "Forgotten promise" in outbox.edits[-1][2]  # moved on


async def test_review_skip_cancel_stop(client, outbox, store, bob):
    old, gave_up = await review_items(store)
    send(client, BOB, "/review")
    press(client, BOB, f"r:skip:{old}")
    assert (await store.get_item(old)).status == "open"
    press(client, BOB, f"r:cancel:{gave_up}")
    assert (await store.get_item(gave_up)).status == "cancelled"
    send(client, BOB, "/review")  # skipped item is still waiting for a decision
    assert "Old task" in outbox.last(BOB)[1]
    press(client, BOB, f"r:stop:{old}")
    assert "paused" in outbox.edits[-1][2]
    press(client, EVE, f"r:done:{old}")
    assert (await store.get_item(old)).status == "open"


async def test_summary_suggests_review(store, outbox, monkeypatch):
    monkeypatch.setattr(runner, "list_message_ids", lambda **kw: ([], False))
    monkeypatch.setattr(runner, "list_spaces", lambda **kw: [])
    await store.save_user(User(id=1, chat_id=1))
    await store.link_account(Account(email="a@x.com", user_id=1, refresh_token="t"))
    for n in range(5):
        await store.create_item(Item(user_id=1, kind="todo", title=f"Old {n}", created_at=utcnow() - timedelta(days=30)))
    await runner.run_all_users(force=True)
    assert "5 items are getting old — /review" in outbox.text(1)
