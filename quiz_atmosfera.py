"""Тест «Атмосфера дома» (пивот E1, T1): 12 вопросов × шкала 1–5, 4 опоры.

Флоу соло: /start test|yt*|pin* или /dom → интро → 12 вопросов в ОДНОМ
редактируемом сообщении → результат (сильная+слабая опора, META, инструмент
на вечер) → приглашение партнёра (deeplink pair_<tg_id>).

Флоу пары: /start pair_<uid> → тот же тест; по завершении обоим — gap-карта
(баллы по 4 опорам, акцент на макс. разрыве, общий вечерний шаг).

Next-day: тик планировщика ~20 ч после прохождения шлёт NEXTDAY_QUESTION
(механика как followup: метка ДО отправки — антидубль).

Тексты — quiz_atmosfera_data.py (контракт данных, пишется смысловиком).
Все тексты шлём parse_mode=None: контент может содержать Markdown-ломающие
символы, дефолт бота — Markdown.
"""

from __future__ import annotations

import json
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)

from database import (
    upsert_user, log_event,
    atm_save_result, atm_get_result, atm_nextday_due, atm_mark_nextday,
)
from quiz_atmosfera_data import (
    QUESTIONS, OPORA_NAMES, RESULTS_WEAK, RESULTS_STRONG, META_TEXT, TONIGHT,
    NEXTDAY_QUESTION, NEXTDAY_YES, NEXTDAY_NO, INVITE_OFFER, INVITE_FORWARD,
    PAIR_INTRO, PAIR_ROW, PAIR_GAP_ACCENT, PAIR_TONIGHT, SCALE_LABELS,
)

logger = logging.getLogger(__name__)
atm_router = Router()

OPORAS = ("teplo", "dogovor", "celi", "talanty")

# In-memory прохождение (паттерн _pending_shadow в handlers.py): тест идёт в
# одной сессии за ~2 минуты; рестарт контейнера посреди → мягкий рестарт теста.
# {tg_id: {"idx": int, "answers": {qid: score}, "pair_src": int|None, "source": str|None}}
_active: dict[int, dict] = {}

# ponytail: интро не входит в контракт данных (смысловик отдаёт только ключи из
# ТЗ) — 2 строки живут здесь; переносить в data-файл, если смысловик добавит ключ.
_INTRO = (
    "Тест «Атмосфера дома» — 12 коротких вопросов, около 2 минут.\n"
    "Покажет, на какой из 4 опор держится ваш дом — и какая просела."
)


def _start_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Начать", callback_data="atmq:go")],
    ])


def _q_text(idx: int) -> str:
    return f"{idx + 1}/{len(QUESTIONS)}\n\n{QUESTIONS[idx]['text']}"


def _q_kbd(idx: int) -> InlineKeyboardMarkup:
    # SCALE_LABELS — list[5], индекс 0 = оценка 1. Кнопки столбиком: подписи
    # длинные («1 · совсем не про нас»), в одном ряду Telegram их обрежет.
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(SCALE_LABELS[i - 1]),
                              callback_data=f"atmq:a:{idx}:{i}")]
        for i in range(1, 6)
    ])


def _scores(answers: dict) -> dict:
    """Сумма по опоре (3 вопроса × 1–5 = 3–15)."""
    s = {o: 0 for o in OPORAS}
    for q in QUESTIONS:
        s[q["opora"]] += int(answers.get(q["id"], 0))
    return s


async def start_atm_quiz(message: Message, source: str | None = None,
                         pair_src: int | None = None):
    """Вход в тест (из deeplink-хендлера /start или /dom). Retake разрешён."""
    tg_id = message.from_user.id
    if pair_src == tg_id:
        pair_src = None  # кликнул собственную парную ссылку → обычное соло
    _active[tg_id] = {"idx": 0, "answers": {}, "pair_src": pair_src,
                      "source": source}
    try:
        await log_event(tg_id, "atm_quiz_start", source)
    except Exception:
        logger.debug("log_event atm_quiz_start failed", exc_info=True)
    await message.answer(_INTRO, parse_mode=None, reply_markup=_start_kbd())


@atm_router.message(Command("dom"))
async def cmd_dom(message: Message):
    u = message.from_user
    await upsert_user(u.id, u.username, u.first_name)
    await start_atm_quiz(message)


