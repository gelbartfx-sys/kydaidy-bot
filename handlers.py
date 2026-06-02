"""Обработчики команд бота."""

from __future__ import annotations

import io
import asyncio
import logging
from pathlib import Path

from aiogram import Router, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (
    Message, FSInputFile, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
)

from urllib.parse import quote

from config import settings
from shadow_test import (
    ARCHETYPES, decode_distribution, winner_from_counts, encode_distribution,
)
from ai_quiz import generate_hero_image, generate_analysis_text
from profile_image import render_profile
import portrait_store

# Публичные адреса для сборки ссылки на профиль.
BOT_PUBLIC_URL = "https://kydaidy-bot.onrender.com"
SITE_URL = "https://kydaidy.com"
from database import (
    upsert_user, get_user, start_nurture, stop_nurture, get_user_purchases,
    set_tribute_post, get_tribute_post,
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

VALID_PRODUCT_CODES = ("manifest_7", "manifest_club", "manifest_1on1")

# Tribute purchase URLs — used as inline button on copied product cards.
# copyMessage doesn't preserve the original inline keyboard, so we add it back.
PRODUCT_BUY_URLS = {
    "manifest_7":    ("Получить",    "https://web.tribute.tg/p/vKD"),
    "manifest_club": ("Подписаться", "https://t.me/tribute/app?startapp=sULY"),
    "manifest_1on1": ("Записаться",  "https://web.tribute.tg/p/vKG"),
}

logger = logging.getLogger(__name__)
router = Router()

# tg_id -> строка-распределение (10 символов), по которой ждём фото для профиля Тени.
# In-memory: генерация идёт сразу после фото в той же сессии; переживать рестарт
# Render free не требуется (если потеряется — бот мягко попросит пройти тест заново).
_pending_shadow: dict[int, str] = {}


def _nurture_optin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✦ Хочу получать", callback_data="nurture_yes")],
            [InlineKeyboardButton(text="Сейчас не нужно", callback_data="nurture_no")],
        ]
    )


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📍 Пройти карту", callback_data="quiz")],
            [InlineKeyboardButton(text="🛍️ Что доступно", callback_data="products")],
            [InlineKeyboardButton(text="👤 Мой кабинет", callback_data="cabinet")],
        ]
    )


@router.message(CommandStart(deep_link=True))
async def cmd_start_with_deeplink(message: Message, command: CommandObject):
    """Старт с deeplink: /start povorot3 → карта; /start shadow_W → тест архетипов."""
    args = command.args or ""
    user = message.from_user

    # Тест тёмных архетипов с полным распределением: /start s_<10 символов>.
    if args.startswith("s_"):
        if decode_distribution(args[2:]):
            await upsert_user(user.id, user.username, user.first_name)
            await _prompt_shadow_photo(message, args[2:])
            return

    # Совместимость: /start shadow_<code> → распределение «весь вес на этот код».
    if args.startswith("shadow_"):
        code = args[len("shadow_"):].upper()
        if code in ARCHETYPES:
            await upsert_user(user.id, user.username, user.first_name)
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

    if povorot:
        await _send_povorot_result(message, povorot)
    else:
        await message.answer(WELCOME_NO_POVOROT, reply_markup=_main_menu_keyboard())


async def _prompt_shadow_photo(message: Message, dist: str):
    """После теста архетипов — показать ведущий архетип и попросить фото."""
    code = winner_from_counts(decode_distribution(dist))
    a = ARCHETYPES[code]
    _pending_shadow[message.from_user.id] = dist
    await message.answer(
        f"🌑 Твоя ведущая Тень — *{a['name']}* _({a['too']})_.\n\n"
        f"{a['tag']}\n\n"
        "Хочешь свой полный *архетипический профиль* — с портретом в образе этой Тени?\n"
        "Пришли мне *одно своё фото* (лицо хорошо видно) — нарисую тебя акварелью и "
        "соберу персональный профиль.\n\n"
        "_Фото нужно только для рисунка, я его не храню._",
        parse_mode="Markdown",
    )


