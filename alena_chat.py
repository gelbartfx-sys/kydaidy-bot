"""«Алёна на связи» — бесплатный AI-диалог, имитирующий подход Алёны.

Модель: одна встреча = один запрос; копаем до настоящего ответа.
Лимит — 3 встречи на скользящие 30 дней (whitelist — без лимита).
Списание — при старте встречи (кнопка «Начать»). Память диалога — в БД
(переживает рестарты Render). На исчерпании лимита — мягкий апселл на 1:1.

Роутер подключается в bot.py ДО основного router: текст-фильтр (активная
встреча) должен сработать раньше catch-all fallback.
"""

from __future__ import annotations

import json
import logging

import aiohttp
from aiogram import Router, F
from aiogram.filters import Command, BaseFilter
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)

from config import settings
from ai_quiz import BASE, TEXT_MODEL
from shadow_test import ARCHETYPES, decode_distribution, winner_from_counts
from database import (
    get_user,
    ai_active_session, ai_sessions_used_30d, ai_total_sessions,
    ai_open_session, ai_add_message, ai_get_messages, ai_bump_turns,
    ai_close_session,
)
from alena_persona import (
    build_system, DISCLAIMER, INTRO, CLOSE_MARK, is_crisis, CRISIS_REPLY,
)

logger = logging.getLogger(__name__)
alena_router = Router()

FREE_SESSIONS = 1          # бесплатных встреч на человека (пожизненно)
TURN_CAP = 20              # предохранитель: после стольких реплик — мягкое закрытие
HISTORY_LIMIT = 40         # сколько сообщений истории отдаём модели
ONE_ON_ONE_URL = "https://web.tribute.tg/p/vKG"
MANIFEST7_URL = "https://web.tribute.tg/p/vKD"


def _is_unlimited(user) -> bool:
    from handlers import _is_unlimited as _h  # late import: избегаем цикла
    return _h(user)


async def _remaining(user) -> int | None:
    """Сколько бесплатных встреч осталось; None — безлимит (whitelist).

    Лимит — пожизненный (1 на человека), считаем все встречи, не за окно.
    """
    if _is_unlimited(user):
        return None
    used = await ai_total_sessions(user.id)
    return max(0, FREE_SESSIONS - used)


# ── Клавиатуры ────────────────────────────────────────────────────────────────

def _start_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Начать встречу 🕯️", callback_data="alena:start")],
    ])


def _pause_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏸ завершить встречу", callback_data="alena:stop")],
    ])


def _menu_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌑 Узнать свою Тень", callback_data="quiz")],
        [InlineKeyboardButton(text="🛍️ Что доступно", callback_data="products")],
    ])


def _one_on_one_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Записаться к Алёне 1:1", url=ONE_ON_ONE_URL)],
    ])


def _m7_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Забрать «Манифест 7» — 1 990 ₽", url=MANIFEST7_URL)],
        [InlineKeyboardButton(text="Сессия 1:1 с живой Алёной", url=ONE_ON_ONE_URL)],
    ])


_EXHAUSTED_TEXT = (
    "Бесплатная встреча у нас уже была — одна на человека.\n\n"
    "Дальше это не разговор, а работа: карта 5 поворотов — воркбук + колода. "
    "А если хочешь глубже и со мной по-настоящему — живая сессия 1:1."
)


# ── Вход ──────────────────────────────────────────────────────────────────────

async def _entry(target: Message, user):
    """Показывает дисклеймер (один раз) + интро + кнопку «Начать»."""
    if await ai_active_session(user.id):
        await target.answer("Мы уже во встрече — просто пиши, я здесь.")
        return
    rem = await _remaining(user)
    if rem is not None and rem <= 0:
        await target.answer(_EXHAUSTED_TEXT, reply_markup=_one_on_one_kbd())
        return
    if await ai_total_sessions(user.id) == 0:
        await target.answer(DISCLAIMER)
    tail = "" if rem is None else "\n\n_(это твоя бесплатная встреча — одна на человека)_"
    await target.answer(INTRO + tail, reply_markup=_start_kbd())


@alena_router.message(Command("alena"))
async def cmd_alena(message: Message):
    await _entry(message, message.from_user)


@alena_router.callback_query(F.data == "alena")
async def cb_alena(callback: CallbackQuery):
    await _entry(callback.message, callback.from_user)
    await callback.answer()


