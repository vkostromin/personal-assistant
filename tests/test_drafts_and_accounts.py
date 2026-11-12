import pytest
from fastapi.testclient import TestClient

from src import runner
from src.main import app
from src.models import Account, User
from src.schedule import current_slot, tz_of
from src.models import utcnow
from tests.conftest import WEBHOOK_HEADERS

HEADERS = WEBHOOK_HEADERS
ADMIN, BOB, CAROL = 1001, 2002, 3003



@pytest.fixture
def client():
    return TestClient(app)


def send(client, uid, text):
    body = {"message": {"message_id": 1, "from": {"id": uid, "first_name": f"U{uid}"},
                        "chat": {"id": uid, "type": "private"}, "text": text}}
    assert client.post("/webhook", json=body, headers=HEADERS).json() == {"ok": True}


@pytest.fixture
async def users(store):
    await store.claim_admin(User(id=ADMIN, chat_id=ADMIN, name="Admin"))
    await store.save_user(User(id=BOB, chat_id=BOB, name="Bob"))
    await store.link_account(Account(email="bob@x.com", user_id=BOB, refresh_token="bob-token"))


async def add_drafts(store, n):
    ids = []
    for i in range(1, n + 1):
        ids.append(await store.save_draft(BOB, {
            "account": "bob@x.com", "to": f"p{i}@x.com", "subject": f"S{i}", "body": f"Body number {i}",
            "status": "pending", "gmail_draft_id": f"g{i}", "thread_id": f"t{i}",
        }))
    return ids


# ------------------------------------------------------------- Gmail sync
async def test_drafts_command_syncs_with_gmail(client, outbox, store, users, gmail_drafts):
    ids = await add_drafts(store, 3)
    gmail_drafts.gone = {"g1": "sent", "g2": "discarded"}
    send(client, BOB, "/drafts")
    text = outbox.last(BOB)[1]
    assert "1 sent, 1 deleted" in text
    assert "S3" in text and "S1" not in text and "S2" not in text
    assert [d["id"] for d in await store.get_pending_drafts(BOB)] == [ids[2]]


async def test_run_syncs_drafts_and_reports(store, outbox, users, gmail_drafts, monkeypatch):
    monkeypatch.setattr(runner, "list_message_ids", lambda **kw: ([], False))  # empty mailbox
    monkeypatch.setattr(runner, "list_spaces", lambda **kw: [])
    await add_drafts(store, 2)
    gmail_drafts.gone = {"g2": "sent"}
    await runner.run_all_users(force=True)
    assert "Drafts updated from Gmail: 1 sent" in outbox.text(BOB)
    assert len(await store.get_pending_drafts(BOB)) == 1


async def test_rewrite_of_already_sent_draft_does_nothing(client, outbox, store, users, gmail_drafts, fake_llm):
    await add_drafts(store, 1)
    gmail_drafts.gone = {"g1": "sent"}
    send(client, BOB, "/rewrite 1 add more detail")
    assert "already sent" in outbox.last(BOB)[1]
    assert fake_llm.calls == []
    assert await store.get_pending_drafts(BOB) == []


# ------------------------------------------------------- manual management
async def test_mark_sent_and_delete(client, outbox, store, users, gmail_drafts):
    ids = await add_drafts(store, 4)
    send(client, BOB, "/sent 1,3")
    assert "2 draft(s) marked as sent" in outbox.last(BOB)[1]
    assert [d["id"] for d in await store.get_pending_drafts(BOB)] == [ids[1], ids[3]]
    assert gmail_drafts.deleted == []  # marking as sent doesn't touch Gmail

    send(client, BOB, "/deletedraft 2")  # numbering follows the current list: ids[3]
    assert gmail_drafts.deleted == ["g4"]
    assert [d["id"] for d in await store.get_pending_drafts(BOB)] == [ids[1]]
    assert "deleted" in outbox.last(BOB)[1]

    send(client, BOB, "/deletedraft all")
    assert await store.get_pending_drafts(BOB) == []


async def test_draft_numbers_validation(client, outbox, store, users):
    await add_drafts(store, 2)
    for bad in ("/sent", "/sent 3", "/sent 0", "/sent x", "/deletedraft 2-5"):
        send(client, BOB, bad)
        assert "Usage" in outbox.last(BOB)[1], bad
    assert len(await store.get_pending_drafts(BOB)) == 2
    send(client, BOB, "/sent 1-2")
    assert await store.get_pending_drafts(BOB) == []


