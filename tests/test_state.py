from datetime import datetime, timedelta, timezone


from src.models import Account, Item, User
from src.state import LinkResult, RedeemResult


def user(uid, **kw):
    return User(id=uid, chat_id=uid, name=f"User {uid}", **kw)


async def test_first_claim_wins_and_second_is_rejected(store):
    assert await store.claim_admin(user(1)) is True
    assert await store.claim_admin(user(2)) is False
    assert await store.get_admin_id() == 1
    assert (await store.get_user(1)).is_admin
    assert await store.get_user(2) is None


async def test_invite_is_single_use(store):
    await store.claim_admin(user(1))
    inv = await store.create_invite(created_by=1, note="bob")

    status, invite = await store.redeem_invite(inv.code, user(2))
    assert status == RedeemResult.OK and invite.created_by == 1
    u2 = await store.get_user(2)
    assert u2.role == "user" and u2.invited_by == 1 and u2.is_active

    status, _ = await store.redeem_invite(inv.code, user(3))
    assert status == RedeemResult.USED
    assert await store.get_user(3) is None
    assert await store.list_invites() == []


async def test_invite_expired_invalid_and_already_registered(store, fake_db):
    await store.claim_admin(user(1))
    inv = await store.create_invite(created_by=1)
    assert (await store.redeem_invite("nope", user(2)))[0] == RedeemResult.INVALID
    assert (await store.redeem_invite("", user(2)))[0] == RedeemResult.INVALID
    assert (await store.redeem_invite("a/b", user(2)))[0] == RedeemResult.INVALID
    assert (await store.redeem_invite(inv.code, user(1)))[0] == RedeemResult.ALREADY_REGISTERED

    fake_db.data["invites"][inv.code]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert (await store.redeem_invite(inv.code, user(2)))[0] == RedeemResult.EXPIRED
    assert await store.get_user(2) is None


async def test_revoke_invite(store):
    inv = await store.create_invite(created_by=1)
    assert len(await store.list_invites()) == 1
    assert await store.revoke_invite(inv.code) is True
    assert await store.revoke_invite(inv.code) is False
    assert (await store.redeem_invite(inv.code, user(2)))[0] == RedeemResult.INVALID


async def test_oauth_state_single_use_and_expiry(store, fake_db):
    st = await store.create_oauth_state(5, "verifier", "https://x/oauth/callback")
    assert (await store.get_oauth_state(st.id)).user_id == 5
    consumed = await store.consume_oauth_state(st.id)
    assert consumed.user_id == 5 and consumed.code_verifier == "verifier"
    assert await store.consume_oauth_state(st.id) is None

    st2 = await store.create_oauth_state(5, "v", "r")
    fake_db.data["oauth_states"][st2.id]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert await store.get_oauth_state(st2.id) is None
    assert await store.consume_oauth_state(st2.id) is None
    assert st2.id not in fake_db.data["oauth_states"]  # consumed even though expired


async def test_link_account_ownership(store, fake_db):
    acc = Account(email="Alice@Example.com", user_id=1, refresh_token="rt1", providers=["gmail"])
    assert await store.link_account(acc) == LinkResult.CREATED
    # refresh token is encrypted at rest
    assert fake_db.data["accounts"]["alice@example.com"]["refresh_token"] != "rt1"
    assert (await store.get_account("alice@example.com")).refresh_token == "rt1"

    await store.update_account("alice@example.com", {"last_run": datetime(2024, 1, 1, tzinfo=timezone.utc), "needs_reauth": True})
    again = Account(email="alice@example.com", user_id=1, refresh_token="rt2", providers=["gmail", "chat"])
    assert await store.link_account(again) == LinkResult.UPDATED
    stored = await store.get_account("alice@example.com")
    assert stored.refresh_token == "rt2" and stored.providers == ["gmail", "chat"]
    assert stored.needs_reauth is False
    assert stored.last_run == datetime(2024, 1, 1, tzinfo=timezone.utc)  # watermark kept

    thief = Account(email="alice@example.com", user_id=2, refresh_token="rt3")
    assert await store.link_account(thief) == LinkResult.TAKEN
    assert (await store.get_account("alice@example.com")).user_id == 1


async def test_records_are_isolated_per_user(store):
    await store.link_account(Account(email="a@x.com", user_id=1, refresh_token="t"))
    await store.link_account(Account(email="b@x.com", user_id=2, refresh_token="t"))
    await store.save_draft(1, {"account": "a@x.com", "to": "z", "subject": "s1", "body": "b", "status": "pending"})
    await store.save_draft(2, {"account": "b@x.com", "to": "z", "subject": "s2", "body": "b", "status": "pending"})
    await store.create_item(Item(user_id=1, kind="promise", title="p1", account="a@x.com"))
    await store.create_item(Item(user_id=2, kind="promise", title="p2", account="b@x.com"))

    assert [a.email for a in await store.list_accounts(user_id=1)] == ["a@x.com"]
    assert [d["subject"] for d in await store.get_pending_drafts(1)] == ["s1"]
    assert [i.title for i in await store.list_open_items(2)] == ["p2"]


