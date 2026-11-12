import re
import json
import os
from pathlib import Path

from dotenv import dotenv_values

_DIR = Path(__file__).parent
# tests/.env (local, git-ignored) or the committed tests/.env.example
ENV: dict[str, str] = {
    k: v or ""
    for k, v in dotenv_values(_DIR / ".env" if (_DIR / ".env").exists() else _DIR / ".env.example").items()
}
os.environ.setdefault("PROJECT_ID", ENV.get("PROJECT_ID", "test-project"))

import pytest  # noqa: E402

from src import config, state  # noqa: E402
from src.config import Settings  # noqa: E402
from tests.fake_firestore import FakeAsyncClient  # noqa: E402

BASE_URL = ENV["PUBLIC_URL"]
CLIENT_ID = ENV["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = ENV["GOOGLE_CLIENT_SECRET"]
WEBHOOK_SECRET = ENV["TELEGRAM_WEBHOOK_SECRET"]
RUN_SECRET = ENV["RUN_SECRET"]
BOT_USERNAME = ENV["TELEGRAM_BOT_USERNAME"]
WEBHOOK_HEADERS = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}


def make_settings(**overrides) -> Settings:
    """Settings from the test env file only (the shell environment can't leak in), plus overrides."""
    values = {k.lower(): v for k, v in ENV.items()}
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture(autouse=True)
def test_settings(monkeypatch):
    s = make_settings()
    monkeypatch.setattr(config, "settings", s)
    return s


@pytest.fixture
def fake_db():
    return FakeAsyncClient()


@pytest.fixture(autouse=True)
def store(fake_db):
    st = state.FirestoreStore(db=fake_db)
    state.set_store(st)
    yield st
    state.set_store(None)


class Outbox(list):
    """Captured Telegram messages: list of (chat_id, text, reply_markup)."""

    def __init__(self):
        super().__init__()
        self.deleted = []
        self.edits = []
        self.answers = []

    def to(self, chat_id):
        return [m for m in self if m[0] == chat_id]

    def last(self, chat_id):
        msgs = self.to(chat_id)
        assert msgs, f"no message sent to {chat_id}"
        return msgs[-1]

    def text(self, chat_id):
        """All text sent to ``chat_id`` joined (summary + focus + notes...)."""
        return "\n".join(m[1] for m in self.to(chat_id))


@pytest.fixture(autouse=True)
def outbox(monkeypatch):
    box = Outbox()

    async def fake_send(token, chat_id, text, reply_markup=None):
        box.append((chat_id, text, reply_markup))
        return len(box)

    async def fake_edit(token, chat_id, message_id, text=None, reply_markup=None):
        box.edits.append((chat_id, message_id, text, reply_markup))
        return True

    async def fake_answer(token, callback_id, text=""):
        box.answers.append((callback_id, text))

    async def fake_revoke(token):
        return True

    for module in ("src.bot", "src.runner", "src.callbacks", "src.reminders"):
        monkeypatch.setattr(f"{module}.send_message", fake_send)
    monkeypatch.setattr("src.callbacks.edit_message", fake_edit)
    monkeypatch.setattr("src.callbacks.answer_callback", fake_answer)
    async def fake_delete(token, chat_id, message_id):
        box.deleted.append((chat_id, message_id))
        return True

    monkeypatch.setattr("src.bot.revoke_token", fake_revoke)
    monkeypatch.setattr("src.bot.delete_message", fake_delete)
    return box


