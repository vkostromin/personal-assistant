"""Firestore persistence.

Collections
-----------
users/{telegram_user_id}   User (role admin|user, status active|disabled)
accounts/{email}           Google account linked to exactly one user (user_id)
drafts/{auto}              user_id, account, ... status pending|...
promises/{auto}            user_id, account, ... status open|done
invites/{code}             single-use invitation codes
usage/{telegram_user_id}   LLM usage in the current window (per-user limits)
llm_keys/{telegram_user_id} user's own LLM provider/key (encrypted) -> no limits
oauth_states/{state}       pending "connect Google account" links (single use)
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
from src.models import Account, Invite, OAuthState, User, utcnow

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2


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

    async def delete_user(self, user_id: int) -> list[Account]:
        """Delete a user and all their data. Returns removed accounts (for token revocation)."""
        accounts = await self.list_accounts(user_id=user_id, enabled_only=False)
        for acc in accounts:
            await self.delete_account(acc.email)
        for coll in ("drafts", "promises"):
            async for doc in self.db.collection(coll).where(filter=FieldFilter("user_id", "==", user_id)).stream():
                await doc.reference.delete()
        for coll in ("llm_keys", "usage"):
            await self.db.collection(coll).document(str(user_id)).delete()
        await self.db.collection("users").document(str(user_id)).delete()
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
                })
                return LinkResult.UPDATED
            new = account.model_copy(update={"email": email})
            transaction.set(ref, self._account_to_doc(new))
            return LinkResult.CREATED

        return await self._run_in_transaction(txn)

    # update/delete take the exact document id (``Account.email`` as loaded)
    async def update_account(self, email: str, updates: dict) -> None:
        await self.db.collection("accounts").document(email).update(updates)

    async def delete_account(self, email: str) -> None:
        """Delete an account link plus its drafts and promises."""
        for coll in ("drafts", "promises"):
            async for doc in self.db.collection(coll).where(filter=FieldFilter("account", "==", email)).stream():
                await doc.reference.delete()
        await self.db.collection("accounts").document(email).delete()

    # ------------------------------------------------------ drafts/promises
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

    async def save_promise(self, user_id: int, promise_data: dict) -> str:
        doc_ref = self.db.collection("promises").document()
        await doc_ref.set({**promise_data, "user_id": user_id, "created_at": utcnow(), "status": "open"})
        return doc_ref.id

    async def get_open_promises(self, user_id: int) -> list[dict]:
        query = (
            self.db.collection("promises")
            .where(filter=FieldFilter("user_id", "==", user_id))
            .where(filter=FieldFilter("status", "==", "open"))
        )
        result = []
        async for doc in query.stream():
            data = doc.to_dict()
            data["id"] = doc.id
            result.append(data)
        return sorted(result, key=lambda d: (_aware(d.get("created_at")) or utcnow(), d["id"]))

    async def close_promise(self, promise_id: str) -> None:
        await self.db.collection("promises").document(promise_id).update({"status": "done"})

    # ------------------------------------------------------------ LLM usage
    # usage/{user_id}: window_start, tokens, emails. A window starts at the user's
    # scheduled run (or at first use after the previous window expired).
    async def get_usage(self, user_id: int) -> dict:
        doc = await self.db.collection("usage").document(str(user_id)).get()
        return doc.to_dict() if doc.exists else {}

    async def start_usage_window(self, user_id: int, now: datetime | None = None) -> None:
        await self.db.collection("usage").document(str(user_id)).set(
            {"window_start": now or utcnow(), "tokens": 0, "emails": 0}
        )

    async def add_usage(self, user_id: int, tokens: int = 0, emails: int = 0) -> None:
        if not tokens and not emails:
            return
        ref = self.db.collection("usage").document(str(user_id))
        window = timedelta(hours=config.settings.usage_window_hours)

        async def txn(transaction):
            snap = await ref.get(transaction=transaction)
            data = snap.to_dict() if snap.exists else {}
            now = utcnow()
            start = _aware(data.get("window_start"))
            if start is None or start + window <= now:
                data = {"window_start": now, "tokens": 0, "emails": 0}
            data["tokens"] = int(data.get("tokens", 0)) + int(tokens)
            data["emails"] = int(data.get("emails", 0)) + int(emails)
            transaction.set(ref, data)

        await self._run_in_transaction(txn)

    # ------------------------------------------------------ personal LLM keys
    async def get_user_llm(self, user_id: int) -> dict | None:
        doc = await self.db.collection("llm_keys").document(str(user_id)).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        data["api_key"] = decrypt(data.get("api_key", ""), config.settings.token_encryption_key)
        return data

    async def set_user_llm(self, user_id: int, provider: str, api_key: str, model: str) -> None:
        await self.db.collection("llm_keys").document(str(user_id)).set({
            "provider": provider,
            "api_key": encrypt(api_key, config.settings.token_encryption_key),
            "model": model,
            "updated_at": utcnow(),
        })

    async def delete_user_llm(self, user_id: int) -> bool:
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

        await self._meta_ref().set({"schema_version": SCHEMA_VERSION}, merge=True)
        log.info("Migration to schema v%d complete", SCHEMA_VERSION)
        return True


_store: FirestoreStore | None = None


def get_store() -> FirestoreStore:
    global _store
    if _store is None:
        _store = FirestoreStore()
    return _store


def set_store(store: FirestoreStore | None) -> None:
    global _store
    _store = store
