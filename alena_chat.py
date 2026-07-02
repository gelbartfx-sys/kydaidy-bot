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
    ai_active_session, ai_total_sessions,
    ai_open_session, ai_add_message, ai_get_messages, ai_bump_turns,
    ai_close_session, ai_set_last_request, save_dossier,
    ai_stale_sessions, ai_mark_nudged, save_lead_signals, set_lead_track,
    get_client_model, save_client_model,
    log_event, followup_schedule,
)
from alena_voice import send_voice_reply
from alena_persona import (
    build_system, DISCLAIMER, INTRO, CLOSE_MARK, is_crisis, CRISIS_REPLY,
    extract_request, extract_dossier, extract_score, strip_dangling_markers,
)
from alena_brain import brain_turn
from lead_policy import classify

logger = logging.getLogger(__name__)
alena_router = Router()

FREE_SESSIONS = 1          # бесплатных встреч на человека (пожизненно)
TURN_CAP = 20              # предохранитель: после стольких реплик — мягкое закрытие
HISTORY_LIMIT = 40         # сколько сообщений истории отдаём модели
ONE_ON_ONE_URL = "https://web.tribute.tg/p/vKG"
CLUB_URL = "https://t.me/tribute/app?startapp=sULY"


def _is_unlimited(user) -> bool:
    from handlers import _is_unlimited as _h  # late import: избегаем цикла
    return _h(user)


async def _is_club_member(tg_id: int) -> bool:
    return await get_active_subscription(tg_id, "manifest_club") is not None


async def _remaining(user) -> int | None:
    """Сколько бесплатных встреч осталось; None — безлимит.

    Безлимит — whitelist ИЛИ активная подписка Клуба «Манифест».
    Для остальных лимит пожизненный (1 на человека).
    """
    if _is_unlimited(user) or await _is_club_member(user.id):
        return None
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
    "Бесплатная встреча у нас уже была — одна на человека.\n\n"
    "Хочешь говорить со мной без лимита — это Клуб «Манифест»: безлимит наших "
    "встреч + эфир раз в неделю + закрытый чат. 990 ₽/мес.\n\n"
    "А если хочешь вглубь и со мной лично — сессия 1:1."
)


# ── Авто-контакт после теста Тени: Алёна заговаривает первой ──────────────────
# Хук-вопрос под каждую Тень (её голос: коротко, в лицо, кончается вопросом,
# от которого неуютно молчать). Рамку «Твоя Тень — X + тизер» собираем из ARCHETYPES.
_SHADOW_HOOK_Q = {
    "W": "Скажи честно: от кого ты чаще всего прячешь, что видишь?",
    "Q": "Кому ты последний раз позволила подойти близко — и почему так давно?",
    "H": "От кого ты ушла первой, хотя, если честно, не хотела?",
    "M": "Перед кем ты в последний раз сделала себя тише, чем ты есть?",
    "F": "Кто видел тебя настоящую — без игры и проверок?",
    "MR": "Кому ты отдала больше, чем осталось себе — и ждёшь, что вернут?",
    "R": "Против чего ты бунтуешь так, что бьёт по тебе самой?",
    "O": "Кого ты держишь на расстоянии, чтобы не успели уйти первыми?",
    "D": "Что ты однажды сожгла дотла — и до сих пор об этом молчишь?",
    "C": "Кого ты не впустила внутрь, хотя, может, хотела?",
}


def _shadow_opener(code: str) -> str:
    a = ARCHETYPES[code]
    q = _SHADOW_HOOK_Q.get(code, "Скажи честно — что у тебя сейчас болит на самом деле?")
    return (f"Вижу твою ведущую Тень — {a['name']}.\n\n{a['teaser']}\n\n"
            "Дальше — наша встреча: ты отвечаешь текстом или голосовым, я читаю и "
            "отвечаю; иногда мне нужна минута — я не исчезаю.\n\n"
            f"«{q}»\n\n— Алёна")