@alena_router.callback_query(F.data == "alena:start")
async def cb_start(callback: CallbackQuery):
    user = callback.from_user
    if await ai_active_session(user.id):
        await callback.message.answer("Мы уже во встрече — пиши.")
        await callback.answer()
        return
    rem = await _remaining(user)
    if rem is not None and rem <= 0:
        await callback.message.answer(_EXHAUSTED_TEXT, reply_markup=_one_on_one_kbd())
        await callback.answer()
        return
    await ai_open_session(user.id)  # списание встречи — при старте
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "Я здесь.\n\nРасскажи, что привело — с чем ты сейчас?\n\n— Алёна",
        reply_markup=_pause_kbd(),
    )
    await callback.answer()


@alena_router.callback_query(F.data == "alena:stop")
async def cb_stop(callback: CallbackQuery):
    sess = await ai_active_session(callback.from_user.id)
    if sess:
        await ai_close_session(sess["id"])
    await callback.message.answer(
        "Хорошо. Встреча закрыта.\n\nВозвращайся, когда будешь готова к новой теме — /alena.\n\n— Алёна",
        reply_markup=_menu_kbd(),
    )
    await callback.answer()


# ── Свободный текст во время активной встречи → Gemini ────────────────────────

class _InAlenaFilter(BaseFilter):
    """Пропускает текст в проводник, только когда есть активная встреча.

    Наследование от BaseFilter обязательно — иначе aiogram 3 не await'ит
    async-фильтр (см. тот же урок в manifest7_guide._InGuideFilter).
    """
    async def __call__(self, message: Message) -> bool:
        if not message.text or message.text.startswith("/"):
            return False
        return await ai_active_session(message.from_user.id) is not None


async def _generate(history: list[dict], name, povorot, archetype,
                    force_close: bool) -> str:
    system = build_system(name, povorot, archetype, force_close)
    contents = [
        {"role": ("model" if m["role"] == "model" else "user"),
         "parts": [{"text": m["content"]}]}
        for m in history
    ]
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.85,
            "maxOutputTokens": 900,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    url = f"{BASE}/models/{TEXT_MODEL}:generateContent?key={settings.gemini_key}"
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload,
                          timeout=aiohttp.ClientTimeout(total=60)) as r:
            body = await r.json()
    if "candidates" not in body:
        raise RuntimeError(f"alena gen failed: {json.dumps(body)[:300]}")
    parts = body["candidates"][0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("alena gen empty")
    return text


@alena_router.message(F.text, _InAlenaFilter())
async def on_alena_talk(message: Message):
    user = message.from_user
    sess = await ai_active_session(user.id)
    if not sess:
        return
    sid = sess["id"]

    await ai_add_message(sid, user.id, "user", message.text)

    # Кризис — не зовём модель, сразу бережная эскалация (встреча остаётся открытой).
    if is_crisis(message.text):
        await message.answer(CRISIS_REPLY, parse_mode=None, reply_markup=_pause_kbd())
        return

    await ai_bump_turns(sid)

    if not settings.gemini_key:
        await message.answer(
            "Я тебя услышала. Сейчас не могу ответить развёрнуто — напиши @kydaidy.",
            parse_mode=None,
        )
        return

    u = await get_user(user.id)
    povorot = (u or {}).get("povorot")
    shadow = (u or {}).get("shadow_dist")
    archetype = None
    if shadow:
        counts = decode_distribution(shadow)
        if counts:
            archetype = ARCHETYPES[winner_from_counts(counts)]

    turns = (sess.get("turns") or 0) + 1
    force_close = turns >= TURN_CAP
    history = await ai_get_messages(sid, HISTORY_LIMIT)

    try:
        reply = await _generate(history, user.first_name, povorot, archetype, force_close)
    except Exception as e:
        logger.exception(f"alena talk failed for {user.id}: {e}")
        await message.answer(
            "Я тут — но прямо сейчас ответить не получается. "
            "Попробуй чуть позже или напиши @kydaidy.",
            parse_mode=None,
        )
        return

    closed = (CLOSE_MARK in reply) or force_close
    reply = reply.replace(CLOSE_MARK, "").strip()
    await ai_add_message(sid, user.id, "model", reply)

    if closed:
        await ai_close_session(sid)
        await message.answer(reply, parse_mode=None)
        await _after_close(message, user)
    else:
        await message.answer(reply, parse_mode=None, reply_markup=_pause_kbd())


async def _after_close(message: Message, user):
    rem = await _remaining(user)
    if rem is None:
        await message.answer(
            "Ещё разговор — просто /alena.", reply_markup=_menu_kbd())
        return
    # Бесплатная встреча использована → первая ступень продуктовой матрицы.
    await message.answer(
        "Это была твоя бесплатная встреча — одна на человека.\n\n"
        "Разговор показал, где ты. Если хочешь не разговор, а пройти весь путь — "
        "карта 5 поворотов: воркбук + колода «Карта перепутья».",
        reply_markup=_m7_kbd())
