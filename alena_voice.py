"""Голос Алёны в диалоге (Волна 1: H1/H3 — присутствие).

Ядро (tts_bytes) не знает про Telegram — при переезде коуча в приложение
переносится как есть, меняется только слой доставки (send_voice_reply).

TTS = HeyGen `POST /v3/voices/speech` (голос Digital Twin Алёны):
синхронный ответ {audio_url, duration}, аудио-TTS не тратит видео-кредиты.
Ключ — settings.heygen_api_key (уже в Render для кредит-монитора).

Всё крэш-сейф: любой сбой (нет ключа / сеть / кривой ответ) → None/False,
вызывающий шлёт тот же ответ текстом. Голос — усилитель, не точка отказа.
"""

from __future__ import annotations

import logging

import aiohttp
from aiogram.types import BufferedInputFile

from config import settings

logger = logging.getLogger(__name__)

_SPEECH_URL = "https://api.heygen.com/v3/voices/speech"

# Голосом шлём только «человеческую» длину: короче — странно (одно слово голосом),
# длиннее — HeyGen дольше генерит и голосовое на 3+ минуты никто не слушает.
VOICE_MIN_CHARS = 40
VOICE_MAX_CHARS = 900


def voice_fits(text: str) -> bool:
    t = (text or "").strip()
    return VOICE_MIN_CHARS <= len(t) <= VOICE_MAX_CHARS


async def tts_bytes(text: str) -> bytes | None:
    """Текст → байты аудио голосом Алёны. None при любом сбое (фолбэк на текст)."""
    if not settings.heygen_api_key or not (text or "").strip():
        return None
    try:
        payload = {"text": text.strip(), "voice_id": settings.alena_voice_id}
        headers = {"X-Api-Key": settings.heygen_api_key}
        async with aiohttp.ClientSession() as s:
            async with s.post(_SPEECH_URL, json=payload, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=45)) as r:
                body = await r.json()
            audio_url = ((body or {}).get("data") or {}).get("audio_url") \
                or (body or {}).get("audio_url")
            if not audio_url:
                logger.warning("heygen speech: нет audio_url в ответе: %s",
                               str(body)[:200])
                return None
            async with s.get(audio_url,
                             timeout=aiohttp.ClientTimeout(total=45)) as r2:
                data = await r2.read()
        return data if data else None
    except Exception:
        logger.warning("heygen tts failed (fallback to text)", exc_info=True)
        return None


async def send_voice_to(bot, chat_id: int, text: str, reply_markup=None) -> bool:
    """То же, что send_voice_reply, но из фонового джоба (нет Message — только bot).
    True — ушло голосом; False — вызывающий шлёт текст."""
    if not settings.voice_replies_enabled or not voice_fits(text):
        return False
    try:
        await bot.send_chat_action(chat_id, "record_voice")
    except Exception:
        pass
    audio = await tts_bytes(text)
    if not audio:
        return False
    try:
        await bot.send_voice(chat_id, BufferedInputFile(audio, filename="alena.mp3"),
                             reply_markup=reply_markup)
        return True
    except Exception:
        logger.warning("send_voice_to failed (fallback to text)", exc_info=True)
        return False


async def send_voice_reply(message, text: str, reply_markup=None) -> bool:
    """Отправить text ГОЛОСОВЫМ Алёны. True — ушло голосом, False — шли текстом.

    Пока генерится — индикатор «записывает голосовое…» (хореография присутствия:
    заодно маскирует латентность TTS как естественную паузу записи).
    """
    if not settings.voice_replies_enabled or not voice_fits(text):
        return False
    try:
        await message.bot.send_chat_action(message.chat.id, "record_voice")
    except Exception:
        pass
    audio = await tts_bytes(text)
    if not audio:
        return False
    try:
        await message.answer_voice(
            BufferedInputFile(audio, filename="alena.mp3"),
            reply_markup=reply_markup)
        return True
    except Exception:
        logger.warning("send_voice failed (fallback to text)", exc_info=True)
        return False