async def test_delete_account_and_user(store, fake_db):
    await store.save_user(user(2))
    await store.link_account(Account(email="a@x.com", user_id=2, refresh_token="t"))
    await store.link_account(Account(email="b@x.com", user_id=2, refresh_token="t"))
    await store.save_draft(2, {"account": "a@x.com", "status": "pending"})
    await store.save_draft(2, {"account": "b@x.com", "status": "pending"})
    await store.create_item(Item(user_id=2, kind="todo", title="p", account="a@x.com"))

    await store.delete_account("a@x.com")
    assert [d["account"] for d in await store.get_pending_drafts(2)] == ["b@x.com"]
    assert await store.list_open_items(2) == []

    removed = await store.delete_user(2)
    assert [a.email for a in removed] == ["b@x.com"]
    assert await store.get_user(2) is None
    assert not fake_db.data["accounts"] and not fake_db.data["drafts"]


async def test_migrate_legacy_single_user_data(store, fake_db):
    last_run = datetime(2025, 5, 1, 6, 0, tzinfo=timezone.utc)
    fake_db.data["settings"] = {"global": {"telegram_chat_id": 111, "timezone": "UTC"}}
    fake_db.data["state"] = {"global": {"last_run": last_run}}
    fake_db.data["accounts"] = {
        "owner@x.com": {"refresh_token": "t", "providers": ["gmail"], "enabled": True},
        "friend@x.com": {"refresh_token": "t", "providers": ["gmail"], "enabled": True, "chat_id": 222},
    }
    fake_db.data["drafts"] = {
        "d1": {"account": "owner@x.com", "status": "pending"},
        "d2": {"account": "friend@x.com", "status": "pending"},
    }
    fake_db.data["promises"] = {"p1": {"account": "friend@x.com", "status": "open", "text": "Send the deck",
                                       "source": "email"}}

    assert await store.migrate_legacy() is True
    assert await store.get_admin_id() == 111
    assert (await store.get_user(111)).is_admin
    assert (await store.get_user(222)).role == "user"
    owner_acc = await store.get_account("owner@x.com")
    assert owner_acc.user_id == 111 and owner_acc.last_run == last_run
    assert (await store.get_account("friend@x.com")).user_id == 222
    assert [d["id"] for d in await store.get_pending_drafts(111)] == ["d1"]
    assert [d["id"] for d in await store.get_pending_drafts(222)] == ["d2"]
    [item] = await store.list_open_items(222)  # legacy promise became an item
    assert (item.kind, item.title, item.source, item.account) == ("promise", "Send the deck", "email", "friend@x.com")
    assert "promises" not in fake_db.data or not fake_db.data["promises"]

    assert await store.migrate_legacy() is False  # idempotent


async def test_migrate_fresh_install_is_noop(store):
    assert await store.migrate_legacy() is True
    assert await store.get_admin_id() is None
    assert await store.migrate_legacy() is False


async def test_adopt_orphan_accounts(store, fake_db):
    fake_db.data["accounts"] = {"old@x.com": {"refresh_token": "t", "enabled": True}}
    fake_db.data["drafts"] = {"d1": {"account": "old@x.com", "status": "pending"}}
    await store.claim_admin(user(1))
    assert await store.adopt_orphan_accounts(1) == ["old@x.com"]
    assert (await store.get_account("old@x.com")).user_id == 1
    assert len(await store.get_pending_drafts(1)) == 1


async def test_legacy_mixed_case_account_id(store, fake_db):
    fake_db.data["accounts"] = {"Boss@Corp.com": {"refresh_token": "t", "enabled": True, "user_id": 1}}
    fake_db.data["drafts"] = {"d1": {"account": "Boss@Corp.com", "status": "pending", "user_id": 1}}
    acc = (await store.list_accounts(user_id=1))[0]
    await store.update_account(acc.email, {"last_run": datetime.now(timezone.utc)})  # must not raise NotFound
    assert (await store.get_account("Boss@Corp.com")).email == "Boss@Corp.com"
    await store.delete_account(acc.email)
    assert fake_db.data["accounts"] == {} and fake_db.data["drafts"] == {}


async def test_migration_lowercases_legacy_account_ids(store, fake_db):
    fake_db.data["settings"] = {"global": {"telegram_chat_id": 111}}
    fake_db.data["accounts"] = {"Boss@Corp.com": {"refresh_token": "t", "enabled": True}}
    fake_db.data["drafts"] = {"d1": {"account": "Boss@Corp.com", "status": "pending"}}
    await store.migrate_legacy()
    assert list(fake_db.data["accounts"]) == ["boss@corp.com"]
    assert fake_db.data["accounts"]["boss@corp.com"]["user_id"] == 111
    assert [d["account"] for d in await store.get_pending_drafts(111)] == ["boss@corp.com"]
    # re-linking after the OAuth client switch updates the same account
    result = await store.link_account(Account(email="boss@corp.com", user_id=111, refresh_token="new"))
    assert result == LinkResult.UPDATED and len(fake_db.data["accounts"]) == 1
