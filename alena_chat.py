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
    get_client_model, save_client_model, get_meta,
    log_event, followup_schedule, get_lead_signals, add_circle_credits,
    ai_last_session, events_count_recent, club_ladder_candidates,
    memory_allowed as _db_memory_allowed,
)
from alena_voice import send_voice_reply, send_voice_to, send_kruzhok_to
from lead_policy import should_spend_circle, CIRCLE_CREDITS
from alena_persona import (
    build_system, DISCLAIMER, INTRO, CLOSE_MARK, is_crisis, CRISIS_REPLY,
    extract_request, extract_dossier, extract_score, strip_dangling_markers,
    extract_phase, classify_objection, objection_directive, OBJECTION_CAP,
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

TURN_CAP = 10              # предохранитель: после стольких реплик — закрытие с оффером.
                           # Консилиум воронки 05.07 + решение Кая: 6 фаз метода
                           # (contact→facade→contradiction→request→shift→bridge) не
                           # влезают в 7 ходов — force_close рубил ДО give_shift (главный
                           # продающий момент). 7→10, чтобы встреча доходила до продажи.
                           # (истор.: 20→12 03.07, 12→7 05.07 — оба перелёт в другую сторону)
HISTORY_LIMIT = 40         # сколько сообщений истории отдаём модели
ONE_ON_ONE_URL = "https://t.me/tribute/app?startapp=sZXq"  # 1:1 подписка (1 встреча/мес, entry); 3 встречи = sZXr
CLUB_URL = "https://t.me/tribute/app?startapp=sULY"


# Per-user сериализация ходов (аудит воронки 06.07): двойной тап / два сообщения
# подряд больше не создают вторую встречу/кружок/списание — второй апдейт ждёт,
# пока идёт первый (желаемое поведение). Лок держится весь ход, включая долгие
# LLM/TTS вызовы. Чистим лениво (при разрастании), макс. пара тысяч юзеров — ок.
_USER_LOCKS: dict[int, "asyncio.Lock"] = {}


def _user_lock(tg_id: int) -> "asyncio.Lock":
    lock = _USER_LOCKS.get(tg_id)
    if lock is None:
        if len(_USER_LOCKS) > 5000:
            for _k, _l in list(_USER_LOCKS.items()):
                if not _l.locked():
                    _USER_LOCKS.pop(_k, None)
        lock = _USER_LOCKS.setdefault(tg_id, asyncio.Lock())
    return lock


def _is_unlimited(user) -> bool:
    from handlers import _is_unlimited as _h  # late import: избегаем цикла
    return _h(user)


async def _is_club_member(tg_id: int) -> bool:
    return await get_active_subscription(tg_id, "manifest_club") is not None


async def _memory_allowed(tg_id: int) -> bool:
    """Досье прошлых встреч подаётся мозгу ТОЛЬКО купившим (мандат Кая 04.07):
    бесплатная тест-встреча — с чистого листа (память приплетала прошлые
    прогоны), после покупки (Клуб или подписка 1:1) — память работает.
    Крэш-сейф: сомнение = без памяти (безопаснее галлюцинаций).

    Делегирует в database.memory_allowed — ЕДИНЫЙ гейт, переиспользуемый
    и в growth_agent (реактивация), чтобы обе точки чтения dossier сверяли
    один и тот же реальный статус покупки, а не имя сегмента/маршрут."""
    return await _db_memory_allowed(tg_id)


async def _gate_dossier(tg_id: int, dossier: str | None) -> str | None:
    """ЕДИНЫЙ чекпойнт памяти (критичный рефактор 04.07): досье прошлых встреч
    попадает в ЛЮБОЙ system-промпт (brain-путь через profile, ЛЕГАСИ v1-путь
    build_system/_generate, джобы orphan-tick) ТОЛЬКО через эту функцию.

    Раньше _generate() (v1/build_system/SESSION_ARC — без ANTI_HALLUCINATION)
    получал СЫРОЙ dossier мимо _memory_allowed(): один тест-аккаунт подмешивал
    досье прошлых бесплатных прогонов → «я помню, ты сказала…» о несказанном
    (корень галлюцинаций, аудит 04.07). Гейтуй dossier ЗДЕСЬ, один раз, сразу
    после чтения из users — и передавай результат везде ниже по потоку."""
    if not dossier:
        return None
    return dossier if await _memory_allowed(tg_id) else None


async def _remaining(user) -> int | None:
    """Сколько встреч осталось; None — безлимит (только whitelist).

    Клуб «Манифест» — 2 встречи с AI-Алёной в месяц (мандат Кая 03.07,
    скользящее окно 30 дней). Остальным — 1 бесплатная пожизненно.
    """
    if _is_unlimited(user):
        return None
    if await _is_club_member(user.id):
        # Считаем встречи ОТ даты вступления (не раньше), чтобы пробная встреча
        # до покупки не съедала месячную квоту члена (аудит 05.07).
        from database import get_active_subscription, ai_sessions_used_member
        club = await get_active_subscription(user.id, "manifest_club")
        since = (club or {}).get("started_at")
        used = (await ai_sessions_used_member(user.id, since) if since
                else await ai_sessions_used_30d(user.id))
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


def _club_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Войти в Клуб «Манифест» — 990 ₽/мес", url=CLUB_URL)],
        [InlineKeyboardButton(text="Личные встречи 1:1 — подписка", url=ONE_ON_ONE_URL)],
    ])


def _bridge_kbd() -> InlineKeyboardMarkup:
    """Нативный мост: вскрытый запрос → 1:1 первым, Клуб — мягкой альтернативой."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Этот запрос — в личную работу 1:1 (подписка)", url=ONE_ON_ONE_URL)],
        [InlineKeyboardButton(text="Быть рядом регулярно — Клуб 990 ₽/мес", url=CLUB_URL)],
    ])


def _offer_kbd() -> InlineKeyboardMarkup:
    """Волна 1: карточка оффера — три двери (Клуб / 1:1 / разбор «подробнее»).
    Идёт и с тизером (кнопка сразу), и с карточкой после кружка — надёжный путь."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Клуб «Манифест» · 990 ₽/мес", url=CLUB_URL)],
        [InlineKeyboardButton(text="Личная работа со мной · 1:1", url=ONE_ON_ONE_URL)],
        [InlineKeyboardButton(text="Сначала расскажи подробнее", callback_data="alena:more")],
    ])


