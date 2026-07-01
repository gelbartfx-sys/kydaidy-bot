"""Hermes-руки: реактивация застрявших лидов (режим ревью).

OFF by default (settings.growth_agent_enabled). Бот НИКОГДА не пишет юзерам сам:
дневной джоб находит застрявших по 3 сегментам, генерит персональный нудж в тоне
Алёны, отправляет его Каю (tg_admin_id) карточкой с кнопками
«✅ Отправить» / «🔁 Другой вариант» / «❌ Не слать».
По ✅ — бот шлёт юзеру и метит reactivated_at (антиспам-кулдаун).

Сегменты («застрял»):
  quiz_no_alena — прошла тест Тени, но не открыла бесплатную AI-встречу /alena;
  alena_no_buy  — была на AI-встрече, но ничего не взяла (зовём в Клуб 990);
  club_churn    — была в Клубе и ушла (тёплая дверь обратно, без вины).

Тон строго анти-лайфкоучинг (как curator): лещ честности + механизм, без давления,
дефицита, «5 шагов», эзотерики, обещаний. Персональный нудж = одно короткое тёплое
сообщение, мягкая открытая дверь, без впаривания.

Роутер growth_router подключается в bot.py ПОСЛЕ curator_router/alena_router —
у него только callback-кнопки (текст-фильтров нет, конфликтов не создаёт).
"""

from __future__ import annotations

import json
import logging

import aiohttp
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)

from config import settings
from ai_quiz import BASE, TEXT_MODEL
from database import (
    growth_candidates, growth_add_draft, growth_get_draft, growth_set_status,
    growth_update_draft, mark_reactivated, growth_counts, get_user,
)

try:
    from shadow_test import ARCHETYPES, decode_distribution, winner_from_counts
except Exception:  # pragma: no cover — деградируем без обогащения архетипом
    ARCHETYPES, decode_distribution, winner_from_counts = {}, None, None

logger = logging.getLogger(__name__)
growth_router = Router()


SEGMENTS = {
    "quiz_no_alena": {
        "label": "прошла тест Тени, но не была на встрече",
        "goal": (
            "Позвать на БЕСПЛАТНУЮ личную AI-встречу (/alena): «ты узнала свою Тень, "
            "но мы с тобой так и не поговорили начистоту». Мягко: одно сообщение, "
            "тёплое, с открытой дверью, без давления. Не продаём ничего платного."
        ),
    },
    "alena_no_buy": {
        "label": "была на встрече, ничего не взяла",
        "goal": (
            "Бережно сослаться на то, что вскрылось на встрече (если дан её запрос — "
            "опереться на него), и предложить Клуб «Манифест» (990 ₽/мес) как шаг "
            "«быть рядом каждый день»: безлимит встреч + закрытый чат + эфиры + "
            "воркбук. Без впаривания: открытая дверь, не ультиматум."
        ),
    },
    "club_churn": {
        "label": "была в Клубе, ушла",
        "goal": (
            "Тёплая дверь обратно БЕЗ вины и давления: «заметила, что тебя нет рядом». "
            "Признать, что уйти — нормально; коротко напомнить, что Клуб ждёт, когда "
            "захочется вернуться. Никакого стыжения и «ты упускаешь»."
        ),
    },
}


# Сегменты, где нудж уходит юзеру АВТОМАТИЧЕСКИ (без ручного ревью Кая).
# Кай: включить авто-догон «поговорил-не-купил». Остальные — по-прежнему на ревью.
_AUTO_SEND_SEGMENTS = {"alena_no_buy"}


_TONE = (
    "Ты пишешь голосом Алёны Kyda Idy — анти-лайфкоучинг. Это ЛИЧНОЕ сообщение "
    "конкретной женщине в личку (не пост). Тон: тёплый «лещ честности», опора на "
    "механизм (психология, привязанность), без жалости и без сюсюканья.\n"
    "ЗАПРЕЩЕНО: давление, срочность/дефицит, «5/7 шагов», «формула», обещания "
    "результата, «ты упускаешь», стыжение, эзотерика, императивы (должна/обязана), "
    "токсичный позитив, «дорогая/милая», смайлов максимум один.\n"
    "Формат: 2–4 коротких предложения, как живое сообщение. Обращайся на «ты». "
    "Если дано имя — можно назвать по имени один раз. Заверши мягкой открытой дверью "
    "(вопрос или приглашение), без кнопочных формулировок.\n"
    "Верни ТОЛЬКО текст сообщения — без преамбулы, без кавычек-обёрток."
)


