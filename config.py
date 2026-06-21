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

    # Fail-closed by default: webhooks without a configured signing secret are
    # rejected. Set true ONLY for local dev to accept unsigned webhook calls.
    webhook_dev_allow_unsigned: bool = False

    # Gemini (Nano Banana) — генерация портрета Тени + разбора в тесте архетипов.
    gemini_key: str = ""

    webhook_base_url: str = ""
    webhook_path: str = "/tg-webhook"
    port: int = 8080

    database_url: str = "sqlite+aiosqlite:///./kydaidy.db"

    manifest_7_price: int = 1990
    manifest_club_price: int = 990
    manifest_plus_price: int = 4990
    one_on_one_price: int = 7000

    # Публичный календарь Алёны для записи на 1:1 (Calendly). Окна видны на странице;
    # тему запроса префиллим через ?a1=... (первый кастомный вопрос формы).
    calendly_1on1_url: str = ""

    # Closed TG channel IDs (numeric, like -1001234567890). Bot must be admin
    # with "Invite Users via Link" permission. Empty => fallback text without link.
    manifest_7_channel_id: int = 0
    manifest_club_channel_id: int = 0
    manifest_plus_channel_id: int = 0

    # ── Контент-конвейер (курирование Алёной) ────────────────────────────────
    # Numeric Telegram id Алёны-куратора. Бот не может писать по @username —
    # нужен числовой id. Узнать: Алёна шлёт боту /myid, число → сюда в env.
    # 0 => утренняя рассылка выключена; ручной /curate доступен админу (tg_admin_id).
    curator_id: int = 0
    curator_username: str = "@al.lazovsky"
    # Час утренней рассылки батча куратору (по curator_tz) и шаг дрипа публикации.
    curator_push_hour: int = 9
    curator_tz: str = "Europe/Moscow"
    curator_publish_every_min: int = 45  # как часто сливать одобренное в TG-канал


settings = Settings()
