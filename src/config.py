from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    project_id: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    # --- Shared (server) LLM. The first provider in LLM_PROVIDER_ORDER with a key set is used.
    llm_provider_order: str = "gemini,deepseek,openai"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash-lite"
    deepseek_api_key: str = ""
    deepseek_api_url: str = "https://opencode.ai/zen/go/v1/chat/completions"
    deepseek_model: str = "deepseek-v4-flash"
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"

    # Per-user limit for the shared LLM key, in tokens (prompt + completion) per usage window.
    # Sized for ~100 emails + chat + digest + a few interactive commands per 12h (see README).
    # Users with their own API key (/setkey) and admins are not limited.
    user_token_limit: int = 150_000
    # The window restarts at every scheduled run (and at the latest after this many hours).
    usage_window_hours: int = 12
    telegram_bot_token: str = ""
    # Optional: bot username without "@" (otherwise fetched via getMe) — used for invite links
    telegram_bot_username: str = ""

    region: str = "us-central1"
    signature: str = "\n\n— Lola, Your AI Assistant"

    token_encryption_key: str = ""
    telegram_webhook_secret: str = ""
    run_secret: str = ""

    # Public base URL of the Cloud Run service, e.g. https://personal-assistant-xxx.a.run.app
    # Used to build the OAuth redirect URI (<PUBLIC_URL>/oauth/callback). If empty, it is
    # derived from the incoming webhook request.
    public_url: str = ""

    # Optional: Telegram user id that is allowed to become the first admin.
    # If empty, the first user who sends /start to a fresh deployment becomes admin.
    admin_telegram_id: str = ""

    # Summaries are sent at these local hours in each user's timezone (scheduler runs hourly)
    summary_hours: str = "8,20"
    # Timezone for users who haven't set one with /timezone
    default_timezone: str = "UTC"

    # No proactive reminders during these local hours ("21-8" = 21:00..08:00)
    quiet_hours: str = "21-8"
    # "Waiting for" items without an explicit deadline are due after this many hours
    waiting_default_hours: int = 48
    # Max reminder cards per user per hourly run (the rest follow next hour)
    max_reminders_per_run: int = 5

    invite_ttl_hours: int = 72
    oauth_link_ttl_minutes: int = 15
    # How far back to look for a newly linked account's first run
    initial_lookback_hours: int = 12
    # How many users are processed in parallel during a scheduled run
    run_concurrency: int = 5
    # Stop *starting* users/accounts after this many seconds, so /run finishes inside the
    # Cloud Run request timeout (300s) instead of being killed half-way through an account.
    run_soft_deadline_seconds: int = 240

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @property
    def admin_telegram_id_int(self) -> int | None:
        value = self.admin_telegram_id.strip()
        return int(value) if value.lstrip("-").isdigit() else None


settings = Settings()