def _shadow_opener_short(code: str) -> str:
    # Онбординг-рамка (фидбек Кая 02.07: «нет приветствия, не объясняется формат,
    # непонятно что ведут»): что происходит, как устроено, чего ждать по времени —
    # и вопрос из кружка текстом перед глазами. Одно сообщение, без простыни.
    q = _SHADOW_HOOK_Q.get(code, "Что у тебя сейчас болит на самом деле?")
    return (
        "Это твоя пробная сессия со мной — одна, бесплатная, как настоящая встреча "
        "один на один.\n\n"
        "Смысл: твоя анкета показала, ГДЕ ты защищаешься. Здесь мы найдём, ЧТО за "
        "этим стоит на самом деле — твой настоящий запрос, а не тот, что сверху.\n\n"
        "Правила простые: отвечай честно, как есть — текстом или голосовым. Я читаю, "
        "думаю и отвечаю, иногда голосом. Если молчу минуту — я не исчезла, я думаю "
        "о тебе.\n\n"
        f"Начнём с вопроса из кружка:\n\n«{q}»\n\n— Алёна")


async def open_shadow_session(target: Message, user, code: str,
                              video_hook: bool = False) -> bool:
    """Сразу после портрета Тени — Алёна САМА открывает встречу с хуком под архетип.

    True  → встреча открыта (дальше говорит on_alena_talk, архетип уже втекает),
            либо исчерпавшей выдан хук-тизер + оффер Клуба;
    False → авто-контакт невозможен (нет ключа) → вызывающий покажет обычное меню.
    """
    if not settings.gemini_key:
        return False
    if await ai_active_session(user.id):
        return True  # уже говорим — не дублируем
    rem = await _remaining(user)
    if rem is not None and rem <= 0:
        # бесплатная встреча исчерпана → хук как тизер + Клуб (без траты модели)
        a = ARCHETYPES[code]
        await target.answer(
            f"Твоя Тень — {a['name']}. {a['teaser']}\n\n"
            "Мы это уже начинали разбирать. Продолжить без лимита — в Клубе «Манифест»: "
            "я рядом в чате и на эфирах, 990 ₽/мес.\n\n— Алёна",
            reply_markup=_club_kbd(), parse_mode=None)
        return True
    await ai_open_session(user.id)  # списание бесплатной встречи — здесь
    await log_event(user.id, "session_open", "auto")
    if await ai_total_sessions(user.id) <= 1:
        await target.answer(DISCLAIMER)
    # Первый контакт — «вживую»: печатает → приходит пузырями (эффект присутствия).
    # Если видео-кружок уже показал Тень — текстовый хук короткий, без дубля.
    opener = _shadow_opener_short(code) if video_hook else _shadow_opener(code)
    await _send_alive(target, opener, _pause_kbd())
    return True


# ── Вход ──────────────────────────────────────────────────────────────────────

async def _entry(target: Message, user):
    """Показывает дисклеймер (один раз) + интро + кнопку «Начать»."""
    if await ai_active_session(user.id):
        await target.answer("Мы уже во встрече — просто пиши, я здесь.")
        return
    rem = await _remaining(user)
    if rem is not None and rem <= 0:
        await target.answer(_EXHAUSTED_TEXT, reply_markup=_club_kbd())
        return
    if await ai_total_sessions(user.id) == 0:
        await target.answer(DISCLAIMER)
    tail = "" if rem is None else "\n\n_(это твоя бесплатная встреча — одна на человека)_"
    await target.answer(INTRO + tail, reply_markup=_start_kbd())


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
        await callback.message.answer(_EXHAUSTED_TEXT, reply_markup=_club_kbd())
        await callback.answer()
        return
    await ai_open_session(user.id)  # списание встречи — при старте
    await log_event(user.id, "session_open", "manual")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "Это твоя пробная сессия со мной — одна, бесплатная, как настоящая встреча "
        "один на один. Смысл: найти твой настоящий запрос — не тот, что сверху.\n\n"
        "Правила простые: отвечай честно, как есть — текстом или голосовым. Я читаю, "
        "думаю и отвечаю, иногда голосом. Если молчу минуту — я не исчезла, я думаю.\n\n"
        "Расскажи, что привело — с чем ты сейчас?\n\n— Алёна",
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
    """Последний вопрос из реплики (для текст-дублёра после голосового)."""
    qs = [s.strip() for s in re.findall(r"[^.!?…\n]*\?", text or "")]
    qs = [q for q in qs if 6 <= len(q) <= 160]
    return qs[-1] if qs else None


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


