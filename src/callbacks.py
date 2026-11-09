"""Inline-button presses (Telegram callback queries) and free-text follow-ups."""

import logging
from datetime import timedelta

from src import config
from src.actions import (
    ActionError,
    add_item,
    chat_reply_account,
    send_chat_reply,
    close_item,
    discard_draft,
    follow_up,
    mark_draft_sent,
    rewrite_pending_draft,
    send_pending_draft,
    set_due,
    set_due_choice,
    snooze_draft,
    snooze_item,
)
from src.cards import (
    button,
    cb,
    chat_preview,
    draft_card,
    draft_keyboard,
    draft_text,
    due_keyboard,
    item_card,
    item_keyboard,
    item_text,
    keyboard,
    review_card,
    review_due_keyboard,
    review_keyboard,
    send_confirm_keyboard,
    snooze_keyboard,
)
from src.llm_client import (
    LLM,
    AnalysisContext,
    ExtractedItem,
    LLMError,
    answer_question,
    parse_due_text,
    parse_task,
    suggest_chat_reply,
)
from src.items import next_reminder, review_queue
from src.models import User, utcnow
from src.quota import LLMUnavailable, QuotaExceeded, get_llm, limit_reached_text, usage_status
from src.schedule import fmt_local, user_tz, user_tz_name
from src.state import _aware, get_store
from src.telegram_client import answer_callback, edit_message, escape_markdown, send_message

log = logging.getLogger(__name__)

PENDING_TTL = timedelta(minutes=15)


async def _send(chat_id: int, text: str, markup: dict | None = None) -> int | None:
    return await send_message(config.settings.telegram_bot_token, chat_id, text, reply_markup=markup)


async def _llm(user: User, chat_id: int) -> LLM | None:
    try:
        return await get_llm(user)
    except LLMUnavailable:
        await _send(chat_id, "⚠️ No AI provider is configured. Add your own key with /setkey.")
        return None


async def _llm_failed(user: User, chat_id: int, exc: Exception) -> None:
    if isinstance(exc, QuotaExceeded):
        await _send(chat_id, limit_reached_text(await usage_status(user), user))
    else:
        await _send(chat_id, "⚠️ The AI service returned an error. Please try again later.")


async def analysis_context(user: User) -> AnalysisContext:
    return AnalysisContext(now=utcnow(), tz=user_tz(user), tz_name=user_tz_name(user),
                           open_items=await get_store().list_open_items(user.id))


# -------------------------------------------------------------- callbacks
class _Msg:
    """The message the button belongs to."""

    def __init__(self, chat_id: int, message_id: int | None):
        self.chat_id, self.message_id = chat_id, message_id

    async def edit(self, text: str | None = None, markup: dict | None = None) -> None:
        if self.message_id:
            await edit_message(config.settings.telegram_bot_token, self.chat_id, self.message_id, text, markup)


async def handle_callback(cq: dict) -> None:
    token = config.settings.telegram_bot_token
    message = cq.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id") or cq.get("from", {}).get("id")
    msg = _Msg(chat_id, message.get("message_id"))
    toast = ""
    try:
        parts = (cq.get("data") or "").split(":")
        user = await get_store().get_user(cq.get("from", {}).get("id"))
        if len(parts) < 3 or user is None or not user.is_active:
            toast = "Not available."
            return
        kind, action, ref = parts[0], parts[1], parts[2]
        arg = parts[3] if len(parts) > 3 else ""
        if kind == "i":
            toast = await _item_action(user, msg, action, ref, arg)
        elif kind == "d":
            toast = await _draft_action(user, msg, action, ref, arg)
        elif kind == "t":
            toast = await _text_action(user, msg, action)
        elif kind == "r":
            toast = await _review_action(user, msg, action, ref, arg)
    except ActionError as exc:
        toast = str(exc)[:190]
        await _send(chat_id, f"❌ {escape_markdown(str(exc))}")
    except (QuotaExceeded, LLMError) as exc:
        toast = "AI unavailable"
        await _llm_failed(user, chat_id, exc)
    finally:
        await answer_callback(token, cq.get("id", ""), toast)


