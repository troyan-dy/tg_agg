from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    bot_token: str
    channel_id: str
    admin_id: int

    # DeepSeek (OpenAI-compatible API)
    deepseek_api_key: str
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # Storage (PostgreSQL by default)
    database_url: str = "postgresql+asyncpg://tg:tg@localhost:5432/tg_agg"

    # Logging
    log_level: str = "INFO"

    # Scheduler: hours of the day (server TIMEZONE) to run the pipeline
    timezone: str = "UTC"
    run_hours: str = "9,13,18"
    run_on_startup: bool = False

    # Pipeline tuning
    max_candidates: int = 20  # newest unseen entries shown to DeepSeek
    post_language: str = "русском"

    @property
    def run_hours_list(self) -> list[int]:
        return [int(h.strip()) for h in self.run_hours.split(",") if h.strip()]


settings = Settings()  # type: ignore[call-arg]  # values come from env/.env
