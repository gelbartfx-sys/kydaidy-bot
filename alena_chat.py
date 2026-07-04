"""«Алёна на связи» — бесплатный AI-диалог, имитирующий подход Алёны.

Модель: одна встреча = один запрос; копаем до настоящего ответа.
Лимит — 1 бесплатная встреча на человека (пожизненно). Безлимит — whitelist
ИЛИ активная подписка Клуба «Манифест». Списание — при старте встречи.
Память диалога — в БД. На исчерпании — апселл в Клуб 990/мес (там безлимит).

Роутер подключается в bot.py ДО основного router: текст-фильтр (активная
встреча) должен сработать раньше catch-all fallback.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re

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
    get_user, get_active_subscription,
    ai_active_session, ai_total_sessions, ai_sessions_used_30d,
    ai_open_session, ai_add_message, ai_get_messages, ai_bump_turns,
    ai_close_session, ai_close_all_active, ai_session_idle_minutes,
    ai_set_last_request, save_dossier,
    ai_stale_sessions, ai_orphan_sessions, ai_mark_nudged, save_lead_signals, set_lead_track,
    get_client_model, save_client_model,
    log_event, followup_schedule, get_lead_signals, add_circle_credits,
    ai_last_session, events_count_recent, club_ladder_candidates,
)
from alena_voice import send_voice_reply, send_voice_to, send_kruzhok_to
from lead_policy import should_spend_circle, CIRCLE_CREDITS
from alena_persona import (
    build_system, DISCLAIMER, INTRO, CLOSE_MARK, is_crisis, CRISIS_REPLY,
    extract_request, extract_dossier, extract_score, strip_dangling_markers,
)
from alena_brain import brain_turn
from lead_policy import classify

logger = logging.getLogger(__name__)
alena_router = Router()

FREE_SESSIONS = 1          # бесплатных встреч на человека (пожизненно)
CLUB_MONTHLY_SESSIONS = 2  # встреч с AI-Алёной в месяц для членов Клуба (мандат Кая 03.07)

# Волна 1:1 (совещание 03.07): маркеры намерения клиентки, НЕ зависят от мозга v2.
# Границы слов — чтобы «отлично» не читалось как «лично».
_DEPTH_RE = re.compile(
    r"\b(глубже|вглубь|лично|личн(ая|ую) встреч\w*|один на один|сесси[юия]|"
    r"консультаци\w*|поработать с тобой|вживую|невыносимо|не справляюсь)\b", re.I)
_HOT_RE = re.compile(
    r"(сколько (это )?стоит|как (записаться|оплатить|купить)|куда платить|"
    r"запиши меня|хочу записаться|готова (платить|начать|записаться)|оплачу)", re.I)

TURN_CAP = 12              # предохранитель: после стольких реплик — мягкое закрытие
                           # (20→12 по прогону Кая 03.07: v1 жевал по кругу)
HISTORY_LIMIT = 40         # сколько сообщений истории отдаём модели
ONE_ON_ONE_URL = "https://web.tribute.tg/p/vKG"
CLUB_URL = "https://t.me/tribute/app?startapp=sULY"


def _is_unlimited(user) -> bool:
    from handlers import _is_unlimited as _h  # late import: избегаем цикла
    return _h(user)


async def _is_club_member(tg_id: int) -> bool:
    return await get_active_subscription(tg_id, "manifest_club") is not None


async def _remaining(user) -> int | None:
    """Сколько встреч осталось; None — безлимит (только whitelist).

    Клуб «Манифест» — 2 встречи с AI-Алёной в месяц (мандат Кая 03.07,
    скользящее окно 30 дней). Остальным — 1 бесплатная пожизненно.
    """
    if _is_unlimited(user):
        return None
    if await _is_club_member(user.id):
        used = await ai_sessions_used_30d(user.id)
        return max(0, CLUB_MONTHLY_SESSIONS - used)
    used = await ai_total_sessions(user.id)
    return max(0, FREE_SESSIONS - used)


# ── Клавиатуры ────────────────────────────────────────────────────────────────

def _start_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Начать встречу 🕯️", callback_data="alena:start")],
    ])


def _pause_kbd() -> None:
    # Во время встречи — НИКАКИХ кнопок вообще (фидбек Кая 02.07, две итерации:
    # сначала убрали Клуб-CTA, потом и «завершить» — она мозолила на каждом ходе).
    # Оффер живёт в своих моментах (закрытие/нудж/дожимы); выход — просто замолчать
    # (stale-нудж мягко закроет) или /start. Хендлер alena:stop оставлен для старых
    # сообщений с кнопкой.
    return None


def _club_only_kbd() -> InlineKeyboardMarkup:
    # Один CTA на пике — только Клуб (Hermes #3), без расщепления цен. 1:1 — текстом.
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✦ Войти в Клуб «Манифест» — 990 ₽/мес", url=CLUB_URL)],
    ])


def _menu_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌑 Узнать свою Тень", callback_data="quiz")],
        [InlineKeyboardButton(text="🛍️ Что доступно", callback_data="products")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
    ])


def _one_on_one_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Записаться к Алёне 1:1", url=ONE_ON_ONE_URL)],
    ])


def _club_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Войти в Клуб «Манифест» — 990 ₽/мес", url=CLUB_URL)],
        [InlineKeyboardButton(text="Сессия 1:1 с живой Алёной", url=ONE_ON_ONE_URL)],
    ])


def _bridge_kbd() -> InlineKeyboardMarkup:
    """Нативный мост: вскрытый запрос → 1:1 первым, Клуб — мягкой альтернативой."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Взять этот запрос на встречу 1:1", url=ONE_ON_ONE_URL)],
        [InlineKeyboardButton(text="Быть рядом регулярно — Клуб 990 ₽/мес", url=CLUB_URL)],
    ])


_EXHAUSTED_TEXT = (
    "Бесплатная встреча у нас уже была — вторых не даю.\n\n"
    "Хочешь продолжать со мной — это Клуб «Манифест»: две встречи со мной, как эта, в каждом "
    "месяце + утреннее аудио «Манифест дня» + живой эфир раз в неделю + "
    "закрытый чат круга + мои письма. 990 ₽/мес.\n\n"
    "А если тянет вглубь и лично — сессия 1:1."
)

_EXHAUSTED_VOICE = (
    "Бесплатная встреча у нас уже была — вторых не даю. Хочешь продолжать со "
    "мной — это Клуб «Манифест»: две встречи со мной, как эта, в каждом "
    "месяце, утреннее аудио «Манифест дня», живой эфир раз в неделю, "
    "закрытый чат круга и мои письма. А если тянет вглубь "
    "и лично — сессия один на один. Обе двери — под этим сообщением."
)


_EXHAUSTED_MEMBER_TEXT = (
    "Наши две встречи этого месяца уже были — новые придут со свежим месяцем.\n\n"
    "Я рядом каждый день: утренний «Манифест дня», эфир на неделе, чат круга.\n\n"
    "А если тянет глубже и лично прямо сейчас — сессия 1:1."
)

_EXHAUSTED_MEMBER_VOICE = (
    "Наши две встречи этого месяца уже были — новые придут со свежим месяцем. "
    "Я рядом каждый день: утренний «Манифест дня», эфир на неделе, чат круга. "
    "А если тянет глубже и лично прямо сейчас — возьми это на сессию один на "
    "один. Дверь — под этим сообщением."
)


def _one_on_one_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Взять на встречу 1:1", url=ONE_ON_ONE_URL)],
    ])


async def _send_exhausted(target: Message):
    """Оффер исчерпавшей лимит — голосом (мандат 03.07), фолбэк текст.
    Членам Клуба (2 встречи/мес исчерпаны) — свой вариант: без оффера Клуба,
    мост в 1:1 (мандат Кая 03.07)."""
    if await _is_club_member(target.chat.id):
        if not await send_voice_reply(target, _EXHAUSTED_MEMBER_VOICE, _one_on_one_kbd()):
            await target.answer(_EXHAUSTED_MEMBER_TEXT, reply_markup=_one_on_one_kbd())
        return
    if not await send_voice_reply(target, _EXHAUSTED_VOICE, _club_kbd()):
        await target.answer(_EXHAUSTED_TEXT, reply_markup=_club_kbd())


# ── Авто-контакт после теста Тени: Алёна заговаривает первой ──────────────────
# Хук-вопрос под каждую Тень — ДОСЛОВНО как в отрендеренных кружках (транскрибация
# 03.07, docs/hermes/funnel-fixes-2026-07-03.md). Кружок кончается этим вопросом →
# текст на экране и кнопки обязаны продолжать ИМЕННО его (фидбек Кая: «кнопки не
# сходятся с вопросом из кружка»). Менять только вместе с перегеном кружков.
_SHADOW_HOOK_Q = {
    "W": "От кого ты прячешь то, что видишь?",
    "Q": "Кому ты последний раз позволила подойти близко?",
    "H": "Иногда ты уходишь от того, от кого не хотела. От кого?",
    "M": "Перед кем ты в последний раз сделала себя тише, чем ты есть?",
    "F": "Кто видел тебя настоящую — без игры?",
    "MR": "Кому ты отдала больше, чем осталось себе?",
    "R": "Против чего ты бунтуешь так, что задевает саму себя?",
    "O": "Кого ты держишь на расстоянии, чтобы не потерять?",
    "D": "Что ты однажды сожгла — и до сих пор молчишь?",
    "C": "Кого ты не впустила внутрь, хотя, может, хотела?",
}