@atm_router.callback_query(F.data == "atmq:go")
async def cb_go(cb: CallbackQuery):
    st = _active.get(cb.from_user.id)
    if st is None:
        # Рестарт контейнера между интро и стартом — начинаем чистое соло.
        st = _active[cb.from_user.id] = {"idx": 0, "answers": {},
                                         "pair_src": None, "source": None}
    st["idx"], st["answers"] = 0, {}
    await cb.message.edit_text(_q_text(0), parse_mode=None,
                               reply_markup=_q_kbd(0))
    await cb.answer()


@atm_router.callback_query(F.data.startswith("atmq:a:"))
async def cb_answer(cb: CallbackQuery):
    st = _active.get(cb.from_user.id)
    if st is None:
        await cb.answer("Тест сбросился — начни заново: /dom", show_alert=True)
        return
    try:
        _, _, idx_s, score_s = cb.data.split(":")
        idx, score = int(idx_s), int(score_s)
    except ValueError:
        await cb.answer()
        return
    if idx != st["idx"] or not 1 <= score <= 5:
        await cb.answer()  # двойной тап / кнопка старого вопроса
        return
    st["answers"][QUESTIONS[idx]["id"]] = score
    st["idx"] += 1
    if st["idx"] < len(QUESTIONS):
        await cb.message.edit_text(_q_text(st["idx"]), parse_mode=None,
                                   reply_markup=_q_kbd(st["idx"]))
        await cb.answer()
        return
    await cb.answer()
    try:
        await _finish(cb.message, cb.from_user.id, st)
    finally:
        _active.pop(cb.from_user.id, None)


async def _finish(msg: Message, tg_id: int, st: dict):
    """Подсчёт, сохранение, выдача результата (+ парная gap-карта / приглашение)."""
    scores = _scores(st["answers"])
    weak = min(OPORAS, key=lambda o: scores[o])
    strong = max(OPORAS, key=lambda o: scores[o])
    try:
        await atm_save_result(tg_id, json.dumps(st["answers"]),
                              json.dumps(scores), weak, st.get("pair_src"))
    except Exception:
        logger.warning("atm_save_result failed (continuing)", exc_info=True)
    try:
        await log_event(tg_id, "atm_quiz_done", st.get("source"))
    except Exception:
        logger.debug("log_event atm_quiz_done failed", exc_info=True)

    result = (
        f"Твоя сильная опора — {OPORA_NAMES[strong]} ({scores[strong]}/15).\n"
        f"{RESULTS_STRONG[strong]}\n\n"
        f"Слабая опора — {OPORA_NAMES[weak]} ({scores[weak]}/15).\n"
        f"{RESULTS_WEAK[weak]}\n\n"
        f"{META_TEXT}"
    )
    await msg.edit_text(result, parse_mode=None)  # финал — в то же сообщение
    await msg.answer(TONIGHT[weak], parse_mode=None)
    try:
        await log_event(tg_id, "atm_tool_shown", weak)
    except Exception:
        logger.debug("log_event atm_tool_shown failed", exc_info=True)

    if st.get("pair_src"):
        await _send_pair_card(msg.bot, st["pair_src"], tg_id, scores)
    else:
        await msg.answer(
            INVITE_OFFER, parse_mode=None,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Пригласить партнёра",
                                      callback_data="atmq:invite")],
            ]))


def _pair_card_text(mine: dict, theirs: dict, gap_o: str) -> str:
    """Текст gap-карты для ОДНОГО получателя: «ты» = mine, «партнёр» = theirs."""
    lines = [PAIR_INTRO, ""]
    lines += [PAIR_ROW.format(opora=OPORA_NAMES[o], a=mine[o], b=theirs[o])
              for o in OPORAS]
    lines += ["", PAIR_GAP_ACCENT.format(opora=OPORA_NAMES[gap_o]),
              "", PAIR_TONIGHT]
    return "\n".join(lines)


async def _send_pair_card(bot, src_id: int, partner_id: int,
                          partner_scores: dict):
    """Gap-карта пары обоим: баллы по опорам, макс. разрыв, общий вечерний шаг."""
    row = await atm_get_result(src_id)
    if not row or not row.get("scores"):
        logger.warning("pair card: initiator %s has no result", src_id)
        return
    try:
        a = json.loads(row["scores"])
    except Exception:
        logger.warning("pair card: bad scores json for %s", src_id, exc_info=True)
        return
    src = {o: int(a.get(o) or 0) for o in OPORAS}
    prt = {o: int(partner_scores.get(o) or 0) for o in OPORAS}
    gap_o = max(OPORAS, key=lambda o: abs(src[o] - prt[o]))
    # Карта ПЕРСОНАЛЬНАЯ (приёмка T1): «ты» в PAIR_ROW = баллы получателя.
    # Разрыв/акцент/вечерний шаг симметричны — одинаковы в обеих картах.
    for uid, text in ((src_id, _pair_card_text(src, prt, gap_o)),
                      (partner_id, _pair_card_text(prt, src, gap_o))):
        try:
            await bot.send_message(uid, text, parse_mode=None)
        except Exception:
            logger.warning("pair card send failed for %s", uid, exc_info=True)
    try:
        await log_event(partner_id, "atm_pair_done", str(src_id))
    except Exception:
        logger.debug("log_event atm_pair_done failed", exc_info=True)


