from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class Draft(BaseModel):
    id: str
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


class Account(BaseModel):
    email: str
    refresh_token: str = ""
    providers: list[str] = ["gmail", "chat"]
    enabled: bool = True
    created_at: datetime = datetime.now()


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
    last_run: Optional[datetime] = None


class SettingsModel(BaseModel):
    telegram_chat_id: Optional[int] = None
    timezone: str = "UTC"
