"""Scheduled processing: every active user gets their own digest for their own accounts,
at 08:00 and 20:00 in their own timezone (the scheduler calls /run hourly)."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from google.auth.exceptions import RefreshError

from src import config
from src.actions import add_item, apply_update
from src.cards import entry_line, focus_message
from src.chat_client import fetch_messages, list_spaces, space_link
from src.drafts_sync import sync_note, sync_user_drafts
from src.gmail_client import create_draft, fetch_recent_messages, thread_link
from src.items import rank_focus
from src.llm_client import LLM, AnalysisContext, Analysis, LLMError, analyze_chat, analyze_emails, analyze_sent, compose_summary
from src.models import Account, User, utcnow
from src.quota import LLMUnavailable, QuotaExceeded, get_llm, start_window
from src.schedule import current_slot, due_slot, fmt_local, local_date, user_tz, user_tz_name
from src.state import get_store
from src.telegram_client import escape_markdown, send_message

log = logging.getLogger(__name__)


@dataclass
class UserRunResult:
    email_items: list[str] = field(default_factory=list)
    chat_items: list[str] = field(default_factory=list)
    new_items: int = 0
    closed_items: int = 0
    drafts_created: int = 0
    reauth_needed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    quota_hit: bool = False
    deferred: list[str] = field(default_factory=list)
    llm_error: str = ""


def _is_revoked(exc: BaseException) -> bool:
    """Token can't be used any more and the user must sign in again.

    invalid_grant: revoked / expired (e.g. 7-day limit for apps in Testing status)
    unauthorized_client: token was issued to a different OAuth client (old Desktop client)
    """
    return isinstance(exc, RefreshError) and any(code in str(exc) for code in ("invalid_grant", "unauthorized_client"))


async def run_all_users(now: datetime | None = None, force: bool = False) -> dict:
    """Process every active user whose summary slot is due (bounded parallelism).

    ``force`` processes all active users regardless of their schedule (manual runs).
    One user's failure never affects others.
    """
    now = now or utcnow()
    log.info("Starting scheduled run (force=%s)...", force)
    store = get_store()
    sem = asyncio.Semaphore(max(1, config.settings.run_concurrency))
    stats = {"users": 0, "skipped": 0, "errors": 0, "deferred": 0, "not_due": 0}
    deadline = time.monotonic() + config.settings.run_soft_deadline_seconds

    due: list[tuple[User, datetime]] = []
    for user in await store.list_users():
        if not user.is_active:
            continue
        slot = current_slot(now, user_tz(user)) if force else due_slot(user, now)
        if slot is None:
            stats["not_due"] += 1
        else:
            due.append((user, slot))

    async def one(user: User, slot: datetime):
        async with sem:
            if time.monotonic() > deadline:
                # slot not claimed, watermarks untouched: caught up at the next hourly run
                stats["deferred"] += 1
                log.warning("Run deadline reached — user %s deferred to next run", user.id)
                return
            if not await store.claim_run_slot(user.id, slot) and not force:
                stats["not_due"] += 1  # processed concurrently by another /run
                return
            try:
                processed = await run_for_user(user, deadline, now=now)
                stats["users" if processed else "skipped"] += 1
            except Exception as exc:  # noqa: BLE001 — slot stays claimed: no hourly retry loop
                stats["errors"] += 1
                log.error("Run failed for user %s: %s", user.id, exc, exc_info=True)

    await asyncio.gather(*(one(u, slot) for u, slot in due))
    log.info("Scheduled run complete: %s", stats)
    return stats


async def run_for_user(user: User, deadline: float | None = None, now: datetime | None = None) -> bool:
    """Process all accounts of ``user`` and send them a digest. Returns False if nothing to do."""
    s = config.settings
    store = get_store()
    accounts = await store.list_accounts(user_id=user.id)
    if not accounts:
        return False

    now = now or utcnow()
    draft_sync = await sync_user_drafts(user.id)  # drafts sent/deleted in Gmail since last time
    await start_window(user.id)  # every scheduled run starts a fresh 12h limit
    try:
        llm = await get_llm(user)
    except LLMUnavailable:
        log.error("No LLM configured (set GEMINI_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY); skipping user %s", user.id)
        return False

    result = UserRunResult()
    for account in accounts:
        if account.needs_reauth:
            result.reauth_needed.append(account.email)
            continue
        if deadline is not None and time.monotonic() > deadline:
            log.warning("Run deadline reached — account %s deferred to next run", account.email)
            result.deferred.append(account.email)
            continue
        await _process_account(user, account, result, llm, now)

    # What matters now: ranked open items + drafts ready to send
    open_items = await store.list_open_items(user.id)
    focus = rank_focus(open_items, await store.get_pending_drafts(user.id), now, limit=5)
    focus_text = "\n".join(f"{n}. {entry_line(e, user, now)}" for n, e in enumerate(focus, 1)) or "nothing urgent"
    tracked = [i for i in open_items if i.kind in ("promise", "waiting")][:10]
    promises_text = "\n".join(
        f"- [{i.kind}] {escape_markdown(i.title)}" + (f" (due {fmt_local(i.due_at, user)})" if i.due_at else "")
        for i in tracked
    ) or "none"
    email_text = "\n".join(f"- {escape_markdown(i)}" for i in result.email_items[:10]) or "no new emails"
    chat_text = "\n".join(f"- {escape_markdown(i)}" for i in result.chat_items[:10]) or "no new chat activity"

    date = local_date(now, user).strftime("%b %d, %Y")
    if len(result.reauth_needed) + len(result.deferred) == len(accounts):
        summary = ""  # nothing was processed — don't spend an LLM call
    else:
        try:
            summary = await compose_summary(
                email_summary=email_text,
                chat_summary=chat_text,
                promises=promises_text,
                date=date,
                llm=llm,
                signature=s.signature,
                focus=focus_text,
            )
        except LLMError as exc:
            log.error("Summary LLM call failed for user %s: %s", user.id, exc)
            result.llm_error = result.llm_error or str(exc)
            summary = (
                f"📋 Summary — {date}\n\n🎯 Focus:\n{focus_text}\n\n📧 Email:\n{email_text}\n\n💬 Chat:\n{chat_text}"
            )

    notes = []
    if result.new_items or result.closed_items:
        parts = [f"{result.new_items} new"] if result.new_items else []
        parts += [f"{result.closed_items} resolved"] if result.closed_items else []
        notes.append(f"📌 Promises & tasks: {', '.join(parts)} — /tasks shows all.")
    if sync_note(draft_sync):
        notes.append(sync_note(draft_sync))
    if result.quota_hit:
        notes.append(
            "⏳ AI limit reached — part of your new mail/chat wasn't analyzed and no drafts were written for it; "
            "it will be picked up at the next run. Add your own API key with /setkey to use Lola without limits."
        )
    if result.llm_error and llm.config.own_key:
        notes.append(
            f"⚠️ Your own {llm.config.provider} API key failed ({escape_markdown(result.llm_error[:120])}). "
            "Fix it with /setkey, or /removekey to use the shared assistant."
        )
    for email in result.reauth_needed:
        notes.append(f"🔑 Access to {escape_markdown(email)} expired or was revoked — send /connect to reconnect it.")
    for email in result.deferred:
        notes.append(f"⏱ {escape_markdown(email)} will be checked at the next run (this run ran out of time).")
    for email in result.failed:
        notes.append(f"⚠️ Could not check {escape_markdown(email)} this time; will retry next run.")
    text = "\n\n".join(part for part in [summary, "\n".join(notes)] if part)

    if text:
        await send_message(s.telegram_bot_token, user.chat_id, text)
        if focus:
            focus_text_md, markup = focus_message(focus, user, now)
            await send_message(s.telegram_bot_token, user.chat_id, focus_text_md, reply_markup=markup)
    log.info("User %s processed: %d drafts, %d new items", user.id, result.drafts_created, result.new_items)
    return True


async def _context(user: User, now: datetime) -> AnalysisContext:
    return AnalysisContext(now=now, tz=user_tz(user), tz_name=user_tz_name(user),
                           open_items=await get_store().list_open_items(user.id))


async def _apply(user: User, analysis: Analysis, result: UserRunResult, *, source: str, account: Account,
                 messages: list[dict] | None = None, space: dict | None = None) -> None:
    """Store extracted items (deduplicated) and apply updates to open items."""
    by_id = {m["id"]: m for m in messages or []}
    for update in analysis.updates:
        await apply_update(user, update)
        if update.status:
            result.closed_items += 1
    for extracted in analysis.items:
        message = by_id.get(extracted.original_email_id or "")
        link = thread_link(account.email, message.get("thread_id")) if message else (space_link(space) if space else "")
        if await add_item(user, extracted, source=source, account=account.email, message=message, space=space, link=link):
            result.new_items += 1


async def _process_account(user: User, account: Account, result: UserRunResult, llm: LLM, now: datetime) -> None:
    s = config.settings
    store = get_store()
    started = utcnow()
    lookback = started - timedelta(hours=s.initial_lookback_hours)

    def since(provider: str) -> datetime:
        if provider in account.provider_last_run:
            return account.provider_last_run[provider]
        # accounts migrated from the single-user version only have the global last_run
        return (account.last_run if not account.provider_last_run else None) or lookback

    succeeded: list[str] = []
    failed = False
    quota_hit = False
    log.info("Processing account %s (user %s)", account.email, user.id)

    async def step(provider: str, work) -> bool:
        """Run one provider; False stops the account (token revoked)."""
        nonlocal failed, quota_hit
        if quota_hit:
            return True
        try:
            if await work() is not False:
                succeeded.append(provider)
        except QuotaExceeded:
            # watermark not advanced: analyzed at the next run with a fresh limit
            quota_hit = result.quota_hit = True
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, LLMError):
                result.llm_error = result.llm_error or str(exc)
            if _is_revoked(exc):
                await _mark_reauth(account, result)
                return False
            failed = True
            log.error("%s processing failed for %s: %s", provider, account.email, exc, exc_info=True)
        return True

    async def inbox():
        messages = await asyncio.to_thread(
            fetch_recent_messages, account=account, client_id=s.google_client_id,
            client_secret=s.google_client_secret, last_run=since("gmail"),
        )
        if not messages:
            return
        analysis = await analyze_emails(messages, account.email, llm, s.signature, await _context(user, now))
        for draft in analysis.drafts:
            draft_id = await store.save_draft(user.id, draft.model_dump())
            gmail_draft_id = await asyncio.to_thread(
                create_draft, account=account, client_id=s.google_client_id, client_secret=s.google_client_secret,
                to=draft.to, subject=draft.subject, body=draft.body, thread_id=draft.thread_id,
                in_reply_to=draft.in_reply_to,
            )
            if gmail_draft_id:
                await store.update_draft(draft_id, {"gmail_draft_id": gmail_draft_id})
                result.drafts_created += 1
            else:
                log.error("Failed to create Gmail draft for %s", account.email)
        await _apply(user, analysis, result, source="email", account=account, messages=messages)
        result.email_items.extend(analysis.summary_items)

    async def sent():
        messages = await asyncio.to_thread(
            fetch_recent_messages, account=account, client_id=s.google_client_id,
            client_secret=s.google_client_secret, last_run=since("gmail_sent"), sent=True,
        )
        if not messages:
            return
        analysis = await analyze_sent(messages, account.email, llm, await _context(user, now))
        await _apply(user, analysis, result, source="sent", account=account, messages=messages)

    async def chat():
        spaces = await asyncio.to_thread(
            list_spaces, account=account, client_id=s.google_client_id, client_secret=s.google_client_secret
        )
        for space in spaces:
            messages = await asyncio.to_thread(
                fetch_messages, account=account, client_id=s.google_client_id,
                client_secret=s.google_client_secret, space=space, last_run=since("chat"),
            )
            if not messages:
                continue
            analysis = await analyze_chat(messages, space.get("displayName", space["name"]), llm, await _context(user, now))
            await _apply(user, analysis, result, source="chat", account=account, space=space)
            result.chat_items.extend(analysis.summary_items)

    if "gmail" in account.providers:
        if not await step("gmail", inbox):
            return
        if not await step("gmail_sent", sent):
            return
    if "chat" in account.providers:
        if not await step("chat", chat):
            return

    # Advance the watermark only for providers that completed, so a failure is retried
    # next run without re-processing (and re-drafting) the providers that succeeded.
    if succeeded:
        updates: dict = {f"provider_last_run.{p}": started for p in succeeded}
        updates["last_run"] = started
        await store.update_account(account.email, updates)
    if failed:
        result.failed.append(account.email)


async def _mark_reauth(account: Account, result: UserRunResult) -> None:
    log.warning("Refresh token for %s is no longer valid", account.email)
    await get_store().update_account(account.email, {"needs_reauth": True})
    result.reauth_needed.append(account.email)
