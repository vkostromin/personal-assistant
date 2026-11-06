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
from src.quota import Budget, get_budget, record_usage, resolve_llm
from src.state import get_store
from src.telegram_client import escape_markdown, send_message

log = logging.getLogger(__name__)


@dataclass
class UserRunResult:
    email_items: list[str] = field(default_factory=list)
    chat_items: list[str] = field(default_factory=list)
    promises: list[Promise] = field(default_factory=list)
    drafts_created: int = 0
    emails_analysed: int = 0
    emails_skipped: int = 0
    drafts_paused: bool = False
    timed_out: bool = False
    own_key_rejected: bool = False
    reauth_needed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


@dataclass
class RunCtx:
    llm: LLM
    budget: Budget
    deadline: float | None  # time.monotonic() value
    emails_left: int

    def past_deadline(self) -> bool:
        return self.deadline is not None and time.monotonic() > self.deadline

    def drafts_allowed(self) -> bool:
        # the window was reset at the start of this run, so only this run's spend counts
        return self.budget.unlimited or self.llm.tokens_used < self.budget.token_limit


def _is_revoked(exc: BaseException) -> bool:
    """Token can't be used any more and the user must sign in again.

    invalid_grant: revoked / expired (e.g. 7-day limit for apps in Testing status)
    unauthorized_client: token was issued to a different OAuth client (old Desktop client)
    """
    return isinstance(exc, RefreshError) and any(code in str(exc) for code in ("invalid_grant", "unauthorized_client"))


def _chunks(items: list, size: int):
    for i in range(0, len(items), max(1, size)):
        yield items[i : i + size]


async def run_all_users(shard: int = 0, shards: int = 1) -> dict:
    """Process every active user (bounded parallelism). One user's failure never affects others.

    ``shard``/``shards`` split users across several scheduler jobs (user_id % shards == shard),
    so each request stays well inside the 300s Cloud Run timeout as the user base grows.
    """
    s = config.settings
    log.info("Starting scheduled run (shard %d/%d)...", shard, shards)
    deadline = time.monotonic() + s.run_time_budget_seconds
    store = get_store()
    users = [u for u in await store.list_users() if u.is_active and (shards <= 1 or u.id % shards == shard)]
    sem = asyncio.Semaphore(max(1, s.run_concurrency))
    stats = {"users": 0, "skipped": 0, "errors": 0, "deferred": 0}

    async def one(user: User):
        async with sem:
            if time.monotonic() > deadline:
                stats["deferred"] += 1
                log.warning("Run time budget exhausted before user %s was processed", user.id)
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
    # A scheduled run starts a fresh usage window ("paused until the next run")
    await store.start_usage_window(user.id)

    accounts = await store.list_accounts(user_id=user.id)
    if not accounts:
        return False
    llm = await resolve_llm(user)
    if llm is None:
        log.error("No LLM provider configured (set GEMINI_API_KEY or DEEPSEEK_API_KEY); skipping user %s", user.id)
        return False
    budget = await get_budget(user, llm)
    rc = RunCtx(llm=llm, budget=budget, deadline=deadline, emails_left=budget.email_limit)

    result = UserRunResult()
    try:
        for account in accounts:
            if account.needs_reauth:
                result.reauth_needed.append(account.email)
                continue
            await _process_account(user, account, rc, result)

        summary = ""
        if len(result.reauth_needed) < len(accounts):  # something was processed
            try:
                summary = await compose_summary(
                    email_summary=_bullets(result.email_items, "no new emails"),
                    chat_summary=_bullets(result.chat_items, "no new chat activity"),
                    promises=_bullets([p.text for p in result.promises], "none"),
                    date=utcnow().strftime("%b %d, %Y"),
                    llm=llm,
                    signature=s.signature,
                )
            except LLMError as exc:
                log.error("Summary LLM call failed for user %s: %s", user.id, exc)
                result.own_key_rejected |= llm.own_key and exc.is_auth_error
                summary = _plain_summary(result)
    finally:
        await record_usage(user, llm, emails=result.emails_analysed)

    text = "\n\n".join(part for part in [summary, _notes(result, rc)] if part)
    if text:
        await send_message(s.telegram_bot_token, user.chat_id, text)
    log.info("User %s processed: %d emails, %d drafts", user.id, result.emails_analysed, result.drafts_created)
    return True


def _bullets(items: list[str], empty: str) -> str:
    return "\n".join(f"- {escape_markdown(i)}" for i in items[:10]) or empty


