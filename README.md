# kydaidy bot — деплой и эксплуатация

> Python TG-бот на aiogram 3. Воронка: захват из Tally → выдача карты + аудио → 7-дневный nurture → продажи через Tribute. SQLite для MVP.

---

## Архитектура

```
bot/
├── bot.py              # main entry point
├── config.py           # настройки из .env
├── database.py         # SQLite схема + методы
├── handlers.py         # команды /start, /quiz, /products, /cabinet
├── nurture.py          # 7-дневная серия (через APScheduler)
├── webhooks.py         # Tally + Tribute webhooks (aiohttp)
├── content_data.py     # ВСЕ ТЕКСТЫ бота (легко редактировать)
├── content/
│   ├── pdf/            # положить 5 PDF-карт
│   └── audio/          # положить 5 аудио-приветствий + nurture аудио
├── requirements.txt
├── Procfile            # для Render / Heroku
├── .env.example        # шаблон переменных окружения
└── README.md           # этот файл
```

---

## Локальный запуск (тест)

```bash
cd /Users/kai/kydaidy/bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Открой .env и впиши TG_BOT_TOKEN от BotFather
# (остальные ключи можно пока пустые — заполним когда подключим Tally и Tribute)

python bot.py
```

Бот должен запуститься, ответить в TG на /start.

---

## Деплой на Render (рекомендую)

### Шаг 1 — Подготовь репозиторий
1. Создай приватный GitHub repo (например, `kydaidy-bot`)
2. Запушь содержимое `bot/` туда:
   ```bash
   cd /Users/kai/kydaidy/bot
   git init
   git add .
   git commit -m "init kydaidy bot"
   git remote add origin git@github.com:USERNAME/kydaidy-bot.git
   git push -u origin main
   ```
3. **НЕ КОММИТЬ `.env`** — он в `.gitignore`. Только `.env.example`.

### Шаг 2 — Создай Web Service на Render
1. https://render.com → New → Web Service
2. Connect GitHub repo `kydaidy-bot`
3. Настройки:
   - **Name**: `kydaidy-bot`
   - **Region**: Frankfurt (ближе к RU юзерам)
   - **Branch**: `main`
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot.py`
   - **Plan**: Free (хватит на старте, до 750 ч/мес)
4. Environment Variables → добавь все из `.env.example`:
   - `TG_BOT_TOKEN` ← из BotFather
   - `TG_CHANNEL_ID` ← `@kydaidy`
   - `TG_ADMIN_ID` ← твой Telegram user ID (узнай у `@userinfobot`)
   - `TRIBUTE_API_KEY` ← пока заглушка, заполнишь позже
   - `WEBHOOK_BASE_URL` ← URL который Render выдаст после первого деплоя (например, `https://kydaidy-bot.onrender.com`)

### Шаг 3 — Подключи webhooks
После деплоя получишь URL типа `https://kydaidy-bot.onrender.com`. Подключи его:

#### Tally webhook
- Tally → твоя форма → Settings → Integrations → Webhooks
- URL: `https://kydaidy-bot.onrender.com/webhook/tally`
- Method: POST
- Test → должен пройти

#### Tribute webhook
- Tribute → API Settings → Webhooks
- URL: `https://kydaidy-bot.onrender.com/webhook/tribute`
- Подпиши secret в `TRIBUTE_WEBHOOK_SECRET` env var

### Шаг 4 — Положи контент
1. **5 PDF-карт** → `bot/content/pdf/karta-povorot-{1-5}.pdf`
   - Можно сначала использовать HTML-версии из `landing/pdf-cards/` → распечатать в PDF через Cmd+P в Chrome
2. **5 аудио-приветствий** → `bot/content/audio/povorot-{1-5}.mp3`
   - Записывает Алёна по скриптам из `docs/products/lead-magnet-quiz.md`
3. Закоммить и запушь — Render автоматически передеплоит

---

## Контент-менеджмент

**Все тексты бота находятся в `content_data.py`**. Чтобы изменить текст — отредактируй файл и передеплой:

```bash
cd /Users/kai/kydaidy/bot
# редактируешь content_data.py
git add content_data.py
git commit -m "update nurture day 4 text"
git push
# Render передеплоит автоматически
```

---

## Команды бота

| Команда | Что делает |
|---|---|
| `/start` | Главное меню |
| `/start povorot{N}` | Выдача карты по повороту (deep link из Tally) |
| `/quiz` | Ссылка на квиз |
| `/products` | Каталог продуктов |
| `/cabinet` | Личный кабинет (что куплено, поворот) |
| `/club` | Описание Клуба «Манифест» |
| `/stop` | Отписаться от nurture |
| `/help` | Справка |

---

## База данных

SQLite файл `kydaidy.db` создаётся автоматически. Таблицы: `users`, `purchases`, `subscriptions`, `messages_log`.

**Важно**: на Render free tier диск эфемерный (стирается при перезапуске). Когда юзеров станет 100+ — мигрируй на PostgreSQL (Render даёт free PG до 30 дней / Railway / Supabase).

---

## Мониторинг

- Render → Logs (real-time)
- Telegram-уведомления админу при ошибках (TODO: добавить sentry или просто `bot.send_message(TG_ADMIN_ID, ...)` в except)

---

## Что доделать после MVP

- [ ] PostgreSQL вместо SQLite
- [ ] Reply-keyboard вместо inline для удобства
- [ ] Google Sheets экспорт лидов (для Кая чтобы видеть в табличке)
- [ ] Connect AI-двойник «Манифест AI» (Phase 3)
- [ ] Аналитика: воронка квиз → tripwire → клуб → 1:1
- [ ] A/B тест nurture текстов
- [ ] Email-резерв (если кто-то отписался от TG)

---

## Troubleshooting

### Бот не отвечает
1. Проверь Render → Logs
2. Проверь `TG_BOT_TOKEN` в env vars
3. Перезапусти Render service

### PDF/аудио не отправляется
1. Проверь что файл лежит по пути из `content_data.py` → `PDF_FILES` / `AUDIO_FILES`
2. Файлы должны быть в `bot/content/...` (не в корне)
3. После добавления файлов — закоммить и запушь

### Tally webhook не приходит
1. Tally → Logs → проверить что webhook отправлен
2. Render → Logs → проверить что `/webhook/tally` POST приходит
3. Tally hidden fields: убедиться что в форме есть `tg_id` и `povorot`

### Nurture не отправляется
1. APScheduler работает каждый час — подожди до часа
2. Проверь в БД: `nurture_active = 1` для юзера
3. Логи Render: search "Nurture day"
