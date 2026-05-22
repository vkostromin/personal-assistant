import httpx

TELEGRAM_API = "https://api.telegram.org/bot"


async def send_message(token: str, chat_id: int, text: str) -> bool:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{TELEGRAM_API}{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
        )
        return resp.is_success


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

