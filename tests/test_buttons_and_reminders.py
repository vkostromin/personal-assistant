from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models import Account, Item, User, utcnow
from src.reminders import run_reminders
from tests.conftest import WEBHOOK_HEADERS

HEADERS = WEBHOOK_HEADERS
BOB, EVE = 2002, 3003
UTC = timezone.utc


@pytest.fixture
def client():
    return TestClient(app)


def send(client, uid, text, **extra):
    msg = {"message_id": 1, "from": {"id": uid, "first_name": f"U{uid}"}, "chat": {"id": uid, "type": "private"},
           "text": text, **extra}
    assert client.post("/webhook", json={"message": msg}, headers=HEADERS).json() == {"ok": True}


def press(client, uid, data, message_id=500):
    cq = {"id": f"cq{message_id}", "from": {"id": uid}, "data": data,
          "message": {"message_id": message_id, "chat": {"id": uid, "type": "private"}}}
    assert client.post("/webhook", json={"callback_query": cq}, headers=HEADERS).json() == {"ok": True}


def buttons(markup):
    return {b.get("callback_data") or b.get("url"): b["text"] for row in (markup or {}).get("inline_keyboard", []) for b in row}


@pytest.fixture
async def bob(store):
    u = User(id=BOB, chat_id=BOB, name="Bob", timezone="UTC")
    await store.save_user(u)
    await store.save_user(User(id=EVE, chat_id=EVE, name="Eve"))
    await store.link_account(Account(email="bob@x.com", user_id=BOB, refresh_token="t"))
    return u


@pytest.fixture
def gmail(monkeypatch):
    calls = {"sent": [], "created": [], "updated": []}

    def send_draft(account, client_id, client_secret, draft_id):
        calls["sent"].append(draft_id)
        return {"id": "msg1", "threadId": "t1"}

    def create_draft(account, client_id, client_secret, to, subject, body, thread_id=None, in_reply_to=None):
        calls["created"].append(dict(to=to, subject=subject, body=body, thread_id=thread_id, in_reply_to=in_reply_to))
        return "g-new"

    def update_gmail_draft(**kw):
        calls["updated"].append(kw)
        return kw["draft_id"], False

    monkeypatch.setattr("src.actions.send_draft", send_draft)
    monkeypatch.setattr("src.actions.create_draft", create_draft)
    monkeypatch.setattr("src.actions.update_gmail_draft", update_gmail_draft)
    monkeypatch.setattr("src.actions.delete_gmail_draft", lambda **kw: True)
    return calls


async def new_item(store, **kw):
    base = dict(user_id=BOB, kind="promise", title="Send Q3 report", counterparty="Alex <alex@x.com>",
                account="bob@x.com", thread_id="t1", in_reply_to="<orig@mail>", subject="Q3 numbers")
    base.update(kw)
    return await store.create_item(Item(**base))


async def new_draft(store, **kw):
    data = {"account": "bob@x.com", "to": "alex@x.com", "subject": "Re: Q3", "body": "Here it is",
            "status": "pending", "gmail_draft_id": "g1", "thread_id": "t1", "priority": "high"}
    data.update(kw)
    return await store.save_draft(BOB, data)


# ----------------------------------------------------------------- item buttons
async def test_item_done_and_cancel(client, outbox, store, bob):
    a, b = await new_item(store), await new_item(store, title="Other")
    press(client, BOB, f"i:done:{a}")
    assert (await store.get_item(a)).status == "done"
    _, _, text, markup = outbox.edits[-1]
    assert "Done" in text and markup is None  # card closed, buttons removed
    press(client, BOB, f"i:cancel:{b}")
    item = await store.get_item(b)
    assert item.status == "cancelled" and item.next_remind_at is None
    press(client, BOB, f"i:done:{b}")
    assert outbox.answers[-1][1] == "Already closed."


async def test_item_snooze_menu_and_choice(client, outbox, store, bob):
    a = await new_item(store)
    press(client, BOB, f"i:snz:{a}")
    assert "i:snz:%s:4" % a in buttons(outbox.edits[-1][3])
    press(client, BOB, f"i:snz:{a}:4")
    item = await store.get_item(a)
    assert abs((item.snoozed_until - utcnow()).total_seconds() - 4 * 3600) < 60
    assert item.next_remind_at == item.snoozed_until


