"""Per-user LLM access.

* A user with their own API key (/setkey) uses it without limits.
* Everybody else uses the shared server key with a token limit per usage window.
  The window restarts at every scheduled run (every 12h) — or lazily after
  USAGE_WINDOW_HOURS if no run happened. When the limit is used up, optional LLM
  features (drafting in the run, /rewrite, /chat, /raw) pause until the next run;
  the digest itself is always sent.
* Admins are not limited (it's their key).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from src import config
from src.llm_client import LLM
from src.models import User, UserLLMKey, utcnow
from src.providers import server_llm_config, user_llm_config
from src.schedule import fmt_local, next_slot, user_tz
from src.state import _aware, get_store


class QuotaExceeded(Exception):
    pass


class LLMUnavailable(Exception):
    """No shared LLM key configured on the server and the user has no own key."""


def effective_limit(user: User) -> int | None:
    """Token limit for the shared key; None = unlimited."""
    if user.is_admin or user.token_limit == 0:
        return None
    if user.token_limit is not None:
        return user.token_limit
    limit = config.settings.user_token_limit
    return limit if limit > 0 else None


def _window_start(data: dict, now: datetime) -> datetime | None:
    start = _aware(data.get("window_start"))
    if start is None or now - start >= timedelta(hours=config.settings.usage_window_hours):
        return None
    return start


async def current_usage(user_id: int) -> tuple[int, datetime | None]:
    """Tokens used from the shared key in the current window, and when the window started."""
    data = await get_store().get_usage(user_id)
    start = _window_start(data, utcnow())
    return (int(data.get("tokens", 0)), start) if start else (0, None)


async def start_window(user_id: int) -> None:
    """Called at the start of each scheduled run: a fresh limit for the next 12h."""
    await get_store().reset_usage(user_id, utcnow())


async def record_usage(user_id: int, tokens: int, own: bool) -> None:
    store = get_store()
    now = utcnow()
    if _window_start(await store.get_usage(user_id), now) is None:
        await store.reset_usage(user_id, now, tokens=0 if own else tokens, own_tokens=tokens if own else 0)
    else:
        await store.increment_usage(user_id, tokens, own=own)


async def get_llm(user: User) -> LLM:
    """LLM for ``user``: own key (unlimited) or the shared key guarded by the user's limit."""
    own = await get_store().get_llm_key(user.id)
    if own:
        cfg = user_llm_config(own.provider, own.api_key, own.model)

        async def record_own(tokens: int) -> None:
            await record_usage(user.id, tokens, own=True)

        return LLM(cfg, on_usage=record_own)

    cfg = server_llm_config()
    if cfg is None:
        raise LLMUnavailable()
    limit = effective_limit(user)
    used, _ = await current_usage(user.id)
    counter = {"used": used}  # local view, so a long run doesn't re-read Firestore before every call

    async def check() -> None:
        if limit is not None and counter["used"] >= limit:
            raise QuotaExceeded()

    async def record(tokens: int) -> None:
        counter["used"] += tokens
        await record_usage(user.id, tokens, own=False)

    return LLM(cfg, before_call=check, on_usage=record)


@dataclass
class UsageStatus:
    own_key: UserLLMKey | None
    provider: str | None
    model: str | None
    used: int
    limit: int | None
    window_start: datetime | None
    next_run: datetime | None = None  # user's next scheduled summary (limit restarts then)

    @property
    def exhausted(self) -> bool:
        return self.own_key is None and self.limit is not None and self.used >= self.limit

    @property
    def resets_by(self) -> datetime | None:
        """The limit restarts at the next scheduled run, or when the window expires if earlier."""
        candidates = [self.next_run] if self.next_run else []
        if self.window_start is not None:
            candidates.append(self.window_start + timedelta(hours=config.settings.usage_window_hours))
        return min(candidates) if candidates else None


async def usage_status(user: User) -> UsageStatus:
    own = await get_store().get_llm_key(user.id)
    used, start = await current_usage(user.id)
    next_run = next_slot(utcnow(), user_tz(user))
    if own:
        cfg = user_llm_config(own.provider, own.api_key, own.model)
        return UsageStatus(own, cfg.provider, cfg.model, used, None, start, next_run)
    cfg = server_llm_config()
    return UsageStatus(
        None, cfg.provider if cfg else None, cfg.model if cfg else None, used, effective_limit(user), start, next_run
    )


def limit_reached_text(status: UsageStatus | None = None, user: User | None = None) -> str:
    when = ""
    if status and status.resets_by and user:
        when = f" at {fmt_local(status.resets_by, user, '%H:%M')}"
    return (
        "⏳ You've used your AI limit for this 12-hour period. Drafting and questions are paused "
        f"until your next summary{when}; your summaries keep coming.\n\n"
        "Tip: add your own API key with /setkey to use Lola without limits."
    )
