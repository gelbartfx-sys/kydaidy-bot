"""Ежедневный чек-ин + reveal — узел кольца (чинит корневой дефект банка).

Поток:
  send_checkin(bot, tg_id) / команда /checkin → вопрос «поворот друг к другу?» (Да/Пока нет).
  cb_checkin_answer → checkin_set (идемпотентно per день) →
    · соло (партнёра нет) → CHECKIN_SOLO, банк/ reveal не применимы;
    · ответил один → CHECKIN_WAIT (ждём партнёра);
    · ответили ОБА → 🔒 ПАРНЫЙ GATE: банк +1 ТОЛЬКО при both_yes (оба подтвердили
      поворот — канон «мне ОТВЕТИЛИ», не само-отчёт) + reveal обоим.

Это исправляет sixsec-дефект (банк рос на само-отчёт): подтверждённый партнёром
рост живёт ЗДЕСЬ, где есть второй голос.

Тексты — ПЛЕЙСХОЛДЕРЫ (smyslovik/Алёна финализируют, tone-gate). НЕ задеплоено.
Автопуш дневного вопроса обоим (планировщик) — follow-up: см. docs/AUDIT-koltso-tech.
parse_mode=None везде (контент может ломать Markdown).
"""
from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)

from database import (
    log_event, resolve_couple, bank_add, render_bank_card,
    couple_partner, checkin_set, checkin_day,
)

logger = logging.getLogger(__name__)
checkin_router = Router()

# ПЛЕЙСХОЛДЕРЫ — smyslovik/Алёна финализируют (tone-gate, анти-лайфкоучинг, к ОБОИМ)
CHECKIN_Q = ("Был ли сегодня поворот друг к другу — момент, когда вы правда "
             "услышали один другого?")
REVEAL_BOTH_YES = ("Вы оба отметили поворот сегодня. Это и есть вклад в общий банк — "
                   "не слова, а услышанность.")
REVEAL_MIXED = ("Один из вас отметил поворот, другой — пока нет. Так честнее, чем "
                "делать вид. Завтра новый день.")
CHECKIN_SOLO = ("Пока вы идёте в одиночку. Пригласите партнёра — тогда чек-ин "
                "раскрывается вдвоём, и банк начинает считать повороты.")
CHECKIN_WAIT = "Записал. Раскроем, когда ответите оба."


def _q_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да", callback_data="chk:a:yes"),
        InlineKeyboardButton(text="Пока нет", callback_data="chk:a:no"),
    ]])


async def send_checkin(bot, tg_id: int):
    """Отправить дневной вопрос чек-ина одному партнёру (для планировщика/CTA)."""
    await bot.send_message(tg_id, CHECKIN_Q, parse_mode=None, reply_markup=_q_keyboard())


@checkin_router.message(Command("checkin"))
async def cmd_checkin(message: Message):
    await message.answer(CHECKIN_Q, parse_mode=None, reply_markup=_q_keyboard())


@checkin_router.callback_query(F.data.startswith("chk:a:"))
async def cb_checkin_answer(cb: CallbackQuery):
    yes = cb.data.rsplit(":", 1)[1] == "yes"
    tg_id = cb.from_user.id
    couple = await resolve_couple(tg_id)
    await checkin_set(couple, tg_id, yes)
    try:
        await log_event(tg_id, "checkin_answer", "da" if yes else "net")
    except Exception:
        logger.debug("log_event checkin_answer failed", exc_info=True)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)  # закрыть кнопки
    except Exception:
        logger.debug("checkin close buttons failed", exc_info=True)

    partner = await couple_partner(couple, tg_id)
    if partner is None:  # соло — подтверждать некому
        await cb.message.answer(CHECKIN_SOLO, parse_mode=None)
        await cb.answer()
        return

    day = await checkin_day(couple)
    if not day["both_answered"]:
        await cb.message.answer(CHECKIN_WAIT, parse_mode=None)
        await cb.answer()
        return

    # 🔒 ПАРНЫЙ GATE: банк +1 за день ТОЛЬКО при подтверждении ОБОИХ.
    if day["both_yes"]:
        try:
            await bank_add(couple, "checkin", 1)  # идемпотентно (kind+day_key)
        except Exception:
            logger.warning("checkin bank_add failed (continuing)", exc_info=True)
    await _reveal(cb.bot, couple, (tg_id, partner), day["both_yes"])
    await cb.answer()


