import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

from src.prompts import ANALYZE_CHAT, ANALYZE_EMAILS, ANSWER_QUESTION, COMPOSE_SUMMARY, REWRITE_DRAFT
from src.models import ChatSummary, Draft, EmailSummary, Promise


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


async def analyze_emails(messages: list[dict], account: str, llm: LLM, signature: str) -> EmailSummary:
    formatted = "\n---\n".join(
        f"From: {m['from']}\nSubject: {m['subject']}\nDate: {m['date']}\nBody:\n{m['body'][:1000]}"
        for m in messages
    )
    prompt = ANALYZE_EMAILS.format(account=account, emails=formatted, signature=signature)
    content = await llm.complete(
        [{"role": "system", "content": "You are Lola, an AI personal assistant. Respond only in valid JSON."},
         {"role": "user", "content": prompt}],
    )
    log.info("analyze_emails raw: %s", content[:300])
    content = _clean_json(content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        log.error("analyze_emails: JSON parse failed: %s. Raw response preview: %s", e, content[:1000])
        return EmailSummary(drafts=[], promises=[], summary_items=["Failed to analyze emails"])

    now = datetime.now()
    drafts = [
        Draft(
            account=account,
            to=d.get("to", ""),
            subject=d.get("subject", ""),
            body=d.get("body", ""),
            thread_id=d.get("thread_id"),
            original_email_id=d.get("original_email_id"),
            created_at=now,
        )
        for d in data.get("drafts", [])
    ]
    promises = [
        Promise(
            text=p.get("text", ""),
            source="email",
            source_detail=p.get("source_detail", ""),
            created_at=now,
        )
        for p in data.get("promises", [])
    ]
    return EmailSummary(drafts=drafts, promises=promises, summary_items=data.get("summary_items", []))


async def analyze_chat(messages: list[dict], space_name: str, llm: LLM) -> ChatSummary:
    formatted = "\n".join(
        f"[{m['time'][:16]}] {m['sender']}: {m['text']}" for m in messages
    )
    prompt = ANALYZE_CHAT.format(space_name=space_name, messages=formatted)
    content = await llm.complete(
        [{"role": "system", "content": "You are Lola, an AI personal assistant. Respond only in valid JSON."},
         {"role": "user", "content": prompt}],
    )
    content = _clean_json(content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        log.error("analyze_chat: JSON parse failed: %s. Raw response preview: %s", e, content[:1000])
        return ChatSummary(space_name=space_name, missed_questions=[], promises=[], summary_items=[])

    now = datetime.now()
    promises = [
        Promise(
            text=p.get("text", ""),
            source="chat",
            source_detail=p.get("source_detail", ""),
            created_at=now,
        )
        for p in data.get("promises", [])
    ]
    return ChatSummary(
        space_name=space_name,
        missed_questions=data.get("missed_questions", []),
        promises=promises,
        summary_items=data.get("summary_items", []),
    )


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
) -> str:
    prompt = COMPOSE_SUMMARY.format(
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
