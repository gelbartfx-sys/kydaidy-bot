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
import os
import tempfile

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

# Экономика касаний (мандат Кая 03.07): на клиента — до 12 голосовых и до 2
# видео-кружков; текст безлимитен. Голос остаётся дефолтом ДО исчерпания квоты.
VOICE_CAP_PER_CLIENT = 12
VIDEO_NOTES_CAP_PER_CLIENT = 2


def voice_fits(text: str) -> bool:
    t = (text or "").strip()
    return VOICE_MIN_CHARS <= len(t) <= VOICE_MAX_CHARS


def _quota_exempt(chat_id: int) -> bool:
    """Whitelist-тестеры (Кай и др.) — вне квот. Late import: без цикла."""
    try:
        from handlers import SHADOW_UNLIMITED_IDS
        return chat_id in SHADOW_UNLIMITED_IDS
    except Exception:
        return False


async def voice_quota_ok(chat_id: int) -> bool:
    """True — голос ещё можно; False — квота 12 исчерпана, вызывающий шлёт текст.
    Крэш-сейф: сомнение трактуем в пользу голоса (регламент канала важнее квоты)."""
    if _quota_exempt(chat_id):
        return True
    try:
        from database import events_count_total
        return (await events_count_total(chat_id, ("voice_sent",))) < VOICE_CAP_PER_CLIENT
    except Exception:
        return True


async def video_quota_ok(chat_id: int) -> bool:
    """True — кружок ещё можно (лимит 2: кружок Тени + именной оффер)."""
    if _quota_exempt(chat_id):
        return True
    try:
        from database import events_count_total
        return (await events_count_total(
            chat_id, ("kruzhok_sent", "video_note_sent"))) < VIDEO_NOTES_CAP_PER_CLIENT
    except Exception:
        return True


async def _mark_voice_sent(chat_id: int, text: str):
    try:
        from database import log_event
        await log_event(chat_id, "voice_sent", str(len(text)))
    except Exception:
        pass


async def tts_bytes(text: str) -> bytes | None:
    """Текст → байты аудио голосом Алёны. None при любом сбое (фолбэк на текст).

    РЕТРАЙ (05.07): единичный транзиентный сбой /v3/voices/speech (таймаут,
    HTTP 5xx/429, пустой ответ — например при пиковой нагрузке на HeyGen от
    параллельной генерации контента) НЕ должен сразу ронять реплику в текст.
    Пробуем до 3 раз с короткой паузой — голос остаётся дефолтом канала."""
    if not settings.heygen_api_key or not (text or "").strip():
        return None
    payload = {"text": text.strip(), "voice_id": settings.alena_voice_id}
    headers = {"X-Api-Key": settings.heygen_api_key}
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(_SPEECH_URL, json=payload, headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=40)) as r:
                    status = r.status
                    body = await r.json()
                audio_url = ((body or {}).get("data") or {}).get("audio_url") \
                    or (body or {}).get("audio_url")
                if not audio_url:
                    logger.warning("heygen speech попытка %s: нет audio_url (HTTP %s): %s",
                                   attempt + 1, status, str(body)[:160])
                    # Диагностика в D1 (05.07): видеть ТОЧНУЮ причину сбоя TTS в проде
                    # (401=ключ, 429=лимит, 402=кредиты) — Render-логи недоступны.
                    try:
                        from database import log_event
                        await log_event(0, "tts_debug",
                                        f"HTTP {status} · {str(body)[:120]}")
                    except Exception:
                        pass
                    raise ValueError("no audio_url")
                async with s.get(audio_url,
                                 timeout=aiohttp.ClientTimeout(total=40)) as r2:
                    data = await r2.read()
            if data:
                return data
            raise ValueError("empty audio")
        except Exception:
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            logger.warning("heygen tts failed после 3 попыток (fallback to text)",
                           exc_info=True)
            return None
    return None


# ── ffmpeg-слой (04.07, фидбек Кая): волна у голосовых + темп 1.1 + честный кружок ──
# Telegram рисует осциллограмму ТОЛЬКО у voice в OGG/Opus — HeyGen отдаёт MP3,
# поэтому часть клиентов показывала плоскую полоску. Перекодируем каждый голосовой
# в OGG/Opus (заодно atempo из settings.voice_tempo). Любой сбой/нет ffmpeg →
# шлём исходный MP3 как раньше: слой — усилитель, не точка отказа.

_FFMPEG_MISSING_REPORTED = False  # телеметрия ffmpeg_missing — один раз за процесс

async def _run_ffmpeg(cmd: list[str], stdin_data: bytes | None = None,
                      timeout: int = 90) -> bytes | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(stdin_data), timeout=timeout)
        if proc.returncode != 0:
            logger.warning("ffmpeg rc=%s: %s", proc.returncode,
                           (err or b"")[-300:].decode(errors="replace"))
            return None
        return out
    except Exception:
        logger.warning("ffmpeg недоступен/упал (шлём исходник)", exc_info=True)
        global _FFMPEG_MISSING_REPORTED
        if not _FFMPEG_MISSING_REPORTED:
            _FFMPEG_MISSING_REPORTED = True
            try:
                from database import log_event
                await log_event(0, "ffmpeg_missing", cmd[0])
            except Exception:
                pass
        return None


