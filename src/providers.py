"""LLM providers (all via OpenAI-compatible chat-completions endpoints)."""

from dataclasses import dataclass

from src import config
from src.llm_client import LLMConfig


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    url: str
    default_model: str
    key_url: str


PROVIDERS: dict[str, Provider] = {
    "gemini": Provider(
        "gemini",
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "gemini-2.5-flash-lite",
        "https://aistudio.google.com/apikey",
    ),
    "deepseek": Provider(
        "deepseek",
        "DeepSeek",
        "https://api.deepseek.com/chat/completions",
        "deepseek-chat",
        "https://platform.deepseek.com/api_keys",
    ),
    "openai": Provider(
        "openai",
        "OpenAI",
        "https://api.openai.com/v1/chat/completions",
        "gpt-4.1-mini",
        "https://platform.openai.com/api-keys",
    ),
}


def server_llm_config() -> LLMConfig | None:
    """The shared LLM: first provider in LLM_PROVIDER_ORDER whose key is configured."""
    s = config.settings
    configured = {
        "gemini": (s.gemini_api_key, s.gemini_model, PROVIDERS["gemini"].url),
        # server-side DeepSeek keeps its configurable endpoint (e.g. the opencode.ai gateway)
        "deepseek": (s.deepseek_api_key, s.deepseek_model, s.deepseek_api_url),
        "openai": (s.openai_api_key, s.openai_model, PROVIDERS["openai"].url),
    }
    for name in (n.strip().lower() for n in s.llm_provider_order.split(",")):
        if name in configured and configured[name][0]:
            key, model, url = configured[name]
            return LLMConfig(provider=name, model=model or PROVIDERS[name].default_model, url=url, api_key=key)
    return None


def user_llm_config(provider: str, api_key: str, model: str = "") -> LLMConfig:
    p = PROVIDERS[provider]
    return LLMConfig(provider=provider, model=model or p.default_model, url=p.url, api_key=api_key, own_key=True)
