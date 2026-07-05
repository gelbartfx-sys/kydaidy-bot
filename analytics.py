"""PostHog-аналитика воронки — тонкий, КРЭШ-СЕЙФ слой.

Событие шлётся прямым HTTP на PostHog capture API (без SDK-зависимости — тот же
aiohttp, что уже в проекте). Правило №1: аналитика НИКОГДА не роняет поток бота.
- нет ключа (settings.posthog_api_key пуст) → no-op, тихо;
- любая ошибка сети/таймаут → проглатывается, ход клиентки не страдает.

Подключено зеркалом в database.log_event: каждое событие воронки, уже пишущееся
в D1 (funnel_events), дублируется в PostHog с distinct_id=tg_id. В дашборде
PostHog по этим событиям (session_open → offer_shown → subscription_activated …)
строятся воронки с отвалом по шагам.
"""

from __future__ import annotations

import logging

import aiohttp

from config import settings

logger = logging.getLogger(__name__)


async def capture(distinct_id, event: str, props: dict | None = None) -> None:
    """Отправить событие в PostHog. Крэш-сейф: нет ключа/ошибка → тихо ничего."""
    key = (getattr(settings, "posthog_api_key", "") or "").strip()
    if not key or not event:
        return
    host = (getattr(settings, "posthog_host", "") or "https://eu.i.posthog.com").rstrip("/")
    payload = {
        "api_key": key,
        "event": str(event),
        "distinct_id": str(distinct_id),
        "properties": {"source_app": "kydaidy_bot", **(props or {})},
    }
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(f"{host}/capture/", json=payload,
                         timeout=aiohttp.ClientTimeout(total=5))
    except Exception:
        logger.warning("posthog capture failed for event=%s (continuing)", event,
                       exc_info=True)
