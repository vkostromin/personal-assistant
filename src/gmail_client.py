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
) -> list[dict]:
    service = _get_service(account, client_id, client_secret)
    query = f"after:{int(last_run.timestamp())} -in:sent -in:drafts" if last_run else "-in:sent -in:drafts"

    try:
        results = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    except HttpError:
        return []

    messages = []
    for msg in results.get("messages", []):
        full = (
            service.users()
            .messages()
            .get(userId="me", id=msg["id"], format="full")
            .execute()
        )
        headers = {h["name"]: h["value"] for h in full["payload"].get("headers", [])}
        body = _extract_body(full["payload"])

        messages.append(
            {
                "id": msg["id"],
                "thread_id": full.get("threadId", ""),
                "from": headers.get("From", ""),
                "to": headers.get("To", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "body": body[:2000],
            }
        )

    return messages


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
    body_with_sig = body

    mime = MIMEText(body_with_sig, "plain", "utf-8")
    mime["To"] = to
    mime["Subject"] = subject

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    draft_body = {"message": {"raw": raw}}
    if thread_id:
        draft_body["message"]["threadId"] = thread_id

    try:
        draft = service.users().drafts().create(userId="me", body=draft_body).execute()
        return draft.get("id")
    except HttpError as e:
        log.error("Gmail draft creation failed: %s", e)
        return None
