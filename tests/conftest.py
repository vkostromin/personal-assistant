import os

import pytest

from src.config import Settings


@pytest.fixture
def override_settings(monkeypatch):
    monkeypatch.setattr(Settings, "model_config", {"env_file": ".env.test", "env_file_encoding": "utf-8", "extra": "ignore"})


@pytest.fixture
def mock_settings(monkeypatch):
    monkeypatch.setattr("src.state.settings", Settings(
        project_id="test-project",
        google_client_id="test-client-id",
        google_client_secret="test-client-secret",
        token_encryption_key="test-encryption-key-1234567890123456",
        telegram_webhook_secret="test-webhook-secret",
        run_secret="test-run-secret",
        telegram_bot_token="test-bot-token",
    ))
    monkeypatch.setattr("src.main.settings", Settings(
        project_id="test-project",
        google_client_id="test-client-id",
        google_client_secret="test-client-secret",
        token_encryption_key="test-encryption-key-1234567890123456",
        telegram_webhook_secret="test-webhook-secret",
        run_secret="test-run-secret",
        telegram_bot_token="test-bot-token",
    ))
    monkeypatch.setattr("src.config.settings", Settings(
        project_id="test-project",
        google_client_id="test-client-id",
        google_client_secret="test-client-secret",
        token_encryption_key="test-encryption-key-1234567890123456",
        telegram_webhook_secret="test-webhook-secret",
        run_secret="test-run-secret",
        telegram_bot_token="test-bot-token",
    ))
