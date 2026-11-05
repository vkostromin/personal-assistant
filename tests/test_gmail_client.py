from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.gmail_client import _build_credentials, GMAIL_SCOPES
from src.models import Account


@pytest.fixture
def account():
    return Account(
        email="test@example.com",
        refresh_token="test-refresh-token",
        providers=["gmail", "chat"],
    )


def test_build_credentials_uses_gmail_scopes(account):
    client_id = "test-client-id"
    client_secret = "test-client-secret"

    with patch("src.gmail_client.Credentials") as mock_creds:
        _build_credentials(account, client_id, client_secret)
        call_args = mock_creds.from_authorized_user_info.call_args
        info = call_args[0][0]
        assert info["refresh_token"] == "test-refresh-token"
        assert info["client_id"] == "test-client-id"
        assert info["client_secret"] == "test-client-secret"
        assert info["token_uri"] == "https://oauth2.googleapis.com/token"
        assert info["scopes"] == GMAIL_SCOPES
        assert "https://www.googleapis.com/auth/gmail.modify" in info["scopes"]


def test_build_credentials_passes_refresh_token(account):
    account.refresh_token = "my-actual-refresh-token"
    client_id = "cid"
    client_secret = "csecret"

    with patch("src.gmail_client.Credentials") as mock_creds:
        _build_credentials(account, client_id, client_secret)
        info = mock_creds.from_authorized_user_info.call_args[0][0]
        assert info["refresh_token"] == "my-actual-refresh-token"


def test_gmail_scopes_contain_modify():
    assert "https://www.googleapis.com/auth/gmail.modify" in GMAIL_SCOPES
