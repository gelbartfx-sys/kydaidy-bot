"""Сверка встреч 1:1 с Calendly через polling (Standard-план, 05.07).

Списание встречи — жёсткий кап при выдаче ссылки (booking.py: dec_oneonone +
pending-бронь). Здесь polling сверяет реальность и ВОЗВРАЩАЕТ встречу, если клиент
отменил бронь или так и не записался:
  • новая бронь (utm_content=tg_id) → pending→'booked' + уведомление Алёне с реальным временем;
  • отмена известной брони → возврат встречи (inc_oneonone) → 'canceled' + уведомление;
  • pending старше суток без брони → возврат встречи → 'expired_restored'.

Без токена (env CALENDLY_API_TOKEN пуст) — тик no-op: флоу деградирует к ручному
возврату Алёной (она уведомляется в момент записи). Всё крэш-сейфово: любой сбой
Calendly/сети не роняет планировщик.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from config import settings
from database import (booking_pending_list, booking_by_event, booking_set,
                      booking_get, booking_pending_expired, inc_oneonone)

logger = logging.getLogger(__name__)
_API = "https://api.calendly.com"


async def _get(session, path, params):
    headers = {"Authorization": f"Bearer {settings.calendly_api_token}"}
    try:
        async with session.get(f"{_API}{path}", params=params, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                logger.warning("Calendly GET %s -> %s", path, r.status)
                return None
            return await r.json()
    except Exception:
        logger.warning("Calendly GET %s failed", path, exc_info=True)
        return None


async def _invitee_ids(session, event_uri: str):
    """(tg_id, booking_id) из utm_content/utm_campaign инвайти события."""
    uuid = event_uri.rstrip("/").split("/")[-1]
    data = await _get(session, f"/scheduled_events/{uuid}/invitees", {"count": 10})
    if not data:
        return None, None
    for inv in data.get("collection", []):
        tr = inv.get("tracking") or {}
        tg, bid = tr.get("utm_content"), tr.get("utm_campaign")
        if tg:
            try:
                return int(tg), (int(bid) if bid else None)
            except (TypeError, ValueError):
                return None, None
    return None, None


async def _notify(bot, tg_id: int, what: str, ev: dict | None):
    when = (ev or {}).get("start_time") or ""
    text = f"🗓 1:1 · клиент id {tg_id} {what}." + (f"\nВремя: {when}" if when else "")
    for admin_id in {settings.tg_admin_id, settings.curator_id}:
        if not admin_id:
            continue
        try:
            await bot.send_message(admin_id, text, parse_mode=None)
        except Exception:
            logger.warning("calendly notify failed for %s", admin_id, exc_info=True)


async def reconcile_tick(bot):
    """Один проход сверки. Вызывается планировщиком раз в calendly_poll_min минут."""
    if not settings.calendly_api_token:
        return
    async with aiohttp.ClientSession() as session:
        since = (datetime.now(timezone.utc) - timedelta(days=40)).strftime(
            "%Y-%m-%dT%H:%M:%S.000000Z")
        data = await _get(session, "/scheduled_events", {
            "user": settings.calendly_user_uri, "count": 100,
            "min_start_time": since})
        if data:
            for ev in data.get("collection", []):
                uri, status = ev.get("uri"), ev.get("status")
                if not uri:
                    continue
                known = await booking_by_event(uri)
                if status == "canceled":
                    # Отмена известной брони → вернуть встречу (один раз).
                    if known and known.get("status") == "booked":
                        if await inc_oneonone(known["tg_id"]):
                            await booking_set(known["id"], "canceled")
                            await _notify(bot, known["tg_id"],
                                          "отменил встречу — вернул её в счётчик", ev)
                    continue
                if known:
                    continue  # активная и уже сматченная
                # Новая активная бронь — сматчить с pending по utm.
                tg, bid = await _invitee_ids(session, uri)
                if not tg:
                    continue
                booking = await booking_get(bid) if bid else None
                if not booking:
                    for b in await booking_pending_list():
                        if b.get("tg_id") == tg:
                            booking = b
                            break
                if booking and booking.get("status") == "pending":
                    await booking_set(booking["id"], "booked", event_uri=uri)
                    await _notify(bot, tg, "записался на встречу", ev)

    # Протухшие pending (сутки без брони) → вернуть встречу.
    for b in await booking_pending_expired(24):
        if await inc_oneonone(b["tg_id"]):
            await booking_set(b["id"], "expired_restored")
            await _notify(bot, b["tg_id"],
                          "не записался за сутки — вернул встречу в счётчик", None)
