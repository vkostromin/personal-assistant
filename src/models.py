from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Draft(BaseModel):
    account: str
    to: str
    subject: str
    body: str
    thread_id: Optional[str] = None
    original_email_id: Optional[str] = None
    in_reply_to: Optional[str] = None  # Message-ID header of the email being answered
    priority: str = "medium"  # high | medium | low
    created_at: datetime
    status: str = "pending"  # pending | sent | discarded


ItemKind = Literal["promise", "waiting", "reply", "todo"]
Priority = Literal["high", "medium", "low"]


class Item(BaseModel):
    """Something that needs attention: the core of "what should I do now".

    kind:
      promise — I promised someone to do something (outbound)
      waiting — someone promised me / owes me an answer (inbound)
      reply   — someone needs an answer from me and there's no draft (e.g. Chat question)
      todo    — anything else I need to do (manual tasks, action items)
    """

    id: str = ""
    user_id: int
    kind: ItemKind
    title: str
    counterparty: str = ""
    due_at: Optional[datetime] = None
    priority: Priority = "medium"
    status: Literal["open", "done", "cancelled"] = "open"
    source: Literal["email", "sent", "chat", "manual"] = "manual"
    account: str = ""
    thread_id: Optional[str] = None  # Gmail thread
    in_reply_to: Optional[str] = None  # Message-ID to answer when following up
    subject: str = ""
    space: Optional[str] = None  # Google Chat space name
    chat_thread: Optional[str] = None  # Chat thread to reply in ("spaces/X/threads/Y")
    chat_message: Optional[str] = None  # Chat message the item came from
    link: str = ""
    detail: str = ""
    snoozed_until: Optional[datetime] = None
    next_remind_at: Optional[datetime] = None
    remind_stage: int = 0  # see src/items.py
    last_reminded_at: Optional[datetime] = None
    fingerprint: str = ""
    resolution: str = ""
    reviewed_at: Optional[datetime] = None  # last time the user handled it in /review
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[datetime] = None


class User(BaseModel):
    """A Telegram user of the bot. Document id = Telegram user id."""

    id: int
    chat_id: int
    name: str = ""
    username: str = ""
    role: Literal["admin", "user"] = "user"
    status: Literal["active", "disabled"] = "active"
    invited_by: Optional[int] = None
    # Shared-LLM token limit per usage window: None = default (USER_TOKEN_LIMIT), 0 = unlimited
    token_limit: Optional[int] = None
    # IANA name ("Asia/Jakarta") or fixed offset ("+05:30"); "" = DEFAULT_TIMEZONE
    timezone: str = ""
    # Last summary slot (UTC) this user was processed for — see src/schedule.py
    last_run_slot: Optional[datetime] = None
    # Waiting for free text from the user, e.g. {"type": "rewrite", "ref": draft_id, "expires_at": ...}
    pending_action: Optional[dict] = None
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def display_name(self) -> str:
        if self.username:
            return f"{self.name} (@{self.username})" if self.name else f"@{self.username}"
        return self.name or str(self.id)


class Account(BaseModel):
    """A linked Google account. Document id = lower-cased email."""

    email: str
    user_id: Optional[int] = None  # owner (Telegram user id)
    refresh_token: str = ""
    providers: list[str] = ["gmail", "chat"]
    enabled: bool = True
    needs_reauth: bool = False
    google_sub: str = ""  # Google account id (= Chat "users/{id}"), to recognise my own Chat messages
    chat_send: bool = False  # granted chat.messages.create → can reply to Chat from Telegram
    last_run: Optional[datetime] = None  # last time any provider was checked successfully
    provider_last_run: dict[str, datetime] = {}  # per provider ("gmail"/"gmail_sent"/"chat") watermark
    # Gmail ids processed exactly at the watermark (messages sharing its internalDate), to skip next time
    provider_seen_ids: dict[str, list[str]] = {}
    backlog: bool = False  # a run stopped before all new mail was processed → continue next hour
    created_at: datetime = Field(default_factory=utcnow)
    chat_id: Optional[int] = None  # legacy field from the single-user version


class UserLLMKey(BaseModel):
    """A user's own LLM API key (stored encrypted in llm_keys/{user_id})."""

    provider: str
    model: str = ""
    api_key: str
    created_at: datetime = Field(default_factory=utcnow)


class Invite(BaseModel):
    code: str
    created_by: int
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    note: str = ""
    used_by: Optional[int] = None
    used_at: Optional[datetime] = None


class OAuthState(BaseModel):
    """Pending Google OAuth link started from Telegram (single use, short lived)."""

    id: str
    user_id: int
    code_verifier: str
    redirect_uri: str
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime


class GlobalState(BaseModel):
    """Legacy (single-user) run state, only read during migration."""

    last_run: Optional[datetime] = None


class SettingsModel(BaseModel):
    """Legacy (single-user) settings, only read during migration."""

    telegram_chat_id: Optional[int] = None
    timezone: str = "UTC"
