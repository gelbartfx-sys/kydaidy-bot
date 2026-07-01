"""AI-Алёна «мозг v2» (Фаза 1 ядра) — 2-проход: ДИАГНОЗ → ОТВЕТ.

Из рефлекс-ответчика (1 вызов Gemini/ход) → в мыслящий агент:
  • ПРОХОД 1 — ДИАГНОЗ (Gemini pro, thinking вкл). Вход компактный (модель
    клиентки + ~6 последних реплик). Выход — строгий JSON: обновлённая модель
    клиентки + скоринг + фаза метода + директива хода. Человек его НЕ видит.
  • ПРОХОД 2 — ОТВЕТ (Gemini flash, голос Алёны). Исполняет директиву её голосом.

Всё крэш-сейф: сбой диагноза/парса/сети → безопасный дефолт-директива, встреча
не падает. Подключается в alena_chat._talk ТОЛЬКО за флагом settings.brain_v2_enabled.
См. docs/hermes/ai-coach-architecture.md.
"""

from __future__ import annotations

import json
import logging

import aiohttp

from config import settings
from ai_quiz import BASE, TEXT_MODEL
from alena_persona import (
    build_diagnose_prompt, build_response_prompt, parse_diagnose_json,
    METHOD_PHASES,
)

logger = logging.getLogger(__name__)

# Сколько последних реплик отдаём диагнозу (компактный вход → дёшево на pro-модели).
DIAGNOSE_HISTORY = 6

# Безопасный дефолт директивы: при любом сбое диагноза встреча продолжается мягко.
_SAFE_DIRECTIVE = {
    "client_model": {},
    "score": {},
    "method_phase": METHOD_PHASES[0],   # contact
    "directive": "будь рядом, слушай, веди мягко",
    "medium": "text",
    "track": "T2",
}


def _default_diagnosis() -> dict:
    """Свежая копия безопасного дефолта (без мутации общего словаря)."""
    d = dict(_SAFE_DIRECTIVE)
    d["client_model"] = {}
    d["score"] = {}
    return d


def _contents(history: list[dict]) -> list[dict]:
    """История бота (role: 'model'/'user') → формат Gemini contents."""
    return [
        {"role": ("model" if m.get("role") == "model" else "user"),
         "parts": [{"text": m.get("content", "")}]}
        for m in (history or [])
    ]


async def _call_gemini(model: str, system: str, contents: list[dict],
                       *, temperature: float, max_tokens: int,
                       thinking_budget: int, timeout: int = 90) -> str:
    """Один вызов Gemini generateContent → текст ответа (может бросить)."""
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "thinkingConfig": {"thinkingBudget": thinking_budget},
        },
    }
    url = f"{BASE}/models/{model}:generateContent?key={settings.gemini_key}"
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload,
                          timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            body = await r.json()
    if "candidates" not in body:
        raise RuntimeError(f"gemini {model} failed: {json.dumps(body)[:300]}")
    parts = body["candidates"][0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError(f"gemini {model} empty")
    return text


async def diagnose(history: list[dict], name, archetype,
                   client_model: dict | None) -> dict:
    """ПРОХОД 1 — диагноз (Gemini pro, thinking). → dict модели/директивы.

    Крэш-сейф: любой сбой (сеть/парс/пустой ответ) → безопасный дефолт."""
    try:
        system = build_diagnose_prompt(name, archetype, client_model)
        contents = _contents((history or [])[-DIAGNOSE_HISTORY:])
        raw = await _call_gemini(
            settings.gemini_diagnose_model, system, contents,
            temperature=0.4, max_tokens=2048, thinking_budget=1024)
        data = parse_diagnose_json(raw)
    except Exception:
        logger.warning("brain diagnose failed (safe default)", exc_info=True)
        return _default_diagnosis()
    if not isinstance(data, dict):
        return _default_diagnosis()
    # Достраиваем недостающие ключи безопасными дефолтами (модель могла их опустить).
    out = _default_diagnosis()
    if isinstance(data.get("client_model"), dict):
        out["client_model"] = data["client_model"]
    if isinstance(data.get("score"), dict):
        out["score"] = data["score"]
    if data.get("method_phase") in METHOD_PHASES:
        out["method_phase"] = data["method_phase"]
    if isinstance(data.get("directive"), str) and data["directive"].strip():
        out["directive"] = data["directive"].strip()
    if data.get("medium") in ("text", "voice"):
        out["medium"] = data["medium"]
    if isinstance(data.get("track"), str) and data["track"].strip():
        out["track"] = data["track"].strip()
    return out


async def respond(directive: str, method_phase: str, name, archetype,
                  history: list[dict]) -> str:
    """ПРОХОД 2 — ответ голосом Алёны (Gemini flash), исполняет директиву.

    maxOutputTokens как в alena_chat._generate (4096, thinkingBudget 1536)."""
    system = build_response_prompt(name, archetype, directive, method_phase)
    contents = _contents(history)
    return await _call_gemini(
        TEXT_MODEL, system, contents,
        temperature=0.9, max_tokens=4096, thinking_budget=1536, timeout=60)


async def brain_turn(history: list[dict], name, archetype,
                     client_model: dict | None) -> tuple[str, dict]:
    """Полный ход мозга v2: диагноз → ответ.

    → (reply Алёны, обновлённая модель клиентки как dict).
    diagnose() крэш-сейф внутри. respond() может бросить — тогда бросаем наверх,
    вызывающий (_talk) фолбэчит на v1-путь в том же ходе."""
    dx = await diagnose(history, name, archetype, client_model)
    reply = await respond(dx.get("directive"), dx.get("method_phase"),
                          name, archetype, history)

    # Собираем модель клиентки для сохранения: обновление от диагноза + служебка.
    cm = dict(client_model) if isinstance(client_model, dict) else {}
    new_cm = dx.get("client_model")
    if isinstance(new_cm, dict):
        cm.update(new_cm)
    cm["method_phase"] = dx.get("method_phase")
    cm["track"] = dx.get("track")
    return reply, cm
