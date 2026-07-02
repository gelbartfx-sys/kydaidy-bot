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

import asyncio
import logging

import aiohttp
from aiogram.types import BufferedInputFile

from config import settings

logger = logging.getLogger(__name__)

_SPEECH_URL = "https://api.heygen.com/v3/voices/speech"

# Голосом шлём только «человеческую» длину: короче — странно (одно слово голосом),
# длиннее — голосовое на 3+ минуты никто не слушает. Верх поднят 900→1600 (Кай:
# «снова куча текста» — длинная реплика должна УЙТИ ГОЛОСОМ, а не упасть в текст;
# короткость лечится лимитом токенов в respond, не отказом от голоса).
VOICE_MIN_CHARS = 40
VOICE_MAX_CHARS = 1600


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


# ── Именной видео-кружок Алёны (Ф2): рендер твина по тексту → video_note ──────
_GEN_URL = "https://api.heygen.com/v2/video/generate"
_STATUS_URL = "https://api.heygen.com/v1/video_status.get"
ALENA_AVATAR_ID = "2ab45471e71149b2b07718d17a40fc9b"   # Digital Twin A «Алёна»


async def render_kruzhok(text: str, timeout_min: int = 7) -> bytes | None:
    """Текст → видео твина Алёны (квадрат 720, под video_note). None при сбое.

    Рендер HeyGen ~2–4 мин — вызывать ТОЛЬКО из фоновой задачи, не в такте диалога.
    """
    if not settings.heygen_api_key or not (text or "").strip():
        return None
    headers = {"X-Api-Key": settings.heygen_api_key}
    payload = {
        "video_inputs": [{
            "character": {"type": "avatar", "avatar_id": ALENA_AVATAR_ID},
            "voice": {"type": "text", "input_text": text.strip(),
                      "voice_id": settings.alena_voice_id},
        }],
        "dimension": {"width": 720, "height": 720},
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(_GEN_URL, json=payload, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=30)) as r:
                body = await r.json()
            vid = ((body or {}).get("data") or {}).get("video_id")
            if not vid:
                logger.warning("kruzhok generate: нет video_id: %s", str(body)[:200])
                return None
            for _ in range(timeout_min * 4):          # поллинг каждые ~15с
                await asyncio.sleep(15)
                async with s.get(f"{_STATUS_URL}?video_id={vid}", headers=headers,
                                 timeout=aiohttp.ClientTimeout(total=30)) as r2:
                    st = ((await r2.json()).get("data") or {})
                if st.get("status") == "completed" and st.get("video_url"):
                    async with s.get(st["video_url"],
                                     timeout=aiohttp.ClientTimeout(total=120)) as r3:
                        return await r3.read()
                if st.get("status") == "failed":
                    logger.warning("kruzhok render failed: %s", str(st)[:200])
                    return None
        return None
    except Exception:
        logger.warning("kruzhok render error", exc_info=True)
        return None


async def send_kruzhok_to(bot, chat_id: int, text: str) -> bool:
    """Отрендерить и отправить именной кружок. True — ушёл."""
    data = await render_kruzhok(text)
    if not data:
        return False
    try:
        await bot.send_chat_action(chat_id, "record_video_note")
        await bot.send_video_note(chat_id, BufferedInputFile(data, filename="alena.mp4"))
        return True
    except Exception:
        logger.warning("send_video_note failed", exc_info=True)
        return False


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