def _shadow_opener(code: str) -> str:
    a = ARCHETYPES[code]
    return (f"Вижу твою ведущую Тень — {a['name']}.\n\n{a['teaser']}\n\n"
            "Дальше — наша встреча: ты отвечаешь текстом или голосовым, я читаю и "
            "отвечаю; иногда мне нужна минута — я не исчезаю.\n\n"
            "Вопрос — под этим сообщением.\n\n— Алёна")


def _shadow_opener_short(code: str) -> str:
    # Текст-фолбэк голосового онбординга (слово «кружок» на экране допустимо).
    # Аудит 03.07 №4: без «не тот, что сверху», без «Смысл:»/«Правила простые».
    return (
        "Это твоя пробная встреча со мной — одна, бесплатная, как разговор "
        "один на один.\n\n"
        "Твой тест показал, ГДЕ ты закрываешься. Здесь найдём, ЧТО за этим "
        "прячется — то настоящее, что обычно молчит под защитой.\n\n"
        "Отвечай честно, как есть — текстом или голосовым. Я читаю, думаю и "
        "отвечаю, чаще голосом. Замолчу на минуту — не исчезла, думаю о тебе.\n\n"
        "Вопрос из кружка — под этим сообщением, там же подсказки.\n\n— Алёна")


async def open_shadow_session(target: Message, user, code: str,
                              video_hook: bool = False) -> bool:
    """Сразу после портрета Тени — Алёна САМА открывает встречу с хуком под архетип.

    True  → встреча открыта (дальше говорит on_alena_talk, архетип уже втекает),
            либо исчерпавшей выдан хук-тизер + оффер Клуба;
    False → авто-контакт невозможен (нет ключа) → вызывающий покажет обычное меню.
    """
    if not settings.gemini_key:
        return False
    sess = await ai_active_session(user.id)
    if sess:
        # Баг прогона №3 (03.07 08:09): встреча, убитая редеплоем, висела
        # «active» — новый путь упирался в неё, и после кружка была ТИШИНА.
        # Живую беседу (<15 мин активности) не дублируем, но и не молчим;
        # осиротевшую — закрываем и открываем путь заново.
        idle = await ai_session_idle_minutes(sess["id"])
        if idle is not None and idle < 15:
            await target.answer("Мы уже во встрече — просто продолжай, я здесь.")
            return True
        try:
            await ai_close_all_active(user.id)
            await log_event(user.id, "session_reopen_stale", str(sess["id"]))
        except Exception:
            logger.warning("stale session close failed", exc_info=True)
            return True  # закрыть не смогли — не плодим вторую активную
    rem = await _remaining(user)
    if rem is not None and rem <= 0:
        # Член Клуба, исчерпавший 2 встречи месяца → свой вариант (1:1, без
        # оффера Клуба самой себе).
        if await _is_club_member(user.id):
            await _send_exhausted(target)
            return True
        # бесплатная встреча исчерпана → хук как тизер + Клуб (без траты модели).
        # Голосом (мандат 03.07), фолбэк текст.
        a = ARCHETYPES[code]
        spent = (f"Твоя Тень — {a['name']}. {a['teaser']} Мы это уже начинали "
                 "разбирать. Продолжить — в Клубе «Манифест»: две встречи со "
                 "мной в месяц, утреннее аудио, живой эфир раз в неделю и "
                 "закрытый чат круга. Дверь — "
                 "под этим сообщением.")
        if not await send_voice_reply(target, spent, _club_kbd()):
            await target.answer(
                f"Твоя Тень — {a['name']}. {a['teaser']}\n\n"
                "Мы это уже начинали разбирать. Продолжить — в Клубе «Манифест»: "
                "две встречи со мной в месяц + утреннее аудио «Манифест дня» + "
                "живой эфир раз в неделю + закрытый чат круга. 990 ₽/мес.\n\n— Алёна",
                reply_markup=_club_kbd(), parse_mode=None)
        return True
    await ai_open_session(user.id)  # списание бесплатной встречи — здесь
    await log_event(user.id, "session_open", "auto")
    if await ai_total_sessions(user.id) <= 1:
        await target.answer(DISCLAIMER)
    # W2: рамку «пробная сессия» Алёна ГОВОРИТ голосом (канал задан с первой
    # секунды); вопрос — текстом перед глазами + W1: кнопки первого шага (барьер
    # чистого листа — главная утечка контакта). Сбой TTS → прежний текст-опенер.
    opener = _shadow_opener_short(code) if video_hook else _shadow_opener(code)
    q = _SHADOW_HOOK_Q.get(code, "Что у тебя сейчас болит на самом деле?")
    name = user.first_name if re.search(r"[а-яА-ЯёЁ]", user.first_name or "") else None
    who = f"{name}, это" if name else "Это"
    # ⚠️ В озвучке слова «кружок/кружке» НЕТ нигде: TTS читает «в крУжке» (как
    # кружка-чашка) — фидбек Кая 03.07. В голосе говорим «видео»/«на видео».
    # Тексты — аудит логики 03.07 (№1–2): без «не тот, что сверху» (запроса ещё
    # не было — якорим к тесту), без «правила простые», с «о себе» через историю.
    # Правило №1 Кая (03.07): биография прозвучала в сообщении с Тенью (handlers)
    # — тут её НЕТ. Этот голос владеет рамкой: «пробная/одна», формат, что дальше.
    if video_hook:
        hello = (f"{who} была я — только что, на видео. Вот и познакомились "
                 "по-настоящему. Теперь — про то, что сейчас будет. Это твоя "
                 "пробная встреча: она у нас одна, как живой разговор с глазу "
                 "на глаз. Тест показал, где ты закрываешься, — а мы поищем, "
                 "что у тебя там, под замком, на самом деле. Говори как есть — "
                 "голосом или текстом. Я отвечаю вслух. Замолчу на минуту — не "
                 "исчезла, просто думаю над твоими словами. Вопрос ты уже "
                 "слышала — он ждёт под этим сообщением, вместе с подсказками: "
                 "выбери ту, что отзывается, или скажи по-своему. А в конце "
                 "подведём главное.")
    else:
        # Кружка нет → обещание из сообщения с Тенью («покажу кое-что личное
        # про неё») исполняем тут тизером архетипа (методолог 03.07, №4).
        t = ARCHETYPES[code]["teaser"]
        hello = (f"Сначала — то, что обещала: личное, про твою Тень. {t} "
                 "Теперь — о том, как всё устроено. Это твоя пробная встреча: "
                 "она у нас одна, как живой разговор с глазу на глаз. Тест "
                 "показал, где ты закрываешься, — поищем, что там, под замком. "
                 "Говори как есть — голосом или текстом. Я отвечаю вслух. "
                 "Замолчу на минуту — не пропала, просто думаю. Мой первый "
                 "вопрос — ниже. " + q)
    if await send_voice_reply(target, hello):
        await log_event(user.id, "voice_reply", "opener")
    else:
        await _send_alive(target, opener)
    # Вопрос Алёны — В ИСТОРИЮ сессии (model-реплика): без этого первый ход
    # клиентки прилетал мозгу без контекста — он не знал, на ЧТО она отвечает
    # (корень «кнопки/ответы не сходятся с вопросом», фидбек Кая 03.07).
    try:
        sess = await ai_active_session(user.id)
        if sess:
            await ai_add_message(sess["id"], user.id, "model", f"«{q}»")
    except Exception:
        logger.warning("seed opener question to history failed", exc_info=True)
    # Вопрос + кнопки первого шага — ВСЕГДА отдельным сообщением (и при живом
    # голосе, и при текст-фолбэке), с собственной страховкой: сбой клавиатуры не
    # должен оставить человека без вопроса (фидбек Кая: «кнопки не появлялись»).
    try:
        await target.answer(
            f"Вот он, перед глазами:\n\n«{q}»\n\n"
            "Твой ход 🎙 — или коснись подсказки ниже:",
            parse_mode=None, reply_markup=_first_step_kbd(code))
        await log_event(user.id, "first_step_kbd")
    except Exception:
        logger.warning("first-step kbd failed (plain fallback)", exc_info=True)
        await target.answer(f"Вот он, перед глазами:\n\n«{q}»\n\n"
                            "Твой ход — текстом или голосовым.", parse_mode=None)
    return True


