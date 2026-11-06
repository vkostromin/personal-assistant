import os

os.environ.setdefault("PROJECT_ID", "test-project")

import pytest  # noqa: E402

from src import config, state  # noqa: E402
from src.config import Settings  # noqa: E402
from tests.fake_firestore import FakeAsyncClient  # noqa: E402

BASE_URL = "https://lola.example.run.app"


def make_settings(**overrides) -> Settings:
    values = dict(
        project_id="test-project",
        google_client_id="test-client-id",
        google_client_secret="test-client-secret",
        token_encryption_key="test-encryption-key-1234567890123456",
        telegram_webhook_secret="test-webhook-secret",
        run_secret="test-run-secret",
        telegram_bot_token="test-bot-token",
        telegram_bot_username="lola_test_bot",
        public_url=BASE_URL,
        admin_telegram_id="",
        gemini_api_key="server-gemini-key",
        deepseek_api_key="",
        openai_api_key="",
    )
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

    deleted: list

    def __init__(self):
        super().__init__()
        self.deleted = []  # (chat_id, message_id)

    def to(self, chat_id):
        return [m for m in self if m[0] == chat_id]

    def last(self, chat_id):
        msgs = self.to(chat_id)
        assert msgs, f"no message sent to {chat_id}"
        return msgs[-1]


@pytest.fixture(autouse=True)
def outbox(monkeypatch):
    box = Outbox()

    async def fake_send(token, chat_id, text, reply_markup=None):
        box.append((chat_id, text, reply_markup))
        return True

    async def fake_revoke(token):
        return True

    async def fake_delete(token, chat_id, message_id):
        box.deleted.append((chat_id, message_id))
        return True

    monkeypatch.setattr("src.bot.send_message", fake_send)
    monkeypatch.setattr("src.runner.send_message", fake_send)
    monkeypatch.setattr("src.bot.revoke_token", fake_revoke)
    monkeypatch.setattr("src.bot.delete_message", fake_delete)
    return box
