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
    # Голос: Haiku дважды ловился на согласовании («ты не бываю», «остаться одна
    # с этим») — мандат Кая «коуч должен быть грамотным» → Sonnet (сильнее в русской
    # морфологии, ~$0.01/ход — приемлемо).
    brain_respond_model: str = "claude-sonnet-5"

    # ── Каскад мозга — провайдер-агностичные аналоги (критичный рефактор 04.07) ──
    # diagnose/respond идут по фиксированному списку сверху вниз, переходя к
    # следующему слою при отказе (см. brain_cascade.py). Anthropic-alt переживает
    # деприкейт/400 конкретной модели (кейс «temperature 400» 03.07) — тот же
    # провайдер, другая модель. Gemini — другой провайдер, под ЕДИНЫМ контрактом
    # (build_diagnose_prompt/build_response_prompt), больше не слабый SESSION_ARC.
    brain_diagnose_model_alt: str = "claude-sonnet-5"
    brain_respond_model_alt: str = "claude-haiku-4-5"
    # OpenAI/Groq/Mistral — «слоты»: тихо пропускаются, пока не задан свой ключ
    # (активация — самим ключом в env, SDK импортируется лениво). Groq/Mistral —
    # ОДИН слот в каскаде: Groq первым, если оба ключа заданы.
    openai_api_key: str = ""
    brain_openai_model: str = "gpt-4o-mini"
    groq_api_key: str = ""
    brain_groq_model: str = "llama-3.3-70b-versatile"
    mistral_api_key: str = ""
    brain_mistral_model: str = "mistral-large-latest"
    # Тест неубиваемости воронки (мандат Кая): форс-выключить слои каскада, напр.
    # BRAIN_DISABLE=anthropic,gemini — прогнать «упали все верхние» и убедиться,
    # что дальше по списку (openai/groq/mistral/static) встреча не рвётся.
    # Имена слоёв: anthropic|gemini|openai|groq|mistral. static выключить нельзя —
    # это последний рубеж (без сети, никогда не падает).
    brain_disable: str = ""

    # ── Голос Алёны в диалоге (Волна 1: H1/H3, присутствие) ───────────────────
    # Ключевые ходы встречи приходят ГОЛОСОВЫМИ: диагноз мозга решает medium=voice
    # (эмоц. пик / сдвиг), оффер Клуба — всегда голосом. TTS = HeyGen v3
    # /voices/speech (голос Digital Twin Алёны, ~бесплатно, ключ уже в Render).
    # Крэш-сейф: любой сбой TTS → тот же ответ текстом, ход не теряется.
    voice_replies_enabled: bool = True
    alena_voice_id: str = "05f5549c38234b74a65c46a0c8937b5e"  # голос твина A «Алёна»
    # Темп речи Алёны (мандат Кая 04.07: «чуть ускорить, 1.1 — хорошо»).
    # Применяется ffmpeg'ом при перекоде в OGG/Opus; 1.0 = без ускорения.
    voice_tempo: float = 1.1

    # ── Дожим после оффера (Волна 1: H6/H7) ──────────────────────────────────
    # Не купила после закрытия встречи → серия из 3 касаний: ~45 мин (голосовое),
    # ~24 ч (жизнь Клуба), ~72 ч (мягкий дедлайн в тоне бренда). Одна серия на
    # человека, купившим не шлём, при активной встрече касание пропускается.
    followup_enabled: bool = True
    followup_tick_min: int = 10
    followup_delays_min: str = "45,1440,4320"  # этапы, минуты от закрытия встречи

    # ── HeyGen кредит-монитор (жёсткая фиксация: Кай узнаёт о кредитах заранее) ──
    # Бот периодически смотрит баланс HeyGen и ПИШЕТ Каю в Telegram, когда кредиты
    # на исходе (живые кружки коуча тратят кредиты; голос — бесплатный). Плюс
    # команда /credits — баланс по запросу. Мониторинг включается, когда задан
    # HEYGEN_API_KEY в env (иначе тихо спит). См. docs/hermes/credit-alerts-SPEC.md.
    heygen_api_key: str = ""
    credit_warn: int = 80      # ≤ этого (≈10 кружков) — предупреждение Каю
    credit_urgent: int = 24    # ≤ этого (≈3 кружка) — срочный алерт
    credit_check_hours: int = 6   # как часто джоб проверяет баланс

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
