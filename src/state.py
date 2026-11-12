"""Firestore persistence.

Collections
-----------
users/{telegram_user_id}   User (role admin|user, status active|disabled)
accounts/{email}           Google account linked to exactly one user (user_id)
drafts/{auto}              user_id, account, gmail_draft_id, ... status pending|sent|discarded
items/{auto}               promises / waiting-for / replies / to-dos (src/models.Item)
promises/{auto}            legacy, migrated to items (schema v3)
invites/{code}             single-use invitation codes
oauth_states/{state}       pending "connect Google account" links (single use)
usage/{telegram_user_id}   shared-LLM token usage in the current window
llm_keys/{telegram_user_id} user's own LLM provider + encrypted API key
telegram_updates/{update_id} seen webhook updates (dedupe of Telegram retries, TTL expires_at)
settings/app               admin_user_id, schema_version
settings/global, state/global   legacy single-user docs (read once by the migration)
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from src import config
from src.crypto import decrypt, encrypt
from src.models import Account, Invite, Item, OAuthState, User, UserLLMKey, utcnow

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class RedeemResult:
    OK = "ok"
    INVALID = "invalid"
    USED = "used"
    EXPIRED = "expired"
    ALREADY_REGISTERED = "already_registered"


class LinkResult:
    CREATED = "created"
    UPDATED = "updated"
    TAKEN = "taken"  # linked to another user


class FirestoreStore:
    def __init__(self, db: Any = None, transactional: Callable | None = None):
        self._db = db
        self._transactional = transactional or firestore.async_transactional

    @property
    def db(self):
        if self._db is None:
            self._db = firestore.AsyncClient(project=config.settings.project_id or None)
        return self._db

    async def _run_in_transaction(self, fn):
        """Run ``fn(transaction)`` atomically (retried by Firestore on contention)."""
        return await self._transactional(fn)(self.db.transaction())

    # ------------------------------------------------------------------ meta
    def _meta_ref(self):
        return self.db.collection("settings").document("app")

    async def get_meta(self) -> dict:
        doc = await self._meta_ref().get()
        return doc.to_dict() if doc.exists else {}

    async def get_admin_id(self) -> int | None:
        return (await self.get_meta()).get("admin_user_id")

    # ----------------------------------------------------------------- users
    @staticmethod
    def _user_from_doc(doc) -> User:
        data = doc.to_dict()
        data["id"] = int(doc.id)
        return User(**data)

    async def get_user(self, user_id: int) -> User | None:
        doc = await self.db.collection("users").document(str(user_id)).get()
        return self._user_from_doc(doc) if doc.exists else None

    async def save_user(self, user: User) -> None:
        await self.db.collection("users").document(str(user.id)).set(user.model_dump(exclude={"id"}))

    async def update_user(self, user_id: int, updates: dict) -> None:
        await self.db.collection("users").document(str(user_id)).update(updates)

    async def list_users(self) -> list[User]:
        return [self._user_from_doc(doc) async for doc in self.db.collection("users").stream()]

    async def claim_admin(self, user: User) -> bool:
        """Atomically make ``user`` the first admin. Returns False if an admin already exists."""
        meta_ref = self._meta_ref()
        user_ref = self.db.collection("users").document(str(user.id))
        admin = user.model_copy(update={"role": "admin", "status": "active"})

        async def txn(transaction):
            snap = await meta_ref.get(transaction=transaction)
            if snap.exists and snap.to_dict().get("admin_user_id"):
                return False
            transaction.set(meta_ref, {"admin_user_id": admin.id, "admin_since": utcnow()}, merge=True)
            transaction.set(user_ref, admin.model_dump(exclude={"id"}))
            return True

        return await self._run_in_transaction(txn)

    async def claim_run_slot(self, user_id: int, slot: datetime) -> bool:
        """Atomically mark ``slot`` as processed for the user. False if already claimed
        (e.g. overlapping /run calls on several instances)."""
        ref = self.db.collection("users").document(str(user_id))

        async def txn(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return False
            last = _aware(snap.to_dict().get("last_run_slot"))
            if last is not None and last >= slot:
                return False
            transaction.update(ref, {"last_run_slot": slot})
            return True

        return await self._run_in_transaction(txn)

    async def transfer_admin(self, from_id: int, to_id: int) -> bool:
        """Make ``to_id`` the admin and ``from_id`` a regular user (atomically)."""
        meta_ref = self._meta_ref()
        from_ref = self.db.collection("users").document(str(from_id))
        to_ref = self.db.collection("users").document(str(to_id))

        async def txn(transaction):
            meta = await meta_ref.get(transaction=transaction)
            target = await to_ref.get(transaction=transaction)
            if not meta.exists or meta.to_dict().get("admin_user_id") != from_id:
                return False
            if not target.exists or target.to_dict().get("status") != "active":
                return False
            transaction.update(to_ref, {"role": "admin"})
            transaction.update(from_ref, {"role": "user"})
            transaction.set(meta_ref, {"admin_user_id": to_id, "admin_since": utcnow()}, merge=True)
            return True

        return await self._run_in_transaction(txn)

    async def delete_user(self, user_id: int) -> list[Account]:
        """Delete a user and all their data. Returns removed accounts (for token revocation)."""
        accounts = await self.list_accounts(user_id=user_id, enabled_only=False)
        for acc in accounts:
            await self.delete_account(acc.email)
        for coll in ("drafts", "items", "promises"):
            async for doc in self.db.collection(coll).where(filter=FieldFilter("user_id", "==", user_id)).stream():
                await doc.reference.delete()
        await self.db.collection("usage").document(str(user_id)).delete()
        await self.db.collection("llm_keys").document(str(user_id)).delete()
        await self.db.collection("users").document(str(user_id)).delete()
        if await self.get_admin_id() == user_id:
            # the (only) admin left: the next /start sets the bot up again
            await self._meta_ref().set({"admin_user_id": None}, merge=True)
        return accounts

    # --------------------------------------------------------------- invites
    async def create_invite(self, created_by: int, note: str = "", ttl_hours: int | None = None) -> Invite:
        ttl = ttl_hours if ttl_hours is not None else config.settings.invite_ttl_hours
        now = utcnow()
        invite = Invite(
            code=secrets.token_urlsafe(12),
            created_by=created_by,
            created_at=now,
            expires_at=now + timedelta(hours=ttl),
            note=note,
        )
        await self.db.collection("invites").document(invite.code).set(invite.model_dump(exclude={"code"}))
        return invite

    async def list_invites(self, active_only: bool = True) -> list[Invite]:
        now = utcnow()
        result = []
        async for doc in self.db.collection("invites").stream():
            inv = Invite(code=doc.id, **doc.to_dict())
            if active_only and (inv.used_by is not None or _aware(inv.expires_at) < now):
                continue
            result.append(inv)
        return sorted(result, key=lambda i: i.created_at)

    async def revoke_invite(self, code: str) -> bool:
        ref = self.db.collection("invites").document(code)
        if not (await ref.get()).exists:
            return False
        await ref.delete()
        return True

    async def redeem_invite(self, code: str, user: User) -> tuple[str, Invite | None]:
        """Atomically consume an invite and register ``user``."""
        if not code or "/" in code:
            return RedeemResult.INVALID, None
        inv_ref = self.db.collection("invites").document(code)
        user_ref = self.db.collection("users").document(str(user.id))

        async def txn(transaction):
            inv_snap = await inv_ref.get(transaction=transaction)
            user_snap = await user_ref.get(transaction=transaction)
            if not inv_snap.exists:
                return RedeemResult.INVALID, None
            invite = Invite(code=code, **inv_snap.to_dict())
            if user_snap.exists:
                return RedeemResult.ALREADY_REGISTERED, invite
            if invite.used_by is not None:
                return RedeemResult.USED, invite
            if _aware(invite.expires_at) < utcnow():
                return RedeemResult.EXPIRED, invite
            new_user = user.model_copy(update={"role": "user", "status": "active", "invited_by": invite.created_by})
            transaction.set(user_ref, new_user.model_dump(exclude={"id"}))
            transaction.update(inv_ref, {"used_by": user.id, "used_at": utcnow()})
            return RedeemResult.OK, invite

        return await self._run_in_transaction(txn)

    # ----------------------------------------------------------- oauth state
    async def create_oauth_state(self, user_id: int, code_verifier: str, redirect_uri: str) -> OAuthState:
        now = utcnow()
        state = OAuthState(
            id=secrets.token_urlsafe(24),
            user_id=user_id,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
            created_at=now,
            expires_at=now + timedelta(minutes=config.settings.oauth_link_ttl_minutes),
        )
        await self.db.collection("oauth_states").document(state.id).set(state.model_dump(exclude={"id"}))
        return state

    async def get_oauth_state(self, state_id: str) -> OAuthState | None:
        if not state_id or "/" in state_id:
            return None
        doc = await self.db.collection("oauth_states").document(state_id).get()
        if not doc.exists:
            return None
        state = OAuthState(id=doc.id, **doc.to_dict())
        if _aware(state.expires_at) < utcnow():
            return None
        return state

    async def consume_oauth_state(self, state_id: str) -> OAuthState | None:
        """Return the state and delete it (single use). None if missing or expired."""
        if not state_id or "/" in state_id:
            return None
        ref = self.db.collection("oauth_states").document(state_id)

        async def txn(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return None
            transaction.delete(ref)
            return OAuthState(id=state_id, **snap.to_dict())

        state = await self._run_in_transaction(txn)
        if state is None or _aware(state.expires_at) < utcnow():
            return None
        return state

    # -------------------------------------------------------------- accounts
    def _account_from_doc(self, doc) -> Account:
        data = doc.to_dict()
        data["email"] = doc.id
        if data.get("refresh_token"):
            data["refresh_token"] = decrypt(data["refresh_token"], config.settings.token_encryption_key)
        return Account(**data)

    def _account_to_doc(self, account: Account) -> dict:
        data = account.model_dump(exclude={"email"})
        if data.get("refresh_token"):
            data["refresh_token"] = encrypt(data["refresh_token"], config.settings.token_encryption_key)
        return data

    async def get_account(self, email: str) -> Account | None:
        """Look up by exact doc id (legacy ids may be mixed-case), then by normalized email."""
        if not email or "/" in email:
            return None
        for doc_id in dict.fromkeys([email, normalize_email(email)]):
            doc = await self.db.collection("accounts").document(doc_id).get()
            if doc.exists:
                return self._account_from_doc(doc)
        return None

    async def list_accounts(self, user_id: int | None = None, enabled_only: bool = True) -> list[Account]:
        query = self.db.collection("accounts")
        if user_id is not None:
            query = query.where(filter=FieldFilter("user_id", "==", user_id))
        result = []
        async for doc in query.stream():
            acc = self._account_from_doc(doc)
            if enabled_only and not acc.enabled:
                continue
            result.append(acc)
        return sorted(result, key=lambda a: a.email)

    async def link_account(self, account: Account) -> str:
        """Create or refresh a Google account link for ``account.user_id``.

        A Google account can be linked to only one user; linking an address that
        belongs to somebody else returns ``LinkResult.TAKEN``.
        """
        email = normalize_email(account.email)
        ref = self.db.collection("accounts").document(email)

        async def txn(transaction):
            snap = await ref.get(transaction=transaction)
            if snap.exists:
                existing = snap.to_dict()
                owner = existing.get("user_id")
                if owner is not None and owner != account.user_id:
                    return LinkResult.TAKEN
                transaction.update(ref, {
                    "user_id": account.user_id,
                    "refresh_token": encrypt(account.refresh_token, config.settings.token_encryption_key),
                    "providers": account.providers,
                    "enabled": True,
                    "needs_reauth": False,
                    "google_sub": account.google_sub or existing.get("google_sub", ""),
                    "chat_send": account.chat_send,
                })
                return LinkResult.UPDATED
            new = account.model_copy(update={"email": email})
            transaction.set(ref, self._account_to_doc(new))
            return LinkResult.CREATED

        return await self._run_in_transaction(txn)

    # update/delete take the exact document id (``Account.email`` as loaded)
    async def users_with_backlog(self) -> set[int]:
        query = self.db.collection("accounts").where(filter=FieldFilter("backlog", "==", True))
        return {doc.to_dict().get("user_id") async for doc in query.stream()}

    async def update_account(self, email: str, updates: dict) -> None:
        await self.db.collection("accounts").document(email).update(updates)

    async def delete_account(self, email: str) -> None:
        """Delete an account link plus its drafts and items."""
        for coll in ("drafts", "items", "promises"):
            async for doc in self.db.collection(coll).where(filter=FieldFilter("account", "==", email)).stream():
                await doc.reference.delete()
        await self.db.collection("accounts").document(email).delete()

    # ---------------------------------------------------------------- drafts
    async def save_draft(self, user_id: int, draft_data: dict) -> str:
        doc_ref = self.db.collection("drafts").document()
        await doc_ref.set({**draft_data, "user_id": user_id, "created_at": utcnow()})
        return doc_ref.id

    async def get_pending_drafts(self, user_id: int) -> list[dict]:
        query = (
            self.db.collection("drafts")
            .where(filter=FieldFilter("user_id", "==", user_id))
            .where(filter=FieldFilter("status", "==", "pending"))
        )
        result = []
        async for doc in query.stream():
            data = doc.to_dict()
            data["id"] = doc.id
            result.append(data)
        # stable numbering for /drafts and /rewrite N
        return sorted(result, key=lambda d: (_aware(d.get("created_at")) or utcnow(), d["id"]))

    async def update_draft(self, draft_id: str, updates: dict) -> None:
        await self.db.collection("drafts").document(draft_id).update(updates)

    async def delete_draft(self, draft_id: str) -> None:
        await self.db.collection("drafts").document(draft_id).delete()

    async def get_draft(self, draft_id: str) -> dict | None:
        if not draft_id or "/" in draft_id:
            return None
        doc = await self.db.collection("drafts").document(draft_id).get()
        if not doc.exists:
            return None
        return {**doc.to_dict(), "id": doc.id}

    async def drafts_due_for_reminder(self, now: datetime) -> list[dict]:
        query = self.db.collection("drafts").where(filter=FieldFilter("next_remind_at", "<=", now))
        return [{**doc.to_dict(), "id": doc.id} async for doc in query.stream()]

    # ----------------------------------------------------------------- items
    @staticmethod
    def _item_from_doc(doc) -> Item:
        return Item(**{**doc.to_dict(), "id": doc.id})

    async def create_item(self, item: Item) -> str:
        ref = self.db.collection("items").document()
        await ref.set(item.model_dump(exclude={"id"}))
        return ref.id

    async def get_item(self, item_id: str) -> Item | None:
        if not item_id or "/" in item_id:
            return None
        doc = await self.db.collection("items").document(item_id).get()
        return self._item_from_doc(doc) if doc.exists else None

    async def update_item(self, item_id: str, updates: dict) -> None:
        await self.db.collection("items").document(item_id).update(updates)

    async def list_open_items(self, user_id: int) -> list[Item]:
        query = (
            self.db.collection("items")
            .where(filter=FieldFilter("user_id", "==", user_id))
            .where(filter=FieldFilter("status", "==", "open"))
        )
        items = [self._item_from_doc(doc) async for doc in query.stream()]
        return sorted(items, key=lambda i: (_aware(i.created_at), i.id))

    async def find_open_item(self, user_id: int, fingerprint: str) -> Item | None:
        query = (
            self.db.collection("items")
            .where(filter=FieldFilter("user_id", "==", user_id))
            .where(filter=FieldFilter("fingerprint", "==", fingerprint))
            .where(filter=FieldFilter("status", "==", "open"))
        )
        async for doc in query.stream():
            return self._item_from_doc(doc)
        return None

    async def items_due_for_reminder(self, now: datetime) -> list[Item]:
        """Single-field range query (automatic index). Closed items have next_remind_at = None."""
        query = self.db.collection("items").where(filter=FieldFilter("next_remind_at", "<=", now))
        return [self._item_from_doc(doc) async for doc in query.stream()]

    # ------------------------------------------------------ webhook dedupe
    async def claim_update(self, update_id: int) -> bool:
        """Record a Telegram update id; False if it was seen before.

        Telegram re-delivers an update when the webhook answers slowly (LLM calls) or
        fails, which would otherwise run a command — e.g. "send draft" — twice."""
        from google.api_core.exceptions import AlreadyExists

        ref = self.db.collection("telegram_updates").document(str(update_id))
        try:
            await ref.create({"created_at": utcnow(), "expires_at": utcnow() + timedelta(days=2)})
        except AlreadyExists:
            return False
        return True

    # ------------------------------------------------------- pending actions
    async def set_pending_action(self, user_id: int, action: dict | None) -> None:
        await self.db.collection("users").document(str(user_id)).update({"pending_action": action})

    # ------------------------------------------------------------ LLM usage
    async def get_usage(self, user_id: int) -> dict:
        doc = await self.db.collection("usage").document(str(user_id)).get()
        return doc.to_dict() if doc.exists else {}

    async def reset_usage(self, user_id: int, window_start: datetime, tokens: int = 0, own_tokens: int = 0) -> None:
        await self.db.collection("usage").document(str(user_id)).set(
            {"window_start": window_start, "tokens": tokens, "own_tokens": own_tokens}
        )

    async def increment_usage(self, user_id: int, tokens: int, own: bool = False) -> None:
        """Atomic increment — several requests of one user may run concurrently."""
        field = "own_tokens" if own else "tokens"
        await self.db.collection("usage").document(str(user_id)).set({field: firestore.Increment(tokens)}, merge=True)

    # ------------------------------------------------------------- LLM keys
    async def get_llm_key(self, user_id: int) -> UserLLMKey | None:
        doc = await self.db.collection("llm_keys").document(str(user_id)).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        data["api_key"] = decrypt(data.get("api_key", ""), config.settings.token_encryption_key)
        return UserLLMKey(**data)

    async def set_llm_key(self, user_id: int, key: UserLLMKey) -> None:
        data = key.model_dump()
        data["api_key"] = encrypt(key.api_key, config.settings.token_encryption_key)
        await self.db.collection("llm_keys").document(str(user_id)).set(data)

    async def delete_llm_key(self, user_id: int) -> bool:
        ref = self.db.collection("llm_keys").document(str(user_id))
        if not (await ref.get()).exists:
            return False
        await ref.delete()
        return True

    # -------------------------------------------------------------- migration
    async def adopt_orphan_accounts(self, user_id: int) -> list[str]:
        """Assign accounts that have no owner (added by the old auth script) to ``user_id``."""
        adopted = []
        async for doc in self.db.collection("accounts").stream():
            if doc.to_dict().get("user_id") is None:
                await doc.reference.update({"user_id": user_id})
                adopted.append(doc.id)
        if adopted:
            await self._assign_orphan_records()
        return adopted

    async def _assign_orphan_records(self, fallback_user_id: int | None = None) -> None:
        owners: dict[str, int] = {}
        async for doc in self.db.collection("accounts").stream():
            if doc.to_dict().get("user_id") is not None:
                owners[doc.id] = doc.to_dict()["user_id"]
        for coll in ("drafts", "promises"):
            async for doc in self.db.collection(coll).stream():
                data = doc.to_dict()
                if data.get("user_id") is not None:
                    continue
                owner = owners.get(normalize_email(data.get("account") or "")) or fallback_user_id
                if owner is not None:
                    await doc.reference.update({"user_id": owner})

    async def _normalize_account_ids(self) -> None:
        """Legacy account ids were typed by hand and may be mixed-case; Google returns
        lower-case emails, so re-linking must hit the same document."""
        async for doc in self.db.collection("accounts").stream():
            email = normalize_email(doc.id)
            if email == doc.id:
                continue
            target = self.db.collection("accounts").document(email)
            if not (await target.get()).exists:
                await target.set(doc.to_dict())
            await doc.reference.delete()
            for coll in ("drafts", "promises"):
                async for rec in self.db.collection(coll).where(filter=FieldFilter("account", "==", doc.id)).stream():
                    await rec.reference.update({"account": email})
            log.info("Migration: account id %s -> %s", doc.id, email)

    async def migrate_legacy(self) -> bool:
        """One-time, idempotent migration from the single-user data layout.

        * legacy owner (settings/global.telegram_chat_id) becomes the admin
        * accounts get ``user_id`` (legacy ``chat_id`` or the owner) and the
          legacy global ``last_run``
        * drafts / promises get ``user_id`` of their account's owner
        """
        meta = await self.get_meta()
        if meta.get("schema_version", 0) >= SCHEMA_VERSION:
            return False

        legacy_settings = await self.db.collection("settings").document("global").get()
        owner_id = legacy_settings.to_dict().get("telegram_chat_id") if legacy_settings.exists else None
        legacy_state = await self.db.collection("state").document("global").get()
        legacy_last_run = legacy_state.to_dict().get("last_run") if legacy_state.exists else None

        await self._normalize_account_ids()

        if owner_id and not meta.get("admin_user_id"):
            if await self.claim_admin(User(id=owner_id, chat_id=owner_id)):
                log.info("Migration: legacy owner %s is now admin", owner_id)

        async for doc in self.db.collection("accounts").stream():
            data = doc.to_dict()
            updates: dict = {}
            if data.get("user_id") is None:
                owner = data.get("chat_id") or owner_id
                if owner:
                    updates["user_id"] = owner
                    if await self.get_user(owner) is None:
                        await self.save_user(User(id=owner, chat_id=owner))
            if data.get("last_run") is None and legacy_last_run is not None:
                updates["last_run"] = legacy_last_run
            if updates:
                await doc.reference.update(updates)
        await self._assign_orphan_records(fallback_user_id=owner_id)

        await self._promises_to_items()

        await self._meta_ref().set({"schema_version": SCHEMA_VERSION}, merge=True)
        log.info("Migration to schema v%d complete", SCHEMA_VERSION)
        return True

    async def _promises_to_items(self) -> None:
        """v3: free-text promises become items (kind "promise", no due date)."""
        moved = 0
        async for doc in self.db.collection("promises").stream():
            data = doc.to_dict()
            if data.get("user_id") is not None and data.get("status", "open") == "open" and data.get("text"):
                source = data.get("source") if data.get("source") in ("email", "chat") else "manual"
                item = Item(
                    user_id=data["user_id"], kind="promise", title=data["text"][:300], source=source,
                    account=data.get("account") or "", detail=data.get("source_detail") or "",
                    created_at=_aware(data.get("created_at")) or utcnow(),
                )
                await self.create_item(item)
                moved += 1
            await doc.reference.delete()
        if moved:
            log.info("Migration: %d open promises moved to items", moved)


_store: FirestoreStore | None = None


def get_store() -> FirestoreStore:
    global _store
    if _store is None:
        _store = FirestoreStore()
    return _store


def set_store(store: FirestoreStore | None) -> None:
    global _store
    _store = store
