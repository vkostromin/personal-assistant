"""Which LLM a user gets, and the per-user limits for the shared (server) key.

* Own key (/setkey)        -> that provider, no limits.
* Shared key, admin        -> no limits (unless LIMIT_ADMINS=true).
* Shared key, other users  -> USER_EMAIL_LIMIT emails analysed per window and
  USER_TOKEN_LIMIT tokens per window. When the tokens are used up, "extras"
  (draft replies, /rewrite, /chat, /raw) pause until the next scheduled run.
  The summary itself keeps working.

A window starts at the user's scheduled run (every 12h) and expires after
USAGE_WINDOW_HOURS at the latest.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from src import config
from src.llm_client import LLM, PROVIDERS, server_llm, user_llm
from src.models import User, utcnow
from src.state import _aware, get_store

log = logging.getLogger(__name__)


async def resolve_llm(user: User) -> LLM | None:
    own = await get_store().get_user_llm(user.id)
    if own and own.get("provider") in PROVIDERS and own.get("api_key"):
        return user_llm(own["provider"], own["api_key"], own.get("model", ""))
    return server_llm()


def is_unlimited(user: User, llm: LLM) -> bool:
    s = config.settings
    return llm.own_key or (user.is_admin and not s.limit_admins) or s.user_token_limit <= 0


@dataclass
class Budget:
    unlimited: bool
    token_limit: int
    email_limit: int
    tokens_used: int
    emails_used: int
    window_start: datetime | None

    @property
    def tokens_left(self) -> int:
        return max(0, self.token_limit - self.tokens_used)

    @property
    def exhausted(self) -> bool:
        return not self.unlimited and self.tokens_used >= self.token_limit

    @property
    def resets_at(self) -> datetime | None:
        if self.window_start is None:
            return None
        return self.window_start + timedelta(hours=config.settings.usage_window_hours)


async def get_budget(user: User, llm: LLM) -> Budget:
    s = config.settings
    usage = await get_store().get_usage(user.id)
    start = _aware(usage.get("window_start"))
    if start is None or start + timedelta(hours=s.usage_window_hours) <= utcnow():
        tokens, emails, start = 0, 0, None  # previous window is over
    else:
        tokens, emails = int(usage.get("tokens", 0)), int(usage.get("emails", 0))
    unlimited = is_unlimited(user, llm)
    email_limit = s.max_emails_per_run if unlimited else min(s.user_email_limit, s.max_emails_per_run)
    return Budget(
        unlimited=unlimited,
        token_limit=s.user_token_limit,
        email_limit=email_limit,
        tokens_used=tokens,
        emails_used=emails,
        window_start=start,
    )


async def record_usage(user: User, llm: LLM, emails: int = 0) -> None:
    """Persist what ``llm`` spent (also for own keys — shown in /usage)."""
    tokens, llm.tokens_used = llm.tokens_used, 0
    try:
        await get_store().add_usage(user.id, tokens=tokens, emails=emails)
    except Exception as exc:  # noqa: BLE001 — accounting must never break the feature
        log.error("Failed to record usage for %s: %s", user.id, exc)


def limit_message(budget: Budget) -> str:
    when = budget.resets_at.strftime("%H:%M UTC") if budget.resets_at else "the next run"
    return (
        "⏳ You've used your AI limit for this 12-hour period, so draft replies, "
        "/rewrite, /chat and /raw are paused. Your summaries continue as usual.\n\n"
        f"The limit resets with the next scheduled run (around {when}).\n"
        "Want no limits? Add your own API key: /setkey"
    )
