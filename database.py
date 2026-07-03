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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER,
    product_code TEXT,
    amount INTEGER,
    tribute_payment_id TEXT,
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
    nudged_at TIMESTAMP
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
        nudged_at TIMESTAMP
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
    await _exec(
        "INSERT INTO purchases (tg_id, product_code, amount, tribute_payment_id) VALUES (?, ?, ?, ?)",
        (tg_id, product_code, amount, payment_id),
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


async def events_count_recent(tg_id: int, event: str, hours: int = 48) -> int:
    """Сколько событий event у юзера за последние N часов (лимиты повторов). Крэш-сейф."""
    try:
        row = await _exec(
            "SELECT COUNT(*) AS n FROM funnel_events WHERE tg_id = ? AND event = ? "
            f"AND datetime(created_at) > datetime('now', '-{int(hours)} hours')",
            (tg_id, event), fetch="one")
        return int((row or {}).get("n") or 0)
    except Exception:
        return 0


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
    """Записать событие воронки. Крэш-сейф: телеметрия НИКОГДА не роняет поток."""
    try:
        await _exec(
            "INSERT INTO funnel_events (tg_id, event, meta) VALUES (?, ?, ?)",
            (tg_id, event, (meta or None)))
    except Exception:
        logger.warning("log_event(%s) failed (continuing)", event, exc_info=True)


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
