"""AI-генерация результата теста «Какая Тень в тебе активна».

Два артефакта по архетипу (см. shadow_test.ARCHETYPES):
  • разбор (текст) — tone of voice Алёны, анти-коучинг, юнгианская рамка Тени;
  • «портрет твоей Тени» — moody Poetcore-акварель по фото пользователя.

Бэкенд — Gemini API. Ключ берётся из аргумента api_key или env GEMINI_KEY,
НЕ хардкодится (репо бота публичный — правило #5 регламента).

Локальный тест качества:
    GEMINI_KEY=... python3 bot/ai_quiz.py scripts/alena-face-ref.jpg W [имя]
выведет разбор в консоль и сохранит картинку в bot/_ai_quiz_test/.
"""

from __future__ import annotations

import os
import sys
import base64
import json
import asyncio
import logging

import aiohttp

from shadow_test import ARCHETYPES

logger = logging.getLogger(__name__)

_ENV_KEY = os.environ.get("GEMINI_KEY", "")
# Nano Banana. GA-идентификатор без "-preview" (preview-модели снимают после GA → 404/ошибка).
# Фолбэк-цепочка: если ведущая модель отвалилась, пробуем следующую стабильную.
IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image")
# env-модель пробуем первой, но GA-модели ВСЕГДА в резерве (даже если env указывает на снятый preview).
IMAGE_MODEL_FALLBACKS = list(dict.fromkeys([
    IMAGE_MODEL,
    "gemini-3.1-flash-image",          # Nano Banana 2 GA
    "gemini-3.1-flash-image-preview",  # если у аккаунта ещё preview-доступ
    "gemini-2.5-flash-image",          # Nano Banana 1 (стабильный GA) — последний резерв
]))
TEXT_MODEL = os.environ.get("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
BASE = "https://generativelanguage.googleapis.com/v1beta"


async def _gen_image_payload(payload: dict, api_key: str | None, what: str) -> bytes:
    """Шлёт payload на image-модель с фолбэком по списку моделей. Возвращает image bytes."""
    import aiohttp as _aiohttp
    last_err = ""
    key = api_key or _ENV_KEY
    for model in IMAGE_MODEL_FALLBACKS:
        url = f"{BASE}/models/{model}:generateContent?key={key}"
        try:
            async with _aiohttp.ClientSession() as s:
                async with s.post(url, json=payload,
                                  timeout=_aiohttp.ClientTimeout(total=120)) as r:
                    body = await r.json()
        except Exception as e:                      # сетевой сбой — пробуем след. модель
            last_err = f"{model}: {e}"; continue
        if "candidates" not in body:
            last_err = f"{model}: {json.dumps(body)[:200]}"
            logger.warning("image gen model %s failed: %s", model, last_err)
            continue
        for p in body["candidates"][0].get("content", {}).get("parts", []):
            inline = p.get("inlineData") or p.get("inline_data")
            if inline and str(inline.get("mimeType") or inline.get("mime_type", "")).startswith("image/"):
                if model != IMAGE_MODEL_FALLBACKS[0]:
                    logger.info("image gen: сработала фолбэк-модель %s", model)
                return base64.b64decode(inline["data"])
        last_err = f"{model}: no image in response"
    raise RuntimeError(f"{what} failed on all models: {last_err}")

# --- Жёсткий tone-of-voice блок (из docs/positioning.md) ---
_FORBIDDEN = (
    "«Ты можешь всё», «просто поверь в себя», «5 шагов», «за 90 дней», «позитивный "
    "настрой», «ты — Богиня», «истинная женственность», «найди своего мужчину», "
    "«открой свою энергию», «у тебя точно получится», любые императивы (должна/обязана/"
    "нужно), любая эзотерика (энергия/поток/вибрации/чакры/магия/ритуалы как настоящие), "
    "«часики тикают», «формула», «программа», «лайфкоуч», «коуч по…»"
)

_TONE_SYSTEM = (
    "Ты пишешь от лица Алёны Kyda Idy — она НЕ лайфкоуч и публикуется без ярлыков "
    "(не «коуч», не «психолог», не «ментор»). Анти-коучинговая позиция — фундамент. "
    "Манифест: «Ты можешь только то, что решила что можешь. Себя ты спасаешь сама. "
    "Я рядом, пока ты разбираешься.»\n\n"
    "Контекст: это разбор результата теста про тёмные женские архетипы. Рамка — "
    "ПСИХОЛОГИЧЕСКАЯ (Карл Юнг, Тень: вытесненные, «слишком» части психики), НЕ "
    "мистика и НЕ магия. Архетип — символ подавленной силы, а не диагноз и не приговор.\n\n"
    "Принципы тона:\n"
    "1. Бережно, но не сладко; можно темно и честно.\n"
    "2. Конкретно, не абстрактно.\n"
    "3. Признание боли без жалости.\n"
    "4. БЕЗ обещаний результата — только «возможно», «у некоторых», «твоя дорога».\n"
    "5. БЕЗ императивов — «можешь попробовать», «если захочешь».\n"
    "6. БЕЗ токсичного позитива и БЕЗ эзотерики.\n"
    f"НИКОГДА не используй: {_FORBIDDEN}."
)


async def generate_analysis_text(code: str, name: str | None = None,
                                 api_key: str | None = None) -> str:
    """Персональный разбор Тени (700–1000 знаков) в tone of voice Алёны."""
    a = ARCHETYPES[code]
    who = f"Её зовут {name}. " if name else ""
    prompt = (
        f"{who}Женщина прошла тест про тёмные женские архетипы. Её активная Тень — "
        f"архетип «{a['name']}» ({a['too']}). Девиз этой тени: {a['tag']}.\n"
        f"Суть архетипа: {a['essence']}\n\n"
        "Напиши ей персональный разбор: 700–1000 знаков, 3–4 коротких абзаца, на «ты». "
        "Назови, как эта Тень проявляется в её жизни (желания, страхи, отношения), без "
        "жалости и без диагноза. Покажи, что это не порок, а вытесненная сила, которую "
        "можно вернуть себе — не отыгрывая её вслепую. Без обещаний и без предписаний. "
        "Заверши отдельной строкой «— Алёна». Без заголовков, списков и markdown."
    )
    payload = {
        "systemInstruction": {"parts": [{"text": _TONE_SYSTEM}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.9,
            "maxOutputTokens": 1200,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    url = f"{BASE}/models/{TEXT_MODEL}:generateContent?key={api_key or _ENV_KEY}"
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as r:
            body = await r.json()
    if "candidates" not in body:
        raise RuntimeError(f"text gen failed: {json.dumps(body)[:300]}")
    parts = body["candidates"][0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError(f"text gen empty: {json.dumps(body)[:300]}")
    return text


def _image_prompt(code: str, clean: bool = False) -> str:
    a = ARCHETYPES[code]
    base = (
        "Image 1 is a reference photo of a real woman. Create a SINGLE vertical "
        "illustration (portrait, ~2:3) with HER as the archetype.\n\n"
        "STYLE: atmospheric Poetcore watercolour on dark-toned textured paper. Deep, "
        "moody, earthy palette — charcoal, deep forest green, oxblood/burgundy, muted "
        "gold candle-light, smoky shadows. Hand-painted, soft bleeding edges, visible "
        "paper grain, fine ink linework. A dark literary illustrated page — NOT "
        "photorealistic, NOT glossy, NOT horror/gore, NOT pink/cute. Beautiful and "
        "shadowy, dignified.\n\n"
        "HEROINE: render the woman from Image 1 as a watercolour character — preserve her "
        "recognisable likeness (face shape, hair, features) but fully painted, in shadow "
        "and candle/moonlight. She embodies the archetype with quiet power, never a "
        "glamour or beauty shot.\n\n"
        f"ARCHETYPE — «{a['name']}» ({a['too']}): {a['scene']}. "
        f"Emotional mood: {a['mood']}.\n\n"
    )
    if clean:
        return base + (
            "IMPORTANT: render ONLY the painted illustration — a clean artwork that fills "
            "the whole frame edge to edge. ABSOLUTELY NO text, NO letters, NO words, NO "
            "captions, NO signature, NO frame or border, NO card layout. Just the "
            "watercolour scene of the heroine. No writing of any kind anywhere."
        )
    return base + (
        "This is a keepsake «портрет твоей Тени» card.\n"
        "TEXT rendered ON the card — clean, correct Russian Cyrillic only, elegant, no "
        "spelling errors, NO latin letters, NO gibberish. EXACTLY these four elements, "
        "each appearing ONCE, never duplicated:\n"
        "  • small handwritten, very top centre: «твоя тень»\n"
        f"  • printed archetype name just under it: «{a['name']}»\n"
        f"  • ONE handwritten phrase, in the LOWER THIRD only: {a['tag']}\n"
        "  • bottom signature line: «— Алёна Kyda Idy» — keep «Kyda Idy» in LATIN "
        "letters exactly (it is a brand surname), only «Алёна» in Cyrillic\n"
        "Do NOT repeat any line. Place the phrase only in the lower third.\n\n"
        "COMPOSITION: keepsake card — illustration + elegant integrated typographic "
        "labels, dark margins, a thin hand-drawn ink frame. Tasteful, cinematic, calm. "
        "ABSOLUTELY NO latin gibberish — only clean readable Cyrillic."
    )


async def generate_hero_image(photo_bytes: bytes, code: str, clean: bool = False,
                              mime: str = "image/jpeg", api_key: str | None = None) -> bytes:
    """«Портрет Тени» по фото пользователя (Nano Banana). Возвращает image bytes."""
    parts = [
        {"inlineData": {"mimeType": mime, "data": base64.b64encode(photo_bytes).decode()}},
        {"text": _image_prompt(code, clean=clean)},
    ]
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    return await _gen_image_payload(payload, api_key, "hero image gen")


async def generate_background_image(prompt: str, api_key: str | None = None) -> bytes:
    """Атмосферный вертикальный ФОН для пина (Nano Banana, без входного фото).

    prompt — готовый текст-промпт сцены. Возвращает image bytes. Текст на фоне
    НЕ рисуем (его накладывает pin_image поверх) — просим чистую сцену без букв."""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    return await _gen_image_payload(payload, api_key, "bg image gen")


# --------------------------------------------------------------------------- CLI
async def _cli():
    if len(sys.argv) < 3:
        print("usage: python3 bot/ai_quiz.py <photo_path> <archetype code> [name]")
        print("codes:", ", ".join(f"{k}={v['name']}" for k, v in ARCHETYPES.items()))
        sys.exit(1)
    if not _ENV_KEY:
        print("ERROR: set GEMINI_KEY env var")
        sys.exit(1)

    photo_path, code = sys.argv[1], sys.argv[2].upper()
    name = sys.argv[3] if len(sys.argv) > 3 else None
    if code not in ARCHETYPES:
        print(f"unknown code {code}; valid: {', '.join(ARCHETYPES)}")
        sys.exit(1)

    from PIL import Image
    import io
    img = Image.open(photo_path).convert("RGB")
    img.thumbnail((1024, 1024))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    photo_bytes = buf.getvalue()
    print(f"photo {len(photo_bytes)//1024} KB · Тень: {ARCHETYPES[code]['name']} ({code})\n")

    out_dir = os.path.join(os.path.dirname(__file__), "_ai_quiz_test")
    os.makedirs(out_dir, exist_ok=True)

    text_task = asyncio.create_task(generate_analysis_text(code, name))
    img_task = asyncio.create_task(generate_hero_image(photo_bytes, code))

    text = await text_task
    print("=" * 60, "\nРАЗБОР:\n", text, "\n", "=" * 60, sep="")
    with open(os.path.join(out_dir, f"shadow_{code}_text.txt"), "w") as f:
        f.write(text)

    image = await img_task
    img_path = os.path.join(out_dir, f"shadow_{code}_portrait.png")
    with open(img_path, "wb") as f:
        f.write(image)
    print(f"\nкартинка: {img_path} ({len(image)//1024} KB)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_cli())
