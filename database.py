"""SQLite база на aiosqlite. Минимум для MVP."""

import aiosqlite
from contextlib import asynccontextmanager
from datetime import datetime

DB_PATH = "kydaidy.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    povorot INTEGER,
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
"""


@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


async def init_db():
    async with get_db() as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def upsert_user(tg_id: int, username: str | None, first_name: str | None, povorot: int | None = None):
    async with get_db() as db:
        await db.execute(
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
        await db.commit()


async def get_user(tg_id: int):
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        return await cursor.fetchone()


async def start_nurture(tg_id: int):
    async with get_db() as db:
        await db.execute(
            "UPDATE users SET nurture_active = 1, nurture_day = 0, last_nurture_at = ? WHERE tg_id = ?",
            (datetime.now(), tg_id),
        )
        await db.commit()


async def stop_nurture(tg_id: int):
    async with get_db() as db:
        await db.execute("UPDATE users SET nurture_active = 0 WHERE tg_id = ?", (tg_id,))
        await db.commit()


async def advance_nurture_day(tg_id: int, day: int):
    async with get_db() as db:
        await db.execute(
            "UPDATE users SET nurture_day = ?, last_nurture_at = ? WHERE tg_id = ?",
            (day, datetime.now(), tg_id),
        )
        await db.commit()


async def get_users_for_nurture():
    """Юзеры с активным nurture, готовые получить следующий день."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT * FROM users
            WHERE nurture_active = 1
              AND nurture_day < 7
              AND (
                  last_nurture_at IS NULL
                  OR datetime(last_nurture_at, '+20 hours') < datetime('now')
              )
            """
        )
        return await cursor.fetchall()


async def add_purchase(tg_id: int, product_code: str, amount: int, payment_id: str):
    async with get_db() as db:
        await db.execute(
            "INSERT INTO purchases (tg_id, product_code, amount, tribute_payment_id) VALUES (?, ?, ?, ?)",
            (tg_id, product_code, amount, payment_id),
        )
        await db.commit()


async def add_subscription(tg_id: int, product_code: str):
    async with get_db() as db:
        await db.execute(
            "INSERT INTO subscriptions (tg_id, product_code) VALUES (?, ?)",
            (tg_id, product_code),
        )
        await db.commit()


async def get_user_purchases(tg_id: int):
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM purchases WHERE tg_id = ? ORDER BY created_at DESC", (tg_id,))
        return await cursor.fetchall()


async def set_tribute_post(product_code: str, src_chat_id: int, src_message_id: int):
    async with get_db() as db:
        await db.execute(
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
        await db.commit()


async def get_tribute_post(product_code: str):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM tribute_posts WHERE product_code = ?",
            (product_code,),
        )
        return await cursor.fetchone()