async def _item_action(user: User, msg: _Msg, action: str, ref: str, arg: str) -> str:
    store = get_store()
    now = utcnow()
    item = await store.get_item(ref)
    if item is None or item.user_id != user.id:
        await msg.edit(markup=None)
        return "Item not found."
    if action == "open":
        text, markup = item_card(item, user, now)
        await _send(msg.chat_id, text, markup if item.status == "open" else None)
        return ""
    if item.status != "open":
        await msg.edit(item_text(item, user, now) + f"\n\n{'✅ Done' if item.status == 'done' else '✖️ Cancelled'}")
        return "Already closed."

    if action in ("done", "cancel"):
        status = "done" if action == "done" else "cancelled"
        await close_item(user, item, status, resolution="by you")
        await msg.edit(item_text(item, user, now) + ("\n\n✅ *Done*" if status == "done" else "\n\n✖️ *Cancelled*"))
        return "Done ✅" if status == "done" else "Cancelled"
    if action == "snz" and not arg:
        await msg.edit(markup=snooze_keyboard("i", item.id))
        return ""
    if action == "snz":
        until = await snooze_item(user, item, arg)
        await msg.edit(item_text(item, user, now) + f"\n\n⏰ Snoozed until {fmt_local(until, user, '%a %H:%M')}")
        return "Snoozed"
    if action == "due" and not arg:
        await msg.edit(markup=due_keyboard(item.id))
        return ""
    if action == "due" and arg == "other":
        await store.set_pending_action(user.id, {"type": "due", "ref": item.id, "expires_at": now + PENDING_TTL})
        await _send(msg.chat_id, "📅 When is it due? Reply in your own words, e.g. “Friday 15:00” or “in 3 days”.")
        await msg.edit(markup=item_keyboard(item))
        return ""
    if action == "due":
        updated = await set_due_choice(user, item, arg)
        await msg.edit(item_text(updated, user, now), item_keyboard(updated))
        return "Deadline updated"
    if action == "fu":
        llm = await _llm(user, msg.chat_id)
        if llm is None:
            return ""
        draft = await follow_up(user, item, llm)
        text, markup = draft_card(draft, user, now, header="✍️ Follow-up draft — check it and send:")
        await _send(msg.chat_id, text, markup)
        return "Follow-up drafted"
    if action == "crep":
        await chat_reply_account(user, item)  # explains how to fix it if replying isn't possible
        await store.set_pending_action(user.id, {"type": "chat_reply", "ref": item.id, "expires_at": now + PENDING_TTL})
        await _send(
            msg.chat_id,
            "↩️ Type your reply — it will be posted in Google Chat as you (you'll see it before it's sent).",
            keyboard([button("✨ Let Lola draft it", cb("i", "csug", item.id))]),
        )
        return ""
    if action == "csug":
        await chat_reply_account(user, item)
        llm = await _llm(user, msg.chat_id)
        if llm is None:
            return ""
        await show_chat_preview(user, msg.chat_id, item, await suggest_chat_reply(item, llm))
        return "Draft ready"
    if action == "csend":
        pending = user.pending_action or {}
        expires = _aware(pending.get("expires_at"))
        if pending.get("type") != "chat_confirm" or pending.get("ref") != item.id or not expires or expires < now:
            await msg.edit(markup=None)
            return "Expired — tap ↩️ Reply again."
        await send_chat_reply(user, item, pending["text"])
        await store.set_pending_action(user.id, None)
        await msg.edit(chat_preview(item, pending["text"])[0] + "\n\n📤 *Sent*")
        return "Sent to Chat"
    if action == "cx":
        await store.set_pending_action(user.id, None)
        await msg.edit(markup=None)
        return "Not sent"
    if action == "back":
        await msg.edit(markup=item_keyboard(item))
        return ""
    return ""


