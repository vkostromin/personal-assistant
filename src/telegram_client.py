import logging

import httpx

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot"

_bot_username_cache: dict[str, str] = {}


def url_button(text: str, url: str) -> dict:
    """Inline keyboard with a single URL button."""
    return {"inline_keyboard": [[{"text": text, "url": url}]]}


MAX_TEXT = 4000  # Telegram limit is 4096


def _fit(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


async def _post_with_markdown_fallback(client: httpx.AsyncClient, url: str, payload: dict) -> httpx.Response:
    resp = await client.post(url, json=payload)
    if resp.status_code == 400 and "parse" in resp.text.lower():
        # LLM output / user data with unbalanced Markdown: resend as plain text
        payload = {k: v for k, v in payload.items() if k != "parse_mode"}
        resp = await client.post(url, json=payload)
    return resp


async def send_message(token: str, chat_id: int, text: str, reply_markup: dict | None = None) -> int | None:
    """Send a message; returns its message_id (None on failure)."""
    payload: dict = {
        "chat_id": chat_id,
        "text": _fit(text),
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await _post_with_markdown_fallback(client, f"{TELEGRAM_API}{token}/sendMessage", payload)
    if not resp.is_success:
        log.warning("Telegram sendMessage to %s failed: %s %s", chat_id, resp.status_code, resp.text[:200])
        return None
    return resp.json().get("result", {}).get("message_id")


async def edit_message(
    token: str, chat_id: int, message_id: int, text: str | None = None, reply_markup: dict | None = None
) -> bool:
    """Edit a message's text (and keyboard), or only its keyboard if ``text`` is None.
    ``reply_markup=None`` removes the keyboard."""
    markup = reply_markup or {"inline_keyboard": []}
    async with httpx.AsyncClient(timeout=10) as client:
        if text is None:
            resp = await client.post(
                f"{TELEGRAM_API}{token}/editMessageReplyMarkup",
                json={"chat_id": chat_id, "message_id": message_id, "reply_markup": markup},
            )
        else:
            payload = {
                "chat_id": chat_id, "message_id": message_id, "text": _fit(text), "parse_mode": "Markdown",
                "disable_web_page_preview": True, "reply_markup": markup,
            }
            resp = await _post_with_markdown_fallback(client, f"{TELEGRAM_API}{token}/editMessageText", payload)
    if not resp.is_success and "not modified" not in resp.text:
        log.warning("Telegram edit of %s/%s failed: %s", chat_id, message_id, resp.text[:200])
    return resp.is_success


async def answer_callback(token: str, callback_id: str, text: str = "") -> None:
    """Stop the button's loading spinner (optionally with a short toast)."""
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"{TELEGRAM_API}{token}/answerCallbackQuery", json={"callback_query_id": callback_id, "text": text[:200]}
        )


async def delete_message(token: str, chat_id: int, message_id: int) -> bool:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{TELEGRAM_API}{token}/deleteMessage", json={"chat_id": chat_id, "message_id": message_id})
    return resp.is_success and resp.json().get("result") is True


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