# W1: кнопки первого шага — тап = первый ответ сделан, дальше говорит сама.
# Пер-Тень (фидбек Кая 03.07: подсказки обязаны отвечать на вопрос ИЗ кружка).
# Формат: код Тени → 3 × (label кнопки, полная реплика клиентки для мозга).
_FIRST_STEPS_BY_SHADOW: dict[str, list[tuple[str, str]]] = {
    # W: «От кого ты прячешь то, что видишь?»
    "W": [("от того, кто рядом", "От того, кто рядом со мной. Ему я это не показываю."),
          ("от родных", "От родных. Им нельзя видеть, что я всё считываю."),
          ("ото всех", "Ото всех, наверное. Так спокойнее — не быть слишком.")],
    # Q: «Кому ты последний раз позволила подойти близко?»
    "Q": [("уже не помню", "Если честно — уже не помню. Это было давно."),
          ("был один человек", "Был один человек. Но это плохо кончилось."),
          ("никому не позволяю", "Никому. Я не подпускаю близко.")],
    # H: «Иногда ты уходишь от того, от кого не хотела. От кого?»
    "H": [("был такой человек", "Был такой человек. Я ушла первой, хотя не хотела."),
          ("ухожу всегда я", "Ухожу всегда я. Не жду, пока уйдут от меня."),
          ("не хочу вспоминать", "Есть от кого. Но вспоминать это тяжело.")],
    # M: «Перед кем ты в последний раз сделала себя тише, чем ты есть?»
    "M": [("перед партнёром", "Перед мужчиной, который рядом. С ним я тише, чем я есть."),
          ("перед семьёй", "Перед семьёй. С ними я всю жизнь приглушаю себя."),
          ("я всегда тише", "Да я всегда тише, чем есть. Уже привычка.")],
    # F: «Кто видел тебя настоящую — без игры?»
    "F": [("никто", "Никто, наверное. Все видели только игру."),
          ("один человек — давно", "Один человек видел. Но это было давно."),
          ("я и сама не видела", "Я и сама себя настоящую давно не видела.")],
    # MR: «Кому ты отдала больше, чем осталось себе?»
    "MR": [("партнёру", "Мужчине. Я растворилась в нём почти до нуля."),
           ("семье", "Семье. Всю себя раздала — им."),
           ("всем понемногу", "Всем понемногу. Себе не осталось.")],
    # R: «Против чего ты бунтуешь так, что задевает саму себя?»
    "R": [("против «как надо»", "Против «как надо». Против правил, которые мне навязали."),
          ("против семьи", "Против семьи и того, что от меня ждали."),
          ("против всего", "Кажется, против всего. И по мне это тоже бьёт.")],
    # O: «Кого ты держишь на расстоянии, чтобы не потерять?»
    "O": [("того, кто дорог", "Того, кто мне по-настоящему дорог."),
          ("всех новых", "Всех новых. Ближе — страшно."),
          ("всех", "Всех. Так хотя бы не бросят.")],
    # D: «Что ты однажды сожгла — и до сих пор молчишь?»
    "D": [("отношения", "Отношения. Я их сожгла сама — и молчу об этом."),
          ("прошлую жизнь", "Прошлую жизнь. Целый кусок себя."),
          ("не готова назвать", "Есть такое. Но назвать пока не готова.")],
    # C: «Кого ты не впустила внутрь, хотя, может, хотела?»
    "C": [("того, кто стучался", "Того, кто стучался дольше всех."),
          ("уже никого", "Уже никого. Давно не впускаю."),
          ("не помню, чтобы хотела", "Не помню, чтобы вообще хотела впускать.")],
}

# Легаси-подсказки: старые сообщения с callback alena:f1..f3 должны остаться живыми.
_FIRST_STEPS = {
    "f1": "Это про отношения. Про то, что с ними у меня не складывается.",
    "f2": "Это про пустоту внутри. Всё вроде нормально, а внутри пусто.",
    "f3": "Это про то, что я всех отталкиваю. Или не подпускаю.",
}


