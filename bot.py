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

from aiogram import Bot, Dispatcher, BaseMiddleware, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update, Message
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import settings
from database import init_db, reconcile_oneonone_due
from handlers import router
from manifest7_guide import guide_router
from alena_chat import (alena_router, run_stale_session_tick,
                        run_orphan_turn_tick, run_club_ladder_tick,
                        run_dead_session_tick, run_reengage_tick)
from heygen_credits import run_credit_check
from booking import book_router
from calendly import reconcile_tick as calendly_reconcile_tick
from curator import curator_router, push_daily_batch, publish_tick
from growth_agent import growth_router, run_growth_tick
from followup import run_followup_tick
from nurture import run_nurture_tick
from quiz_atmosfera import atm_router, run_atm_nextday_tick
from sixsec import sixsec_router, run_sixsec_tick
from checkin import checkin_router
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
            fwd_type = type(m.forward_origin).__name__ if m.forward_origin else None
            fwd_chat_id = None
            fwd_msg_id = None
            fwd_chat_title = None
            if m.forward_origin:
                # MessageOriginChannel has .chat and .message_id
                # MessageOriginUser has .sender_user
                # MessageOriginChat has .sender_chat
                if hasattr(m.forward_origin, "chat") and m.forward_origin.chat:
                    fwd_chat_id = m.forward_origin.chat.id
                    fwd_chat_title = m.forward_origin.chat.title
                if hasattr(m.forward_origin, "message_id"):
                    fwd_msg_id = m.forward_origin.message_id
                if hasattr(m.forward_origin, "sender_chat") and m.forward_origin.sender_chat:
                    fwd_chat_id = m.forward_origin.sender_chat.id
                    fwd_chat_title = m.forward_origin.sender_chat.title
            sender_chat = m.sender_chat.id if m.sender_chat else None
            from_user_id = m.from_user.id if m.from_user else None
            txt = (m.text or m.caption or "")[:80]
            logger.info(
                f"DM update_id={event.update_id} "
                f"msg_id={m.message_id} "
                f"from_user={from_user_id} "
                f"chat={m.chat.id} "
                f"via=@{via} "
                f"fwd_origin={fwd_type} "
                f"fwd_chat_id={fwd_chat_id} "
                f"fwd_chat_title={fwd_chat_title!r} "
                f"fwd_msg_id={fwd_msg_id} "
                f"sender_chat={sender_chat} "
                f"photo={bool(m.photo)} "
                f"buttons={bool(m.reply_markup)} "
                f"text={txt!r}"
            )
        return await handler(event, data)



_ADMIN_IDS = {6271776494, 680319075}  # Кай, Алёна


async def _admin_chat_id(message: Message):
    """Служебный (только админы): переслать сообщение из канала/чата → бот вернёт chat_id.
    Зарегистрирован НА dp (проверяется раньше всех роутеров). Безопасно: чужих не трогает."""
    try:
        origin = getattr(message, "forward_origin", None)
        chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if chat is None:
            chat = getattr(message, "forward_from_chat", None)
        if chat is not None:
            title = getattr(chat, "title", "") or getattr(chat, "username", "") or ""
            await message.reply(f"chat_id: {chat.id}\n{title}")
        else:
            await message.reply("Переслано из скрытого источника — chat_id недоступен.")
    except Exception:
        logging.exception("admin chat_id handler failed")


async def _oneonone_reconcile_tick():
    """Ежедневная сверка счётчика встреч 1:1 (страховка на случай потери
    вебхука продления). Крэш-сейф — ошибка не роняет планировщик."""
    try:
        n = await reconcile_oneonone_due()
        if n:
            logging.info("1:1 reconcile: досброшено счётчиков — %s", n)
    except Exception:
        logging.exception("1:1 reconcile tick failed")


