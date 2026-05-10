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

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
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


async def main():
    await init_db()

    bot = Bot(
        token=settings.tg_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
    dp = Dispatcher()
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
