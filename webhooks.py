"""Webhooks для Tally (квиз) и Tribute (платежи)."""

from __future__ import annotations

import base64
import hmac
import hashlib
import logging
import json

from aiohttp import web
from aiogram import Bot

from urllib.parse import quote

from config import settings
from database import upsert_user, add_purchase, add_subscription, get_user
from handlers import _send_povorot_result

logger = logging.getLogger(__name__)


def _verify_tribute_signature(body: bytes, signature: str) -> bool:
    """Проверка подписи Tribute webhook (HMAC-SHA256, hex, header Trbt-Signature).

    Tribute подписывает тем же ключом, что и API (см. DAY_LOG), поэтому при
    отсутствии отдельного webhook-секрета используем tribute_api_key.
    Fail-closed: если секрета нет вообще — отклоняем (кроме явного дев-режима).
    """
    secret = settings.tribute_webhook_secret or settings.tribute_api_key
    if not secret:
        return settings.webhook_dev_allow_unsigned
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _verify_tally_signature(body: bytes, signature: str) -> bool:
    """Проверка подписи Tally webhook (HMAC-SHA256, base64, header tally-signature).

    Fail-closed: без настроенного tally_webhook_secret все запросы отклоняются
    (кроме явного дев-режима) — эндпоинт пишет в прод-БД.
    """
    if not settings.tally_webhook_secret:
        return settings.webhook_dev_allow_unsigned
    expected = base64.b64encode(
        hmac.new(settings.tally_webhook_secret.encode(), body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, signature)


async def tally_webhook(request: web.Request) -> web.Response:
    """Webhook от Tally после прохождения квиза.

    В Tally → Settings → Integrations → Webhook → URL.
    Tally отправляет JSON с ответами + UTM-параметром, который мы извлекаем как 'povorot'.
    """
    try:
        body = await request.read()

        signature = request.headers.get("tally-signature", "")
        if not _verify_tally_signature(body, signature):
            logger.warning(f"Tally webhook: bad signature, body={body[:200]!r}")
            return web.Response(status=403, text="invalid signature")

        data = json.loads(body)
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
        return web.Response(status=500, text="error")


# Реальные имена событий Tribute (camelCase) + совместимость со старыми плоскими.
_TRIBUTE_SUB_EVENTS = {"newSubscription", "renewedSubscription", "subscription.created"}
_TRIBUTE_CANCEL_EVENTS = {"cancelledSubscription", "subscription.cancelled"}
_TRIBUTE_DIGITAL_EVENTS = {"newDigitalProduct", "purchase.completed"}


def _tribute_product_code(event: str, payload: dict) -> str | None:
    """Tribute НЕ шлёт наш product_code → маппим сами: подписку по каналу, цифровой продукт по имени."""
    if event in _TRIBUTE_SUB_EVENTS or event in _TRIBUTE_CANCEL_EVENTS:
        ch = payload.get("channel_id")
        try:
            if ch is not None and settings.manifest_club_channel_id and int(ch) == settings.manifest_club_channel_id:
                return "manifest_club"
        except (TypeError, ValueError):
            pass
        return "manifest_club"  # сейчас единственная подписка
    if event in _TRIBUTE_DIGITAL_EVENTS:
        name = str(payload.get("product_name") or payload.get("digital_product_name")
                   or payload.get("subscription_name") or "").lower()
        if any(k in name for k in ("1:1", "1 на 1", "1on1", "сесс", "встреч", "консульт", "созвон")):
            return "manifest_1on1"
        if any(k in name for k in ("воркбук", "манифест 7", "манифест7", "7 повор", "карта")):
            return "manifest_7"
        # неизвестный цифровой продукт — по цене (1:1 дороже воркбука), иначе None (залогируем)
        amt = int(payload.get("amount") or payload.get("price") or 0)
        if amt >= settings.manifest_7_price * 2:
            return "manifest_1on1"
        if amt >= settings.manifest_7_price:
            return "manifest_7"
    return None


async def tribute_webhook(request: web.Request) -> web.Response:
    """Webhook от Tribute после оплаты (реальный формат: name + вложенный payload)."""
    try:
        body = await request.read()

        signature = request.headers.get("Trbt-Signature", "")
        if not _verify_tribute_signature(body, signature):
            logger.warning(f"Tribute webhook: bad signature, body={body[:200]!r}")
            return web.Response(status=403, text="invalid signature")

        data = json.loads(body)
        # Реальный Tribute: событие в "name" (camelCase: newSubscription/newDigitalProduct/...),
        # данные во вложенном "payload". Старый плоский формат ("event"/поля в корне) — тоже поддержим.
        event = data.get("name") or data.get("event")
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else data

        if not event:
            logger.info(f"Tribute test/ping webhook OK: {body[:200]!r}")
            return web.Response(status=200, text="ok")

        # ВСЕГДА логируем сырой payload — чтобы видеть реальные имена продуктов и точно замапить.
        logger.info("Tribute event=%s payload=%s", event, json.dumps(payload, ensure_ascii=False)[:400])

        tg_id = int(payload.get("telegram_user_id") or payload.get("user_telegram_id") or 0)
        amount = int(payload.get("amount") or payload.get("price") or 0)
        payment_id = str(payload.get("subscription_id") or payload.get("order_id")
                         or payload.get("payment_id") or "")
        code = _tribute_product_code(event, payload)

        if not tg_id or not code:
            logger.warning("Tribute event=%s не замаплен (tg_id=%s code=%s) payload=%s",
                           event, tg_id, code, json.dumps(payload, ensure_ascii=False)[:400])
            return web.Response(status=200, text="ok")  # 200 — чтобы Tribute не ретраил бесконечно

        if event in _TRIBUTE_SUB_EVENTS:
            await add_subscription(tg_id, code)
            await _grant_access(request.app["bot"], tg_id, code)
            logger.info("Tribute: подписка %s выдана %s", code, tg_id)
        elif event in _TRIBUTE_DIGITAL_EVENTS:
            await add_purchase(tg_id, code, amount, payment_id)
            await _grant_access(request.app["bot"], tg_id, code)
            logger.info("Tribute: продукт %s выдан %s", code, tg_id)
        elif event in _TRIBUTE_CANCEL_EVENTS:
            logger.info("Tribute: отмена %s у %s (отзыв доступа — TODO)", code, tg_id)

        return web.Response(status=200, text="ok")
    except Exception as e:
        logger.exception(f"Tribute webhook error: {e}")
        return web.Response(status=500, text="error")


_CHANNEL_BY_PRODUCT = {
    "manifest_7": "manifest_7_channel_id",
    "manifest_club": "manifest_club_channel_id",
    "manifest_plus": "manifest_plus_channel_id",
}

_BASE_TEXTS = {
    "manifest_7": (
        "✅ Спасибо. Воркбук «Манифест 7» — твой.\n\n"
        "Что внутри:\n"
        "• Воркбук «Манифест 7» — в закрытом канале\n"
        "• Практики с проводником — прямо в боте: /praktiki. "
        "Веду шаг за шагом, темп задаёшь ты\n\n"
        "Никаких обещаний быстрого результата. Карта работает, когда ты готова смотреть."
    ),
    "manifest_club": (
        "✅ Добро пожаловать в Клуб «Манифест».\n\n"
        "Здесь — эфир раз в неделю, безлимит «Алёны на связи» и чат, где я отвечаю.\n"
        "Воркбук «Манифест 7» — в закреплённом: 80+ страниц, практики, задания и инсайты "
        "по 5 поворотам. Осваивайся."
    ),
    "manifest_plus": (
        "✅ «Манифест+» подключён.\n\n"
        "VIP-канал + персональный отклик 1×/неделя.\n"
        "Я свяжусь с тобой лично в течение 1–2 дней — для приветственного звонка."
    ),
    "manifest_1on1": (
        "✅ Запись на «Манифест 1:1» оплачена.\n\n"
        "Теперь выбери удобное окно в моём календаре и подтверди запись — там же "
        "напиши тему запроса и пару деталей, чтобы я пришла к встрече готовой."
    ),
}


async def _calendly_link_for(tg_id: int) -> str | None:
    """Ссылка на календарь Алёны для записи 1:1, с префиллом темы запроса.

    Запрос, вскрытый на AI-встрече, подставляем в первый кастомный вопрос
    Calendly (?a1=...). Нет настроенного календаря — None (fallback на текст).
    """
    base = settings.calendly_1on1_url
    if not base:
        return None
    request = None
    try:
        u = await get_user(tg_id)
        request = (u or {}).get("last_ai_request")
    except Exception:
        logger.warning("calendly prefill: get_user failed (continuing)", exc_info=True)
    if request:
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}a1={quote(request)}"
    return base