async def main():
    await init_db()

    bot = Bot(
        token=settings.tg_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
    dp = Dispatcher()
    # Служебный админ-хэндлер (chat_id из форварда) — на dp, раньше всех роутеров.
    dp.message.register(_admin_chat_id, F.forward_origin, F.from_user.id.in_(_ADMIN_IDS))
    dp.update.outer_middleware(DMInspectMiddleware())
    # curator_router ПЕРВЫМ: текст-фильтр режима правки (только когда куратор
    # awaiting='edit') должен перехватить сообщение Алёны раньше AI-встречи и catch-all.
    # alena_router и guide_router: их текст-фильтры (активная встреча / практика)
    # должны сработать раньше catch-all fallback в router.
    # alena РАНЬШE guide: активная встреча с Алёной перебивает зависшую практику.
    dp.include_router(curator_router)
    dp.include_router(alena_router)
    dp.include_router(guide_router)
    # book_router: /zapis + callback'и записи 1:1 — раньше главного router,
    # чтобы команду записи не перехватил catch-all fallback.
    dp.include_router(book_router)
    # growth_router — только callback-кнопки ревью реактивации (без текст-фильтров,
    # конфликтов с catch-all не создаёт). После основного router тоже ок.
    dp.include_router(growth_router)
    # atm_router (тест «Атмосфера дома», E1/T1): /dom + callback'и atmq:* —
    # раньше главного router, чтобы /dom не съел catch-all fallback.
    dp.include_router(atm_router)
    # sixsec_router («6 секунд», on-ramp): callback'и six:* — раньше главного
    # router, чтобы не съел catch-all fallback. Инвайт в конце реюзит atmq:invite
    # (в atm_router выше). Только callback'и, текст-фильтров нет — конфликтов нет.
    dp.include_router(sixsec_router)
    # checkin_router: /checkin + callback chk:* — только callback'и + команда,
    # текст-фильтров нет (конфликтов с главным router нет). Узел кольца: парный
    # gate банка (подтверждение партнёра), чинит рост банка на само-отчёт в sixsec.
    dp.include_router(checkin_router)
    dp.include_router(router)

    # Запуск nurture-tick каждый час
    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_nurture_tick, "interval", hours=1, args=[bot])
    # Контент-конвейер: утренняя рассылка батча куратору + дрип-автопостинг в канал.
    scheduler.add_job(
        push_daily_batch, "cron",
        hour=settings.curator_push_hour, minute=0,
        timezone=settings.curator_tz, args=[bot])
    scheduler.add_job(
        publish_tick, "interval",
        minutes=settings.curator_publish_every_min, args=[bot])
    # Hermes-руки: дневной тик реактивации. Сам джоб no-op, пока
    # growth_agent_enabled=False — кандидаты не набираются, никому ничего не шлётся.
    scheduler.add_job(
        run_growth_tick, "interval",
        hours=settings.growth_tick_hours, args=[bot])
    # Hermes #1: мягкий оффер Клуба на «затихшей» AI-встрече (человек замолчал
    # на пике). Джоб no-op, если stale_nudge_enabled=False. Проверка — часто,
    # порог молчания (stale_nudge_minutes) фильтрует сам запрос.
    scheduler.add_job(
        run_stale_session_tick, "interval",
        minutes=settings.stale_nudge_tick_min, args=[bot])
    # Re-engage (Кай 09.07): мягкое «я тут, жду» ДО оффер-нуджа, если человек затих.
    scheduler.add_job(
        run_reengage_tick, "interval",
        minutes=settings.stale_nudge_tick_min, args=[bot])
    # T-1 (03.07): само-восстановление хода, убитого редеплоем (реплика клиентки
    # без ответа >2 мин) — доотвечаем сами, тишина себя чинит.
    scheduler.add_job(run_orphan_turn_tick, "interval", minutes=2, args=[bot])
    # Батч Б: мёртвая встреча (клиент замолк после наджа, turns≥2, молчит
    # ≥dead_session_minutes) закрывается ВДОГОНКУ с оффером — иначе лид уходит мимо
    # оффер-пути (ни оффера, ни followup-серии). No-op, если таких встреч нет.
    scheduler.add_job(
        run_dead_session_tick, "interval",
        minutes=settings.dead_session_tick_min, args=[bot])
    # Спящая лестница 1:1 (совещание 03.07): члену Клуба ≥14 дней — разовое
    # приглашение на живой разбор. При 0 членов — no-op.
    scheduler.add_job(run_club_ladder_tick, "interval", hours=24, args=[bot])
    # Волна 1 (H6/H7): дожим после оффера — серия из 3 касаний (45м/24ч/72ч).
    # Оплатившие отфильтровываются в самом запросе; no-op при FOLLOWUP_ENABLED=0.
    scheduler.add_job(
        run_followup_tick, "interval",
        minutes=settings.followup_tick_min, args=[bot])
    # Тест «Атмосфера дома»: next-day чек ~20 ч после прохождения (E1/T1).
    scheduler.add_job(run_atm_nextday_tick, "interval", minutes=30, args=[bot])
    # «6 секунд» (Шаг 2, on-ramp): вечера 2–3 через ~20 ч после предыдущего.
    scheduler.add_job(run_sixsec_tick, "interval", minutes=30, args=[bot])
    # HeyGen кредит-монитор: заранее пишет Каю, когда кредиты на исходе (живые
    # кружки коуча их тратят). No-op, пока не задан HEYGEN_API_KEY.
    scheduler.add_job(
        run_credit_check, "interval",
        hours=settings.credit_check_hours, args=[bot])
    # Подписочный 1:1: страховка сброса счётчика встреч. Если вебхук продления
    # потерялся, cron добьёт sessions_left до тарифа активным подписчикам, чей
    # период старше ~30 дней — оплативший не заперт со 2-го месяца.
    scheduler.add_job(_oneonone_reconcile_tick, "interval", hours=24)
    # Calendly polling: списание на реальную бронь / возврат при отмене-незаписи.
    # No-op без CALENDLY_API_TOKEN (флоу деградирует к ручному возврату Алёной).
    scheduler.add_job(calendly_reconcile_tick, "interval",
                      minutes=settings.calendly_poll_min, args=[bot])
    scheduler.start()

    # Батч Б: прогнать orphan-восстановление СРАЗУ на старте (не ждать первого
    # интервального тика). Редеплой рвёт ход и рестартит планировщик — до 5 мин
    # тишины; разовый create_task чинит подвисшие встречи мгновенно после подъёма.
    asyncio.create_task(run_orphan_turn_tick(bot))
    # Страховка сна на Render free (диагноз 15.07): web-сервис на free-плане
    # засыпает после ~15 мин без ВХОДЯЩЕГО HTTP → APScheduler замирает ровно в окна
    # молчания, которые ловят stale/dead-тики (у затихшей встречи nudged_at так и
    # остаётся NULL, dead-тик её не берёт — потерянный лид). Реальный фикс — внешний
    # keep-alive пинг на /health каждые 10–14 мин (UptimeRobot/cron-job.org) или уход
    # с free-плана. Здесь — подстраховка: как только инстанс проснулся (любой апдейт/
    # деплой/пинг), сразу подметаем просроченные затихшие и мёртвые встречи, не ожидая
    # интервального тика. Тики крэш-сейф внутри; no-op, если таких встреч нет.
    asyncio.create_task(run_stale_session_tick(bot))
    asyncio.create_task(run_dead_session_tick(bot))

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
