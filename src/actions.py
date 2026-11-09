"""State changes on items and drafts — shared by buttons, commands, reminders and the run."""

import asyncio
import logging
from datetime import datetime, timedelta

from google.auth.exceptions import RefreshError

from src import config
from src.chat_client import send_chat_message
from src.gmail_client import create_draft, delete_gmail_draft, send_draft, update_gmail_draft
from src.items import due_choice, fingerprint, next_reminder, snooze_until
from src.llm_client import LLM, ExtractedItem, ItemUpdate, rewrite_draft, write_follow_up
from src.models import Account, Item, User, utcnow
from src.schedule import fmt_local, user_tz
from src.state import get_store

log = logging.getLogger(__name__)


class ActionError(Exception):
    """User-facing failure (message is shown in Telegram)."""


# -------------------------------------------------------------------- items
async def add_item(user: User, extracted: ExtractedItem, *, source: str, account: str = "",
                   message: dict | None = None, space: dict | None = None, link: str = "",
                   chat_message: dict | None = None) -> Item | None:
    """Create an item (deduplicated against open items). Returns None if it already exists."""
    store = get_store()
    now = utcnow()
    tz = user_tz(user)
    fp = fingerprint(extracted.kind, extracted.title, extracted.counterparty)
    if await store.find_open_item(user.id, fp):
        return None
    due = extracted.due_at
    if extracted.kind == "waiting" and due is None:
        due = now + timedelta(hours=config.settings.waiting_default_hours)
    item = Item(
        user_id=user.id,
        kind=extracted.kind,
        title=extracted.title,
        counterparty=extracted.counterparty,
        due_at=due,
        priority=extracted.priority,
        source=source,
        account=account,
        thread_id=(message or {}).get("thread_id") or None,
        in_reply_to=(message or {}).get("message_id_header") or None,
        subject=(message or {}).get("subject", "")[:200],
        space=(space or {}).get("name"),
        chat_thread=(chat_message or {}).get("thread") or None,
        chat_message=(chat_message or {}).get("name") or None,
        detail=((chat_message or {}).get("text") or "")[:500],
        link=link,
        fingerprint=fp,
        created_at=now,
    )
    item.next_remind_at = next_reminder(item, now, tz)
    item.id = await store.create_item(item)
    return item


async def apply_update(user: User, update: ItemUpdate) -> None:
    store = get_store()
    item = await store.get_item(update.id)
    if item is None or item.user_id != user.id or item.status != "open":
        return
    if update.status in ("done", "cancelled"):
        await close_item(user, item, update.status, resolution=update.note or "detected automatically")
    elif update.due_changed and update.due_at:
        await set_due(user, item, update.due_at)


async def close_item(user: User, item: Item, status: str, resolution: str = "") -> None:
    await get_store().update_item(item.id, {
        "status": status, "closed_at": utcnow(), "next_remind_at": None, "resolution": resolution[:200],
    })


async def snooze_item(user: User, item: Item, choice: str) -> datetime:
    now = utcnow()
    until = snooze_until(choice, now, user_tz(user))
    await get_store().update_item(item.id, {"snoozed_until": until, "next_remind_at": until})
    return until


async def set_due(user: User, item: Item, due: datetime | None) -> Item:
    """New deadline: reminder policy restarts (heads-up → due → overdue)."""
    now = utcnow()
    updated = item.model_copy(update={"due_at": due, "remind_stage": 0, "snoozed_until": None})
    await get_store().update_item(item.id, {
        "due_at": due, "remind_stage": 0, "snoozed_until": None,
        "next_remind_at": next_reminder(updated, now, user_tz(user)),
    })
    return updated


async def set_due_choice(user: User, item: Item, choice: str) -> Item:
    return await set_due(user, item, due_choice(choice, utcnow(), user_tz(user)))


async def _owned_account(user: User, email: str) -> Account:
    account = await get_store().get_account(email)
    if account is None or account.user_id != user.id:
        raise ActionError("The Google account is no longer linked. See /accounts.")
    if account.needs_reauth:
        raise ActionError(f"Access to {email} expired — send /connect to reconnect it.")
    return account


async def chat_reply_account(user: User, item: Item) -> Account:
    """The account to reply from; raises ActionError with what to do if replying isn't possible."""
    if not item.space or not item.account:
        raise ActionError("This item isn't from Google Chat.")
    account = await _owned_account(user, item.account)
    if not account.chat_send:
        raise ActionError(
            f"Replying from Telegram needs one more permission. Send /connect and sign in with {account.email} again."
        )
    return account


async def send_chat_reply(user: User, item: Item, text: str) -> None:
    s = config.settings
    account = await chat_reply_account(user, item)
    try:
        sent = await asyncio.to_thread(
            send_chat_message, account=account, client_id=s.google_client_id, client_secret=s.google_client_secret,
            space=item.space, text=text, thread=item.chat_thread,
        )
    except RefreshError as exc:
        raise ActionError(f"Access to {account.email} expired — send /connect to reconnect it.") from exc
    if not sent:
        raise ActionError("Google Chat refused the message.")
    await close_item(user, item, "done", "replied from Telegram")


