"""Pexels: подбор эстетичного вертикального фото-фона для пина (стиль Алёны).

Бесплатный API (нужен PEXELS_API_KEY в env). Возвращает image bytes вертикального
фото по запросу. Запрос строит pin_image.photo_query() из визуал-брифа единицы.
"""

from __future__ import annotations

import os
import logging
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)

PEXELS_KEY = os.getenv("PEXELS_API_KEY", "")


async def fetch_background(query: str, index: int = 0,
                           api_key: str | None = None) -> bytes | None:
    """Найти вертикальное фото по запросу и скачать. None — если нет ключа/результата."""
    key = api_key or PEXELS_KEY
    if not key:
        logger.info("pexels: no PEXELS_API_KEY — skip")
        return None
    url = (f"https://api.pexels.com/v1/search?query={quote(query)}"
           f"&orientation=portrait&size=large&per_page=20")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers={"Authorization": key},
                             timeout=aiohttp.ClientTimeout(total=30)) as r:
                data = await r.json()
        photos = data.get("photos") or []
        if not photos:
            return None
        pick = photos[index % len(photos)]
        src = pick.get("src", {})
        img_url = src.get("large2x") or src.get("large") or src.get("portrait")
        if not img_url:
            return None
        async with aiohttp.ClientSession() as s:
            async with s.get(img_url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                return await r.read()
    except Exception:
        logger.exception("pexels fetch failed for %r", query)
        return None