async def _review_action(user: User, msg: _Msg, action: str, ref: str, arg: str) -> str:
    """One /review step: act on the item, then show the next old item in the same message."""
    store = get_store()
    now = utcnow()
    item = await store.get_item(ref)
    if item is None or item.user_id != user.id:
        await msg.edit(markup=None)
        return "Item not found."
    if action == "stop":
        await msg.edit("🧹 Review paused. Send /review to continue.")
        return ""
    if action == "back":
        await msg.edit(markup=review_keyboard(item))
        return ""
    toast = ""
    if item.status == "open":
        if action in ("done", "cancel"):
            await close_item(user, item, "done" if action == "done" else "cancelled", resolution="in review")
            toast = "Done ✅" if action == "done" else "Cancelled"
        elif action == "again" or (action == "due" and not arg):
            if action == "again" and item.due_at:
                # restart the reminder policy: heads-up/due/3 daily reminders again
                fresh = item.model_copy(update={"remind_stage": 0, "last_reminded_at": None, "snoozed_until": None})
                await store.update_item(item.id, {
                    "remind_stage": 0, "last_reminded_at": None, "snoozed_until": None, "reviewed_at": now,
                    "next_remind_at": next_reminder(fresh, now, user_tz(user)),
                })
                toast = "Reminders restarted"
            else:
                await msg.edit(markup=review_due_keyboard(item.id))
                return "Pick a deadline" if action == "again" else ""
        elif action == "due":
            await set_due_choice(user, item, arg)
            await store.update_item(item.id, {"reviewed_at": now})
            toast = "Deadline updated"
        elif action == "skip":
            toast = "Skipped"
    await _show_next_review(user, msg, after=item)
    return toast


async def _show_next_review(user: User, msg: _Msg, after) -> None:
    now = utcnow()
    key = (_aware(after.created_at), after.id)
    queue = [i for i in review_queue(await get_store().list_open_items(user.id), now) if (_aware(i.created_at), i.id) > key]
    if not queue:
        await msg.edit("🎉 Review finished — everything old has a decision.")
        return
    text, markup = review_card(queue[0], user, now, left=len(queue))
    await msg.edit(text, markup)


async def show_chat_preview(user: User, chat_id: int, item, text: str) -> None:
    """Ask for confirmation before posting to Google Chat."""
    await get_store().set_pending_action(
        user.id, {"type": "chat_confirm", "ref": item.id, "text": text[:4000], "expires_at": utcnow() + PENDING_TTL}
    )
    preview, markup = chat_preview(item, text)
    await _send(chat_id, preview, markup)


async def _draft_action(user: User, msg: _Msg, action: str, ref: str, arg: str) -> str:
    store = get_store()
    now = utcnow()
    draft = await store.get_draft(ref)
    if draft is None or draft.get("user_id") != user.id:
        await msg.edit(markup=None)
        return "Draft not found."
    if draft.get("status") != "pending":
        await msg.edit(markup=None)
        return f"Already {draft.get('status')}."
    if action == "open":
        text, markup = draft_card(draft, user, now)
        await _send(msg.chat_id, text, markup)
        return ""
    if action == "send":
        await msg.edit(markup=send_confirm_keyboard(draft))
        return "Confirm sending"
    if action == "sendok":
        await send_pending_draft(user, draft)
        await msg.edit(draft_text(draft, user, now) + f"\n\n📤 *Sent* {fmt_local(now, user, '%H:%M')}")
        return "Sent 📤"
    if action == "rw":
        await store.set_pending_action(user.id, {"type": "rewrite", "ref": draft["id"], "expires_at": now + PENDING_TTL})
        await _send(msg.chat_id, "✏️ What should I change? Reply with instructions, e.g. “more formal, propose Tuesday”.")
        return ""
    if action == "snz" and not arg:
        await msg.edit(markup=snooze_keyboard("d", draft["id"]))
        return ""
    if action == "snz":
        until = await snooze_draft(user, draft, arg)
        await msg.edit(draft_text(draft, user, now) + f"\n\n⏰ Snoozed until {fmt_local(until, user, '%a %H:%M')}")
        return "Snoozed"
    if action == "del":
        removed = await discard_draft(user, draft)
        note = "" if removed else " (couldn't remove it from Gmail — delete it there)"
        await msg.edit(draft_text(draft, user, now) + f"\n\n🗑 *Discarded*{note}")
        return "Discarded"
    if action == "sent":
        await mark_draft_sent(user, draft)
        await msg.edit(draft_text(draft, user, now) + "\n\n✔️ *Marked as sent*")
        return "Marked as sent"
    if action == "back":
        await msg.edit(markup=draft_keyboard(draft))
        return ""
    return ""


async def _text_action(user: User, msg: _Msg, action: str) -> str:
    pending = user.pending_action or {}
    expires = _aware(pending.get("expires_at"))
    if pending.get("type") != "text" or not expires or expires < utcnow():
        await msg.edit(markup=None)
        return "Expired — send the text again."
    text = pending.get("text", "")
    await get_store().set_pending_action(user.id, None)
    await msg.edit(markup=None)
    if action == "add":
        await add_task_from_text(user, msg.chat_id, text)
        return "Added"
    if action == "ask":
        await ask_lola(user, msg.chat_id, text)
    return ""


