"""Telegram "cards": messages with inline buttons for drafts and items.

Callback data format (max 64 bytes): "<type>:<action>:<id>[:<arg>]"
  type d = draft, i = item, t = free text the user sent (ref is unused)
"""

from datetime import datetime, timezone

from src.chat_client import space_link
from src.gmail_client import drafts_link, thread_link
from src.items import KIND_LABEL, PRIORITY_ICON, FocusEntry
from src.models import Item, User
from src.schedule import fmt_local
from src.telegram_client import escape_markdown as esc

DRAFT_BODY_PREVIEW = 1500


def cb(kind: str, action: str, ref: str = "", arg: str = "") -> str:
    data = f"{kind}:{action}:{ref}" + (f":{arg}" if arg else "")
    assert len(data.encode()) <= 64, data
    return data


def button(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def url_btn(text: str, url: str) -> dict:
    return {"text": text, "url": url}


def keyboard(*rows: list[dict]) -> dict:
    return {"inline_keyboard": [r for r in rows if r]}


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def humanize(seconds: float) -> str:
    seconds = abs(seconds)
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def due_text(due: datetime | None, now: datetime, user: User) -> str:
    due = _aware(due)
    if due is None:
        return ""
    when = fmt_local(due, user, "%a %d %b %H:%M")
    delta = (due - now).total_seconds()
    if delta < 0:
        return f"⚠️ overdue by {humanize(delta)} ({when})"
    return f"📅 due {when} (in {humanize(delta)})"


# ------------------------------------------------------------------- items
def item_link(item: Item) -> str:
    if item.link:
        return item.link
    if item.thread_id and item.account:
        return thread_link(item.account, item.thread_id)
    if item.space:
        return space_link(item.space)
    return ""


def item_text(item: Item, user: User, now: datetime) -> str:
    lines = [f"{KIND_LABEL[item.kind]} · {PRIORITY_ICON[item.priority]} {item.priority}", f"*{esc(item.title)}*"]
    if item.counterparty:
        lines.append(f"👤 {esc(item.counterparty)}")
    if item.due_at:
        lines.append(due_text(item.due_at, now, user))
    snoozed = _aware(item.snoozed_until)
    if snoozed and snoozed > now:
        lines.append(f"⏰ snoozed until {fmt_local(snoozed, user, '%a %H:%M')}")
    origin = {"email": "📧 from email", "sent": "📤 from my email", "chat": "💬 from Chat", "manual": "✍️ added by you"}[item.source]
    if item.subject:
        origin += f" “{esc(item.subject[:60])}”"
    if item.account:
        origin += f" · {esc(item.account)}"
    lines.append(origin)
    return "\n".join(lines)


def item_keyboard(item: Item) -> dict:
    row1 = [button("✅ Done", cb("i", "done", item.id)), button("⏰ Snooze", cb("i", "snz", item.id)),
            button("📅 Due", cb("i", "due", item.id))]
    row2 = [button("✖️ Cancel", cb("i", "cancel", item.id))]
    if item.kind == "waiting" and item.thread_id and item.account:
        row2.append(button("✍️ Follow-up", cb("i", "fu", item.id)))
    link = item_link(item)
    if link:
        row2.append(url_btn("🔗 Open", link))
    return keyboard(row1, row2)


def item_card(item: Item, user: User, now: datetime, header: str = "") -> tuple[str, dict]:
    text = item_text(item, user, now)
    return (f"{header}\n\n{text}" if header else text), item_keyboard(item)


def snooze_keyboard(kind: str, ref: str) -> dict:
    return keyboard(
        [button("2h", cb(kind, "snz", ref, "2")), button("4h", cb(kind, "snz", ref, "4")),
         button("8h", cb(kind, "snz", ref, "8")), button("Tomorrow 9:00", cb(kind, "snz", ref, "tm"))],
        [button("« Back", cb(kind, "back", ref))],
    )


def due_keyboard(ref: str) -> dict:
    return keyboard(
        [button("Today 18:00", cb("i", "due", ref, "today")), button("Tomorrow", cb("i", "due", ref, "tm")),
         button("+3 days", cb("i", "due", ref, "3d"))],
        [button("Next Monday", cb("i", "due", ref, "mon")), button("No deadline", cb("i", "due", ref, "none")),
         button("✏️ Other…", cb("i", "due", ref, "other"))],
        [button("« Back", cb("i", "back", ref))],
    )


# ------------------------------------------------------------------ drafts
def draft_text(draft: dict, user: User, now: datetime) -> str:
    priority = draft.get("priority", "medium")
    body = draft.get("body", "")
    if len(body) > DRAFT_BODY_PREVIEW:
        body = body[:DRAFT_BODY_PREVIEW] + "…"
    lines = [
        f"📧 Draft reply · {PRIORITY_ICON.get(priority, '🟡')} {priority}",
        f"*To:* {esc(draft.get('to', ''))}",
        f"*Subject:* {esc(draft.get('subject', ''))}",
        f"*From:* {esc(draft.get('account', ''))}",
    ]
    snoozed = _aware(draft.get("snoozed_until"))
    if snoozed and snoozed > now:
        lines.append(f"⏰ snoozed until {fmt_local(snoozed, user, '%a %H:%M')}")
    return "\n".join(lines) + "\n\n" + esc(body)


def draft_keyboard(draft: dict) -> dict:
    ref = draft["id"]
    link = thread_link(draft.get("account", ""), draft.get("thread_id")) or drafts_link(draft.get("account", ""))
    return keyboard(
        [button("📤 Send", cb("d", "send", ref)), button("✏️ Rewrite", cb("d", "rw", ref)),
         button("⏰ Snooze", cb("d", "snz", ref))],
        [button("🗑 Discard", cb("d", "del", ref)), button("✔️ Already sent", cb("d", "sent", ref)),
         url_btn("🔗 Gmail", link)],
    )


def draft_card(draft: dict, user: User, now: datetime, header: str = "") -> tuple[str, dict]:
    text = draft_text(draft, user, now)
    return (f"{header}\n\n{text}" if header else text), draft_keyboard(draft)


def send_confirm_keyboard(draft: dict) -> dict:
    return keyboard(
        [button(f"✅ Send to {draft.get('to', '')[:30]}", cb("d", "sendok", draft["id"]))],
        [button("« Back", cb("d", "back", draft["id"]))],
    )


# ------------------------------------------------------------------- lists
def entry_line(entry: FocusEntry, user: User, now: datetime) -> str:
    if entry.type == "draft":
        d = entry.ref
        return f"📧 Reply to {esc(d.get('to', '?'))} — “{esc(d.get('subject', '')[:60])}” (draft ready)"
    item: Item = entry.ref
    line = f"{KIND_LABEL[item.kind].split(' ', 1)[0]} {PRIORITY_ICON[item.priority]} {esc(item.title)}"
    if item.counterparty:
        line += f" — {esc(item.counterparty[:40])}"
    if item.due_at:
        line += f" · {due_text(item.due_at, now, user)}"
    return line


def numbered_keyboard(refs: list[tuple[str, str]], per_row: int = 5) -> dict:
    """refs = [(type 'd'|'i', id)] → number buttons that open the card."""
    buttons = [button(str(n), cb(t, "open", ref)) for n, (t, ref) in enumerate(refs, 1)]
    return keyboard(*[buttons[i:i + per_row] for i in range(0, len(buttons), per_row)])


def focus_message(entries: list[FocusEntry], user: User, now: datetime) -> tuple[str, dict | None]:
    if not entries:
        return "🎯 Nothing urgent right now. 🎉", None
    lines = ["🎯 *Focus now* — tap a number to act:\n"]
    refs = []
    for n, e in enumerate(entries, 1):
        lines.append(f"{n}. {entry_line(e, user, now)}")
        refs.append(("d" if e.type == "draft" else "i", e.ref["id"] if e.type == "draft" else e.ref.id))
    return "\n".join(lines), numbered_keyboard(refs)


def tasks_message(items: list[Item], user: User, now: datetime, limit: int = 25) -> tuple[str, dict | None]:
    if not items:
        return "No open items. Add one with /task <text> or forward me any message.", None
    groups = [("reply", []), ("promise", []), ("todo", []), ("waiting", [])]
    by_kind = dict(groups)
    for it in items:
        by_kind[it.kind].append(it)
    lines, refs = [], []
    for kind, group in groups:
        if not group:
            continue
        lines.append(f"\n*{KIND_LABEL[kind]}*")
        group.sort(key=lambda i: (_aware(i.due_at) or datetime.max.replace(tzinfo=timezone.utc), i.priority != "high"))
        for it in group:
            if len(refs) >= limit:
                break
            refs.append(("i", it.id))
            due = f" · {due_text(it.due_at, now, user)}" if it.due_at else ""
            who = f" — {esc(it.counterparty[:40])}" if it.counterparty else ""
            lines.append(f"{len(refs)}. {PRIORITY_ICON[it.priority]} {esc(it.title)}{who}{due}")
    more = f"\n…and {len(items) - len(refs)} more" if len(items) > len(refs) else ""
    return "📋 *Open items*" + "\n".join(lines) + more + "\n\nTap a number to act on it.", numbered_keyboard(refs)
