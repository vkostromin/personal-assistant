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


def fetch_recent_messages(
    account: Account, client_id: str, client_secret: str, last_run: datetime | None, max_results: int = 20
) -> tuple[list[dict], int]:
    """Fetch up to ``max_results`` newest messages since ``last_run``.

    Returns ``(messages, skipped)`` where ``skipped`` is how many more matching messages
    exist beyond the cap (a lower bound if there are more than 500).
    """
    service = _get_service(account, client_id, client_secret)
    query = f"after:{int(last_run.timestamp())} -in:sent -in:drafts" if last_run else "-in:sent -in:drafts"

    try:
        # ids only — cheap; lets us report how many emails were over the limit
        results = service.users().messages().list(userId="me", q=query, maxResults=500).execute()
    except HttpError:
        return [], 0
    ids = [m["id"] for m in results.get("messages", [])]
    take = ids[: max(0, max_results)]

    messages = []
    for msg_id in take:
        full = (
            service.users()
            .messages()
            .get(userId="me", id=msg_id, format="full")
            .execute()
        )
        headers = {h["name"]: h["value"] for h in full["payload"].get("headers", [])}
        body = _extract_body(full["payload"])

        messages.append(
            {
                "id": msg_id,
                "thread_id": full.get("threadId", ""),
                "from": headers.get("From", ""),
                "to": headers.get("To", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "body": body[:2000],
            }
        )

    return messages, len(ids) - len(take)


def _build_message(to: str, subject: str, body: str, thread_id: str | None) -> dict:
    mime = MIMEText(body, "plain", "utf-8")
    mime["To"] = to
    mime["Subject"] = subject
    message: dict = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode()}
    if thread_id:
        message["threadId"] = thread_id
    return message


def create_draft(
    account: Account,
    client_id: str,
    client_secret: str,
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
) -> str | None:
    service = _get_service(account, client_id, client_secret)
    try:
        draft = service.users().drafts().create(
            userId="me", body={"message": _build_message(to, subject, body, thread_id)}
        ).execute()
        return draft.get("id")
    except HttpError as e:
        log.error("Gmail draft creation failed: %s", e)
        return None


class DraftGone(Exception):
    """The Gmail draft no longer exists (sent or deleted by the user)."""


def update_draft(
    account: Account,
    client_id: str,
    client_secret: str,
    draft_id: str,
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
) -> str:
    """Replace the content of an existing Gmail draft in place. Returns the draft id.

    Raises ``DraftGone`` if the draft doesn't exist any more; other API errors propagate.
    """
    service = _get_service(account, client_id, client_secret)
    try:
        draft = service.users().drafts().update(
            userId="me",
            id=draft_id,
            body={"id": draft_id, "message": _build_message(to, subject, body, thread_id)},
        ).execute()
        return draft.get("id", draft_id)
    except HttpError as e:
        if getattr(e, "resp", None) is not None and e.resp.status == 404:
            raise DraftGone(draft_id) from e
        raise
