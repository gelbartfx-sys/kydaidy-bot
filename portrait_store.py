"""Эфемерное хранилище сгенерированных портретов Тени.

Бот генерит портрет по фото и кладёт сюда; aiohttp-роут /p/{token} отдаёт его
странице kydaidy.com/profile (для геро-слота). In-memory: переживать рестарт
Render free не требуется — если потеряется, профиль покажет плейсхолдер.
"""

from __future__ import annotations

import time
import secrets

_TTL = 3600          # портрет живёт 1 час
_MAX = 300           # мягкий потолок записей
_store: dict[str, tuple[bytes, float]] = {}


def put(data: bytes) -> str:
    """Сохранить картинку, вернуть короткий токен."""
    _evict()
    token = secrets.token_urlsafe(9)
    _store[token] = (data, time.time() + _TTL)
    return token


def get(token: str) -> bytes | None:
    rec = _store.get(token)
    if not rec:
        return None
    data, exp = rec
    if time.time() > exp:
        _store.pop(token, None)
        return None
    return data


def _evict() -> None:
    now = time.time()
    expired = [k for k, (_, exp) in _store.items() if exp < now]
    for k in expired:
        _store.pop(k, None)
    if len(_store) > _MAX:
        for k in list(_store)[: len(_store) - _MAX]:
            _store.pop(k, None)