async def _talk(message: Message, text: str, by_voice: bool = False):
    """Один ход встречи (текст ИЛИ расшифрованный голос) → ответ Алёны.

    by_voice=True — человек говорил голосом → Алёна отвечает голосом (зеркало канала)."""
    user = message.from_user
    sess = await ai_active_session(user.id)
    if not sess:
        return
    sid = sess["id"]

    await ai_add_message(sid, user.id, "user", text)

    # Кризис — не зовём модель, сразу бережная эскалация (встреча остаётся открытой).
    if is_crisis(text):
        await message.answer(CRISIS_REPLY, parse_mode=None, reply_markup=_pause_kbd())
        return

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
                reply = None  # → фолбэк ниже на v1-путь

        if reply is None:
            try:
                reply = await _generate(history, user.first_name, povorot, archetype, force_close, dossier)
            except Exception as e:
                logger.exception(f"alena talk failed for {user.id}: {e}")
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
        await ai_close_session(sid)
        if request:
            await ai_set_last_request(user.id, request)
        await _send_alive(message, reply)
        await _after_close(message, user, request)
    else:
        # H1: канал хода решён в brain_turn (диагноз ИЛИ ключевая фаза → voice, и
        # текст уже написан как устная речь). Сбой TTS → тот же текст, ход не теряется.
        if brain_medium == "voice":
            if await send_voice_reply(message, reply, _pause_kbd()):
                await log_event(user.id, "voice_reply", brain_phase)
                # Ведение (фидбек Кая 02.07): после голосового — вопрос текстом перед
                # глазами + что сделать. Голосовое прослушал и забыл, текст остаётся.
                q = _last_question(reply)
                tail = (f"Мой вопрос: «{q}»\n\nОтветь текстом или голосовым 🎙" if q
                        else "Ответь, как есть — текстом или голосовым 🎙")
                await message.answer(tail, parse_mode=None)
                return
        await _send_alive(message, reply, _pause_kbd())


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


# ── Hermes #1: «затихшая» встреча → один мягкий оффер Клуба ────────────────────
# Человек начал встречу, Алёна задала вопрос/сделала оффер — и он замолчал на
# пике. Оффер не должен теряться в тишине: через N минут молчания шлём ОДИН
# тёплый нудж с дверью в Клуб. Встреча остаётся открытой — можно ответить дальше.
_STALE_NUDGE_TEXT = (
    "Ты затихла — и это нормально. Иногда то, что мы задели, нужно донести молча.\n\n"
    "Я не тороплю и не исчезаю. Захочешь продолжить — просто напиши, я здесь.\n\n"
    "А если почувствуешь, что не хочешь оставаться с этим одна — я рядом каждый "
    "день в Клубе «Манифест»: без лимита наших встреч, в чате и на эфирах. "
    "990 в месяц, чтобы не разбираться в одиночку.\n\n— Алёна"
)


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
            # текст сам делает оффер (из _pause_kbd кнопку убрали, фидбек Кая 02.07).
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


