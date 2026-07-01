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

    # ── AI-Алёна «мозг v2» (Фаза 1 ядра, 2-проход диагноз→ответ) ──────────────
    # OFF by default: живой поток _talk() идёт старым путём v1, байт-в-байт как
    # сейчас, пока флаг не включён. Включается только когда ядро выверено.
    brain_v2_enabled: bool = False
    # Мозг Гермеса на Claude (Gemini pro упирался в квоту 429). Ключ —
    # ANTHROPIC_API_KEY в env (SDK читает сам). Проход-ДИАГНОЗ (аналитик):
    # Opus 4.8 + adaptive thinking на КОМПАКТНОМ входе. Проход-ОТВЕТ (голос
    # Алёны): Haiku 4.5. См. docs/hermes/ai-coach-architecture.md.
    brain_diagnose_model: str = "claude-opus-4-8"
    brain_respond_model: str = "claude-haiku-4-5"

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
    calendly_1on1_url: str = "https://calendly.com/al-lazovsky/30min"

    # Closed TG channel IDs (numeric, like -1001234567890). Bot must be admin
    # with "Invite Users via Link" permission. Empty => fallback text without link.
    manifest_7_channel_id: int = 0
    manifest_club_channel_id: int = -1003798652811  # Клуб «Манифест» (closed channel)
    manifest_plus_channel_id: int = 0

    # ── Контент-конвейер (курирование Алёной) ────────────────────────────────
    # Numeric Telegram id Алёны-куратора. Бот не может писать по @username —
    # нужен числовой id. Узнать: Алёна шлёт боту /myid, число → сюда в env.
    # 0 => утренняя рассылка выключена; ручной /curate доступен админу (tg_admin_id).
    curator_id: int = 0
    curator_username: str = "@al_lazovsky"
    # Час утренней рассылки батча куратору (по curator_tz) и шаг дрипа публикации.
    curator_push_hour: int = 9
    curator_tz: str = "Europe/Moscow"
    curator_publish_every_min: int = 45  # как часто сливать одобренное в TG-канал

    # ── Hermes-руки: реактивация застрявших лидов ────────────────────────────
    # OFF by default. Включается только когда Кай явно захочет (env GROWTH_AGENT_ENABLED=1).
    # Режим всегда «ревью»: бот не шлёт юзерам сам — генерит персональный нудж,
    # отправляет Каю (tg_admin_id) на одобрение кнопкой, по ✅ — бот шлёт юзеру.
    growth_agent_enabled: bool = False
    growth_daily_limit: int = 5        # сколько черновиков готовить за один прогон
    growth_cooldown_days: int = 21     # не трогать одного юзера чаще, чем раз в N дней
    growth_tick_hours: int = 24        # период джоба реактивации

    # ── Hermes #1: мягкий оффер Клуба на «затихшей» встрече ───────────────────
    # Активная AI-встреча, где Алёна задала вопрос, а человек молчит N минут
    # (замолчал на пике) → ОДИН тёплый нудж с дверью в Клуб, чтобы оффер не
    # терялся. Встреча остаётся открытой — можно ответить и продолжить.
    # Один нудж на встречу. Выключить целиком: STALE_NUDGE_ENABLED=0.
    stale_nudge_enabled: bool = True
    stale_nudge_minutes: int = 20      # сколько человек молчит до нуджа
    stale_nudge_tick_min: int = 5      # как часто джоб проверяет затихшие встречи


settings = Settings()
