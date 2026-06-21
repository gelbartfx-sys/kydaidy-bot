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
    closed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ai_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    tg_id INTEGER,
    role TEXT,
    content TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        closed_at TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS ai_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        tg_id INTEGER,
        role TEXT,
        content TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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


async def ai_open_session(tg_id: int):
    """Создаёт активную встречу и возвращает её строку (с id)."""
    await _exec(
        "INSERT INTO ai_sessions (tg_id, status, turns) VALUES (?, 'active', 0)",
        (tg_id,))
    return await ai_active_session(tg_id)


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
