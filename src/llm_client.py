import json
import logging
from datetime import datetime

import httpx
from uuid import uuid4

log = logging.getLogger(__name__)

from src.prompts import ANALYZE_CHAT, ANALYZE_EMAILS, ANSWER_QUESTION, COMPOSE_SUMMARY, REWRITE_DRAFT
from src.models import ChatSummary, Draft, EmailSummary, Promise


async def _call(
    messages: list[dict],
    api_key: str,
    api_url: str,
    model: str = "deepseek-v4-flash",
    response_format: dict | None = None,  # kept for signature compat, not sent
) -> str:
    try:
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 8000,
        }
        # response_format omitted — not supported by all endpoints
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                api_url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]
            return msg.get("content") or ""
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"LLM API error {e.response.status_code}: {e.response.text[:200]}") from e


async def analyze_emails(
    messages: list[dict], account: str, api_key: str, api_url: str, signature: str
) -> EmailSummary:
    formatted = "\n---\n".join(
        f"From: {m['from']}\nSubject: {m['subject']}\nDate: {m['date']}\nBody:\n{m['body'][:1000]}"
        for m in messages
    )
    prompt = ANALYZE_EMAILS.format(account=account, emails=formatted, signature=signature)
    content = await _call(
        [{"role": "system", "content": "You are Lola, an AI personal assistant. Respond only in valid JSON."},
         {"role": "user", "content": prompt}],
        api_key,
        api_url,
        response_format={"type": "json_object"},
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
            id=str(uuid4()),
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


async def analyze_chat(
    messages: list[dict], space_name: str, api_key: str, api_url: str
) -> ChatSummary:
    formatted = "\n".join(
        f"[{m['time'][:16]}] {m['sender']}: {m['text']}" for m in messages
    )
    prompt = ANALYZE_CHAT.format(space_name=space_name, messages=formatted)
    content = await _call(
        [{"role": "system", "content": "You are Lola, an AI personal assistant. Respond only in valid JSON."},
         {"role": "user", "content": prompt}],
        api_key,
        api_url,
        response_format={"type": "json_object"},
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


async def rewrite_draft(
    to: str, subject: str, body: str, context: str, api_key: str, api_url: str, signature: str
) -> dict | None:
    prompt = REWRITE_DRAFT.format(to=to, subject=subject, body=body, context=context, signature=signature)
    content = await _call(
        [{"role": "system", "content": "You are Lola, an AI personal assistant. Respond only in valid JSON."},
         {"role": "user", "content": prompt}],
        api_key,
        api_url,
        response_format={"type": "json_object"},
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
    api_key: str,
    api_url: str,
    signature: str,
) -> str:
    prompt = ANSWER_QUESTION.format(
        question=question,
        drafts_summary=drafts_summary,
        promises_summary=promises_summary,
        last_activity=last_activity,
        signature=signature,
    )
    return await _call(
        [{"role": "system", "content": "You are Lola, an AI personal assistant. Respond concisely."},
         {"role": "user", "content": prompt}],
        api_key,
        api_url,
    )


async def compose_summary(
    email_summary: str,
    chat_summary: str,
    promises: str,
    date: str,
    api_key: str,
    api_url: str,
    signature: str,
) -> str:
    prompt = COMPOSE_SUMMARY.format(
        email_summary=email_summary,
        chat_summary=chat_summary,
        promises=promises,
        date=date,
        signature=signature,
    )
    return await _call(
        [{"role": "system", "content": "You are Lola, an AI personal assistant. Compose a concise summary."},
         {"role": "user", "content": prompt}],
        api_key,
        api_url,
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