async def _to_voice_ogg(mp3: bytes) -> bytes | None:
    """MP3 → OGG/Opus моно 48к с темпом voice_tempo. None → вызывающий шлёт MP3."""
    tempo = float(getattr(settings, "voice_tempo", 1.0) or 1.0)
    af = f"atempo={tempo:.2f}" if abs(tempo - 1.0) >= 0.01 else "anull"
    out = await _run_ffmpeg(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-af", af, "-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1",
         "-f", "ogg", "pipe:1"], stdin_data=mp3)
    return out or None


async def _voice_file(text_audio: bytes) -> BufferedInputFile:
    ogg = await _to_voice_ogg(text_audio)
    if ogg:
        return BufferedInputFile(ogg, filename="alena.ogg")
    return BufferedInputFile(text_audio, filename="alena.mp3")


async def _probe_duration(path: str) -> int | None:
    out = await _run_ffmpeg(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path])
    try:
        return max(1, int(float((out or b"").decode().strip())))
    except Exception:
        return None


async def _to_video_note(mp4: bytes) -> tuple[bytes, int, int | None]:
    """Рендер твина → честный video_note: 640×640, faststart, темп voice_tempo
    (видео+звук вместе — синхрон губ сохраняется, заодно короче пинг-понг похода
    аватара). Возвращает (байты, length, duration|None); сбой → исходник 720."""
    tempo = float(getattr(settings, "voice_tempo", 1.0) or 1.0)
    src = dst = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(mp4)
            src = f.name
        dst = src.replace(".mp4", "_vn.mp4")
        if abs(tempo - 1.0) >= 0.01:
            fc = (f"[0:v]setpts=PTS/{tempo:.2f},scale=640:640[v];"
                  f"[0:a]atempo={tempo:.2f}[a]")
        else:
            fc = "[0:v]scale=640:640[v];[0:a]anull[a]"
        ok = await _run_ffmpeg(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
             "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", dst],
            timeout=240)
        if ok is None or not os.path.exists(dst) or os.path.getsize(dst) == 0:
            return mp4, 720, None
        dur = await _probe_duration(dst)
        with open(dst, "rb") as f:
            return f.read(), 640, dur
    except Exception:
        logger.warning("video_note transcode failed (шлём исходник)", exc_info=True)
        return mp4, 720, None
    finally:
        for p in (src, dst):
            try:
                if p and os.path.exists(p):
                    os.unlink(p)
            except Exception:
                pass


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
    """Отрендерить и отправить именной кружок. True — ушёл.
    Гейт квоты — ДО рендера (кредиты HeyGen не тратим на исчерпавшего лимит)."""
    if not await video_quota_ok(chat_id):
        return False  # квота 2 кружков исчерпана → вызывающий шлёт голос/текст
    data = await render_kruzhok(text)
    if not data:
        return False
    try:
        await bot.send_chat_action(chat_id, "record_video_note")
        # 04.07 (фидбек Кая «оффер пришёл не кружком»): Telegram надёжно рисует
        # круглое сообщение только при квадрате ≤640 + явных length/duration.
        vn, length, duration = await _to_video_note(data)
        await bot.send_video_note(
            chat_id, BufferedInputFile(vn, filename="alena.mp4"),
            length=length, duration=duration)
        try:
            from database import log_event
            await log_event(chat_id, "video_note_sent", "offer")
        except Exception:
            pass
        return True
    except Exception:
        logger.warning("send_video_note failed", exc_info=True)
        return False


async def send_voice_to(bot, chat_id: int, text: str, reply_markup=None) -> bool:
    """То же, что send_voice_reply, но из фонового джоба (нет Message — только bot).
    True — ушло голосом; False — вызывающий шлёт текст."""
    if not settings.voice_replies_enabled or not voice_fits(text):
        return False
    if not await voice_quota_ok(chat_id):
        return False  # квота 12 голосовых исчерпана → вызывающий шлёт текст
    try:
        await bot.send_chat_action(chat_id, "record_voice")
    except Exception:
        pass
    audio = await tts_bytes(text)
    if not audio:
        return False
    try:
        await bot.send_voice(chat_id, await _voice_file(audio),
                             reply_markup=reply_markup)
        await _mark_voice_sent(chat_id, text)
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
    if not await voice_quota_ok(message.chat.id):
        return False  # квота 12 голосовых исчерпана → вызывающий шлёт текст
    try:
        await message.bot.send_chat_action(message.chat.id, "record_voice")
    except Exception:
        pass
    audio = await tts_bytes(text)
    if not audio:
        return False
    try:
        await message.answer_voice(
            await _voice_file(audio),
            reply_markup=reply_markup)
        await _mark_voice_sent(message.chat.id, text)
        return True
    except Exception:
        logger.warning("send_voice failed (fallback to text)", exc_info=True)
        return False
