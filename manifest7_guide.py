"""AI-проводник по воркбуку «Манифест 7».

Покупательница проходит 7 практик в чате: бот ведёт шаг за шагом
(контент — manifest7_guide_data.py), темп задаёт она («дальше»/«пауза»).
Свободный текст посреди практики → Gemini-ответ в tone of voice Алёны
(система — как в ai_quiz, анти-коучинг) с учётом практики и её Тени.

Доступ: покупка manifest_7 (purchases) или whitelist (handlers._is_unlimited).
Прогресс в БД (manifest7_guide) — переживает рестарты Render.
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
from ai_quiz import _TONE_SYSTEM, TEXT_MODEL, BASE
from shadow_test import ARCHETYPES, decode_distribution, winner_from_counts
from database import (
    get_user, get_user_purchases, get_active_subscription,
    guide_get_all, guide_get, guide_set_step, guide_complete,
)
from manifest7_guide_data import PRACTICES, INTRO, OUTRO_ALL_DONE

logger = logging.getLogger(__name__)
guide_router = Router()

# Практики-проводник — бонус Клуба «Манифест». Историческим покупателям
# разового «Манифеста 7» доступ тоже сохраняем.
GUIDE_PRODUCT = "manifest_7"


# ── Доступ ───────────────────────────────────────────────────────────────────

async def _has_access(user) -> bool:
    from handlers import _is_unlimited  # late import: избегаем цикла
    if _is_unlimited(user):
        return True
    if await get_active_subscription(user.id, "manifest_club"):
        return True
    purchases = await get_user_purchases(user.id) or []
    return any(p["product_code"] == GUIDE_PRODUCT for p in purchases)


NO_ACCESS_TEXT = (
    "Практики с проводником — бонус Клуба «Манифест» (воркбук + 7 практик "
    "+ эфир раз в неделю + безлимит «Алёны на связи»).\n\n"
    "Уже в Клубе, а доступ не открылся — напиши @kydaidy, разберёмся."
)


# ── Клавиатуры ───────────────────────────────────────────────────────────────

def _menu_keyboard(done: set[int]) -> InlineKeyboardMarkup:
    rows = []
    for n, p in PRACTICES.items():
        mark = "✓ " if n in done else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{n}. {p['title']} · {p['duration']}",
            callback_data=f"g:open:{n}",
        )])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _step_keyboard(n: int, step: int, last: bool) -> InlineKeyboardMarkup:
    nxt = "завершить 🌿" if last else "дальше →"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=nxt, callback_data=f"g:next:{n}:{step}")],
        [InlineKeyboardButton(text="⏸ пауза", callback_data="g:pause")],
    ])


# ── Меню практик ─────────────────────────────────────────────────────────────

@guide_router.message(Command("praktiki"))
async def cmd_praktiki(message: Message):
    if not await _has_access(message.from_user):
        await message.answer(NO_ACCESS_TEXT)
        return
    rows = await guide_get_all(message.from_user.id) or []
    done = {r["practice"] for r in rows if r.get("completed_at")}
    text = OUTRO_ALL_DONE if len(done) == len(PRACTICES) else INTRO
    await message.answer(text, reply_markup=_menu_keyboard(done))


@guide_router.callback_query(F.data == "g:menu")
async def cb_menu(callback: CallbackQuery):
    rows = await guide_get_all(callback.from_user.id) or []
    done = {r["practice"] for r in rows if r.get("completed_at")}
    await callback.message.answer(
        "Практики «Манифеста 7». Куда идти — выбираешь ты.",
        reply_markup=_menu_keyboard(done),
    )
    await callback.answer()


# ── Прохождение ──────────────────────────────────────────────────────────────

async def _send_step(message: Message, tg_id: int, n: int, step: int):
    p = PRACTICES[n]
    steps = p["steps"]
    if step >= len(steps):  # дошли до конца → closing
        await guide_complete(tg_id, n)
        kbd = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← К списку практик", callback_data="g:menu")],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
        ])
        await message.answer(p["closing"], reply_markup=kbd)
        return
    await guide_set_step(tg_id, n, step)
    await message.answer(
        steps[step],
        reply_markup=_step_keyboard(n, step, last=(step == len(steps) - 1)),
    )


@guide_router.callback_query(F.data.startswith("g:open:"))
async def cb_open(callback: CallbackQuery):
    if not await _has_access(callback.from_user):
        await callback.answer("Доступ — после покупки «Манифеста 7»", show_alert=True)
        return
    n = int(callback.data.split(":")[2])
    if n not in PRACTICES:
        await callback.answer("Нет такой практики", show_alert=True)
        return
    state = await guide_get(callback.from_user.id, n)
    step = 0
    if state and not state.get("completed_at") and state.get("step"):
        step = state["step"]  # продолжаем с места паузы
        await callback.message.answer("Продолжаем с того места, где остановились.")
    await _send_step(callback.message, callback.from_user.id, n, step)
    await callback.answer()


@guide_router.callback_query(F.data.startswith("g:next:"))
async def cb_next(callback: CallbackQuery):
    _, _, n, step = callback.data.split(":")
    n, step = int(n), int(step)
    if n not in PRACTICES:
        await callback.answer("Нет такой практики", show_alert=True)
        return
    # убираем кнопки у пройденного шага, чтобы не плодить «дальше»
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _send_step(callback.message, callback.from_user.id, n, step + 1)
    await callback.answer()


@guide_router.callback_query(F.data == "g:pause")
async def cb_pause(callback: CallbackQuery):
    kbd = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← К списку практик", callback_data="g:menu")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
    ])
    await callback.message.answer(
        "Хорошо. Я сохранила, где ты. Вернёшься — продолжим с этого места.\n\n— Алёна",
        reply_markup=kbd,
    )
    await callback.answer()


# ── Свободный текст посреди практики → Gemini ────────────────────────────────

async def _active_practice(tg_id: int) -> dict | None:
    """Последняя незавершённая практика с прогрессом (для контекста разговора)."""
    rows = await guide_get_all(tg_id) or []
    active = [r for r in rows if not r.get("completed_at")]
    if not active:
        return None
    return max(active, key=lambda r: str(r.get("updated_at") or ""))


class _InGuideFilter(BaseFilter):
    """Пропускает текст в проводник, только когда есть незавершённая практика.

    ВАЖНО: наследование от BaseFilter обязательно — иначе aiogram 3 не await'ит
    async-фильтр, получает объект-корутину (всегда truthy) и матчит ЛЮБОЙ текст,
    из-за чего guide_router (подключён первым) глотает /start и прочие команды.
    """
    async def __call__(self, message: Message) -> bool:
        if not message.text or message.text.startswith("/"):
            return False
        if not await _has_access(message.from_user):
            return False
        return await _active_practice(message.from_user.id) is not None


async def _guide_reply(user_text: str, practice_no: int, step: int,
                       name: str | None, shadow_dist: str | None) -> str:
    p = PRACTICES[practice_no]
    step_text = p["steps"][min(step, len(p["steps"]) - 1)]
    shadow_note = ""
    if shadow_dist:
        counts = decode_distribution(shadow_dist)
        if counts:
            a = ARCHETYPES[winner_from_counts(counts)]
            shadow_note = (
                f"Её ведущая Тень по тесту — «{a['name']}» ({a['too']}): {a['essence']} "
                "Можешь бережно опереться на это, если уместно — не обязана."
            )
    who = f"Её зовут {name}. " if name else ""
    prompt = (
        f"{who}Женщина проходит практику {practice_no} «{p['title']}» "
        f"({p['subtitle']}) из воркбука «Манифест 7». Текущий шаг практики:\n"
        f"«{step_text}»\n\n{shadow_note}\n\n"
        f"Посреди практики она написала тебе:\n«{user_text}»\n\n"
        "Ответь ей как Алёна: коротко (300–600 знаков), по сути её слов, бережно "
        "и конкретно. Если ей тяжело — признай это без жалости и напомни, что можно "
        "остановиться. Ничего не продавай, никуда не записывай. Без списков, без "
        "заголовков, без markdown. В конце мягко предложи вернуться к практике, "
        "когда будет готова — без давления."
    )
    payload = {
        "systemInstruction": {"parts": [{"text": _TONE_SYSTEM}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.8,
            "maxOutputTokens": 800,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    url = f"{BASE}/models/{TEXT_MODEL}:generateContent?key={settings.gemini_key}"
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload,
                          timeout=aiohttp.ClientTimeout(total=45)) as r:
            body = await r.json()
    if "candidates" not in body:
        raise RuntimeError(f"guide reply failed: {json.dumps(body)[:300]}")
    parts = body["candidates"][0].get("content", {}).get("parts", [])
    text = "".join(x.get("text", "") for x in parts).strip()
    if not text:
        raise RuntimeError("guide reply empty")
    return text


@guide_router.message(F.text, _InGuideFilter())
async def on_guide_talk(message: Message):
    tg_id = message.from_user.id
    state = await _active_practice(tg_id)
    if not state:
        return
    n, step = state["practice"], state.get("step") or 0

    if not settings.gemini_key:
        reply = ("Я тебя услышала. Сейчас не могу ответить развёрнуто — "
                 "но ты можешь написать @kydaidy напрямую.")
    else:
        try:
            user = await get_user(tg_id)
            shadow_dist = (user or {}).get("shadow_dist")
            reply = await _guide_reply(
                message.text, n, step, message.from_user.first_name, shadow_dist)
        except Exception as e:
            logger.exception(f"guide talk failed for {tg_id}: {e}")
            reply = ("Я тебя услышала — но прямо сейчас ответить не получается. "
                     "Попробуй чуть позже или напиши @kydaidy.")

    kbd = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="продолжить практику →", callback_data=f"g:open:{n}")],
        [InlineKeyboardButton(text="← К списку практик", callback_data="g:menu"),
         InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
    ])
    await message.answer(reply, parse_mode=None, reply_markup=kbd)
