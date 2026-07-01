"""AI-Алёна «мозг v2» (Фаза 1 ядра) — 2-проход: ДИАГНОЗ → ОТВЕТ. На Claude.

Из рефлекс-ответчика (1 вызов/ход) → в мыслящий агент:
  • ПРОХОД 1 — ДИАГНОЗ (Claude Opus 4.8, adaptive thinking). Вход компактный
    (модель клиентки + ~6 последних реплик). Выход — строгий JSON: обновлённая
    модель клиентки + скоринг + фаза метода + директива хода. Человек его НЕ видит.
  • ПРОХОД 2 — ОТВЕТ (Claude Haiku 4.5, голос Алёны). Исполняет директиву её голосом.

Почему Claude, а не Gemini: gemini-2.5-pro упирался в квоту (429). Мозг Гермеса
переведён на Anthropic SDK (решение Кая). Честно: скилы/плагины Claude Code сюда
НЕ переносятся — это чистые вызовы модели; инструменты дал бы Claude Agent SDK.

Всё крэш-сейф: сбой диагноза/парса/сети/ключа → безопасный дефолт-директива,
встреча не падает. Подключается в alena_chat._talk ТОЛЬКО за флагом
settings.brain_v2_enabled. См. docs/hermes/ai-coach-architecture.md.
"""

from __future__ import annotations

import logging

import anthropic

from config import settings
from alena_persona import (
    build_diagnose_prompt, build_response_prompt, parse_diagnose_json,
    METHOD_PHASES,
)

logger = logging.getLogger(__name__)

# Сколько последних реплик отдаём диагнозу (компактный вход → дёшево на Opus).
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

# Ленивый общий async-клиент. Читает ANTHROPIC_API_KEY из окружения (Render env).
# Строим при первом вызове, чтобы импорт модуля не падал, когда ключа нет
# (локально / флаг OFF). Любой сбой конструктора ловится в вызывающих try.
_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()  # ANTHROPIC_API_KEY из env
    return _client


def _default_diagnosis() -> dict:
    """Свежая копия безопасного дефолта (без мутации общего словаря)."""
    d = dict(_SAFE_DIRECTIVE)
    d["client_model"] = {}
    d["score"] = {}
    return d


def _to_claude_messages(history: list[dict]) -> list[dict]:
    """История бота (role 'model'/'user') → messages для Claude.

    Claude требует: роли user/assistant, первая — user, соседние одной роли
    склеиваем. Пустые реплики выкидываем. Ведущие assistant-реплики (Алёна
    открыла встречу) отбрасываем, пока не встретим первую user-реплику."""
    msgs: list[dict] = []
    for m in (history or []):
        text = (m.get("content") or "").strip()
        if not text:
            continue
        role = "assistant" if m.get("role") == "model" else "user"
        if not msgs and role == "assistant":
            continue  # первым сообщением к Claude должен идти user
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n\n" + text  # склеить соседей одной роли
        else:
            msgs.append({"role": role, "content": text})
    return msgs


def _extract_text(message) -> str:
    """Текст из ответа Claude: конкатенация text-блоков (thinking-блоки пропускаем)."""
    return "".join(
        b.text for b in message.content
        if getattr(b, "type", None) == "text"
    ).strip()


async def _call_claude(model: str, system: str, messages: list[dict], *,
                       max_tokens: int, thinking: dict | None = None,
                       temperature: float | None = None,
                       timeout: float = 90.0) -> str:
    """Один вызов Claude messages.create → текст. Может бросить (ловит вызывающий)."""
    if not messages:
        raise RuntimeError("claude: пустая история")
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    if thinking is not None:
        kwargs["thinking"] = thinking          # temperature с thinking не задаём
    elif temperature is not None:
        kwargs["temperature"] = temperature
    message = await _get_client().messages.create(
        **kwargs, timeout=timeout)
    text = _extract_text(message)
    if not text:
        raise RuntimeError(f"claude {model} пустой ответ")
    return text


async def diagnose(history: list[dict], name, archetype,
                   client_model: dict | None) -> dict:
    """ПРОХОД 1 — диагноз (Opus 4.8, adaptive thinking). → dict модели/директивы.

    Крэш-сейф: любой сбой (сеть/ключ/парс/пустой ответ) → безопасный дефолт."""
    try:
        system = build_diagnose_prompt(name, archetype, client_model)
        messages = _to_claude_messages((history or [])[-DIAGNOSE_HISTORY:])
        raw = await _call_claude(
            settings.brain_diagnose_model, system, messages,
            max_tokens=3000, thinking={"type": "adaptive"})
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
    """ПРОХОД 2 — ответ голосом Алёны (Haiku 4.5), исполняет директиву."""
    system = build_response_prompt(name, archetype, directive, method_phase)
    messages = _to_claude_messages(history)
    return await _call_claude(
        settings.brain_respond_model, system, messages,
        max_tokens=1500, temperature=0.9, timeout=60)


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