def _first_step_kbd(code: str | None = None) -> InlineKeyboardMarkup:
    steps = _FIRST_STEPS_BY_SHADOW.get(code or "")
    if steps:
        rows = [[InlineKeyboardButton(text=label, callback_data=f"alena:fs:{code}:{i}")]
                for i, (label, _) in enumerate(steps)]
    else:  # нет Тени (ручной старт) → универсальные подсказки
        rows = [[InlineKeyboardButton(text="про отношения", callback_data="alena:f1")],
                [InlineKeyboardButton(text="про пустоту внутри", callback_data="alena:f2")],
                [InlineKeyboardButton(text="про то, что я всех отталкиваю", callback_data="alena:f3")]]
    rows.append([InlineKeyboardButton(text="скажу сама…", callback_data="alena:f0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@alena_router.callback_query(F.data.startswith("alena:f"))
async def cb_first_step(callback: CallbackQuery):
    """W1: выбор подсказки = первая реплика клиентки → обычный ход встречи."""
    parts = callback.data.split(":")
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    text = None
    if parts[1] == "fs" and len(parts) == 4:      # alena:fs:<code>:<n> — пер-Тень
        steps = _FIRST_STEPS_BY_SHADOW.get(parts[2]) or []
        try:
            text = steps[int(parts[3])][1]
        except (ValueError, IndexError):
            text = None
    elif parts[1] == "f0":
        await callback.message.answer("Я слушаю. Скажи, как есть — текстом или голосовым 🎙",
                                      parse_mode=None)
        return
    else:                                          # легаси alena:f1..f3
        text = _FIRST_STEPS.get(parts[1])
    if not text:
        return
    await callback.message.answer(f"— {text}", parse_mode=None)
    await _talk(callback.message, text, user_override=callback.from_user)


# ── Вход ──────────────────────────────────────────────────────────────────────

async def _entry(target: Message, user):
    """Показывает дисклеймер (один раз) + интро + кнопку «Начать»."""
    if await ai_active_session(user.id):
        await target.answer("Мы уже во встрече — просто пиши, я здесь.")
        return
    rem = await _remaining(user)
    if rem is not None and rem <= 0:
        await _send_exhausted(target)
        return
    if await ai_total_sessions(user.id) == 0:
        await target.answer(DISCLAIMER)
    # Правило №1 Кая (03.07): «бесплатная/одна» звучит ТОЛЬКО во frame (_do_start)
    # — раньше tail дублировал это соседним сообщением (вердикт аудитора).
    await target.answer(INTRO, reply_markup=_start_kbd())


_ALENA_FAIL = ("Я рядом. Секунду не получилось открыть встречу — попробуй ещё раз "
               "через /alena или напиши мне @kydaidy.")


@alena_router.message(Command("alena"))
async def cmd_alena(message: Message):
    try:
        await _entry(message, message.from_user)
    except Exception:
        logger.exception("alena _entry failed (cmd) for %s", message.from_user.id)
        await message.answer(_ALENA_FAIL, parse_mode=None)


@alena_router.callback_query(F.data == "alena")
async def cb_alena(callback: CallbackQuery):
    try:
        await _entry(callback.message, callback.from_user)
    except Exception:
        logger.exception("alena _entry failed (cb) for %s", callback.from_user.id)
        await callback.message.answer(_ALENA_FAIL, parse_mode=None)
    await callback.answer()


@alena_router.callback_query(F.data == "alena:start")
async def cb_start(callback: CallbackQuery):
    try:
        await _do_start(callback)
    except Exception:
        logger.exception("alena start failed for %s", callback.from_user.id)
        await callback.message.answer(_ALENA_FAIL, parse_mode=None)
    await callback.answer()


async def _do_start(callback: CallbackQuery):
    user = callback.from_user
    if await ai_active_session(user.id):
        await callback.message.answer("Мы уже во встрече — пиши.")
        await callback.answer()
        return
    rem = await _remaining(user)
    if rem is not None and rem <= 0:
        await _send_exhausted(callback.message)
        await callback.answer()
        return
    await ai_open_session(user.id)  # списание встречи — при старте
    await log_event(user.id, "session_open", "manual")
    # Вопрос Алёны — в историю (контекст первого хода для мозга, как в авто-пути).
    try:
        sess = await ai_active_session(user.id)
        if sess:
            await ai_add_message(sess["id"], user.id, "model",
                                 "«С чем ты сейчас? Что привело?»")
    except Exception:
        logger.warning("seed manual opener question failed", exc_info=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # Рамка-онбординг ГОЛОСОМ (мандат 03.07), вопрос — текстом; фолбэк — текст.
    # Аудит 03.07 №3/№5: тут НЕТ ни теста, ни Тени — никакого «не тот, что
    # сверху»; знакомство + «о себе», честно про AI (мог прийти без дисклеймера в
    # памяти).
    frame = ("Ну, привет. Пара слов о том, кто перед тобой: формул счастья я "
             "не раздаю и по голове гладить не буду. Я сама когда-то решила, "
             "что близость не для меня, — пока не поняла, что это была "
             "защита, а не выбор. Это твоя пробная встреча: бесплатная, одна, "
             "с глазу на глаз. То, что скажешь первым, — ещё не всё; под этим "
             "и поищем настоящее. Говори как есть — голосом или текстом. Я "
             "отвечаю вслух. Замолчу на минуту — значит, думаю. Итак: с чем "
             "ты сейчас, что привело?")
    if await send_voice_reply(callback.message, frame):
        await log_event(user.id, "voice_reply", "opener")
        await callback.message.answer(
            "Мой вопрос:\n\n«С чем ты сейчас? Что привело?»\n\n"
            "Ответь как тебе удобно 🎙", parse_mode=None)
    else:
        await callback.message.answer(
            "Привет. Пара слов о том, кто перед тобой: формул счастья я не "
            "раздаю и по голове гладить не буду. Я сама когда-то решила, что "
            "близость не для меня, — пока не поняла, что это была защита, а "
            "не выбор.\n\n"
            "Это твоя пробная встреча: бесплатная, одна, с глазу на глаз. То, "
            "что скажешь первым, — ещё не всё; под этим и поищем настоящее.\n\n"
            "Говори как есть — текстом или голосовым. Я читаю и отвечаю "
            "голосом. Замолчу на минуту — не исчезла, думаю.\n\n"
            "Итак: с чем ты сейчас, что привело?\n\n— Алёна",
            reply_markup=_pause_kbd(),
        )
    await callback.answer()


@alena_router.callback_query(F.data == "alena:stop")
async def cb_stop(callback: CallbackQuery):
    # A2: закрываем ВСЕ активные встречи (двойной тап мог наплодить сироту).
    await ai_close_all_active(callback.from_user.id)
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
        # Сбой D1-прокси не должен ронять фильтр (иначе исключение = сообщение не
        # доходит даже до catch-all, тишина для всех). При сбое — не перехватываем.
        try:
            return await ai_active_session(message.from_user.id) is not None
        except Exception:
            logger.warning("ai_active_session failed in filter (continuing)", exc_info=True)
            return False


async def _generate(history: list[dict], name, povorot, archetype,
                    force_close: bool, dossier: str | None = None) -> str:
    system = build_system(name, povorot, archetype, force_close, dossier)
    contents = [
        {"role": ("model" if m["role"] == "model" else "user"),
         "parts": [{"text": m["content"]}]}
        for m in history
    ]
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.9,
            # maxOutputTokens ВКЛЮЧАЕТ токены мышления → должен быть заметно больше
            # thinkingBudget, иначе видимый ответ обрывается на полуслове (баг «И…»).
            "maxOutputTokens": 4096,
            "thinkingConfig": {"thinkingBudget": 1536},
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


def _last_question(text: str) -> str | None:
    """Последний вопрос из реплики (для текст-дублёра после голосового).

    Фидбек Кая 03.07: эхо стало огрызком — короткий финальный вопрос («А телу
    как?») без предыдущей вопрос-фразы теряет смысл → склеиваем два последних."""
    qs = [s.strip() for s in re.findall(r"[^.!?…\n]*\?", text or "")]
    qs = [q for q in qs if 6 <= len(q) <= 200]
    if not qs:
        return None
    if len(qs) >= 2 and len(qs[-1]) < 45:
        pair = f"{qs[-2]} {qs[-1]}"
        if len(pair) <= 220:
            return pair
    return qs[-1]


# ── «Живое присутствие»: индикатор печати + паузы + разбивка на реплики ───────
def _split_bubbles(text: str, max_bubbles: int = 3) -> list[str]:
    """Ответ → 1–3 «пузыря» по абзацам — как человек печатает несколькими сообщениями."""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paras) <= 1:
        return [text.strip()] if text.strip() else []
    if len(paras) <= max_bubbles:
        return paras
    per = -(-len(paras) // max_bubbles)  # ceil
    return ["\n\n".join(paras[i:i + per]) for i in range(0, len(paras), per)]


async def _typing(message):
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass


async def _keep_typing(message, stop: "asyncio.Event"):
    """Держит индикатор «печатает…» живым, пока Алёна думает.

    Telegram гасит send_chat_action через ~5с. Мозг v2 (Opus + adaptive thinking)
    может считать 15-40с — без переотправки юзер видит тишину и решает, что бот умер.
    Пере-пингуем каждые ~4с до сигнала stop. Крэш-сейф, никогда не роняет ход."""
    try:
        while not stop.is_set():
            await _typing(message)
            try:
                await asyncio.wait_for(stop.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass
    except Exception:
        pass


async def _send_alive(message, text: str, reply_markup=None):
    """Ответ Алёны «вживую»: печатает → пауза → приходит МАКСИМУМ двумя репликами.

    Фидбек Кая 02.07: «текста выплывает много и сразу несколькими сообщениями —
    сбивает» → не больше 2 пузырей и паузы длиннее (человек так печатает)."""
    bubbles = _split_bubbles(text, max_bubbles=2)
    if not bubbles:
        return
    for i, chunk in enumerate(bubbles):
        last = (i == len(bubbles) - 1)
        await _typing(message)
        await asyncio.sleep(min(5.0, max(1.6, len(chunk) / 38)))  # читает/думает/печатает
        await message.answer(chunk, parse_mode=None,
                             reply_markup=(reply_markup if last else None))


@alena_router.message(F.text, _InAlenaFilter())
async def on_alena_talk(message: Message):
    """Текст во время активной встречи → общий обработчик хода."""
    await _talk(message, message.text)


async def _talk(message: Message, text: str, by_voice: bool = False,
                user_override=None):
    """Один ход встречи (текст ИЛИ расшифрованный голос) → ответ Алёны.

    by_voice=True — человек говорил голосом → Алёна отвечает голосом.
    user_override — реальный юзер, когда ход пришёл callback-кнопкой (W1): у
    callback.message from_user = бот, брать его нельзя."""
    user = user_override or message.from_user
    sess = await ai_active_session(user.id)
    if not sess:
        return
    sid = sess["id"]

    await ai_add_message(sid, user.id, "user", text)

    # Кризис — не зовём модель, сразу бережная эскалация (встреча остаётся открытой).
    if is_crisis(text):
        await message.answer(CRISIS_REPLY, parse_mode=None, reply_markup=_pause_kbd())
        return

    # Волна 1:1 (совещание 03.07): keyword-детекторы намерения (крэш-сейф).
    # hot → на закрытии флагман 1:1 первым; depth → дверь «глубже и лично».
    try:
        if _HOT_RE.search(text):
            await log_event(user.id, "lead_hot_kw", text[:60])
        elif _DEPTH_RE.search(text):
            await log_event(user.id, "depth_intent", text[:60])
    except Exception:
        logger.warning("intent detector failed (continuing)", exc_info=True)

    await ai_bump_turns(sid)

    # Устойчивость к смерти ключа: текстовый ответ может дать мозг v2 (Anthropic) ИЛИ
    # v1 (Gemini). Блокируем встречу ТОЛЬКО если НИ ОДИН путь недоступен — раньше гард
    # рубил встречу без Gemini, даже когда мозг на Anthropic жив (Gemini-квота уже
    # падала в истории проекта → встречи молча умирали). Если мозг упадёт в рантайме
    # без Gemini-фолбэка — сработает try/except ниже с мягким сообщением.
    if not settings.gemini_key and not settings.brain_v2_enabled:
        await message.answer(
            "Я тебя услышала. Сейчас не могу ответить развёрнуто — напиши @kydaidy.",
            parse_mode=None,
        )
        return

    u = await get_user(user.id)
    povorot = (u or {}).get("povorot")
    shadow = (u or {}).get("shadow_dist")
    dossier = (u or {}).get("dossier")
    archetype = None
    profile = None   # индивидуальная карта для мозга: ПОЛНОЕ распределение + досье
    if shadow:
        counts = decode_distribution(shadow)
        if counts:
            archetype = ARCHETYPES[winner_from_counts(counts)]
            # Комбинации у всех разные (фидбек Кая 02.07): мозг работает от полной
            # смеси Теней из её теста, не только от ведущей. Топ-3 с процентами.
            top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
            mix = " · ".join(
                f"{ARCHETYPES[c]['name']} {n * 10}%" for c, n in top if n > 0)
            profile = f"смесь Теней из теста: {mix}"
    if dossier:
        profile = f"{profile + '. ' if profile else ''}досье прошлых встреч: {dossier[:600]}"

    turns = (sess.get("turns") or 0) + 1
    force_close = turns >= TURN_CAP
    history = await ai_get_messages(sid, HISTORY_LIMIT)

    # «печатает…» живёт всё время генерации (мозг Opus+thinking = 15-40с, одиночный
    # индикатор гаснет через 5с). Гасим в finally — на любом выходе, включая ошибку.
    _stop_typing = asyncio.Event()
    _typer = asyncio.create_task(_keep_typing(message, _stop_typing))

    # Мозг v2 (Фаза 1 ядра) — ТОЛЬКО за флагом. При OFF путь v1 нетронут.
    # Если brain_turn упал — фолбэк на v1 в том же ходе (try/except).
    reply = None
    brain_signals = None      # скоринг из диагноза (brain-путь); в reply маркера нет
    brain_track = None
    brain_phase = None        # фаза метода из диагноза → триггер закрытия на native_offer
    brain_medium = None       # H1: "voice" на эмоц. пике/сдвиге → ответ голосовым
    try:
        if settings.brain_v2_enabled:
            try:
                cm = await get_client_model(user.id)
                if turns <= 1:
                    # Анти-протечка (прогон Кая 03.07, session_reopen_stale):
                    # client_model живёт на users, не на сессии — на первом ходе
                    # новой встречи в нём ещё тактика ПРОШЛОГО прогона (facade_lie/
                    # true_request/цитаты given), и через .update в brain_turn она
                    # копится дальше → «я помню, ты сказала…» о несказанном.
                    # Новая встреча = тактика с нуля; факт-досье едет отдельно (profile).
                    cm = None
                # turns==1 → первый ход НОВОЙ сессии: прошлые встречи = память,
                # метод-петля заново (фидбек Кая: не смешивать контексты сессий).
                # Имя в речь — только кириллицей: латинский ник («Creater») в русской
                # реплике ломает живость (фидбек Кая 02.07).
                spoken_name = user.first_name if re.search(
                    r"[а-яА-ЯёЁ]", user.first_name or "") else None
                reply, new_cm, brain_signals, brain_track = await brain_turn(
                    history, spoken_name, archetype, cm, profile,
                    fresh=(turns <= 1), force_voice=by_voice)
                await save_client_model(user.id, json.dumps(new_cm, ensure_ascii=False))
                brain_phase = (new_cm or {}).get("method_phase")
                brain_medium = (new_cm or {}).get("medium")
            except Exception as e:
                logger.warning("brain_v2 turn failed for %s → fallback v1: %s", user.id, e,
                               exc_info=True)
                # Телеметрия в D1: падение мозга не должно прятаться в логах Render
                # (03.07 весь день сидели на v1 и не видели этого).
                try:
                    await log_event(user.id, "brain_fail",
                                    f"{type(e).__name__}: {str(e)[:120]}")
                except Exception:
                    pass
                reply = None  # → фолбэк ниже на v1-путь

        if reply is None:
            try:
                reply = await _generate(history, user.first_name, povorot, archetype, force_close, dossier)
            except Exception as e:
                logger.exception(f"alena talk failed for {user.id}: {e}")
                # Телеметрия ДВОЙНОГО отказа (мозг + v1): обрыв 03.07 19:12 был
                # немым — в D1 не оставалось следа, что Gemini тоже лёг.
                try:
                    await log_event(user.id, "v1_fail",
                                    f"{type(e).__name__}: {str(e)[:120]}")
                except Exception:
                    pass
                # Сирена админу: ДВОЙНОЙ отказ (мозг+v1) = клиентка в тишине.
                # Обрыв 03.07 обнаружился только из жалобы Кая — так нельзя.
                try:
                    if settings.tg_admin_id:
                        await message.bot.send_message(
                            settings.tg_admin_id,
                            f"🚨 Воронка: оба движка упали (юзер {user.id}). "
                            f"v1: {type(e).__name__}: {str(e)[:150]}. "
                            "Проверь brain_fail/v1_fail в D1.")
                except Exception:
                    pass
                await message.answer(
                    "Я тут — но прямо сейчас ответить не получается. "
                    "Попробуй чуть позже или напиши @kydaidy.",
                    parse_mode=None,
                )
                return
    finally:
        _stop_typing.set()
        try:
            await _typer
        except Exception:
            pass

    # Закрытие встречи → показ оффера Клуба + КНОПКА оплаты Tribute (_after_close).
    # Триггеры: (1) модель поставила CLOSE_MARK; (2) предохранитель TURN_CAP;
    # (3) 🔴 мозг дошёл до фазы native_offer — ЖЕЛЕЗНО закрываем, даже если Haiku не
    # выдал CLOSE_MARK. Без этого коуч питчит Клуб, но ссылку на оплату не даёт
    # (баг: питч заканчивался вопросом, кнопка не появлялась).
    closed = (CLOSE_MARK in reply) or force_close or (brain_phase == "native_offer")
    reply = reply.replace(CLOSE_MARK, "").strip()
    reply, request = extract_request(reply)
    reply, dossier_new = extract_dossier(reply)
    # Служебный маркер скоринга (Фаза 1): ВСЕГДА вырезаем из reply до отправки,
    # чтобы [[SCORE ...]] не утёк человеку; сигналы кладём в БД (крэш-сейф).
    reply, score = extract_score(reply)
    # Финальная зачистка: срезать ЛЮБОЙ обрезанный маркер-огрызок ([[ЗАПРОС/[[ДОСЬЕ/
    # [[ВСТРЕЧА… без ]]) при обрыве генерации по лимиту токенов — чтобы служебка не
    # утекла человеку (полные маркеры уже извлечены выше).
    reply = strip_dangling_markers(reply)
    # v1-путь: скоринг маркером [[SCORE]] в тексте. brain-путь: структурой из диагноза
    # (в reply маркера нет). Берём то, что пришло этим ходом.
    signals = score or brain_signals
    if signals:
        await save_lead_signals(
            user.id,
            heat=signals.get("heat"), open_=signals.get("open"),
            resist=signals.get("resist"), value=signals.get("value"))
    # Трек лида (T1..T4) → колонка lead_track: топливо для /sources, догона и ворот
    # бюджета кружков. brain отдаёт трек прямо (валидируем против T1-T4 — Haiku мог
    # выдать мусор); для v1 выводим из сигналов classify.
    track = brain_track if brain_track in ("T1", "T2", "T3", "T4") else None
    if not track and signals:
        track = classify(signals)
    if track:
        await set_lead_track(user.id, track)
    if dossier_new:
        await save_dossier(user.id, dossier_new)
    # Пустой reply после вырезания маркеров (модель вернула почти одну служебку) →
    # без фолбэка _send_alive не отправит ничего и не покажет кнопки = «бот умер».
    if not reply.strip() and not closed:
        reply = "Я рядом — скажи это чуть иначе, я слушаю."
    await ai_add_message(sid, user.id, "model", reply)

    if closed:
        await ai_close_all_active(user.id)   # A2: все активные, не только текущая
        if request:
            await ai_set_last_request(user.id, request)
        # Финальная реплика встречи — голосом (мандат 03.07), фолбэк текст.
        if await send_voice_reply(message, reply):
            await log_event(user.id, "voice_reply", "close")
        else:
            await _send_alive(message, reply)
        await _after_close(message, user, request)
    else:
        # Мандат Кая 03.07: ГОЛОС — канал КАЖДОГО хода Алёны, включая v1-фолбэк
        # (раньше голос шёл только из мозга v2 → v1-путь сыпал текстом = «снова
        # куча текста»). Текст — только фолбэк при сбое TTS, ход не теряется.
        sent_voice = await send_voice_reply(message, reply, _pause_kbd())
        if sent_voice:
            await log_event(user.id, "voice_reply", brain_phase or "v1")
            # Мандат Кая: «текст используем для описания вопросов» — вопрос из
            # голосового дублируем коротким текстом перед глазами (ротация форм).
            q = _last_question(reply)
            if q:
                heads = ("Мой вопрос: «%s»", "«%s»", "Вопрос перед глазами: «%s»",
                         "Оставлю здесь: «%s»")
                await message.answer(heads[turns % len(heads)] % q, parse_mode=None)
        else:
            # Телеметрия отказа голоса: видно В ЧЁМ дело (длина/TTS), а не гадаем.
            await log_event(user.id, "voice_fallback_text", f"len={len(reply)}")
            await _send_alive(message, reply, _pause_kbd())
            # Ведение: вопрос текстом перед глазами. НЕ как робот (мандат Кая):
            # формулировки ротируются, инструкция «как отвечать» — только в первых
            # двух ходах (дальше она уже знает), без вопроса — тишина, не шаблон.
            q = _last_question(reply)
            if not q and turns <= 1:
                await message.answer("Ответь, как есть — текстом или голосовым 🎙",
                                     parse_mode=None)
        # W7: чекпойнт пути на 5-м ходу — ощущение «меня ведут» (карта прогресса
        # из модели клиентки). Только в brain-пути и если есть чем наполнить.
        if turns == 5 and settings.brain_v2_enabled:
            try:
                cm_now = await get_client_model(user.id) or {}
                came = (cm_now.get("facade_lie") or "").strip()
                seen = (cm_now.get("true_request_hypothesis") or "").strip()
                if came or seen:
                    # Мандат Кая 03.07: чекпойнт — тоже её речь → голосом, фолбэк текст.
                    spoken = "Смотри, куда мы дошли. "
                    if came:
                        spoken += f"Ты пришла с «{came[:120]}». "
                    if seen:
                        spoken += f"А под этим уже проступает настоящее: «{seen[:120]}». "
                    spoken += "Осталось главное. Идём."
                    if not await send_voice_reply(message, spoken):
                        parts = ["🗺 Смотри, куда мы дошли:"]
                        if came:
                            parts.append(f"— ты пришла с «{came[:120]}»")
                        if seen:
                            parts.append(f"— а под этим уже проступает настоящее: «{seen[:120]}»")
                        parts.append("Осталось главное. Идём.")
                        await message.answer("\n".join(parts), parse_mode=None)
                    await log_event(user.id, "checkpoint_shown")
            except Exception:
                logger.warning("checkpoint failed (continuing)", exc_info=True)


# ── Голосовой ввод: человек отвечает голосом → распознаём → тот же ход ────────
class _InAlenaVoiceFilter(BaseFilter):
    """Голосовое во время активной встречи → распознать и провести как реплику."""
    async def __call__(self, message: Message) -> bool:
        return message.voice is not None and \
            await ai_active_session(message.from_user.id) is not None


async def _transcribe_voice(bot, voice) -> str:
    """Голосовое Telegram (ogg/opus) → текст через Gemini audio. Возвращает расшифровку."""
    buf = io.BytesIO()
    await bot.download(voice, destination=buf)
    audio_b64 = base64.b64encode(buf.getvalue()).decode()
    payload = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "audio/ogg", "data": audio_b64}},
            {"text": "Расшифруй это русское голосовое сообщение в текст дословно. "
                     "Верни ТОЛЬКО расшифровку, без кавычек и комментариев."},
        ]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 1024,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }
    url = f"{BASE}/models/{TEXT_MODEL}:generateContent?key={settings.gemini_key}"
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload,
                          timeout=aiohttp.ClientTimeout(total=90)) as r:
            body = await r.json()
    parts = body.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()


