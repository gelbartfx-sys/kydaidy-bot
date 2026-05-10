"""Конфигурация бота из переменных окружения."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    tg_bot_token: str
    tg_channel_id: str = "@kydaidy"
    tg_admin_id: int

    tribute_api_key: str = ""
    tribute_webhook_secret: str = ""
    tally_webhook_secret: str = ""

    webhook_base_url: str = ""
    webhook_path: str = "/tg-webhook"
    port: int = 8080

    database_url: str = "sqlite+aiosqlite:///./kydaidy.db"

    manifest_7_price: int = 1990
    manifest_club_price: int = 990
    manifest_plus_price: int = 4990
    one_on_one_price: int = 7000


settings = Settings()