async def _create_personal_invite(bot: Bot, channel_id: int, tg_id: int, product_code: str) -> str | None:
    """Generate a unique, single-use, 7-day invite link for this purchase."""
    import time
    expire = int(time.time()) + 7 * 24 * 3600
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=channel_id,
            name=f"{product_code}-{tg_id}",
            member_limit=1,
            expire_date=expire,
        )
        return invite.invite_link
    except Exception as e:
        logger.error(f"create_chat_invite_link failed for {product_code} chat={channel_id} user={tg_id}: {e}")
        return None


async def _grant_access(bot: Bot, tg_id: int, product_code: str):
    """Выдать доступ к продукту после оплаты.

    Для канальных продуктов (manifest_7, manifest_club, manifest_plus) —
    генерируем уникальную одноразовую invite-ссылку с лимитом 1 пользователь
    и сроком 7 дней через Bot API createChatInviteLink.
    """
    text = _BASE_TEXTS.get(product_code, "✅ Оплата получена. Спасибо.")

    channel_attr = _CHANNEL_BY_PRODUCT.get(product_code)
    if channel_attr:
        channel_id = getattr(settings, channel_attr, 0)
        if channel_id:
            invite_url = await _create_personal_invite(bot, channel_id, tg_id, product_code)
            if invite_url:
                text += f"\n\n🔑 Твоя личная ссылка-доступ (одноразовая, 7 дней):\n{invite_url}"
            else:
                text += "\n\n_Если ссылки не пришло — напиши @kydaidy._"
        else:
            logger.warning(f"channel_id not configured for {product_code} — sending text only")
            text += "\n\n_Сейчас Алёна свяжется с тобой лично._"

    # 1:1 → ссылка на календарь с окнами (+ префилл темы запроса со встречи).
    if product_code == "manifest_1on1":
        cal = await _calendly_link_for(tg_id)
        if cal:
            text += f"\n\n🗓 Календарь Алёны — выбери окно:\n{cal}"
        else:
            text += "\n\n_Календарь скоро открою — Алёна свяжется с тобой лично для записи._"

    text += "\n\n— Алёна"

    try:
        await bot.send_message(tg_id, text)
    except Exception as e:
        logger.error(f"Failed to send access message to {tg_id}: {e}")

    # Notify admin about the purchase (for manifest_1on1 — Алёна вручную пишет)
    if product_code == "manifest_1on1" and settings.tg_admin_id:
        try:
            await bot.send_message(
                settings.tg_admin_id,
                f"💬 Manifest 1:1 продан\nuser tg_id={tg_id}\nNeed to schedule call.",
            )
        except Exception:
            pass


async def portrait_route(request: web.Request) -> web.Response:
    """Отдаёт сгенерированный портрет Тени странице профиля (геро-слот)."""
    from portrait_store import get as get_portrait
    data = get_portrait(request.match_info.get("token", ""))
    if not data:
        return web.Response(status=404, text="not found")
    return web.Response(
        body=data,
        content_type="image/png",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=3600",
        },
    )


def setup_webhooks(app: web.Application, bot: Bot):
    app["bot"] = bot
    app.router.add_post("/webhook/tally", tally_webhook)
    app.router.add_post("/webhook/tribute", tribute_webhook)
    app.router.add_get("/p/{token}", portrait_route)
    app.router.add_get("/", lambda r: web.Response(text="kydaidy bot is running"))
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