@alena_router.message(_InAlenaVoiceFilter())
async def on_alena_voice(message: Message):
    """Человек отвечает голосом → распознаём в текст → ведём встречу как обычно."""
    if not settings.gemini_key:
        await message.answer("Голос сейчас не распознаю — напиши, пожалуйста, текстом.",
                             parse_mode=None)
        return
    try:
        text = await _transcribe_voice(message.bot, message.voice)
    except Exception:
        logger.exception("voice transcribe failed for %s", message.from_user.id)
        await message.answer("Не расслышала голосовое — скажи ещё раз или напиши текстом.",
                             parse_mode=None)
        return
    if not text:
        await message.answer("Голосовое будто пустое — скажи ещё раз или напиши.",
                             parse_mode=None)
        return
    # Показываем расшифровку — человек видит, что я расслышала его слова.
    await message.answer(f"🎙️ {text}", parse_mode=None)
    await _talk(message, text, by_voice=True)


# ── Кружок ОТ человека: распознаём видео-кружок как реплику (Кай 02.07) ────────
class _InAlenaVideoFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        try:
            return message.video_note is not None and \
                await ai_active_session(message.from_user.id) is not None
        except Exception:
            return False


async def _transcribe_video_note(bot, video_note) -> tuple[str, str]:
    """Видео-кружок Telegram (mp4) → (речь дословно, невербалика) через Gemini.

    T-2 (мандат Кая 03.07): помимо слов читаем КАДР — эмоциональное состояние,
    несовпадение слов и лица (приём Мурадяна «слово↔тело»). Невербалика идёт
    мозгу служебной припиской, человеку не показывается."""
    buf = io.BytesIO()
    await bot.download(video_note, destination=buf)
    b64 = base64.b64encode(buf.getvalue()).decode()
    payload = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "video/mp4", "data": b64}},
            {"text": "Видео-сообщение женщины на русском. Верни СТРОГО JSON без "
                     "пояснений: {\"speech\": \"дословная расшифровка речи\", "
                     "\"nonverbal\": \"1-2 фразы: эмоциональное состояние в кадре "
                     "(мимика, глаза, голос, паузы) и совпадает ли оно со словами; "
                     "пиши только наблюдаемое, без диагнозов\"}"},
        ]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 1024,
                             "responseMimeType": "application/json",
                             "thinkingConfig": {"thinkingBudget": 0}},
    }
    url = f"{BASE}/models/{TEXT_MODEL}:generateContent?key={settings.gemini_key}"
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload,
                          timeout=aiohttp.ClientTimeout(total=120)) as r:
            body = await r.json()
    parts = body.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    raw = "".join(p.get("text", "") for p in parts).strip()
    try:
        d = json.loads(raw)
        return (d.get("speech") or "").strip(), (d.get("nonverbal") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        return raw, ""   # Gemini вернул не-JSON → трактуем как чистую расшифровку


@alena_router.message(_InAlenaVideoFilter())
async def on_alena_video_note(message: Message):
    """Человек ответил КРУЖКОМ → распознаём речь+невербалику → обычный ход."""
    if not settings.gemini_key:
        await message.answer("Кружок сейчас не распознаю — скажи голосовым или текстом.",
                             parse_mode=None)
        return
    try:
        text, nonverbal = await _transcribe_video_note(message.bot, message.video_note)
    except Exception:
        logger.exception("video note transcribe failed for %s", message.from_user.id)
        text, nonverbal = "", ""
    if not text:
        await message.answer("Не расслышала кружок — скажи ещё раз или напиши.",
                             parse_mode=None)
        return
    # Человек видит только свою речь; невербалика — служебная приписка мозгу
    # внутри реплики (история сессии), наружу не звучит.
    await message.answer(f"🎥 {text}", parse_mode=None)
    if nonverbal:
        await log_event(message.from_user.id, "video_nonverbal")
        text = f"{text}\n[видно в кадре: {nonverbal}]"
    await _talk(message, text, by_voice=True)


# ── После оффера: сомнения/возражения НЕ теряем — дожимаем (Кай 02.07) ─────────
class _AfterOfferFilter(BaseFilter):
    """Пишет ПОСЛЕ закрытой встречи (оффер показан, не купила, <48ч) → возражение."""
    async def __call__(self, message: Message) -> bool:
        if not message.text or message.text.startswith("/"):
            return False
        try:
            u = message.from_user
            if await ai_active_session(u.id):
                return False           # живую встречу ведёт основной хендлер
            last = await ai_last_session(u.id)
            if not last or last.get("status") != "closed":
                return False
            uu = await get_user(u.id)
            if not (uu or {}).get("last_ai_request"):
                return False           # оффера не было — не наш случай
            if await get_active_subscription(u.id, "manifest_club"):
                return False           # уже в Клубе
            if await events_count_recent(u.id, "objection", 48) >= 3:
                return False           # 3 отработки — дальше не давим
            return True
        except Exception:
            return False


@alena_router.message(F.text, _AfterOfferFilter())
async def on_after_offer(message: Message):
    """Отработка сомнения после оффера: признать → назвать страх → вернуть к двери."""
    user = message.from_user
    await log_event(user.id, "objection")
    u = await get_user(user.id)
    request = (u or {}).get("last_ai_request") or ""
    last = await ai_last_session(user.id)
    history = await ai_get_messages((last or {}).get("id"), HISTORY_LIMIT) or []
    history = history + [{"role": "user", "content": message.text}]
    archetype = None
    shadow = (u or {}).get("shadow_dist")
    if shadow:
        counts = decode_distribution(shadow)
        if counts:
            archetype = ARCHETYPES[winner_from_counts(counts)]
    _stop = asyncio.Event()
    _typer = asyncio.create_task(_keep_typing(message, _stop))
    try:
        from alena_brain import respond
        name = user.first_name if re.search(r"[а-яА-ЯёЁ]", user.first_name or "") else None
        reply = await respond(
            f"Она сомневается/возражает после приглашения (её настоящий запрос: "
            f"«{request}»). Отработай как живой продавец, по шагам: 1) ВСКРОЙ, что "
            "на самом деле стоит за возражением — деньги, время, страх боли или "
            "недоверие (к себе, ко мне, к формату); назови это мягко, своими "
            "словами. 2) ВАЛИДИРУЙ чувство: сопротивление — защита, так психика "
            "бережёт от перемен, это не слабость. 3) ОТВЕТЬ механизмом: чем "
            "именно то, что за дверью, снимает ЕЁ конкретный страх — под её "
            "слова, не общими фразами. 4) Оставь выбор за ней без нажима и "
            "позови спросить ещё, если что-то осталось. Не оправдывайся, не "
            "дави, без цен и ссылок. Лещ честности, не уговоры.",
            "native_offer", name, archetype, history, voice_mode=True)
        reply, _ = extract_request(reply)
        reply = strip_dangling_markers(reply.replace(CLOSE_MARK, "")).strip()
    except Exception:
        logger.warning("objection respond failed", exc_info=True)
        reply = ("Слышу. Сомнение — это защита, не слабость: так психика бережёт "
                 "себя от перемен. Решать прямо сейчас ничего не надо. Что "
                 "держит — спроси здесь, отвечу честно. А готова — дверь под "
                 "этим сообщением.")
    finally:
        _stop.set()
        try:
            await _typer
        except Exception:
            pass
    # Совещание 04.07: рассинхрон сегмента — горячей/глубокой ветке оффер давал
    # обе двери (_bridge_kbd), а отработка возражения схлопывала до одного Клуба,
    # теряя дверь 1:1. Держим сегментную клавиатуру и здесь (крэш-сейф).
    kbd = _club_only_kbd()
    try:
        if ((u or {}).get("lead_track") == "T4"
                or await events_count_recent(user.id, "lead_hot_kw", 48) > 0
                or await events_count_recent(user.id, "depth_intent", 48) > 0):
            kbd = _bridge_kbd()
    except Exception:
        pass
    if not await send_voice_reply(message, reply, kbd):
        await message.answer(reply, parse_mode=None, reply_markup=kbd)


# ── Hermes #1: «затихшая» встреча → один мягкий оффер Клуба ────────────────────
# Человек начал встречу, Алёна задала вопрос/сделала оффер — и он замолчал на
# пике. Оффер не должен теряться в тишине: через N минут молчания шлём ОДИН
# тёплый нудж с дверью в Клуб. Встреча остаётся открытой — можно ответить дальше.
_STALE_NUDGE_TEXT = (
    "Ты затихла — и это нормально. Иногда то, что мы задели, нужно донести молча.\n\n"
    "Я не тороплю и не исчезаю. Захочешь продолжить — просто напиши, я здесь.\n\n"
    "А если почувствуешь, что не хочешь оставаться с этим одна — я рядом каждый "
    "день в Клубе «Манифест»: две наши встречи в месяц, утреннее аудио, живой эфир раз в неделю, закрытый чат. "
    "990 в месяц, чтобы не разбираться в одиночку.\n\n— Алёна"
)

# Тот же нудж устной речью (для TTS): без переносов-подписи, «дверь ниже».
_STALE_NUDGE_VOICE = (
    "Ты затихла — и это нормально. Иногда то, что мы задели, нужно донести молча. "
    "Я не тороплю и не исчезаю. Захочешь продолжить — просто напиши, я здесь. "
    "А если почувствуешь, что не хочешь оставаться с этим одна — я рядом каждый "
    "день в Клубе «Манифест»: две наши встречи в месяц, утреннее аудио, живой эфир раз в неделю, закрытый чат. "
    "Дверь — под этим сообщением."
)


# ── T-1 (03.07): само-восстановление потерянного хода ─────────────────────────
# Render-редеплой убивает контейнер посреди генерации (15–40с): реплика клиентки
# записана, ответа Алёны нет — тишина (вскрыто прогоном Кая 03.07 12:00).
# Тик находит такие встречи и доотвечает сам.
_LADDER_TEXT = (
    "Мы рядом уже пару недель. Если чувствуешь, что какая-то тема просится "
    "глубже, чем чат и эфиры, — есть живой разбор один на один: час только "
    "про тебя. Мест — всего восемь в месяц. Дверь — под этим сообщением."
)


async def run_club_ladder_tick(bot):
    """Спящая лестница 1:1 (совещание 03.07): члену Клуба ≥14 дней — один раз
    мягкое приглашение на живой разбор. При 0 членов — no-op. Крэш-сейф."""
    try:
        for row in await club_ladder_candidates(min_days=14):
            tg = row["tg_id"]
            if await events_count_recent(tg, "ladder_1on1", hours=24 * 365):
                continue  # уже звали — не повторяем
            try:
                if not await send_voice_to(bot, tg, _LADDER_TEXT, _one_on_one_kbd()):
                    await bot.send_message(tg, _LADDER_TEXT + "\n\n— Алёна",
                                           reply_markup=_one_on_one_kbd(),
                                           parse_mode=None)
                await log_event(tg, "ladder_1on1", "sent")
            except Exception:
                logger.warning("ladder send failed for %s", tg, exc_info=True)
    except Exception:
        logger.warning("club ladder tick failed", exc_info=True)


async def run_orphan_turn_tick(bot):
    rows = await ai_orphan_sessions(minutes=3)
    healed = 0
    for r in rows:
        sid, tg_id = r.get("session_id"), r.get("tg_id")
        if not sid or not tg_id:
            continue
        try:
            history = await ai_get_messages(sid, HISTORY_LIMIT)
            if not history or history[-1].get("role") != "user":
                continue  # гонка: ответ уже пришёл — не дублируем
            u = await get_user(tg_id)
            shadow = (u or {}).get("shadow_dist")
            archetype = None
            profile = None
            if shadow:
                counts = decode_distribution(shadow)
                if counts:
                    archetype = ARCHETYPES[winner_from_counts(counts)]
            if (u or {}).get("dossier"):
                profile = f"досье прошлых встреч: {(u or {}).get('dossier')[:600]}"
            name = None  # имени из Message тут нет; мозг ведёт без обращения
            reply, new_cm, signals, track = await brain_turn(
                history, name, archetype, await get_client_model(tg_id),
                profile, fresh=False)
            await save_client_model(tg_id, json.dumps(new_cm, ensure_ascii=False))
            reply = strip_dangling_markers(
                extract_score(extract_dossier(extract_request(
                    reply.replace(CLOSE_MARK, ""))[0])[0])[0]).strip()
            if not reply:
                continue
            await ai_add_message(sid, tg_id, "model", reply)
            if not await send_voice_to(bot, tg_id, reply):
                await bot.send_message(tg_id, reply, parse_mode=None)
            await log_event(tg_id, "orphan_recovered")
            healed += 1
        except Exception:
            logger.warning("orphan recover failed for %s", tg_id, exc_info=True)
    if healed:
        logger.info("orphan tick: восстановлено ходов — %s", healed)
    return healed


async def run_stale_session_tick(bot):
    """Фоновый джоб (планировщик bot.py): активные встречи, где последней была
    реплика Алёны и человек молчит дольше settings.stale_nudge_minutes → ОДИН
    мягкий оффер Клуба. Один нудж на встречу (метка nudged_at). Возвращает
    число отправленных — для лога/диагностики.
    """
    if not settings.stale_nudge_enabled:
        return 0
    rows = await ai_stale_sessions(settings.stale_nudge_minutes)
    sent = 0
    for r in rows:
        sid = r.get("session_id")
        tg_id = r.get("tg_id")
        if not sid or not tg_id:
            continue
        # Метим ДО отправки: сбой доставки (юзер закрыл личку) не должен крутить
        # нудж на следующем тике — задвоенный outreach хуже одного пропуска.
        await ai_mark_nudged(sid)
        try:
            # Нудж — единственное место, где кнопка Клуба ВО ВРЕМЯ встречи уместна:
            # оффер ГОЛОСОМ (мандат 03.07), фолбэк текст с той же кнопкой.
            if not await send_voice_to(bot, tg_id, _STALE_NUDGE_VOICE,
                                       _club_only_kbd()):
                await bot.send_message(tg_id, _STALE_NUDGE_TEXT,
                                       reply_markup=_club_only_kbd(), parse_mode=None)
            await log_event(tg_id, "stale_nudge")
            sent += 1
        except Exception:
            logger.warning("stale nudge send failed for %s", tg_id, exc_info=True)
    if sent:
        logger.info("stale nudge: sent %s club offer(s) to quiet meetings", sent)
    return sent


def _followup_delays() -> list[int]:
    """settings.followup_delays_min ("45,1440,4320") → [45, 1440, 4320]. Крэш-сейф."""
    try:
        out = [int(x) for x in str(settings.followup_delays_min).split(",") if x.strip()]
        return out[:3] or [45, 1440, 4320]
    except Exception:
        return [45, 1440, 4320]


async def _schedule_followups(tg_id: int):
    """H6: не купила после оффера → серия дожима (одна на человека, купившим не шлётся)."""
    if settings.followup_enabled:
        await followup_schedule(tg_id, _followup_delays())


_OFFER_FALLBACK_TEXT = (
    "В Клубе «Манифест» продолжим ровно с этого места: две встречи со мной в месяц, каждое утро — моё аудио "
    "«Манифест дня», раз в неделю — живой эфир, и круг женщин в закрытом чате, "
    "где не нужно держать лицо.\n\n"
    "990 в месяц — чтобы не разбираться с этим одной. Дверь ниже.\n\n— Алёна")


async def _offer_kruzhok(bot, chat_id: int, tg_id: int,
                         name: str | None, request: str):
    """Ф2 (мандат Кая 02.07): САМ ОФФЕР Клуба = именной видео-кружок Алёны.

    Рендер твина ~2–4 мин → идёт фоном после тизера «записываю тебе кружок».
    Кнопка оплаты приходит ВМЕСТЕ с кружком. Сбой рендера → страховка: оффер
    текстом с кнопкой — продажа не теряется никогда."""
    q = request.strip().rstrip(".")[:140]
    try:
        who = f"{name}, послушай" if name else "Послушай"
        # ⚠️ Без слова «кружок» в озвучке (TTS читает «крУжком» — фидбек Кая 03.07).
        # Совещание 04.07 (мандат Кая: мост боль→механизм + приглашение к
        # вопросам): каждый элемент Клуба привязан к функции под ЕЁ запрос,
        # финал зовёт спросить — это поджигает уже готовую ветку возражений.
        script = (f"{who}. То, что сегодня вскрылось — «{q}» — это твоя настоящая "
                  "точка. Один разговор её показал, но чтобы она поменялась, к "
                  "ней нужно возвращаться — не наскоком. Для этого и есть Клуб. "
                  "Наши встречи внутри — разматываем твой узор шаг за шагом. По "
                  "утрам тебя держит мой «Манифест дня», пять минут опоры. Раз в "
                  "неделю эфир, где я разбираю вживую — можно и про твоё. А в "
                  "закрытом кругу рядом женщины с похожим — ты больше не тащишь "
                  "это одна. Если что-то мешает или хочешь спросить — просто "
                  "ответь мне сюда, я на связи. Готова продолжить — дверь прямо "
                  "под этим видео.")
        await add_circle_credits(tg_id, CIRCLE_CREDITS)  # леджер ДО рендера (антидубль)
        if await send_kruzhok_to(bot, chat_id, script):
            await log_event(tg_id, "offer_kruzhok", "sent")
            await bot.send_message(
                chat_id, "Это тебе. Лично.\n\nВойти — здесь:",
                reply_markup=_club_only_kbd(), parse_mode=None)
            return
    except Exception:
        logger.warning("offer kruzhok failed (fallback text)", exc_info=True)
    # Страховка: кружок не собрался → оффер голосом (сбой видео ≠ сбой TTS),
    # дальше текстом — кнопка обязана дойти в любом случае.
    try:
        spoken = " ".join(_OFFER_FALLBACK_TEXT.replace("— Алёна", "").split())
        if await send_voice_to(bot, chat_id, spoken, _club_only_kbd()):
            await log_event(tg_id, "offer_kruzhok", "fallback_voice")
            return
        await log_event(tg_id, "offer_kruzhok", "fallback_text")
        await bot.send_message(chat_id, _OFFER_FALLBACK_TEXT,
                               reply_markup=_club_only_kbd(), parse_mode=None)
    except Exception:
        logger.warning("offer fallback send failed", exc_info=True)


async def _after_close(message: Message, user, request: str | None = None):
    # Сегментация оффера — ТОЛЬКО по реальному членству в Клубе (фикс 02.07:
    # whitelist-тестеры раньше улетали в VIP-ветку и не видели боевой путь —
    # кружок-оффер/дожимы; теперь тестовый аккаунт проходит как обычная клиентка,
    # безлимит встреч у него остаётся).
    is_member = await _is_club_member(user.id)

    # Вскрылся настоящий запрос → ведём дальше. Куда именно — по сегменту.
    # Мандат Кая 03.07: офферы — ГОЛОСОМ (это её речь), кнопка на голосовом;
    # сбой TTS → тот же текст с кнопкой, продажа не теряется.
    if request:
        q = request.strip().rstrip(".")
        if is_member:
            # Член Клуба → тёплый докрут в 1:1 (я тебя уже знаю).
            await log_event(user.id, "offer_shown", "bridge_1on1")
            # Методолог №2 (2а): закрытие уже назвало запрос — оффер не пере-
            # представляет его заново, а сразу ведёт дальше.
            bridge = (
                f"«{q}» — вот с чем ты пришла на самом деле. Увидеть — уже "
                "много, но не всё: прожить и поменять такое в переписке не "
                "выходит. Это работа для живой встречи, один на один. Живых "
                "мест у меня всего восемь в месяц. Готова — дверь под этим "
                "сообщением: после оплаты откроется мой календарь, выберешь "
                "окно — и продолжим ровно с этого места.")
            if await send_voice_reply(message, bridge, _bridge_kbd()):
                await log_event(user.id, "voice_reply", "offer_bridge")
            else:
                await message.answer(bridge + "\n\n— Алёна",
                                     reply_markup=_bridge_kbd(), parse_mode=None)
            return
        # Адаптивный порядок офферов (Кай 02.07): ГОРЯЧЕЙ (трек T4 — готова к шагу)
        # первым предлагаем ФЛАГМАН 1:1, Клуб — рядом второй строкой. Остальным —
        # Клуб (трипваер). Рынок: AI слабо закрывает высокий чек холодным, поэтому
        # 1:1-первым только по скорингу готовности.
        u_row = await get_user(user.id)
        # Совещание 1:1 (03.07): T4-ветка больше НЕ зависит от мозга — keyword-
        # фолбэк готовности (lead_hot_kw) включает флагман и при лежащем скоринге.
        hot_kw = (await events_count_recent(user.id, "lead_hot_kw", 48)) > 0
        if (u_row or {}).get("lead_track") == "T4" or hot_kw:
            await log_event(user.id, "offer_shown",
                            "flagship_1on1_kw" if hot_kw else "flagship_1on1_T4")
            # Совещание 04.07: почему 1:1 (механизм), а не перечень Клуба +
            # приглашение спросить (вскрывает возражения само).
            flagship = (
                f"«{q}» — с этим не в переписку, и ты сама это чувствуешь. "
                "Такое не отражают в чате — это проживают: медленно, один на "
                "один, пока не отпустит. Скажу прямо: возьми свой запрос на "
                "встречу со мной — час, где есть только ты и он. Живых окон — "
                "всего восемь в месяц. А если сначала мягче — рядом Клуб, где "
                "можно просто побыть в кругу. Сомневаешься или есть вопрос — "
                "напиши, отвечу честно. Обе двери — под этим сообщением.")
            if await send_voice_reply(message, flagship, _bridge_kbd()):
                await log_event(user.id, "voice_reply", "offer_flagship")
            else:
                await message.answer(flagship + "\n\n— Алёна",
                                     reply_markup=_bridge_kbd(), parse_mode=None)
            await _schedule_followups(user.id)
            return
        # Совещание 1:1 (03.07), pull-триггер: она САМА просила «глубже/лично» в
        # сессии (depth_intent) → дверь 1:1 + Клуб мягкой альтернативой. Дефолт
        # для остальных не трогаем (один Клуб-CTA на пике).
        if (await events_count_recent(user.id, "depth_intent", 48)) > 0:
            await log_event(user.id, "offer_shown", "depth_1on1")
            # Совещание 04.07: разведено с flagship по корням (ПРАВИЛО №1).
            depth = (
                f"«{q}» — и ты сама потянулась глубже. Услышала. То, что просит "
                "изнутри, перепиской не закрыть — для такого есть живой час "
                "наедине, только про тебя. Мест — восемь на месяц. Входить "
                "постепенно — рядом Клуб: круг, эфиры, моя ежедневная опора. "
                "Что останавливает — спроси прямо здесь, я на связи. Обе "
                "двери — ниже.")
            if await send_voice_reply(message, depth, _bridge_kbd()):
                await log_event(user.id, "voice_reply", "offer_depth")
            else:
                await message.answer(depth + "\n\n— Алёна",
                                     reply_markup=_bridge_kbd(), parse_mode=None)
            await _schedule_followups(user.id)
            return
        # Бесплатная встреча исчерпана → оффер Клуба = ИМЕННОЙ КРУЖОК (мандат Кая:
        # «кружочки в начале и в конце, остальное голосом»). Схема: тизер голосом
        # («записываю тебе кружок») → фоновый рендер твина ~2–4 мин → кружок с именем
        # и её запросом + кнопка. Сбой рендера → страховка текстом с кнопкой.
        await log_event(user.id, "offer_shown", "club_request")
        # ⚠️ Без слова «кружок» в озвучке (кривое ударение TTS — фидбек Кая 03.07).
        # Аудит №8: честный тайминг (рендер твина 2–4 мин, не «пару минут»).
        # Методолог 03.07: без повтора «{q}» (закрывающий ход его уже назвал) и с
        # честным таймингом (рендер твина 2–4 мин).
        teaser = ("Постой — это ещё не всё. То, что не передать текстом, я "
                  "записываю тебе на видео — лично. Несколько минут, и оно "
                  "у тебя.")
        if not await send_voice_reply(message, teaser):
            await message.answer(teaser, parse_mode=None)
        _name = user.first_name if re.search(r"[а-яА-ЯёЁ]", user.first_name or "") else None
        asyncio.create_task(_offer_kruzhok(
            message.bot, message.chat.id, user.id, _name, q))
        await _schedule_followups(user.id)
        return

    # Запроса не вскрылось / ей хватило — честно, без втюхивания.
    if is_member:
        await message.answer(
            "На сегодня всё. Ещё разговор — просто /alena.", reply_markup=_menu_kbd())
        return
    await log_event(user.id, "offer_shown", "club_soft")
    soft = ("Это была твоя бесплатная встреча — больше таких не будет. Захочешь "
            "продолжить — я рядом каждый день в Клубе «Манифест»: две наши встречи в месяц, утренние аудио, "
            "живые эфиры и закрытый чат. Он только открылся — заходишь в числе первых. "
            "990 в месяц, чтобы не разбираться со всем этим в одиночку. Остались "
            "вопросы — просто напиши, отвечу. Дверь — под этим сообщением.")
    if await send_voice_reply(message, soft, _club_only_kbd()):
        await log_event(user.id, "voice_reply", "offer_soft")
    else:
        await message.answer(soft + "\n\n— Алёна",
                             reply_markup=_club_only_kbd(), parse_mode=None)
    await _schedule_followups(user.id)