def _plain_summary(result: UserRunResult) -> str:
    """Digest without the LLM (used when the summary call fails)."""
    parts = [f"📋 Summary — {utcnow().strftime('%b %d, %Y')}"]
    if result.email_items:
        parts.append("📧 Email\n" + _bullets(result.email_items, ""))
    if result.chat_items:
        parts.append("💬 Chat\n" + _bullets(result.chat_items, ""))
    if result.promises:
        parts.append("⚠️ Promises\n" + _bullets([p.text for p in result.promises], ""))
    if len(parts) == 1:
        parts.append("Nothing new.")
    return "\n\n".join(parts)


def _notes(result: UserRunResult, rc: RunCtx) -> str:
    notes = []
    if result.emails_skipped:
        per = "per run" if rc.budget.unlimited else f"per {config.settings.usage_window_hours} hours"
        notes.append(
            f"📭 {result.emails_skipped} more new email(s) were not analysed "
            f"(limit: {rc.budget.email_limit} emails {per})."
        )
    if result.drafts_paused:
        notes.append("✍️ Draft replies were paused: the AI limit for this period is used up. They resume after the next run.")
    if result.timed_out:
        notes.append("⏱ Not everything could be processed in time this run.")
    if result.own_key_rejected:
        notes.append(
            f"🔑 Your own {escape_markdown(rc.llm.provider)} API key was rejected. "
            "Update it with /setkey or go back to the shared AI with /removekey."
        )
    for email in result.reauth_needed:
        notes.append(f"🔑 Access to {escape_markdown(email)} expired or was revoked — send /connect to reconnect it.")
    for email in result.failed:
        notes.append(f"⚠️ Could not check {escape_markdown(email)} this time; will retry next run.")
    return "\n".join(notes)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _process_account(user: User, account: Account, rc: RunCtx, result: UserRunResult) -> None:
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
    log.info("Processing account %s (user %s)", account.email, user.id)

    def handle_error(provider: str, exc: Exception) -> None:
        nonlocal failed
        if isinstance(exc, LLMError) and exc.is_auth_error and rc.llm.own_key:
            result.own_key_rejected = True
        failed = True
        log.error("%s processing failed for %s: %s", provider, account.email, exc, exc_info=True)

    # --- Gmail ---
    if "gmail" in account.providers:
        try:
            messages, skipped = await asyncio.to_thread(
                fetch_recent_messages,
                account=account,
                client_id=s.google_client_id,
                client_secret=s.google_client_secret,
                last_run=since("gmail"),
                max_results=rc.emails_left,
            )
            rc.emails_left -= len(messages)
            result.emails_skipped += skipped
            batches = list(_chunks(messages, s.email_batch_size))
            for i, batch in enumerate(batches):
                if rc.past_deadline():
                    result.timed_out = True
                    result.emails_skipped += sum(len(b) for b in batches[i:])
                    break
                with_drafts = rc.drafts_allowed()
                result.drafts_paused |= not with_drafts
                analysis = await analyze_emails(
                    messages=batch, account=account.email, llm=rc.llm, signature=s.signature, with_drafts=with_drafts
                )
                result.emails_analysed += len(batch)
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
        except Exception as exc:  # noqa: BLE001
            if _is_revoked(exc):
                await _mark_reauth(account, result)
                return
            handle_error("Gmail", exc)

    # --- Google Chat ---
    if "chat" in account.providers:
        try:
            chat_since = since("chat")
            spaces = await asyncio.to_thread(
                list_spaces, account=account, client_id=s.google_client_id, client_secret=s.google_client_secret
            )
            # only spaces with activity since the last check, most recent first
            active = []
            for space in spaces:
                last_active = _parse_time(space.get("lastActiveTime"))
                if last_active is None or last_active >= chat_since:
                    active.append((last_active or started, space))
            active.sort(key=lambda x: x[0], reverse=True)
            for space in [sp for _, sp in active[: s.max_chat_spaces]]:
                if rc.past_deadline():
                    result.timed_out = True
                    break
                messages = await asyncio.to_thread(
                    fetch_messages,
                    account=account,
                    client_id=s.google_client_id,
                    client_secret=s.google_client_secret,
                    space=space,
                    last_run=chat_since,
                )
                if not messages:
                    continue
                analysis = await analyze_chat(
                    messages=messages, space_name=space.get("displayName", space["name"]), llm=rc.llm
                )
                for p in analysis.promises:
                    await store.save_promise(user.id, {**p.model_dump(), "account": account.email})
                    result.promises.append(p)
                result.chat_items.extend(analysis.missed_questions)
                result.chat_items.extend(analysis.summary_items)
            succeeded.append("chat")
        except Exception as exc:  # noqa: BLE001
            if _is_revoked(exc):
                await _mark_reauth(account, result)
                return
            handle_error("Chat", exc)

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
