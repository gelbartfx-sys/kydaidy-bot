"""База данных бота.

Два бэкенда, выбор по env:
  • Cloudflare D1 — переживает деплои Render (free tier стирает локальный диск).
    Транспорт 1: Pages Function-прокси (D1_PROXY_URL + D1_PROXY_SECRET) —
      https://kydaidy.com/api/d1, не требует CF API-токена.
    Транспорт 2: CF REST API (CF_ACCOUNT_ID + CF_D1_DATABASE_ID + CF_API_TOKEN).
    Оба принимают {sql, params} и отвечают одинаковым JSON.
  • Локальный SQLite (aiosqlite) — fallback для разработки и если D1 не настроен.

D1 — это SQLite под капотом, поэтому весь SQL ниже одинаков для обоих путей.
Схема для D1 применяется вне кода (wrangler d1 execute --file=bot/schema.sql).
"""

from __future__ import annotations

import asyncio
import os
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime

import aiohttp
import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = "kydaidy.db"

CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_D1_DATABASE_ID = os.getenv("CF_D1_DATABASE_ID")
CF_API_TOKEN = os.getenv("CF_API_TOKEN")
D1_PROXY_URL = os.getenv("D1_PROXY_URL")  # напр. https://kydaidy.com/api/d1
D1_PROXY_SECRET = os.getenv("D1_PROXY_SECRET")
USE_D1 = bool(D1_PROXY_URL and D1_PROXY_SECRET) or bool(
    CF_ACCOUNT_ID and CF_D1_DATABASE_ID and CF_API_TOKEN
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    povorot INTEGER,
    shadow_dist TEXT,
    quiz_completed_at TIMESTAMP,
    nurture_day INTEGER DEFAULT 0,
    nurture_active INTEGER DEFAULT 0,
    last_nurture_at TIMESTAMP,
    last_ai_request TEXT,
    dossier TEXT,
    dossier_at TIMESTAMP,
    client_model TEXT,
    lead_heat INTEGER,
    lead_open INTEGER,
    lead_resist INTEGER,
    lead_value INTEGER,
    lead_track TEXT,
    circle_credits_spent INTEGER DEFAULT 0,
    lead_updated_at TIMESTAMP,
    ref_seller TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER,
    product_code TEXT,
    amount INTEGER,
    tribute_payment_id TEXT,
    ref_seller TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tg_id) REFERENCES users(tg_id)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER,
    product_code TEXT,
    active INTEGER DEFAULT 1,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    next_charge_at TIMESTAMP,
    cancelled_at TIMESTAMP,
    FOREIGN KEY (tg_id) REFERENCES users(tg_id)
);