async def _gen(user_text: str, temperature: float = 0.85, max_tokens: int = 600) -> str:
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
        raise RuntimeError(f"growth gen failed: {json.dumps(body)[:300]}")
    parts = body["candidates"][0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("growth gen empty")
    return text


def _archetype_name(user: dict) -> str | None:
    dist = user.get("shadow_dist")
    if not dist or not (decode_distribution and winner_from_counts and ARCHETYPES):
        return None
    try:
        code = winner_from_counts(decode_distribution(dist))
        a = ARCHETYPES.get(code)
        return a["name"] if a else None
    except Exception:
        return None


def _context(user: dict, segment: str) -> str:
    seg = SEGMENTS[segment]
    bits = [f"СЕГМЕНТ: {seg['label']}.", f"ЦЕЛЬ СООБЩЕНИЯ: {seg['goal']}"]
    name = user.get("first_name")
    if name:
        bits.append(f"Имя: {name}.")
    arch = _archetype_name(user)
    if arch:
        bits.append(f"Её ведущая Тень по тесту: {arch}.")
    req = user.get("last_ai_request")
    if req:
        bits.append(f"Запрос, вскрытый на встрече: «{req}». Бережно обопрись на него.")
    dossier = user.get("dossier")
    if dossier:
        bits.append(
            f"ДОСЬЕ (что ты уже знаешь о ней с прошлых встреч): {dossier}. "
            "Пиши как та, кто её ПОМНИТ — сошлись на конкретное, что она приносила, "
            "не общими словами. Именно это делает сообщение живым, а не рассылкой.")
    bits.append("Напиши одно личное сообщение под эту ситуацию.")
    return "\n".join(bits)


async def _make_draft_text(user: dict, segment: str) -> str:
    return await _gen(_context(user, segment))


# ── Карточка ревью для Кая ────────────────────────────────────────────────────

def _review_kbd(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить", callback_data=f"gr:ok:{draft_id}"),
         InlineKeyboardButton(text="🔁 Другой вариант", callback_data=f"gr:var:{draft_id}")],
        [InlineKeyboardButton(text="❌ Не слать", callback_data=f"gr:no:{draft_id}")],
    ])


def _review_text(user: dict, segment: str, draft: str, prefix: str = "") -> str:
    seg = SEGMENTS.get(segment, {})
    who = user.get("first_name") or "—"
    uname = f"@{user['username']}" if user.get("username") else "—"
    head = (f"🤝 Реактивация · {seg.get('label', segment)}\n"
            f"Кому: {who} ({uname}, id {user.get('tg_id')})")
    body = [head, "", draft]
    if prefix:
        body = [prefix, ""] + body
    return "\n".join(body)


async def _send_review_card(bot, draft, user, prefix: str = ""):
    await bot.send_message(
        settings.tg_admin_id,
        _review_text(user, draft["segment"], draft.get("draft") or "", prefix),
        parse_mode=None, reply_markup=_review_kbd(draft["id"]))


# ── Дневной джоб: набрать кандидатов, сгенерить нуджи, отдать Каю на ревью ─────

async def run_growth_tick(bot, force: bool = False):
    """Раз в сутки (или по /growth force): готовит до growth_daily_limit черновиков
    и шлёт их Каю на ревью. force=True игнорирует флаг enabled (ручной запуск)."""
    if not (settings.growth_agent_enabled or force):
        return 0
    if not settings.tg_admin_id:
        return 0
    if not settings.gemini_key:
        logger.warning("growth tick: no gemini_key — skip")
        return 0

    limit = max(1, settings.growth_daily_limit)
    cooldown = settings.growth_cooldown_days
    made = 0
    # Раскидываем лимит по сегментам по кругу, чтобы охватить все три.
    order = list(SEGMENTS.keys())
    pools = {}
    for seg in order:
        pools[seg] = await growth_candidates(seg, cooldown, limit)

    idx = 0
    seen: set[int] = set()  # один юзер — максимум один черновик за прогон
    while made < limit and any(pools.values()):
        seg = order[idx % len(order)]
        idx += 1
        pool = pools.get(seg)
        if not pool:
            continue
        user = pool.pop(0)
        if user["tg_id"] in seen:
            continue
        seen.add(user["tg_id"])
        try:
            text = await _make_draft_text(user, seg)
        except Exception:
            logger.exception("growth draft gen failed for %s/%s", user.get("tg_id"), seg)
            continue
        draft = await growth_add_draft(user["tg_id"], seg, text)
        if not draft:
            continue
        if seg in _AUTO_SEND_SEGMENTS:
            # Авто-догон: шлём юзеру сразу, 1 нудж (без ревью). Cooldown уже учтён.
            try:
                await bot.send_message(user["tg_id"], text, parse_mode=None)
                await growth_set_status(draft["id"], "sent")
                await mark_reactivated(user["tg_id"])
                made += 1
            except Exception:
                logger.exception("growth auto-send failed for %s", user["tg_id"])
                await growth_set_status(draft["id"], "failed")
            continue
        try:
            await _send_review_card(bot, draft, user)
            made += 1
        except Exception:
            logger.exception("growth: cannot DM admin review card")
            break

    if made and (settings.growth_agent_enabled or force):
        try:
            await bot.send_message(
                settings.tg_admin_id,
                f"🤝 Hermes-руки: {made} черновик(ов) на ревью. "
                "✅ — бот отправит юзеру, ❌ — пропустим. Сводка: /growth",
                parse_mode=None)
        except Exception:
            pass
    return made


