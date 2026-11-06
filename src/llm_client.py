import json
import logging
from dataclasses import dataclass
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

from src.prompts import ANALYZE_CHAT, ANALYZE_EMAILS, ANSWER_QUESTION, COMPOSE_SUMMARY, DRAFTS_OFF, DRAFTS_ON, REWRITE_DRAFT
from src.models import ChatSummary, Draft, EmailSummary, Promise


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    url: str
    default_model: str
    key_help: str


# All providers are called through the OpenAI-compatible chat completions API.
PROVIDERS: dict[str, Provider] = {
    "gemini": Provider(
        "gemini", "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "gemini-2.5-flash-lite", "aistudio.google.com/apikey",
    ),
    "deepseek": Provider(
        "deepseek", "DeepSeek", "https://api.deepseek.com/chat/completions",
        "deepseek-chat", "platform.deepseek.com/api_keys",
    ),
    "openai": Provider(
        "openai", "OpenAI", "https://api.openai.com/v1/chat/completions",
        "gpt-4.1-mini", "platform.openai.com/api-keys",
    ),
}


@dataclass
class LLM:
    """A provider + key + model, and the tokens spent through it (for per-user limits)."""

    provider: str
    model: str
    api_key: str
    api_url: str
    own_key: bool = False  # user's own key -> no limits
    tokens_used: int = 0

    @property
    def label(self) -> str:
        p = PROVIDERS.get(self.provider)
        return f"{p.label if p else self.provider} ({self.model})"


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def is_auth_error(self) -> bool:
        return self.status in (401, 403)


def server_llm() -> LLM | None:
    """Shared LLM: the first provider whose key is configured (Gemini -> DeepSeek -> OpenAI)."""
    from src import config

    s = config.settings
    if s.gemini_api_key:
        return LLM("gemini", s.gemini_model, s.gemini_api_key, PROVIDERS["gemini"].url)
    if s.deepseek_api_key:
        return LLM("deepseek", s.deepseek_model, s.deepseek_api_key, s.deepseek_api_url)
    if s.openai_api_key:
        return LLM("openai", s.openai_model, s.openai_api_key, PROVIDERS["openai"].url)
    return None


def user_llm(provider: str, api_key: str, model: str = "") -> LLM:
    p = PROVIDERS[provider]
    return LLM(provider, model or p.default_model, api_key, p.url, own_key=True)


def _payload(llm: LLM, messages: list[dict], max_tokens: int) -> dict:
    payload: dict = {"model": llm.model, "messages": messages}
    if llm.provider == "openai":
        # current OpenAI models take max_completion_tokens; reasoning models reject temperature
        payload["max_completion_tokens"] = max_tokens
        if not llm.model.startswith(("o1", "o3", "o4", "gpt-5")):
            payload["temperature"] = 0.1
    else:
        payload["max_tokens"] = max_tokens
        payload["temperature"] = 0.1
    return payload


async def _call(llm: LLM, messages: list[dict], max_tokens: int = 8000) -> str:
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                llm.api_url,
                headers={"Authorization": f"Bearer {llm.api_key}", "Content-Type": "application/json"},
                json=_payload(llm, messages, max_tokens),
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        raise LLMError(f"LLM API error {e.response.status_code}: {e.response.text[:200]}", e.response.status_code) from e
    except httpx.HTTPError as e:
        raise LLMError(f"LLM API request failed: {e.__class__.__name__}") from e

    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    usage = data.get("usage") or {}
    tokens = usage.get("total_tokens")
    if not tokens:  # provider didn't report usage: rough estimate (~4 chars per token)
        tokens = (sum(len(m.get("content", "")) for m in messages) + len(content)) // 4
    llm.tokens_used += int(tokens)
    return content


async def check_key(llm: LLM) -> tuple[bool, str]:
    """Tiny request to validate a user's key + model. Returns (ok, message)."""
    try:
        await _call(llm, [{"role": "user", "content": "Reply with OK."}], max_tokens=16)
        return True, ""
    except LLMError as e:
        if e.status == 429:
            return True, "the key works but is rate-limited or out of quota right now"
        if e.is_auth_error:
            return False, "the key was rejected"
        if e.status in (400, 404):
            return False, f"the provider rejected the request — check the model name ({llm.model})"
        return False, str(e)[:200]


SYSTEM_JSON = {"role": "system", "content": "You are Lola, an AI personal assistant. Respond only in valid JSON."}


async def analyze_emails(
    messages: list[dict], account: str, llm: LLM, signature: str, with_drafts: bool = True
) -> EmailSummary:
    formatted = "\n---\n".join(
        f"Message ID: {m.get('id', '')}\nThread ID: {m.get('thread_id', '')}\n"
        f"From: {m['from']}\nSubject: {m['subject']}\nDate: {m['date']}\nBody:\n{m['body'][:1000]}"
        for m in messages
    )
    draft_instruction = DRAFTS_ON.format(signature=signature) if with_drafts else DRAFTS_OFF
    prompt = ANALYZE_EMAILS.format(account=account, emails=formatted, draft_instruction=draft_instruction)
    content = await _call(llm, [SYSTEM_JSON, {"role": "user", "content": prompt}])
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
            thread_id=d.get("thread_id") or None,
            original_email_id=d.get("original_email_id") or None,
            created_at=now,
        )
        for d in data.get("drafts", [])
    ] if with_drafts else []
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
    content = await _call(llm, [SYSTEM_JSON, {"role": "user", "content": prompt}])
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
    to: str, subject: str, body: str, context: str, llm: LLM, signature: str
) -> dict | None:
    prompt = REWRITE_DRAFT.format(to=to, subject=subject, body=body, context=context, signature=signature)
    content = await _call(llm, [SYSTEM_JSON, {"role": "user", "content": prompt}])
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
    return await _call(
        llm,
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
    return await _call(
        llm,
        [{"role": "system", "content": "You are Lola, an AI personal assistant. Compose a concise summary."},
         {"role": "user", "content": prompt}],
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
