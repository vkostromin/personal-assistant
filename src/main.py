import asyncio
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request

from src.config import settings
from src.models import Account, Promise
from src.state import (
    close_promise,
    get_open_promises,
    get_pending_drafts,
    get_settings,
    get_state,
    list_accounts,
    save_draft,
    save_promise,
    save_settings,
    save_state,
    update_draft,
)
from src.gmail_client import create_draft, fetch_recent_messages
from src.chat_client import fetch_messages, list_spaces
from src.llm_client import analyze_chat, analyze_emails, answer_question, compose_summary, rewrite_draft
from src.telegram_client import send_message, escape_markdown

from src.models import GlobalState

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="Personal Assistant")


@app.on_event("startup")
async def startup():
    pass


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def telegram_webhook(request: Request, body: dict):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if settings.telegram_webhook_secret and secret != settings.telegram_webhook_secret:
        log.warning("Telegram webhook received invalid secret token")
        return {"ok": False}

    msg = body.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return {"ok": True}

    # Save chat_id on /start (but don't overwrite existing one)
    if text == "/start":
        s_start = await get_settings()
        if s_start.telegram_chat_id and s_start.telegram_chat_id != chat_id:
            await send_message(
                settings.telegram_bot_token,
                chat_id,
                "This bot is already configured for another user.",
            )
            return {"ok": True}
        s_start.telegram_chat_id = chat_id
        await save_settings(s_start)
        await send_message(
            settings.telegram_bot_token,
            chat_id,
            "Hello! I'm Lola, your AI personal assistant.\n\n"
            "/status — current state\n"
            "/drafts — pending email drafts\n"
            "/rewrite <n> <context> — update a draft\n"
            "/raw <text> — analyze text\n"
            "/chat <question> — ask about your state\n"
            "/help — all commands",
        )
        return {"ok": True}

    # Check if this chat has any accounts (fall back to owner's accounts for backward compat)
    s = await get_settings()
    is_owner = chat_id == s.telegram_chat_id
    my_accounts = await list_accounts(chat_id=chat_id)
    if my_accounts:
        account_emails = [a.email for a in my_accounts]
    elif is_owner:
        my_accounts = await list_accounts()
        account_emails = None
    else:
        await send_message(
            settings.telegram_bot_token, chat_id, "No accounts linked to this chat."
        )
        return {"ok": True}

    def filter_by_account(items: list[dict]) -> list[dict]:
        if account_emails is None:
            return items
        return [i for i in items if i.get("account") in account_emails]

    if text == "/status":
        drafts = filter_by_account(await get_pending_drafts())
        promises = filter_by_account(await get_open_promises())
        draft_text = f"{len(drafts)} pending drafts" if drafts else "no pending drafts"
        promise_text = f"{len(promises)} open promises" if promises else "no open promises"
        state = await get_state()
        last_run = state.last_run.strftime("%b %d %H:%M") if state.last_run else "never"
        msg_text = f"📋 Status\n\n📧 {draft_text}\n⚠️ {promise_text}\n🕐 Last check: {last_run}"
        await send_message(settings.telegram_bot_token, chat_id, msg_text)
        return {"ok": True}

    if text.startswith("/drafts"):
        drafts = filter_by_account(await get_pending_drafts())
        if not drafts:
            await send_message(settings.telegram_bot_token, chat_id, "No pending drafts.")
            return {"ok": True}
        lines = []
        for i, d in enumerate(drafts, 1):
            to_esc = escape_markdown(d.get("to", "?"))
            subj_esc = escape_markdown(d.get("subject", "?"))
            body_preview = escape_markdown(d.get("body", "")[:80].replace("\n", " "))
            lines.append(f"{i}. To: {to_esc} — \"{subj_esc}\"")
            lines.append(f"   {body_preview}...")
        entries = "\n".join(lines)
        await send_message(
            settings.telegram_bot_token,
            chat_id,
            f"📧 Pending drafts:\n\n{entries}\n\nUse /rewrite N <context> to update a draft.",
        )
        return {"ok": True}

    if text.startswith("/rewrite"):
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await send_message(settings.telegram_bot_token, chat_id, "Usage: /rewrite N <new context>")
            return {"ok": True}
        try:
            idx = int(parts[1]) - 1
        except ValueError:
            await send_message(settings.telegram_bot_token, chat_id, "N must be a number")
            return {"ok": True}
        context = parts[2]

        drafts_list = filter_by_account(await get_pending_drafts())
        if idx < 0 or idx >= len(drafts_list):
            await send_message(settings.telegram_bot_token, chat_id, "Draft number out of range")
            return {"ok": True}

        draft = drafts_list[idx]
        result = await rewrite_draft(
            to=draft["to"],
            subject=draft["subject"],
            body=draft["body"],
            context=context,
            api_key=settings.deepseek_api_key,
            api_url=settings.deepseek_api_url,
            signature=settings.signature,
        )
        if not result:
            await send_message(settings.telegram_bot_token, chat_id, "Failed to rewrite draft.")
            return {"ok": True}

        new_subject = result.get("subject", draft["subject"])
        new_body = result.get("body", draft["body"])

        await update_draft(draft["id"], {"subject": new_subject, "body": new_body})

        for acc in my_accounts:
            if acc.email == draft.get("account"):
                await asyncio.to_thread(
                    create_draft,
                    account=acc,
                    client_id=settings.google_client_id,
                    client_secret=settings.google_client_secret,
                    to=draft["to"],
                    subject=new_subject,
                    body=new_body,
                    thread_id=draft.get("thread_id"),
                )

        await send_message(settings.telegram_bot_token, chat_id, f"✅ Draft updated with new context.")
        return {"ok": True}

    if text.startswith("/raw"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await send_message(settings.telegram_bot_token, chat_id, "Usage: /raw <text>")
            return {"ok": True}
        raw_text = parts[1]
        response = await answer_question(
            question=f"Analyze this text. What action items, promises, or questions are in it?\n\n{raw_text}",
            drafts_summary="",
            promises_summary="",
            last_activity="",
            api_key=settings.deepseek_api_key,
            api_url=settings.deepseek_api_url,
            signature=settings.signature,
        )
        await send_message(settings.telegram_bot_token, chat_id, response)
        return {"ok": True}

    if text.startswith("/chat"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await send_message(settings.telegram_bot_token, chat_id, "Usage: /chat <question>")
            return {"ok": True}
        question = parts[1]
        drafts = filter_by_account(await get_pending_drafts())
        promises = filter_by_account(await get_open_promises())
        state = await get_state()
        response = await answer_question(
            question=question,
            drafts_summary=str(len(drafts)) if drafts else "none",
            promises_summary="\n".join(f"- {escape_markdown(p.get('text', ''))}" for p in promises[:5]) if promises else "none",
            last_activity=state.last_run.strftime("%b %d %H:%M") if state.last_run else "never",
            api_key=settings.deepseek_api_key,
            api_url=settings.deepseek_api_url,
            signature=settings.signature,
        )
        await send_message(settings.telegram_bot_token, chat_id, response)
        return {"ok": True}

    if text == "/help":
        await send_message(
            settings.telegram_bot_token,
            chat_id,
            "/status — current state\n"
            "/drafts — pending drafts\n"
            "/rewrite N <context> — update draft\n"
            "/raw <text> — analyze text\n"
            "/chat <question> — ask about state",
        )
        return {"ok": True}

    await send_message(settings.telegram_bot_token, chat_id, f"Unknown command: {text}\nUse /help")
    return {"ok": True}


@app.post("/run")
async def scheduled_run():
    await _process_accounts()
    return {"status": "ok"}


async def _process_accounts():
    log.info("Starting scheduled run...")
    
    state = await get_state()
    settings_model = await get_settings()

    all_email_items: list[str] = []
    all_chat_items: list[str] = []
    all_promises: list[Promise] = []
    total_drafts = 0

    accounts = await list_accounts()

    for account in accounts:
        log.info("Processing account: %s", account.email)
        if not account.enabled:
            continue

        try:
            # --- Gmail ---
            if "gmail" in account.providers:
                log.info("Fetching emails for %s...", account.email)
                messages = await asyncio.to_thread(
                    fetch_recent_messages,
                    account=account,
                    client_id=settings.google_client_id,
                    client_secret=settings.google_client_secret,
                    last_run=state.last_run,
                )
                if messages:
                    log.info("Analyzing %d emails...", len(messages))
                    result = await analyze_emails(
                        messages=messages,
                        account=account.email,
                        api_key=settings.deepseek_api_key,
                        api_url=settings.deepseek_api_url,
                        signature=settings.signature,
                    )
                    for draft in result.drafts:
                        draft_data = draft.model_dump()
                        draft_id = await save_draft(draft_data)
                        gmail_draft_id = await asyncio.to_thread(
                            create_draft,
                            account=account,
                            client_id=settings.google_client_id,
                            client_secret=settings.google_client_secret,
                            to=draft.to,
                            subject=draft.subject,
                            body=draft.body,
                            thread_id=draft.thread_id,
                        )
                        if gmail_draft_id:
                            await update_draft(draft_id, {"gmail_draft_id": gmail_draft_id})
                            log.info("Created draft: %s -> %s", draft.subject, draft.to)
                            total_drafts += 1
                        else:
                            log.error("Failed to create Gmail draft: %s -> %s", draft.subject, draft.to)

                    for p in result.promises:
                        p_data = p.model_dump()
                        p_data["account"] = account.email
                        await save_promise(p_data)
                        all_promises.append(p)

                    all_email_items.extend(result.summary_items)

            # --- Google Chat ---
            if "chat" in account.providers:
                log.info("Fetching chat spaces for %s...", account.email)
                spaces = await asyncio.to_thread(
                    list_spaces,
                    account=account,
                    client_id=settings.google_client_id,
                    client_secret=settings.google_client_secret,
                )
                for space in spaces:
                    log.info("Fetching messages from %s...", space.get("displayName", space["name"]))
                    messages = await asyncio.to_thread(
                        fetch_messages,
                        account=account,
                        client_id=settings.google_client_id,
                        client_secret=settings.google_client_secret,
                        space=space,
                        last_run=state.last_run,
                    )
                    if messages:
                        log.info("Analyzing %d chat messages...", len(messages))
                        result = await analyze_chat(
                            messages=messages,
                            space_name=space.get("displayName", space["name"]),
                            api_key=settings.deepseek_api_key,
                            api_url=settings.deepseek_api_url,
                        )
                        for p in result.promises:
                            p_data = p.model_dump()
                            p_data["account"] = account.email
                            await save_promise(p_data)
                            all_promises.append(p)

                        if result.missed_questions:
                            all_chat_items.extend(result.missed_questions)
                        all_chat_items.extend(result.summary_items)

        except Exception as exc:
            log.error("Error processing account %s: %s", account.email, exc, exc_info=True)

    # Compose and send summary
    promises_text = "\n".join(f"- {escape_markdown(p.text)}" for p in all_promises[:10]) if all_promises else "none"
    email_text = "\n".join(f"- {escape_markdown(item)}" for item in all_email_items[:10]) if all_email_items else "no new emails"
    chat_text = "\n".join(f"- {escape_markdown(item)}" for item in all_chat_items[:10]) if all_chat_items else "no new chat activity"

    date_str = datetime.now(timezone.utc).strftime("%b %d, %Y")
    summary = await compose_summary(
        email_summary=email_text,
        chat_summary=chat_text,
        promises=promises_text,
        date=date_str,
        api_key=settings.deepseek_api_key,
        api_url=settings.deepseek_api_url,
        signature=settings.signature,
    )

    if settings_model.telegram_chat_id:
        await send_message(settings.telegram_bot_token, settings_model.telegram_chat_id, summary)
        log.info("Summary sent to Telegram")
    else:
        log.info("No Telegram chat_id configured. Run /start from your bot first.")

    await save_state(GlobalState(last_run=datetime.now(timezone.utc)))
    log.info("Scheduled run complete. Created %d drafts.", total_drafts)
