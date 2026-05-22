import logging
from datetime import datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.models import Account

log = logging.getLogger(__name__)

CHAT_SCOPES = [
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
]


def _build_credentials(account: Account, client_id: str, client_secret: str) -> Credentials:
    return Credentials.from_authorized_user_info(
        {
            "refresh_token": account.refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": CHAT_SCOPES,
        }
    )


def _get_service(account: Account, client_id: str, client_secret: str):
    creds = _build_credentials(account, client_id, client_secret)
    creds.refresh(Request())
    return build("chat", "v1", credentials=creds)


def list_spaces(account: Account, client_id: str, client_secret: str) -> list[dict]:
    service = _get_service(account, client_id, client_secret)
    try:
        result = service.spaces().list().execute()
        spaces = [
            s for s in result.get("spaces", [])
            if s.get("spaceType") in ("SPACE", "DIRECT_MESSAGE", "GROUP_CHAT")
        ]
        log.info("Found %d spaces", len(spaces))
        return spaces
    except HttpError as e:
        log.error("Failed to list spaces: %s", e)
        return []


def fetch_messages(
    account: Account,
    client_id: str,
    client_secret: str,
    space: dict,
    last_run: datetime | None,
    max_results: int = 30,
) -> list[dict]:
    service = _get_service(account, client_id, client_secret)
    try:
        result = (
            service.spaces()
            .messages()
            .list(parent=space["name"], pageSize=max_results, orderBy="createTime DESC")
            .execute()
        )
    except HttpError:
        return []

    filtered = []
    for msg in result.get("messages", []):
        created = None
        if "createTime" in msg:
            created = datetime.fromisoformat(msg["createTime"].replace("Z", "+00:00"))
        if last_run and created and created < last_run:
            continue

        filtered.append(
            {
                "sender": msg.get("sender", {}).get("displayName", "Unknown"),
                "text": msg.get("text", ""),
                "time": msg.get("createTime", ""),
                "space": space.get("displayName", space.get("name", "")),
            }
        )

    return filtered
