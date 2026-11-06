from datetime import datetime
from unittest.mock import patch

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


# ---------------------------------------------------------------- fetch / drafts
import base64  # noqa: E402
from email import message_from_bytes  # noqa: E402

from googleapiclient.errors import HttpError  # noqa: E402
from httplib2 import Response  # noqa: E402

from src import gmail_client  # noqa: E402
from src.gmail_client import DraftGone, create_draft, fetch_recent_messages, update_draft  # noqa: E402


class FakeGmail:
    def __init__(self, n_messages=0, update_status=None):
        self.n = n_messages
        self.update_status = update_status
        self.calls = []

    # service.users().messages().list(...).execute() style chaining
    def users(self):
        return self

    def messages(self):
        return self

    def drafts(self):
        return self

    def _exec(self, value):
        class R:
            def execute(_self):
                if isinstance(value, Exception):
                    raise value
                return value
        return R()

    def list(self, userId, q, maxResults):
        self.calls.append(("list", q, maxResults))
        return self._exec({"messages": [{"id": f"m{i}"} for i in range(self.n)]})

    def get(self, userId, id, format):
        self.calls.append(("get", id))
        return self._exec({"threadId": f"t-{id}", "payload": {"headers": [{"name": "Subject", "value": id}],
                           "body": {"data": base64.urlsafe_b64encode(b"hello").decode()}}})

    def create(self, userId, body):
        self.calls.append(("create", body))
        return self._exec({"id": "d-new"})

    def update(self, userId, id, body):
        self.calls.append(("update", id, body))
        if self.update_status:
            return self._exec(HttpError(Response({"status": self.update_status}), b"{}"))
        return self._exec({"id": id})


def test_fetch_respects_cap_and_reports_skipped(account, monkeypatch):
    svc = FakeGmail(n_messages=7)
    monkeypatch.setattr(gmail_client, "_get_service", lambda *a: svc)
    messages, skipped = fetch_recent_messages(account, "c", "s", datetime(2025, 1, 1), max_results=5)
    assert [m["id"] for m in messages] == ["m0", "m1", "m2", "m3", "m4"]
    assert skipped == 2 and messages[0]["thread_id"] == "t-m0" and messages[0]["body"] == "hello"
    assert sum(1 for c in svc.calls if c[0] == "get") == 5

    svc.calls.clear()
    messages, skipped = fetch_recent_messages(account, "c", "s", None, max_results=0)
    assert messages == [] and skipped == 7
    assert not [c for c in svc.calls if c[0] == "get"]


def test_update_draft_replaces_content_in_place(account, monkeypatch):
    svc = FakeGmail()
    monkeypatch.setattr(gmail_client, "_get_service", lambda *a: svc)
    assert update_draft(account, "c", "s", "d-1", "to@x.com", "Re: hi", "new body", thread_id="t-1") == "d-1"
    _, draft_id, body = svc.calls[0]
    assert draft_id == "d-1" and body["id"] == "d-1" and body["message"]["threadId"] == "t-1"
    mime = message_from_bytes(base64.urlsafe_b64decode(body["message"]["raw"]))
    assert mime["To"] == "to@x.com" and mime["Subject"] == "Re: hi"
    assert mime.get_payload(decode=True).decode() == "new body"


def test_update_missing_draft_raises_draft_gone(account, monkeypatch):
    monkeypatch.setattr(gmail_client, "_get_service", lambda *a: FakeGmail(update_status=404))
    with pytest.raises(DraftGone):
        update_draft(account, "c", "s", "d-1", "to", "s", "b")
    monkeypatch.setattr(gmail_client, "_get_service", lambda *a: FakeGmail(update_status=500))
    with pytest.raises(HttpError):
        update_draft(account, "c", "s", "d-1", "to", "s", "b")


def test_create_draft(account, monkeypatch):
    svc = FakeGmail()
    monkeypatch.setattr(gmail_client, "_get_service", lambda *a: svc)
    assert create_draft(account, "c", "s", "to", "subj", "body", thread_id=None) == "d-new"
    assert "threadId" not in svc.calls[0][1]["message"]
