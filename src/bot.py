"""Telegram bot: registration, Google account linking, assistant and admin commands.

Identity is the Telegram *user id* (``message.from.id``). The bot only works in
private chats, where chat id == user id, so replies always go to the sender.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from src import config
from src.gmail_client import DraftGone, create_draft, update_draft
from src.llm_client import LLM, PROVIDERS, answer_question, check_key, rewrite_draft, user_llm
from src.models import User
from src.oauth import make_code_verifier, revoke_token
from src.quota import get_budget, limit_message, record_usage, resolve_llm
from src.state import RedeemResult, get_store, normalize_email
from src.telegram_client import delete_message, escape_markdown, get_bot_username, send_message, url_button

log = logging.getLogger(__name__)

USER_HELP = (
    "/status — current state\n"
    "/drafts — pending email drafts\n"
    "/rewrite N <context> — update draft N\n"
    "/raw <text> — analyze text\n"
    "/chat <question> — ask about your state\n"
    "/connect — link a Google account\n"
    "/accounts — your linked Google accounts\n"
    "/disconnect <email> — unlink a Google account\n"
    "/usage — AI usage and limits\n"
    "/setkey <provider> <key> [model] — use your own AI key (no limits)\n"
    "/removekey — remove your own AI key\n"
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
    "/remove <user id> — delete a user and all their data"
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


async def llm_for_command(ctx: Ctx) -> LLM | None:
    """LLM for an interactive AI command, or None (user already told why) if over the limit."""
    llm = await resolve_llm(ctx.user)
    if llm is None:
        await reply(ctx.chat_id, "⚠️ No AI provider is configured on the server. Add your own key with /setkey.")
        return None
    budget = await get_budget(ctx.user, llm)
    if budget.exhausted:
        await reply(ctx.chat_id, limit_message(budget))
        return None
    return llm


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
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
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
            await send_connect_prompt(chat_id, new_user.id, base_url, intro + "\n\nNext step: link your Google account.")
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
            "Next step: link your Google account.",
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
        last = acc.last_run.strftime("%b %d %H:%M UTC") if acc.last_run else "not checked yet"
        lines.append(f"{status} {escape_markdown(acc.email)} — {', '.join(acc.providers) or 'no access'} — {last}")
    lines.append("\n/connect to add another, /disconnect <email> to remove.")
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
    await reply(ctx.chat_id, f"✅ {escape_markdown(account.email)} disconnected and access revoked.")


async def cmd_status(ctx: Ctx) -> None:
    store = get_store()
    drafts = await store.get_pending_drafts(ctx.user.id)
    promises = await store.get_open_promises(ctx.user.id)
    accounts = await store.list_accounts(user_id=ctx.user.id)
    runs = [a.last_run for a in accounts if a.last_run]
    last_run = max(runs).strftime("%b %d %H:%M UTC") if runs else "never"
    draft_text = f"{len(drafts)} pending drafts" if drafts else "no pending drafts"
    promise_text = f"{len(promises)} open promises" if promises else "no open promises"
    await reply(
        ctx.chat_id,
        f"📋 Status\n\n🔗 {len(accounts)} linked account(s)\n📧 {draft_text}\n⚠️ {promise_text}\n🕐 Last check: {last_run}",
    )


async def cmd_drafts(ctx: Ctx) -> None:
    drafts = await get_store().get_pending_drafts(ctx.user.id)
    if not drafts:
        await reply(ctx.chat_id, "No pending drafts.")
        return
    lines = []
    for i, d in enumerate(drafts, 1):
        to_esc = escape_markdown(d.get("to", "?"))
        subj_esc = escape_markdown(d.get("subject", "?"))
        body_preview = escape_markdown(d.get("body", "")[:80].replace("\n", " "))
        lines.append(f"{i}. To: {to_esc} — \"{subj_esc}\"")
        lines.append(f"   {body_preview}...")
    entries = "\n".join(lines)
    await reply(ctx.chat_id, f"📧 Pending drafts:\n\n{entries}\n\nUse /rewrite N <context> to update a draft.")


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
        await reply(ctx.chat_id, "The Google account of this draft is no longer linked.")
        return

    llm = await llm_for_command(ctx)
    if llm is None:
        return
    try:
        result = await rewrite_draft(
            to=draft["to"],
            subject=draft["subject"],
            body=draft["body"],
            context=parts[1],
            llm=llm,
            signature=s.signature,
        )
    finally:
        await record_usage(ctx.user, llm)
    if not result:
        await reply(ctx.chat_id, "Failed to rewrite draft.")
        return
    new_subject = result.get("subject") or draft["subject"]
    new_body = result.get("body") or draft["body"]

    # Update the existing Gmail draft in place (no duplicates). If it's gone — sent or
    # deleted in Gmail — or was never created, create a new one.
    gmail_kwargs = dict(
        account=account,
        client_id=s.google_client_id,
        client_secret=s.google_client_secret,
        to=draft["to"],
        subject=new_subject,
        body=new_body,
        thread_id=draft.get("thread_id"),
    )
    old_id = draft.get("gmail_draft_id")
    note = ""
    gmail_id = None
    if old_id:
        try:
            gmail_id = await asyncio.to_thread(update_draft, draft_id=old_id, **gmail_kwargs)
        except DraftGone:
            note = "\nThe previous version was no longer in Gmail (sent or deleted), so I created a new draft."
    if not gmail_id:
        gmail_id = await asyncio.to_thread(create_draft, **gmail_kwargs)
    if not gmail_id:
        await reply(ctx.chat_id, "❌ Could not save the draft in Gmail. Please try again later.")
        return

    await store.update_draft(draft["id"], {"subject": new_subject, "body": new_body, "gmail_draft_id": gmail_id})
    await reply(ctx.chat_id, "✅ Draft updated in Gmail." + note)


async def cmd_raw(ctx: Ctx) -> None:
    s = config.settings
    if not ctx.args:
        await reply(ctx.chat_id, "Usage: /raw <text>")
        return
    llm = await llm_for_command(ctx)
    if llm is None:
        return
    try:
        response = await answer_question(
            question=f"Analyze this text. What action items, promises, or questions are in it?\n\n{ctx.args}",
            drafts_summary="",
            promises_summary="",
            last_activity="",
            llm=llm,
            signature=s.signature,
        )
    finally:
        await record_usage(ctx.user, llm)
    await reply(ctx.chat_id, response)


async def cmd_chat(ctx: Ctx) -> None:
    s = config.settings
    store = get_store()
    if not ctx.args:
        await reply(ctx.chat_id, "Usage: /chat <question>")
        return
    llm = await llm_for_command(ctx)
    if llm is None:
        return
    drafts = await store.get_pending_drafts(ctx.user.id)
    promises = await store.get_open_promises(ctx.user.id)
    runs = [a.last_run for a in await store.list_accounts(user_id=ctx.user.id) if a.last_run]
    try:
        response = await answer_question(
            question=ctx.args,
            drafts_summary=str(len(drafts)) if drafts else "none",
            promises_summary="\n".join(f"- {escape_markdown(p.get('text', ''))}" for p in promises[:5]) if promises else "none",
            last_activity=max(runs).strftime("%b %d %H:%M") if runs else "never",
            llm=llm,
            signature=s.signature,
        )
    finally:
        await record_usage(ctx.user, llm)
    await reply(ctx.chat_id, response)


# ------------------------------------------------------------- AI key / usage
def _providers_help() -> str:
    lines = [f"• `{p.name}` — {p.label}, default model `{p.default_model}` (key: {p.key_help})" for p in PROVIDERS.values()]
    return "\n".join(lines)


async def cmd_usage(ctx: Ctx) -> None:
    llm = await resolve_llm(ctx.user)
    if llm is None:
        await reply(ctx.chat_id, "⚠️ No AI provider is configured on the server. Add your own key with /setkey.")
        return
    b = await get_budget(ctx.user, llm)
    source = "your own key" if llm.own_key else "shared key"
    lines = [f"🤖 AI: {escape_markdown(llm.label)} — {source}"]
    if b.unlimited:
        lines.append(f"✅ No AI limits. Up to {b.email_limit} emails are analysed per run.")
        lines.append(f"Used this period: {b.tokens_used:,} tokens, {b.emails_used} emails.")
    else:
        pct = min(100, round(100 * b.tokens_used / b.token_limit)) if b.token_limit else 0
        lines.append(f"Used this period: {b.tokens_used:,} / {b.token_limit:,} tokens ({pct}%)")
        lines.append(f"Emails analysed: {b.emails_used} / {b.email_limit}")
        if b.exhausted:
            lines.append("⏳ Limit reached — drafts, /rewrite, /chat and /raw are paused until the next run.")
    when = b.resets_at.strftime("%b %d %H:%M UTC") if b.resets_at else "next scheduled run"
    lines.append(f"🔄 New period: {when}")
    if not llm.own_key:
        lines.append("\nUse your own API key without limits: /setkey")
    await reply(ctx.chat_id, "\n".join(lines))


async def cmd_setkey(ctx: Ctx) -> None:
    parts = ctx.args.split()
    if len(parts) < 2:
        await reply(
            ctx.chat_id,
            "🔑 Use your own AI key — no limits, billed to your account.\n\n"
            "Usage: /setkey <provider> <api key> [model]\n\n"
            f"Providers:\n{_providers_help()}\n\n"
            "Example: `/setkey gemini AIza... gemini-2.5-flash`\n"
            "I delete your message right away and store the key encrypted.",
        )
        return
    # the message contains a secret: remove it from the chat history immediately
    if ctx.message_id:
        await delete_message(config.settings.telegram_bot_token, ctx.chat_id, ctx.message_id)

    provider, api_key = parts[0].lower(), parts[1]
    model = parts[2] if len(parts) > 2 else ""
    if provider not in PROVIDERS:
        await reply(ctx.chat_id, f"Unknown provider. Choose one of: {', '.join(PROVIDERS)}")
        return
    llm = user_llm(provider, api_key, model)
    ok, problem = await check_key(llm)
    if not ok:
        await reply(ctx.chat_id, f"❌ Key not saved: {escape_markdown(problem)}.")
        return
    await get_store().set_user_llm(ctx.user.id, provider, api_key, llm.model)
    warning = f"\n⚠️ Note: {escape_markdown(problem)}." if problem else ""
    await reply(
        ctx.chat_id,
        f"✅ Saved. I'll use {escape_markdown(llm.label)} with your key — no limits.{warning}\n"
        "Your message with the key was deleted. /removekey to go back to the shared AI.",
    )


async def cmd_removekey(ctx: Ctx) -> None:
    removed = await get_store().delete_user_llm(ctx.user.id)
    if removed:
        await reply(ctx.chat_id, "✅ Your API key was deleted. Back to the shared AI (with limits). See /usage.")
    else:
        await reply(ctx.chat_id, "You don't have your own API key set. See /setkey.")


# ------------------------------------------------------------- admin commands
async def cmd_invite(ctx: Ctx) -> None:
    invite = await get_store().create_invite(created_by=ctx.user.id, note=ctx.args[:100])
    username = config.settings.telegram_bot_username or await get_bot_username(config.settings.telegram_bot_token)
    expires = invite.expires_at.strftime("%b %d %H:%M UTC")
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
        lines.append(f"`{inv.code}`{note} (until {inv.expires_at.strftime('%b %d %H:%M UTC')})")
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
        lines.append(f"{role} `{u.id}` {escape_markdown(u.display_name)} — {n_acc} account(s){status}")
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


Handler = Callable[[Ctx], Awaitable[None]]

USER_COMMANDS: dict[str, Handler] = {
    "/start": cmd_start,
    "/help": cmd_help,
    "/connect": cmd_connect,
    "/accounts": cmd_accounts,
    "/disconnect": cmd_disconnect,
    "/status": cmd_status,
    "/drafts": cmd_drafts,
    "/rewrite": cmd_rewrite,
    "/raw": cmd_raw,
    "/chat": cmd_chat,
    "/usage": cmd_usage,
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
}
