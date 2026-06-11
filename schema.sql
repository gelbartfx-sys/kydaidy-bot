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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER,
    product_code TEXT,
    active INTEGER DEFAULT 1,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    next_charge_at TIMESTAMP,
    cancelled_at TIMESTAMP
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
