import base64
import logging
from datetime import datetime
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.models import Account

log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def _build_credentials(account: Account, client_id: str, client_secret: str) -> Credentials:
    return Credentials.from_authorized_user_info(
        {
            "refresh_token": account.refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": GMAIL_SCOPES,
        }
    )


def _get_service(account: Account, client_id: str, client_secret: str):
    creds = _build_credentials(account, client_id, client_secret)
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def _extract_body(payload: dict) -> str:
    if "parts" in payload:
        for part in payload["parts"]:
            if part["mimeType"] == "text/plain" and "data" in part.get("body", {}):
                return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
            if "parts" in part:
                result = _extract_body(part)
                if result:
                    return result
    if "body" in payload and "data" in payload.get("body", {}):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    return ""


def list_message_ids(
    account: Account,
    client_id: str,
    client_secret: str,
    since: datetime,
    sent: bool = False,
    cap: int | None = None,
) -> tuple[list[str], bool]:
    """IDs of messages received (or, with ``sent``, sent) after ``since``, newest first.

    At most ``cap`` ids (None = no limit). Returns ``(ids, truncated)``. The query starts a
    second early — exact filtering happens on ``internalDate`` after fetching.
    """
    service = _get_service(account, client_id, client_secret)
    base = "in:sent" if sent else "-in:sent -in:drafts"
    query = f"after:{int(since.timestamp()) - 1} {base}"
    ids: list[str] = []
    page_token = None
    while True:
        try:
            resp = (
                service.users().messages()
                .list(userId="me", q=query, maxResults=500, pageToken=page_token)
                .execute()
            )
        except HttpError as e:
            log.error("Gmail list failed for %s: %s", account.email, e)
            break
        ids.extend(m["id"] for m in resp.get("messages", []))
        if cap is not None and len(ids) > cap:
            return ids[:cap], True
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids, False


def get_messages(account: Account, client_id: str, client_secret: str, ids: list[str]) -> list[dict]:
    """Full messages for ``ids``, oldest first. ``internal_ms`` = when Gmail received/sent it."""
    service = _get_service(account, client_id, client_secret)
    messages = []
    for msg_id in ids:
        try:
            full = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        except HttpError as e:
            log.warning("Gmail get %s failed: %s", msg_id, e)
            continue
        headers = {h["name"].lower(): h["value"] for h in full["payload"].get("headers", [])}
        messages.append(
            {
                "id": msg_id,
                "thread_id": full.get("threadId", ""),
                "internal_ms": int(full.get("internalDate", 0)),
                "from": headers.get("from", ""),
                "reply_to": headers.get("reply-to", ""),
                "to": headers.get("to", ""),
                "subject": headers.get("subject", ""),
                "date": headers.get("date", ""),
                "message_id_header": headers.get("message-id", ""),
                "body": _extract_body(full["payload"])[:2000],
            }
        )
    return sorted(messages, key=lambda m: m["internal_ms"])


def _draft_resource(to: str, subject: str, body: str, thread_id: str | None, in_reply_to: str | None = None) -> dict:
    mime = MIMEText(body, "plain", "utf-8")
    mime["To"] = to
    mime["Subject"] = subject
    if in_reply_to:
        # Gmail (and the recipient's mail client) thread replies by these headers
        mime["In-Reply-To"] = in_reply_to
        mime["References"] = in_reply_to
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    resource: dict = {"message": {"raw": raw}}
    if thread_id:
        resource["message"]["threadId"] = thread_id
    return resource


def create_draft(
    account: Account,
    client_id: str,
    client_secret: str,
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
) -> str | None:
    service = _get_service(account, client_id, client_secret)
    try:
        resource = _draft_resource(to, subject, body, thread_id, in_reply_to)
        draft = service.users().drafts().create(userId="me", body=resource).execute()
        return draft.get("id")
    except HttpError as e:
        log.error("Gmail draft creation failed: %s", e)
        return None


