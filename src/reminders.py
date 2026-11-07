"""Hourly reminders: deadlines, overdue promises, unanswered requests, snoozes ending."""

import logging
from collections import defaultdict
from datetime import datetime

from src import config
from src.cards import draft_card, humanize, item_card
from src.items import after_reminder, in_quiet_hours
from src.models import Item, User, utcnow
from src.schedule import fmt_local, user_tz
from src.state import _aware, get_store
from src.telegram_client import escape_markdown, send_message

log = logging.getLogger(__name__)


def reminder_header(item: Item, user: User, now: datetime) -> str:
    snoozed = _aware(item.snoozed_until)
    if snoozed and snoozed <= now:
        return "⏰ *Reminder* (snooze is over)"
    due = _aware(item.due_at)
    who = escape_markdown(item.counterparty or "They")
    if due and due > now:
        return f"⏰ *Due in {humanize((due - now).total_seconds())}*"
    if item.kind == "waiting":
        return f"⏳ *No answer yet* — {who} was expected by {fmt_local(due, user, '%a %d %b %H:%M')}. Nudge them?"
    if item.kind == "promise":
        return "⚠️ *Overdue — you promised this*"
    return "⚠️ *Overdue*"


def _urgency(entry: tuple[str, object]) -> tuple:
    kind, obj = entry
    if kind == "d":
        return (1, _aware(obj.get("next_remind_at")) or utcnow())
    return ({"high": 0, "medium": 1, "low": 2}[obj.priority], _aware(obj.due_at) or utcnow())


async def run_reminders(now: datetime | None = None) -> dict:
    now = now or utcnow()
    store = get_store()
    s = config.settings
    stats = {"reminders": 0, "quiet": 0, "postponed": 0}

    by_user: dict[int, list[tuple[str, object]]] = defaultdict(list)
    for item in await store.items_due_for_reminder(now):
        if item.status == "open":
            by_user[item.user_id].append(("i", item))
        else:
            await store.update_item(item.id, {"next_remind_at": None})
    for draft in await store.drafts_due_for_reminder(now):
        if draft.get("status") == "pending":
            by_user[draft["user_id"]].append(("d", draft))
        else:
            await store.update_draft(draft["id"], {"next_remind_at": None})

    for user_id, entries in by_user.items():
        user = await store.get_user(user_id)
        if user is None or not user.is_active:
            continue
        tz = user_tz(user)
        if in_quiet_hours(now, tz):
            stats["quiet"] += len(entries)  # stays due; sent when quiet hours end
            continue
        entries.sort(key=_urgency)
        for n, (kind, obj) in enumerate(entries):
            if n >= s.max_reminders_per_run:
                stats["postponed"] += 1  # still due: goes out next hour
                continue
            try:
                if kind == "i":
                    text, markup = item_card(obj, user, now, header=reminder_header(obj, user, now))
                    await send_message(s.telegram_bot_token, user.chat_id, text, reply_markup=markup)
                    await store.update_item(obj.id, after_reminder(obj, now, tz))
                else:
                    text, markup = draft_card(obj, user, now, header="⏰ *Back from snooze* — reply ready to send")
                    await send_message(s.telegram_bot_token, user.chat_id, text, reply_markup=markup)
                    await store.update_draft(obj["id"], {"snoozed_until": None, "next_remind_at": None})
                stats["reminders"] += 1
            except Exception as exc:  # noqa: BLE001 — one bad entry mustn't block the rest
                log.error("Reminder failed for user %s: %s", user_id, exc, exc_info=True)
    if any(stats.values()):
        log.info("Reminders: %s", stats)
    return stats
