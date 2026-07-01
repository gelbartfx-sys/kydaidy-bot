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

# ВАЖНО: /v2/user/remaining_quota отдаёт API-квоту (details.api) — у подписочного
# аккаунта она 0. Кредиты ПОДПИСКИ (те самые, что тратят кружки) — в user-profile:
# GET /v3/users/me → remaining_credits (июнь-2026 HeyGen добавил это поле; v1/user/me
# как фолбэк). Нормализуем ÷60 при больших числах (на случай 1/60-единиц).
_ME_URLS = (
    "https://api.heygen.com/v3/users/me",
    "https://api.heygen.com/v1/user/me",
)


async def get_credits() -> int | None:
    """Остаток кредитов ПОДПИСКИ HeyGen. None — нет ключа/сбой.

    Читает remaining_credits из /v3/users/me (фолбэк /v1/user/me), с запасным
    путём к вложенному premium_credits.remaining. Нормализует к «кредитам»."""
    if not settings.heygen_api_key:
        return None
    headers = {"X-Api-Key": settings.heygen_api_key, "accept": "application/json"}
    for url in _ME_URLS:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, headers=headers,
                                 timeout=aiohttp.ClientTimeout(total=20)) as r:
                    body = await r.json()
        except Exception:
            logger.warning("HeyGen %s failed (continuing)", url, exc_info=True)
            continue
        node = body.get("data") if isinstance(body.get("data"), dict) else body
        val = _extract_balance(node)
        if val is None:
            logger.warning("HeyGen %s: баланс не найден в %s", url, str(body)[:200])
            continue
        return int(val / 60) if val > 5000 else int(val)
    return None


def _num(x):
    """int/float из x, иначе None."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _extract_balance(node: dict):
    """Остаток из UserInfoResponse (/v3/users/me) по типу биллинга.

    Подписка: subscription.credits.(premium+add_on).remaining (суммируем).
    Иначе — usage_based.remaining_credits / wallet.remaining_balance /
    плоское remaining_credits. → число | None."""
    if not isinstance(node, dict):
        return None
    sub = node.get("subscription")
    if isinstance(sub, dict):
        creds = sub.get("credits") if isinstance(sub.get("credits"), dict) else {}
        total, found = 0.0, False
        for k in ("premium_credits", "add_on_credits"):
            c = creds.get(k)
            if isinstance(c, dict):
                n = _num(c.get("remaining"))
                if n is not None:
                    total += n
                    found = True
        if found:
            return total
    ub = node.get("usage_based")
    if isinstance(ub, dict):
        n = _num(ub.get("remaining_credits"))
        if n is not None:
            return n
    w = node.get("wallet")
    if isinstance(w, dict):
        n = _num(w.get("remaining_balance"))
        if n is not None:
            return n
    return _num(node.get("remaining_credits"))


def circles_left(credits: int) -> int:
    """Сколько живых кружков ещё можно записать при таком балансе."""
    return credits // CIRCLE_CREDITS if CIRCLE_CREDITS else 0


async def probe() -> str:
    """Диагностика (админ): что реально отдают эндпоинты — статус + сырой ответ.

    Нужна, когда /credits показывает не то: сразу видно, какой эндпоинт жив и
    какие поля в ответе. Ответ обрезаем (в нём только данные аккаунта Кая)."""
    if not settings.heygen_api_key:
        return "HEYGEN_API_KEY не задан"
    headers = {"X-Api-Key": settings.heygen_api_key, "accept": "application/json"}
    lines = []
    for url in _ME_URLS:
        tail = url.split("heygen.com", 1)[-1]
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, headers=headers,
                                 timeout=aiohttp.ClientTimeout(total=20)) as r:
                    status = r.status
                    text = await r.text()
            lines.append(f"{tail} [{status}]: {text[:350]}")
        except Exception as e:
            lines.append(f"{tail} ERR: {e}")
    return "\n".join(lines)


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
