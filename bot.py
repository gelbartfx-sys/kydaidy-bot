"""kydaidy Telegram bot — main entry point.

Запуск:
- Локально: python bot.py
- На Render: автоматически через Procfile (или Start Command: python bot.py)

Архитектура:
- aiogram 3 для Telegram API
- aiohttp как webhook server
- SQLite для хранения юзеров, покупок, nurture-стейта
- APScheduler для 7-дневной nurture-серии

Источник правды для контента: content_data.py
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import settings
from database import init_db
from handlers import router
from nurture import run_nurture_tick
from webhooks import setup_webhooks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class DMInspectMiddleware(BaseMiddleware):
    """Logs every DM update so we can see exactly what arrives from
    Tribute mini-app share. Does not interfere with normal handlers."""

    async def __call__(self, handler, event: Update, data):
        m = getattr(event, "message", None)
        if m and m.chat and m.chat.type == "private":
            via = m.via_bot.username if m.via_bot else None
            fwd = type(m.forward_origin).__name__ if m.forward_origin else None
            sender_chat = m.sender_chat.id if m.sender_chat else None
            from_user_id = m.from_user.id if m.from_user else None
            txt = (m.text or m.caption or "")[:80]
            logger.info(
                f"DM update_id={event.update_id} "
                f"msg_id={m.message_id} "
                f"from_user={from_user_id} "
                f"chat={m.chat.id} "
                f"via=@{via} "
                f"fwd_origin={fwd} "
                f"sender_chat={sender_chat} "
                f"photo={bool(m.photo)} "
                f"buttons={bool(m.reply_markup)} "
                f"text={txt!r}"
            )
        return await handler(event, data)


async def main():
    await init_db()

    bot = Bot(
        token=settings.tg_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
    dp = Dispatcher()
    dp.update.outer_middleware(DMInspectMiddleware())
    dp.include_router(router)

    # Запуск nurture-tick каждый час
    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_nurture_tick, "interval", hours=1, args=[bot])
    scheduler.start()

    # Webhook server (для Tally + Tribute)
    app = web.Application()
    setup_webhooks(app, bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.port)
    await site.start()
    logger.info(f"Webhook server started on port {settings.port}")

    # Polling Telegram (на старте — polling, потом можно переключить на webhook)
    logger.info("Starting Telegram polling...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        scheduler.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