def update_gmail_draft(
    account: Account,
    client_id: str,
    client_secret: str,
    draft_id: str | None,
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
) -> tuple[str | None, bool]:
    """Replace the content of an existing Gmail draft in place.

    If the draft no longer exists (sent or deleted in Gmail) or was never created,
    a new draft is created instead. Returns ``(draft_id, created_new)``;
    ``draft_id`` is None if Gmail rejected both operations.
    """
    service = _get_service(account, client_id, client_secret)
    resource = _draft_resource(to, subject, body, thread_id, in_reply_to)
    if draft_id:
        try:
            draft = service.users().drafts().update(userId="me", id=draft_id, body={"id": draft_id, **resource}).execute()
            return draft.get("id", draft_id), False
        except HttpError as e:
            if e.resp.status not in (400, 404):  # 404: deleted; 400: no longer a draft (sent)
                log.error("Gmail draft update failed: %s", e)
                return None, False
            log.info("Gmail draft %s no longer exists, creating a new one", draft_id)
    try:
        draft = service.users().drafts().create(userId="me", body=resource).execute()
        return draft.get("id"), True
    except HttpError as e:
        log.error("Gmail draft creation failed: %s", e)
        return None, False


def delete_gmail_draft(account: Account, client_id: str, client_secret: str, draft_id: str) -> bool:
    """Delete a draft from Gmail. Already-gone drafts count as deleted."""
    service = _get_service(account, client_id, client_secret)
    try:
        service.users().drafts().delete(userId="me", id=draft_id).execute()
        return True
    except HttpError as e:
        if e.resp.status in (400, 404):
            return True
        log.error("Gmail draft deletion failed: %s", e)
        return False


def _was_sent(service, draft: dict) -> bool:
    """Did the user send a message in this conversation after the draft was created?"""
    created = draft.get("created_at")
    since = int(created.timestamp()) - 60 if created else 0
    try:
        if draft.get("thread_id"):
            thread = service.users().threads().get(userId="me", id=draft["thread_id"], format="minimal").execute()
            return any(
                "SENT" in m.get("labelIds", []) and int(m.get("internalDate", 0)) // 1000 >= since
                for m in thread.get("messages", [])
            )
        subject = (draft.get("subject") or "").replace('"', "")
        query = f'in:sent after:{since} to:{draft.get("to", "")} subject:"{subject}"'
        resp = service.users().messages().list(userId="me", q=query, maxResults=1).execute()
        return bool(resp.get("messages"))
    except HttpError as e:
        log.warning("Could not check sent state of draft %s: %s", draft.get("id"), e)
        return False


def check_drafts(account: Account, client_id: str, client_secret: str, drafts: list[dict]) -> dict[str, str]:
    """Find Lola drafts that are no longer drafts in Gmail.

    ``drafts`` are app draft records (id, gmail_draft_id, thread_id, to, subject, created_at).
    Returns ``{app_draft_id: "sent" | "discarded"}`` for drafts that disappeared from Gmail:
    "sent" if a sent message appeared in the conversation after the draft was created,
    otherwise "discarded" (deleted in Gmail).
    """
    tracked = [d for d in drafts if d.get("gmail_draft_id")]
    if not tracked:
        return {}
    service = _get_service(account, client_id, client_secret)
    existing: set[str] = set()
    page_token = None
    while True:
        resp = service.users().drafts().list(userId="me", maxResults=500, pageToken=page_token).execute()
        existing.update(d["id"] for d in resp.get("drafts", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return {
        d["id"]: ("sent" if _was_sent(service, d) else "discarded")
        for d in tracked
        if d["gmail_draft_id"] not in existing
    }


def send_draft(account: Account, client_id: str, client_secret: str, draft_id: str) -> dict | None:
    """Send an existing Gmail draft exactly as it is in Gmail (including the user's own edits).
    Returns the sent message ({id, threadId}) or None if Gmail refused."""
    service = _get_service(account, client_id, client_secret)
    try:
        return service.users().drafts().send(userId="me", body={"id": draft_id}).execute()
    except HttpError as e:
        log.error("Gmail draft send failed: %s", e)
        return None


def thread_link(account_email: str, thread_id: str | None) -> str:
    if not thread_id:
        return ""
    return f"https://mail.google.com/mail/u/{account_email}/#all/{thread_id}"


def drafts_link(account_email: str) -> str:
    return f"https://mail.google.com/mail/u/{account_email}/#drafts"
