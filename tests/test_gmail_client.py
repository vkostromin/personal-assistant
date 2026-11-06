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


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeDrafts:
    """Mimics service.users().drafts() with an in-memory mailbox."""

    def __init__(self, existing=()):
        self.store = {d: "old" for d in existing}
        self.created, self.updated = [], []

    def update(self, userId, id, body):
        def run():
            from googleapiclient.errors import HttpError
            if id not in self.store:
                raise HttpError(MagicMock(status=404), b"not found")
            assert body["id"] == id and "raw" in body["message"]
            self.updated.append(id)
            self.store[id] = body["message"]["raw"]
            return {"id": id}
        return _Req(run)

    def create(self, userId, body):
        def run():
            new_id = f"new{len(self.created) + 1}"
            self.created.append(new_id)
            self.store[new_id] = body["message"]["raw"]
            return {"id": new_id}
        return _Req(run)


def _service(drafts):
    svc = MagicMock()
    svc.users.return_value.drafts.return_value = drafts
    return svc


def test_update_gmail_draft_updates_in_place(account):
    from src.gmail_client import update_gmail_draft
    drafts = FakeDrafts(existing=["d1"])
    with patch("src.gmail_client._get_service", return_value=_service(drafts)):
        result = update_gmail_draft(account, "c", "s", "d1", "to@x.com", "Subj", "Body", thread_id="t1")
    assert result == ("d1", False)
    assert drafts.updated == ["d1"] and drafts.created == []


def test_update_gmail_draft_recreates_when_deleted_in_gmail(account):
    from src.gmail_client import update_gmail_draft
    drafts = FakeDrafts(existing=[])
    with patch("src.gmail_client._get_service", return_value=_service(drafts)):
        result = update_gmail_draft(account, "c", "s", "gone", "to@x.com", "Subj", "Body")
    assert result == ("new1", True) and drafts.updated == []


def test_update_gmail_draft_without_id_creates(account):
    from src.gmail_client import update_gmail_draft
    drafts = FakeDrafts()
    with patch("src.gmail_client._get_service", return_value=_service(drafts)):
        assert update_gmail_draft(account, "c", "s", None, "to@x.com", "S", "B") == ("new1", True)