@router.message(F.photo)
async def on_photo(message: Message):
    """Фото после теста → clean-портрет Тени + разбор + ссылка на полный профиль."""
    tg_id = message.from_user.id
    dist = _pending_shadow.get(tg_id)
    if not dist:
        await message.answer(
            "Спасибо за фото 🙏 Сначала пройди тест «Какая Тень в тебе активна» — "
            "и пришли фото следом: https://kydaidy.com/shadow"
        )
        return

    if not settings.gemini_key:
        logger.error("gemini_key not configured — cannot generate shadow portrait")
        await message.answer("Сейчас рисунок недоступен. Напиши @kydaidy — поможем вручную.")
        return

    code = winner_from_counts(decode_distribution(dist))
    a = ARCHETYPES[code]
    status = await message.answer(
        f"Рисую твою Тень — *{a['name']}*… Это займёт около минуты. Не закрывай чат 🕯️",
        parse_mode="Markdown",
    )

    try:
        buf = io.BytesIO()
        await message.bot.download(message.photo[-1], destination=buf)
        photo_bytes = buf.getvalue()

        portrait = await generate_hero_image(
            photo_bytes, code, clean=True, api_key=settings.gemini_key)
        # рендер профиля — CPU-работа PIL, уводим из event loop
        profile_png = await asyncio.to_thread(
            render_profile, portrait, dist, message.from_user.first_name)
    except Exception as e:
        logger.exception(f"shadow generation failed for {tg_id} dist={dist}: {e}")
        await message.answer(
            "Что-то пошло не так с рисунком. Попробуй прислать другое фото — "
            "или напиши @kydaidy."
        )
        return
    finally:
        try:
            await status.delete()
        except Exception:
            pass

    # Хостим портрет → ссылка на веб-профиль (HD-скачивание + шеринг).
    token = portrait_store.put(portrait)
    portrait_url = f"{BOT_PUBLIC_URL}/p/{token}"
    profile_url = f"{SITE_URL}/profile?d={dist}&p={quote(portrait_url, safe='')}"
    kbd = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌑 Открыть в HD / поделиться", url=profile_url)],
    ])

    await message.answer_photo(
        BufferedInputFile(profile_png, filename="arhetip-profil.png"),
        caption=f"🌑 Твой архетипический профиль · ведущая Тень: {a['name']}",
    )
    await message.answer(
        "Сохрани картинку (зажми → «Сохранить») или открой в высоком качестве "
        "и поделись — кнопка ниже. Отметишь @kydaidy 🤍",
        reply_markup=kbd,
    )
    await message.answer(
        "Хочешь разобраться со своей Тенью глубже — посмотри, что у меня есть.",
        reply_markup=_main_menu_keyboard(),
    )
    _pending_shadow.pop(tg_id, None)


@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    await upsert_user(user.id, user.username, user.first_name)

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
        f"🎁 Привет.\n\nЯ Алёна Kyda Idy. Я отправляю тебе твою карту.\n\n"
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
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✦ «Манифест 7» — 1 990 ₽", callback_data="buy:manifest_7")],
            [InlineKeyboardButton(text="✦ Клуб «Манифест» — 990 ₽/мес", callback_data="buy:manifest_club")],
            [InlineKeyboardButton(text="✦ «Манифест 1:1» — от 7 000 ₽", callback_data="buy:manifest_1on1")],
            [InlineKeyboardButton(text="📍 Пройти карту перепутья (бесплатно)", callback_data="quiz")],
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
        kbd = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, url=url)]])
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
    await bot.send_message(user_id, PRODUCT_FALLBACKS[code], parse_mode="Markdown")
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
        "Карта перепутья — диагностический квиз. 10 вопросов, 5 минут.\n\n"
        "Пройди здесь: https://tally.so/r/YOUR_QUIZ_ID\n\n"
        "После прохождения автоматически вернёшься сюда с твоей картой."
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
        await target.answer("Кабинет пока пуст. Начни с /quiz")
        return

    text = f"*Твой кабинет*\n\n"
    if user["povorot"]:
        text += f"📍 Поворот: {user['povorot']} — {POVOROT_NAMES[user['povorot']]}\n"
    if purchases:
        text += "\n*Покупки:*\n"
        for p in purchases:
            text += f"  • {p['product_code']} — {p['amount']} ₽\n"
    else:
        text += "\nПокупок пока нет.\n\nЕсли хочешь идти глубже: /products"

    await target.answer(text, parse_mode="Markdown")
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.message(Command("club"))
async def show_club(message: Message):
    await message.answer(CLUB_DESCRIPTION, parse_mode="Markdown")


@router.message(Command("stop"))
async def cmd_stop_nurture(message: Message):
    await stop_nurture(message.from_user.id)
    await message.answer("Поняла. Не буду писать ежедневно.\n\nКанал @kydaidy всегда открыт.")


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "*Команды бота*\n\n"
        "/start — главное меню\n"
        "/quiz — пройти карту перепутья\n"
        "/products — что доступно\n"
        "/cabinet — мой кабинет\n"
        "/club — про Клуб «Манифест»\n"
        "/stop — отписаться от ежедневных сообщений\n"
        "/help — эта справка",
        parse_mode="Markdown",
    )


def _detect_product_code(caption: str | None) -> str | None:
    """Определяет product_code по тексту/caption поста от Tribute.
    Самое специфичное вперёд (1:1) → дальше клуб → манифест 7.
    """
    if not caption:
        return None
    text = caption.lower()
    if "1:1" in text or "1 на 1" in text or "личная сессия" in text or "audio-call" in text:
        return "manifest_1on1"
    if "клуб" in text or "манифест дня" in text or "5-7 минут" in text or "5–7 минут" in text:
        return "manifest_club"
    if "манифест 7" in text or "воркбук" in text or "pdf" in text or "7 аудио" in text or "5 поворотов" in text:
        return "manifest_7"
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
            [InlineKeyboardButton(text="✦ Манифест 7", callback_data=f"cap:manifest_7:{src_message_id}")],
            [InlineKeyboardButton(text="✦ Клуб «Манифест»", callback_data=f"cap:manifest_club:{src_message_id}")],
            [InlineKeyboardButton(text="✦ Манифест 1:1", callback_data=f"cap:manifest_1on1:{src_message_id}")],
        ]),
    )


@router.message()
async def fallback(message: Message):
    """Любые непонятные сообщения — мягко вернуть в меню."""
    await message.answer(
        "Я не отвечаю на свободный текст — пока что.\n\n"
        "Если хочешь связаться лично — напиши мне: @kydaidy\n\n"
        "Команды бота: /help",
    )