@atm_router.callback_query(F.data == "atmq:invite")
async def cb_invite(cb: CallbackQuery):
    me = await cb.bot.me()  # кэшируется aiogram'ом
    link = f"https://t.me/{me.username}?start=pair_{cb.from_user.id}"
    try:
        await log_event(cb.from_user.id, "atm_pair_invite")
    except Exception:
        logger.debug("log_event atm_pair_invite failed", exc_info=True)
    await cb.message.answer(INVITE_FORWARD.format(link=link), parse_mode=None)
    await cb.answer("Перешли это сообщение партнёру")


# ── Next-day чек (~20 ч после прохождения) ────────────────────────────────────

async def run_atm_nextday_tick(bot) -> int:
    """Тик планировщика: NEXTDAY_QUESTION всем, кто прошёл тест ≥20 ч назад.
    Метка ДО отправки (антидубль, как followup). Крэш-сейф."""
    rows = await atm_nextday_due(hours=20)
    sent = 0
    for r in rows:
        tg = r.get("tg_id")
        if not tg:
            continue
        await atm_mark_nextday(tg)
        try:
            await bot.send_message(
                tg, NEXTDAY_QUESTION, parse_mode=None,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Да", callback_data="atmq:nd:yes"),
                    InlineKeyboardButton(text="Пока нет",
                                         callback_data="atmq:nd:no"),
                ]]))
            sent += 1
        except Exception:
            logger.warning("atm nextday send failed for %s", tg, exc_info=True)
    if sent:
        logger.info("atm nextday: sent %s check(s)", sent)
    return sent


@atm_router.callback_query(F.data.startswith("atmq:nd:"))
async def cb_nextday(cb: CallbackQuery):
    yes = cb.data.endswith(":yes")
    try:
        await log_event(cb.from_user.id,
                        "atm_nextday_yes" if yes else "atm_nextday_no")
    except Exception:
        logger.debug("log_event atm_nextday failed", exc_info=True)
    await cb.message.edit_text(NEXTDAY_YES if yes else NEXTDAY_NO,
                               parse_mode=None)
    await cb.answer()


if __name__ == "__main__":
    # Само-проверка подсчёта (единственная нетривиальная логика без сети/бота).
    ans = {q["id"]: 5 if q["opora"] == "teplo" else (1 if q["opora"] == "celi" else 3)
           for q in QUESTIONS}
    s = _scores(ans)
    assert s["teplo"] == 15 and s["celi"] == 3 and s["dogovor"] == 9, s
    assert min(OPORAS, key=lambda o: s[o]) == "celi"
    assert max(OPORAS, key=lambda o: s[o]) == "teplo"
    assert len(QUESTIONS) == 12 and all(q["opora"] in OPORAS for q in QUESTIONS)

    # Персональность gap-карты (приёмка T1): два виртуальных юзера —
    # в карте каждого «ты» ({a}) = ЕГО баллы, «партнёр» ({b}) = баллы второго.
    init_s = {"teplo": 15, "dogovor": 9, "celi": 3, "talanty": 6}    # инициатор
    part_s = {"teplo": 4, "dogovor": 9, "celi": 12, "talanty": 6}    # партнёр
    gap = max(OPORAS, key=lambda o: abs(init_s[o] - part_s[o]))
    assert gap == "teplo", gap
    card_init = _pair_card_text(init_s, part_s, gap)
    card_part = _pair_card_text(part_s, init_s, gap)
    row_init = PAIR_ROW.format(opora=OPORA_NAMES["teplo"], a=15, b=4)
    row_part = PAIR_ROW.format(opora=OPORA_NAMES["teplo"], a=4, b=15)
    assert row_init in card_init and row_part not in card_init
    assert row_part in card_part and row_init not in card_part
    assert PAIR_GAP_ACCENT.format(opora=OPORA_NAMES[gap]) in card_init
    assert card_init.count(PAIR_TONIGHT) == 1 == card_part.count(PAIR_TONIGHT)
    print("quiz_atmosfera self-check OK:", s, "| pair cards personalized OK")