class FakeLLM:
    """Replaces the HTTP call to the LLM provider; the real prompt/parsing/quota code runs."""

    def __init__(self):
        self.calls = []  # (LLMConfig, prompt)
        self.tokens_per_call = 1000
        self.fail_with = None  # LLMError to raise
        self.canned = {}  # prompt substring -> response content
        self.responses = {}

    async def __call__(self, cfg, messages, max_tokens=8000):
        prompt = messages[-1]["content"]
        self.calls.append((cfg, prompt))
        if self.fail_with:
            raise self.fail_with
        if prompt in self.responses:
            return self.responses[prompt], self.tokens_per_call
        for needle, content in self.canned.items():
            if needle in prompt:
                return content, self.tokens_per_call
        if "Analyze these emails received by" in prompt:
            account = prompt.split("Analyze these emails received by ")[1].split(".\n")[0]
            first = re.search(r"Message ID: (\S+)", prompt)  # reply to the email actually shown
            msg_id = first.group(1) if first else "m1"
            content = json.dumps({
                "drafts": [{"to": "x@y.com", "subject": f"Re: {account}", "body": "b", "original_email_id": msg_id,
                            "priority": "high"}],
                "items": [{"kind": "waiting", "title": f"promise from {account}", "counterparty": "x@y.com",
                           "due_at": None, "priority": "medium", "original_email_id": msg_id}],
                "updates": [],
                "summary_items": [f"email item for {account}"],
            })
        elif "emails I (" in prompt:
            content = json.dumps({"items": [], "updates": []})
        elif "Google Chat messages" in prompt:
            content = json.dumps({"items": [{"kind": "reply", "title": "Answer q?", "counterparty": "a"}],
                                  "updates": [], "summary_items": ["chat item"]})
        elif "Compose a concise summary" in prompt:
            content = "SUMMARY\n" + prompt
        elif "Rewrite this email draft" in prompt:
            content = json.dumps({"subject": "Rewritten subject", "body": "Rewritten body"})
        elif "Turn it into one task" in prompt:
            content = json.dumps({"kind": "todo", "title": "Parsed task", "counterparty": "", "due_at": None,
                                  "priority": "high"})
        elif "Convert this deadline" in prompt:
            content = json.dumps({"due_at": "2030-01-10T15:00:00+00:00"})
        elif "follow-up email" in prompt:
            content = json.dumps({"subject": "Re: numbers", "body": "Just checking in"})
        else:
            content = "ANSWER"
        return content, self.tokens_per_call

    def prompts(self, needle):
        return [p for _, p in self.calls if needle in p]


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    llm = FakeLLM()
    monkeypatch.setattr("src.llm_client.chat_completion", llm)
    monkeypatch.setattr("src.bot.chat_completion", llm)
    return llm


class FakeGmailDrafts:
    """Gmail draft state seen by draft sync / deletion: which Gmail drafts are gone and why."""

    def __init__(self):
        self.gone: dict[str, str] = {}  # gmail_draft_id -> "sent" | "discarded"
        self.deleted: list[str] = []
        self.check_calls = 0

    def check_drafts(self, account, client_id, client_secret, drafts):
        self.check_calls += 1
        return {d["id"]: self.gone[d["gmail_draft_id"]] for d in drafts if d.get("gmail_draft_id") in self.gone}

    def delete_gmail_draft(self, account, client_id, client_secret, draft_id):
        self.deleted.append(draft_id)
        return True


@pytest.fixture(autouse=True)
def gmail_drafts(monkeypatch):
    fake = FakeGmailDrafts()
    monkeypatch.setattr("src.drafts_sync.check_drafts", fake.check_drafts)
    monkeypatch.setattr("src.bot.check_drafts", fake.check_drafts)
    monkeypatch.setattr("src.actions.delete_gmail_draft", fake.delete_gmail_draft)
    return fake


class FakeMailbox:
    """Gmail stand-in for list_message_ids / get_messages with real time filtering."""

    def __init__(self):
        self.messages: dict[tuple[str, str], list[dict]] = {}  # (email, "inbox"|"sent") -> messages
        self.list_calls: list[dict] = []
        self.get_calls: list[list[str]] = []
        self.fail: dict[str, Exception] = {}  # email -> exception raised on listing
        self._n = 0

    def add(self, email, *, sent=False, at=None, count=1, **fields):
        from datetime import datetime, timezone
        at = at or datetime.now(timezone.utc)
        added = []
        for _ in range(count):
            self._n += 1
            msg = {"id": f"m{self._n}", "thread_id": f"t{self._n}", "internal_ms": int(at.timestamp() * 1000),
                   "from": "x@y.com", "to": email, "subject": f"subject {self._n}", "date": "",
                   "message_id_header": f"<m{self._n}@mail>", "body": "?"}
            msg.update(fields)
            self.messages.setdefault((email, "sent" if sent else "inbox"), []).append(msg)
            added.append(msg)
        return added

    def list_message_ids(self, account, client_id, client_secret, since, sent=False, cap=None):
        self.list_calls.append({"email": account.email, "since": since, "sent": sent, "cap": cap})
        if account.email in self.fail and not sent:
            raise self.fail[account.email]
        after_ms = (int(since.timestamp()) - 1) * 1000
        msgs = [m for m in self.messages.get((account.email, "sent" if sent else "inbox"), []) if m["internal_ms"] > after_ms]
        ids = [m["id"] for m in sorted(msgs, key=lambda m: m["internal_ms"], reverse=True)]
        if cap is not None and len(ids) > cap:
            return ids[:cap], True
        return ids, False

    def get_messages(self, account, client_id, client_secret, ids):
        self.get_calls.append(list(ids))
        everything = {m["id"]: m for msgs in self.messages.values() for m in msgs}
        return sorted((dict(everything[i]) for i in ids if i in everything), key=lambda m: m["internal_ms"])


@pytest.fixture
def mailbox(monkeypatch):
    box = FakeMailbox()
    monkeypatch.setattr("src.runner.list_message_ids", box.list_message_ids)
    monkeypatch.setattr("src.runner.get_messages", box.get_messages)
    return box
