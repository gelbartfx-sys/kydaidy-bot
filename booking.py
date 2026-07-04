"""Запись на личную встречу 1:1 — подписочная модель (мандат Кая 04.07).

Продаём 1:1 не разово, а ПОДПИСКОЙ: тариф «1 встреча/мес» (sZXq) или
«3 встречи/мес» (sZXr). Счётчик встреч (oneonone_subs) гейтит запись — записаться
сверх тарифа невозможно. Оплата/продление в Tribute сбрасывает счётчик на полный
тариф (см. webhooks.py). Здесь — сама запись в боте:

  /zapis (или deep-link ?start=book1on1, или кнопка из канала «Манифест · 1:1»)
    → проверяем счётчик
    → если есть встречи: явное согласие «списать 1» (чтобы человек понимал)
    → списываем 1 и даём ссылку на календарь Алёны (Calendly)

Принцип Кая: безопасно для нас (жёсткий кап по тарифу) + просто и приятно для
пользователя (понятный счётчик, явное подтверждение, дверь к Алёне если что).
"""
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (Message, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup)

from config import settings
from database import get_oneonone, dec_oneonone, get_user

logger = logging.getLogger(__name__)

book_router = Router()

# Подписки-тарифы 1:1 в Tribute (deep-link mini-app).
TARIFF_1X_URL = "https://t.me/tribute/app?startapp=sZXq"  # 1 встреча/мес
TARIFF_3X_URL = "https://t.me/tribute/app?startapp=sZXr"  # 3 встречи/мес

CURATOR = "@al_lazovsky"


def _calendly_url(user: dict | None) -> str:
    """Ссылка на календарь Алёны; если знаем вскрытый запрос — префиллим тему."""
    url = settings.calendly_1on1_url
    req = (user or {}).get("last_ai_request") if user else None
    if req:
        from urllib.parse import quote
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}a1={quote(str(req)[:200])}"
    return url


async def start_booking(message: Message):
    """Точка входа записи: показать статус счётчика и предложить записаться."""
    tg_id = message.from_user.id
    sub = await get_oneonone(tg_id)

    # Нет активной подписки на встречи → мягко предлагаем оформить (оба тарифа).
    if not sub:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="1 встреча в месяц", url=TARIFF_1X_URL)],
            [InlineKeyboardButton(text="3 встречи в месяц", url=TARIFF_3X_URL)],
        ])
        await message.answer(
            "Личные встречи со мной идут по подписке — так я держу для тебя "
            "место в расписании каждый месяц.\n\n"
            "Выбери, как тебе удобнее, и вернись сюда — запишемся:",
            reply_markup=kb, parse_mode=None)
        return

    left = int(sub.get("sessions_left") or 0)
    tariff = int(sub.get("tariff") or 1)

    # Встречи на месяц исчерпаны → без жёсткости, дверь к Алёне открыта.
    if left <= 0:
        await message.answer(
            f"На этот месяц ты уже записала все встречи по своему тарифу "
            f"({tariff} в месяц) 🌙\n\n"
            "В начале следующего месяца счётчик станет полным — и мы снова "
            "увидимся.\n\n"
            f"Если что-то важное и ждать не хочется — напиши мне: {CURATOR}.",
            parse_mode=None)
        return

    # Есть встречи → явное согласие на списание (человек понимает, что тратит одну).
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, записать встречу",
                              callback_data="book_confirm")],
        [InlineKeyboardButton(text="Не сейчас", callback_data="book_cancel")],
    ])
    await message.answer(
        f"У тебя осталось встреч в этом месяце: {left} из {tariff}.\n\n"
        "Записать сейчас? Спишется одна встреча из тарифа — а дальше выберешь "
        "удобное время в моём календаре.",
        reply_markup=kb, parse_mode=None)


@book_router.message(Command("zapis"))
async def cmd_zapis(message: Message):
    await start_booking(message)


@book_router.callback_query(F.data == "book_cancel")
async def cb_book_cancel(callback: CallbackQuery):
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "Хорошо, я тут. Захочешь записаться — команда /zapis.",
        parse_mode=None)


@book_router.callback_query(F.data == "book_confirm")
async def cb_book_confirm(callback: CallbackQuery):
    """Списываем 1 встречу под guard и отдаём ссылку на календарь."""
    await callback.answer()
    tg_id = callback.from_user.id
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    ok = await dec_oneonone(tg_id)
    if not ok:
        # Гонка/двойной клик/истёк тариф — счётчик защитил нас от переспенда.
        await callback.message.answer(
            "Похоже, встречи на этот месяц уже закончились — счётчик обновится "
            f"с продлением подписки. Если нужно раньше, напиши мне: {CURATOR}.",
            parse_mode=None)
        return

    sub = await get_oneonone(tg_id)
    left = int((sub or {}).get("sessions_left") or 0)
    user = await get_user(tg_id)
    url = _calendly_url(user)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Выбрать время", url=url)],
    ])
    await callback.message.answer(
        "Готово, встреча за тобой закреплена 🌑\n\n"
        f"Осталось встреч в этом месяце: {left}.\n\n"
        "Выбери удобное время в моём календаре — и я буду ждать тебя там. "
        f"Если планы изменятся, просто напиши мне: {CURATOR}.",
        reply_markup=kb, parse_mode=None)
