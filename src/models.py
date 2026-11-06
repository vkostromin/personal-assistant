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
    created_at: datetime
    status: str = "pending"


class Promise(BaseModel):
    text: str
    source: str
    source_detail: Optional[str] = None
    created_at: datetime
    status: str = "open"
    account: Optional[str] = None


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
    last_run: Optional[datetime] = None  # last time any provider was checked successfully
    provider_last_run: dict[str, datetime] = {}  # per provider ("gmail"/"chat") watermark
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


class ChatSummary(BaseModel):
    space_name: str
    missed_questions: list[str]
    promises: list[Promise]
    summary_items: list[str]


class EmailSummary(BaseModel):
    drafts: list[Draft]
    promises: list[Promise]
    summary_items: list[str]


class GlobalState(BaseModel):
    """Legacy (single-user) run state, only read during migration."""

    last_run: Optional[datetime] = None


class SettingsModel(BaseModel):
    """Legacy (single-user) settings, only read during migration."""

    telegram_chat_id: Optional[int] = None
    timezone: str = "UTC"
