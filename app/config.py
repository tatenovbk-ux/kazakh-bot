from pydantic import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str
    database_url: str = "sqlite:///./kazakh_bot.db"
    secret_key: str
    admin_token: str
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    daily_task_limit_default: int = 15
    google_tts_api_key: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
