from unittest.mock import patch

import pytest

from src.chat_client import _build_credentials, CHAT_SCOPES
from src.models import Account


@pytest.fixture
def account():
    return Account(
        email="test@example.com",
        refresh_token="test-refresh-token",
        providers=["gmail", "chat"],
    )


def test_build_credentials_uses_chat_scopes(account):
    client_id = "test-client-id"
    client_secret = "test-client-secret"

    with patch("src.chat_client.Credentials") as mock_creds:
        _build_credentials(account, client_id, client_secret)
        call_args = mock_creds.from_authorized_user_info.call_args
        info = call_args[0][0]
        assert info["refresh_token"] == "test-refresh-token"
        assert info["client_id"] == "test-client-id"
        assert info["client_secret"] == "test-client-secret"
        assert info["token_uri"] == "https://oauth2.googleapis.com/token"
        assert info["scopes"] == CHAT_SCOPES
        assert "https://www.googleapis.com/auth/chat.spaces.readonly" in info["scopes"]
        assert "https://www.googleapis.com/auth/chat.messages.readonly" in info["scopes"]


def test_chat_scopes_contain_spaces_and_messages():
    assert "https://www.googleapis.com/auth/chat.spaces.readonly" in CHAT_SCOPES
    assert "https://www.googleapis.com/auth/chat.messages.readonly" in CHAT_SCOPES