async def _after_close(message: Message, user, request: str | None = None):
    rem = await _remaining(user)

    # Вскрылся настоящий запрос → ведём дальше. Куда именно — по сегменту:
    if request:
        q = request.strip().rstrip(".")
        if rem is None:
            # Член Клуба / whitelist → тёплый докрут в 1:1 (я тебя уже знаю).
            await log_event(user.id, "offer_shown", "bridge_1on1")
            await message.answer(
                f"Твой настоящий запрос — вот он:\n\n«{q}»\n\n"
                "Я тебе его показала. Но показать — не значит прожить. Размотать это и "
                "правда поменять — работа для живой встречи, не для переписки.\n\n"
                "Готова взять этот запрос в работу со мной лично — вот дверь. После "
                "оплаты откроется мой календарь: выберешь окно и придёшь именно с этим."
                "\n\n— Алёна",
                reply_markup=_bridge_kbd(), parse_mode=None)
            return
        # Адаптивный порядок офферов (Кай 02.07): ГОРЯЧЕЙ (трек T4 — готова к шагу)
        # первым предлагаем ФЛАГМАН 1:1, Клуб — рядом второй строкой. Остальным —
        # Клуб (трипваер). Рынок: AI слабо закрывает высокий чек холодным, поэтому
        # 1:1-первым только по скорингу готовности.
        u_row = await get_user(user.id)
        if (u_row or {}).get("lead_track") == "T4":
            await log_event(user.id, "offer_shown", "flagship_1on1_T4")
            await message.answer(
                f"Твой настоящий запрос — вот он:\n\n«{q}»\n\n"
                "Показать я показала. Но такое разматывают не в переписке. Ты готова — "
                "я это вижу по нашему разговору. Поэтому скажу прямо: возьми этот запрос "
                "на живую встречу со мной, 1:1 — час только про тебя, именно с этим.\n\n"
                "Если хочешь мягче и постепенно — есть Клуб: я рядом каждый день, "
                "безлимит наших встреч, эфиры, круг женщин с похожими историями.\n\n— Алёна",
                reply_markup=_bridge_kbd(), parse_mode=None)
            await _schedule_followups(user.id)
            return
        # Бесплатная встреча исчерпана → первая ступень = Клуб (трипваер, решение Кая).
        # Копирайт Hermes: H1 (не ждёт) + H3 (ты из первых) + H5 (якорь цены).
        # H3 (Волна 1): личную часть оффера Алёна ГОВОРИТ голосом — доверие в момент
        # решения. Голос ушёл → текстом остаётся короткое CTA + кнопка; сбой TTS →
        # прежний полный текст, ничего не деградирует.
        await log_event(user.id, "offer_shown", "club_request")
        voiced = await send_voice_reply(
            message,
            f"Твой настоящий запрос — вот он: «{q}». Показать я показала. Но увидеть — "
            "не значит прожить. И оно не ждёт: каждую неделю, что ты в это не смотришь, "
            "оно тихо выбирает за тебя. Я рядом, пока ты это разматываешь — "
            "если решишь не оставаться с этим одна.")
        if voiced:
            await log_event(user.id, "voice_reply", "offer")
            await message.answer(
                "В Клубе «Манифест» я рядом без лимита — в чате и на эфирах каждую "
                "неделю. Клуб только открылся: ты заходишь одной из первых.\n\n"
                "990 в месяц — меньше, чем кофе раз в неделю, чтобы не быть с этим "
                "одной. А если захочешь сразу вглубь и лично — напиши, есть встреча "
                "1:1.\n\n— Алёна",
                reply_markup=_club_only_kbd(), parse_mode=None)
        else:
            await message.answer(
                f"Твой настоящий запрос — вот он:\n\n«{q}»\n\n"
                "Показать я показала. Но увидеть — не значит прожить. И оно не ждёт: каждую "
                "неделю, что ты в это не смотришь, оно тихо выбирает за тебя.\n\n"
                "В Клубе «Манифест» я рядом, пока ты это разматываешь — без лимита, в чате и "
                "на эфирах каждую неделю. Клуб только открылся: ты заходишь одной из первых — "
                "это твой круг с самого начала.\n\n"
                "990 в месяц — меньше, чем кофе раз в неделю, чтобы не быть с этим одной. "
                "А если захочешь сразу вглубь и лично — напиши, есть встреча 1:1.\n\n— Алёна",
                reply_markup=_club_only_kbd(), parse_mode=None)
        await _schedule_followups(user.id)
        return

    # Запроса не вскрылось / ей хватило — честно, без втюхивания.
    if rem is None:
        await message.answer(
            "На сегодня всё. Ещё разговор — просто /alena.", reply_markup=_menu_kbd())
        return
    await log_event(user.id, "offer_shown", "club_soft")
    await message.answer(
        "Это была твоя бесплатная встреча — одна на человека.\n\n"
        "Если захочешь продолжить — я рядом регулярно в Клубе «Манифест»: без лимита, "
        "в чате и на эфирах. Клуб только открылся, ты заходишь одной из первых. "
        "990 в месяц — чтобы не быть с этим одной.",
        reply_markup=_club_only_kbd())
    await _schedule_followups(user.id)
