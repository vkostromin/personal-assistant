"""Telegram bot: registration, Google account linking, assistant and admin commands.

Identity is the Telegram *user id* (``message.from.id``). The bot only works in
private chats, where chat id == user id, so replies always go to the sender.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from google.auth.exceptions import RefreshError

from src import config
from src.callbacks import add_task_from_text, handle_callback, handle_free_text
from src.cards import draft_card, focus_message, numbered_keyboard, people_message, review_card, tasks_message, who_message
from src.drafts_sync import sync_note, sync_user_drafts
from src.items import matches_person, person_key, rank_focus, review_queue
from src.gmail_client import check_drafts, delete_gmail_draft, update_gmail_draft
from src.llm_client import LLM, LLMError, chat_completion, answer_question, rewrite_draft
from src.models import User, UserLLMKey, utcnow
from src.oauth import make_code_verifier, revoke_token
from src.providers import PROVIDERS, user_llm_config
from src.quota import LLMUnavailable, QuotaExceeded, effective_limit, get_llm, limit_reached_text, usage_status
from src.schedule import current_slot, fmt_local, next_slot, parse_timezone, summary_hours, user_tz, user_tz_name
from src.state import RedeemResult, get_store, normalize_email
from src.telegram_client import delete_message, escape_markdown, get_bot_username, send_message, url_button

log = logging.getLogger(__name__)

USER_HELP = (
    "🎯 /focus — what to do right now\n"
    "📋 /tasks — promises, waiting-for, replies, to-dos\n"
    "/task <text> — add a task (or just send / forward me any message)\n"
    "👤 /who <name> — everything open with a person\n"
    "🧹 /review — go through old items: done / cancel / remind again\n"
    "/status — current state\n"
    "/chat <question> — ask about your state\n"
    "/raw <text> — analyze text\n\n"
    "📧 Drafts\n"
    "/drafts — pending drafts (synced with Gmail)\n"
    "/draft N — draft N with Send / Rewrite / Snooze buttons\n"
    "/rewrite N <context> — update draft N\n"
    "/sent N… — mark draft(s) as sent\n"
    "/deletedraft N… — delete draft(s) here and in Gmail\n\n"
    "🔗 Google accounts\n"
    "/connect — link a Google account\n"
    "/accounts — linked accounts\n"
    "/unlink [email] <gmail|chat> — stop using Gmail or Chat of an account\n"
    "/disconnect <email> — unlink an account and revoke access\n\n"
    "⚙️ Settings\n"
    "/timezone [zone] — your timezone (summaries at 08:00 and 20:00)\n"
    "/llm — AI provider and usage limit\n"
    "/setkey <provider> <key> [model] — use your own AI key (no limits)\n"
    "/removekey — stop using your own AI key\n"
    "/deleteme — delete your Lola account and all your data\n"
    "/help — all commands"
)

ADMIN_HELP = (
    "\n\n👑 Admin\n"
    "/invite [note] — create an invitation link\n"
    "/invites — active invitations\n"
    "/revoke <code> — cancel an invitation\n"
    "/users — list users\n"
    "/disable <user id> — block a user\n"
    "/enable <user id> — unblock a user\n"
    "/remove <user id> — delete a user and all their data\n"
    "/setlimit <user id> <tokens|default|unlimited> — AI limit per 12h\n"
    "/transferadmin <user id> — make another user the admin"
)

NOT_INVITED = (
    "👋 Hi! I'm Lola, a private AI assistant.\n\n"
    "You need an invitation link from the administrator to use me."
)


@dataclass
class Ctx:
    chat_id: int
    user: User
    args: str
    base_url: str
    message_id: int | None = None


async def reply(chat_id: int, text: str, markup: dict | None = None) -> None:
    await send_message(config.settings.telegram_bot_token, chat_id, text, reply_markup=markup)


def parse_command(text: str) -> tuple[str, str]:
    if not text.startswith("/"):
        return "", text
    parts = text.split(maxsplit=1)
    cmd = parts[0].split("@", 1)[0].lower()  # "/status@lola_bot" -> "/status"
    return cmd, parts[1].strip() if len(parts) > 1 else ""


def help_text(user: User) -> str:
    return USER_HELP + (ADMIN_HELP if user.is_admin else "")


async def connect_markup(user_id: int, base_url: str) -> dict | None:
    """Create a single-use OAuth link for ``user_id`` as an inline button."""
    s = config.settings
    if not (s.google_client_id and s.google_client_secret and base_url):
        log.error("Cannot create connect link: GOOGLE_CLIENT_ID/SECRET or PUBLIC_URL missing")
        return None
    state = await get_store().create_oauth_state(
        user_id, make_code_verifier(), f"{base_url}/oauth/callback"
    )
    return url_button("🔗 Connect Google account", f"{base_url}/oauth/start?state={state.id}")


async def send_connect_prompt(chat_id: int, user_id: int, base_url: str, intro: str = "") -> None:
    markup = await connect_markup(user_id, base_url)
    if markup is None:
        await reply(chat_id, (intro + "\n\n" if intro else "") + "⚠️ Google sign-in is not configured on the server yet.")
        return
    ttl = config.settings.oauth_link_ttl_minutes
    text = (
        (intro + "\n\n" if intro else "")
        + "Tap the button to sign in with Google and give me access to Gmail and Google Chat.\n"
        f"The link is personal, works once and expires in {ttl} minutes — don't forward it."
    )
    await reply(chat_id, text, markup)


# --------------------------------------------------------------------- entry
async def handle_update(update: dict, base_url: str) -> None:
    if update.get("callback_query"):
        cq = update["callback_query"]
        if ((cq.get("message") or {}).get("chat") or {}).get("type", "private") == "private":
            await handle_callback(cq)
        return
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    # forwarded photos/documents carry their text as caption
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not chat_id or not text:
        return

    sender = msg.get("from") or {}
    user_id = sender.get("id", chat_id)
    if chat.get("type", "private") != "private" or user_id != chat_id:
        if text.startswith("/"):
            await reply(chat_id, "I only work in a private chat — please message me directly.")
        return

    name = " ".join(p for p in (sender.get("first_name"), sender.get("last_name")) if p)
    username = sender.get("username") or ""
    cmd, args = parse_command(text)

    store = get_store()
    user = await store.get_user(user_id)
    if user is None:
        await _handle_unregistered(chat_id, User(id=user_id, chat_id=chat_id, name=name, username=username), cmd, args, base_url)
        return
    if not user.is_active:
        await reply(chat_id, "⛔ Your access has been disabled by the administrator.")
        return
    if (name and user.name != name) or user.username != username:
        await store.update_user(user.id, {"name": name or user.name, "username": username})
        user = user.model_copy(update={"name": name or user.name, "username": username})

    if not cmd:
        # plain or forwarded text: answer a pending question, or offer "add as task / ask"
        await handle_free_text(user, chat_id, text)
        return
    handler = USER_COMMANDS.get(cmd)
    if handler is None and user.is_admin:
        handler = ADMIN_COMMANDS.get(cmd)
    if handler is None:
        await reply(chat_id, f"Unknown command: {escape_markdown(text[:50])}\nUse /help")
        return
    await handler(Ctx(chat_id=chat_id, user=user, args=args, base_url=base_url, message_id=msg.get("message_id")))


async def _handle_unregistered(chat_id: int, new_user: User, cmd: str, args: str, base_url: str) -> None:
    if cmd != "/start":
        await reply(chat_id, NOT_INVITED)
        return
    store = get_store()

    # First run: the first person to /start becomes the admin
    # (optionally restricted to ADMIN_TELEGRAM_ID).
    if await store.get_admin_id() is None:
        allowed = config.settings.admin_telegram_id_int
        if (allowed is None or allowed == new_user.id) and await store.claim_admin(new_user):
            adopted = await store.adopt_orphan_accounts(new_user.id)
            intro = (
                "👋 Hello! I'm Lola, your AI personal assistant.\n\n"
                "👑 You are the first user, so you are now the *administrator*.\n"
                "Use /invite to create invitation links for other people."
            )
            if adopted:
                intro += "\n\nExisting accounts assigned to you: " + ", ".join(escape_markdown(a) for a in adopted)
            await send_connect_prompt(
                chat_id, new_user.id, base_url,
                intro + "\n\nNext step: link your Google account. Then set your timezone with /timezone.",
            )
            return

    if not args:
        await reply(chat_id, NOT_INVITED)
        return

    status, invite = await store.redeem_invite(args, new_user)
    if status == RedeemResult.OK:
        await send_connect_prompt(
            chat_id,
            new_user.id,
            base_url,
            "👋 Welcome! I'm Lola, your AI personal assistant.\n\n"
            "Every 12 hours I check your email and Google Chat, send you a summary, "
            "highlight what needs your attention and prepare draft replies.\n\n"
            "Next step: link your Google account. Then set your timezone with /timezone, "
            "e.g. `/timezone Europe/Berlin`.",
        )
        if invite:
            await reply(
                invite.created_by,
                f"👤 {escape_markdown(new_user.display_name)} joined using your invitation"
                + (f" ({escape_markdown(invite.note)})" if invite.note else "")
                + f". User id: `{new_user.id}`",
            )
        return
    if status == RedeemResult.ALREADY_REGISTERED:
        await reply(chat_id, "You are already registered. Use /help.")
    elif status == RedeemResult.USED:
        await reply(chat_id, "❌ This invitation link has already been used. Ask the administrator for a new one.")
    elif status == RedeemResult.EXPIRED:
        await reply(chat_id, "❌ This invitation link has expired. Ask the administrator for a new one.")
    else:
        await reply(chat_id, "❌ Invalid invitation link. Ask the administrator for a new one.")


# ---------------------------------------------------------------- LLM helpers
def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.0f}k" if n >= 10_000 else f"{n:,}"


def _llm_error_text(llm: LLM, exc: LLMError) -> str:
    if llm.config.own_key:
        return (
            f"⚠️ Your {llm.config.provider} API key returned an error ({escape_markdown(str(exc)[:150])}).\n"
            "Update it with /setkey or /removekey to use the shared assistant."
        )
    return "⚠️ The AI service is temporarily unavailable. Please try again later."


async def _get_llm_or_reply(ctx: Ctx) -> LLM | None:
    try:
        return await get_llm(ctx.user)
    except LLMUnavailable:
        await reply(ctx.chat_id, "⚠️ No shared AI provider is configured. Add your own key with /setkey.")
        return None


async def _guarded(ctx: Ctx, llm: LLM, call) -> tuple[bool, object]:
    """Await an LLM call; on limit/provider errors tell the user and return (False, None)."""
    try:
        return True, await call
    except QuotaExceeded:
        await reply(ctx.chat_id, limit_reached_text(await usage_status(ctx.user), ctx.user))
    except LLMError as exc:
        await reply(ctx.chat_id, _llm_error_text(llm, exc))
    return False, None


# -------------------------------------------------------------- user commands
async def cmd_start(ctx: Ctx) -> None:
    accounts = await get_store().list_accounts(user_id=ctx.user.id, enabled_only=False)
    if not accounts:
        await send_connect_prompt(ctx.chat_id, ctx.user.id, ctx.base_url, "👋 Welcome back! You have no Google account linked yet.")
        return
    await reply(ctx.chat_id, "Hello! I'm Lola, your AI personal assistant.\n\n" + help_text(ctx.user))


async def cmd_help(ctx: Ctx) -> None:
    await reply(ctx.chat_id, help_text(ctx.user))


async def cmd_connect(ctx: Ctx) -> None:
    await send_connect_prompt(ctx.chat_id, ctx.user.id, ctx.base_url)


async def cmd_accounts(ctx: Ctx) -> None:
    accounts = await get_store().list_accounts(user_id=ctx.user.id, enabled_only=False)
    if not accounts:
        await reply(ctx.chat_id, "No Google accounts linked. Use /connect.")
        return
    lines = ["🔗 Linked Google accounts:\n"]
    for acc in accounts:
        status = "🔑 needs /connect" if acc.needs_reauth else ("⏸ disabled" if not acc.enabled else "✅")
        last = fmt_local(acc.last_run, ctx.user) if acc.last_run else "not checked yet"
        reply_note = " (↩️ replies: /connect again)" if "chat" in acc.providers and not acc.chat_send else ""
        lines.append(f"{status} {escape_markdown(acc.email)} — {', '.join(acc.providers) or 'nothing'}{reply_note} — {last}")
    lines.append(
        "\n/connect to add another (or to re-enable Gmail/Chat), /unlink <email> <gmail|chat> to stop using one, "
        "/disconnect <email> to remove."
    )
    await reply(ctx.chat_id, "\n".join(lines))


async def cmd_disconnect(ctx: Ctx) -> None:
    store = get_store()
    if not ctx.args:
        await reply(ctx.chat_id, "Usage: /disconnect <email>\nSee /accounts for your linked accounts.")
        return
    wanted = normalize_email(ctx.args)
    mine = await store.list_accounts(user_id=ctx.user.id, enabled_only=False)
    account = next((a for a in mine if normalize_email(a.email) == wanted), None)
    if account is None:
        await reply(ctx.chat_id, "No such account linked to you. See /accounts.")
        return
    await revoke_token(account.refresh_token)
    await store.delete_account(account.email)
    await reply(
        ctx.chat_id,
        f"✅ {escape_markdown(account.email)} disconnected and Google access revoked. "
        "Its drafts were removed from Lola (drafts already in Gmail stay there).",
    )


async def cmd_unlink(ctx: Ctx) -> None:
    """Stop using Gmail or Chat of one account, keeping the other."""
    store = get_store()
    usage = "Usage: /unlink [email] <gmail|chat>\nExample: /unlink me@company.com chat"
    parts = ctx.args.lower().split()
    if not parts or parts[-1] not in ("gmail", "chat") or len(parts) > 2:
        await reply(ctx.chat_id, usage)
        return
    provider = parts[-1]
    mine = await store.list_accounts(user_id=ctx.user.id, enabled_only=False)
    if len(parts) == 2:
        account = next((a for a in mine if normalize_email(a.email) == normalize_email(parts[0])), None)
    elif len(mine) == 1:
        account = mine[0]
    else:
        await reply(ctx.chat_id, "You have several accounts — say which one.\n" + usage)
        return
    if account is None:
        await reply(ctx.chat_id, "No such account linked to you. See /accounts.")
        return
    if provider not in account.providers:
        await reply(ctx.chat_id, f"{provider.capitalize()} is not in use for {escape_markdown(account.email)}.")
        return
    remaining = [p for p in account.providers if p != provider]
    if not remaining:
        await revoke_token(account.refresh_token)
        await store.delete_account(account.email)
        await reply(
            ctx.chat_id,
            f"✅ {provider.capitalize()} was the last thing Lola used from {escape_markdown(account.email)}, "
            "so the account was disconnected and Google access revoked.",
        )
        return
    await store.update_account(account.email, {"providers": remaining})
    await reply(
        ctx.chat_id,
        f"✅ Lola no longer reads {provider.capitalize()} of {escape_markdown(account.email)}. "
        "Use /connect with this account to turn it back on, or /disconnect to revoke Google access completely.",
    )


async def cmd_status(ctx: Ctx) -> None:
    store = get_store()
    drafts = await store.get_pending_drafts(ctx.user.id)
    items = await store.list_open_items(ctx.user.id)
    accounts = await store.list_accounts(user_id=ctx.user.id)
    runs = [a.last_run for a in accounts if a.last_run]
    now = utcnow()
    overdue = [i for i in items if i.due_at and i.due_at < now]
    counts = {k: sum(1 for i in items if i.kind == k) for k in ("promise", "waiting", "reply", "todo")}
    last_run = fmt_local(max(runs), ctx.user) if runs else "never"
    next_run = fmt_local(next_slot(utcnow(), user_tz(ctx.user)), ctx.user, "%H:%M")
    draft_text = f"{len(drafts)} pending drafts" if drafts else "no pending drafts"
    promise_text = (
        f"{counts['promise']} promised · {counts['waiting']} waiting for · {counts['reply']} to reply · {counts['todo']} to do"
        + (f" — ⚠️ {len(overdue)} overdue" if overdue else "")
    )
    usage = await usage_status(ctx.user)
    if usage.own_key:
        ai_text = f"own {usage.provider} key"
    elif usage.limit is None:
        ai_text = "unlimited"
    else:
        ai_text = f"{_fmt_tokens(usage.used)} / {_fmt_tokens(usage.limit)} tokens" + (" — limit reached" if usage.exhausted else "")
    await reply(
        ctx.chat_id,
        f"📋 Status\n\n🔗 {len(accounts)} linked account(s)\n📧 {draft_text}\n📌 {promise_text}\n"
        f"🕐 Last check: {last_run}, next summary {next_run} ({escape_markdown(user_tz_name(ctx.user))})\n🤖 AI: {ai_text}",
    )


async def cmd_drafts(ctx: Ctx) -> None:
    note = sync_note(await sync_user_drafts(ctx.user.id))
    drafts = await get_store().get_pending_drafts(ctx.user.id)
    if not drafts:
        await reply(ctx.chat_id, "\n\n".join(p for p in (note, "No pending drafts.") if p))
        return
    lines = []
    for i, d in enumerate(drafts, 1):
        to_esc = escape_markdown(d.get("to", "?"))
        subj_esc = escape_markdown(d.get("subject", "?"))
        body_preview = escape_markdown(d.get("body", "")[:80].replace("\n", " "))
        lines.append(f"{i}. To: {to_esc} — \"{subj_esc}\"")
        lines.append(f"   {body_preview}...")
    entries = "\n".join(lines)
    await reply(
        ctx.chat_id,
        (note + "\n\n" if note else "")
        + f"📧 Pending drafts:\n\n{entries}\n\n"
        "Tap a number for Send / Rewrite / Snooze / Discard.",
        numbered_keyboard([("d", d["id"]) for d in drafts[:25]]),
    )


def parse_draft_numbers(args: str, count: int) -> list[int] | None:
    """'1 3', '1,3', '2-4' or 'all' -> sorted 0-based indexes; None if invalid/out of range."""
    text = args.replace(",", " ").strip().lower()
    if not text:
        return None
    if text == "all":
        return list(range(count)) or None
    result = set()
    for part in text.split():
        lo, _, hi = part.partition("-")
        if not lo.isdigit() or (hi and not hi.isdigit()):
            return None
        start, end = int(lo), int(hi or lo)
        if start < 1 or end > count or start > end:
            return None
        result.update(range(start - 1, end))
    return sorted(result)


async def _selected_drafts(ctx: Ctx, usage: str) -> list[dict] | None:
    drafts = await get_store().get_pending_drafts(ctx.user.id)
    if not drafts:
        await reply(ctx.chat_id, "No pending drafts.")
        return None
    idx = parse_draft_numbers(ctx.args, len(drafts))
    if idx is None:
        await reply(ctx.chat_id, f"{usage}\nYou have {len(drafts)} pending draft(s), see /drafts.")
        return None
    return [drafts[i] for i in idx]


async def cmd_draft(ctx: Ctx) -> None:
    selected = await _selected_drafts(ctx, "Usage: /draft N")
    if not selected:
        return
    for d in selected[:3]:
        text, markup = draft_card(d, ctx.user, utcnow())
        await reply(ctx.chat_id, text, markup)


async def cmd_sent(ctx: Ctx) -> None:
    selected = await _selected_drafts(ctx, "Usage: /sent N… (e.g. /sent 1 3, /sent 2-4, /sent all)")
    if not selected:
        return
    store = get_store()
    for d in selected:
        await store.update_draft(d["id"], {"status": "sent", "status_changed_at": utcnow(), "status_source": "user"})
    await reply(ctx.chat_id, f"✅ {len(selected)} draft(s) marked as sent. /drafts shows the rest.")


async def cmd_deletedraft(ctx: Ctx) -> None:
    s = config.settings
    selected = await _selected_drafts(ctx, "Usage: /deletedraft N… (e.g. /deletedraft 1 3, /deletedraft all)")
    if not selected:
        return
    store = get_store()
    kept_in_gmail = 0
    for d in selected:
        account = await store.get_account(d.get("account", ""))
        if d.get("gmail_draft_id") and account and account.user_id == ctx.user.id:
            try:
                ok = await asyncio.to_thread(
                    delete_gmail_draft,
                    account=account,
                    client_id=s.google_client_id,
                    client_secret=s.google_client_secret,
                    draft_id=d["gmail_draft_id"],
                )
            except RefreshError:
                ok = False
            if not ok:
                kept_in_gmail += 1
        await store.delete_draft(d["id"])
    text = f"🗑 {len(selected)} draft(s) deleted."
    if kept_in_gmail:
        text += f" {kept_in_gmail} couldn't be removed from Gmail (no access) — delete them there manually."
    await reply(ctx.chat_id, text)


async def cmd_rewrite(ctx: Ctx) -> None:
    s = config.settings
    store = get_store()
    parts = ctx.args.split(maxsplit=1)
    if len(parts) < 2:
        await reply(ctx.chat_id, "Usage: /rewrite N <new context>")
        return
    try:
        idx = int(parts[0]) - 1
    except ValueError:
        await reply(ctx.chat_id, "N must be a number")
        return
    drafts = await store.get_pending_drafts(ctx.user.id)
    if idx < 0 or idx >= len(drafts):
        await reply(ctx.chat_id, "Draft number out of range")
        return
    draft = drafts[idx]

    account = await store.get_account(draft.get("account", ""))
    if account is None or account.user_id != ctx.user.id:
        await reply(ctx.chat_id, "The Google account of this draft is no longer linked. See /accounts.")
        return
    # Was it sent or deleted in Gmail meanwhile? Then don't rewrite (and don't spend AI tokens).
    try:
        gone = await asyncio.to_thread(
            check_drafts, account=account, client_id=s.google_client_id, client_secret=s.google_client_secret, drafts=[draft]
        )
    except RefreshError:
        await reply(ctx.chat_id, f"🔑 Access to {escape_markdown(account.email)} expired — send /connect to reconnect it.")
        return
    if draft["id"] in gone:
        status = gone[draft["id"]]
        await store.update_draft(draft["id"], {"status": status, "status_changed_at": utcnow(), "status_source": "gmail"})
        what = "already sent" if status == "sent" else "deleted in Gmail"
        await reply(ctx.chat_id, f"This draft was {what}, so I removed it from the list. See /drafts.")
        return
    llm = await _get_llm_or_reply(ctx)
    if llm is None:
        return
    ok, result = await _guarded(
        ctx,
        llm,
        rewrite_draft(
            to=draft["to"], subject=draft["subject"], body=draft["body"], context=parts[1], llm=llm, signature=s.signature
        ),
    )
    if not ok:
        return
    if not result:
        await reply(ctx.chat_id, "Failed to rewrite draft.")
        return
    new_subject = result.get("subject", draft["subject"])
    new_body = result.get("body", draft["body"])

    # Update the existing Gmail draft in place (no duplicates). Gmail first, so Firestore
    # never claims a text that isn't in the user's mailbox.
    old_gmail_id = draft.get("gmail_draft_id")
    try:
        gmail_id, created_new = await asyncio.to_thread(
            update_gmail_draft,
            account=account,
            client_id=s.google_client_id,
            client_secret=s.google_client_secret,
            draft_id=old_gmail_id,
            to=draft["to"],
            subject=new_subject,
            body=new_body,
            thread_id=draft.get("thread_id"),
            in_reply_to=draft.get("in_reply_to"),
        )
    except RefreshError:
        await reply(ctx.chat_id, f"🔑 Access to {escape_markdown(account.email)} expired — send /connect to reconnect it.")
        return
    if gmail_id is None:
        await reply(ctx.chat_id, "❌ Gmail rejected the draft update. Please try again later.")
        return
    await store.update_draft(draft["id"], {"subject": new_subject, "body": new_body, "gmail_draft_id": gmail_id})
    note = " (the original was no longer in Gmail, so a new draft was created)" if created_new and old_gmail_id else ""
    await reply(ctx.chat_id, f"✅ Draft updated in Gmail{note}.")


async def cmd_raw(ctx: Ctx) -> None:
    s = config.settings
    if not ctx.args:
        await reply(ctx.chat_id, "Usage: /raw <text>")
        return
    llm = await _get_llm_or_reply(ctx)
    if llm is None:
        return
    ok, response = await _guarded(
        ctx,
        llm,
        answer_question(
            question=f"Analyze this text. What action items, promises, or questions are in it?\n\n{ctx.args}",
            drafts_summary="",
            promises_summary="",
            last_activity="",
            llm=llm,
            signature=s.signature,
        ),
    )
    if ok:
        await reply(ctx.chat_id, response)


async def cmd_chat(ctx: Ctx) -> None:
    s = config.settings
    store = get_store()
    if not ctx.args:
        await reply(ctx.chat_id, "Usage: /chat <question>")
        return
    drafts = await store.get_pending_drafts(ctx.user.id)
    items = await store.list_open_items(ctx.user.id)
    runs = [a.last_run for a in await store.list_accounts(user_id=ctx.user.id) if a.last_run]
    llm = await _get_llm_or_reply(ctx)
    if llm is None:
        return
    ok, response = await _guarded(
        ctx,
        llm,
        answer_question(
            question=ctx.args,
            drafts_summary=str(len(drafts)) if drafts else "none",
            promises_summary="\n".join(
                f"- [{i.kind}] {i.title}" + (f" (due {fmt_local(i.due_at, ctx.user)})" if i.due_at else "")
                for i in items[:15]
            ) or "none",
            last_activity=max(runs).strftime("%b %d %H:%M") if runs else "never",
            llm=llm,
            signature=s.signature,
        ),
    )
    if ok:
        await reply(ctx.chat_id, response)


# ------------------------------------------------------------ focus & tasks
async def cmd_focus(ctx: Ctx) -> None:
    store = get_store()
    now = utcnow()
    entries = rank_focus(await store.list_open_items(ctx.user.id), await store.get_pending_drafts(ctx.user.id), now, limit=5)
    text, markup = focus_message(entries, ctx.user, now)
    await reply(ctx.chat_id, text, markup)


async def cmd_tasks(ctx: Ctx) -> None:
    items = await get_store().list_open_items(ctx.user.id)
    kind = {"mine": "promise", "promises": "promise", "waiting": "waiting", "replies": "reply", "todo": "todo"}.get(
        ctx.args.strip().lower()
    )
    if kind:
        items = [i for i in items if i.kind == kind]
    text, markup = tasks_message(items, ctx.user, utcnow())
    await reply(ctx.chat_id, text, markup)


async def cmd_task(ctx: Ctx) -> None:
    if not ctx.args:
        await reply(
            ctx.chat_id,
            "Usage: /task <text>, e.g. `/task send contract to Anna by Friday` or "
            "`/task Bob promised the invoice tomorrow`.\nYou can also just forward me any message.",
        )
        return
    await add_task_from_text(ctx.user, ctx.chat_id, ctx.args)


async def cmd_who(ctx: Ctx) -> None:
    store = get_store()
    now = utcnow()
    items = await store.list_open_items(ctx.user.id)
    drafts = await store.get_pending_drafts(ctx.user.id)
    query = ctx.args.strip()
    if not query:
        await reply(ctx.chat_id, people_message(items, drafts, now))
        return
    # match the query, then include everything with the same person (same email), e.g.
    # "/who alex smith" also finds items where the counterparty is just "alex@x.com"
    keys = {person_key(i.counterparty) for i in items if matches_person(query, i.counterparty)}
    keys |= {person_key(d.get("to", "")) for d in drafts if matches_person(query, d.get("to", ""))}
    keys.discard("")
    text, markup = who_message(
        query,
        [i for i in items if person_key(i.counterparty) in keys],
        [d for d in drafts if person_key(d.get("to", "")) in keys],
        ctx.user,
        now,
    )
    await reply(ctx.chat_id, text, markup)


async def cmd_review(ctx: Ctx) -> None:
    now = utcnow()
    queue = review_queue(await get_store().list_open_items(ctx.user.id), now)
    if not queue:
        await reply(ctx.chat_id, "🎉 Nothing to review — no old or forgotten items.")
        return
    text, markup = review_card(queue[0], ctx.user, now, left=len(queue))
    await reply(ctx.chat_id, text, markup)


# ------------------------------------------------------------ settings
async def cmd_timezone(ctx: Ctx) -> None:
    hours = " and ".join(f"{h:02d}:00" for h in summary_hours())
    if not ctx.args:
        now_local = fmt_local(utcnow(), ctx.user, "%H:%M")
        await reply(
            ctx.chat_id,
            f"🕐 Your timezone: {escape_markdown(user_tz_name(ctx.user))} (now {now_local}). Summaries at {hours}.\n\n"
            "Change it: /timezone <zone>, e.g. `/timezone Europe/Berlin`, `/timezone Jakarta`, `/timezone +7`",
        )
        return
    tz = parse_timezone(ctx.args)
    if tz is None:
        await reply(ctx.chat_id, "Unknown timezone. Try e.g. `Europe/Berlin`, `Asia/Jakarta`, `New York` or `+7`.")
        return
    store = get_store()
    updated = ctx.user.model_copy(update={"timezone": tz})
    now = utcnow()
    # Don't fire an extra summary right away just because the clock moved: start with the next slot.
    await store.update_user(ctx.user.id, {"timezone": tz, "last_run_slot": current_slot(now, user_tz(updated))})
    next_local = fmt_local(next_slot(now, user_tz(updated)), updated, "%b %d %H:%M")
    await reply(
        ctx.chat_id,
        f"✅ Timezone set to {escape_markdown(tz)} (now {fmt_local(now, updated, '%H:%M')}). "
        f"Summaries at {hours}; the next one at {next_local}.",
    )


async def cmd_deleteme(ctx: Ctx) -> None:
    store = get_store()
    if ctx.user.is_admin:
        others = [u for u in await store.list_users() if u.id != ctx.user.id and u.is_active]
        if others:
            await reply(
                ctx.chat_id,
                "You are the administrator. Make another user the admin first with /transferadmin <user id> "
                "(see /users), then delete your account.",
            )
            return
    if ctx.args.strip().upper() != "YES":
        await reply(
            ctx.chat_id,
            "⚠️ This permanently deletes your Lola account: linked Google accounts (access is revoked), "
            "drafts, promises, settings and your API key. Drafts already in Gmail stay there.\n\n"
            "To confirm, send: `/deleteme YES`",
        )
        return
    accounts = await store.delete_user(ctx.user.id)
    for acc in accounts:
        await revoke_token(acc.refresh_token)
    await reply(
        ctx.chat_id,
        f"👋 Your account and all data were deleted ({len(accounts)} Google account(s) disconnected). "
        + ("The bot is reset: the next person to send /start becomes the admin." if ctx.user.is_admin
           else "To use Lola again you'll need a new invitation."),
    )


# ------------------------------------------------------------ own LLM key
def _providers_text() -> str:
    return "\n".join(
        f"• `{p.name}` — {p.label}, default model `{p.default_model}` — key: {escape_markdown(p.key_url)}"
        for p in PROVIDERS.values()
    )


SETKEY_USAGE = (
    "Usage: /setkey <provider> <api key> [model]\n"
    "Example: /setkey gemini AIza... gemini-2.5-flash\n\n"
    "Providers:\n"
)


async def cmd_llm(ctx: Ctx) -> None:
    st = await usage_status(ctx.user)
    if st.own_key:
        lines = [f"🤖 Using *your own* {st.provider} key (`{st.model}`) — no limits.", "/removekey to switch back to the shared assistant."]
    else:
        provider = f"{st.provider} (`{st.model}`)" if st.provider else "not configured"
        lines = [f"🤖 Using the shared assistant: {provider}"]
        if st.limit is None:
            lines.append("Limit: unlimited")
        else:
            pct = min(100, round(100 * st.used / st.limit)) if st.limit else 100
            resets = f" at {fmt_local(st.resets_by, ctx.user, '%H:%M')}" if st.resets_by else ""
            lines.append(f"Used this period: {_fmt_tokens(st.used)} / {_fmt_tokens(st.limit)} tokens ({pct}%)")
            lines.append(f"Resets with your next summary{resets}.")
            lines.append("When the limit is used up, drafting and questions pause until then; summaries keep coming.")
        lines.append("\nUse your own API key without limits:\n/setkey <provider> <api key> [model]\n\n" + _providers_text())
    await reply(ctx.chat_id, "\n".join(lines))


async def cmd_setkey(ctx: Ctx) -> None:
    parts = ctx.args.split()
    deleted = True
    if len(parts) >= 2 and ctx.message_id:
        # the message contains a secret: remove it from the chat history right away
        deleted = await delete_message(config.settings.telegram_bot_token, ctx.chat_id, ctx.message_id)
    if len(parts) < 2 or len(parts) > 3:
        await reply(ctx.chat_id, SETKEY_USAGE + _providers_text())
        return
    provider, api_key = parts[0].lower(), parts[1]
    model = parts[2] if len(parts) == 3 else ""
    if provider not in PROVIDERS:
        await reply(ctx.chat_id, f"Unknown provider. Choose one of: {', '.join(PROVIDERS)}")
        return
    cfg = user_llm_config(provider, api_key, model)
    try:
        await chat_completion(cfg, [{"role": "user", "content": "Reply with OK"}], max_tokens=32)
    except LLMError as exc:
        reasons = {
            401: "the key was rejected",
            403: "the key was rejected",
            404: f"model `{cfg.model}` was not found",
            400: f"the request was rejected — check the model name `{cfg.model}`",
            429: "the key has no quota or credit left",
        }
        await reply(ctx.chat_id, f"❌ Key check failed: {reasons.get(exc.status, escape_markdown(str(exc)[:150]))}. Nothing was saved.")
        return
    await get_store().set_llm_key(ctx.user.id, UserLLMKey(provider=provider, model=model, api_key=api_key))
    text = (
        f"✅ Lola now uses your own {PROVIDERS[provider].label} key (`{cfg.model}`) — no limits. "
        "The key is stored encrypted. /removekey to switch back to the shared assistant."
    )
    if not deleted:
        text += "\n\n⚠️ I couldn't delete your message with the key — please delete it yourself."
    await reply(ctx.chat_id, text)


async def cmd_removekey(ctx: Ctx) -> None:
    removed = await get_store().delete_llm_key(ctx.user.id)
    if removed:
        limit = effective_limit(ctx.user)
        limit_text = "unlimited" if limit is None else f"{_fmt_tokens(limit)} tokens per 12h"
        await reply(ctx.chat_id, f"✅ Your API key was deleted. Back to the shared assistant ({limit_text}).")
    else:
        await reply(ctx.chat_id, "You don't have your own API key set. See /llm.")


# ------------------------------------------------------------- admin commands
async def cmd_invite(ctx: Ctx) -> None:
    invite = await get_store().create_invite(created_by=ctx.user.id, note=ctx.args[:100])
    username = config.settings.telegram_bot_username or await get_bot_username(config.settings.telegram_bot_token)
    expires = fmt_local(invite.expires_at, ctx.user)
    if username:
        link = f"https://t.me/{username}?start={invite.code}"
        await reply(
            ctx.chat_id,
            f"✉️ Invitation link (single use, valid until {expires}):\n\n`{link}`\n\n"
            "Send it to the person you want to invite. They open it, press Start and link their Google account.",
        )
    else:
        await reply(ctx.chat_id, f"✉️ Invitation code (valid until {expires}). The person should send me:\n\n`/start {invite.code}`")


async def cmd_invites(ctx: Ctx) -> None:
    invites = await get_store().list_invites(active_only=True)
    if not invites:
        await reply(ctx.chat_id, "No active invitations. Create one with /invite [note].")
        return
    lines = ["✉️ Active invitations:\n"]
    for inv in invites:
        note = f" — {escape_markdown(inv.note)}" if inv.note else ""
        lines.append(f"`{inv.code}`{note} (until {fmt_local(inv.expires_at, ctx.user)})")
    lines.append("\nCancel with /revoke <code>.")
    await reply(ctx.chat_id, "\n".join(lines))


async def cmd_revoke(ctx: Ctx) -> None:
    if not ctx.args:
        await reply(ctx.chat_id, "Usage: /revoke <code>")
        return
    ok = await get_store().revoke_invite(ctx.args.split()[0])
    await reply(ctx.chat_id, "✅ Invitation revoked." if ok else "No such invitation.")


async def cmd_users(ctx: Ctx) -> None:
    store = get_store()
    users = sorted(await store.list_users(), key=lambda u: u.created_at)
    lines = [f"👥 Users ({len(users)}):\n"]
    for u in users:
        n_acc = len(await store.list_accounts(user_id=u.id, enabled_only=False))
        role = "👑" if u.is_admin else "👤"
        status = "" if u.is_active else " ⛔ disabled"
        st = await usage_status(u)
        if st.own_key:
            ai = f"own {st.provider} key"
        elif st.limit is None:
            ai = f"AI {_fmt_tokens(st.used)} (unlimited)"
        else:
            ai = f"AI {_fmt_tokens(st.used)}/{_fmt_tokens(st.limit)}"
        lines.append(f"{role} `{u.id}` {escape_markdown(u.display_name)} — {n_acc} account(s), {ai}{status}")
    await reply(ctx.chat_id, "\n".join(lines))


async def _target_user(ctx: Ctx, usage: str) -> User | None:
    try:
        target_id = int(ctx.args.split()[0])
    except (IndexError, ValueError):
        await reply(ctx.chat_id, usage)
        return None
    if target_id == ctx.user.id:
        await reply(ctx.chat_id, "You can't do that to yourself.")
        return None
    target = await get_store().get_user(target_id)
    if target is None:
        await reply(ctx.chat_id, "No such user. See /users.")
        return None
    if target.is_admin:
        await reply(ctx.chat_id, "That user is an administrator.")
        return None
    return target


async def cmd_disable(ctx: Ctx) -> None:
    target = await _target_user(ctx, "Usage: /disable <user id>")
    if target:
        await get_store().update_user(target.id, {"status": "disabled"})
        await reply(ctx.chat_id, f"⛔ {escape_markdown(target.display_name)} disabled. Their data is kept; /enable to restore.")


async def cmd_enable(ctx: Ctx) -> None:
    target = await _target_user(ctx, "Usage: /enable <user id>")
    if target:
        await get_store().update_user(target.id, {"status": "active"})
        await reply(ctx.chat_id, f"✅ {escape_markdown(target.display_name)} enabled.")


async def cmd_remove(ctx: Ctx) -> None:
    target = await _target_user(ctx, "Usage: /remove <user id>")
    if target:
        accounts = await get_store().delete_user(target.id)
        for acc in accounts:
            await revoke_token(acc.refresh_token)
        await reply(
            ctx.chat_id,
            f"🗑 {escape_markdown(target.display_name)} removed with {len(accounts)} linked account(s), drafts and promises.",
        )


def _parse_limit(value: str) -> int | None | str:
    """'default' -> None, 'unlimited'/'0' -> 0, '200k' / '200000' -> int; invalid -> 'invalid'."""
    v = value.strip().lower().replace("_", "").replace(",", "")
    if v == "default":
        return None
    if v in ("unlimited", "0"):
        return 0
    mult = 1_000 if v.endswith("k") else 1_000_000 if v.endswith("m") else 1
    v = v.rstrip("km")
    return int(float(v) * mult) if v.replace(".", "", 1).isdigit() and float(v) > 0 else "invalid"


async def cmd_setlimit(ctx: Ctx) -> None:
    usage = "Usage: /setlimit <user id> <tokens|default|unlimited>\nExample: /setlimit 123456 300k"
    parts = ctx.args.split()
    if len(parts) != 2 or not parts[0].lstrip("-").isdigit():
        await reply(ctx.chat_id, usage)
        return
    limit = _parse_limit(parts[1])
    if limit == "invalid":
        await reply(ctx.chat_id, usage)
        return
    store = get_store()
    target = await store.get_user(int(parts[0]))
    if target is None:
        await reply(ctx.chat_id, "No such user. See /users.")
        return
    await store.update_user(target.id, {"token_limit": limit})
    if limit == 0:
        shown = "unlimited"
    else:
        tokens = config.settings.user_token_limit if limit is None else limit
        shown = f"{_fmt_tokens(tokens)} tokens per 12h" + (" (default)" if limit is None else "")
    note = " — note: admins are never limited" if target.is_admin else ""
    await reply(ctx.chat_id, f"✅ AI limit for {escape_markdown(target.display_name)}: {shown}{note}.")


async def cmd_transferadmin(ctx: Ctx) -> None:
    target = await _target_user(ctx, "Usage: /transferadmin <user id>")
    if target is None:
        return
    if not target.is_active:
        await reply(ctx.chat_id, "That user is disabled. /enable them first.")
        return
    if not await get_store().transfer_admin(ctx.user.id, target.id):
        await reply(ctx.chat_id, "❌ Could not transfer the admin role.")
        return
    await reply(ctx.chat_id, f"✅ {escape_markdown(target.display_name)} is now the administrator. You are a regular user.")
    await reply(target.chat_id, "👑 You are now the administrator of Lola. Send /help to see admin commands.")


Handler = Callable[[Ctx], Awaitable[None]]

USER_COMMANDS: dict[str, Handler] = {
    "/start": cmd_start,
    "/help": cmd_help,
    "/connect": cmd_connect,
    "/accounts": cmd_accounts,
    "/disconnect": cmd_disconnect,
    "/focus": cmd_focus,
    "/tasks": cmd_tasks,
    "/task": cmd_task,
    "/who": cmd_who,
    "/review": cmd_review,
    "/unlink": cmd_unlink,
    "/draft": cmd_draft,
    "/sent": cmd_sent,
    "/deletedraft": cmd_deletedraft,
    "/timezone": cmd_timezone,
    "/deleteme": cmd_deleteme,
    "/status": cmd_status,
    "/drafts": cmd_drafts,
    "/rewrite": cmd_rewrite,
    "/raw": cmd_raw,
    "/chat": cmd_chat,
    "/llm": cmd_llm,
    "/setkey": cmd_setkey,
    "/removekey": cmd_removekey,
}

ADMIN_COMMANDS: dict[str, Handler] = {
    "/invite": cmd_invite,
    "/invites": cmd_invites,
    "/revoke": cmd_revoke,
    "/users": cmd_users,
    "/disable": cmd_disable,
    "/enable": cmd_enable,
    "/remove": cmd_remove,
    "/setlimit": cmd_setlimit,
    "/transferadmin": cmd_transferadmin,
}
