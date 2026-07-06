"""Webhooks для Tally (квиз) и Tribute (платежи)."""

from __future__ import annotations

import base64
import hmac
import hashlib
import logging
import json
from datetime import datetime

from aiohttp import web
from aiogram import Bot

from urllib.parse import quote

from config import settings
from database import (upsert_user, add_purchase, add_subscription, get_user,
                      set_oneonone, get_oneonone, deactivate_subscription, log_event)
from handlers import _send_povorot_result

logger = logging.getLogger(__name__)


def _to_int(v) -> int:
    """Безопасный парс числа из вебхука Tribute. Сумма/id могут прийти строкой или
    десятичной ('990.00') → голый int() бы кинул ValueError → вебхук падал в 500 →
    Tribute ретраил бесконечно, а админ НЕ уведомлялся (оплата терялась молча).
    '990.00'→990, мусор/None→0."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


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
            ch_int = int(ch) if ch is not None else None
        except (TypeError, ValueError):
            ch_int = None
        # Подписочный 1:1 (мандат 04.07) — отдельный закрытый канал.
        if ch_int is not None and settings.manifest_1on1_channel_id \
                and ch_int == settings.manifest_1on1_channel_id:
            return "manifest_1on1"
        if ch_int is not None and settings.manifest_club_channel_id \
                and ch_int == settings.manifest_club_channel_id:
            return "manifest_club"
        # Фолбэк по имени, если channel_id не пришёл (различаем 1:1 и Клуб).
        name = str(payload.get("subscription_name") or payload.get("product_name") or "").lower()
        if any(k in name for k in ("1:1", "1 на 1", "1on1", "встреч", "сесс")):
            return "manifest_1on1"
        if any(k in name for k in ("клуб", "club")):
            return "manifest_club"
        # Неоднозначно (нет ни channel_id, ни узнаваемого имени) → НЕ дефолтим в
        # Клуб (это мис-грант не в тот канал), а отдаём None → зовём админа разрулить.
        return None
    if event in _TRIBUTE_DIGITAL_EVENTS:
        name = str(payload.get("product_name") or payload.get("digital_product_name")
                   or payload.get("subscription_name") or "").lower()
        if any(k in name for k in ("1:1", "1 на 1", "1on1", "сесс", "встреч", "консульт", "созвон")):
            return "manifest_1on1"
        if any(k in name for k in ("воркбук", "манифест 7", "манифест7", "7 повор", "карта")):
            return "manifest_7"
        # неизвестный цифровой продукт — по цене (1:1 дороже воркбука), иначе None (залогируем)
        amt = _to_int(payload.get("amount") or payload.get("price") or 0)
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

        tg_id = _to_int(payload.get("telegram_user_id") or payload.get("user_telegram_id") or 0)
        amount = _to_int(payload.get("amount") or payload.get("price") or 0)
        payment_id = str(payload.get("subscription_id") or payload.get("order_id")
                         or payload.get("payment_id") or "")
        code = _tribute_product_code(event, payload)

        if not tg_id or not code:
            logger.warning("Tribute event=%s не замаплен (tg_id=%s code=%s) payload=%s",
                           event, tg_id, code, json.dumps(payload, ensure_ascii=False)[:400])
            # НЕ теряем оплату молча: зовём админа выдать доступ руками + доточить маппинг.
            await _notify_admin(request.app["bot"],
                "⚠️ Оплата Tribute пришла, но доступ НЕ выдан (не замаплено).\n"
                f"event={event} · tg_id={tg_id or '—'} · code={code or '—'} · amount={amount}\n"
                f"payload={json.dumps(payload, ensure_ascii=False)[:600]}\n"
                "→ Выдай доступ вручную и скажи мне — доточу маппинг.")
            return web.Response(status=200, text="ok")  # 200 — чтобы Tribute не ретраил бесконечно

        # A1 (аудит): идемпотентность — ретрай Tribute (таймаут/5xx) НЕ должен давать
        # второй грант (2 invite-ссылки, 2 welcome). Дедуп по payment_id через bot_meta
        # (покрывает и подписки: там payment_id = subscription_id). Метка ставится
        # ПОСЛЕ успешного гранта — сбой до гранта корректно переиграется ретраем.
        from database import get_meta, set_meta

        # ── Подписочный 1:1: сброс счётчика встреч на полный тариф ─────────────
        # Делаем ДО общего гейта, т.к. общий дедуп по subscription_id мог бы
        # заблокировать помесячный сброс (id подписки стабилен между периодами).
        # Дедуп сброса — период-зависимый (по дате окончания/оплаты периода):
        #   • ретрай ОДНОГО вебхука не восстановит уже потраченную встречу;
        #   • реальное продление следующего периода гарантированно сбросит счётчик.
        # Тариф определяем по сумме: ≈18000 → 3 встречи, иначе → 1 встреча.
        if code == "manifest_1on1" and event in _TRIBUTE_SUB_EVENTS:
            # Тариф: при ПРОДЛЕНИИ берём из БД (сумма в renew-вебхуке может не
            # прийти → иначе тариф «3 встречи» ошибочно сбросился бы до 1). При
            # первой оплате — по сумме (≈18000 → 3, иначе 1).
            _existing = await get_oneonone(tg_id)
            if event != "newSubscription" and _existing and _existing.get("tariff"):
                tariff = int(_existing["tariff"])
            else:
                tariff = 3 if amount >= int(settings.one_on_one_3x_price * 0.9) else 1
            # Период для дедупа сброса. Если Tribute не прислал ни одной date-метки
            # — падаем на месячный бакет (YYYY-MM): ретрай в том же месяце дедупится,
            # а реальное продление в новом месяце ГАРАНТИРОВАННО сбросит счётчик
            # (иначе оплаченный клиент со 2-го месяца молча заперт).
            period = str(payload.get("expires_at") or payload.get("period_id")
                         or payload.get("paid_at") or payload.get("created_at")
                         or datetime.now().strftime("%Y-%m"))
            reset_key = f"1on1reset_{payment_id}_{period}" if payment_id else ""
            if not reset_key or not await get_meta(reset_key):
                await set_oneonone(tg_id, tariff, tariff)
                if reset_key:
                    await set_meta(reset_key, "1")
                logger.info("Tribute 1:1: счётчик сброшен tg=%s тариф=%s встреч",
                            tg_id, tariff)

        # Дедуп: при наличии payment_id — по нему; иначе фолбэк на event+tg+сумму
        # (иначе идемпотентность отключалась бы и ретрай давал 2 доступа).
        _dedup_key = f"pay_{event}_{payment_id}" if payment_id else f"pay_{event}_{tg_id}_{amount}"
        if _dedup_key and await get_meta(_dedup_key):
            logger.info("Tribute: дубль вебхука %s (payment_id=%s) — уже выдано, скип",
                        event, payment_id)
            return web.Response(status=200, text="ok")

        if event in _TRIBUTE_SUB_EVENTS:
            await add_subscription(tg_id, code)
            await _grant_access(request.app["bot"], tg_id, code)
            if _dedup_key:
                await set_meta(_dedup_key, "1")
            logger.info("Tribute: подписка %s выдана %s", code, tg_id)
        elif event in _TRIBUTE_DIGITAL_EVENTS:
            await add_purchase(tg_id, code, amount, payment_id)
            await _grant_access(request.app["bot"], tg_id, code)
            if _dedup_key:
                await set_meta(_dedup_key, "1")
            logger.info("Tribute: продукт %s выдан %s", code, tg_id)
        elif event in _TRIBUTE_CANCEL_EVENTS:
            # Отмена: деактивируем подписку (active=0 + cancelled_at). Доступ в
            # закрытый канал Tribute отзывает сам (channel-gated подписка). Счётчик
            # встреч 1:1 НЕ обнуляем — оплаченный период клиент дорабатывает, а
            # следующий сброс просто не наступит (нет продления; cron сверяет
            # активность). Это оживляет реактивацию club_churn (ушёл → вернём).
            await deactivate_subscription(tg_id, code)
            if _dedup_key:
                await set_meta(_dedup_key, "1")
            logger.info("Tribute: отмена %s у %s — подписка деактивирована", code, tg_id)

        return web.Response(status=200, text="ok")
    except Exception as e:
        logger.exception(f"Tribute webhook error: {e}")
        return web.Response(status=500, text="error")


_CHANNEL_BY_PRODUCT = {
    "manifest_7": "manifest_7_channel_id",
    "manifest_club": "manifest_club_channel_id",
    "manifest_plus": "manifest_plus_channel_id",
    "manifest_1on1": "manifest_1on1_channel_id",
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
        "✅ Подписка на личные встречи оформлена — я держу для тебя место в "
        "расписании.\n\n"
        "Как записаться: нажми /zapis прямо здесь, в этом чате. Я покажу, "
        "сколько встреч осталось у тебя в этом месяце по тарифу, и дам ссылку "
        "на мой календарь — выберешь удобное время. В начале следующего месяца "
        "счётчик снова полный."
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


async def _notify_admin(bot: Bot, text: str):
    """Безопасно уведомить админа (Кая). Никогда не роняет обработку вебхука."""
    if not settings.tg_admin_id:
        return
    try:
        await bot.send_message(settings.tg_admin_id, text)
    except Exception:
        logger.warning("admin notify failed (continuing)", exc_info=True)


async def _grant_access(bot: Bot, tg_id: int, product_code: str):
    """Выдать доступ к продукту после оплаты.

    Для канальных продуктов (manifest_7, manifest_club, manifest_plus) —
    генерируем уникальную одноразовую invite-ссылку с лимитом 1 пользователь
    и сроком 7 дней через Bot API createChatInviteLink.
    """
    text = _BASE_TEXTS.get(product_code, "✅ Оплата получена. Спасибо.")

    # Аналитика воронки: ОПЛАТА — терминальный шаг (без него PostHog слеп на деньгах).
    # Крэш-сейф: телеметрия не должна мешать выдаче доступа.
    try:
        await log_event(tg_id, "payment", product_code)
    except Exception:
        logger.warning("log_event payment failed (continuing)", exc_info=True)

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

    # 1:1 — подписочный: запись идёт через /zapis (счётчик встреч гейтит доступ
    # к календарю), поэтому прямую ссылку тут НЕ даём — только напоминаем команду.
    # (текст про /zapis уже в _BASE_TEXTS["manifest_1on1"].)

    text += "\n\n— Алёна"

    sent = True
    try:
        await bot.send_message(tg_id, text)
    except Exception as e:
        sent = False
        logger.error(f"Failed to send access message to {tg_id}: {e}")

    # Уведомляем админа о КАЖДОЙ продаже (видимость выручки) + ГРОМКО, если доступ
    # не доставлен (юзер оплатил через web и ни разу не жал /start → бот не может писать первым).
    if not sent:
        await _notify_admin(bot,
            f"🔴 Оплата «{product_code}» прошла, но бот НЕ смог написать юзеру tg_id={tg_id} "
            "(скорее всего не жал /start). Доступ НЕ доставлен — напиши ему первым.\n\n"
            f"Что должно было прийти:\n{text}")
    else:
        note = " · нужно назначить созвон" if product_code == "manifest_1on1" else ""
        await _notify_admin(bot, f"✅ Продажа: «{product_code}» · tg_id={tg_id}{note}")


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
