"""Auto-nurture серия: 7 дней после захвата лида.

Запускается через APScheduler — каждый час проверяет кому пора отправить следующее сообщение.
"""

import logging
from aiogram import Bot

from database import get_users_for_nurture, advance_nurture_day, stop_nurture
from content_data import NURTURE_DAYS, POVOROT_NAMES

logger = logging.getLogger(__name__)


async def send_nurture_message(bot: Bot, tg_id: int, povorot: int, day: int):
    """Отправить сообщение дня X пользователю."""
    text_template = NURTURE_DAYS.get(day)
    if not text_template:
        return

    text = text_template.format(povorot=povorot, povorot_name=POVOROT_NAMES.get(povorot, ""))

    try:
        await bot.send_message(tg_id, text, parse_mode="Markdown")
        await advance_nurture_day(tg_id, day)
        logger.info(f"Nurture day {day} sent to {tg_id}")
    except Exception as e:
        logger.error(f"Failed to send nurture to {tg_id}: {e}")
        # Если юзер заблокировал бот — отключаем nurture
        if "blocked" in str(e).lower() or "forbidden" in str(e).lower():
            await stop_nurture(tg_id)


async def run_nurture_tick(bot: Bot):
    """Тикер: каждый час проверяем и шлём nurture сообщения."""
    users = await get_users_for_nurture()
    for user in users:
        next_day = (user["nurture_day"] or 0) + 1
        if next_day > 7:
            await stop_nurture(user["tg_id"])
            continue
        await send_nurture_message(bot, user["tg_id"], user["povorot"], next_day)
        if next_day == 7:
            await stop_nurture(user["tg_id"])
