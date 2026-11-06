import html
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from src import config
from src.bot import handle_update, reply
from src.models import Account
from src.oauth import OAuthError, build_auth_url, exchange_code, parse_id_token, providers_from_scopes
from src.runner import run_all_users
from src.state import LinkResult, get_store
from src.telegram_client import escape_markdown

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    s = config.settings
    if not s.telegram_webhook_secret:
        log.warning("TELEGRAM_WEBHOOK_SECRET is not set — anyone can forge Telegram updates. Set it!")
    if not s.run_secret:
        log.warning("RUN_SECRET is not set — anyone can trigger /run for all users. Set it!")
    if not s.token_encryption_key:
        log.warning("TOKEN_ENCRYPTION_KEY is not set — refresh tokens are stored unencrypted.")
    try:
        await get_store().migrate_legacy()
    except Exception as exc:  # noqa: BLE001 — never block startup on migration
        log.error("Legacy data migration failed: %s", exc, exc_info=True)
    yield


app = FastAPI(title="Personal Assistant", lifespan=lifespan)


def public_base_url(request: Request) -> str:
    """Base URL used for OAuth links: PUBLIC_URL, or derived from the request (Cloud Run is HTTPS)."""
    if config.settings.public_url:
        return config.settings.public_url.rstrip("/")
    host = request.headers.get("host") or request.url.netloc
    local = host.startswith(("localhost", "127.0.0.1"))
    proto = "http" if local else "https"
    return f"{proto}://{host}"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def telegram_webhook(request: Request, body: dict):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if config.settings.telegram_webhook_secret and secret != config.settings.telegram_webhook_secret:
        log.warning("Telegram webhook received invalid secret token")
        return {"ok": False}
    try:
        await handle_update(body, public_base_url(request))
    except Exception as exc:  # noqa: BLE001 — return 200 so Telegram doesn't retry forever
        log.error("Error handling update: %s", exc, exc_info=True)
        chat_id = (body.get("message") or {}).get("chat", {}).get("id")
        if chat_id:
            try:
                await reply(chat_id, "😵 Sorry, something went wrong. Please try again later.")
            except Exception:  # noqa: BLE001
                pass
    return {"ok": True}


@app.post("/run")
async def scheduled_run(request: Request):
    secret = request.headers.get("X-Run-Secret", "")
    if config.settings.run_secret and secret != config.settings.run_secret:
        log.warning("Scheduled run received invalid secret token")
        return {"status": "unauthorized"}
    stats = await run_all_users()
    return {"status": "ok", **(stats or {})}


# ---------------------------------------------------------------- OAuth pages
def _page(title: str, body_html: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:520px;margin:48px auto;padding:0 16px;line-height:1.5}}
.btn{{display:inline-block;background:#1a73e8;color:#fff;padding:12px 20px;border-radius:6px;text-decoration:none}}
.warn{{background:#fff4e5;padding:12px;border-radius:6px}}</style></head>
<body><h2>{html.escape(title)}</h2>{body_html}</body></html>""",
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


EXPIRED_PAGE = "<p>This link is invalid or has expired. Send <b>/connect</b> to the bot to get a new one.</p>"


@app.get("/oauth/start")
async def oauth_start(state: str = ""):
    """Confirmation page shown before redirecting to Google.

    Shows *which Telegram user* the Google account will be linked to, so a person
    who received a forwarded link doesn't unknowingly hand their mailbox to someone else.
    """
    store = get_store()
    oauth_state = await store.get_oauth_state(state)
    if oauth_state is None:
        return _page("Link expired", EXPIRED_PAGE, 400)
    user = await store.get_user(oauth_state.user_id)
    if user is None or not user.is_active:
        return _page("Link expired", EXPIRED_PAGE, 400)
    url = build_auth_url(config.settings.google_client_id, oauth_state.redirect_uri, oauth_state.id, oauth_state.code_verifier)
    who = html.escape(user.display_name)
    return _page(
        "Connect your Google account",
        f"""<p>You are about to give <b>Lola</b> access to the Gmail and Google Chat of the Google
account you choose next. Summaries and drafts will be delivered to the Telegram user
<b>{who}</b>.</p>
<p class="warn">⚠️ Continue only if <b>{who}</b> is <b>your own</b> Telegram account.
If somebody sent you this link, close this page.</p>
<p><a class="btn" href="{html.escape(url)}">Continue with Google</a></p>""",
    )


@app.get("/oauth/callback")
async def oauth_callback(state: str = "", code: str = "", error: str = ""):
    s = config.settings
    store = get_store()
    oauth_state = await store.consume_oauth_state(state)
    if oauth_state is None:
        return _page("Link expired", EXPIRED_PAGE, 400)
    user = await store.get_user(oauth_state.user_id)
    if user is None or not user.is_active:
        return _page("Link expired", EXPIRED_PAGE, 400)

    if error or not code:
        await reply(user.chat_id, "Google sign-in was cancelled. Send /connect to try again.")
        return _page("Cancelled", "<p>Google sign-in was cancelled. You can close this window.</p>")

    try:
        tokens = await exchange_code(code, s.google_client_id, s.google_client_secret, oauth_state.redirect_uri, oauth_state.code_verifier)
        claims = parse_id_token(tokens.get("id_token", ""), s.google_client_id)
    except OAuthError as exc:
        log.error("OAuth callback failed for user %s: %s", user.id, exc)
        await reply(user.chat_id, "❌ Could not link the Google account. Send /connect to try again.")
        return _page("Something went wrong", "<p>Could not complete Google sign-in. Please try again from Telegram.</p>", 400)

    email = (claims.get("email") or "").lower()
    refresh_token = tokens.get("refresh_token", "")
    providers = providers_from_scopes(tokens.get("scope", ""))

    if not email or not claims.get("email_verified", False):
        problem = "Google did not return a verified email address."
    elif not refresh_token:
        problem = "Google did not return offline access. Remove Lola at myaccount.google.com/permissions and try again."
    elif not providers:
        problem = "No Gmail or Chat permissions were granted. Please tick the permission checkboxes."
    else:
        problem = ""
    # NB: never revoke the new token on failure paths — revocation kills the whole grant
    # for that Google account, including a valid token an existing link may rely on.
    if problem:
        await reply(user.chat_id, f"❌ {escape_markdown(problem)} Send /connect to try again.")
        return _page("Account not linked", f"<p>{html.escape(problem)}</p>", 400)

    result = await store.link_account(Account(email=email, user_id=user.id, refresh_token=refresh_token, providers=providers))
    if result == LinkResult.TAKEN:
        await reply(user.chat_id, f"❌ {escape_markdown(email)} is already linked to another user.")
        return _page("Account not linked", f"<p>{html.escape(email)} is already linked to another user of this bot.</p>", 409)

    missing = [p for p in ("gmail", "chat") if p not in providers]
    note = f"\n(No access granted for: {', '.join(missing)})" if missing else ""
    verb = "reconnected" if result == LinkResult.UPDATED else "connected"
    await reply(
        user.chat_id,
        f"✅ {escape_markdown(email)} {verb}.{note}\n\nYou'll get your summary at the next scheduled run. /accounts shows linked accounts.",
    )
    return _page(
        "Account connected ✅",
        f"<p><b>{html.escape(email)}</b> is now linked. You can close this window and return to Telegram.</p>",
    )
