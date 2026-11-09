import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

from src.items import parse_due
from src.models import Draft, Item
from src.prompts import (
    ANALYZE_CHAT,
    ANALYZE_EMAILS,
    ANALYZE_SENT,
    ANSWER_QUESTION,
    CHAT_REPLY,
    COMPOSE_SUMMARY,
    CONTEXT,
    FOLLOW_UP,
    ITEM_RULES,
    PARSE_DUE,
    PARSE_TASK,
    REWRITE_DRAFT,
)


class LLMError(RuntimeError):
    """Provider returned an error (bad key, quota, model not found, outage...)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    url: str
    api_key: str
    own_key: bool = False  # user's personal key (no limits) vs the shared server key


async def chat_completion(config: LLMConfig, messages: list[dict], max_tokens: int = 8000) -> tuple[str, int]:
    """OpenAI-compatible chat completion. Returns (content, total tokens used)."""
    payload: dict = {
        "model": config.model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                config.url,
                headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        raise LLMError(
            f"{config.provider} API error {e.response.status_code}: {e.response.text[:200]}", e.response.status_code
        ) from e
    except httpx.HTTPError as e:
        raise LLMError(f"{config.provider} API unreachable: {e}") from e
    content = data["choices"][0]["message"].get("content") or ""
    tokens = (data.get("usage") or {}).get("total_tokens")
    if not tokens:  # provider didn't report usage: rough estimate (~4 chars per token)
        tokens = (sum(len(m["content"]) for m in messages) + len(content)) // 4
    return content, int(tokens)


UsageHook = Callable[[int], Awaitable[None]]
CheckHook = Callable[[], Awaitable[None]]


class LLM:
    """An LLM bound to one provider/key, with optional quota check and usage recording."""

    def __init__(self, config: LLMConfig, before_call: CheckHook | None = None, on_usage: UsageHook | None = None):
        self.config = config
        self._before_call = before_call
        self._on_usage = on_usage

    async def complete(self, messages: list[dict], essential: bool = False, max_tokens: int = 8000) -> str:
        """``essential`` calls (the digest itself) skip the quota check but are still counted."""
        if self._before_call and not essential:
            await self._before_call()
        content, tokens = await chat_completion(self.config, messages, max_tokens=max_tokens)
        if self._on_usage:
            await self._on_usage(tokens)
        return content


@dataclass
class AnalysisContext:
    """Per-user context for extraction: local time (to resolve "tomorrow") and open items."""

    now: datetime
    tz: tzinfo
    tz_name: str
    open_items: list[Item] = field(default_factory=list)

    def now_text(self) -> str:
        return self.now.astimezone(self.tz).strftime("%A %Y-%m-%d %H:%M") + f" ({self.tz_name})"

    def items_text(self, limit: int = 30) -> str:
        if not self.open_items:
            return "none"
        lines = []
        for it in self.open_items[-limit:]:
            due = it.due_at.astimezone(self.tz).strftime("%Y-%m-%d %H:%M") if it.due_at else "-"
            lines.append(f"{it.id} | {it.kind} | {it.title} | {it.counterparty or '-'} | {due}")
        return "\n".join(lines)

    def render(self) -> str:
        return CONTEXT.format(now=self.now_text(), open_items=self.items_text())

    @property
    def open_ids(self) -> set[str]:
        return {it.id for it in self.open_items}


@dataclass
class ExtractedItem:
    kind: str
    title: str
    counterparty: str = ""
    due_at: datetime | None = None
    priority: str = "medium"
    original_email_id: str | None = None
    message_ref: str | None = None  # Chat: "c3" = the message this item is about


@dataclass
class ItemUpdate:
    id: str
    status: str | None = None  # done | cancelled | None (unchanged)
    due_at: datetime | None = None
    due_changed: bool = False
    note: str = ""


@dataclass
class Analysis:
    drafts: list[Draft] = field(default_factory=list)
    items: list[ExtractedItem] = field(default_factory=list)
    updates: list[ItemUpdate] = field(default_factory=list)
    summary_items: list[str] = field(default_factory=list)


_KINDS = {"promise", "waiting", "reply", "todo"}
_PRIORITIES = {"high", "medium", "low"}


def _parse_items(raw, ctx: AnalysisContext, allowed_kinds: set[str] = _KINDS) -> list[ExtractedItem]:
    result = []
    for d in raw if isinstance(raw, list) else []:
        if not isinstance(d, dict):
            continue
        kind = str(d.get("kind", "")).lower()
        title = str(d.get("title") or "").strip()
        if kind not in allowed_kinds or not title:
            continue
        priority = str(d.get("priority", "medium")).lower()
        result.append(
            ExtractedItem(
                kind=kind,
                title=title[:200],
                counterparty=str(d.get("counterparty") or "")[:200],
                due_at=parse_due(d.get("due_at"), ctx.tz),
                priority=priority if priority in _PRIORITIES else "medium",
                original_email_id=str(d["original_email_id"]) if d.get("original_email_id") else None,
                message_ref=str(d["message_ref"]) if d.get("message_ref") else None,
            )
        )
    return result


def _parse_updates(raw, ctx: AnalysisContext) -> list[ItemUpdate]:
    result = []
    for u in raw if isinstance(raw, list) else []:
        if not isinstance(u, dict) or u.get("id") not in ctx.open_ids:
            continue  # never touch items the model wasn't shown (or invented ids)
        status = str(u.get("status") or "").lower()
        due_given = bool(u.get("due_at"))
        result.append(
            ItemUpdate(
                id=u["id"],
                status=status if status in ("done", "cancelled") else None,
                due_at=parse_due(u.get("due_at"), ctx.tz) if due_given else None,
                due_changed=due_given,
                note=str(u.get("note") or "")[:200],
            )
        )
    return [u for u in result if u.status or (u.due_changed and u.due_at)]


def _format_emails(messages: list[dict]) -> str:
    return "\n---\n".join(
        f"Message ID: {m['id']}\nFrom: {m['from']}\nTo: {m.get('to', '')}\nSubject: {m['subject']}\n"
        f"Date: {m['date']}\nBody:\n{m['body'][:1000]}"
        for m in messages
    )


async def _json_call(llm: LLM, prompt: str, name: str) -> dict | None:
    content = await llm.complete(
        [{"role": "system", "content": "You are Lola, an AI personal assistant. Respond only in valid JSON."},
         {"role": "user", "content": prompt}],
    )
    try:
        data = json.loads(_clean_json(content))
    except json.JSONDecodeError as e:
        log.error("%s: JSON parse failed: %s. Raw response preview: %s", name, e, content[:1000])
        return None
    return data if isinstance(data, dict) else None


async def analyze_emails(
    messages: list[dict], account: str, llm: LLM, signature: str, ctx: AnalysisContext
) -> Analysis:
    prompt = ANALYZE_EMAILS.format(
        account=account, emails=_format_emails(messages), signature=signature, context=ctx.render(), item_rules=ITEM_RULES
    )
    data = await _json_call(llm, prompt, "analyze_emails")
    if data is None:
        return Analysis(summary_items=["Failed to analyze emails"])

    now = datetime.now()
    # Thread/Message-ID come from the real email (never trust IDs invented by the model):
    # a made-up threadId makes Gmail reject the draft, and the ids are needed for threading
    # and for detecting later that the reply was sent.
    by_id = {m["id"]: m for m in messages}
    by_thread = {m.get("thread_id"): m for m in messages if m.get("thread_id")}
    drafts = []
    for d in data.get("drafts", []) if isinstance(data.get("drafts"), list) else []:
        original = by_id.get(str(d.get("original_email_id") or "")) or by_thread.get(str(d.get("thread_id") or ""))
        priority = str(d.get("priority", "medium")).lower()
        drafts.append(
            Draft(
                account=account,
                to=d.get("to", ""),
                subject=d.get("subject", ""),
                body=d.get("body", ""),
                thread_id=original.get("thread_id") or None if original else None,
                original_email_id=original["id"] if original else None,
                in_reply_to=original.get("message_id_header") or None if original else None,
                priority=priority if priority in _PRIORITIES else "medium",
                created_at=now,
            )
        )
    return Analysis(
        drafts=drafts,
        items=_parse_items(data.get("items"), ctx),
        updates=_parse_updates(data.get("updates"), ctx),
        summary_items=[str(x) for x in data.get("summary_items", []) if x][:20],
    )


async def analyze_sent(messages: list[dict], account: str, llm: LLM, ctx: AnalysisContext) -> Analysis:
    prompt = ANALYZE_SENT.format(account=account, emails=_format_emails(messages), context=ctx.render(), item_rules=ITEM_RULES)
    data = await _json_call(llm, prompt, "analyze_sent")
    if data is None:
        return Analysis()
    return Analysis(
        items=_parse_items(data.get("items"), ctx, allowed_kinds={"promise", "waiting"}),
        updates=_parse_updates(data.get("updates"), ctx),
    )


async def analyze_chat(messages: list[dict], space_name: str, llm: LLM, ctx: AnalysisContext) -> Analysis:
    # short refs (c1, c2…) instead of long Chat resource names, mapped back by the caller
    formatted = "\n".join(f"[c{n} {m['time'][:16]}] {m['sender']}: {m['text']}" for n, m in enumerate(messages, 1))
    prompt = ANALYZE_CHAT.format(space_name=space_name, messages=formatted, context=ctx.render(), item_rules=ITEM_RULES)
    data = await _json_call(llm, prompt, "analyze_chat")
    if data is None:
        return Analysis()
    return Analysis(
        items=_parse_items(data.get("items"), ctx),
        updates=_parse_updates(data.get("updates"), ctx),
        summary_items=[str(x) for x in data.get("summary_items", []) if x][:20],
    )


async def parse_task(text: str, llm: LLM, ctx: AnalysisContext) -> ExtractedItem | None:
    data = await _json_call(llm, PARSE_TASK.format(text=text[:2000], now=ctx.now_text()), "parse_task")
    if data is None:
        return None
    data.setdefault("kind", "todo")
    items = _parse_items([data], ctx, allowed_kinds={"todo", "promise", "waiting"})
    return items[0] if items else None


async def parse_due_text(text: str, llm: LLM, ctx: AnalysisContext) -> datetime | None:
    data = await _json_call(llm, PARSE_DUE.format(text=text[:200], now=ctx.now_text()), "parse_due")
    return parse_due(data.get("due_at"), ctx.tz) if data else None


async def suggest_chat_reply(item: Item, llm: LLM) -> str:
    prompt = CHAT_REPLY.format(title=item.title, counterparty=item.counterparty or "-", message=item.detail or "-")
    content = await llm.complete([{"role": "user", "content": prompt}])
    return content.strip().strip('"')


async def write_follow_up(item: Item, due_text: str, llm: LLM, signature: str) -> dict | None:
    prompt = FOLLOW_UP.format(
        title=item.title, counterparty=item.counterparty or "-", subject=item.subject or "-", due=due_text, signature=signature
    )
    data = await _json_call(llm, prompt, "follow_up")
    if not data or not data.get("body"):
        return None
    return data


async def rewrite_draft(to: str, subject: str, body: str, context: str, llm: LLM, signature: str) -> dict | None:
    prompt = REWRITE_DRAFT.format(to=to, subject=subject, body=body, context=context, signature=signature)
    content = await llm.complete(
        [{"role": "system", "content": "You are Lola, an AI personal assistant. Respond only in valid JSON."},
         {"role": "user", "content": prompt}],
    )
    content = _clean_json(content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


async def answer_question(
    question: str,
    drafts_summary: str,
    promises_summary: str,
    last_activity: str,
    llm: LLM,
    signature: str,
) -> str:
    prompt = ANSWER_QUESTION.format(
        question=question,
        drafts_summary=drafts_summary,
        promises_summary=promises_summary,
        last_activity=last_activity,
        signature=signature,
    )
    return await llm.complete(
        [{"role": "system", "content": "You are Lola, an AI personal assistant. Respond concisely."},
         {"role": "user", "content": prompt}],
    )


async def compose_summary(
    email_summary: str,
    chat_summary: str,
    promises: str,
    date: str,
    llm: LLM,
    signature: str,
    focus: str = "none",
) -> str:
    prompt = COMPOSE_SUMMARY.format(
        focus=focus,
        email_summary=email_summary,
        chat_summary=chat_summary,
        promises=promises,
        date=date,
        signature=signature,
    )
    return await llm.complete(
        [{"role": "system", "content": "You are Lola, an AI personal assistant. Compose a concise summary."},
         {"role": "user", "content": prompt}],
        essential=True,  # the digest is always delivered, even when the limit is used up
    )


def _clean_json(content: str) -> str:
    content = content.strip()
    # Isolates the outer JSON object by matching from the first '{' to the last '}'
    first_brace = content.find('{')
    last_brace = content.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        content = content[first_brace:last_brace+1]
    
    # Strip any potential leading/trailing markdown fences that weren't caught
    if content.startswith("```"):
        content = "\n".join(content.split("\n")[1:])
    if content.endswith("```"):
        content = "\n".join(content.split("\n")[:-1])
    content = content.strip()
    return content