async def test_item_due_presets_and_free_text(client, outbox, store, bob, fake_llm):
    a = await new_item(store)
    press(client, BOB, f"i:due:{a}")
    assert f"i:due:{a}:tm" in buttons(outbox.edits[-1][3])
    press(client, BOB, f"i:due:{a}:tm")
    item = await store.get_item(a)
    assert item.due_at is not None and item.next_remind_at is not None and item.remind_stage == 0

    press(client, BOB, f"i:due:{a}:other")
    assert "When is it due" in outbox.last(BOB)[1]
    send(client, BOB, "next friday at 3pm")
    item = await store.get_item(a)
    assert item.due_at == datetime(2030, 1, 10, 15, tzinfo=UTC)  # from the (fake) LLM
    assert "Deadline updated" in outbox.last(BOB)[1]
    assert (await store.get_user(BOB)).pending_action is None


async def test_follow_up_creates_threaded_draft(client, outbox, store, bob, gmail):
    a = await new_item(store, kind="waiting", due_at=utcnow() - timedelta(hours=5))
    press(client, BOB, f"i:fu:{a}")
    [created] = gmail["created"]
    assert created["thread_id"] == "t1" and created["in_reply_to"] == "<orig@mail>"
    assert created["body"] == "Just checking in" and created["to"] == "Alex <alex@x.com>"
    text, markup = outbox.last(BOB)[1], outbox.last(BOB)[2]
    assert "Follow-up draft" in text and any(k.startswith("d:send:") for k in buttons(markup))
    item = await store.get_item(a)
    assert item.due_at > utcnow() + timedelta(hours=47)  # waiting window extended


async def test_cannot_press_other_users_buttons(client, outbox, store, bob, gmail):
    a = await new_item(store)
    d = await new_draft(store)
    press(client, EVE, f"i:done:{a}")
    press(client, EVE, f"d:sendok:{d}")
    assert (await store.get_item(a)).status == "open"
    assert gmail["sent"] == []
    assert [t for _, t in outbox.answers] == ["Item not found.", "Draft not found."]


async def test_unknown_user_buttons_are_ignored(client, outbox, store, bob):
    a = await new_item(store)
    press(client, 999, f"i:done:{a}")
    assert (await store.get_item(a)).status == "open"
    assert outbox.answers[-1][1] == "Not available."


# ---------------------------------------------------------------- draft buttons
async def test_send_requires_confirmation(client, outbox, store, bob, gmail):
    reply_item = await new_item(store, kind="reply", title="Answer Alex")
    d = await new_draft(store)
    press(client, BOB, f"d:send:{d}")
    assert gmail["sent"] == []
    assert f"d:sendok:{d}" in buttons(outbox.edits[-1][3])
    press(client, BOB, f"d:sendok:{d}")
    assert gmail["sent"] == ["g1"]
    assert (await store.get_draft(d))["status"] == "sent"
    assert "Sent" in outbox.edits[-1][2]
    assert (await store.get_item(reply_item)).status == "done"  # reply in the same thread closes it
    press(client, BOB, f"d:sendok:{d}")  # double tap
    assert gmail["sent"] == ["g1"]


async def test_rewrite_via_reply_text(client, outbox, store, bob, gmail):
    d = await new_draft(store)
    press(client, BOB, f"d:rw:{d}")
    assert "What should I change" in outbox.last(BOB)[1]
    send(client, BOB, "make it shorter")
    [upd] = gmail["updated"]
    assert upd["draft_id"] == "g1" and upd["body"] == "Rewritten body"
    assert "Updated in Gmail" in outbox.last(BOB)[1]
    assert (await store.get_draft(d))["body"] == "Rewritten body"


async def test_draft_snooze_discard_and_already_sent(client, outbox, store, bob, gmail):
    d1, d2, d3 = await new_draft(store), await new_draft(store), await new_draft(store)
    press(client, BOB, f"d:snz:{d1}:2")
    assert (await store.get_draft(d1))["next_remind_at"] is not None
    press(client, BOB, f"d:del:{d2}")
    assert await store.get_draft(d2) is None
    press(client, BOB, f"d:sent:{d3}")
    assert (await store.get_draft(d3))["status"] == "sent"
    assert [d["id"] for d in await store.get_pending_drafts(BOB)] == [d1]


