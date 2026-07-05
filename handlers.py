"""Обработчики команд бота."""

from __future__ import annotations

import io
import re
import asyncio
import logging
from pathlib import Path

from aiogram import Router, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (
    Message, FSInputFile, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
)

from config import settings
from shadow_test import (
    ARCHETYPES, decode_distribution, winner_from_counts, encode_distribution,
)
from ai_quiz import generate_hero_image, generate_analysis_text
from profile_image import render_profile

# Публичные адреса для сборки ссылки на профиль.
BOT_PUBLIC_URL = "https://kydaidy-bot.onrender.com"
SITE_URL = "https://kydaidy.com"

# Аккаунты без лимита (могут генерить профиль сколько угодно).
# По username (без @, lowercase) И по tg_id — username ненадёжен (может быть скрыт/изменён).
SHADOW_UNLIMITED = {"autocreater", "kyda_idy", "al_lazovsky"}
SHADOW_UNLIMITED_IDS = {6271776494}  # admin (Кай); id Алёны/2-го добавим через /whoami


def _is_unlimited(user) -> bool:
    uname = (user.username or "").lower().lstrip("@")
    return uname in SHADOW_UNLIMITED or user.id in SHADOW_UNLIMITED_IDS
from database import (
    upsert_user, get_user, start_nurture, stop_nurture, get_user_purchases,
    set_tribute_post, get_tribute_post,
    has_generated_shadow, mark_shadow_generated, save_shadow_dist,
    set_user_source, source_stats, log_event, event_counts,
)
from content_data import (
    POVOROT_RESULTS,
    POVOROT_NAMES,
    POVOROT_TAGLINES,
    PDF_FILES,
    AUDIO_FILES,
    WELCOME_NO_POVOROT,
    PRODUCTS_MENU,
    PRODUCT_FALLBACKS,
    PRODUCT_TRIBUTE_POSTS_DEFAULT,
    CLUB_DESCRIPTION,
)

VALID_PRODUCT_CODES = ("manifest_club", "manifest_1on1")

# Tribute purchase URLs — used as inline button on copied product cards.
# copyMessage doesn't preserve the original inline keyboard, so we add it back.
PRODUCT_BUY_URLS = {
    "manifest_club": ("Подписаться", "https://t.me/tribute/app?startapp=sULY"),
    "manifest_1on1": ("Оформить подписку", "https://t.me/tribute/app?startapp=sZXq"),
}

# ── Атрибуция источника трафика (deep-link /start <tag>) ─────────────────────
# Канонический формат ссылки контента: t.me/kydaidy_bot?start=<tag>
# (напр. ?start=threads) — бэр-токен. Можно и суффиксом к другому deep-link
# через «__»: ?start=s_ABCDE__pin (источник + сразу тест Тени из пина).
# Telegram разрешает в start-параметре только [A-Za-z0-9_-], поэтому «__».
SOURCE_TAGS = {
    "threads", "pin", "pinterest", "dzen", "zen", "video", "reels", "shorts",
    "tg", "telegram", "ig", "inst", "instagram", "yt", "youtube", "vk", "site",
    "bio", "rutube", "tiktok", "tt",
}
# Нормализация синонимов к одному имени канала.
_SOURCE_ALIAS = {
    "pin": "pinterest", "zen": "dzen", "inst": "instagram", "ig": "instagram",
    "yt": "youtube", "tg": "telegram", "reels": "video", "shorts": "video",
    "tt": "tiktok",
}


# Функциональные deep-link префиксы — их НИКОГДА не считаем источником.
_FUNC_PREFIXES = ("s_", "shadow_", "povorot", "book1on1")

# Человекочитаемые имена продуктов для /cabinet (product_code содержит «_»,
# который ломает Markdown-разметку — показываем чистое имя).
_PRODUCT_TITLES = {
    "manifest_club": "Клуб «Манифест»",
    "manifest_1on1": "«Манифест 1:1»",
    "manifest_7": "«Манифест 7»",
}


def _split_source(args: str) -> tuple[str, str | None]:
    """(core_args, source). Отрезает источник: суффикс «__tag» или весь бэр-токен.

    Ловим переходы со ВСЕХ ресурсов: известный канал (SOURCE_TAGS, с нормализацией
    синонимов) ИЛИ произвольная метка новой площадки (?start=facebook, ?start=blog_jan)
    — чтобы новый канал трекался без правки кода. Не ломает функциональные deep-link
    (s_/shadow_/povorot): если метки нет — возвращает args как есть и source=None."""
    if not args:
        return args, None

    def _norm_any(tok: str) -> str | None:
        """Нормализованное имя источника или None (если это не похоже на метку)."""
        t = tok.strip().lower()
        if t.startswith("src_") or t.startswith("src-"):
            t = t[4:]
        if t in SOURCE_TAGS:                       # известный канал → синоним → канон
            return _SOURCE_ALIAS.get(t, t)
        # произвольная метка новой площадки: буквы/цифры/_/-, до 32 симв.,
        # но не функциональный deep-link (его обрабатывают ниже как s_/shadow_/povorot).
        if t.startswith(_FUNC_PREFIXES):
            return None
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", t):
            return _SOURCE_ALIAS.get(t, t)
        return None

    if "__" in args:
        core, _, tail = args.rpartition("__")
        tag = _norm_any(tail)
        if tag:
            return core, tag
        # Хвост после __ не распознан как источник (кривой/длиннее 32) — НО core может
        # быть валидным функц. deep-link (s_/shadow_/povorot). Не теряем сам линк:
        # отдаём core, метку роняем. Иначе длинная метка убивала вход в тест Тени.
        if core.startswith(_FUNC_PREFIXES):
            return core, None
    bare = _norm_any(args)
    if bare:
        return "", bare
    return args, None


logger = logging.getLogger(__name__)
router = Router()


# tg_id -> строка-распределение (10 символов), по которой ждём фото для профиля Тени.
# In-memory: генерация идёт сразу после фото в той же сессии; переживать рестарт
# Render free не требуется (если потеряется — бот мягко попросит пройти тест заново).
_pending_shadow: dict[int, str] = {}

