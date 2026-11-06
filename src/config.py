from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    project_id: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    # Shared (server) LLM: the FIRST provider with a key set is used, in this order:
    # Gemini -> DeepSeek -> OpenAI. Users can bring their own key with /setkey.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash-lite"
    deepseek_api_key: str = ""
    deepseek_api_url: str = "https://opencode.ai/zen/go/v1/chat/completions"
    deepseek_model: str = "deepseek-v4-flash"
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
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

    # ---- Per-user limits when using the shared key (own key = no limits) ----
    # Usage window: reset by each scheduled run (and after this many hours at the latest)
    usage_window_hours: int = 12
    # LLM tokens per window. ~100 emails with drafts + chat + summary need ~60-90k;
    # the rest is for /rewrite, /chat, /raw. When used up, drafts and these commands are
    # paused until the next run. 0 = unlimited.
    user_token_limit: int = 150_000
    # Emails analysed per window (per scheduled run)
    user_email_limit: int = 100
    # Admins use the shared key without limits unless this is true
    limit_admins: bool = False

    # ---- Run-time safety (Cloud Run request timeout is 300s) ----
    # Hard cap on emails per user per run, also for own-key users and admins
    max_emails_per_run: int = 200
    email_batch_size: int = 20  # emails per LLM call
    max_chat_spaces: int = 10  # most recently active spaces analysed per account per run
    # Stop starting new LLM work after this many seconds; the rest is reported as skipped
    run_time_budget_seconds: int = 240

    invite_ttl_hours: int = 72
    oauth_link_ttl_minutes: int = 15
    # How far back to look for a newly linked account's first run
    initial_lookback_hours: int = 12
    # How many users are processed in parallel during a scheduled run
    run_concurrency: int = 3

    # env_ignore_empty: Cloud Build passes unset substitutions as "" -> use the default
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore", "env_ignore_empty": True}

    @property
    def admin_telegram_id_int(self) -> int | None:
        value = self.admin_telegram_id.strip()
        return int(value) if value.lstrip("-").isdigit() else None


settings = Settings()
