from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.config import Settings
from src.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_settings():
    return Settings(
        project_id="test-project",
        telegram_webhook_secret="correct-webhook-secret",
        run_secret="correct-run-secret",
        google_client_id="",
        google_client_secret="",
    )


class TestWebhookAuth:
    def test_webhook_rejects_missing_secret_header(self, client, monkeypatch):
        monkeypatch.setattr("src.main.settings", Settings(
            project_id="test-project",
            telegram_webhook_secret="correct-secret",
        ))
        response = client.post("/webhook", json={"message": {}})
        assert response.status_code == 200
        assert response.json() == {"ok": False}

    def test_webhook_rejects_wrong_secret(self, client, monkeypatch):
        monkeypatch.setattr("src.main.settings", Settings(
            project_id="test-project",
            telegram_webhook_secret="correct-secret",
        ))
        response = client.post(
            "/webhook",
            json={"message": {}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": False}

    def test_webhook_allows_no_secret_configured(self, client, monkeypatch):
        monkeypatch.setattr("src.main.settings", Settings(
            project_id="test-project",
            telegram_webhook_secret="",
        ))
        response = client.post(
            "/webhook",
            json={"message": {"chat": {"id": 123}, "text": "/help"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "anything"},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_webhook_allows_correct_secret(self, client, monkeypatch):
        monkeypatch.setattr("src.main.settings", Settings(
            project_id="test-project",
            telegram_webhook_secret="correct-secret",
            telegram_bot_token="test-bot",
        ))
        monkeypatch.setattr("src.main.get_settings", AsyncMock(return_value=MagicMock(telegram_chat_id=123)))
        monkeypatch.setattr("src.main.list_accounts", AsyncMock(return_value=[]))
        monkeypatch.setattr("src.main.send_message", AsyncMock(return_value=True))

        response = client.post(
            "/webhook",
            json={"message": {"chat": {"id": 123}, "text": "/help"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret"},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_webhook_ignores_empty_body(self, client, monkeypatch):
        monkeypatch.setattr("src.main.settings", Settings(
            project_id="test-project",
            telegram_webhook_secret="correct-secret",
        ))
        response = client.post(
            "/webhook",
            json={},
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret"},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_webhook_rejects_empty_chat_id(self, client, monkeypatch):
        monkeypatch.setattr("src.main.settings", Settings(
            project_id="test-project",
            telegram_webhook_secret="correct-secret",
        ))
        response = client.post(
            "/webhook",
            json={"message": {"text": "hello"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret"},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}


class TestRunAuth:
    def test_run_rejects_missing_secret_header(self, client, monkeypatch):
        monkeypatch.setattr("src.main.settings", Settings(
            project_id="test-project",
            run_secret="correct-run-secret",
        ))
        response = client.post("/run")
        assert response.status_code == 200
        assert response.json() == {"status": "unauthorized"}

    def test_run_rejects_wrong_secret(self, client, monkeypatch):
        monkeypatch.setattr("src.main.settings", Settings(
            project_id="test-project",
            run_secret="correct-run-secret",
        ))
        response = client.post(
            "/run",
            headers={"X-Run-Secret": "wrong"},
        )
        assert response.status_code == 200
        assert response.json() == {"status": "unauthorized"}

    def test_run_allows_no_secret_configured(self, client, monkeypatch):
        monkeypatch.setattr("src.main.settings", Settings(
            project_id="test-project",
            run_secret="",
        ))
        with patch("src.main._process_accounts", new_callable=AsyncMock):
            response = client.post(
                "/run",
                headers={"X-Run-Secret": "anything"},
            )
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_run_allows_correct_secret(self, client, monkeypatch):
        monkeypatch.setattr("src.main.settings", Settings(
            project_id="test-project",
            run_secret="correct-run-secret",
        ))
        with patch("src.main._process_accounts", new_callable=AsyncMock):
            response = client.post(
                "/run",
                headers={"X-Run-Secret": "correct-run-secret"},
            )
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
