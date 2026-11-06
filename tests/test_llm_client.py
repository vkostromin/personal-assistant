import json

import httpx
import pytest

from src import config, llm_client
from src.llm_client import LLM, LLMError, PROVIDERS, analyze_emails, check_key, server_llm, user_llm
from tests.conftest import make_settings


@pytest.fixture
def http(monkeypatch):
    """Route llm_client's httpx calls to a handler; records requests."""
    state = {"requests": [], "status": 200, "body": None}

    def handler(request: httpx.Request):
        state["requests"].append({"url": str(request.url), "auth": request.headers.get("authorization"),
                                  "json": json.loads(request.content)})
        body = state["body"] or {"choices": [{"message": {"content": '{"drafts": [], "promises": [], "summary_items": ["x"]}'}}],
                                 "usage": {"prompt_tokens": 100, "completion_tokens": 23, "total_tokens": 123}}
        return httpx.Response(state["status"], json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return state


def test_server_provider_order(monkeypatch):
    monkeypatch.setattr(config, "settings", make_settings(gemini_api_key="g", deepseek_api_key="d", openai_api_key="o"))
    assert server_llm().provider == "gemini"
    monkeypatch.setattr(config, "settings", make_settings(gemini_api_key="", deepseek_api_key="d", openai_api_key="o"))
    llm = server_llm()
    assert llm.provider == "deepseek" and llm.model == "deepseek-v4-flash" and "opencode.ai" in llm.api_url
    monkeypatch.setattr(config, "settings", make_settings(gemini_api_key="", deepseek_api_key="", openai_api_key="o"))
    assert server_llm().provider == "openai"
    monkeypatch.setattr(config, "settings", make_settings(gemini_api_key="", deepseek_api_key="", openai_api_key=""))
    assert server_llm() is None


async def test_gemini_request_and_token_accounting(http):
    llm = server_llm()
    assert llm.model == "gemini-2.5-flash-lite" and not llm.own_key
    await analyze_emails([{"id": "m1", "thread_id": "t1", "from": "a", "subject": "s", "date": "", "body": "b"}], "me@x.com", llm, "sig")
    await analyze_emails([{"id": "m2", "thread_id": "t2", "from": "a", "subject": "s", "date": "", "body": "b"}], "me@x.com", llm, "sig")
    req = http["requests"][0]
    assert req["url"] == PROVIDERS["gemini"].url
    assert req["auth"] == "Bearer server-gemini-key"
    assert req["json"]["model"] == "gemini-2.5-flash-lite" and req["json"]["max_tokens"] == 8000
    prompt = req["json"]["messages"][1]["content"]
    assert "Message ID: m1" in prompt and "Thread ID: t1" in prompt  # lets the LLM thread replies
    assert llm.tokens_used == 246


async def test_drafts_off_prompt_and_result(http):
    http["body"] = {"choices": [{"message": {"content": json.dumps({"drafts": [{"to": "a", "subject": "s", "body": "b"}], "promises": [], "summary_items": []})}}]}
    llm = server_llm()
    result = await analyze_emails([{"from": "a", "subject": "s", "date": "", "body": "b"}], "me@x.com", llm, "sig", with_drafts=False)
    assert result.drafts == []  # ignored even if the model wrote some
    assert "Do NOT write draft replies" in http["requests"][0]["json"]["messages"][1]["content"]
    assert llm.tokens_used > 0  # no usage reported -> estimated


async def test_openai_payload_quirks(http):
    await llm_client._call(user_llm("openai", "k"), [{"role": "user", "content": "hi"}])
    await llm_client._call(user_llm("openai", "k", "gpt-5-mini"), [{"role": "user", "content": "hi"}])
    first, second = http["requests"][0]["json"], http["requests"][1]["json"]
    assert first["max_completion_tokens"] == 8000 and "max_tokens" not in first and first["temperature"] == 0.1
    assert "temperature" not in second


async def test_errors_are_typed(http):
    http["status"] = 401
    with pytest.raises(LLMError) as exc:
        await llm_client._call(user_llm("gemini", "bad"), [{"role": "user", "content": "hi"}])
    assert exc.value.is_auth_error


@pytest.mark.parametrize("status,ok,fragment", [
    (200, True, ""), (429, True, "rate-limited"), (401, False, "rejected"), (404, False, "model name"),
])
async def test_check_key(http, status, ok, fragment):
    http["status"] = status
    result_ok, message = await check_key(user_llm("gemini", "k", "gemini-x"))
    assert result_ok is ok and fragment in message
    assert http["requests"][0]["json"]["max_tokens"] == 16


def test_every_provider_has_a_default_model():
    for p in PROVIDERS.values():
        llm = user_llm(p.name, "k")
        assert llm.own_key and llm.model == p.default_model and isinstance(llm, LLM)
