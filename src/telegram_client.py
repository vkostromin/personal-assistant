import logging

import httpx

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot"

_bot_username_cache: dict[str, str] = {}


def url_button(text: str, url: str) -> dict:
    """Inline keyboard with a single URL button."""
    return {"inline_keyboard": [[{"text": text, "url": url}]]}


async def send_message(token: str, chat_id: int, text: str, reply_markup: dict | None = None) -> bool:
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{TELEGRAM_API}{token}/sendMessage", json=payload)
        if resp.status_code == 400 and "parse" in resp.text.lower():
            # LLM output / user data with unbalanced Markdown: resend as plain text
            payload.pop("parse_mode")
            resp = await client.post(f"{TELEGRAM_API}{token}/sendMessage", json=payload)
        if not resp.is_success:
            log.warning("Telegram sendMessage to %s failed: %s %s", chat_id, resp.status_code, resp.text[:200])
        return resp.is_success


async def delete_message(token: str, chat_id: int, message_id: int) -> bool:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{TELEGRAM_API}{token}/deleteMessage", json={"chat_id": chat_id, "message_id": message_id}
        )
        return resp.is_success


async def get_bot_username(token: str) -> str:
    if token in _bot_username_cache:
        return _bot_username_cache[token]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{TELEGRAM_API}{token}/getMe")
    if not resp.is_success:
        return ""
    username = resp.json().get("result", {}).get("username", "")
    if username:
        _bot_username_cache[token] = username
    return username


async def set_webhook(token: str, url: str, secret_token: str = "") -> bool:
    payload: dict = {"url": url, "drop_pending_updates": True}
    if secret_token:
        payload["secret_token"] = secret_token
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{TELEGRAM_API}{token}/setWebhook",
            json=payload,
        )
        return resp.is_success


def escape_markdown(text: str) -> str:
    if not text:
        return ""
    # Escape legacy Telegram Markdown characters: _, *, `, [
    for char in ("_", "*", "`", "["):
        text = text.replace(char, "\\" + char)
    return text
