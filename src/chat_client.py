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
# optional: lets the user reply to Chat messages from Telegram (as themselves)
CHAT_SEND_SCOPE = "https://www.googleapis.com/auth/chat.messages.create"


def _build_credentials(account: Account, client_id: str, client_secret: str, scopes: list[str] = CHAT_SCOPES) -> Credentials:
    return Credentials.from_authorized_user_info(
        {
            "refresh_token": account.refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": scopes,
        }
    )


def _get_service(account: Account, client_id: str, client_secret: str, scopes: list[str] = CHAT_SCOPES):
    creds = _build_credentials(account, client_id, client_secret, scopes)
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

    me = f"users/{account.google_sub}" if account.google_sub else None
    filtered = []
    for msg in result.get("messages", []):
        created = None
        if "createTime" in msg:
            created = datetime.fromisoformat(msg["createTime"].replace("Z", "+00:00"))
        if last_run and created and created < last_run:
            continue

        sender = msg.get("sender", {})
        is_me = bool(me) and sender.get("name") == me
        filtered.append(
            {
                "sender": "Me" if is_me else sender.get("displayName") or sender.get("name") or "Unknown",
                "is_me": is_me,
                "name": msg.get("name", ""),
                "thread": (msg.get("thread") or {}).get("name", ""),
                "text": msg.get("text", ""),
                "time": msg.get("createTime", ""),
                "space": space.get("displayName", space.get("name", "")),
            }
        )

    return filtered


def space_link(space: dict | str) -> str:
    """Web link to a Chat space / DM ("spaces/AAAA" -> https://chat.google.com/room/AAAA)."""
    name = space if isinstance(space, str) else space.get("name", "")
    space_id = name.split("/", 1)[-1] if name else ""
    if not space_id:
        return ""
    kind = "" if isinstance(space, str) else space.get("spaceType", "")
    return f"https://chat.google.com/{'dm' if kind == 'DIRECT_MESSAGE' else 'room'}/{space_id}"


def send_chat_message(
    account: Account, client_id: str, client_secret: str, space: str, text: str, thread: str | None = None
) -> dict | None:
    """Post ``text`` as the user into ``space`` (in ``thread`` when given). None if Chat refused."""
    service = _get_service(account, client_id, client_secret, scopes=[CHAT_SEND_SCOPE])
    body: dict = {"text": text}
    kwargs: dict = {}
    if thread:
        body["thread"] = {"name": thread}
        kwargs["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    try:
        return service.spaces().messages().create(parent=space, body=body, **kwargs).execute()
    except HttpError as e:
        log.error("Chat message send failed: %s", e)
        return None
