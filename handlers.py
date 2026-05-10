"""Обработчики команд бота."""

import logging
from pathlib import Path

from aiogram import Router, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from config import settings
from database import upsert_user, get_user, start_nurture, stop_nurture, get_user_purchases
from content_data import (
    POVOROT_RESULTS,
    POVOROT_NAMES,
    POVOROT_TAGLINES,
    PDF_FILES,
    AUDIO_FILES,
    WELCOME_NO_POVOROT,
    PRODUCTS_MENU,
    CLUB_DESCRIPTION,
)

logger = logging.getLogger(__name__)
router = Router()


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
    """Старт с UTM-параметром: /start povorot3 → выдать карту по повороту 3."""
    args = command.args or ""

    povorot = None
    if args.startswith("povorot"):
        try:
            povorot = int(args.replace("povorot", ""))
            if povorot not in (1, 2, 3, 4, 5):
                povorot = None
        except ValueError:
            povorot = None

    user = message.from_user
    await upsert_user(user.id, user.username, user.first_name, povorot)

    if povorot:
        await _send_povorot_result(message, povorot)
    else:
        await message.answer(WELCOME_NO_POVOROT, reply_markup=_main_menu_keyboard())


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
        await message.answer_voice(
            FSInputFile(audio_path),
            caption="60 секунд от меня — про твой поворот",
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


@router.callback_query(F.data == "products")
@router.message(Command("products"))
async def show_products(event):
    target = event.message if isinstance(event, CallbackQuery) else event
    await target.answer(PRODUCTS_MENU, parse_mode="Markdown")
    if isinstance(event, CallbackQuery):
        await event.answer()


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


@router.message()
async def fallback(message: Message):
    """Любые непонятные сообщения — мягко вернуть в меню."""
    await message.answer(
        "Я не отвечаю на свободный текст — пока что.\n\n"
        "Если хочешь связаться лично — напиши мне: @kydaidy\n\n"
        "Команды бота: /help",
    )