async def test_show_full_draft(client, outbox, store, users):
    await add_drafts(store, 2)
    send(client, BOB, "/draft 2")
    text = outbox.last(BOB)[1]
    assert "p2@x.com" in text and "Body number 2" in text


async def test_cannot_touch_other_users_drafts(client, outbox, store, users, gmail_drafts):
    await add_drafts(store, 1)
    send(client, ADMIN, "/deletedraft 1")
    assert "No pending drafts" in outbox.last(ADMIN)[1]
    assert len(await store.get_pending_drafts(BOB)) == 1


# ------------------------------------------------------------ self deletion
async def test_user_deletes_own_account(client, outbox, store, users, fake_db):
    await add_drafts(store, 1)
    send(client, BOB, "/deleteme")
    assert "/deleteme YES" in outbox.last(BOB)[1]
    assert await store.get_user(BOB) is not None

    send(client, BOB, "/deleteme YES")
    assert "deleted" in outbox.last(BOB)[1]
    assert await store.get_user(BOB) is None
    assert await store.get_account("bob@x.com") is None
    assert not fake_db.data["drafts"]
    send(client, BOB, "/status")
    assert "invitation" in outbox.last(BOB)[1]


async def test_admin_must_transfer_before_deleting(client, outbox, store, users):
    send(client, ADMIN, "/deleteme YES")
    assert "/transferadmin" in outbox.last(ADMIN)[1]
    assert await store.get_user(ADMIN) is not None

    send(client, ADMIN, f"/transferadmin {BOB}")
    assert "now the administrator" in outbox.last(ADMIN)[1]
    assert "You are now the administrator" in outbox.last(BOB)[1]
    assert (await store.get_user(BOB)).is_admin and not (await store.get_user(ADMIN)).is_admin
    assert await store.get_admin_id() == BOB

    send(client, BOB, "/users")  # Bob has admin commands now
    assert "Users" in outbox.last(BOB)[1]
    send(client, ADMIN, "/users")
    assert "Unknown command" in outbox.last(ADMIN)[1]

    send(client, ADMIN, "/deleteme YES")
    assert await store.get_user(ADMIN) is None


async def test_sole_admin_deletion_resets_the_bot(client, outbox, store):
    send(client, ADMIN, "/start")
    send(client, ADMIN, "/deleteme YES")
    assert "next person" in outbox.last(ADMIN)[1]
    send(client, CAROL, "/start")
    assert "administrator" in outbox.last(CAROL)[1]
    assert await store.get_admin_id() == CAROL


# ------------------------------------------------------------------ unlink
async def test_unlink_chat_then_gmail(client, outbox, store, users):
    send(client, BOB, "/unlink chat")
    assert (await store.get_account("bob@x.com")).providers == ["gmail"]
    send(client, BOB, "/unlink chat")
    assert "not in use" in outbox.last(BOB)[1]
    send(client, BOB, "/unlink bob@x.com gmail")
    assert "disconnected" in outbox.last(BOB)[1]
    assert await store.get_account("bob@x.com") is None


async def test_unlink_needs_email_with_several_accounts(client, outbox, store, users):
    await store.link_account(Account(email="bob2@x.com", user_id=BOB, refresh_token="t"))
    send(client, BOB, "/unlink gmail")
    assert "several accounts" in outbox.last(BOB)[1]
    send(client, BOB, "/unlink bob2@x.com gmail")
    assert (await store.get_account("bob2@x.com")).providers == ["chat"]
    send(client, BOB, "/unlink admin@x.com gmail")
    assert "No such account" in outbox.last(BOB)[1]


# ----------------------------------------------------------------- timezone
async def test_set_timezone(client, outbox, store, users):
    send(client, BOB, "/timezone")
    assert "UTC" in outbox.last(BOB)[1]
    send(client, BOB, "/timezone jakarta")
    assert "Asia/Jakarta" in outbox.last(BOB)[1]
    bob = await store.get_user(BOB)
    assert bob.timezone == "Asia/Jakarta"
    # no extra summary right after changing the timezone
    assert bob.last_run_slot == current_slot(utcnow(), tz_of("Asia/Jakarta"))
    send(client, BOB, "/timezone Narnia")
    assert "Unknown timezone" in outbox.last(BOB)[1]
