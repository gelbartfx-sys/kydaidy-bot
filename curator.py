"""Контент-конвейер: курирование Алёной в живом диалоге.

Идея (решено Каем 2026-06-21): в заданный час бот сам пишет Алёне в личку,
кидает дневную пачку черновиков по одному, она курирует кнопками — а на «✏️ Правка»
ведёт с ботом ЖИВОЙ диалог: говорит, что поменять (или шлёт свой текст), бот
переписывает в её тоне через Gemini. Одобренное → очередь автопостинга в TG-канал;
прочие каналы (Pinterest/Threads/Дзен/видео) — выгружаются /curate_export для Кая.

Тон правок строго по docs/voice-and-turns-sharp.md (лещ честности + механизм,
без императивов/эзотерики/обещаний/«5 шагов»).

Роутер подключается в bot.py ПЕРВЫМ (до alena_router): текст-фильтр режима правки
должен перехватить сообщение Алёны раньше, чем catch-all и AI-встреча.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Router, F
from aiogram.filters import Command, BaseFilter
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile,
)

from config import settings
from ai_quiz import BASE, TEXT_MODEL
from curator_data import BATCH, ITEMS
from database import (
    content_batch_size, content_add_item, content_get_item,
    content_next_pending, content_set_final, content_decide, content_defer,
    content_counts, content_approved_by_channel, content_wipe_batch,
    curator_get_state, curator_set_state, curator_mark_pushed,
    pq_enqueue, pq_next_queued, pq_mark_posted, pq_counts,
)

logger = logging.getLogger(__name__)
curator_router = Router()

CHANNEL_LABEL = {
    "threads": "THREADS", "pinterest": "PINTEREST", "dzen": "ДЗЕН",
    "video": "ВИДЕО (faceless)", "telegram": "TELEGRAM-КАНАЛ",
}

# Каналы, которые бот публикует сам. Прочие — ручной экспорт (/curate_export).
AUTOPOST_CHANNELS = {"telegram"}


def _is_curator(uid: int) -> bool:
    """Курировать может Алёна (curator_id) и админ-Кай (tg_admin_id)."""
    return uid == settings.curator_id or uid == settings.tg_admin_id


def _tz():
    """Таймзона рассылки. Фолбэк на фиксированный UTC+3 (МСК, без DST),
    если в контейнере нет tz-базы — тогда zoneinfo не нужен вовсе."""
    try:
        return ZoneInfo(settings.curator_tz)
    except Exception:
        return timezone(timedelta(hours=3))


# ── Тон правок (источник: docs/voice-and-turns-sharp.md) ─────────────────────

_TONE = (
    "Ты — редактор постов для проекта kydaidy. Пишешь голосом Алёны Kyda Idy: "
    "анти-лайфкоучинг. Тон: «лещ честности» — вскрываешь самообман, не утешаешь, "
    "опираешься на МЕХАНИЗМ (психология, нейробиология привязанности, схема-терапия), "
    "а не на поэзию. Бьёшь по иллюзии, не по человеку.\n"
    "ЗАПРЕЩЕНО: «5/7 шагов», «формула», «за 90 дней», «ты можешь всё», «просто полюби "
    "себя», «ты — богиня», «истинная женственность», эзотерика (энергия/поток/чакры/"
    "вибрации), императивы (должна/обязана/соберись), обещания результата, токсичный "
    "позитив, стыжение, жалость.\n"
    "Можно: конкретику, признание боли без жалости, уместный сухой юмор, крючок-вопрос "
    "в конце там, где это к месту.\n"
    "Всегда возвращай ТОЛЬКО готовый текст поста — без преамбулы, без пояснений, без "
    "кавычек-обёрток вокруг всего текста."
)


async def _gen(user_text: str, temperature: float, max_tokens: int = 1500) -> str:
    payload = {
        "systemInstruction": {"parts": [{"text": _TONE}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    url = f"{BASE}/models/{TEXT_MODEL}:generateContent?key={settings.gemini_key}"
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload,
                          timeout=aiohttp.ClientTimeout(total=60)) as r:
            body = await r.json()
    if "candidates" not in body:
        raise RuntimeError(f"curator gen failed: {json.dumps(body)[:300]}")
    parts = body["candidates"][0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("curator gen empty")
    return text


async def _apply_edit(item: dict, instruction: str) -> str:
    base = item.get("final") or item.get("draft") or ""
    user = (
        f"КАНАЛ: {CHANNEL_LABEL.get(item['channel'], item['channel'])}\n"
        f"ФОРМАТ: {item.get('fmt')}\n\n"
        f"ТЕКУЩИЙ ТЕКСТ ПОСТА:\n{base}\n\n"
        f"Алёна сказала, как поправить (или прислала свой готовый текст):\n"
        f"{instruction}\n\n"
        "Если это готовый текст — верни его, причесав под тон. "
        "Если это указание — примени к тексту выше. Верни только финальный пост."
    )
    return await _gen(user, temperature=0.7)


async def _make_variant(item: dict) -> str:
    user = (
        f"КАНАЛ: {CHANNEL_LABEL.get(item['channel'], item['channel'])}\n"
        f"ФОРМАТ: {item.get('fmt')}\n\n"
        f"ЧЕРНОВИК:\n{item.get('draft')}\n\n"
        "Дай ДРУГОЙ вариант этого поста: тот же смысл и тон, но иначе сформулированный, "
        "со свежим хуком. Верни только текст."
    )
    return await _gen(user, temperature=0.95)


# ── Карточка единицы контента ────────────────────────────────────────────────

def _item_kbd(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Беру", callback_data=f"cur:ok:{item_id}"),
         InlineKeyboardButton(text="✏️ Правка", callback_data=f"cur:edit:{item_id}")],
        [InlineKeyboardButton(text="🔁 Вариант", callback_data=f"cur:var:{item_id}"),
         InlineKeyboardButton(text="❌ Не моё", callback_data=f"cur:no:{item_id}")],
        [InlineKeyboardButton(text="⏭ Потом", callback_data=f"cur:skip:{item_id}"),
         InlineKeyboardButton(text="⏹ Стоп", callback_data="cur:stop")],
    ])


def _card_text(item: dict, pending: int, prefix: str = "") -> str:
    label = CHANNEL_LABEL.get(item["channel"], item["channel"])
    body = item.get("final") or item.get("draft") or ""
    head = f"🗂 {label} · {item.get('ext_id')} · {item.get('fmt')}  [{item.get('hypothesis')}]"
    lines = [head, f"📥 в очереди ещё: {pending}", "", body]
    if item.get("visual"):
        lines += ["", f"🎨 {item['visual']}"]
    if item.get("cta"):
        lines += [f"➡️ CTA: {item['cta']}"]
    if prefix:
        lines = [prefix, ""] + lines
    return "\n".join(lines)


async def _send_pin_image(bot, chat_id: int, item: dict, caption: str | None = None):
    """Рендер брендового пина из тезиса и отправка картинкой. Тихо пропускаем при сбое."""
    try:
        from pin_image import render_pin
        png = render_pin(
            item.get("final") or item.get("draft") or "",
            item.get("fmt"), item.get("ext_id"))
    except Exception:
        logger.exception("pin render failed for %s", item.get("ext_id"))
        return False
    cap = caption if caption is not None else f"📌 Пин {item.get('ext_id')} — готов для Pinterest"
    if item.get("cta") and caption is None:
        cap += f"\n➡️ {item['cta']}"
    try:
        await bot.send_photo(
            chat_id,
            BufferedInputFile(png, filename=f"pin_{item.get('ext_id')}.png"),
            caption=cap, parse_mode=None)
        return True
    except Exception:
        logger.exception("pin send failed for %s", item.get("ext_id"))
        return False


async def _send_card(bot_or_msg, chat_id: int, item: dict, prefix: str = ""):
    counts = await content_counts()
    text = _card_text(item, counts.get("pending", 0), prefix)
    await bot_or_msg.send_message(
        chat_id, text, parse_mode=None, reply_markup=_item_kbd(item["id"]))


async def _send_next(bot, chat_id: int, curator_id: int):
    item = await content_next_pending(BATCH)
    if not item:
        c = await content_counts()
        await curator_set_state(curator_id, None, None)
        q = await pq_counts()
        qline = ""
        if q.get("telegram"):
            qline = f"\nВ очереди на автопостинг в канал: {q['telegram']}."
        await bot.send_message(
            chat_id,
            "Готово — батч разобрала 🌑\n\n"
            f"✅ беру: {c.get('approved', 0)}   ❌ не моё: {c.get('rejected', 0)}"
            f"{qline}\n\n"
            "Одобренное для других сетей (Pinterest/Threads/Дзен/видео) — забрать "
            "командой /curate_export. Продолжить позже — /curate.",
            parse_mode=None)
        return
    await curator_set_state(curator_id, item["id"], None)
    await _send_card(bot, chat_id, item)


# ── Команды ───────────────────────────────────────────────────────────────────

@curator_router.message(Command("myid"))
async def cmd_myid(message: Message):
    u = message.from_user
    # parse_mode=None обязателен: дефолт бота = Markdown, а username с «_»
    # (напр. @al_lazovsky) ломает разметку → TelegramBadRequest и тишина.
    await message.answer(
        f"Твой Telegram id: {u.id}\n@{u.username or '—'}", parse_mode=None)


@curator_router.message(Command("curate_load"))
async def cmd_load(message: Message):
    if not _is_curator(message.from_user.id):
        return
    existing = await content_batch_size(BATCH)
    if existing:
        await message.answer(
            f"Батч «{BATCH}» уже загружен: {existing} единиц. "
            "Курировать — /curate.", parse_mode=None)
        return
    for pos, (ext_id, channel, fmt, hyp, draft, visual, cta) in enumerate(ITEMS):
        await content_add_item(BATCH, ext_id, channel, fmt, hyp, draft, visual, cta, pos)
    await message.answer(
        f"Загрузила батч «{BATCH}»: {len(ITEMS)} единиц.\nНачать курировать — /curate.",
        parse_mode=None)


@curator_router.message(Command("curate_reload"))
async def cmd_reload(message: Message):
    """Стереть батч и залить заново из curator_data (новая версия призывов).

    Доступно куратору (Алёна) и админу: курирует Алёна — ей и обновлять пачку.
    Иначе при админ-гейте клик с её аккаунта молча возвращается (как было с /myid)."""
    if not _is_curator(message.from_user.id):
        return
    await content_wipe_batch(BATCH)
    await curator_set_state(message.from_user.id, None, None)
    for pos, (ext_id, channel, fmt, hyp, draft, visual, cta) in enumerate(ITEMS):
        await content_add_item(BATCH, ext_id, channel, fmt, hyp, draft, visual, cta, pos)
    await message.answer(
        f"Перезалила батч «{BATCH}»: {len(ITEMS)} единиц (новые призывы). "
        "Курировать — /curate.", parse_mode=None)


@curator_router.message(Command("curate"))
async def cmd_curate(message: Message):
    uid = message.from_user.id
    if not _is_curator(uid):
        return
    if await content_batch_size(BATCH) == 0:
        await message.answer(
            "Батч ещё не загружен. Сначала /curate_load.", parse_mode=None)
        return
    await message.answer(
        "Привет 🌑 Я накидала контент — пройдёмся по одному.\n"
        "По каждому: «✅ Беру» / «✏️ Правка» (скажешь, что поменять, или пришлёшь свой "
        "текст — перепишу) / «🔁 Вариант» / «❌ Не моё». «⏹ Стоп» — пауза, продолжим позже.",
        parse_mode=None)
    await _send_next(message.bot, message.chat.id, uid)


@curator_router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    uid = message.from_user.id
    if not _is_curator(uid):
        return
    state = await curator_get_state(uid)
    if not state or state.get("awaiting") != "edit":
        return
    item = await content_get_item(state.get("current_item"))
    await curator_set_state(uid, state.get("current_item"), None)
    if item:
        await _send_card(message.bot, message.chat.id, item, prefix="Отменила правку.")


@curator_router.message(Command("curate_status"))
async def cmd_status(message: Message):
    if not _is_curator(message.from_user.id):
        return
    c = await content_counts()
    q = await pq_counts()
    qline = ", ".join(f"{k}: {v}" for k, v in q.items()) or "пусто"
    await message.answer(
        f"Батч «{BATCH}»\n"
        f"⏳ pending: {c.get('pending', 0)}\n"
        f"✅ approved: {c.get('approved', 0)}\n"
        f"❌ rejected: {c.get('rejected', 0)}\n"
        f"📤 очередь публикации: {qline}",
        parse_mode=None)


@curator_router.message(Command("curate_export"))
async def cmd_export(message: Message):
    if not _is_curator(message.from_user.id):
        return
    rows = await content_approved_by_channel()
    if not rows:
        await message.answer("Одобренного пока нет.", parse_mode=None)
        return
    by_ch: dict[str, list] = {}
    for r in rows:
        by_ch.setdefault(r["channel"], []).append(r)
    for ch, items in by_ch.items():
        label = CHANNEL_LABEL.get(ch, ch)
        chunk = [f"=== {label} ({len(items)}) ==="]
        for it in items:
            chunk.append(f"\n— {it['ext_id']} ({it.get('fmt')}):\n{it.get('final') or it.get('draft')}")
            if it.get("visual"):
                chunk.append(f"🎨 {it['visual']}")
            if it.get("cta"):
                chunk.append(f"➡️ CTA: {it['cta']}")
        text = "\n".join(chunk)
        # Telegram лимит 4096 — режем по абзацам.
        for part in _split(text, 3800):
            await message.answer(part, parse_mode=None)
        # Pinterest — досылаем готовые брендовые картинки (по одной на пин).
        if ch == "pinterest":
            for it in items:
                await _send_pin_image(message.bot, message.chat.id, it)


@curator_router.message(Command("curate_publish"))
async def cmd_publish(message: Message):
    """Принудительно слить очередь автопостинга в TG-канал прямо сейчас."""
    if message.from_user.id != settings.tg_admin_id:
        return
    n = 0
    while True:
        row = await pq_next_queued("telegram")
        if not row:
            break
        try:
            await message.bot.send_message(settings.tg_channel_id, row["text"], parse_mode=None)
            await pq_mark_posted(row["id"])
            n += 1
        except Exception:
            logger.exception("curate_publish: post failed")
            break
    await message.answer(f"Опубликовано в канал: {n}.", parse_mode=None)


def _split(text: str, limit: int):
    out, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            out.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out


# ── Callback-кнопки ───────────────────────────────────────────────────────────

def _cid(callback: CallbackQuery) -> int:
    return callback.from_user.id


@curator_router.callback_query(F.data.startswith("cur:ok:"))
async def cb_ok(callback: CallbackQuery):
    if not _is_curator(_cid(callback)):
        await callback.answer(); return
    item_id = int(callback.data.split(":")[2])
    await content_decide(item_id, "approved")
    item = await content_get_item(item_id)
    if item and item["channel"] in AUTOPOST_CHANNELS:
        await pq_enqueue(item_id, item["channel"], item.get("final") or item.get("draft"))
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("✅ Беру")
    # Pinterest: сразу отдаём готовую брендовую картинку из тезиса.
    if item and item["channel"] == "pinterest":
        await _send_pin_image(callback.message.bot, callback.message.chat.id, item)
    await _send_next(callback.message.bot, callback.message.chat.id, _cid(callback))


@curator_router.callback_query(F.data.startswith("cur:no:"))
async def cb_no(callback: CallbackQuery):
    if not _is_curator(_cid(callback)):
        await callback.answer(); return
    item_id = int(callback.data.split(":")[2])
    await content_decide(item_id, "rejected")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("❌ Убрала")
    await _send_next(callback.message.bot, callback.message.chat.id, _cid(callback))


@curator_router.callback_query(F.data.startswith("cur:skip:"))
async def cb_skip(callback: CallbackQuery):
    if not _is_curator(_cid(callback)):
        await callback.answer(); return
    item_id = int(callback.data.split(":")[2])
    await content_defer(item_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("⏭ Потом")
    await _send_next(callback.message.bot, callback.message.chat.id, _cid(callback))


@curator_router.callback_query(F.data == "cur:stop")
async def cb_stop(callback: CallbackQuery):
    if not _is_curator(_cid(callback)):
        await callback.answer(); return
    await curator_set_state(_cid(callback), None, None)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "Окей, пауза. Продолжим, когда будет минута — /curate.", parse_mode=None)
    await callback.answer()


@curator_router.callback_query(F.data.startswith("cur:edit:"))
async def cb_edit(callback: CallbackQuery):
    if not _is_curator(_cid(callback)):
        await callback.answer(); return
    item_id = int(callback.data.split(":")[2])
    await curator_set_state(_cid(callback), item_id, "edit")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "✏️ Скажи, что поменять — своими словами (например «жёстче», «короче», "
        "«убери последний вопрос», «сделай про маму»). Или пришли сразу свой текст — "
        "перепишу в тон. /cancel — вернуться.", parse_mode=None)
    await callback.answer()


@curator_router.callback_query(F.data.startswith("cur:var:"))
async def cb_var(callback: CallbackQuery):
    if not _is_curator(_cid(callback)):
        await callback.answer(); return
    item_id = int(callback.data.split(":")[2])
    item = await content_get_item(item_id)
    if not item:
        await callback.answer(); return
    if not settings.gemini_key:
        await callback.answer("Нет ключа Gemini — вариант не сгенерить", show_alert=True)
        return
    await callback.answer("🔁 Генерю вариант…")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    try:
        variant = await _make_variant(item)
    except Exception:
        logger.exception("variant gen failed")
        await callback.message.answer(
            "Не вышло сгенерить вариант — попробуй ещё раз или поправь вручную (✏️).",
            parse_mode=None)
        await _send_card(callback.message.bot, callback.message.chat.id, item)
        return
    await content_set_final(item_id, variant)
    item = await content_get_item(item_id)
    await _send_card(callback.message.bot, callback.message.chat.id, item,
                     prefix="🔁 Другой вариант:")


# ── Живой текст в режиме правки → Gemini ─────────────────────────────────────

class _CuratorEditFilter(BaseFilter):
    """Перехватывает текст куратора ТОЛЬКО когда он в режиме правки (awaiting='edit').

    Наследование от BaseFilter обязательно — иначе aiogram 3 не await'ит async-фильтр
    (тот же урок, что в alena_chat._InAlenaFilter)."""
    async def __call__(self, message: Message) -> bool:
        if not message.text or message.text.startswith("/"):
            return False
        if not _is_curator(message.from_user.id):
            return False
        state = await curator_get_state(message.from_user.id)
        return bool(state and state.get("awaiting") == "edit")


@curator_router.message(F.text, _CuratorEditFilter())
async def on_curator_edit(message: Message):
    uid = message.from_user.id
    state = await curator_get_state(uid)
    item = await content_get_item(state.get("current_item")) if state else None
    if not item:
        await curator_set_state(uid, None, None)
        return
    if not settings.gemini_key:
        # Без модели — берём её текст как есть (она и так в тоне).
        await content_set_final(item["id"], message.text)
        await curator_set_state(uid, item["id"], None)
        upd = await content_get_item(item["id"])
        await _send_card(message.bot, message.chat.id, upd, prefix="✏️ Записала твой текст.")
        return
    await message.answer("Секунду, переписываю…", parse_mode=None)
    try:
        revised = await _apply_edit(item, message.text)
    except Exception:
        logger.exception("apply_edit failed")
        await curator_set_state(uid, item["id"], None)
        await _send_card(message.bot, message.chat.id, item,
                         prefix="Не получилось переписать — вот текущий вариант. "
                                "Можешь снова ✏️ или прислать свой текст.")
        return
    await content_set_final(item["id"], revised)
    await curator_set_state(uid, item["id"], None)
    upd = await content_get_item(item["id"])
    await _send_card(message.bot, message.chat.id, upd,
                     prefix="✏️ Поправила. Так берём? (можно ещё раз ✏️)")


# ── Планировщик: утренняя рассылка + дрип-автопостинг ────────────────────────

async def push_daily_batch(bot):
    """Раз в день в заданный час сам пишет куратору и начинает прогон батча."""
    if not settings.curator_id:
        return
    cid = settings.curator_id
    state = await curator_get_state(cid)
    if state and state.get("awaiting") == "edit":
        return  # не перебиваем активную правку
    today = datetime.now(_tz()).strftime("%Y-%m-%d")
    if state and state.get("last_pushed_date") == today:
        return  # сегодня уже слали
    item = await content_next_pending(BATCH)
    if not item:
        return  # курировать нечего
    await curator_mark_pushed(cid, today)
    try:
        await bot.send_message(
            cid,
            "Доброе утро 🌑 Свежая пачка контента готова. Пройдёмся?\n"
            "По каждому: ✅ Беру / ✏️ Правка / 🔁 Вариант / ❌ Не моё.",
            parse_mode=None)
        await _send_next(bot, cid, cid)
    except Exception:
        logger.exception("push_daily_batch: cannot DM curator %s", cid)


async def publish_tick(bot):
    """Дрип: публикует один одобренный TG-пост из очереди в канал за тик."""
    row = await pq_next_queued("telegram")
    if not row:
        return
    try:
        await bot.send_message(settings.tg_channel_id, row["text"], parse_mode=None)
        await pq_mark_posted(row["id"])
        logger.info("published queued post %s to channel", row["id"])
    except Exception:
        logger.exception("publish_tick: post failed")
