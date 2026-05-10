"""Webhooks для Tally (квиз) и Tribute (платежи)."""

import hmac
import hashlib
import logging
import json

from aiohttp import web
from aiogram import Bot

from config import settings
from database import upsert_user, add_purchase, add_subscription
from handlers import _send_povorot_result

logger = logging.getLogger(__name__)


def _verify_tribute_signature(body: bytes, signature: str) -> bool:
    """Проверка подписи Tribute webhook."""
    if not settings.tribute_webhook_secret:
        return True  # для дев-режима
    expected = hmac.new(
        settings.tribute_webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def tally_webhook(request: web.Request) -> web.Response:
    """Webhook от Tally после прохождения квиза.

    В Tally → Settings → Integrations → Webhook → URL.
    Tally отправляет JSON с ответами + UTM-параметром, который мы извлекаем как 'povorot'.
    """
    try:
        data = await request.json()
        logger.info(f"Tally webhook: {json.dumps(data)[:200]}")

        # Извлекаем tg_id и povorot из hidden fields формы
        # Tally хранит ответы в data["data"]["fields"]
        fields = {f["label"]: f.get("value") for f in data.get("data", {}).get("fields", [])}

        tg_id = fields.get("tg_id")
        povorot = fields.get("povorot")

        if not tg_id or not povorot:
            return web.Response(status=200, text="missing fields, ignoring")

        try:
            tg_id = int(tg_id)
            povorot = int(povorot)
        except (TypeError, ValueError):
            return web.Response(status=200, text="invalid fields")

        if povorot not in (1, 2, 3, 4, 5):
            return web.Response(status=200, text="invalid povorot")

        await upsert_user(tg_id, None, None, povorot)
        # send_povorot_result не вызываем напрямую — она требует Message,
        # вместо этого юзер сам перейдёт в бот по deeplink t.me/bot?start=povorot{N}

        return web.Response(status=200, text="ok")
    except Exception as e:
        logger.exception(f"Tally webhook error: {e}")
        return web.Response(status=500, text=str(e))


async def tribute_webhook(request: web.Request) -> web.Response:
    """Webhook от Tribute после оплаты.

    Tribute отправляет signed payload с product_code, amount, user_telegram_id.
    """
    try:
        body = await request.read()

        signature = request.headers.get("Trbt-Signature", "")
        if not _verify_tribute_signature(body, signature):
            logger.warning(f"Tribute webhook: bad signature, body={body[:200]!r}")
            return web.Response(status=403, text="invalid signature")

        data = json.loads(body)
        event_type = data.get("event")

        if event_type is None:
            logger.info(f"Tribute test webhook OK: {body[:200]!r}")
            return web.Response(status=200, text="ok")

        tg_id = int(data.get("user_telegram_id", 0))
        product_code = data.get("product_code")
        amount = int(data.get("amount", 0))
        payment_id = data.get("payment_id")

        if event_type == "purchase.completed":
            await add_purchase(tg_id, product_code, amount, payment_id)
            await _grant_access(request.app["bot"], tg_id, product_code)

        elif event_type == "subscription.created":
            await add_subscription(tg_id, product_code)
            await _grant_access(request.app["bot"], tg_id, product_code)

        elif event_type == "subscription.cancelled":
            # тут логика отзыва доступа
            pass

        return web.Response(status=200, text="ok")
    except Exception as e:
        logger.exception(f"Tribute webhook error: {e}")
        return web.Response(status=500, text=str(e))


async def _grant_access(bot: Bot, tg_id: int, product_code: str):
    """Выдать доступ к продукту после оплаты."""
    messages = {
        "manifest_7": (
            "✅ Спасибо. «Манифест 7» — твой.\n\n"
            "Доступ к воркбуку и 7 аудио-практикам в личном кабинете: /cabinet\n\n"
            "Никаких обещаний быстрого результата. Прохождение в твоём темпе.\n\n— Алёна"
        ),
        "manifest_club": (
            "✅ Добро пожаловать в Клуб «Манифест».\n\n"
            "Каждое утро в 6:00 — «Манифест дня». Первое голосовое придёт завтра.\n\n"
            "Закрытый канал: https://t.me/+XXXX (заменить ссылку)\n\n— Алёна"
        ),
        "manifest_plus": (
            "✅ «Манифест+» подключён.\n\n"
            "Закрытый VIP-канал: https://t.me/+YYYY (заменить ссылку)\n\n"
            "Я свяжусь с тобой лично в течение 1–2 дней — для приветственного звонка.\n\n— Алёна"
        ),
        "manifest_1on1": (
            "✅ Запись на «Манифест 1:1» оплачена.\n\n"
            "Я свяжусь с тобой в течение 24 часов для согласования времени сессии.\n\n— Алёна"
        ),
    }
    text = messages.get(product_code, f"✅ Оплата получена. Спасибо.")
    try:
        await bot.send_message(tg_id, text)
    except Exception as e:
        logger.error(f"Failed to send access message to {tg_id}: {e}")


def setup_webhooks(app: web.Application, bot: Bot):
    app["bot"] = bot
    app.router.add_post("/webhook/tally", tally_webhook)
    app.router.add_post("/webhook/tribute", tribute_webhook)
    app.router.add_get("/", lambda r: web.Response(text="kydaidy bot is running"))
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
