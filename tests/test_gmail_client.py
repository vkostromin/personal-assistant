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


class FakeGmailForSync:
    def __init__(self, existing_draft_ids, threads=None, sent_search_hits=0):
        self.existing = existing_draft_ids
        self.thread_data = threads or {}
        self.sent_search_hits = sent_search_hits
        self.queries = []

    def users(self):
        return self

    def drafts(self):
        outer = self

        class D:
            def list(self, userId, maxResults, pageToken=None):
                return _Req(lambda: {"drafts": [{"id": i} for i in outer.existing]})
        return D()

    def threads(self):
        outer = self

        class T:
            def get(self, userId, id, format):
                return _Req(lambda: {"messages": outer.thread_data.get(id, [])})
        return T()

    def messages(self):
        outer = self

        class M:
            def list(self, userId, q, maxResults):
                outer.queries.append(q)
                return _Req(lambda: {"messages": [{"id": "s"}] * outer.sent_search_hits})
        return M()


def test_check_drafts_detects_sent_and_deleted(account):
    from datetime import timezone
    from src.gmail_client import check_drafts
    created = datetime(2025, 1, 10, 8, 0, tzinfo=timezone.utc)
    later_ms = str(int(created.timestamp() * 1000) + 3_600_000)
    earlier_ms = str(int(created.timestamp() * 1000) - 86_400_000)
    svc = FakeGmailForSync(
        existing_draft_ids=["g-still-there"],
        threads={
            "t-sent": [{"labelIds": ["INBOX"], "internalDate": earlier_ms}, {"labelIds": ["SENT"], "internalDate": later_ms}],
            "t-deleted": [{"labelIds": ["SENT"], "internalDate": earlier_ms}],  # old reply, not ours
        },
    )
    drafts = [
        {"id": "a", "gmail_draft_id": "g-still-there", "thread_id": "t-x", "created_at": created},
        {"id": "b", "gmail_draft_id": "g-sent", "thread_id": "t-sent", "created_at": created},
        {"id": "c", "gmail_draft_id": "g-deleted", "thread_id": "t-deleted", "created_at": created},
        {"id": "d", "gmail_draft_id": None, "created_at": created},  # never reached Gmail: ignored
    ]
    with patch("src.gmail_client._get_service", return_value=svc):
        assert check_drafts(account, "c", "s", drafts) == {"b": "sent", "c": "discarded"}


def test_check_drafts_without_thread_searches_sent_mail(account):
    from datetime import timezone
    from src.gmail_client import check_drafts
    svc = FakeGmailForSync(existing_draft_ids=[], sent_search_hits=1)
    draft = {"id": "a", "gmail_draft_id": "g1", "to": "boss@x.com", "subject": 'Re: "plan"',
             "created_at": datetime(2025, 1, 10, tzinfo=timezone.utc)}
    with patch("src.gmail_client._get_service", return_value=svc):
        assert check_drafts(account, "c", "s", [draft]) == {"a": "sent"}
    assert "in:sent" in svc.queries[0] and "to:boss@x.com" in svc.queries[0]


def test_reply_draft_has_threading_headers():
    import base64
    from email import message_from_bytes
    from src.gmail_client import _draft_resource
    res = _draft_resource("a@x.com", "Re: hi", "body", "thread-1", "<orig@mail>")
    assert res["message"]["threadId"] == "thread-1"
    msg = message_from_bytes(base64.urlsafe_b64decode(res["message"]["raw"]))
    assert msg["In-Reply-To"] == "<orig@mail>" and msg["References"] == "<orig@mail>"


async def test_analyze_emails_takes_thread_from_real_message(monkeypatch):
    import json
    from src.llm_client import LLM, LLMConfig, analyze_emails

    async def fake_completion(cfg, messages, max_tokens=8000):
        assert "Message ID: m42" in messages[-1]["content"]
        return json.dumps({"drafts": [
            {"to": "a@x.com", "subject": "Re: hi", "body": "b", "original_email_id": "m42", "thread_id": "INVENTED"},
            {"to": "b@x.com", "subject": "Re: ?", "body": "b", "original_email_id": "nope"},
        ]}), 10

    monkeypatch.setattr("src.llm_client.chat_completion", fake_completion)
    msgs = [{"id": "m42", "thread_id": "T42", "from": "a@x.com", "subject": "hi", "date": "", "body": "?",
             "message_id_header": "<m42@mail>"}]
    from datetime import timezone as tz_
    from src.llm_client import AnalysisContext
    ctx = AnalysisContext(now=datetime.now(tz_.utc), tz=tz_.utc, tz_name="UTC")
    result = await analyze_emails(msgs, "me@x.com", LLM(LLMConfig("gemini", "m", "u", "k")), "sig", ctx)
    d1, d2 = result.drafts
    assert (d1.thread_id, d1.original_email_id, d1.in_reply_to) == ("T42", "m42", "<m42@mail>")
    assert (d2.thread_id, d2.original_email_id, d2.in_reply_to) == (None, None, None)