def _member_offer_kbd() -> InlineKeyboardMarkup:
    """То же для члена Клуба: он уже внутри → без двери Клуба, только 1:1 + разбор."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Личная работа со мной · 1:1", url=ONE_ON_ONE_URL)],
        [InlineKeyboardButton(text="Сначала расскажи подробнее", callback_data="alena:more")],
    ])


def _request_from_cm(cm: dict | None) -> str | None:
    """Волна 1: реконструкция запроса из модели клиентки, когда закрытие пришло
    БЕЗ маркера [[ЗАПРОС]] (жёсткие доводки: TURN_CAP, «да» на мост). ТОЛЬКО
    вскрытый настоящий запрос: facade_lie не берём — это её защита-ложь (ярлык
    в 3-м лице), подать её как «вскрытую боль» = инверсия смысла (аудит W1 #1).
    Пусто → None (уйдёт в мягкую ветку без цитаты). Чистая, тестируемая."""
    request = ((cm or {}).get("true_request_hypothesis") or "").strip()
    return request or None


def _should_binary_close(otype: str | None, readiness: float | None) -> bool:
    """Волна 1, Шаг 10: бинарный дожим уместен, только если сомнение — «подумаю»
    И готовность к офферу уже высокая (≥0.6). Иначе — обычная отработка. Чистая."""
    try:
        r = float(readiness) if readiness is not None else 0.0
    except (TypeError, ValueError):
        r = 0.0
    return otype == "think" and r >= 0.6


def _offer_kbd_kind(track: str | None, hot: bool, depth: bool,
                    msg_text: str | None = None, obj_count: int = 0) -> str:
    """Чистый селектор клавиатуры отработки/дожима → 'bridge'|'club'. ОДНА точка правды.
    'bridge' (обе двери, 1:1 первым) когда: горячий/глубокий сегмент (события/трек);
    ИЛИ в САМОМ возражении СВЕЖО звучат depth/hot-слова (кросс-селл вверх, Волна 2);
    ИЛИ это последняя отработка перед потолком (obj_count ≥ OBJECTION_CAP-1) — тогда
    альтернатива 1:1 вместо той же двери Клуба. Иначе 'club'. Тестируется без БД."""
    if track == "T4" or hot or depth:
        return "bridge"
    if msg_text and (_HOT_RE.search(msg_text) or _DEPTH_RE.search(msg_text)):
        return "bridge"
    if obj_count >= OBJECTION_CAP - 1:
        return "bridge"
    return "club"


async def _after_offer_kbd(user_id: int, u_row: dict | None,
                           msg_text: str | None = None,
                           obj_count: int = 0) -> InlineKeyboardMarkup:
    """Сегментная клавиатура отработки/дожима: горячим/глубоким (события/трек),
    свежему depth/hot прямо в возражении (кросс-селл, Волна 2) и на последней
    отработке перед потолком — обе двери; иначе один Клуб-CTA (совещание 04.07).
    Крэш-сейф → Клуб. Выбор — в чистом _offer_kbd_kind (тестируется без БД)."""
    hot = depth = False
    try:
        hot = await events_count_recent(user_id, "lead_hot_kw", 48) > 0
        depth = await events_count_recent(user_id, "depth_intent", 48) > 0
    except Exception:
        pass
    kind = _offer_kbd_kind((u_row or {}).get("lead_track"), hot, depth,
                           msg_text, obj_count)
    return _bridge_kbd() if kind == "bridge" else _club_only_kbd()


async def send_soc_proof_video(bot, chat_id: int) -> bool:
    """Волна 2, Шаг 9: соц-пруф «Смотрим с Алёной» — видео живого разбора на
    возражении доверия. file_id берём из bot_meta (ключ socproof_video_file_id):
    ЛОКАЛЬНОГО файла на Render НЕТ, продюсер сеет file_id отдельно. Пусто/сбой
    отправки → False (вызывающий фолбэчит на обычную trust-отработку). Крэш-сейф."""
    try:
        file_id = (await get_meta("socproof_video_file_id") or "").strip()
        if not file_id:
            return False
        await bot.send_video(chat_id, video=file_id)
        return True
    except Exception:
        logger.warning("soc-proof video send failed (fallback to text)", exc_info=True)
        return False


_EXHAUSTED_TEXT = (
    "Бесплатная встреча у нас уже была — вторых не даю.\n\n"
    "Хочешь продолжать со мной — это Клуб «Манифест»: две такие встречи, как эта, каждый "
    "месяц + утреннее аудио «Манифест дня» + живой эфир раз в неделю + "
    "закрытый чат круга + мои письма. 990 ₽/мес.\n\n"
    "А если тянет вглубь и лично — личные встречи 1:1, по подписке."
)

_EXHAUSTED_VOICE = (
    "Бесплатная встреча у нас уже была — вторых не даю. Хочешь продолжать со "
    "мной — это Клуб «Манифест»: две такие встречи, как эта, каждый "
    "месяц, утреннее аудио «Манифест дня», живой эфир раз в неделю, "
    "закрытый чат круга и мои письма. А если тянет вглубь "
    "и лично — личные встречи один на один, по подписке. Обе двери — под этим сообщением."
)


_EXHAUSTED_MEMBER_TEXT = (
    "Наши две встречи этого месяца уже были — новые придут со свежим месяцем.\n\n"
    "Я рядом каждый день: утренний «Манифест дня», эфир на неделе, чат круга.\n\n"
    "А если тянет глубже и лично прямо сейчас — личные встречи 1:1, по подписке."
)

_EXHAUSTED_MEMBER_VOICE = (
    "Наши две встречи этого месяца уже были — новые придут со свежим месяцем. "
    "Я рядом каждый день: утренний «Манифест дня», эфир на неделе, чат круга. "
    "А если тянет глубже и лично прямо сейчас — возьми это в личную работу один "
    "на один, по подписке. Дверь — под этим сообщением."
)


def _one_on_one_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Личные встречи 1:1 — подписка", url=ONE_ON_ONE_URL)],
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
    async with _user_lock(user.id):
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
                # Мандат Кая 06.07 (вскрыто тестом боем): Тень пришла ПОВЕРХ живой
                # встречи — кружок задал СВОЙ вопрос, а тот не попадал ни в историю,
                # ни текстом (голое «мы уже во встрече») → мозг вёл по старой ветке,
                # клиентка теряла нить. Алёна ВЕДЁТ, а не отдаёт инициативу: вопрос
                # Тени — в историю сессии + перед глазами + подсказки первого шага.
                q = _SHADOW_HOOK_Q.get(code, "Что у тебя сейчас болит на самом деле?")
                try:
                    await ai_add_message(sess["id"], user.id, "model", f"«{q}»")
                except Exception:
                    logger.warning("seed shadow q to live session failed", exc_info=True)
                try:
                    await target.answer(
                        f"Мы уже во встрече — и твоя Тень как раз к месту. Теперь мой "
                        f"вопрос такой:\n\n«{q}»\n\n"
                        "Твой ход 🎙 — или коснись подсказки ниже:",
                        parse_mode=None, reply_markup=_first_step_kbd(code))
                    await log_event(user.id, "first_step_kbd")
                except Exception:
                    logger.warning("shadow-over-live kbd failed (plain)", exc_info=True)
                    await target.answer(f"«{q}»", parse_mode=None)
                return True
            try:
                await ai_close_all_active(user.id)
                await log_event(user.id, "session_reopen_stale", str(sess["id"]))
            except Exception:
                logger.warning("stale session close failed", exc_info=True)
                # Не молчать: осиротевшую встречу закрыть не смогли, но клиентка после
                # Тени не должна остаться в тишине — живой текст возврата ко входу.
                try:
                    await target.answer(
                        "Секунду, я тут — начнём заново: нажми /alena", parse_mode=None)
                except Exception:
                    pass
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
        await callback.message.answer(
            "Хорошо — тогда веди ты. С чего острее: что давит на тебя снаружи или "
            "что грызёт изнутри? Говори голосом или пиши буквами — как ляжет.",
            parse_mode=None)
        return
    else:                                          # легаси alena:f1..f3
        text = _FIRST_STEPS.get(parts[1])
    if not text:
        return
    # I9: если встреча закрылась (Render усыпил контейнер / истекла), тап подсказки
    # раньше давал эхо реплики и тишину (_talk молча выходит без сессии). Теперь —
    # не роняем в пустоту, а мягко возвращаем ко входу.
    if not await ai_active_session(callback.from_user.id):
        await callback.message.answer(
            "Кажется, наша встреча закрылась, пока тебя не было. Нажми /alena — "
            "и вернёмся ровно сюда.", parse_mode=None)
        return
    await callback.message.answer(f"— {text}", parse_mode=None)
    await _talk(callback.message, text, user_override=callback.from_user)


# Волна 1: «Сначала расскажи подробнее» под карточкой оффера → короткий разбор,
# чем разнятся Клуб и 1:1 (текст №4, tone-gate), + повтор той же клавиатуры сегмента.
_OFFER_MORE_TEXT = (
    "Расскажу без прикрас. Клуб — это ритм: каждое утро тебе приходит «Манифест "
    "дня», пять минут, чтобы не проснуться в старой яме. Дважды в месяц садимся "
    "лицом к лицу. И всё время рядом женщины, при которых можно снять броню. "
    "Девятьсот девяносто рублей. Личная работа — другое: только ты и я, никакого "
    "зала. Час — семь тысяч, три встречи разом — восемнадцать. Там идём в самую "
    "глубину твоего. Что тянет сильнее — тёплый круг или только мы вдвоём?")


@alena_router.callback_query(F.data == "alena:more")
async def cb_offer_more(callback: CallbackQuery):
    """Разбор «что входит» + повтор клавиатуры сегмента. Жать можно сколько угодно,
    но сам текст №4 повторяем не чаще 1×/10 мин (щит от спама, клавиатура — всегда)."""
    await callback.answer()
    uid = callback.from_user.id
    kbd = _member_offer_kbd() if await _is_club_member(uid) else _offer_kbd()
    try:
        shown_recently = await events_count_recent(uid, "offer_more", minutes=10) > 0
    except Exception:
        shown_recently = False
    if shown_recently:
        return   # уже показывали недавно — не дублируем, кнопки на карточке живы
    if not await _send_alive(callback.message, _OFFER_MORE_TEXT, kbd):
        await callback.message.answer(_OFFER_MORE_TEXT, parse_mode=None, reply_markup=kbd)
    await log_event(uid, "offer_more")


# ── Вход ──────────────────────────────────────────────────────────────────────

# Тень-гейт (мандат Кая 06.07): ВЕСЬ трафик идёт через тест Тени — это и есть
# воронка. Открывать встречу с Алёной БЕЗ пройденной Тени с входа мы не будем,
# иначе воронка ломается. Прошёл Тень → бесплатная demo-встреча (движок продаёт
# Клуб) остаётся; Клуб = 2 встречи/мес отдельным доступом; позже — «моя Алёна»
# отдельным платным продуктом. Whitelist — всегда (наши тесты).
_NEED_SHADOW_TEXT = (
    "Со мной — только по-настоящему. А для этого мне нужно сперва увидеть, с "
    "какой Тенью ты сейчас живёшь: иначе разговор пойдёт вслепую.\n\n"
    "Пройди короткий тест — десять вопросов — и вернёмся к разговору уже по делу.")


def _need_shadow_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌑 Узнать свою Тень", callback_data="quiz")],
    ])


async def _meeting_gate(user) -> str:
    """→ 'ok' | 'need_shadow'. Whitelist → ok; без пройденной Тени → need_shadow.
    Крэш-сейф: сбой чтения = ok (не отсекаем реального юзера из-за сбоя D1)."""
    if _is_unlimited(user):
        return "ok"
    try:
        u = await get_user(user.id)
        if not (u and u.get("shadow_dist")):
            return "need_shadow"
    except Exception:
        logger.warning("_meeting_gate: get_user failed for %s → allow", user.id,
                       exc_info=True)
    return "ok"


async def _send_need_shadow(target: Message):
    await target.answer(_NEED_SHADOW_TEXT + "\n\n— Алёна",
                        reply_markup=_need_shadow_kbd(), parse_mode=None)


async def _entry(target: Message, user):
    """Показывает дисклеймер (один раз) + интро + кнопку «Начать»."""
    if await ai_active_session(user.id):
        await target.answer("Мы уже во встрече — просто пиши, я здесь.")
        return
    if await _meeting_gate(user) == "need_shadow":
        await _send_need_shadow(target)
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
    async with _user_lock(user.id):
        if await ai_active_session(user.id):
            await callback.message.answer("Мы уже во встрече — пиши.")
            await callback.answer()
            return
        if await _meeting_gate(user) == "need_shadow":
            await _send_need_shadow(callback.message)
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
        # Встреча УЖЕ открыта (сессия создана выше). Сбой отправки опенера НЕ должен
        # всплывать как «не удалось открыть встречу» (_ALENA_FAIL) — это вводило в
        # заблуждение (баг вскрыт на тесте 06.07). Ловим тут, мягко просим написать.
        try:
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
        except Exception:
            logger.warning("opener send failed AFTER session open for %s", user.id,
                           exc_info=True)
            try:
                await callback.message.answer(
                    "Я здесь, встреча открыта. Напиши, с чем ты сейчас — и я отвечу.",
                    parse_mode=None)
            except Exception:
                # Тройной провал опенера: встреча открыта, а клиентка в тишине — не
                # быть слепыми (событие + сирена админу, образец «оба движка упали»).
                try:
                    await log_event(user.id, "opener_totally_lost")
                except Exception:
                    pass
                try:
                    if settings.tg_admin_id:
                        await callback.message.bot.send_message(
                            settings.tg_admin_id,
                            f"🚨 Воронка: опенер НЕ доставлен клиенту {user.id} — "
                            "встреча открыта, отправка упала трижды.")
                except Exception:
                    pass
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


_EMPTY_REPLY_STUB = "Я рядом — скажи это чуть иначе, я слушаю."


def _ensure_reply(reply: str) -> str:
    """Пустая реплика (модель вернула почти одну служебку) → заглушка ВСЕГДА,
    включая ход закрытия (аудит воронки 06.07): иначе финальная реплика перед
    оффером уходит пустой, а _send_alive не покажет ни текста, ни кнопок = «бот
    умер». Закрытие-агностична (без параметра closed)."""
    return reply if (reply or "").strip() else _EMPTY_REPLY_STUB


def _last_question(text: str) -> str | None:
    """Хвост реплики для текст-дублёра после голосового.

    Есть вопрос — берём последний (короткий финальный склеиваем с предыдущим,
    фидбек Кая 03.07: огрызок «А телу как?» без пары теряет смысл). Вопроса НЕТ
    — дублируем ХВОСТ самой реплики (последнее предложение, до 200 симв.), а не
    шаблон: перед глазами клиентки — ЕЁ настоящие слова (аудит воронки 06.07).
    Совсем пусто → None (тогда вызывающий даёт ротацию форм)."""
    t = (text or "").strip()
    if not t:
        return None
    qs = [s.strip() for s in re.findall(r"[^.!?…\n]*\?", t)]
    qs = [q for q in qs if 6 <= len(q) <= 200]
    if qs:
        if len(qs) >= 2 and len(qs[-1]) < 45:
            pair = f"{qs[-2]} {qs[-1]}"
            if len(pair) <= 220:
                return pair
        return qs[-1]
    # Вопроса нет → хвост реплики (последнее предложение, обрезка до 200 симв.).
    sents = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", t) if s.strip()]
    tail = sents[-1] if sents else t
    if len(tail) > 200:
        tail = tail[-200:].lstrip()
    return tail or None


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


async def _keep_recording(bot, chat_id: int, stop: "asyncio.Event"):
    """Индикатор жизни «записывает видео…» пока рендерится кружок-оффер (2–4 мин).

    По образцу _keep_typing: Telegram гасит chat_action через ~5с → пере-пингуем
    каждые ~4с до сигнала stop, чтобы клиентка видела, что оффер готовится, а не
    тишину (аудит воронки 06.07). Крэш-сейф, никогда не роняет оффер."""
    try:
        while not stop.is_set():
            try:
                await bot.send_chat_action(chat_id, "record_video_note")
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass
    except Exception:
        pass


async def _send_alive(message, text: str, reply_markup=None) -> bool:
    """Ответ Алёны «вживую»: печатает → пауза → приходит МАКСИМУМ двумя репликами.

    Фидбек Кая 02.07: «текста выплывает много и сразу несколькими сообщениями —
    сбивает» → не больше 2 пузырей и паузы длиннее (человек так печатает).

    Возвращает True, если ушёл хоть один пузырь. Сбой отдельного пузыря не рвёт
    ход (аудит воронки 06.07): ловим, пробуем следующий; упало ВСЁ → False, чтобы
    вызывающий не считал реплику доставленной."""
    bubbles = _split_bubbles(text, max_bubbles=2)
    if not bubbles:
        return False
    any_sent = False
    for i, chunk in enumerate(bubbles):
        last = (i == len(bubbles) - 1)
        try:
            await _typing(message)
            await asyncio.sleep(min(5.0, max(1.6, len(chunk) / 38)))  # читает/думает/печатает
            await message.answer(chunk, parse_mode=None,
                                 reply_markup=(reply_markup if last else None))
            any_sent = True
        except Exception:
            logger.warning("_send_alive: пузырь не ушёл (пробую следующий)", exc_info=True)
    return any_sent


@alena_router.message(F.text, _InAlenaFilter())
async def on_alena_talk(message: Message):
    """Текст во время активной встречи → общий обработчик хода."""
    try:
        await _talk(message, message.text)
    except Exception:
        # Сбой хода не оставляет клиентку в тишине (аудит воронки 06.07). НЕ врём
        # про «не удалось открыть» — встреча жива, просто просим повторить.
        logger.exception("on_alena_talk failed for %s", message.from_user.id)
        try:
            await message.answer(
                "Ой, меня на секунду тут заело — не с тобой, а с проводами. Скажи "
                "это ещё разок, я уже здесь и слушаю.",
                parse_mode=None)
        except Exception:
            pass


async def _talk(message: Message, text: str, by_voice: bool = False,
                user_override=None):
    """Один ход встречи (текст ИЛИ расшифрованный голос) → ответ Алёны.

    by_voice=True — человек говорил голосом → Алёна отвечает голосом.
    user_override — реальный юзер, когда ход пришёл callback-кнопкой (W1): у
    callback.message from_user = бот, брать его нельзя."""
    user = user_override or message.from_user
    async with _user_lock(user.id):
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
        # Мандат Кая 04.07: память между встречами — ТОЛЬКО купившим. Бесплатная
        # тест-встреча всегда с чистого листа (память приплетала прошлые прогоны);
        # пришла через покупку (Клуб/1:1) — досье работает. ЕДИНЫЙ чекпойнт (_gate_dossier)
        # — гейтуем ЗДЕСЬ, один раз: `dossier` ниже по потоку уже безопасен ВЕЗДЕ,
        # включая легаси v1-путь (_generate/build_system), который раньше получал
        # СЫРОЕ досье мимо гейта (корень галлюцинаций, аудит 04.07).
        dossier = await _gate_dossier(user.id, (u or {}).get("dossier"))
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
                        fresh=(turns <= 1), force_voice=by_voice, tg_id=user.id)
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
        # 🔴 Денежный фикс (05.07, диагностика): раньше закрытие висело ТОЛЬКО на
        # маркере/фазе. Мозг часто прощался нарративно («на сегодня достаточно»,
        # «этого пока хватит») БЕЗ маркера → _after_close не звался → НЕ было ни
        # оффера, ни кружка, ни дожима (прямая потеря денег). Детектор завершающих
        # фраз = 4-я нога флага: сказала «хватит/достаточно/остановимся» → закрываем
        # с оффером в любом случае. Path-agnostic (чинит и мозг v2, и v1).
        _closing_phrase = bool(re.search(
            r"(на\s+сегодня|на\s+сейчас|пока)\s+(этого\s+)?(достаточно|хватит)"
            r"|этого\s+(пока\s+)?(достаточно|хватит)"
            r"|(давай\s+)?(на\s+этом\s+)?(и\s+)?остановимся"
            r"|это\s+была\s+тво[яю]\s+(пробн|бесплатн|перв)", reply, re.I))
        # 🔴 Сигнал ГОТОВНОСТИ от КЛИЕНТКИ (её реплика этим ходом) = железный триггер
        # закрытия. На прогоне Кая клиентка написала «что дальше?» — прямой запрос
        # следующего шага — а мозг проигнорил и задал ещё вопрос (воронка не доводит
        # до продажи). Теперь ловим это детерминированно: «что дальше/что теперь/я
        # готова/и что» → закрываем с оффером, что бы мозг ни сгенерил.
        _user_ready = bool(re.search(
            r"^\s*(и\s+)?(что|как)\s+(же\s+)?(дальше|теперь|потом|мне\s+делать|делать)"
            r"|я\s+готов[ая]|готова\s+(идти|дальше|двигаться)"
            r"|что\s+ты\s+предлага|как\s+с\s+тобой", (text or ""), re.I))
        # 🔴 Согласие на МОСТ (мандат Кая 06.07, вскрыто тестом боем): прошлая реплика
        # Алёны звала перейти к главному («Идём?/продолжим?/готова?/начнём?»), а клиентка
        # отвечает коротким «да/давай/идём/го» — это ГОТОВНОСТЬ к офферу. Без этого «да»
        # на «Идём?» уходило в НОВЫЙ круг — воронка не доводила до продажи (потеря денег).
        _prev_alena = ""
        try:
            for _m in reversed(history or []):
                if isinstance(_m, dict) and _m.get("role") == "model":
                    _prev_alena = _m.get("content") or _m.get("text") or _m.get("message") or ""
                    break
        except Exception:
            _prev_alena = ""
        _bridge_invite = bool(re.search(
            r"(ид[её]м|продолжим|готова(\s+(идти|дальше))?|начн[её]м|поехали"
            r"|двин[еуё]мся|пойд[её]м|погнали)\s*[?!.…]*\s*$",
            (_prev_alena or "").strip(), re.I))
        _user_affirm = bool(re.search(
            r"^\s*(да|ага|угу|давай(те)?|ид[её]м|го|поехали|погнали|хочу|конечно"
            r"|согласна|можно|нужно|ок(ей)?|начн[её]м|вперёд|вперед)\b",
            (text or "").strip(), re.I))
        _bridge_yes = _bridge_invite and _user_affirm
        closed = (CLOSE_MARK in reply) or force_close \
            or (brain_phase == "native_offer") or _closing_phrase or _user_ready \
            or _bridge_yes
        reply = reply.replace(CLOSE_MARK, "").strip()
        reply, request = extract_request(reply)
        reply, dossier_new = extract_dossier(reply)
        # State-маркер фазы [[PHASE:…]] (карта 12 шагов): каноничная фаза уже пришла из
        # диагноза (brain_phase), но если модель вписала маркер в живой текст — вырезаем
        # ДО отправки, чтобы служебка не утекла в TTS/кружок (полный закрытый [[PHASE]]
        # не ловится dangling-защитой).
        reply, _ = extract_phase(reply)
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
        # заглушка ВСЕГДА, включая ход закрытия (без неё финальная реплика перед
        # оффером уходит пустой — аудит воронки 06.07).
        reply = _ensure_reply(reply)
        await ai_add_message(sid, user.id, "model", reply)

        if closed:
            await ai_close_all_active(user.id)   # A2: все активные, не только текущая
            if request:
                await ai_set_last_request(user.id, request)
            # Финальная реплика встречи — голосом (мандат 03.07), фолбэк текст.
            # КРИТИЧНО (аудит воронки 06.07): сбой отправки финальной реплики НЕ должен
            # съесть оффер — _after_close (кружок/карточка/дожим) обязан вызваться,
            # поэтому отправка в try/finally.
            try:
                if await send_voice_reply(message, reply):
                    await log_event(user.id, "voice_reply", "close")
                else:
                    await _send_alive(message, reply)
            finally:
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
                    if q.rstrip().endswith("?"):
                        heads = ("Мой вопрос: «%s»", "«%s»", "Вопрос перед глазами: «%s»",
                                 "Оставлю здесь: «%s»")
                    else:
                        # Вопроса в реплике нет — дублируем ХВОСТ её слов (не шаблон),
                        # нейтральной рамкой (аудит воронки 06.07: перед глазами — ЕЁ
                        # реплика, а не «мой вопрос» о невопросе).
                        heads = ("Оставлю здесь: «%s»", "«%s»", "Побудь с этим: «%s»",
                                 "Перед глазами: «%s»")
                    await message.answer(heads[turns % len(heads)] % q, parse_mode=None)
                else:
                    # Совсем пустая реплика (край) — ротация форм, чтобы после голосового
                    # не выглядело как «бот замолчал» (фидбек Кая 06.07). Не робот.
                    cues = ("Я рядом — что из этого отзывается? 🎙",
                            "Что тут твоё? Скажи как есть — голосом или текстом 🎙",
                            "Побудь с этим. Твой ход 🎙")
                    await message.answer(cues[turns % len(cues)], parse_mode=None)
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
    """Отработка сомнения после оффера: классифицируем возражение → своя продающая
    ветка (Шаг 9 воронки, единый обработчик). Потолок OBJECTION_CAP отработок
    суммарно держит _AfterOfferFilter (после него не давим); на самой последней —
    мягкий выход в директиве. Тип возражения тегаем в событие для /sources."""
    user = message.from_user
    otype = classify_objection(message.text)
    await log_event(user.id, "objection", otype)
    # Счётчик отработанных возражений суммарно (включая текущее) — 48ч окно как в
    # _AfterOfferFilter; на потолке objection_directive добавит мягкий выход.
    try:
        obj_count = await events_count_recent(user.id, "objection", 48)
    except Exception:
        obj_count = 1
    u = await get_user(user.id)
    request = (u or {}).get("last_ai_request") or ""
    # Волна 2, Шаг 9: соц-пруф «Смотрим с Алёной» на возражении ДОВЕРИЯ. Показываем
    # ОДИН раз (флаг-событие soc_proof_shown): подводка №6 голосом → видео (file_id
    # из bot_meta) → послесловие №6 + двери. Это И ЕСТЬ отработка trust этого раунда
    # (objection уже залогирован выше) — цикл не удлиняем. Нет file_id / видео не
    # ушло → ПОЛНЫЙ фолбэк на обычную trust-директиву ниже (продажа не теряется).
    if otype == "trust":
        try:
            shown = await events_count_recent(user.id, "soc_proof_shown", 24 * 365) > 0
        except Exception:
            shown = True   # не смогли проверить → не спамим видео, идём обычным путём
        # file_id есть в bot_meta? проверяем ДО подводки, чтобы не пообещать «покажу»
        # впустую (на Render его нет, пока продюсер не засеял) → обычная отработка.
        if not shown and bool(await get_meta("socproof_video_file_id")):
            pre = ("Слышу тебя. И не спорю — обещаний вокруг тьма, каждый второй "
                   "клянётся спасти. Поэтому доказывать на словах не стану — покажу. "
                   "Вот живой кусок настоящей работы: как это идёт по-честному, без "
                   "блеска и монтажа. Погляди — а потом сама скажешь.")
            if not await send_voice_reply(message, pre):
                await message.answer(pre, parse_mode=None)
            if await send_soc_proof_video(message.bot, message.chat.id):
                await log_event(user.id, "soc_proof_shown")
                post = ("Вот так это и происходит — ничего волшебного, просто два "
                        "живых человека и правда между ними. Ну что, готова "
                        "попробовать это на себе — в кругу или наедине? Двери ниже.")
                # Текст обещает ОБЕ двери («в кругу или наедине») → клавиатура обязана
                # их дать (аудит W2 #1): соц-пруф = пик интента, обе двери оправданы.
                kbd = _bridge_kbd()
                if not await send_voice_reply(message, post, kbd):
                    await message.answer(post, parse_mode=None, reply_markup=kbd)
                return
            # видео не ушло (сбой после чтения file_id) → обычная trust-отработка ниже
    # Волна 1, Шаг 10: «подумаю» при уже высокой готовности (offer_readiness≥0.6) →
    # не размазываем директиву, а бьём бинарным дожимом: выбор только КАК (текст №5,
    # дословно, голосом; фолбэк текст) + клавиатура сегмента.
    try:
        _readiness = ((await get_client_model(user.id)) or {}).get("offer_readiness")
    except Exception:
        _readiness = None
    if _should_binary_close(otype, _readiness):
        binary = (
            "Смотри. Вопрос сейчас не «идти или нет» — это ты уже решила одним: ты "
            "всё ещё здесь. Остаётся только КАК. Мягко и вместе — Клуб, где каждый "
            "день чувствуешь плечо. Или сразу вглубь, наедине, где я берусь за то "
            "самое, что болит. Куда ступить — в круг или ко мне вплотную? Обе двери "
            "ждут ниже.")
        # Бинарная вилка обещает ОБЕ двери — клавиатура обязана их дать (аудит W2 #1).
        kbd = _bridge_kbd()
        await log_event(user.id, "binary_close")
        if not await send_voice_reply(message, binary, kbd):
            await message.answer(binary, parse_mode=None, reply_markup=kbd)
        return
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
            objection_directive(otype, request, obj_count),
            "native_offer", name, archetype, history, voice_mode=True, tg_id=user.id)
        reply, _ = extract_request(reply)
        reply, _ = extract_phase(reply)
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
    # теряя дверь 1:1. Держим сегментную клавиатуру и здесь (крэш-сейф). Волна 2:
    # свежий depth/hot в возражении и потолок отработок тоже поднимают обе двери.
    kbd = await _after_offer_kbd(user.id, u, message.text, obj_count)
    if not await send_voice_reply(message, reply, kbd):
        await message.answer(reply, parse_mode=None, reply_markup=kbd)


# ── Hermes #1: «затихшая» встреча → один мягкий оффер Клуба ────────────────────
# Человек начал встречу, Алёна задала вопрос/сделала оффер — и он замолчал на
# пике. Оффер не должен теряться в тишине: через N минут молчания шлём ОДИН
# тёплый нудж с дверью в Клуб. Встреча остаётся открытой — можно ответить дальше.
_STALE_NUDGE_TEXT = (
    "Ты затихла — и это нормально. Иногда то, что мы задели, нужно донести молча.\n\n"
    "А если почувствуешь, что не хочешь оставаться с этим одна — я рядом каждый "
    "день в Клубе «Манифест»: две наши встречи в месяц, утреннее аудио, живой эфир раз в неделю, закрытый чат. "
    "990 в месяц, чтобы не разбираться в одиночку.\n\n"
    "Решать сейчас необязательно. Можно шагнуть дальше прямо в эту минуту — а можно "
    "побыть с тем, что поднялось, и вернуться позже, когда согреешься. Я никуда не "
    "денусь ни в том, ни в другом.\n\n— Алёна"
)

# Тот же нудж устной речью (для TTS): без переносов-подписи, бережная вилка-финал.
_STALE_NUDGE_VOICE = (
    "Ты затихла — и это нормально. Иногда то, что мы задели, нужно донести молча. "
    "А если почувствуешь, что не хочешь оставаться с этим одна — я рядом каждый "
    "день в Клубе «Манифест»: две наши встречи в месяц, утреннее аудио, живой эфир раз в неделю, закрытый чат. "
    "Решать сейчас необязательно. Можно шагнуть дальше прямо в эту минуту — а можно "
    "побыть с тем, что поднялось, и вернуться позже, когда согреешься. Я никуда не "
    "денусь ни в том, ни в другом."
)


# ── T-1 (03.07): само-восстановление потерянного хода ─────────────────────────
# Render-редеплой убивает контейнер посреди генерации (15–40с): реплика клиентки
# записана, ответа Алёны нет — тишина (вскрыто прогоном Кая 03.07 12:00).
# Тик находит такие встречи и доотвечает сам.
_LADDER_TEXT = (
    "Мы рядом уже пару недель. Если чувствуешь, что какая-то тема просится "
    "глубже, чем чат и эфиры, — есть живой разбор один на один: час только "
    "про тебя. Дверь — под этим сообщением."
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
            # ЕДИНЫЙ чекпойнт памяти (_gate_dossier) — та же гарантия, что и в _talk.
            gated_dossier = await _gate_dossier(tg_id, (u or {}).get("dossier"))
            if gated_dossier:
                profile = f"досье прошлых встреч: {gated_dossier[:600]}"
            name = None  # имени из Message тут нет; мозг ведёт без обращения
            reply, new_cm, signals, track = await brain_turn(
                history, name, archetype, await get_client_model(tg_id),
                profile, fresh=False, tg_id=tg_id)
            await save_client_model(tg_id, json.dumps(new_cm, ensure_ascii=False))
            reply = strip_dangling_markers(extract_phase(
                extract_score(extract_dossier(extract_request(
                    reply.replace(CLOSE_MARK, ""))[0])[0])[0])[0]).strip()
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


# ── Волна 1: тексты оффер-механики (tone-gate Алёны, эталон funnel-texts-wave12) ──
# Тизер перед кружком (текст №1): идёт голосом ВО ВСЕХ сегментах, кнопка сегмента
# прикреплена сразу (мандат Кая: «кнопки — карточкой сразу за кружком, надёжный путь»).
_OFFER_TEASER = (
    "Погоди — самое важное я скажу тебе в лицо, не буквами: дай пару минут, запишу "
    "это для тебя одной. А внизу уже ждёт то, куда можно шагнуть дальше.")

# Карточка под кружком (текст №3): текст над кнопками оффера.
_OFFER_CARD = (
    "Выбор за тобой — вот три двери. Тронь ту, что ближе; захочешь понять, чем они "
    "разнятся, — жми «расскажи подробнее».")

# Карточка члену Клуба: у него две кнопки (Клуба нет — он уже внутри), «три двери»
# были бы враньём (флаг исполнителя Волны 1, правка продюсера через tone-правила).
_MEMBER_OFFER_CARD = (
    "Эта дверь — только про тебя. Шагни, когда откликнется; а если хочешь сначала "
    "разобраться, как всё устроено, — жми «расскажи подробнее».")

# Скрипт оффер-кружка члену Клуба (текст №8в): мост в 1:1, вход = одна пробная встреча.
_MEMBER_KRUZHOK_SCRIPT = (
    "Ты давно в кругу, и я вижу — одна тема у тебя переросла общий формат, ей тесно "
    "среди многих. Такое доразбирают наедине: только ты и я, час целиком на тебя. Не "
    "надо решаться на цикл сразу — возьми первую встречу, пробную, и почувствуй, "
    "твоё ли. Кнопка записи ниже.")


def _default_kruzhok_script(name: str | None, q: str) -> str:
    """Скрипт оффер-кружка дефолт-ветки (текст №2): {Имя}/«{запрос}» подставлены."""
    head = f"{name}, послушай меня." if name else "Послушай меня."
    return (
        f"{head} То, что сегодня поднялось со дна, — «{q}» — за один разговор мы это "
        "только увидели. Чтобы оно сдвинулось, к нему возвращаются снова и снова, "
        "бережно, а не рывком. Со мной это делают двумя путями. Первый — Клуб "
        "«Манифест», девятьсот девяносто в месяц: две встречи, как эта, каждое утро "
        "короткая опора от меня и женщины рядом, которым не надо ничего объяснять. "
        "Второй — работа только про тебя, наедине, где я веду тебя глубже, чем "
        "получается на людях. Что тянет сильнее — быть среди своих или сесть со мной "
        "с глазу на глаз? Обе двери прямо под этим видео.")


def _flagship_kruzhok_script(q: str) -> str:
    """T4/hot: существующий боевой оффер-флагман 1:1 как скрипт кружка (без правок)."""
    return (
        f"«{q}» — с этим не в переписку, и ты сама это чувствуешь. Такое не отражают "
        "в чате — это проживают: медленно, один на один, пока не отпустит. Скажу "
        "прямо: возьми свой запрос в личную работу со мной — встречи наедине, где "
        "есть только ты и он. А если сначала мягче — рядом Клуб, где можно просто "
        "побыть в кругу. Сомневаешься или есть вопрос — напиши, отвечу честно. Обе "
        "двери — под этим сообщением.")


def _depth_kruzhok_script(q: str) -> str:
    """depth_intent: существующий боевой depth-оффер как скрипт кружка (без правок)."""
    return (
        f"«{q}» — и ты сама потянулась глубже. Услышала. То, что просит изнутри, "
        "перепиской не закрыть — для такого есть живые встречи наедине, только про "
        "тебя. Входить постепенно — рядом Клуб: круг, эфиры, моя ежедневная опора. "
        "Что останавливает — спроси прямо здесь, я на связи. Обе двери — ниже.")


_OFFER_FALLBACK_TEXT = (
    "В Клубе «Манифест» продолжим ровно с этого места: две встречи со мной в месяц, каждое утро — моё аудио "
    "«Манифест дня», раз в неделю — живой эфир, и круг женщин в закрытом чате, "
    "где не нужно держать лицо.\n\n"
    "990 в месяц — чтобы не разбираться с этим одной. Дверь ниже.\n\n— Алёна")


async def _offer_kruzhok(bot, chat_id: int, tg_id: int,
                         script: str, kbd: InlineKeyboardMarkup,
                         card: str = _OFFER_CARD,
                         fallback: str = _OFFER_FALLBACK_TEXT):
    """Ф2 (мандат Кая 02.07): САМ ОФФЕР = именной видео-кружок Алёны.

    Волна 1: скрипт и клавиатура сегментные — их задаёт _after_close (дефолт/член/
    T4/depth). Рендер твина ~2–4 мин → идёт фоном после тизера. После кружка —
    карточка №3 с кнопками (надёжный путь). Сбой рендера → страховка: оффер голосом,
    дальше текстом, с ТОЙ ЖЕ клавиатурой — продажа не теряется никогда."""
    try:
        await add_circle_credits(tg_id, CIRCLE_CREDITS)  # леджер ДО рендера (антидубль)
        # Индикатор жизни на время рендера твина (2–4 мин): фоновый chat_action,
        # чтобы клиентка не видела глухую тишину (аудит воронки 06.07).
        _stop_rec = asyncio.Event()
        _recorder = asyncio.create_task(_keep_recording(bot, chat_id, _stop_rec))
        try:
            sent = await send_kruzhok_to(bot, chat_id, script)
        finally:
            _stop_rec.set()
            try:
                await _recorder
            except Exception:
                pass
        if sent:
            await log_event(tg_id, "offer_kruzhok", "sent")
            await bot.send_message(
                chat_id, card, reply_markup=kbd, parse_mode=None)
            return
    except Exception:
        logger.warning("offer kruzhok failed (fallback text)", exc_info=True)
    # Страховка: кружок не собрался → оффер голосом (сбой видео ≠ сбой TTS),
    # дальше текстом — кнопка обязана дойти в любом случае.
    try:
        # Фолбэк сегментный (аудит W1 #2): члену Клуба нельзя питчить Клуб без
        # кнопки Клуба — его fallback = мост в 1:1 (передаётся из _after_close).
        spoken = " ".join(fallback.replace("— Алёна", "").split())
        if await send_voice_to(bot, chat_id, spoken, kbd):
            await log_event(tg_id, "offer_kruzhok", "fallback_voice")
            return
        await log_event(tg_id, "offer_kruzhok", "fallback_text")
        await bot.send_message(chat_id, fallback,
                               reply_markup=kbd, parse_mode=None)
        return
    except Exception:
        logger.warning("offer fallback send failed", exc_info=True)
    # Сюда попадаем ТОЛЬКО если НИ кружок, ни голос, ни текст не ушли — оффер
    # потерян в тишине. Не быть слепыми (аудит воронки 06.07): событие + сирена
    # админу (образец «оба движка упали»).
    try:
        await log_event(tg_id, "offer_delivery_lost")
    except Exception:
        pass
    try:
        if settings.tg_admin_id:
            await bot.send_message(
                settings.tg_admin_id,
                f"🚨 Воронка: оффер НЕ доставлен клиенту {tg_id} — "
                "кружок+голос+текст все упали.")
    except Exception:
        pass


async def _nudge_channel(message: Message, user):
    """Мягкий нудж подписки на канал в КОНЦЕ встречи (пик доверия) — отдельным
    коротким сообщением ПОСЛЕ оффера, не в платной клавиатуре. Крэш-сейф.
    Уже подписанных не трогаем (бот админ @kydaidy → _is_subscribed надёжен;
    None/сбой → мягко покажем, cb_check_sub всё равно засчитает)."""
    try:
        from handlers import _is_subscribed, _subscribe_kbd
        if await _is_subscribed(message.bot, user.id) is True:
            return
        await message.answer(
            "И ещё — что бы ты сейчас ни выбрала, я никуда не денусь: в канале "
            "я рядом каждый день, там честное, без глазури. Если тебя там ещё "
            "нет — заходи.",
            parse_mode=None, reply_markup=_subscribe_kbd())
    except Exception:
        logger.warning("_nudge_channel failed (continuing)", exc_info=True)


async def _after_close(message: Message, user, request: str | None = None):
    # Сегментация оффера — ТОЛЬКО по реальному членству в Клубе (фикс 02.07:
    # whitelist-тестеры раньше улетали в VIP-ветку и не видели боевой путь —
    # кружок-оффер/дожимы; теперь тестовый аккаунт проходит как обычная клиентка,
    # безлимит встреч у него остаётся).
    is_member = await _is_club_member(user.id)

    # Волна 1: закрытие могло прийти БЕЗ маркера [[ЗАПРОС]] (жёсткие доводки —
    # TURN_CAP, «да» на мост) → request пуст, оффер-кружок раньше проваливался.
    # Реконструируем запрос из модели клиентки (настоящий → фасад), чтобы
    # кружок-оффер собрался всегда.
    if not request:
        cm = await get_client_model(user.id) or {}
        request = _request_from_cm(cm)

    # Вскрылся (или реконструирован) настоящий запрос → ведём дальше. Волна 1:
    # ВСЕ сегменты получают именной оффер-кружок. Схема одна: (а) тизер №1 голосом
    # с прикреплённой клавиатурой сегмента → (б) фоновый рендер кружка сегментным
    # скриптом → (в) карточка №3 с кнопками после кружка (надёжный путь, мандат Кая).
    if request:
        q = request.strip().rstrip(".")[:140]
        _name = user.first_name if re.search(r"[а-яА-ЯёЁ]", user.first_name or "") else None
        if is_member:
            # Член Клуба → мост в 1:1 (текст №8в), без двери Клуба (он уже внутри).
            await log_event(user.id, "offer_shown", "bridge_1on1")
            script = _MEMBER_KRUZHOK_SCRIPT
            kbd = _member_offer_kbd()
            card = _MEMBER_OFFER_CARD
            fallback = _MEMBER_KRUZHOK_SCRIPT  # сбой кружка → тот же мост в 1:1 текстом/голосом
        else:
            # Адаптивный порядок (Кай 02.07): ГОРЯЧЕЙ (трек T4 / lead_hot_kw) — скрипт
            # флагмана 1:1; сама тянулась глубже (depth_intent) — depth-скрипт; иначе
            # дефолт (текст №2). Скрипты флагмана/depth = боевые формулировки без правок.
            u_row = await get_user(user.id)
            hot_kw = (await events_count_recent(user.id, "lead_hot_kw", 48)) > 0
            if (u_row or {}).get("lead_track") == "T4" or hot_kw:
                await log_event(user.id, "offer_shown",
                                "flagship_1on1_kw" if hot_kw else "flagship_1on1_T4")
                script = _flagship_kruzhok_script(q)
            elif (await events_count_recent(user.id, "depth_intent", 48)) > 0:
                await log_event(user.id, "offer_shown", "depth_1on1")
                script = _depth_kruzhok_script(q)
            else:
                await log_event(user.id, "offer_shown", "club_request")
                script = _default_kruzhok_script(_name, q)
            kbd = _offer_kbd()
            card = _OFFER_CARD
            fallback = _OFFER_FALLBACK_TEXT
        # (а) тизер №1 голосом + клавиатура сегмента (кнопка уже с тизером — мандат Кая).
        if not await send_voice_reply(message, _OFFER_TEASER, kbd):
            await message.answer(_OFFER_TEASER, parse_mode=None, reply_markup=kbd)
        # (б) оффер-кружок сегментным скриптом → (в) карточка сегмента + kbd (внутри).
        asyncio.create_task(_offer_kruzhok(
            message.bot, message.chat.id, user.id, script, kbd, card, fallback))
        # Нудж канала/дожим — как раньше, только не члену (он уже внутри Клуба).
        if not is_member:
            await _nudge_channel(message, user)
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
    await _nudge_channel(message, user)
    await _schedule_followups(user.id)