async def follow_up(user: User, item: Item, llm: LLM) -> dict:
    """Draft a polite nudge in the original thread for a "waiting" item. Returns the new draft record."""
    s = config.settings
    if not (item.thread_id and item.account):
        raise ActionError("This item isn't linked to an email thread.")
    account = await _owned_account(user, item.account)
    due = fmt_local(item.due_at, user, "%a %d %b") if item.due_at else "recently"
    text = await write_follow_up(item, due, llm, s.signature)
    if not text:
        raise ActionError("Couldn't write a follow-up. Try again later.")
    to = item.counterparty
    subject = text.get("subject") or (f"Re: {item.subject}" if item.subject else "Follow-up")
    gmail_id = await asyncio.to_thread(
        create_draft, account=account, client_id=s.google_client_id, client_secret=s.google_client_secret,
        to=to, subject=subject, body=text["body"], thread_id=item.thread_id, in_reply_to=item.in_reply_to,
    )
    if not gmail_id:
        raise ActionError("Gmail rejected the follow-up draft.")
    store = get_store()
    record = {
        "account": account.email, "to": to, "subject": subject, "body": text["body"], "thread_id": item.thread_id,
        "in_reply_to": item.in_reply_to, "priority": item.priority, "status": "pending",
        "gmail_draft_id": gmail_id, "item_id": item.id,
    }
    draft_id = await store.save_draft(user.id, record)
    # give them two more days after the nudge
    await set_due(user, item, utcnow() + timedelta(hours=s.waiting_default_hours))
    return {**record, "id": draft_id}


# ------------------------------------------------------------------- drafts
async def send_pending_draft(user: User, draft: dict) -> None:
    s = config.settings
    if not draft.get("gmail_draft_id"):
        raise ActionError("This draft isn't in Gmail — open Gmail to send it.")
    account = await _owned_account(user, draft.get("account", ""))
    sent = await asyncio.to_thread(
        send_draft, account=account, client_id=s.google_client_id, client_secret=s.google_client_secret,
        draft_id=draft["gmail_draft_id"],
    )
    if not sent:
        raise ActionError("Gmail refused to send it (was it already sent or deleted?). Check /drafts.")
    store = get_store()
    await store.update_draft(draft["id"], {
        "status": "sent", "status_changed_at": utcnow(), "status_source": "lola", "next_remind_at": None,
    })
    # answering closes "needs my reply" items in the same conversation
    if draft.get("thread_id"):
        for it in await store.list_open_items(user.id):
            if it.kind == "reply" and it.thread_id == draft["thread_id"]:
                await close_item(user, it, "done", "reply sent from Lola")


async def discard_draft(user: User, draft: dict) -> bool:
    """Delete in Gmail and in Lola. Returns False if it couldn't be removed from Gmail."""
    s = config.settings
    removed = True
    if draft.get("gmail_draft_id"):
        try:
            account = await _owned_account(user, draft.get("account", ""))
            removed = await asyncio.to_thread(
                delete_gmail_draft, account=account, client_id=s.google_client_id,
                client_secret=s.google_client_secret, draft_id=draft["gmail_draft_id"],
            )
        except ActionError:
            removed = False
    await get_store().delete_draft(draft["id"])
    return removed


async def mark_draft_sent(user: User, draft: dict) -> None:
    await get_store().update_draft(draft["id"], {
        "status": "sent", "status_changed_at": utcnow(), "status_source": "user", "next_remind_at": None,
    })


async def snooze_draft(user: User, draft: dict, choice: str) -> datetime:
    until = snooze_until(choice, utcnow(), user_tz(user))
    await get_store().update_draft(draft["id"], {"snoozed_until": until, "next_remind_at": until})
    return until


async def rewrite_pending_draft(user: User, draft: dict, instructions: str, llm: LLM) -> dict:
    """LLM rewrite + update the same Gmail draft in place. Returns the updated record."""
    s = config.settings
    account = await _owned_account(user, draft.get("account", ""))
    result = await rewrite_draft(
        to=draft["to"], subject=draft["subject"], body=draft["body"], context=instructions, llm=llm, signature=s.signature
    )
    if not result:
        raise ActionError("Failed to rewrite the draft.")
    new_subject = result.get("subject", draft["subject"])
    new_body = result.get("body", draft["body"])
    gmail_id, _ = await asyncio.to_thread(
        update_gmail_draft, account=account, client_id=s.google_client_id, client_secret=s.google_client_secret,
        draft_id=draft.get("gmail_draft_id"), to=draft["to"], subject=new_subject, body=new_body,
        thread_id=draft.get("thread_id"), in_reply_to=draft.get("in_reply_to"),
    )
    if gmail_id is None:
        raise ActionError("Gmail rejected the draft update. Please try again later.")
    updates = {"subject": new_subject, "body": new_body, "gmail_draft_id": gmail_id}
    await get_store().update_draft(draft["id"], updates)
    return {**draft, **updates}