# -------------------------------------------------------------- free text
async def add_task_from_text(user: User, chat_id: int, text: str) -> None:
    """LLM turns free text into a task (kind, deadline, person); falls back to a plain to-do."""
    extracted = None
    llm = await _llm(user, chat_id)
    if llm is not None:
        try:
            extracted = await parse_task(text, llm, await analysis_context(user))
        except (QuotaExceeded, LLMError) as exc:
            log.info("parse_task unavailable for %s: %s", user.id, exc)
    if extracted is None:
        extracted = ExtractedItem(kind="todo", title=text.strip().splitlines()[0][:200] or "Task")
    item = await add_item(user, extracted, source="manual")
    if item is None:
        await _send(chat_id, "You already have this item open. See /tasks.")
        return
    card, markup = item_card(item, user, utcnow(), header="➕ Added:")
    await _send(chat_id, card, markup)


async def ask_lola(user: User, chat_id: int, question: str) -> None:
    store = get_store()
    llm = await _llm(user, chat_id)
    if llm is None:
        return
    drafts = await store.get_pending_drafts(user.id)
    items = await store.list_open_items(user.id)
    try:
        answer = await answer_question(
            question=question,
            drafts_summary=str(len(drafts)) if drafts else "none",
            promises_summary="\n".join(f"- [{i.kind}] {i.title}" + (f" (due {fmt_local(i.due_at, user)})" if i.due_at else "")
                                       for i in items[:15]) or "none",
            last_activity="",
            llm=llm,
            signature=config.settings.signature,
        )
    except (QuotaExceeded, LLMError) as exc:
        await _llm_failed(user, chat_id, exc)
        return
    await _send(chat_id, answer)


async def handle_free_text(user: User, chat_id: int, text: str) -> None:
    """Non-command text: answer a pending question (rewrite / deadline), or offer options."""
    store = get_store()
    now = utcnow()
    pending = user.pending_action or {}
    expires = _aware(pending.get("expires_at"))
    if pending.get("type") == "chat_reply" and expires and expires > now:
        item = await store.get_item(pending["ref"])
        if item is None or item.user_id != user.id or item.status != "open":
            await store.set_pending_action(user.id, None)
            await _send(chat_id, "That item is no longer open. See /tasks.")
            return
        await show_chat_preview(user, chat_id, item, text)
        return
    if pending.get("type") in ("rewrite", "due") and expires and expires > now:
        await store.set_pending_action(user.id, None)
        llm = await _llm(user, chat_id)
        if llm is None:
            return
        try:
            if pending["type"] == "rewrite":
                draft = await store.get_draft(pending["ref"])
                if draft is None or draft.get("user_id") != user.id or draft.get("status") != "pending":
                    await _send(chat_id, "That draft is no longer pending. See /drafts.")
                    return
                updated = await rewrite_pending_draft(user, draft, text, llm)
                card, markup = draft_card(updated, user, now, header="✏️ Updated in Gmail:")
                await _send(chat_id, card, markup)
            else:
                item = await store.get_item(pending["ref"])
                if item is None or item.user_id != user.id or item.status != "open":
                    await _send(chat_id, "That item is no longer open. See /tasks.")
                    return
                due = await parse_due_text(text, llm, await analysis_context(user))
                if due is None:
                    await _send(chat_id, "I couldn't understand that date. Use the 📅 Due button again.")
                    return
                updated = await set_due(user, item, due)
                card, markup = item_card(updated, user, now, header="📅 Deadline updated:")
                await _send(chat_id, card, markup)
        except ActionError as exc:
            await _send(chat_id, f"❌ {escape_markdown(str(exc))}")
        except (QuotaExceeded, LLMError) as exc:
            await _llm_failed(user, chat_id, exc)
        return

    await store.set_pending_action(user.id, {"type": "text", "text": text[:2000], "expires_at": now + PENDING_TTL})
    await _send(
        chat_id,
        "What should I do with this?",
        keyboard([button("➕ Add as task", cb("t", "add")), button("💬 Ask Lola", cb("t", "ask"))]),
    )
