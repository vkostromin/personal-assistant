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


def test_my_messages_are_marked_me():
    from unittest.mock import MagicMock
    from src.chat_client import fetch_messages, space_link
    acc = Account(email="me@x.com", refresh_token="t", google_sub="111")
    svc = MagicMock()
    svc.spaces.return_value.messages.return_value.list.return_value.execute.return_value = {"messages": [
        {"sender": {"name": "users/111"}, "text": "I'll do it", "createTime": "2030-01-01T10:00:00Z"},
        {"sender": {"name": "users/222", "displayName": "Ann"}, "text": "thanks", "createTime": "2030-01-01T10:01:00Z"},
    ]}
    with patch("src.chat_client._get_service", return_value=svc):
        msgs = fetch_messages(acc, "c", "s", {"name": "spaces/AAA"}, last_run=None)
    assert [(m["sender"], m["is_me"]) for m in msgs] == [("Me", True), ("Ann", False)]
    assert space_link({"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"}) == "https://chat.google.com/dm/AAA"
    assert space_link("spaces/BBB") == "https://chat.google.com/room/BBB"