# tg_id, для которых портрет Тени сейчас генерится (дорогой вызов Gemini ~60с).
# Гейт лимита проставляется ПОСЛЕ генерации → без этого лока два селфи подряд
# проходят гейт и запускают 2× генерацию (утечка квоты/денег). Держим, пока рисуем.
_generating_shadow: set[int] = set()

# --- рост ТГ-канала: подписка ---
_CHANNEL = settings.tg_channel_id                       # "@kydaidy" (для getChatMember)
_CHANNEL_URL = "https://t.me/" + _CHANNEL.lstrip("@")


async def _is_subscribed(bot, tg_id: int) -> bool | None:
    """Подписан ли юзер на публичный канал.
    True — подписан, False — точно нет, None — ПРОВЕРИТЬ НЕЛЬЗЯ (бот не админ канала:
    get_chat_member даёт «member list is inaccessible»). None разводим оптимистично,
    чтобы не блокировать рост, пока боту не выдали права админа в @kydaidy."""
    try:
        m = await bot.get_chat_member(_CHANNEL, tg_id)
        return getattr(m, "status", "") in ("member", "administrator", "creator")
    except Exception:
        logger.warning("get_chat_member(%s) недоступен — бот не админ канала?", _CHANNEL, exc_info=True)
        return None


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(cb: CallbackQuery):
    sub = await _is_subscribed(cb.bot, cb.from_user.id)
    if sub is not False:  # True (подписан) ИЛИ None (не смогли проверить) → засчитываем
        await cb.answer("Готово 🤍")
        await cb.message.answer(
            "🤍 Вижу тебя в канале — спасибо, что рядом.\n\n"
            "А если хочешь быть рядом каждую неделю — живые эфиры, закрытый чат, я без "
            "лимита в переписке — это Клуб «Манифест», 990 ₽/мес.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✦ Клуб «Манифест» — 990 ₽/мес",
                                      callback_data="buy:manifest_club")],
            ]))
    else:
        await cb.answer("Пока не вижу подписки — подпишись и жми ещё раз 🙏", show_alert=True)


