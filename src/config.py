from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    project_id: str
    google_client_id: str = ""
    google_client_secret: str = ""
    deepseek_api_key: str = ""
    deepseek_api_url: str = "https://opencode.ai/zen/go/v1/chat/completions"
    telegram_bot_token: str = ""

    region: str = "us-central1"
    signature: str = "\n\n— Lola, Vladimir's AI Assistant"

    token_encryption_key: str = ""
    telegram_webhook_secret: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