# -------------------------------------------------------------- tasks & focus
async def test_free_text_offers_task_or_question(client, outbox, store, bob, fake_llm):
    send(client, BOB, "Remember to renew the domain", forward_origin={"type": "user"})
    assert {"t:add:", "t:ask:"} <= set(buttons(outbox.last(BOB)[2]))
    press(client, BOB, "t:add:")
    [item] = await store.list_open_items(BOB)
    assert (item.title, item.priority, item.source) == ("Parsed task", "high", "manual")
    assert "Added" in outbox.last(BOB)[1]
    press(client, BOB, "t:add:")  # stale button
    assert "Expired" in outbox.answers[-1][1]

    send(client, BOB, "what is overdue?")
    press(client, BOB, "t:ask:")
    assert outbox.last(BOB)[1] == "ANSWER"


async def test_task_command_falls_back_without_llm(client, outbox, store, bob, monkeypatch):
    from src import config
    from tests.conftest import make_settings
    monkeypatch.setattr(config, "settings", make_settings(gemini_api_key=""))
    send(client, BOB, "/task call the bank")
    [item] = await store.list_open_items(BOB)
    assert item.title == "call the bank" and item.kind == "todo"


async def test_focus_and_tasks_commands(client, outbox, store, bob):
    now = utcnow()
    late = await new_item(store, title="Overdue promise", due_at=now - timedelta(days=1))
    await new_item(store, kind="waiting", title="Not late yet", due_at=now + timedelta(days=2))
    d = await new_draft(store)
    send(client, BOB, "/focus")
    text, markup = outbox.last(BOB)[1], outbox.last(BOB)[2]
    assert text.index("Overdue promise") < text.index("Reply to")
    assert "Not late yet" not in text
    assert {f"i:open:{late}", f"d:open:{d}"} <= set(buttons(markup))

    press(client, BOB, f"i:open:{late}")
    assert "Overdue promise" in outbox.last(BOB)[1] and f"i:done:{late}" in buttons(outbox.last(BOB)[2])

    send(client, BOB, "/tasks")
    text = outbox.last(BOB)[1]
    assert "Waiting for" in text and "Not late yet" in text and "I promised" in text
    send(client, BOB, "/tasks waiting")
    assert "Overdue promise" not in outbox.last(BOB)[1]


# ------------------------------------------------------------------- reminders
async def test_reminders_send_cards_and_advance(store, outbox, bob):
    now = datetime(2030, 1, 10, 12, tzinfo=UTC)  # Bob is in UTC: 12:00, not quiet
    a = await new_item(store, kind="waiting", title="Get numbers", due_at=now - timedelta(hours=1),
                       next_remind_at=now - timedelta(minutes=5))
    closed = await new_item(store, status="done", next_remind_at=now - timedelta(hours=1))
    d = await new_draft(store, snoozed_until=now - timedelta(minutes=1), next_remind_at=now - timedelta(minutes=1))

    stats = await run_reminders(now)
    assert stats["reminders"] == 2
    texts = outbox.text(BOB)
    assert "No answer yet" in texts and "Back from snooze" in texts
    item = await store.get_item(a)
    assert item.remind_stage == 2 and item.next_remind_at == datetime(2030, 1, 11, 9, tzinfo=UTC)
    assert (await store.get_item(closed)).next_remind_at is None
    assert (await store.get_draft(d))["next_remind_at"] is None

    assert (await run_reminders(now))["reminders"] == 0  # nothing twice


async def test_reminders_respect_quiet_hours_and_cap(store, outbox, bob, monkeypatch):
    night = datetime(2030, 1, 10, 23, tzinfo=UTC)
    for n in range(7):
        await new_item(store, title=f"Task {n}", due_at=night - timedelta(days=1), next_remind_at=night - timedelta(hours=1))
    stats = await run_reminders(night)
    assert stats["reminders"] == 0 and stats["quiet"] == 7 and not outbox.to(BOB)

    morning = datetime(2030, 1, 11, 8, tzinfo=UTC)
    stats = await run_reminders(morning)
    assert stats["reminders"] == 5 and stats["postponed"] == 2  # MAX_REMINDERS_PER_RUN
    stats = await run_reminders(morning + timedelta(hours=1))
    assert stats["reminders"] == 2