def _subscribe_kbd() -> InlineKeyboardMarkup:
    """Мягкий нудж подписки на канал: дверь в канал + «я уже там» (переиспользует
    cb_check_sub, который трактует None/True как «засчитано»)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🕯 Заглянуть в канал", url=_CHANNEL_URL),
        InlineKeyboardButton(text="✅ Я уже там", callback_data="check_sub"),
    ]])


async def _nudge_subscribe_photo(message: Message):
    """Мягкий нудж подписки после фото, когда встреча НЕ открылась (живой сессии
    нет — прерывать нечего). Крэш-сейф; тест-аккаунты не шумим."""
    try:
        if _is_unlimited(message.from_user):
            return
        await message.answer(
            "Пока рисовала тебя — подумала: если захочешь быть рядом между "
            "встречами, я почти каждый день пишу в канале то, что не влезает в "
            "переписку — без причёсанного тона, как есть. Не обязательно. Просто "
            "дверь открыта 🕯",
            parse_mode=None, reply_markup=_subscribe_kbd())
    except Exception:
        logger.warning("_nudge_subscribe_photo failed (continuing)", exc_info=True)


def _nurture_optin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✦ Хочу получать", callback_data="nurture_yes")],
            [InlineKeyboardButton(text="Сейчас не нужно", callback_data="nurture_no")],
        ]
    )


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    # Тест-первым (мандат Кая 05.07): «Узнать свою Тень» = входной лид-магнит,
    # первой кнопкой; синхронно с текстом WELCOME_NO_POVOROT (он уже тест-первый).
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌑 Узнать свою Тень", callback_data="quiz")],
            [InlineKeyboardButton(text="💬 Алёна на связи", callback_data="alena")],
            [InlineKeyboardButton(text="🛍️ Что доступно", callback_data="products")],
            [InlineKeyboardButton(text="👤 Мой кабинет", callback_data="cabinet")],
        ]
    )


# ── Навигация: «← Назад» / «🏠 Меню» (единый стиль на всех экранах) ───────────
# callback "menu" ловит handlers.router (подключён последним) → срабатывает из
# любого экрана/роутера (alena, guide). "← Назад" ведёт на родительский callback.
def _home_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="🏠 Меню", callback_data="menu")


def _nav_row(back: str | None = None) -> list[InlineKeyboardButton]:
    """Ряд навигации: опц. «← Назад» (на родительский callback) + «🏠 Меню»."""
    row = []
    if back:
        row.append(InlineKeyboardButton(text="← Назад", callback_data=back))
    row.append(_home_btn())
    return row


def _menu_only_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_home_btn()]])


@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery):
    """Возврат в главное меню из любого экрана."""
    # I10: выход в меню = выход из активной встречи → закрываем сессию (синхрон
    # состояния, иначе следующий текст трактуется как реплика в разговоре).
    try:
        from database import ai_close_all_active
        await ai_close_all_active(callback.from_user.id)
    except Exception:
        logger.warning("cb_menu: ai_close_all_active failed", exc_info=True)
    await callback.message.answer(
        "Главное меню. Куда идём?", reply_markup=_main_menu_keyboard())
    await callback.answer()


@router.message(CommandStart(deep_link=True))
async def cmd_start_with_deeplink(message: Message, command: CommandObject):
    """Старт с deeplink: /start povorot3 → карта; /start shadow_W → тест архетипов.

    Дополнительно ловим метку источника трафика: бэр-токен (?start=threads)
    или суффикс ?start=s_ABCDE__pin. First-touch — пишем только первый источник."""
    args = command.args or ""
    user = message.from_user

    # Запись на 1:1 из канала «Манифест · 1:1» (кнопка ?start=book1on1).
    # ВАЖНО: обрабатываем ДО _split_source — иначе атрибуция нормализует
    # «book1on1» как метку источника (args становится "") и ветка ниже не
    # срабатывает → кнопка записи роняла бы юзера на общее приветствие.
    if args == "book1on1":
        await upsert_user(user.id, user.username, user.first_name)
        from booking import start_booking
        await start_booking(message)
        return

    args, source = _split_source(args)

    # Тест тёмных архетипов с полным распределением: /start s_<10 символов>.
    if args.startswith("s_"):
        if decode_distribution(args[2:]):
            await upsert_user(user.id, user.username, user.first_name)
            await set_user_source(user.id, source)
            await _prompt_shadow_photo(message, args[2:])
            return

    # Совместимость: /start shadow_<code> → распределение «весь вес на этот код».
    if args.startswith("shadow_"):
        code = args[len("shadow_"):].upper()
        if code in ARCHETYPES:
            await upsert_user(user.id, user.username, user.first_name)
            await set_user_source(user.id, source)
            await _prompt_shadow_photo(message, encode_distribution({code: 10}))
            return

    povorot = None
    if args.startswith("povorot"):
        try:
            povorot = int(args.replace("povorot", ""))
            if povorot not in (1, 2, 3, 4, 5):
                povorot = None
        except ValueError:
            povorot = None

    await upsert_user(user.id, user.username, user.first_name, povorot)
    await set_user_source(user.id, source)

    if povorot:
        await _send_povorot_result(message, povorot)
    else:
        await message.answer(WELCOME_NO_POVOROT, reply_markup=_main_menu_keyboard())


async def _prompt_shadow_photo(message: Message, dist: str):
    """После теста архетипов — показать ведущий архетип и попросить фото."""
    code = winner_from_counts(decode_distribution(dist))
    a = ARCHETYPES[code]
    _pending_shadow[message.from_user.id] = dist
    # Персистим распределение в БД СРАЗУ: Render free усыпляет/редеплоит контейнер,
    # in-memory _pending_shadow теряется → фото пришло бы «в пустоту» и юзера
    # футболило обратно на сайт. shadow_dist в БД = фолбэк в on_photo.
    try:
        await save_shadow_dist(message.from_user.id, dist)
    except Exception:
        logger.warning("persist shadow_dist failed (continuing)", exc_info=True)
    # Первый контакт с Алёной: поздороваться + карта пути + формат (мандат Кая
    # 03.07: «в сообщении с Тенью, где просим фото — поздороваться и объяснить
    # правила: что будет происходить, что это сессия, что делать и в каком формате»).
    # Правило №1 Кая (03.07): биографией владеет ЭТО сообщение; «бесплатная/одна/
    # формат ответов» — зона голосового опенера (alena_chat), тут их НЕТ.
    await message.answer(
        "Привет. Это я, Алёна. Рада, что ты дошла до меня 🌑\n\n"
        "Пара слов, чтобы ты знала, с кем говоришь: формул счастья я не раздаю "
        "и по голове гладить не буду. Я сама когда-то решила, что близость и "
        "семья — не для меня. Думала, это моя сила. Оказалось — броня, за "
        "которой я пряталась. Свою я сняла — и теперь рядом с теми, кто "
        "снимает свою.\n\n"
        f"Твоя ведущая Тень — *{a['name']}* _({a['too']})_. {a['tag']}\n\n"
        "Как всё будет: пришли мне *одно своё фото*, где хорошо видно лицо, — "
        "я его не храню, оно нужно только для кисти. Нарисую тебя акварелью в "
        "образе этой Тени и соберу твой профиль. Потом покажу кое-что личное "
        "про неё. А дальше — поговорим: бесплатно, ты и я.",
        parse_mode="Markdown",
    )


# Видео-кружки под каждую Тень (file_id готового кружка в Telegram). Пусто = не шлём.
# v8-рецепт (Photo Avatar кабинет + Avatar IV + Motion + голос multilingual_v2). 01.07.
# Обновлено 01.07: НОВЫЕ Digital Twin (Avatar V, 2 твина, чередование ракурсов),
# тексты выверены, Кай одобрил. Твины A `2ab45471…` (W·H·F·R·D) / B `ba9b5ad5…` (Q·M·MR·O·C).
_KRUZHOK_FILE_IDS: dict[str, str] = {
    "W": "DQACAgUAAxkDAAIB5mpE9NVsEXSWhFowZiivtWq8z4iPAAK2IgACO4spVouE6Sn7sFoRPAQ",
    "C": "DQACAgUAAxkDAAIB52pE9OHWg50qgUg3j65apkEjn2oAA7ciAAI7iylWU8ijvXLP9OM8BA",
    "H": "DQACAgUAAxkDAAIB6GpE9OnLzzB4LLxXV1tK0EUIumyTAAK4IgACO4spVogQilfJMhy-PAQ",
    "F": "DQACAgUAAxkDAAIB6WpE9PNvG1uo9jREw-T8weqQVbCHAAK5IgACO4spVkoDRu5PgGWNPAQ",
    "R": "DQACAgUAAxkDAAIB6mpE9P-xlddKgUM9Y3_251-u5P-NAAK6IgACO4spVu1CSHRs-u3LPAQ",
    # D перегенерён 03.07: TTS-дефолты + «разруши́тельница» (U+0301) + motionPrompt
    # моргания (avatar_v). Аудио-эталон: scratchpad/kruzhki/arch/D_regen_v2.mp3.
    "D": "DQACAgUAAxkDAAIC1mpHhE8PjoQDnyMndlOJ8k-cwN7ZAALUHwACspI4Vg0tLkA39bCvPAQ",
    "Q": "DQACAgUAAxkDAAIB7GpE9VjpcdqeF6wuzrJ_9cajVq-CAAK8IgACO4spVk92J0ehk4XgPAQ",
    "M": "DQACAgUAAxkDAAIB7WpE9Wej88AE_epZojKjR9W5izUpAAK9IgACO4spVuRvGsb_IrczPAQ",
    "MR": "DQACAgUAAxkDAAIB7mpE9XYN4kdrKH21sLED6N9hZXrfAAK_IgACO4spVlHC4FrXzg2OPAQ",
    "O": "DQACAgUAAxkDAAIB72pE9YB0b8aJbbP1Ig9I5ErjrMzlAALAIgACO4spVqPVU3D209AwPAQ",
}


async def _send_shadow_kruzhok(message: Message, code: str) -> bool:
    """Видео-кружок Алёны про её Тень (если готов). True — отправлен, False — нет."""
    fid = _KRUZHOK_FILE_IDS.get(code)
    if not fid:
        return False
    # Квота 2 кружков на клиента (мандат Кая 03.07): исчерпана → путь без видео
    # (опенер сам исполнит «личное про Тень» тизером).
    from alena_voice import video_quota_ok
    if not await video_quota_ok(message.chat.id):
        return False
    try:
        await message.bot.send_chat_action(message.chat.id, "record_video_note")
        await message.answer_video_note(fid)
        await log_event(message.from_user.id, "kruzhok_sent", code)
        return True
    except Exception:
        logger.warning("shadow kruzhok send failed for %s", code, exc_info=True)
        return False


async def _download_selfie(message: Message) -> bytes | None:
    """Байты селфи: сжатое фото ИЛИ документ-картинка (десктоп «отправить без сжатия»).
    Раньше ловили только F.photo → селфи-файлом молча падало в fallback (потеря лида)."""
    src = None
    if message.photo:
        src = message.photo[-1]
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        src = message.document
    if src is None:
        return None
    try:
        buf = io.BytesIO()
        await message.bot.download(src, destination=buf)
        return buf.getvalue()
    except Exception:
        logger.warning("selfie download failed for %s", message.from_user.id, exc_info=True)
        return None


@router.message(F.photo)
async def on_photo(message: Message):
    """Фото после теста → clean-портрет Тени + разбор + ссылка на полный профиль."""
    tg_id = message.from_user.id
    dist = _pending_shadow.get(tg_id)
    if not dist:
        # Фолбэк на БД: контейнер Render мог уснуть/редеплоиться после теста →
        # память пуста, но распределение персистнуто в _prompt_shadow_photo.
        try:
            u = await get_user(tg_id)
            dist = (u or {}).get("shadow_dist")
        except Exception:
            logger.warning("shadow_dist DB fallback failed", exc_info=True)
    if not dist:
        await message.answer(
            "Спасибо за фото 🙏 Сначала пройди тест «Какая Тень в тебе активна» — "
            "и пришли фото следом: https://kydaidy.com/shadow"
        )
        return

    if not _is_unlimited(message.from_user) and await has_generated_shadow(tg_id):
        await message.answer(
            "Бесплатный профиль Тени — один раз на человека, и ты его уже получала 🌑\n\n"
            "Если хочешь пойти глубже по своей Тени — это уже «Манифест»: путь сквозь неё к себе.",
            reply_markup=_products_menu_keyboard(),
        )
        _pending_shadow.pop(tg_id, None)
        return

    if not settings.gemini_key:
        logger.error("gemini_key not configured — cannot generate shadow portrait")
        await message.answer("Сейчас рисунок недоступен. Напиши @kydaidy — поможем вручную.")
        return

    # #6: битый/старый dist из БД → decode None → без этой проверки winner_from_counts(None)
    # роняет хендлер и юзер получает тишину. Мягко зовём перепройти тест.
    counts = decode_distribution(dist)
    if not counts:
        await message.answer(
            "Кажется, результат теста потерялся 🙏 Пройди его ещё раз и пришли фото следом: "
            "https://kydaidy.com/shadow")
        _pending_shadow.pop(tg_id, None)
        return
    code = winner_from_counts(counts)

    # #2: лок против гонки двух селфи (гейт лимита ставится только после ~60с генерации).
    if tg_id in _generating_shadow:
        await message.answer("Уже рисую твою Тень — секунду, не присылай ещё раз 🕯️")
        return
    _generating_shadow.add(tg_id)   # лок сразу после check (без await между) → гонка закрыта

    a = ARCHETYPES[code]
    status = None
    try:
        # статус-сообщение ВНУТРИ try: если его отправка упадёт (сеть/флуд), finally всё
        # равно снимет лок (раньше add стоял до try → сбой answer навсегда залипал юзера).
        status = await message.answer(
            f"Рисую твою Тень — *{a['name']}*… Это займёт около минуты. Не закрывай чат 🕯️",
            parse_mode="Markdown",
        )
        photo_bytes = await _download_selfie(message)
        if not photo_bytes:
            raise RuntimeError("no selfie bytes (photo/document download failed)")

        portrait = await generate_hero_image(
            photo_bytes, code, clean=True, api_key=settings.gemini_key)
        # рендер профиля — CPU-работа PIL, уводим из event loop
        profile_png = await asyncio.to_thread(
            render_profile, portrait, dist, message.from_user.first_name)
    except Exception as e:
        logger.exception(f"shadow generation failed for {tg_id} dist={dist}: {e}")
        await log_event(tg_id, "portrait_fail")
        # УСТОЙЧИВОСТЬ (01.07): не рвём воронку при сбое картинки (напр. 429-квота Gemini).
        # Выдаём результат Тени ТЕКСТОМ (архетип статичен, без API) + призыв подписаться + продукты.
        try:
            await status.delete()
        except Exception:
            pass
        await message.answer(
            f"🌑 Твоя ведущая Тень — *{a['name']}* _({a['too']})_.\n\n"
            f"{a.get('tag','')}\n\n{a.get('essence','')}\n\n"
            "_(Портрет сейчас не нарисовался — перегружены мощности. "
            "Пришли фото ещё раз чуть позже, и я нарисую твою Тень акварелью.)_",
            parse_mode="Markdown",
        )
        kruzhok_shown = await _send_shadow_kruzhok(message, code)
        from alena_chat import open_shadow_session
        if not await open_shadow_session(message, message.from_user, code, video_hook=kruzhok_shown):
            await message.answer(
                "Твой архетип — это *Тень*: где ты защищаешься сейчас.\n\n"
                "Хочешь поговорить про неё начистоту — бесплатная встреча: /alena\n\n"
                "А вот с чего ещё можно начать ↓",
                parse_mode="Markdown",
                reply_markup=_products_menu_keyboard(),
            )
            await _nudge_subscribe_photo(message)  # встречи нет → нудж безопасен
        return
    finally:
        _generating_shadow.discard(tg_id)   # #2: снять лок генерации (успех/ошибка)
        try:
            await status.delete()
        except Exception:
            pass

    await log_event(tg_id, "portrait_ok", code)
    # Портрет БЕЗ кнопок (фидбек Кая 02.07: HD-кнопка уводила из потока перед
    # сессией — ничего кликабельного между анкетой и встречей).
    await message.answer_photo(
        BufferedInputFile(profile_png, filename="arhetip-profil.png"),
        caption=f"🌑 Твой архетипический профиль · ведущая Тень: {a['name']}",
    )
    # Видео-кружок про её Тень (вовлекающий момент перед хуком, если готов для архетипа).
    kruzhok_shown = await _send_shadow_kruzhok(message, code)
    # Тёплый авто-контакт: Алёна САМА открывает встречу с хуком под его Тень и
    # докручивает в Клуб. Если кружок уже показал Тень — текстовый хук сокращаем.
    # #5: портрет уже доставлен — сбой открытия встречи (напр. запись сессии в D1)
    # НЕ должен ронять хендлер и оставлять юзера без опенера/лимита. Ловим → фолбэк-меню.
    from alena_chat import open_shadow_session
    try:
        opened = await open_shadow_session(message, message.from_user, code, video_hook=kruzhok_shown)
    except Exception:
        logger.warning("open_shadow_session failed for %s (fallback menu)", tg_id, exc_info=True)
        opened = False
    if not opened:
        await message.answer(
            "Твой архетип — это *Тень*: где ты защищаешься сейчас, в какой маске застряла.\n\n"
            "Путь сквозь неё — *карта 5 поворотов*. Хочешь поговорить про свою Тень "
            "начистоту — у меня есть бесплатная встреча: /alena\n\n"
            "А вот с чего ещё можно начать ↓",
            parse_mode="Markdown",
            reply_markup=_products_menu_keyboard(),
        )
        await _nudge_subscribe_photo(message)  # opened=False → встречи нет, нудж безопасен
    if not _is_unlimited(message.from_user):
        await mark_shadow_generated(tg_id)
    # Сохраняем распределение Тени — AI-проводник практик адаптируется под него.
    try:
        await save_shadow_dist(tg_id, dist)
    except Exception:
        logger.warning(f"save_shadow_dist failed for {tg_id}", exc_info=True)
    _pending_shadow.pop(tg_id, None)


@router.message(F.document)
async def on_document(message: Message):
    """#1: селфи, присланное ФАЙЛОМ (десктоп «отправить без сжатия») — частый кейс.
    Картинка-документ → тот же путь портрета Тени; не картинка → обычный fallback."""
    mime = (message.document.mime_type or "") if message.document else ""
    if mime.startswith("image/"):
        await on_photo(message)
    else:
        await fallback(message)


@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    await upsert_user(user.id, user.username, user.first_name)

    # I10: /start = выход из активной AI-встречи. Закрываем сессию, иначе
    # состояние рассинхронится (человек «вышел» в меню, а система думает, что
    # разговор идёт, и следующий текст уходит в мозг). Крэш-сейф.
    try:
        from database import ai_close_all_active
        await ai_close_all_active(user.id)
    except Exception:
        logger.warning("cmd_start: ai_close_all_active failed", exc_info=True)

    existing = await get_user(user.id)
    if existing and existing["povorot"]:
        await message.answer(
            f"С возвращением, {user.first_name or 'друг'}.\n\n"
            f"Ты на Повороте {existing['povorot']}: {POVOROT_NAMES[existing['povorot']]}.",
            reply_markup=_main_menu_keyboard(),
        )
    else:
        await message.answer(WELCOME_NO_POVOROT, reply_markup=_main_menu_keyboard())


async def _send_povorot_result(message: Message, povorot: int):
    """Выдача карты + аудио + предложение nurture после квиза."""
    name = POVOROT_NAMES[povorot]
    tagline = POVOROT_TAGLINES[povorot]

    # 1. Заголовок
    await message.answer(
        f"🎁 Привет. Это я, Алёна. Держи — отправляю тебе твою карту.\n\n"
        f"Ты на Повороте *{povorot}*: *{name}* — _«{tagline}»_",
        parse_mode="Markdown",
    )

    # 2. PDF-карта (если файл существует)
    pdf_path = Path(__file__).parent / PDF_FILES[povorot]
    if pdf_path.exists():
        await message.answer_document(
            FSInputFile(pdf_path),
            caption="Твоя карта перепутья",
        )
    else:
        logger.warning(f"PDF файл не найден: {pdf_path}")

    # 3. Текст результата
    await message.answer(POVOROT_RESULTS[povorot], parse_mode="Markdown")

    # 4. Аудио-приветствие (если файл существует)
    audio_path = Path(__file__).parent / AUDIO_FILES[povorot]
    if audio_path.exists():
        await message.answer_audio(
            FSInputFile(audio_path),
            title=f"Карта перепутья — Поворот {povorot}",
            performer="Алёна Kyda Idy",
            caption="Минута от меня — про твой поворот.",
        )
    else:
        logger.warning(f"Аудио не найдено: {audio_path}")

    # 5. Предложение nurture
    await message.answer(
        "Это начало.\n\n"
        "Если хочешь — буду присылать тебе по одному короткому посланию каждый день, 7 дней. "
        "Без давления. Можно отписаться в любой момент.",
        reply_markup=_nurture_optin_keyboard(),
    )


@router.callback_query(F.data == "nurture_yes")
async def cb_nurture_yes(callback: CallbackQuery):
    await start_nurture(callback.from_user.id)
    await callback.message.edit_text(
        "Хорошо. Завтра в 8:00 утра — первое сообщение.\n\nДо завтра.\n\n— Алёна"
    )
    await callback.answer()


@router.callback_query(F.data == "nurture_no")
async def cb_nurture_no(callback: CallbackQuery):
    await callback.message.edit_text(
        "Поняла. Карта твоя — она с тобой.\n\n"
        "Если когда-то надумаешь — я тут: @kydaidy.\n\n"
        "— Алёна"
    )
    await callback.answer()


def _products_menu_keyboard() -> InlineKeyboardMarkup:
    # Клуб — герой (первым). Бесплатное «поговорить» уведено ВНИЗ (Hermes #2:
    # бесплатное не должно стоять над платным).
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✦ Клуб «Манифест» — 990 ₽/мес", callback_data="buy:manifest_club")],
            [InlineKeyboardButton(text="✦ «Манифест 1:1» — от 7 000 ₽", callback_data="buy:manifest_1on1")],
            [InlineKeyboardButton(text="💬 Сначала поговорить со мной", callback_data="alena")],
            _nav_row(),
        ]
    )


@router.callback_query(F.data == "products")
@router.message(Command("products"))
async def show_products(event):
    target = event.message if isinstance(event, CallbackQuery) else event
    await target.answer(
        "*Что доступно сейчас.*\n\nКуда идти — решаешь ты.\n\n— Алёна",
        parse_mode="Markdown",
        reply_markup=_products_menu_keyboard(),
    )
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.callback_query(F.data.startswith("buy:"))
async def show_one_product(callback: CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if code not in VALID_PRODUCT_CODES:
        await callback.answer("Неизвестный продукт", show_alert=True)
        return
    user_id = callback.from_user.id
    bot = callback.bot

    # Try DB first, then hardcoded defaults (Render free wipes SQLite on deploy)
    post = await get_tribute_post(code)
    src_chat = src_msg = None
    if post:
        src_chat, src_msg = post["src_chat_id"], post["src_message_id"]
    elif code in PRODUCT_TRIBUTE_POSTS_DEFAULT:
        src_chat, src_msg = PRODUCT_TRIBUTE_POSTS_DEFAULT[code]

    if src_chat and src_msg:
        # Add inline button manually — copyMessage doesn't preserve original keyboard
        label, url = PRODUCT_BUY_URLS[code]
        kbd = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label, url=url)],
            _nav_row(back="products"),
        ])
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=src_chat,
                message_id=src_msg,
                reply_markup=kbd,
            )
            await callback.answer()
            return
        except Exception as e:
            logger.warning(f"copy_message failed for {code} (chat={src_chat} msg={src_msg}): {e}")

    # Fallback: текст со ссылкой-превью если пост ещё не захвачен / не доступен для копирования
    await bot.send_message(user_id, PRODUCT_FALLBACKS[code], parse_mode="Markdown",
                           reply_markup=InlineKeyboardMarkup(
                               inline_keyboard=[_nav_row(back="products")]))
    await callback.answer()


@router.message(Command("capture"))
async def cmd_capture(message: Message, command: CommandObject):
    """Захватывает Tribute-пост: reply'ом на пост от Tribute + /capture <code>.

    Пример: ответить на сообщение от @tribute с текстом
        /capture manifest_7
    Бот сохранит chat_id+message_id того сообщения в БД tribute_posts.

    Доступна любому в private chat — Tribute-посты приходят от разных
    отправителей (mini-app share не сохраняет from_user админа), поэтому
    мы не фильтруем по from_user.id. Без правильного reply команда
    безвредна.
    """
    if message.chat.type != "private":
        return  # не в личке — игнор

    code = (command.args or "").strip()
    if code not in VALID_PRODUCT_CODES:
        await message.reply(
            f"Использование: reply на пост от @tribute + /capture <code>\n"
            f"Допустимые коды: {', '.join(VALID_PRODUCT_CODES)}"
        )
        return

    target = message.reply_to_message
    if not target:
        await message.reply("Команда работает только как reply на пост от @tribute.")
        return

    src_chat_id = target.chat.id
    src_message_id = target.message_id
    await set_tribute_post(code, src_chat_id, src_message_id)
    await message.reply(
        f"✅ Захвачено: {code}\n"
        f"chat_id={src_chat_id}, message_id={src_message_id}\n\n"
        f"Теперь /products будет копировать это сообщение пользователям.",
        parse_mode=None,
    )




@router.callback_query(F.data.startswith("cap:"))
async def capture_callback(callback: CallbackQuery):
    # Разрешено любому в private chat. Захват требует осмысленного callback_data,
    # который генерируется только из реальных Tribute-постов в auto_capture_tribute.
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Битый callback", show_alert=True)
        return
    _, code, msg_id_str = parts
    if code not in VALID_PRODUCT_CODES:
        await callback.answer("Неизвестный продукт", show_alert=True)
        return
    src_message_id = int(msg_id_str)
    src_chat_id = callback.message.chat.id  # чат админа с ботом

    await set_tribute_post(code, src_chat_id, src_message_id)
    await callback.message.edit_text(
        f"✅ Захвачено: {code}\nchat_id={src_chat_id}, message_id={src_message_id}\n\n"
        f"Теперь /products будет копировать это сообщение пользователям.",
        parse_mode=None,
    )
    await callback.answer("Сохранено")


@router.callback_query(F.data == "quiz")
@router.message(Command("quiz"))
async def show_quiz(event):
    target = event.message if isinstance(event, CallbackQuery) else event
    await target.answer(
        "«Какая Тень в тебе активна» — тест из 10 тёмных женских архетипов. "
        "10 вопросов, 5 минут.\n\n"
        "Пройди здесь: https://kydaidy.com/shadow\n\n"
        "В конце пришли мне сюда своё фото — соберу твой архетипический профиль. Бесплатно.",
        reply_markup=_menu_only_kbd(),
    )
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.callback_query(F.data == "cabinet")
@router.message(Command("cabinet"))
async def show_cabinet(event):
    target_user = event.from_user
    target = event.message if isinstance(event, CallbackQuery) else event

    user = await get_user(target_user.id)
    purchases = await get_user_purchases(target_user.id)

    if not user:
        await target.answer("Кабинет пока пуст. Начни с /quiz",
                            reply_markup=_menu_only_kbd())
        if isinstance(event, CallbackQuery):
            await event.answer()
        return

    text = f"*Твой кабинет*\n\n"
    if user["povorot"]:
        text += f"📍 Поворот: {user['povorot']} — {POVOROT_NAMES[user['povorot']]}\n"
    if purchases:
        text += "\n*Покупки:*\n"
        for p in purchases:
            # product_code (напр. manifest_club) содержит «_» → непарный Markdown-италик
            # ронял весь /cabinet (Telegram 400) у купивших. Убираем «_» из динамики +
            # человекочитаемое имя.
            name = _PRODUCT_TITLES.get(
                p["product_code"], str(p["product_code"]).replace("_", " "))
            text += f"  • {name} — {p['amount']} ₽\n"
    else:
        text += "\nПокупок пока нет.\n\nЕсли хочешь идти глубже: /products"

    try:
        await target.answer(text, parse_mode="Markdown", reply_markup=_menu_only_kbd())
    except Exception:
        # Предохранитель: любая Markdown-сущность в динамике не должна ронять кабинет —
        # шлём тем же текстом без разметки.
        logger.warning("cabinet Markdown parse failed — fallback to plain", exc_info=True)
        await target.answer(text.replace("*", ""), parse_mode=None,
                            reply_markup=_menu_only_kbd())
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.message(Command("club"))
async def show_club(message: Message):
    await message.answer(CLUB_DESCRIPTION, parse_mode="Markdown")


@router.message(Command("dossier"))
async def show_dossier(message: Message):
    """Админ/Алёна: живой портрет участницы для подготовки к 1:1. /dossier <tg_id>."""
    if not _is_unlimited(message.from_user):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Формат: /dossier <tg_id>\n(id можно взять из /sources или пинга оплаты)")
        return
    tg_id = int(parts[1])
    u = await get_user(tg_id)
    if not u:
        await message.answer("Не нашла такого человека в базе.")
        return
    g = lambda k: (u or {}).get(k)
    lines = [f"🗂️ Досье · {g('first_name') or '—'} · @{g('username') or '—'} · id {tg_id}"]
    if g("source"):
        lines.append(f"Пришла: {g('source')}")
    if g("shadow_dist"):
        try:
            a = ARCHETYPES[winner_from_counts(decode_distribution(g("shadow_dist")))]
            lines.append(f"Тень: {a['name']} ({a['too']})")
        except Exception:
            pass
    if g("povorot"):
        lines.append(f"Поворот: {g('povorot')}")
    if g("last_ai_request"):
        lines.append(f"Настоящий запрос: {g('last_ai_request')}")
    # Рентген метод-петли (для тестов Кая): где сейчас встреча по мозгу v2.
    try:
        import json as _json
        cm = _json.loads(g("client_model") or "{}")
        phase_names = {
            "contact": "1/6 контакт", "surface_facade": "2/6 фасад",
            "catch_contradiction": "3/6 противоречие",
            "name_true_request": "4/6 истинный запрос",
            "give_shift": "5/6 сдвиг", "native_offer": "6/6 оффер (конец петли)",
        }
        if cm.get("method_phase"):
            lines.append(
                f"Фаза метода: {phase_names.get(cm['method_phase'], cm['method_phase'])}"
                f" · канал: {cm.get('medium') or 'text'}"
                f" · трек: {g('lead_track') or '—'}"
                f" (ж{g('lead_heat') if g('lead_heat') is not None else '·'}"
                f"/о{g('lead_open') if g('lead_open') is not None else '·'}"
                f"/с{g('lead_resist') if g('lead_resist') is not None else '·'}"
                f"/ц{g('lead_value') if g('lead_value') is not None else '·'})")
        if cm.get("true_request_hypothesis"):
            lines.append(f"Гипотеза запроса: {cm['true_request_hypothesis']}")
    except Exception:
        pass
    lines.append("\nПортрет (со встреч с AI-Алёной):\n" +
                 (g("dossier") or "— пока пусто (встречи ещё не было)"))
    await message.answer("\n".join(lines), parse_mode=None)


@router.message(Command("credits"))
async def cmd_credits(message: Message):
    """Админ: баланс HeyGen-кредитов по запросу — сколько живых кружков осталось."""
    if not _is_unlimited(message.from_user):
        return
    from heygen_credits import get_credits, circles_left, probe
    if not settings.heygen_api_key:
        await message.answer(
            "HeyGen-мониторинг спит: не задан HEYGEN_API_KEY в env.\n"
            "Добавь ключ в Render → пойдут авто-алерты о кредитах + /credits.",
            parse_mode=None)
        return
    c = await get_credits()
    head = ("HeyGen баланс сейчас недоступен (API молчит)." if c is None
            else f"💳 HeyGen: {c} кред ≈ {circles_left(c)} живых кружков.\n"
                 f"Голос Алёны — бесплатный, не тратит.\n"
                 f"Пороги алерта: {settings.credit_warn} / {settings.credit_urgent}.")
    # Диагностика (пока калибруем эндпоинт): показываем, что реально отдаёт API.
    diag = await probe()
    await message.answer(f"{head}\n\n— диагностика —\n{diag}", parse_mode=None)


@router.message(Command("stop"))
async def cmd_stop_nurture(message: Message):
    await stop_nurture(message.from_user.id)
    await message.answer("Поняла. Не буду писать ежедневно.\n\nКанал @kydaidy всегда открыт.")


@router.message(Command("whoami"))
async def cmd_whoami(message: Message):
    u = message.from_user
    unlimited = "да ✅" if _is_unlimited(u) else "нет (лимит 1)"
    await message.answer(
        f"id: `{u.id}`\nusername: @{u.username or '—'}\nбезлимит профиля: {unlimited}",
        parse_mode="Markdown",
    )


@router.message(Command("sources"))
async def cmd_sources(message: Message):
    """Админ: сводка по источникам трафика и конверсии в тест Тени."""
    if message.from_user.id != settings.tg_admin_id:
        return
    rows = await source_stats()
    if not rows:
        await message.answer(
            "Данных по источникам пока нет.\n\n"
            "Метить трафик: t.me/kydaidy_bot?start=<канал>\n"
            "Каналы: threads · pinterest · dzen · video · telegram · instagram · "
            "youtube · vk · site · bio\n"
            "Можно и к ссылке теста: ?start=s_<код>__pinterest",
            parse_mode=None)
        return
    def _i(r, k): return int(r.get(k) or 0)
    total = sum(_i(r, "users") for r in rows)
    t_test = sum(_i(r, "test_passed") for r in rows)
    t_port = sum(_i(r, "portrait") for r in rows)
    t_talk = sum(_i(r, "talked") for r in rows)
    t_req = sum(_i(r, "req") for r in rows)
    t_paid = sum(_i(r, "paid") for r in rows)
    lines = ["📊 Воронка по источникам (first-touch)\n"
             "пришли → тест → портрет → 💬разговор → 🔥запрос → 💰оплата\n"]
    for r in rows:
        u = _i(r, "users"); t = _i(r, "test_passed"); p = _i(r, "portrait")
        tk = _i(r, "talked"); rq = _i(r, "req"); pd = _i(r, "paid")
        lines.append(f"{r['source']}: {u}→{t}→{p}→💬{tk}→🔥{rq}→💰{pd}")

    def _pct(a, b): return f"{round(a / b * 100)}%" if b else "—"
    lines.append(
        f"\nИтого: {total} пришли · {t_test} тест · {t_port} портрет · "
        f"💬{t_talk} разговор · 🔥{t_req} запрос · 💰{t_paid} оплат")
    # где рвётся — переходы между стадиями
    lines.append(
        "\nПереходы: тест " + _pct(t_test, total) +
        " · портрет→разговор " + _pct(t_talk, t_port) +
        " · разговор→запрос " + _pct(t_req, t_talk) +
        " · запрос→💰 " + _pct(t_paid, t_req))
    # Волна 1 (H12): гранулярные события за 30 дней — видно работу присутствия
    # (voice_reply), офферов и дожимов, а не только агрегаты по людям.
    ev = await event_counts(30)
    if ev:
        order = ("portrait_ok", "portrait_fail", "kruzhok_sent", "session_open",
                 "voice_reply", "offer_shown", "stale_nudge",
                 "followup_1", "followup_2", "followup_3")
        parts = [f"{k} {ev[k][0]}({ev[k][1]}ч)" for k in order if k in ev]
        parts += [f"{k} {v[0]}({v[1]}ч)" for k, v in ev.items() if k not in order]
        lines.append("\nСобытия 30д (всего/людей):\n" + " · ".join(parts))
    await message.answer("\n".join(lines), parse_mode=None)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "*Команды бота*\n\n"
        "/start — главное меню\n"
        "/alena — личная встреча с Алёной (бесплатно)\n"
        "/quiz — узнать свою Тень (тест)\n"
        "/praktiki — практики «Манифеста 7» с проводником\n"
        "/products — что доступно\n"
        "/cabinet — мой кабинет\n"
        "/club — про Клуб «Манифест»\n"
        "/stop — отписаться от ежедневных сообщений\n"
        "/help — эта справка",
        parse_mode="Markdown",
    )


def _detect_product_code(caption: str | None) -> str | None:
    """Определяет product_code по тексту/caption поста от Tribute.
    Самое специфичное вперёд (1:1) → дальше клуб.
    """
    if not caption:
        return None
    text = caption.lower()
    if "1:1" in text or "1 на 1" in text or "личная сессия" in text or "audio-call" in text:
        return "manifest_1on1"
    if ("клуб" in text or "эфир" in text or "воркбук" in text or "безлимит" in text
            or "990" in text):
        return "manifest_club"
    return None


@router.message(F.via_bot.username == "tribute", F.chat.type == "private")
async def auto_capture_tribute(message: Message):
    """Tribute mini-app share присылает inline-сообщение с via_bot=@tribute.
    Пытаемся определить product_code по caption и сохранить автоматически.
    Если не получилось — показываем кнопки.
    """
    caption = message.caption or message.text or ""
    code = _detect_product_code(caption)
    src_chat_id = message.chat.id
    src_message_id = message.message_id

    if code:
        await set_tribute_post(code, src_chat_id, src_message_id)
        # parse_mode=None — caption Tribute может содержать спецсимволы, ломающие Markdown
        await message.reply(
            f"✅ Захвачено: {code}\n\n"
            f"Теперь /products будет копировать этот пост пользователям.",
            parse_mode=None,
        )
        logger.info(f"auto-captured {code} chat={src_chat_id} msg={src_message_id}")
        return

    # Не определилось — кнопки
    await message.reply(
        f"📥 Пост от @tribute получен. Какой это продукт?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✦ Клуб «Манифест»", callback_data=f"cap:manifest_club:{src_message_id}")],
            [InlineKeyboardButton(text="✦ Манифест 1:1", callback_data=f"cap:manifest_1on1:{src_message_id}")],
        ]),
    )


@router.message()
async def fallback(message: Message):
    """Последний рубеж (сюда попадает только то, что не поймали фильтры встречи /
    возражений). Никаких канцелярских отписок (мандат Кая 02.07) — тёплый мостик."""
    try:
        u = await get_user(message.from_user.id)
    except Exception:
        u = None
    req = (u or {}).get("last_ai_request")
    if req:
        # Она уже была на встрече — Алёна помнит и зовёт продолжить, без отписок.
        await message.answer(
            f"Я тебя слышу. Мы остановились на главном — «{req}».\n\n"
            "Захочешь продолжить разговор — просто набери /alena, я помню, где мы. "
            "А если готова, чтобы я была рядом регулярно — Клуб всегда открыт: /products.\n\n— Алёна",
            parse_mode=None)
        return
    await message.answer(
        "Я здесь. Поговорить со мной по-настоящему — /alena.\n"
        "Узнать свою Тень — /quiz. Живой человек — @kydaidy.\n\n— Алёна",
        parse_mode=None)
