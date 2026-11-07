from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src import config
from src.main import app
from tests.conftest import make_settings


@pytest.fixture
def client():
    return TestClient(app)


def use_settings(monkeypatch, **kw):
    monkeypatch.setattr(config, "settings", make_settings(**kw))


class TestWebhookAuth:
    def test_webhook_rejects_missing_secret_header(self, client, monkeypatch):
        use_settings(monkeypatch, telegram_webhook_secret="correct-secret")
        response = client.post("/webhook", json={"message": {}})
        assert response.json() == {"ok": False}

    def test_webhook_rejects_wrong_secret(self, client, monkeypatch, outbox):
        use_settings(monkeypatch, telegram_webhook_secret="correct-secret")
        response = client.post(
            "/webhook",
            json={"message": {"chat": {"id": 123, "type": "private"}, "from": {"id": 123}, "text": "/start"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
        )
        assert response.json() == {"ok": False}
        assert list(outbox) == []  # nothing processed

    def test_webhook_allows_no_secret_configured(self, client, monkeypatch):
        use_settings(monkeypatch, telegram_webhook_secret="")
        response = client.post(
            "/webhook",
            json={"message": {"chat": {"id": 123}, "text": "/help"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "anything"},
        )
        assert response.json() == {"ok": True}

    def test_webhook_allows_correct_secret(self, client, monkeypatch, outbox):
        use_settings(monkeypatch, telegram_webhook_secret="correct-secret")
        response = client.post(
            "/webhook",
            json={"message": {"chat": {"id": 123, "type": "private"}, "from": {"id": 123}, "text": "/help"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret"},
        )
        assert response.json() == {"ok": True}
        assert outbox.to(123)

    def test_webhook_ignores_empty_body(self, client, monkeypatch):
        use_settings(monkeypatch, telegram_webhook_secret="correct-secret")
        response = client.post("/webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret"})
        assert response.json() == {"ok": True}

    def test_webhook_rejects_empty_chat_id(self, client, monkeypatch, outbox):
        use_settings(monkeypatch, telegram_webhook_secret="correct-secret")
        response = client.post(
            "/webhook",
            json={"message": {"text": "hello"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret"},
        )
        assert response.json() == {"ok": True}
        assert list(outbox) == []


class TestRunAuth:
    def test_run_rejects_missing_secret_header(self, client, monkeypatch):
        use_settings(monkeypatch, run_secret="correct-run-secret")
        with patch("src.main.run_all_users", new_callable=AsyncMock) as run:
            response = client.post("/run")
        assert response.json() == {"status": "unauthorized"}
        run.assert_not_called()

    def test_run_rejects_wrong_secret(self, client, monkeypatch):
        use_settings(monkeypatch, run_secret="correct-run-secret")
        with patch("src.main.run_all_users", new_callable=AsyncMock) as run:
            response = client.post("/run", headers={"X-Run-Secret": "wrong"})
        assert response.json() == {"status": "unauthorized"}
        run.assert_not_called()

    def test_run_allows_no_secret_configured(self, client, monkeypatch):
        use_settings(monkeypatch, run_secret="")
        with patch("src.main.run_all_users", new_callable=AsyncMock, return_value={}), \
                patch("src.main.run_reminders", new_callable=AsyncMock, return_value={}):
            response = client.post("/run", headers={"X-Run-Secret": "anything"})
        assert response.json() == {"status": "ok"}

    def test_run_allows_correct_secret(self, client, monkeypatch):
        use_settings(monkeypatch, run_secret="correct-run-secret")
        with patch("src.main.run_all_users", new_callable=AsyncMock, return_value={"users": 2}), \
                patch("src.main.run_reminders", new_callable=AsyncMock, return_value={"reminders": 1}):
            response = client.post("/run", headers={"X-Run-Secret": "correct-run-secret"})
        assert response.json() == {"status": "ok", "users": 2, "reminders": 1}


class TestPublicUrl:
    def test_connect_link_derived_from_host_when_public_url_unset(self, client, monkeypatch, outbox):
        use_settings(monkeypatch, public_url="")
        client.post(
            "/webhook",
            json={"message": {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "/start"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret", "Host": "svc-abc.a.run.app"},
        )
        url = outbox.last(7)[2]["inline_keyboard"][0][0]["url"]
        assert url.startswith("https://svc-abc.a.run.app/oauth/start?state=")


def test_startup_runs_migration(store, fake_db):
    fake_db.data["settings"] = {"global": {"telegram_chat_id": 42}}
    with TestClient(app):  # triggers lifespan
        pass
    assert fake_db.data["settings"]["app"]["admin_user_id"] == 42
