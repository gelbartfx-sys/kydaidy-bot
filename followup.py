"""Дожим после оффера (Волна 1: H6/H7) — серия из 3 касаний.

Рынок: первое касание через 30–60 мин работает в 3–4 раза лучше, чем через
сутки; серия из 3 возвращает 20–35% не купивших; дальше — стоп (не спамим).

Серия ставится в _after_close (alena_chat), когда оффер Клуба показан
не-участнице. Оплатившие отфильтровываются в самом запросе followups_due —
каждое касание перепроверяет оплату на момент отправки. Одна серия на
человека за всю жизнь. При активной встрече касание пропускается (не влезаем
в живой разговор — это лучше дожима).

Тон — строго бренд (docs/positioning): никакого «скидка сгорает», давления и
обещаний результата. Дедлайн — мягкий, «я отпускаю тему», не «ты теряешь».

Касание 1 (~45 мин) — ГОЛОСОВОЕ Алёны (личное, по вскрытому запросу/Тени).
Касание 2 (~24 ч)  — атмосфера Клуба (место, где можно не держать лицо).
Касание 3 (~72 ч)  — мягкий дедлайн: держу наш разговор под рукой ещё немного.
"""

from __future__ import annotations

import logging

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import settings
from database import (
    get_user, followups_due, followup_mark, ai_active_session, log_event,
)
from shadow_test import ARCHETYPES, decode_distribution, winner_from_counts
from alena_voice import send_voice_to

logger = logging.getLogger(__name__)

CLUB_URL = "https://t.me/tribute/app?startapp=sULY"


def _club_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✦ Клуб «Манифест» — 990 ₽/мес", url=CLUB_URL)],
    ])


# Экологичный триггер подписки на канал (мандат Кая 04.07): один раз, после
# оффера — в касании-45м, отдельным ТЕКСТОМ после голосового (не тратит квоту
# голосовых и не давит). Ништяк — бесплатные утренние аудио в самом канале.
_CHANNEL_NUDGE = (
    "И ещё. Каждое утро я выхожу в канале — короткий голос, бесплатно, "
    "для всех. Загляни:"
)


def _channel_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌿 Канал Алёны — @kydaidy",
                              url="https://t.me/kydaidy")],
    ])


def _archetype_name(u: dict | None) -> str | None:
    try:
        dist = (u or {}).get("shadow_dist")
        if not dist:
            return None
        counts = decode_distribution(dist)
        if not counts:
            return None
        return ARCHETYPES[winner_from_counts(counts)]["name"]
    except Exception:
        return None


def _touch1_text(u: dict | None) -> str:
    """Личное «я ещё подумала о тебе» — по вскрытому запросу, иначе по Тени."""
    request = ((u or {}).get("last_ai_request") or "").strip().rstrip(".")
    arch = _archetype_name(u)
    if request:
        hook = f"То, что открылось у тебя — «{request}» — оно после нашего разговора не отпустило и меня."
    elif arch:
        hook = f"Твоя {arch} после нашего разговора не выходила у меня из головы."
    else:
        hook = "Наш разговор после встречи не отпустил и меня."
    return (
        f"Это я. {hook} "
        "Такое не рассасывается от того, что мы один раз про него поговорили — "
        "оно ждёт, посмотришь ты в него дальше или снова накроешь крышкой. "
        "Я не тороплю. Просто знай: я рядом, если решишь не оставаться с этим одна. "
        "Появится вопрос по дороге — пиши прямо сюда, я отвечу."
    )


_TOUCH2_TEXT = (
    "Знаешь, что я замечаю в Клубе: почти каждая начинает с «у меня всё нормально».\n\n"
    "А потом — через неделю, через две — пишет в чат то, что не говорила никому. "
    "Не потому что я волшебная. Потому что появляется место, где можно "
    "перестать быть удобной.\n\n"
    "Твоё место там пока свободно. Есть сомнение или вопрос про это — спрашивай "
    "прямо здесь, отвечу сама.\n\n— Алёна"
)

_TOUCH3_TEXT = (
    "Я держу наш разговор и твой портрет под рукой ещё несколько дней — думала, "
    "ты вернёшься.\n\n"
    "Потом отпускаю — и тему, и тебя: навязываться не в моих правилах. "
    "Если внутри ещё звенит то, что мы задели — дверь вот она, открыта. "
    "Если отпустило — тоже честно, я рада. Что-то хочешь спросить перед решением "
    "— успеешь, просто напиши.\n\n— Алёна"
)


def _spoken(text: str) -> str:
    """Текст касания → устная версия для TTS: без переносов и подписи «— Алёна»."""
    return " ".join(text.replace("— Алёна", "").split())


async def run_followup_tick(bot) -> int:
    """Фоновый джоб: отправить готовые касания. Возвращает число отправленных."""
    if not settings.followup_enabled:
        return 0
    rows = await followups_due()
    sent = 0
    for r in rows:
        fid, tg_id, stage = r.get("fid"), r.get("tg_id"), int(r.get("stage") or 0)
        if not fid or not tg_id:
            continue
        # Живая встреча важнее дожима: не влезаем, касание помечаем пропущенным
        # (следующие стадии серии останутся).
        try:
            if await ai_active_session(tg_id):
                await followup_mark(fid, "skipped")
                continue
        except Exception:
            pass
        # Метим ДО отправки (антидубль: сбой доставки не должен крутить касание вечно).
        await followup_mark(fid, "sent")
        try:
            if stage == 1:
                u = await get_user(tg_id)
                text = _touch1_text(u)
                if not await send_voice_to(bot, tg_id, text, _club_kbd()):
                    await bot.send_message(tg_id, text + "\n\n— Алёна",
                                           reply_markup=_club_kbd(), parse_mode=None)
                try:
                    await bot.send_message(tg_id, _CHANNEL_NUDGE,
                                           reply_markup=_channel_kbd(),
                                           parse_mode=None)
                    await log_event(tg_id, "channel_nudge")
                except Exception:
                    logger.warning("channel nudge failed", exc_info=True)
            elif stage == 2:
                # Мандат Кая 03.07: все касания Алёны — голосом, фолбэк текст.
                if not await send_voice_to(bot, tg_id, _spoken(_TOUCH2_TEXT), _club_kbd()):
                    await bot.send_message(tg_id, _TOUCH2_TEXT,
                                           reply_markup=_club_kbd(), parse_mode=None)
            else:
                if not await send_voice_to(bot, tg_id, _spoken(_TOUCH3_TEXT), _club_kbd()):
                    await bot.send_message(tg_id, _TOUCH3_TEXT,
                                           reply_markup=_club_kbd(), parse_mode=None)
            await log_event(tg_id, f"followup_{stage}")
            sent += 1
        except Exception:
            logger.warning("followup send failed for %s stage %s", tg_id, stage,
                           exc_info=True)
    if sent:
        logger.info("followup: sent %s touch(es)", sent)
    return sent