CREATE TABLE IF NOT EXISTS messages_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER,
    message_type TEXT,
    content TEXT,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tribute_posts (
    product_code TEXT PRIMARY KEY,
    src_chat_id INTEGER NOT NULL,
    src_message_id INTEGER NOT NULL,
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shadow_generations (
    tg_id INTEGER PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS manifest7_guide (
    tg_id INTEGER,
    practice INTEGER,
    step INTEGER DEFAULT 0,
    completed_at TIMESTAMP,
    updated_at TIMESTAMP,
    PRIMARY KEY (tg_id, practice)
);

CREATE TABLE IF NOT EXISTS ai_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER,
    status TEXT DEFAULT 'active',
    turns INTEGER DEFAULT 0,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP,
    nudged_at TIMESTAMP,
    paid_touch_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ai_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    tg_id INTEGER,
    role TEXT,
    content TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bot_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS funnel_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER,
    event TEXT,
    meta TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER,
    stage INTEGER,
    due_at TIMESTAMP,
    sent_at TIMESTAMP,
    status TEXT DEFAULT 'due'
);

CREATE TABLE IF NOT EXISTS atm_quiz (
    tg_id INTEGER PRIMARY KEY,
    answers TEXT,
    scores TEXT,
    weak TEXT,
    pair_src INTEGER,
    nextday_sent INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS couples (
    couple_id INTEGER PRIMARY KEY,
    partner_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bank_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    couple_id INTEGER,
    kind TEXT,
    sign INTEGER,
    day_key TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(couple_id, kind, day_key)
);
"""


# ── D1 REST transport ────────────────────────────────────────────────────────

def _d1_param(value):
    """D1 REST принимает только JSON-скаляры. datetime → строка, как у sqlite3."""
    if isinstance(value, datetime):
        return str(value)
    return value


async def _d1_query(sql: str, params: tuple):
    if D1_PROXY_URL and D1_PROXY_SECRET:
        url, token = D1_PROXY_URL, D1_PROXY_SECRET
    else:
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
            f"/d1/database/{CF_D1_DATABASE_ID}/query"
        )
        token = CF_API_TOKEN
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"sql": sql, "params": [_d1_param(p) for p in params]}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as r:
            data = await r.json()
    if not data.get("success"):
        raise RuntimeError(f"D1 query failed: {data.get('errors')}")
    return data["result"][0]["results"]


# ── SQLite transport ─────────────────────────────────────────────────────────

@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


# ── Единый исполнитель: один SQL, выбор бэкенда внутри ───────────────────────

async def _exec(sql: str, params: tuple = (), fetch: str = "none"):
    """fetch: 'none' | 'one' | 'all'. Всегда возвращает dict / list[dict] / None."""
    if USE_D1:
        rows = await _d1_query(sql, params)
        if fetch == "one":
            return rows[0] if rows else None
        if fetch == "all":
            return rows
        return None
    async with get_db() as db:
        cursor = await db.execute(sql, params)
        if fetch == "one":
            row = await cursor.fetchone()
            await db.commit()
            return dict(row) if row else None
        if fetch == "all":
            rows = await cursor.fetchall()
            await db.commit()
            return [dict(r) for r in rows]
        await db.commit()
        return None


# Идемпотентные DDL новых таблиц — докатываются в D1 прямо из рантайма
# (креды D1 уже есть), чтобы не требовать ручного wrangler d1 execute.
_RUNTIME_MIGRATIONS = (
    """CREATE TABLE IF NOT EXISTS ai_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER,
        status TEXT DEFAULT 'active',
        turns INTEGER DEFAULT 0,
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        closed_at TIMESTAMP,
        nudged_at TIMESTAMP,
        paid_touch_count INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS ai_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        tg_id INTEGER,
        role TEXT,
        content TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    # Колонка для нативного моста AI→1:1: вскрытый запрос префиллим в запись.
    # ALTER упадёт «duplicate column» после первого прогона — это ловится
    # try/except в init_db (деградирует только эта фича).
    "ALTER TABLE users ADD COLUMN last_ai_request TEXT",
    # Атрибуция источника трафика (deep-link /start <tag>): first-touch.
    # Цель — видеть, какой канал реально гонит квизы Тени. Те же try/except.
    "ALTER TABLE users ADD COLUMN source TEXT",
    "ALTER TABLE users ADD COLUMN source_at TIMESTAMP",
    # ── Контент-конвейер (курирование Алёной + автопостинг) ──────────────────
    """CREATE TABLE IF NOT EXISTS content_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch TEXT,
        ext_id TEXT,
        channel TEXT,
        fmt TEXT,
        hypothesis TEXT,
        draft TEXT,
        final TEXT,
        visual TEXT,
        cta TEXT,
        status TEXT DEFAULT 'pending',
        position INTEGER,
        decided_at TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS curator_state (
        curator_id INTEGER PRIMARY KEY,
        current_item INTEGER,
        awaiting TEXT,
        last_pushed_date TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS post_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER,
        channel TEXT,
        text TEXT,
        status TEXT DEFAULT 'queued',
        queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        posted_at TIMESTAMP
    )""",
    # ── Hermes-руки: реактивация застрявших лидов (режим ревью) ──────────────
    # Когда юзера в последний раз трогали реактивацией — антиспам-кулдаун.
    "ALTER TABLE users ADD COLUMN reactivated_at TIMESTAMP",
    # Черновики нуджей: бот генерит → Кай одобряет кнопкой → бот шлёт юзеру.
    """CREATE TABLE IF NOT EXISTS growth_drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER,
        segment TEXT,
        draft TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        decided_at TIMESTAMP
    )""",
    # ── Досье участницы: живой портрет (почему пришла, что болит, запрос, заметки).
    # AI-Алёна дописывает после каждой встречи; подгружается в её контекст и в вид 1:1.
    "ALTER TABLE users ADD COLUMN dossier TEXT",
    "ALTER TABLE users ADD COLUMN dossier_at TIMESTAMP",
    # ── Hermes #1: когда на «затихшей» встрече уже слали мягкий оффер Клуба.
    # NULL => ещё не слали. Существующий прод-D1 уже имеет ai_sessions без неё —
    # ALTER докатывает; «duplicate column» после первого прогона ловит try/except.
    "ALTER TABLE ai_sessions ADD COLUMN nudged_at TIMESTAMP",
    # Re-engage (Кай 09.07): мягкое «я тут, жду тебя» ДО оффер-нуджа, если молчит.
    # NULL => ещё не возвращали. Отдельная метка, чтобы re-engage и оффер-нудж не мешались.
    "ALTER TABLE ai_sessions ADD COLUMN reengaged_at TIMESTAMP",
    # ── Волна 3 (мандат Кая 06.07): бюджет «12 платных касаний НА ВСТРЕЧУ»
    # (аудио+кружки). Счётчик пер-встреча вместо лифтайм-квоты — член Клуба
    # (2 встречи/мес) больше не глохнет навсегда в текст. ALTER «duplicate column»
    # после первого прогона ловит try/except в init_db — деградирует только фича.
    "ALTER TABLE ai_sessions ADD COLUMN paid_touch_count INTEGER DEFAULT 0",
    # ── Скоринг лида (Фаза 1 AI-Алёны): «мозг» без изменения поведения.
    # Алёна каждый ход скрыто оценивает собеседника (маркер [[SCORE ...]]),
    # сигналы копятся здесь; чистая политика треков считается в lead_policy.py.
    # 4 сигнала (0–3), текущий трек, и леджер потраченных HeyGen-кредитов на
    # персональные кружки (Фаза 2). ALTER «duplicate column» после первого
    # прогона ловит try/except в init_db — деградирует только эта фича.
    "ALTER TABLE users ADD COLUMN lead_heat INTEGER",
    "ALTER TABLE users ADD COLUMN lead_open INTEGER",
    "ALTER TABLE users ADD COLUMN lead_resist INTEGER",
    "ALTER TABLE users ADD COLUMN lead_value INTEGER",
    "ALTER TABLE users ADD COLUMN lead_track TEXT",
    "ALTER TABLE users ADD COLUMN circle_credits_spent INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN lead_updated_at TIMESTAMP",
    # ── AI-Алёна «мозг v2» (Фаза 1 ядра): структурная модель клиентки как
    # JSON-строка. Диагноз-проход обновляет её каждый ход (pattern, facade_lie,
    # true_request_hypothesis, defenses[], objections[], readiness, given[],
    # method_phase, track). ALTER «duplicate column» после первого прогона ловит
    # try/except в init_db — деградирует только эта фича, не весь бот.
    "ALTER TABLE users ADD COLUMN client_model TEXT",
    # ── SELLer-атрибуция: реф-продавец на юзере (first-touch) + снапшот в покупке.
    # ?start=ref_<sellerId> → продажа несёт продавца для сверки %. Крэш-сейф ALTER.
    "ALTER TABLE users ADD COLUMN ref_seller TEXT",
    "ALTER TABLE purchases ADD COLUMN ref_seller TEXT",
    # ── Служебное key-value бота (напр. дата последнего кредит-алерта HeyGen,
    # чтобы не спамить Каю чаще раза в день на уровень). Крэш-сейф как остальные.
    """CREATE TABLE IF NOT EXISTS bot_meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )""",
    # ── Волна 1 (H12): события воронки — гранулярное «где рвётся».
    # Пишутся из хендлеров (portrait/kruzhok/session/voice/offer/дожимы);
    # /sources показывает сводку. Любая ошибка записи глотается (log_event).
    """CREATE TABLE IF NOT EXISTS funnel_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER,
        event TEXT,
        meta TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    # ── Волна 1 (H6/H7): дожим после оффера — серия из 3 отложенных касаний.
    # Одна серия на человека (стадии 1..3), due_at считается при закрытии встречи.
    """CREATE TABLE IF NOT EXISTS followups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER,
        stage INTEGER,
        due_at TIMESTAMP,
        sent_at TIMESTAMP,
        status TEXT DEFAULT 'due'
    )""",
    # ── Подписочный 1:1 (мандат Кая 04.07): продаём личные встречи ПОДПИСКОЙ
    # (тариф 1 встреча/мес = sZXq, 3 встречи/мес = sZXr), а не разовым продуктом.
    # Счётчик встреч гейтит запись: невозможно записаться сверх тарифа. Оплата/
    # продление Tribute (newSubscription/renewedSubscription) СБРАСЫВАЕТ счётчик
    # на полный тариф (новый период). sessions_left уменьшается при записи в боте.
    """CREATE TABLE IF NOT EXISTS oneonone_subs (
        tg_id INTEGER PRIMARY KEY,
        tariff INTEGER,
        sessions_left INTEGER,
        period_start TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    # Брони 1:1 для сверки с Calendly (polling, 05.07). При выдаче ссылки создаём
    # pending-запись (встреча уже списана — жёсткий кап). Polling матчит реальную
    # бронь по utm_content=tg_id → 'booked'; отмену → возврат встречи ('canceled');
    # pending дольше суток без брони → возврат ('expired_restored').
    """CREATE TABLE IF NOT EXISTS oneonone_bookings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER,
        issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        event_uri TEXT,
        status TEXT DEFAULT 'pending',
        matched_at TIMESTAMP
    )""",
    # ── Воронка v2 (планёрка 10.07): стадия покупательской готовности лида —
    # ОТДЕЛЬНО от lead_track (тот = бюджет кружков, НЕ путать). cold/warm_entry/
    # warm_qualified/hot. Гейт продажи читает её (fail-open: NULL → разрешить).
    # Крэш-сейф ALTER как остальные.
    "ALTER TABLE users ADD COLUMN purchase_stage TEXT",
    "ALTER TABLE users ADD COLUMN purchase_stage_at TIMESTAMP",
    # ── Пивот E1 (T1): тест «Атмосфера дома» — 12 вопросов, 4 опоры.
    # PRIMARY KEY tg_id: повторное прохождение перезаписывает (retake разрешён).
    # pair_src = tg_id инициатора пары (deeplink pair_<uid>) — связь пары.
    # nextday_sent — флаг next-day чека (~20 ч), см. quiz_atmosfera.run_atm_nextday_tick.
    """CREATE TABLE IF NOT EXISTS atm_quiz (
        tg_id INTEGER PRIMARY KEY,
        answers TEXT,
        scores TEXT,
        weak TEXT,
        pair_src INTEGER,
        nextday_sent INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    # ── Сквозной Банк 5:1 (Шаг 1, спина кольца): цифра принадлежит ПАРЕ ────────
    # couples: couple_id = pair_src инициатора (для соло = собственный tg_id),
    # partner_id заполняется при образовании пары. bank_ledger: журнал поворотов,
    # UNIQUE(couple_id,kind,day_key) даёт идемпотентность bank_add. Крэш-сейф
    # докатка как остальные — упавшая миграция деградирует фичу, не роняет бота.
    """CREATE TABLE IF NOT EXISTS couples (
        couple_id INTEGER PRIMARY KEY,
        partner_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS bank_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        couple_id INTEGER,
        kind TEXT,
        sign INTEGER,
        day_key TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(couple_id, kind, day_key)
    )""",
)


async def init_db():
    # Базовая схема в D1 применяется вне кода (wrangler), но новые таблицы
    # докатываем идемпотентно отсюда — для обоих бэкендов.
    if USE_D1:
        # Крэш-сейф: ошибка миграции деградирует ТОЛЬКО новую фичу, не весь бот.
        for ddl in _RUNTIME_MIGRATIONS:
            try:
                await _exec(ddl)
            except Exception:
                logger.warning("D1 runtime migration failed (continuing)", exc_info=True)
        return
    async with get_db() as db:
        await db.executescript(SCHEMA)
        await db.commit()


# ── Пользователи / воронка ───────────────────────────────────────────────────

async def upsert_user(tg_id: int, username: str | None, first_name: str | None, povorot: int | None = None):
    await _exec(
        """
        INSERT INTO users (tg_id, username, first_name, povorot, quiz_completed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(tg_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            povorot = COALESCE(excluded.povorot, users.povorot),
            quiz_completed_at = COALESCE(excluded.quiz_completed_at, users.quiz_completed_at)
        """,
        (tg_id, username, first_name, povorot, datetime.now() if povorot else None),
    )


async def get_user(tg_id: int):
    return await _exec("SELECT * FROM users WHERE tg_id = ?", (tg_id,), fetch="one")


async def ai_set_last_request(tg_id: int, request: str | None):
    """Сохранить последний вскрытый на AI-встрече запрос (для префилла записи 1:1).

    Крэш-сейф: если колонки ещё нет (миграция не докатилась) — деградируем
    тихо, не ломая закрытие встречи.
    """
    if not request:
        return
    try:
        await _exec(
            "UPDATE users SET last_ai_request = ? WHERE tg_id = ?",
            (request, tg_id),
        )
    except Exception:
        logger.warning("ai_set_last_request failed (continuing)", exc_info=True)


async def save_dossier(tg_id: int, dossier: str | None):
    """Перезаписать живой портрет участницы (AI-Алёна отдаёт обновлённый целиком).

    Крэш-сейф: нет колонки (миграция не докатилась) — деградируем тихо."""
    if not dossier:
        return
    try:
        await _exec(
            "UPDATE users SET dossier = ?, dossier_at = CURRENT_TIMESTAMP WHERE tg_id = ?",
            (dossier.strip()[:2000], tg_id),
        )
    except Exception:
        logger.warning("save_dossier failed (continuing)", exc_info=True)


# ── AI-Алёна «мозг v2» (Фаза 1 ядра): структурная модель клиентки (JSON) ──────
# Живёт в users.client_model как JSON-строка. Диагноз-проход обновляет её каждый
# ход. Всё крэш-сейф: любая ошибка (нет колонки / битый JSON) деградирует ТОЛЬКО
# эту фичу, встреча не падает (вызывающий код фолбэчит на v1-путь).

async def save_client_model(tg_id: int, model_json: str | None):
    """Перезаписать структурную модель клиентки (JSON-строка). Крэш-сейф."""
    if not model_json:
        return
    try:
        await _exec(
            "UPDATE users SET client_model = ? WHERE tg_id = ?",
            (str(model_json)[:8000], tg_id),
        )
    except Exception:
        logger.warning("save_client_model failed (continuing)", exc_info=True)


async def get_client_model(tg_id: int) -> dict | None:
    """Прочитать структурную модель клиентки → dict | None.

    None при пусто/отсутствии колонки/битом JSON. Крэш-сейф."""
    try:
        row = await _exec(
            "SELECT client_model FROM users WHERE tg_id = ?", (tg_id,), fetch="one")
    except Exception:
        logger.warning("get_client_model read failed (continuing)", exc_info=True)
        return None
    raw = (row or {}).get("client_model")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# ── Служебное key-value (bot_meta) — напр. дата последнего кредит-алерта ──────
async def get_meta(key: str) -> str | None:
    """Значение служебного ключа bot_meta → str | None. Крэш-сейф."""
    try:
        row = await _exec("SELECT value FROM bot_meta WHERE key = ?", (key,), fetch="one")
    except Exception:
        logger.warning("get_meta failed (continuing)", exc_info=True)
        return None
    return (row or {}).get("value")


async def set_meta(key: str, value: str) -> None:
    """Записать служебный ключ bot_meta (upsert). Крэш-сейф."""
    try:
        await _exec(
            "INSERT INTO bot_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))
    except Exception:
        logger.warning("set_meta failed (continuing)", exc_info=True)


# ── Подписочный 1:1: счётчик встреч в текущем периоде ─────────────────────────
# Крэш-сейф: ошибка (миграция не докатилась) деградирует ТОЛЬКО фичу записи 1:1,
# не роняя воронку/оплаты.
async def set_oneonone(tg_id: int, tariff: int, sessions_left: int) -> None:
    """Установить/сбросить счётчик 1:1 (при оплате и продлении — полный тариф).
    upsert: обновляет tariff+sessions_left и стартует новый период."""
    try:
        await _exec(
            "INSERT INTO oneonone_subs (tg_id, tariff, sessions_left, "
            "period_start, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP) ON CONFLICT(tg_id) DO UPDATE SET "
            "tariff = excluded.tariff, sessions_left = excluded.sessions_left, "
            "period_start = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP",
            (tg_id, tariff, sessions_left))
    except Exception:
        logger.warning("set_oneonone failed (continuing)", exc_info=True)


async def get_oneonone(tg_id: int) -> dict | None:
    """Запись счётчика 1:1 → {tariff, sessions_left, period_start} | None."""
    try:
        return await _exec(
            "SELECT tariff, sessions_left, period_start FROM oneonone_subs "
            "WHERE tg_id = ?", (tg_id,), fetch="one")
    except Exception:
        logger.warning("get_oneonone failed (continuing)", exc_info=True)
        return None


async def inc_oneonone(tg_id: int) -> bool:
    """Вернуть 1 встречу (не выше тарифа). Для авто-возврата, когда клиент отменил
    бронь или не записался. Крэш-сейф. True — возвращено."""
    try:
        row = await get_oneonone(tg_id)
        if not row:
            return False
        tariff = int(row.get("tariff") or 1)
        if int(row.get("sessions_left") or 0) >= tariff:
            return False  # уже полный — возвращать нечего
        await _exec(
            "UPDATE oneonone_subs SET sessions_left = sessions_left + 1, "
            "updated_at = CURRENT_TIMESTAMP WHERE tg_id = ? AND sessions_left < tariff",
            (tg_id,))
        return True
    except Exception:
        logger.warning("inc_oneonone failed (continuing)", exc_info=True)
        return False


# ── Брони 1:1 (сверка с Calendly) ────────────────────────────────────────────
async def booking_issue(tg_id: int) -> int | None:
    """Создать pending-бронь при выдаче ссылки. Возвращает id (для utm_campaign)."""
    try:
        await _exec(
            "INSERT INTO oneonone_bookings (tg_id, status) VALUES (?, 'pending')",
            (tg_id,))
        row = await _exec(
            "SELECT id FROM oneonone_bookings WHERE tg_id = ? ORDER BY id DESC LIMIT 1",
            (tg_id,), fetch="one")
        return int(row["id"]) if row and row.get("id") is not None else None
    except Exception:
        logger.warning("booking_issue failed (continuing)", exc_info=True)
        return None


async def booking_pending_list():
    """Все pending-брони (для матчинга с Calendly и проверки протухания)."""
    try:
        return await _exec(
            "SELECT * FROM oneonone_bookings WHERE status = 'pending' "
            "ORDER BY id ASC", fetch="all") or []
    except Exception:
        return []


async def booking_get(booking_id: int):
    try:
        return await _exec("SELECT * FROM oneonone_bookings WHERE id = ?",
                           (booking_id,), fetch="one")
    except Exception:
        return None


async def oneonone_nobook_candidates(days: int = 3):
    """I4: оплатил 1:1, но ни разу не открыл запись — счётчик нетронут
    (sessions_left = tariff), подписка активна, период старше N дней. Крэш-сейф."""
    try:
        return await _exec(
            "SELECT o.tg_id AS tg_id, o.tariff AS tariff FROM oneonone_subs o "
            "WHERE o.sessions_left = o.tariff "
            "AND datetime(o.period_start, ?) < datetime('now') "
            "AND o.tg_id IN (SELECT tg_id FROM subscriptions "
            "                WHERE product_code = 'manifest_1on1' AND active = 1)",
            (f"+{int(days)} days",), fetch="all") or []
    except Exception:
        return []


async def club_quiet_candidates(days: int = 20):
    """I5: активный член Клуба, который не был на AI-встрече N дней — напомнить,
    что встречи месяца доступны (не растерять). Крэш-сейф."""
    try:
        return await _exec(
            "SELECT tg_id FROM subscriptions "
            "WHERE product_code = 'manifest_club' AND active = 1 "
            "AND tg_id NOT IN (SELECT tg_id FROM ai_sessions "
            "                  WHERE datetime(started_at) > datetime('now', ?))",
            (f"-{int(days)} days",), fetch="all") or []
    except Exception:
        return []


async def booking_pending_expired(hours: int = 24):
    """Pending-брони старше N часов (клиент получил ссылку, но не записался) —
    их встречу возвращаем. Крэш-сейф."""
    try:
        return await _exec(
            "SELECT * FROM oneonone_bookings WHERE status = 'pending' "
            "AND datetime(issued_at, ?) < datetime('now')",
            (f"+{int(hours)} hours",), fetch="all") or []
    except Exception:
        return []


async def booking_by_event(event_uri: str):
    try:
        return await _exec("SELECT * FROM oneonone_bookings WHERE event_uri = ?",
                           (event_uri,), fetch="one")
    except Exception:
        return None


async def booking_set(booking_id: int, status: str, event_uri: str | None = None):
    """Обновить статус брони (+ event_uri при матче). Крэш-сейф."""
    try:
        if event_uri is not None:
            await _exec(
                "UPDATE oneonone_bookings SET status = ?, event_uri = ?, "
                "matched_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, event_uri, booking_id))
        else:
            await _exec(
                "UPDATE oneonone_bookings SET status = ?, "
                "matched_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, booking_id))
    except Exception:
        logger.warning("booking_set failed (continuing)", exc_info=True)


async def dec_oneonone(tg_id: int) -> bool:
    """Списать 1 встречу, ТОЛЬКО если осталось > 0.
    True — списано (можно давать ссылку на календарь); False — нечего списывать
    (сверх тарифа / нет подписки) или ошибка. WHERE sessions_left>0 в UPDATE —
    защита БД от ухода в минус даже при гонке; предчтение даёт корректный ответ."""
    try:
        row = await _exec(
            "SELECT sessions_left FROM oneonone_subs WHERE tg_id = ?",
            (tg_id,), fetch="one")
        if not row or int(row.get("sessions_left") or 0) <= 0:
            return False
        await _exec(
            "UPDATE oneonone_subs SET sessions_left = sessions_left - 1, "
            "updated_at = CURRENT_TIMESTAMP WHERE tg_id = ? AND sessions_left > 0",
            (tg_id,))
        return True
    except Exception:
        logger.warning("dec_oneonone failed (continuing)", exc_info=True)
        return False


# ── Скоринг лида (Фаза 1): сигналы собеседника + леджер кредитов ──────────────
# Всё крэш-сейф: любая ошибка (нет колонки — миграция не докатилась) деградирует
# ТОЛЬКО скоринг, не роняя встречу.

def _clamp03(v):
    """0..3 или None (None => поле не трогаем в UPDATE)."""
    if v is None:
        return None
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return max(0, min(3, v))


async def save_lead_signals(tg_id: int, heat=None, open_=None,
                            resist=None, value=None):
    """Записать сигналы лида. Клампим 0..3; None-поля НЕ перезаписываем
    (COALESCE-семантика: обновляем только переданные не-None). Крэш-сейф."""
    h, o, r, val = (_clamp03(heat), _clamp03(open_),
                    _clamp03(resist), _clamp03(value))
    if h is None and o is None and r is None and val is None:
        return
    try:
        await _exec(
            "UPDATE users SET "
            "lead_heat = COALESCE(?, lead_heat), "
            "lead_open = COALESCE(?, lead_open), "
            "lead_resist = COALESCE(?, lead_resist), "
            "lead_value = COALESCE(?, lead_value), "
            "lead_updated_at = CURRENT_TIMESTAMP "
            "WHERE tg_id = ?",
            (h, o, r, val, tg_id),
        )
    except Exception:
        logger.warning("save_lead_signals failed (continuing)", exc_info=True)


async def get_lead_signals(tg_id: int):
    """Текущие сигналы лида + circle_credits_spent + lead_track. → dict | None.
    Крэш-сейф: при отсутствии колонок вернём None."""
    try:
        return await _exec(
            "SELECT lead_heat, lead_open, lead_resist, lead_value, lead_track, "
            "circle_credits_spent, lead_updated_at FROM users WHERE tg_id = ?",
            (tg_id,), fetch="one")
    except Exception:
        logger.warning("get_lead_signals failed (continuing)", exc_info=True)
        return None


async def add_circle_credits(tg_id: int, credits: int):
    """Леджер: прибавить потраченные HeyGen-кредиты на кружки этому лиду.
    Крэш-сейф."""
    try:
        await _exec(
            "UPDATE users SET "
            "circle_credits_spent = COALESCE(circle_credits_spent, 0) + ? "
            "WHERE tg_id = ?",
            (int(credits), tg_id),
        )
    except Exception:
        logger.warning("add_circle_credits failed (continuing)", exc_info=True)


async def set_lead_track(tg_id: int, track: str):
    """Назначить текущий трек лида ('T1'/'T2'/'T3'/'T4'). Крэш-сейф."""
    try:
        await _exec(
            "UPDATE users SET lead_track = ? WHERE tg_id = ?", (track, tg_id))
    except Exception:
        logger.warning("set_lead_track failed (continuing)", exc_info=True)


async def set_purchase_stage(tg_id: int, stage: str):
    """Записать стадию покупательской готовности (cold/warm_entry/warm_qualified/
    hot). ОТДЕЛЬНО от lead_track. Крэш-сейф: сбой не должен ронять встречу/тик —
    гейт fail-open переживёт отсутствие записи."""
    try:
        await _exec(
            "UPDATE users SET purchase_stage = ?, purchase_stage_at = CURRENT_TIMESTAMP "
            "WHERE tg_id = ?", (stage, tg_id))
    except Exception:
        logger.warning("set_purchase_stage failed (continuing)", exc_info=True)


async def get_purchase_stage(tg_id: int) -> str | None:
    """Текущая стадия | None (не посчитана / ошибка / нет колонки). Крэш-сейф."""
    try:
        row = await _exec(
            "SELECT purchase_stage FROM users WHERE tg_id = ?", (tg_id,), fetch="one")
        return (row or {}).get("purchase_stage")
    except Exception:
        logger.warning("get_purchase_stage failed (continuing)", exc_info=True)
        return None


async def set_user_source(tg_id: int, source: str | None):
    """First-touch атрибуция: пишем источник только если он ещё не записан.

    Крэш-сейф: если колонок ещё нет (миграция не докатилась) — деградируем
    тихо, не ломая /start. Требует, чтобы строка users уже была (upsert_user
    вызывается раньше в обоих /start-хендлерах).
    """
    if not source:
        return
    try:
        await _exec(
            "UPDATE users SET source = ?, source_at = CURRENT_TIMESTAMP "
            "WHERE tg_id = ? AND source IS NULL",
            (source, tg_id),
        )
    except Exception:
        logger.warning("set_user_source failed (continuing)", exc_info=True)


async def set_user_ref_seller(tg_id: int, seller: str | None):
    """First-touch: пишем реф-продавца SELLer только если ещё не записан.

    Крэш-сейф, как set_user_source: если колонки нет — тихо деградируем, не
    ломая /start. Строка users создаётся upsert_user раньше в хендлере.
    """
    if not seller:
        return
    try:
        await _exec(
            "UPDATE users SET ref_seller = ? WHERE tg_id = ? AND ref_seller IS NULL",
            (seller, tg_id),
        )
    except Exception:
        logger.warning("set_user_ref_seller failed (continuing)", exc_info=True)


async def source_stats():
    """Воронка по источникам (трекинг трафика): пришли → прошли тест Тени → получили портрет → оплатили.

    FIX 01.07: раньше «квиз» мерился по povorot (legacy-флоу), которого в shadow-воронке НЕТ →
    у всего трафика было quiz=0. Теперь: test = shadow_dist задан; portrait = есть в shadow_generations;
    paid = есть подписка или покупка. Крэш-сейф."""
    try:
        return await _exec(
            "SELECT COALESCE(u.source, '(прямой/нет метки)') AS source, "
            "COUNT(*) AS users, "
            "SUM(CASE WHEN u.shadow_dist IS NOT NULL THEN 1 ELSE 0 END) AS test_passed, "
            "SUM(CASE WHEN g.tg_id IS NOT NULL THEN 1 ELSE 0 END) AS portrait, "
            "SUM(CASE WHEN s.tg_id IS NOT NULL THEN 1 ELSE 0 END) AS talked, "
            "SUM(CASE WHEN u.last_ai_request IS NOT NULL THEN 1 ELSE 0 END) AS req, "
            "SUM(CASE WHEN p.tg_id IS NOT NULL THEN 1 ELSE 0 END) AS paid "
            "FROM users u "
            "LEFT JOIN shadow_generations g ON g.tg_id = u.tg_id "
            "LEFT JOIN (SELECT DISTINCT tg_id FROM ai_sessions) s ON s.tg_id = u.tg_id "
            "LEFT JOIN (SELECT tg_id FROM subscriptions UNION SELECT tg_id FROM purchases) p "
            "  ON p.tg_id = u.tg_id "
            "GROUP BY u.source ORDER BY users DESC",
            fetch="all") or []
    except Exception:
        logger.warning("source_stats failed (continuing)", exc_info=True)
        return []


async def save_shadow_dist(tg_id: int, dist: str):
    await _exec("UPDATE users SET shadow_dist = ? WHERE tg_id = ?", (dist, tg_id))


async def has_generated_shadow(tg_id: int) -> bool:
    row = await _exec("SELECT 1 FROM shadow_generations WHERE tg_id = ?", (tg_id,), fetch="one")
    return row is not None


async def mark_shadow_generated(tg_id: int):
    await _exec("INSERT OR IGNORE INTO shadow_generations (tg_id) VALUES (?)", (tg_id,))


async def start_nurture(tg_id: int):
    await _exec(
        "UPDATE users SET nurture_active = 1, nurture_day = 0, last_nurture_at = ? WHERE tg_id = ?",
        (datetime.now(), tg_id),
    )


async def stop_nurture(tg_id: int):
    await _exec("UPDATE users SET nurture_active = 0 WHERE tg_id = ?", (tg_id,))


async def advance_nurture_day(tg_id: int, day: int):
    await _exec(
        "UPDATE users SET nurture_day = ?, last_nurture_at = ? WHERE tg_id = ?",
        (day, datetime.now(), tg_id),
    )


async def get_users_for_nurture():
    """Юзеры с активным nurture, готовые получить следующий день."""
    return await _exec(
        """
        SELECT * FROM users
        WHERE nurture_active = 1
          AND nurture_day < 7
          AND (
              last_nurture_at IS NULL
              OR datetime(last_nurture_at, '+20 hours') < datetime('now')
          )
        """,
        fetch="all",
    )


# ── Покупки / подписки ───────────────────────────────────────────────────────

async def add_purchase(tg_id: int, product_code: str, amount: int, payment_id: str):
    # ref_seller тянем из юзера в момент покупки — продажа несёт продавца (SELLer-сверка).
    await _exec(
        "INSERT INTO purchases (tg_id, product_code, amount, tribute_payment_id, ref_seller) "
        "VALUES (?, ?, ?, ?, (SELECT ref_seller FROM users WHERE tg_id = ?))",
        (tg_id, product_code, amount, payment_id, tg_id),
    )


async def add_subscription(tg_id: int, product_code: str):
    await _exec(
        "INSERT INTO subscriptions (tg_id, product_code) VALUES (?, ?)",
        (tg_id, product_code),
    )


async def get_user_purchases(tg_id: int):
    return await _exec(
        "SELECT * FROM purchases WHERE tg_id = ? ORDER BY created_at DESC",
        (tg_id,),
        fetch="all",
    )


async def get_active_subscription(tg_id: int, product_code: str):
    return await _exec(
        "SELECT * FROM subscriptions WHERE tg_id = ? AND product_code = ? "
        "AND active = 1 ORDER BY id DESC LIMIT 1",
        (tg_id, product_code), fetch="one")


async def deactivate_subscription(tg_id: int, product_code: str) -> None:
    """Отмена подписки (Tribute cancelledSubscription): active=0 + cancelled_at.
    Закрывает вечный доступ и ОЖИВЛЯЕТ сегмент реактивации club_churn
    (active=0 OR cancelled_at IS NOT NULL). Крэш-сейф. Счётчик встреч 1:1 тут
    НЕ трогаем — оплаченный период клиент дорабатывает; следующий сброс просто
    не наступит (нет продления + cron сверяет активность подписки)."""
    try:
        await _exec(
            "UPDATE subscriptions SET active = 0, cancelled_at = CURRENT_TIMESTAMP "
            "WHERE tg_id = ? AND product_code = ? AND active = 1",
            (tg_id, product_code))
    except Exception:
        logger.warning("deactivate_subscription failed (continuing)", exc_info=True)


async def reconcile_oneonone_due(days: int = 29) -> int:
    """Cron-страховка сброса счётчика 1:1 (мандат «швейцарские часы» 05.07):
    если период старше ~30 дней, а вебхук продления НЕ пришёл (потерян/таймаут),
    но подписка 1:1 ещё активна — добить sessions_left до тарифа. Вебхук —
    основной путь, cron — подстраховка, чтобы оплативший не заперся со 2-го
    месяца. Отменённые (active=0) НЕ трогаем. Возвращает число сброшенных."""
    try:
        rows = await _exec(
            "SELECT tg_id, tariff FROM oneonone_subs "
            "WHERE period_start <= datetime('now', ?)", (f"-{days} days",),
            fetch="all") or []
    except Exception:
        logger.warning("reconcile_oneonone_due select failed", exc_info=True)
        return 0
    n = 0
    for r in rows:
        tg = r.get("tg_id"); tar = int(r.get("tariff") or 1)
        try:
            if await get_active_subscription(tg, "manifest_1on1"):
                await set_oneonone(tg, tar, tar)
                n += 1
        except Exception:
            logger.warning("reconcile_oneonone row failed tg=%s", tg, exc_info=True)
    return n


async def memory_allowed(tg_id: int) -> bool:
    """ЕДИНЫЙ гейт памяти (мандат Кая 04.07, «чистый лист для не купивших»):
    досье прошлых встреч можно подавать модели ТОЛЬКО купившим — член Клуба
    «Манифест» ИЛИ есть хотя бы одна покупка (напр. подписка 1:1).
    Бесплатным/не купившим — с чистого листа (иначе модель «помнит»
    несказанное — корень галлюцинаций, аудит 04.07).
    Крэш-сейф: любая ошибка проверки = память ЗАПРЕЩЕНА (безопаснее, чем
    рискнуть галлюцинацией)."""
    try:
        if await get_active_subscription(tg_id, "manifest_club"):
            return True
        return bool(await get_user_purchases(tg_id))
    except Exception:
        return False


# ── AI-проводник «Манифест 7» (прогресс практик) ─────────────────────────────

async def guide_get_all(tg_id: int):
    return await _exec(
        "SELECT * FROM manifest7_guide WHERE tg_id = ?", (tg_id,), fetch="all")


async def guide_get(tg_id: int, practice: int):
    return await _exec(
        "SELECT * FROM manifest7_guide WHERE tg_id = ? AND practice = ?",
        (tg_id, practice), fetch="one")


async def guide_set_step(tg_id: int, practice: int, step: int):
    await _exec(
        """
        INSERT INTO manifest7_guide (tg_id, practice, step, completed_at, updated_at)
        VALUES (?, ?, ?, NULL, CURRENT_TIMESTAMP)
        ON CONFLICT(tg_id, practice) DO UPDATE SET
            step = excluded.step,
            completed_at = NULL,
            updated_at = CURRENT_TIMESTAMP
        """,
        (tg_id, practice, step),
    )


async def guide_complete(tg_id: int, practice: int):
    await _exec(
        """
        INSERT INTO manifest7_guide (tg_id, practice, step, completed_at, updated_at)
        VALUES (?, ?, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(tg_id, practice) DO UPDATE SET
            completed_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        """,
        (tg_id, practice),
    )


# ── Tribute-посты (кэш карточек для copy_message) ────────────────────────────

async def set_tribute_post(product_code: str, src_chat_id: int, src_message_id: int):
    await _exec(
        """
        INSERT INTO tribute_posts (product_code, src_chat_id, src_message_id, captured_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(product_code) DO UPDATE SET
            src_chat_id = excluded.src_chat_id,
            src_message_id = excluded.src_message_id,
            captured_at = CURRENT_TIMESTAMP
        """,
        (product_code, src_chat_id, src_message_id),
    )


async def get_tribute_post(product_code: str):
    return await _exec(
        "SELECT * FROM tribute_posts WHERE product_code = ?",
        (product_code,),
        fetch="one",
    )


# ── «Алёна на связи» (сессии живого AI-диалога) ──────────────────────────────

async def ai_active_session(tg_id: int):
    return await _exec(
        "SELECT * FROM ai_sessions WHERE tg_id = ? AND status = 'active' "
        "ORDER BY id DESC LIMIT 1",
        (tg_id,), fetch="one")


async def ai_sessions_used_30d(tg_id: int) -> int:
    row = await _exec(
        "SELECT COUNT(*) AS n FROM ai_sessions WHERE tg_id = ? "
        "AND datetime(started_at) > datetime('now', '-30 days')",
        (tg_id,), fetch="one")
    return int((row or {}).get("n") or 0)


async def ai_sessions_used_member(tg_id: int, since: str) -> int:
    """Встречи члена Клуба за скользящее окно 30 дней, но НЕ раньше вступления
    (since = subscriptions.started_at). Иначе бесплатная пробная встреча ДО
    покупки съедала бы месячную квоту (аудит 05.07) — новый член получал 1 из 2."""
    row = await _exec(
        "SELECT COUNT(*) AS n FROM ai_sessions WHERE tg_id = ? "
        "AND datetime(started_at) > datetime('now', '-30 days') "
        "AND datetime(started_at) >= datetime(?)",
        (tg_id, since), fetch="one")
    return int((row or {}).get("n") or 0)


async def ai_total_sessions(tg_id: int) -> int:
    row = await _exec(
        "SELECT COUNT(*) AS n FROM ai_sessions WHERE tg_id = ?",
        (tg_id,), fetch="one")
    return int((row or {}).get("n") or 0)


async def ai_last_session(tg_id: int):
    """Последняя встреча юзера любого статуса (для пост-оффер режима возражений)."""
    return await _exec(
        "SELECT * FROM ai_sessions WHERE tg_id = ? ORDER BY id DESC LIMIT 1",
        (tg_id,), fetch="one")


async def events_count_recent(tg_id: int, event: str, hours: int = 48,
                              minutes: int | None = None) -> int:
    """Сколько событий event у юзера за последние N часов (лимиты повторов). Крэш-сейф.
    minutes задан → окно в минутах (щиты мельче часа, напр. повтор оффера)."""
    try:
        window = (f"-{int(minutes)} minutes" if minutes is not None
                  else f"-{int(hours)} hours")
        row = await _exec(
            "SELECT COUNT(*) AS n FROM funnel_events WHERE tg_id = ? AND event = ? "
            f"AND datetime(created_at) > datetime('now', '{window}')",
            (tg_id, event), fetch="one")
        return int((row or {}).get("n") or 0)
    except Exception:
        return 0


async def events_count_total(tg_id: int, events: tuple) -> int:
    """Событий за ВСЁ время (квоты касаний, мандат Кая 03.07). Крэш-сейф → 0."""
    try:
        marks = ",".join("?" * len(events))
        row = await _exec(
            f"SELECT COUNT(*) AS n FROM funnel_events WHERE tg_id = ? AND event IN ({marks})",
            (tg_id, *events), fetch="one")
        return int((row or {}).get("n") or 0)
    except Exception:
        return 0


async def club_ladder_candidates(min_days: int = 14, limit: int = 10):
    """Члены Клуба ≥N дней — для спящей лестницы 1:1 (совещание 03.07).
    Крэш-сейф → []."""
    try:
        return await _exec(
            "SELECT tg_id FROM subscriptions WHERE product_code = 'manifest_club' "
            "AND active = 1 "
            f"AND datetime(started_at) < datetime('now', '-{int(min_days)} days') "
            f"LIMIT {int(limit)}",
            fetch="all") or []
    except Exception:
        logger.warning("club_ladder_candidates failed (degraded)", exc_info=True)
        return []


async def ai_open_session(tg_id: int):
    """Создаёт активную встречу и возвращает её строку (с id).

    A2 (аудит): открытие АТОМАРНОЕ — INSERT только если активной встречи нет
    (двойной тап «Начать» раньше давал 2 активные сессии → обход лимита)."""
    await _exec(
        "INSERT INTO ai_sessions (tg_id, status, turns) "
        "SELECT ?, 'active', 0 "
        "WHERE NOT EXISTS (SELECT 1 FROM ai_sessions WHERE tg_id = ? AND status = 'active')",
        (tg_id, tg_id))
    return await ai_active_session(tg_id)


async def ai_session_idle_minutes(session_id: int) -> float | None:
    """Минут с последней активности встречи (последнее сообщение, иначе старт).
    Для детекта встреч-сирот, убитых редеплоем (прогон №3 03.07: висящая active
    глотала новый путь после кружка). Крэш-сейф: сбой → None."""
    try:
        row = await _exec(
            "SELECT (julianday('now') - julianday(COALESCE("
            "  (SELECT MAX(created_at) FROM ai_messages WHERE session_id = s.id),"
            "  s.started_at))) * 1440.0 AS idle_min "
            "FROM ai_sessions s WHERE s.id = ?",
            (session_id,), fetch="one")
        if row is None or row.get("idle_min") is None:
            return None
        return float(row["idle_min"])
    except Exception:
        logger.warning("ai_session_idle_minutes failed (degraded)", exc_info=True)
        return None


async def ai_close_all_active(tg_id: int):
    """A2: закрыть ВСЕ активные встречи юзера (раньше закрывалась только новейшая —
    осиротевшая старая жила вечно и давала безлимит модели)."""
    await _exec(
        "UPDATE ai_sessions SET status = 'closed', closed_at = CURRENT_TIMESTAMP "
        "WHERE tg_id = ? AND status = 'active'", (tg_id,))


async def ai_close_session(session_id: int):
    await _exec(
        "UPDATE ai_sessions SET status = 'closed', closed_at = CURRENT_TIMESTAMP "
        "WHERE id = ?",
        (session_id,))


async def ai_bump_turns(session_id: int):
    await _exec(
        "UPDATE ai_sessions SET turns = turns + 1 WHERE id = ?", (session_id,))


async def ai_bump_paid_touch(session_id: int):
    """Волна 3: +1 платное касание (голос/кружок) на встрече. Зеркало ai_bump_turns.
    Крэш-сейф: сбой инкремента не должен ронять отправку — ловится у вызывающего."""
    await _exec(
        "UPDATE ai_sessions SET paid_touch_count = paid_touch_count + 1 "
        "WHERE id = ?", (session_id,))


async def ai_add_message(session_id: int, tg_id: int, role: str, content: str):
    await _exec(
        "INSERT INTO ai_messages (session_id, tg_id, role, content) "
        "VALUES (?, ?, ?, ?)",
        (session_id, tg_id, role, content))


async def ai_get_messages(session_id: int, limit: int = 40):
    return await _exec(
        "SELECT role, content FROM ai_messages WHERE session_id = ? "
        "ORDER BY id ASC LIMIT ?",
        (session_id, limit), fetch="all")


async def ai_stale_sessions(minutes: int, limit: int = 50):
    """Активные встречи, «затихшие на пике»: последнее сообщение — от Алёны
    ('model', т.е. она задала вопрос/сделала оффер), человек молчит дольше
    `minutes`, и мягкий нудж ещё не слали (nudged_at IS NULL).

    Крэш-сейф: если колонки/таблицы нет (миграция не докатилась) — вернём [],
    деградирует только эта фича, не планировщик.
    """
    try:
        return await _exec(
            "SELECT s.id AS session_id, s.tg_id AS tg_id "
            "FROM ai_sessions s "
            "JOIN ai_messages m ON m.id = ("
            "    SELECT id FROM ai_messages WHERE session_id = s.id "
            "    ORDER BY id DESC LIMIT 1) "
            "WHERE s.status = 'active' AND s.nudged_at IS NULL AND s.turns > 0 "
            "AND m.role = 'model' "
            f"AND datetime(m.created_at) < datetime('now', '-{int(minutes)} minutes') "
            f"LIMIT {int(limit)}",
            fetch="all") or []
    except Exception:
        logger.warning("ai_stale_sessions failed (degraded)", exc_info=True)
        return []


async def ai_reengage_sessions(minutes: int, limit: int = 50):
    """Re-engage (Кай 09.07): активные встречи, где последней была реплика Алёны
    ('model'), человек молчит дольше `minutes`, и мягкое возвращение ещё не слали
    (reengaged_at IS NULL) И оффер-нудж ещё не слали (nudged_at IS NULL — иначе
    поздно возвращать). Крэш-сейф: сбой/нет колонки → [] (деградирует только фича)."""
    try:
        return await _exec(
            "SELECT s.id AS session_id, s.tg_id AS tg_id "
            "FROM ai_sessions s "
            "JOIN ai_messages m ON m.id = ("
            "    SELECT id FROM ai_messages WHERE session_id = s.id "
            "    ORDER BY id DESC LIMIT 1) "
            "WHERE s.status = 'active' AND s.reengaged_at IS NULL "
            "AND s.nudged_at IS NULL AND s.turns > 0 AND m.role = 'model' "
            f"AND datetime(m.created_at) < datetime('now', '-{int(minutes)} minutes') "
            f"LIMIT {int(limit)}",
            fetch="all") or []
    except Exception:
        logger.warning("ai_reengage_sessions failed (degraded)", exc_info=True)
        return []


async def ai_mark_reengaged(session_id: int):
    """Пометить встречу как «уже возвращали» (одно мягкое возвращение на встречу)."""
    try:
        await _exec("UPDATE ai_sessions SET reengaged_at = datetime('now') "
                    "WHERE id = ?", (session_id,))
    except Exception:
        logger.warning("ai_mark_reengaged failed for %s", session_id, exc_info=True)


async def ai_orphan_sessions(minutes: int = 3, limit: int = 20):
    """T-1 (03.07): активные встречи, где последнее сообщение — от ЧЕЛОВЕКА и
    висит без ответа дольше `minutes` (редеплой убил ход посреди генерации).
    Крэш-сейф: сбой → [] (деградирует только само-восстановление)."""
    try:
        return await _exec(
            "SELECT s.id AS session_id, s.tg_id AS tg_id "
            "FROM ai_sessions s "
            "JOIN ai_messages m ON m.id = ("
            "    SELECT id FROM ai_messages WHERE session_id = s.id "
            "    ORDER BY id DESC LIMIT 1) "
            "WHERE s.status = 'active' AND m.role = 'user' "
            f"AND datetime(m.created_at) < datetime('now', '-{int(minutes)} minutes') "
            f"LIMIT {int(limit)}",
            fetch="all") or []
    except Exception:
        logger.warning("ai_orphan_sessions failed (degraded)", exc_info=True)
        return []


async def ai_dead_sessions(minutes: int = 30, limit: int = 50):
    """Батч Б: встречи, «умершие молчанием» — активные, где мягкий нудж УЖЕ слали
    (nudged_at IS NOT NULL), turns >= 2, последнее сообщение — от Алёны ('model'),
    а человек молчит дольше `minutes`. Такие закрываем ВДОГОНКУ полным оффер-путём
    (иначе лид уходит без оффера и без followup-серии — навсегда, аудит 06.07).
    Крэш-сейф: сбой → [] (деградирует только эта фича, не планировщик)."""
    try:
        return await _exec(
            "SELECT s.id AS session_id, s.tg_id AS tg_id, s.turns AS turns "
            "FROM ai_sessions s "
            "JOIN ai_messages m ON m.id = ("
            "    SELECT id FROM ai_messages WHERE session_id = s.id "
            "    ORDER BY id DESC LIMIT 1) "
            "WHERE s.status = 'active' AND s.nudged_at IS NOT NULL AND s.turns >= 2 "
            "AND m.role = 'model' "
            f"AND datetime(m.created_at) < datetime('now', '-{int(minutes)} minutes') "
            f"LIMIT {int(limit)}",
            fetch="all") or []
    except Exception:
        logger.warning("ai_dead_sessions failed (degraded)", exc_info=True)
        return []


async def ai_mark_nudged(session_id: int):
    """Пометить, что на этой встрече уже слали мягкий оффер Клуба (один на встречу)."""
    await _exec(
        "UPDATE ai_sessions SET nudged_at = CURRENT_TIMESTAMP WHERE id = ?",
        (session_id,))


# ── Контент-конвейер: items ──────────────────────────────────────────────────

async def content_batch_size(batch: str) -> int:
    row = await _exec(
        "SELECT COUNT(*) AS n FROM content_items WHERE batch = ?",
        (batch,), fetch="one")
    return int((row or {}).get("n") or 0)


async def content_add_item(batch, ext_id, channel, fmt, hypothesis,
                           draft, visual, cta, position):
    await _exec(
        """INSERT INTO content_items
           (batch, ext_id, channel, fmt, hypothesis, draft, visual, cta, position)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (batch, ext_id, channel, fmt, hypothesis, draft, visual, cta, position))


async def content_get_item(item_id: int):
    return await _exec(
        "SELECT * FROM content_items WHERE id = ?", (item_id,), fetch="one")


async def content_next_pending(batch: str | None = None):
    if batch:
        return await _exec(
            "SELECT * FROM content_items WHERE status = 'pending' AND batch = ? "
            "ORDER BY position ASC, id ASC LIMIT 1", (batch,), fetch="one")
    return await _exec(
        "SELECT * FROM content_items WHERE status = 'pending' "
        "ORDER BY position ASC, id ASC LIMIT 1", fetch="one")


async def content_set_final(item_id: int, text: str):
    await _exec(
        "UPDATE content_items SET final = ? WHERE id = ?", (text, item_id))


async def content_decide(item_id: int, status: str):
    """status: 'approved' | 'rejected'. Для approved final ← draft, если пуст."""
    await _exec(
        "UPDATE content_items SET status = ?, "
        "final = COALESCE(final, draft), decided_at = CURRENT_TIMESTAMP "
        "WHERE id = ?", (status, item_id))


async def content_defer(item_id: int):
    """«Потом»: отодвигает item в конец очереди, не меняя статус pending."""
    await _exec(
        "UPDATE content_items SET position = position + 100000 WHERE id = ?",
        (item_id,))


async def content_wipe_batch(batch: str):
    """Полностью удаляет батч (для /curate_reload — перезалить новую версию)."""
    await _exec("DELETE FROM content_items WHERE batch = ?", (batch,))


async def content_counts():
    rows = await _exec(
        "SELECT status, COUNT(*) AS n FROM content_items GROUP BY status",
        fetch="all") or []
    out = {"pending": 0, "approved": 0, "rejected": 0}
    for r in rows:
        out[r["status"]] = int(r["n"])
    return out


async def content_approved_by_channel(channel: str | None = None):
    if channel:
        return await _exec(
            "SELECT * FROM content_items WHERE status = 'approved' AND channel = ? "
            "ORDER BY position ASC", (channel,), fetch="all") or []
    return await _exec(
        "SELECT * FROM content_items WHERE status = 'approved' "
        "ORDER BY channel, position ASC", fetch="all") or []


# ── Контент-конвейер: состояние куратора ─────────────────────────────────────

async def curator_get_state(curator_id: int):
    return await _exec(
        "SELECT * FROM curator_state WHERE curator_id = ?",
        (curator_id,), fetch="one")


async def curator_set_state(curator_id: int, current_item, awaiting):
    await _exec(
        """INSERT INTO curator_state (curator_id, current_item, awaiting)
           VALUES (?, ?, ?)
           ON CONFLICT(curator_id) DO UPDATE SET
               current_item = excluded.current_item,
               awaiting = excluded.awaiting""",
        (curator_id, current_item, awaiting))


async def curator_mark_pushed(curator_id: int, date_str: str):
    await _exec(
        """INSERT INTO curator_state (curator_id, last_pushed_date)
           VALUES (?, ?)
           ON CONFLICT(curator_id) DO UPDATE SET
               last_pushed_date = excluded.last_pushed_date""",
        (curator_id, date_str))


# ── Контент-конвейер: очередь публикации ─────────────────────────────────────

async def pq_enqueue(item_id: int, channel: str, text: str):
    await _exec(
        "INSERT INTO post_queue (item_id, channel, text) VALUES (?, ?, ?)",
        (item_id, channel, text))


async def pq_next_queued(channel: str):
    return await _exec(
        "SELECT * FROM post_queue WHERE status = 'queued' AND channel = ? "
        "ORDER BY id ASC LIMIT 1", (channel,), fetch="one")


async def pq_mark_posted(pq_id: int):
    await _exec(
        "UPDATE post_queue SET status = 'posted', posted_at = CURRENT_TIMESTAMP "
        "WHERE id = ?", (pq_id,))


async def pq_counts():
    rows = await _exec(
        "SELECT channel, COUNT(*) AS n FROM post_queue WHERE status = 'queued' "
        "GROUP BY channel", fetch="all") or []
    return {r["channel"]: int(r["n"]) for r in rows}


# ── Волна 1 (H12): события воронки ────────────────────────────────────────────

async def log_event(tg_id: int, event: str, meta: str | None = None):
    """Записать событие воронки в D1 + зеркалом в PostHog. Крэш-сейф: телеметрия
    НИКОГДА не роняет поток (обе ветки независимо обёрнуты)."""
    try:
        await _exec(
            "INSERT INTO funnel_events (tg_id, event, meta) VALUES (?, ?, ?)",
            (tg_id, event, (meta or None)))
    except Exception:
        logger.warning("log_event(%s) failed (continuing)", event, exc_info=True)
    # Зеркало в PostHog — FIRE-AND-FORGET (аудит 05.07): create_task, НЕ await, чтобы
    # медленный/недоступный PostHog не добавлял задержку в горячий путь воронки
    # (в offer-flow ~2-4 события за ход → до 10-15с на самом денежном моменте). Отдельный
    # try, чтобы падение аналитики не трогало запись в D1. Нет ключа → capture() сам no-op.
    try:
        from analytics import capture as _ph_capture
        asyncio.create_task(_ph_capture(tg_id, event, ({"meta": meta} if meta else None)))
    except Exception:
        logger.debug("posthog mirror skipped (continuing)", exc_info=True)


async def event_counts(days: int = 30):
    """Сводка событий за N дней: {event: (всего, уникальных людей)}. Крэш-сейф."""
    try:
        rows = await _exec(
            "SELECT event, COUNT(*) AS n, COUNT(DISTINCT tg_id) AS u "
            f"FROM funnel_events WHERE datetime(created_at) > datetime('now', '-{int(days)} days') "
            "GROUP BY event ORDER BY n DESC",
            fetch="all") or []
        return {r["event"]: (int(r["n"]), int(r["u"])) for r in rows}
    except Exception:
        logger.warning("event_counts failed (continuing)", exc_info=True)
        return {}


# ── Волна 1 (H6/H7): дожим после оффера — серия из 3 касаний ──────────────────

async def followup_schedule(tg_id: int, delays_min: list[int]):
    """Поставить серию дожимов (стадии 1..N через delays_min минут от «сейчас»).

    Одна серия на человека за всю жизнь: если у него уже есть строки — no-op.
    Крэш-сейф: сбой планирования не ломает закрытие встречи."""
    try:
        row = await _exec(
            "SELECT 1 FROM followups WHERE tg_id = ? LIMIT 1", (tg_id,), fetch="one")
        if row:
            return
        for stage, minutes in enumerate(delays_min, start=1):
            # {:+d} → '+45'/'-5': валидный модификатор SQLite в обоих знаках
            await _exec(
                "INSERT INTO followups (tg_id, stage, due_at) "
                f"VALUES (?, ?, datetime('now', '{int(minutes):+d} minutes'))",
                (tg_id, stage))
    except Exception:
        logger.warning("followup_schedule failed (continuing)", exc_info=True)


async def followups_due(limit: int = 30):
    """Готовые к отправке касания: due_at прошёл, не отправлены, человек НЕ купил.

    Оплатившие отфильтровываются прямо здесь (не полагаемся на отмену серии).
    Крэш-сейф: при сбое вернём []."""
    try:
        return await _exec(
            "SELECT f.id AS fid, f.tg_id AS tg_id, f.stage AS stage "
            "FROM followups f "
            "WHERE f.status = 'due' AND datetime(f.due_at) <= datetime('now') "
            "AND f.tg_id NOT IN (SELECT tg_id FROM subscriptions WHERE active = 1) "
            "AND f.tg_id NOT IN (SELECT tg_id FROM purchases) "
            f"ORDER BY f.due_at ASC LIMIT {int(limit)}",
            fetch="all") or []
    except Exception:
        logger.warning("followups_due failed (continuing)", exc_info=True)
        return []


async def followup_mark(fid: int, status: str = "sent"):
    """Пометить касание: sent / skipped / cancelled. Метка ДО отправки (антидубль)."""
    await _exec(
        "UPDATE followups SET status = ?, sent_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, fid))


async def followup_cancel_all(tg_id: int):
    """Отменить несостоявшиеся касания (например, после оплаты). Крэш-сейф."""
    try:
        await _exec(
            "UPDATE followups SET status = 'cancelled' "
            "WHERE tg_id = ? AND status = 'due'", (tg_id,))
    except Exception:
        logger.warning("followup_cancel_all failed (continuing)", exc_info=True)


# ── Hermes-руки: реактивация застрявших лидов ────────────────────────────────
# Три сегмента «застрял»:
#   quiz_no_alena — прошёл тест Тени (povorot), но не открывал AI-встречу /alena;
#   alena_no_buy  — был на AI-встрече, но ничего не купил (нет подписки/покупки);
#   club_churn    — был в Клубе, отписался (нет активной подписки).
# Кандидаты исключают тех, кого недавно трогали (cooldown) и у кого уже есть
# незакрытый черновик.

_GROWTH_SEGMENT_SQL = {
    "quiz_no_alena": (
        "povorot IS NOT NULL "
        "AND tg_id NOT IN (SELECT DISTINCT tg_id FROM ai_sessions)"
    ),
    "alena_no_buy": (
        "tg_id IN (SELECT DISTINCT tg_id FROM ai_sessions) "
        "AND tg_id NOT IN (SELECT tg_id FROM subscriptions WHERE active = 1) "
        "AND tg_id NOT IN (SELECT tg_id FROM purchases)"
    ),
    "club_churn": (
        "tg_id IN (SELECT tg_id FROM subscriptions "
        "          WHERE active = 0 OR cancelled_at IS NOT NULL) "
        "AND tg_id NOT IN (SELECT tg_id FROM subscriptions WHERE active = 1)"
    ),
}


async def growth_candidates(segment: str, cooldown_days: int, limit: int):
    """Кандидаты на реактивацию в сегменте. Crash-safe: при отсутствии колонок
    (миграция не докатилась) возвращаем пусто, не роняя джоб."""
    cond = _GROWTH_SEGMENT_SQL.get(segment)
    if not cond:
        return []
    cd = int(cooldown_days)
    lim = int(limit)
    sql = (
        f"SELECT * FROM users WHERE {cond} "
        f"AND (reactivated_at IS NULL "
        f"     OR datetime(reactivated_at, '+{cd} days') < datetime('now')) "
        f"AND tg_id NOT IN (SELECT tg_id FROM growth_drafts "
        f"                  WHERE status IN ('pending','approved','sent')) "
        f"ORDER BY created_at DESC LIMIT {lim}"
    )
    try:
        return await _exec(sql, fetch="all") or []
    except Exception:
        logger.warning("growth_candidates(%s) failed (continuing)", segment, exc_info=True)
        return []


async def growth_add_draft(tg_id: int, segment: str, draft: str):
    await _exec(
        "INSERT INTO growth_drafts (tg_id, segment, draft) VALUES (?, ?, ?)",
        (tg_id, segment, draft))
    return await _exec(
        "SELECT * FROM growth_drafts WHERE tg_id = ? AND segment = ? "
        "ORDER BY id DESC LIMIT 1", (tg_id, segment), fetch="one")


async def growth_get_draft(draft_id: int):
    return await _exec(
        "SELECT * FROM growth_drafts WHERE id = ?", (draft_id,), fetch="one")


async def growth_set_status(draft_id: int, status: str):
    await _exec(
        "UPDATE growth_drafts SET status = ?, decided_at = CURRENT_TIMESTAMP "
        "WHERE id = ?", (status, draft_id))


async def growth_update_draft(draft_id: int, text: str):
    await _exec(
        "UPDATE growth_drafts SET draft = ? WHERE id = ?", (text, draft_id))


async def mark_reactivated(tg_id: int):
    """Антиспам-метка: юзера тронули реактивацией (ставим при создании черновика,
    чтобы следующий прогон его не выбрал снова)."""
    try:
        await _exec(
            "UPDATE users SET reactivated_at = CURRENT_TIMESTAMP WHERE tg_id = ?",
            (tg_id,))
    except Exception:
        logger.warning("mark_reactivated failed (continuing)", exc_info=True)


async def growth_counts():
    rows = await _exec(
        "SELECT status, COUNT(*) AS n FROM growth_drafts GROUP BY status",
        fetch="all") or []
    return {r["status"]: int(r["n"]) for r in rows}


# ── Пивот E1 (T1): тест «Атмосфера дома» ─────────────────────────────────────

async def atm_save_result(tg_id: int, answers_json: str, scores_json: str,
                          weak: str, pair_src: int | None = None):
    """Upsert результата теста: retake перезаписывает; next-day чек сбрасывается
    на новый круг. pair_src не затираем NULL'ом при соло-перепрохождении."""
    await _exec(
        "INSERT INTO atm_quiz (tg_id, answers, scores, weak, pair_src, "
        "nextday_sent, created_at) VALUES (?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP) "
        "ON CONFLICT(tg_id) DO UPDATE SET "
        "answers = excluded.answers, scores = excluded.scores, "
        "weak = excluded.weak, "
        "pair_src = COALESCE(excluded.pair_src, atm_quiz.pair_src), "
        "nextday_sent = 0, created_at = CURRENT_TIMESTAMP",
        (tg_id, answers_json, scores_json, weak, pair_src))


async def atm_get_result(tg_id: int):
    """Результат теста юзера → dict | None. Крэш-сейф (нет таблицы → None)."""
    try:
        return await _exec("SELECT * FROM atm_quiz WHERE tg_id = ?",
                           (tg_id,), fetch="one")
    except Exception:
        logger.warning("atm_get_result failed (continuing)", exc_info=True)
        return None


async def atm_nextday_due(hours: int = 20, limit: int = 50):
    """Кому пора слать next-day чек: прошли тест ≥N часов назад, ещё не слали.
    Крэш-сейф → []."""
    try:
        return await _exec(
            "SELECT tg_id FROM atm_quiz WHERE nextday_sent = 0 "
            f"AND datetime(created_at, '+{int(hours)} hours') < datetime('now') "
            f"LIMIT {int(limit)}", fetch="all") or []
    except Exception:
        logger.warning("atm_nextday_due failed (continuing)", exc_info=True)
        return []


async def atm_mark_nextday(tg_id: int):
    """Метка «next-day чек отправлен» — ДО отправки (антидубль). Крэш-сейф."""
    try:
        await _exec("UPDATE atm_quiz SET nextday_sent = 1 WHERE tg_id = ?",
                    (tg_id,))
    except Exception:
        logger.warning("atm_mark_nextday failed (continuing)", exc_info=True)


# ── Сквозной Банк 5:1 (Шаг 1, спина кольца) ──────────────────────────────────
# Одна цифра-соотношение на ПАРУ (не на users): «повороты-к» / «повороты-от».
# couple_id резолвится из atm_quiz.pair_src; счётчик не кэшируем — SUM на чтение.
# Всё крэш-сейф по стилю atm_*: сбой деградирует фичу банка, не роняет поток.

def _bank_day_key(day_key: str | None) -> str:
    """day_key по умолчанию = сегодняшняя дата UTC (YYYY-MM-DD) — ключ идемпотентности."""
    return day_key or datetime.utcnow().strftime("%Y-%m-%d")


def _bank_ratio_str(plus: int, minus: int) -> str:
    """Соотношение «к:от» несократимой дробью: 4,1 → '4:1'; 8,2 → '4:1'.
    minus=0 → 'N:0' (пара ещё без холодных моментов; деления на ноль нет)."""
    if minus <= 0:
        return f"{plus}:0"
    from math import gcd
    g = gcd(plus, minus) or 1
    return f"{plus // g}:{minus // g}"


def _bank_progress_to_5(plus: int, minus: int) -> float:
    """Прогресс к якорю 5:1 ∈ [0,1]. plus=0 → 0.0; minus=0 при plus>0 → 1.0."""
    if plus <= 0:
        return 0.0
    if minus <= 0:
        return 1.0
    return min(1.0, (plus / minus) / 5.0)


async def resolve_couple(tg_id: int) -> int:
    """couple_id пары: из atm_quiz.pair_src если есть пара, иначе собственный tg_id
    (соло, partner_id=NULL). Создаёт запись couples при первом обращении. Крэш-сейф:
    сбой чтения → деградируем на соло (couple_id = tg_id), поток не роняем."""
    pair_src = None
    try:
        row = await _exec(
            "SELECT pair_src FROM atm_quiz WHERE tg_id = ?", (tg_id,), fetch="one")
        pair_src = (row or {}).get("pair_src")
    except Exception:
        logger.warning("resolve_couple read pair_src failed (continuing)", exc_info=True)
    if pair_src:
        couple_id, partner_id = int(pair_src), tg_id
    else:
        couple_id, partner_id = tg_id, None
    try:
        await _exec(
            "INSERT OR IGNORE INTO couples (couple_id, partner_id) VALUES (?, ?)",
            (couple_id, partner_id))
        # инициатор мог создать соло-строку раньше (partner_id NULL) — фиксируем пару
        if partner_id is not None:
            await _exec(
                "UPDATE couples SET partner_id = ? "
                "WHERE couple_id = ? AND partner_id IS NULL",
                (partner_id, couple_id))
    except Exception:
        logger.warning("resolve_couple upsert failed (continuing)", exc_info=True)
    return couple_id


async def bank_add(couple_id: int, kind: str, sign: int = 1,
                   day_key: str | None = None) -> bool:
    """Идемпотентный INSERT OR IGNORE поворота в bank_ledger. day_key по умолчанию =
    сегодня UTC. True — если реально добавили (не дубль за день по UNIQUE), False —
    дубль/сбой. Крэш-сейф.
    ponytail: SELECT-до-INSERT даёт честный возврат (у _exec нет rowcount); UNIQUE
    в БД гарантирует отсутствие дубля даже при гонке. Парный gate (+1 только по
    подтверждению партнёра) живёт в местах вызова — примитив тупой и переиспользуемый."""
    dk = _bank_day_key(day_key)
    try:
        existing = await _exec(
            "SELECT 1 FROM bank_ledger WHERE couple_id = ? AND kind = ? AND day_key = ?",
            (couple_id, kind, dk), fetch="one")
        await _exec(
            "INSERT OR IGNORE INTO bank_ledger (couple_id, kind, sign, day_key) "
            "VALUES (?, ?, ?, ?)",
            (couple_id, kind, int(sign), dk))
    except Exception:
        logger.warning("bank_add failed (continuing)", exc_info=True)
        return False
    return existing is None


async def get_bank(couple_id: int) -> dict:
    """Состояние банка пары → {plus, minus, ratio_str, turns, progress_to_5}.
    plus = SUM положительных знаков, minus = |SUM отрицательных|, turns = plus.
    SUM по ledger на чтение, без кэша (N мал). Крэш-сейф → нулевой банк."""
    plus = minus = 0
    try:
        row = await _exec(
            "SELECT "
            "COALESCE(SUM(CASE WHEN sign > 0 THEN sign ELSE 0 END), 0) AS plus, "
            "COALESCE(SUM(CASE WHEN sign < 0 THEN -sign ELSE 0 END), 0) AS minus "
            "FROM bank_ledger WHERE couple_id = ?",
            (couple_id,), fetch="one")
        plus = int((row or {}).get("plus") or 0)
        minus = int((row or {}).get("minus") or 0)
    except Exception:
        logger.warning("get_bank failed (continuing)", exc_info=True)
    return {
        "plus": plus,
        "minus": minus,
        "ratio_str": _bank_ratio_str(plus, minus),
        "turns": plus,
        "progress_to_5": _bank_progress_to_5(plus, minus),
    }


async def merge_banks(canonical_couple_id: int, other_couple_id: int) -> None:
    """Слить соло-банк other в canonical при образовании пары: repoint ledger,
    погасить дубли по UNIQUE, удалить осиротевшую строку other, проставить
    partner_id. Идемпотентно (повторный вызов — no-op), крэш-сейф.
    ponytail: UPDATE OR IGNORE гасит коллизию kind+day_key (обе стороны сделали
    один kind в один день) — остаётся canonical-строка, непереехавший дубль other
    чистим DELETE ниже; счёт не задваивается и обе стороны не теряются."""
    if canonical_couple_id == other_couple_id:
        return
    try:
        await _exec(
            "UPDATE OR IGNORE bank_ledger SET couple_id = ? WHERE couple_id = ?",
            (canonical_couple_id, other_couple_id))
        await _exec("DELETE FROM bank_ledger WHERE couple_id = ?", (other_couple_id,))
        # гарантируем строку canonical (обычно её создал resolve_couple) и фиксируем пару
        await _exec(
            "INSERT OR IGNORE INTO couples (couple_id, partner_id) VALUES (?, ?)",
            (canonical_couple_id, other_couple_id))
        await _exec(
            "UPDATE couples SET partner_id = ? "
            "WHERE couple_id = ? AND partner_id IS NULL",
            (other_couple_id, canonical_couple_id))
        await _exec("DELETE FROM couples WHERE couple_id = ?", (other_couple_id,))
    except Exception:
        logger.warning("merge_banks failed (continuing)", exc_info=True)


async def render_bank_card(couple_id: int) -> str:
    """Единый текст-карточка банка пары — ОДИН вид для всех 5 поверхностей
    (gap-карта, recap 6-секунд, чек-ин, дневная монета, недельный отчёт клуба).
    Тон анти-лайфкоучинг: арифметика поворотов, без похвалы и стоп-фраз.
    ponytail: ПЛЕЙСХОЛДЕР-ТЕКСТ — smyslovik отполирует формулировки, designer даст
    визуал карточки. Структура (цифра-соотношение + прогресс к 5:1 + N поворотов)
    фиксирована; шлётся parse_mode=None (символы могут ломать Markdown)."""
    b = await get_bank(couple_id)
    filled = round(b["progress_to_5"] * 5)
    bar = "▰" * filled + "▱" * (5 - filled)
    return (
        f"Ваш банк: {b['ratio_str']}\n"
        f"{bar}  к цели 5:1\n"
        f"Поворотов-к: {b['turns']}"
    )
