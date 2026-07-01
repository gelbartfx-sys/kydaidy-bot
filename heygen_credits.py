"""HeyGen кредит-монитор — Кай узнаёт о кредитах ЗАРАНЕЕ (жёсткая фиксация).

Живые кружки коуча тратят HeyGen-кредиты (голос — бесплатный). Чтобы воронка
никогда не встала молча, бот:
  • периодически (credit_check_hours) смотрит баланс HeyGen и ПИШЕТ Каю в Telegram,
    когда кредиты на исходе (порог credit_warn / credit_urgent);
  • отдаёт баланс по команде /credits (админ).
Один алерт на уровень в день (bot_meta), чтобы не спамить. Всё крэш-сейф: сбой
API/сети → тихо, воронка не падает. Спит, пока не задан HEYGEN_API_KEY.
См. docs/hermes/credit-alerts-SPEC.md.
"""

from __future__ import annotations

import datetime
import logging

import aiohttp

from config import settings
from database import get_meta, set_meta
from lead_policy import CIRCLE_CREDITS

logger = logging.getLogger(__name__)

_QUOTA_URL = "https://api.heygen.com/v2/user/remaining_quota"


async def get_credits() -> int | None:
    """Остаток HeyGen-кредитов (нормализовано). None — нет ключа/сбой.

    HeyGen может отдавать remaining_quota в 1/60-кредита — нормализуем к кредитам
    (÷60 при больших числах), чтобы порог был в понятных «кредитах»."""
    if not settings.heygen_api_key:
        return None
    try:
        headers = {"X-Api-Key": settings.heygen_api_key, "accept": "application/json"}
        async with aiohttp.ClientSession() as s:
            async with s.get(_QUOTA_URL, headers=headers,
                             timeout=aiohttp.ClientTimeout(total=20)) as r:
                body = await r.json()
    except Exception:
        logger.warning("HeyGen quota request failed (continuing)", exc_info=True)
        return None
    raw = (body.get("data") or {}).get("remaining_quota")
    if raw is None:
        logger.warning("HeyGen quota: no remaining_quota in %s", str(body)[:200])
        return None
    try:
        raw = float(raw)
    except (TypeError, ValueError):
        return None
    return int(raw / 60) if raw > 5000 else int(raw)


def circles_left(credits: int) -> int:
    """Сколько живых кружков ещё можно записать при таком балансе."""
    return credits // CIRCLE_CREDITS if CIRCLE_CREDITS else 0


def _alert_text(credits: int, urgent: bool) -> str:
    head = "🔴 HeyGen: кредиты почти кончились" if urgent else "🟡 HeyGen: кредиты на исходе"
    return (
        f"{head}\n\n"
        f"Осталось: {credits} кред ≈ {circles_left(credits)} живых кружков.\n"
        f"Голос Алёны — бесплатный, он не встанет. Встанут только именные видео-кружки.\n\n"
        f"Докупить пак (2 клика): app.heygen.com → Settings → Plan & Billing → "
        f"Premium Credit Pack (300 кред / $15).\n"
        f"Проверить баланс в любой момент: /credits"
    )


async def run_credit_check(bot) -> None:
    """Джоб: смотрит баланс, при низком — шлёт Каю алерт (раз в день на уровень)."""
    if not settings.heygen_api_key or not settings.tg_admin_id:
        return
    credits = await get_credits()
    if credits is None:
        return
    if credits <= settings.credit_urgent:
        level, urgent = "urgent", True
    elif credits <= settings.credit_warn:
        level, urgent = "warn", False
    else:
        return
    today = datetime.date.today().isoformat()
    mkey = f"credit_alert_{level}"
    try:
        if await get_meta(mkey) == today:
            return  # уже алертили сегодня на этом уровне
        await set_meta(mkey, today)
        await bot.send_message(settings.tg_admin_id, _alert_text(credits, urgent),
                               parse_mode=None)
    except Exception:
        logger.warning("credit alert send failed (continuing)", exc_info=True)
