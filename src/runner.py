"""Scheduled processing: every active user gets their own digest for their own accounts."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from google.auth.exceptions import RefreshError

from src import config
from src.chat_client import fetch_messages, list_spaces
from src.gmail_client import create_draft, fetch_recent_messages
from src.llm_client import LLM, LLMError, analyze_chat, analyze_emails, compose_summary
from src.models import Account, Promise, User, utcnow
from src.quota import LLMUnavailable, QuotaExceeded, get_llm, start_window
from src.state import get_store
from src.telegram_client import escape_markdown, send_message

log = logging.getLogger(__name__)


@dataclass
class UserRunResult:
    email_items: list[str] = field(default_factory=list)
    chat_items: list[str] = field(default_factory=list)
    promises: list[Promise] = field(default_factory=list)
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


async def run_all_users() -> dict:
    """Process every active user (bounded parallelism). One user's failure never affects others."""
    log.info("Starting scheduled run...")
    store = get_store()
    users = [u for u in await store.list_users() if u.is_active]
    sem = asyncio.Semaphore(max(1, config.settings.run_concurrency))
    stats = {"users": 0, "skipped": 0, "errors": 0, "deferred": 0}
    deadline = time.monotonic() + config.settings.run_soft_deadline_seconds

    async def one(user: User):
        async with sem:
            if time.monotonic() > deadline:
                # watermarks untouched: everything is picked up at the next run
                stats["deferred"] += 1
                log.warning("Run deadline reached — user %s deferred to next run", user.id)
                return
            try:
                processed = await run_for_user(user, deadline)
                stats["users" if processed else "skipped"] += 1
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                log.error("Run failed for user %s: %s", user.id, exc, exc_info=True)

    await asyncio.gather(*(one(u) for u in users))
    log.info("Scheduled run complete: %s", stats)
    return stats


async def run_for_user(user: User, deadline: float | None = None) -> bool:
    """Process all accounts of ``user`` and send them a digest. Returns False if nothing to do."""
    s = config.settings
    store = get_store()
    accounts = await store.list_accounts(user_id=user.id)
    if not accounts:
        return False

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
        await _process_account(user, account, result, llm)

    promises_text = "\n".join(f"- {escape_markdown(p.text)}" for p in result.promises[:10]) or "none"
    email_text = "\n".join(f"- {escape_markdown(i)}" for i in result.email_items[:10]) or "no new emails"
    chat_text = "\n".join(f"- {escape_markdown(i)}" for i in result.chat_items[:10]) or "no new chat activity"

    date = utcnow().strftime("%b %d, %Y")
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
            )
        except LLMError as exc:
            log.error("Summary LLM call failed for user %s: %s", user.id, exc)
            result.llm_error = result.llm_error or str(exc)
            summary = (
                f"📋 Summary — {date}\n\n📧 Email:\n{email_text}\n\n💬 Chat:\n{chat_text}\n\n⚠️ Promises:\n{promises_text}"
            )

    notes = []
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
    log.info("User %s processed: %d drafts created", user.id, result.drafts_created)
    return True


async def _process_account(user: User, account: Account, result: UserRunResult, llm: LLM) -> None:
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

    # --- Gmail ---
    if "gmail" in account.providers:
        try:
            messages = await asyncio.to_thread(
                fetch_recent_messages,
                account=account,
                client_id=s.google_client_id,
                client_secret=s.google_client_secret,
                last_run=since("gmail"),
            )
            if messages:
                analysis = await analyze_emails(messages=messages, account=account.email, llm=llm, signature=s.signature)
                for draft in analysis.drafts:
                    draft_id = await store.save_draft(user.id, draft.model_dump())
                    gmail_draft_id = await asyncio.to_thread(
                        create_draft,
                        account=account,
                        client_id=s.google_client_id,
                        client_secret=s.google_client_secret,
                        to=draft.to,
                        subject=draft.subject,
                        body=draft.body,
                        thread_id=draft.thread_id,
                    )
                    if gmail_draft_id:
                        await store.update_draft(draft_id, {"gmail_draft_id": gmail_draft_id})
                        result.drafts_created += 1
                    else:
                        log.error("Failed to create Gmail draft for %s", account.email)
                for p in analysis.promises:
                    await store.save_promise(user.id, {**p.model_dump(), "account": account.email})
                    result.promises.append(p)
                result.email_items.extend(analysis.summary_items)
            succeeded.append("gmail")
        except QuotaExceeded:
            # watermark not advanced: these emails are analyzed at the next run with a fresh limit
            quota_hit = result.quota_hit = True
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, LLMError):
                result.llm_error = result.llm_error or str(exc)
            if _is_revoked(exc):
                await _mark_reauth(account, result)
                return
            failed = True
            log.error("Gmail processing failed for %s: %s", account.email, exc, exc_info=True)

    # --- Google Chat ---
    if "chat" in account.providers and not quota_hit:
        try:
            spaces = await asyncio.to_thread(
                list_spaces, account=account, client_id=s.google_client_id, client_secret=s.google_client_secret
            )
            for space in spaces:
                messages = await asyncio.to_thread(
                    fetch_messages,
                    account=account,
                    client_id=s.google_client_id,
                    client_secret=s.google_client_secret,
                    space=space,
                    last_run=since("chat"),
                )
                if not messages:
                    continue
                try:
                    analysis = await analyze_chat(
                        messages=messages, space_name=space.get("displayName", space["name"]), llm=llm
                    )
                except QuotaExceeded:
                    quota_hit = result.quota_hit = True
                    break
                for p in analysis.promises:
                    await store.save_promise(user.id, {**p.model_dump(), "account": account.email})
                    result.promises.append(p)
                result.chat_items.extend(analysis.missed_questions)
                result.chat_items.extend(analysis.summary_items)
            if not quota_hit:
                succeeded.append("chat")
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, LLMError):
                result.llm_error = result.llm_error or str(exc)
            if _is_revoked(exc):
                await _mark_reauth(account, result)
                return
            failed = True
            log.error("Chat processing failed for %s: %s", account.email, exc, exc_info=True)

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