async def _reveal(bot, couple_id: int, member_ids, both_yes: bool):
    """Раскрыть итог дня ОБОИМ партнёрам + обновлённый банк."""
    text = REVEAL_BOTH_YES if both_yes else REVEAL_MIXED
    try:
        card = await render_bank_card(couple_id)
    except Exception:
        card = ""
        logger.warning("checkin reveal bank card failed (continuing)", exc_info=True)
    body = f"{text}\n\n{card}" if card else text
    for uid in set(member_ids):
        try:
            await bot.send_message(uid, body, parse_mode=None)
        except Exception:
            logger.warning("checkin reveal send failed for %s", uid, exc_info=True)


if __name__ == "__main__":
    # Само-проверка парного gate (нетривиальная логика без сети/бота): локальный sqlite.
    import asyncio
    import os
    import tempfile
    import database

    database.DB_PATH = tempfile.mktemp(suffix=".db")
    database.USE_D1 = False
    from database import (  # noqa: E402  (после подмены DB_PATH)
        init_db, resolve_couple as _rc, bank_add as _ba, get_bank as _gb,
        checkin_set as _cs, checkin_day as _cd, couple_partner as _cp, _exec,
    )

    async def _demo():
        await init_db()
        # Пара: инициатор 111 + партнёр 222 (222 привязан через atm_quiz.pair_src=111)
        await _exec("INSERT OR IGNORE INTO atm_quiz (tg_id, pair_src) VALUES (?, ?)", (222, 111))
        await _rc(111)
        assert (await _rc(222)) == 111, "партнёр должен резолвиться в couple инициатора"
        assert await _cp(111, 111) == 222 and await _cp(111, 222) == 111

        # Один ответил → банк 0 (подтверждать нечем)
        await _cs(111, 111, True)
        d = await _cd(111)
        assert d["n"] == 1 and not d["both_answered"], d
        assert (await _gb(111))["plus"] == 0

        # Оба «Да» → both_yes → gate начисляет +1
        await _cs(111, 222, True)
        d = await _cd(111)
        assert d["both_answered"] and d["both_yes"], d
        await _ba(111, "checkin", 1)  # то, что делает cb при both_yes
        assert (await _gb(111))["plus"] == 1, await _gb(111)

        # Идемпотентность: повтор ответа дня не перезаписывает, банк не двоится
        await _cs(111, 111, False)
        assert (await _cd(111))["both_yes"], "повторный ответ не должен менять первый"
        await _ba(111, "checkin", 1)
        assert (await _gb(111))["plus"] == 1

        # Смешанный день (другая пара): один Да, один Нет → not both_yes → банк 0
        await _exec("INSERT OR IGNORE INTO atm_quiz (tg_id, pair_src) VALUES (?, ?)", (444, 333))
        await _rc(333)
        await _rc(444)
        await _cs(333, 333, True)
        await _cs(333, 444, False)
        d = await _cd(333)
        assert d["both_answered"] and not d["both_yes"], d
        assert (await _gb(333))["plus"] == 0, "смешанный день не растит банк"

        # Соло: партнёра нет → None (gate/reveal не применимы)
        await _rc(555)
        assert await _cp(555, 555) is None

        print("checkin self-check OK: оба-да→банк+1; один→0; смешанный→reveal(0); "
              "соло→None; идемпотентно (gate=подтверждение партнёра, не само-отчёт)")

    asyncio.run(_demo())
    os.remove(database.DB_PATH)