# ── Команды (админ) ────────────────────────────────────────────────────────────

@growth_router.message(Command("growth"))
async def cmd_growth(message: Message):
    """Админ: сводка по реактивации + ручной запуск ревью-тика (/growth run)."""
    if message.from_user.id != settings.tg_admin_id:
        return
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) > 1 and arg[1].strip().lower() in ("run", "force", "go"):
        await message.answer("Готовлю черновики реактивации…", parse_mode=None)
        n = await run_growth_tick(message.bot, force=True)
        if not n:
            await message.answer(
                "Никого подходящего сейчас нет (либо все на кулдауне, либо нет данных).",
                parse_mode=None)
        return
    c = await growth_counts()
    state = "включён ✅" if settings.growth_agent_enabled else "выключен (env GROWTH_AGENT_ENABLED=1)"
    await message.answer(
        f"🤝 Hermes-руки (реактивация)\n"
        f"Режим джоба: {state}\n"
        f"Лимит/прогон: {settings.growth_daily_limit} · кулдаун: {settings.growth_cooldown_days} дн.\n\n"
        f"Черновики: pending {c.get('pending', 0)} · "
        f"отправлено {c.get('sent', 0)} · отклонено {c.get('rejected', 0)}\n\n"
        f"Ручной прогон ревью: /growth run",
        parse_mode=None)


# ── Callback-кнопки ревью ──────────────────────────────────────────────────────

@growth_router.callback_query(F.data.startswith("gr:ok:"))
async def cb_send(callback: CallbackQuery):
    if callback.from_user.id != settings.tg_admin_id:
        await callback.answer(); return
    draft_id = int(callback.data.split(":")[2])
    draft = await growth_get_draft(draft_id)
    if not draft or draft["status"] != "pending":
        await callback.answer("Уже обработан", show_alert=True); return
    try:
        await callback.message.bot.send_message(
            draft["tg_id"], draft.get("draft") or "", parse_mode=None)
    except Exception:
        logger.exception("growth send to user %s failed", draft["tg_id"])
        await callback.answer("Не удалось отправить (юзер мог закрыть личку)", show_alert=True)
        await growth_set_status(draft_id, "failed")
        await mark_reactivated(draft["tg_id"])
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    await growth_set_status(draft_id, "sent")
    await mark_reactivated(draft["tg_id"])
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("✅ Отправлено")


@growth_router.callback_query(F.data.startswith("gr:no:"))
async def cb_reject(callback: CallbackQuery):
    if callback.from_user.id != settings.tg_admin_id:
        await callback.answer(); return
    draft_id = int(callback.data.split(":")[2])
    draft = await growth_get_draft(draft_id)
    if not draft:
        await callback.answer(); return
    await growth_set_status(draft_id, "rejected")
    # Кулдаун, чтобы джоб не предлагал этого же человека завтра снова.
    await mark_reactivated(draft["tg_id"])
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("❌ Не отправляем")


@growth_router.callback_query(F.data.startswith("gr:var:"))
async def cb_variant(callback: CallbackQuery):
    if callback.from_user.id != settings.tg_admin_id:
        await callback.answer(); return
    draft_id = int(callback.data.split(":")[2])
    draft = await growth_get_draft(draft_id)
    if not draft or draft["status"] != "pending":
        await callback.answer("Уже обработан", show_alert=True); return
    if not settings.gemini_key:
        await callback.answer("Нет ключа Gemini", show_alert=True); return
    await callback.answer("🔁 Генерю вариант…")
    user = await get_user(draft["tg_id"]) or {"tg_id": draft["tg_id"]}
    try:
        text = await _gen(_context(user, draft["segment"]), temperature=1.0)
    except Exception:
        logger.exception("growth variant gen failed")
        await callback.message.answer(
            "Не вышло сгенерить вариант — оставляю текущий.", parse_mode=None)
        return
    await growth_update_draft(draft_id, text)  # статус остаётся pending
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    upd = await growth_get_draft(draft_id)
    await _send_review_card(callback.message.bot, upd, user, prefix="🔁 Другой вариант:")
