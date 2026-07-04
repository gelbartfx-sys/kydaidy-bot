"""Провайдер-агностичный каскад мозга (критичный рефактор 04.07, мандат Кая:
«воронка не ломается НИ ПРИ КАКИХ обстоятельствах» — «аналоги аналоги аналоги»).

Аудит по коду+данным D1 нашёл: фолбэк мозга v2 (Anthropic) на v1 шёл на Gemini
по СЛАБОМУ промпту SESSION_ARC без защит (build_system — без ANTI_HALLUCINATION)
→ он и ломал (галлюцинации/перепрыжка, brain_fail ×9). Плюс единственный
отказавший провайдер (кейс «temperature 400» 03.07: Anthropic задепрекейтил
параметр — 400 на КАЖДОМ ходе) ронял диагноз/ответ целиком.

Фикс: единый упорядоченный список слоёв, ОБЩИЙ для diagnose и respond, — оба
идут по нему сверху вниз, переходя к следующему при отказе слоя:
  1. anthropic (основная модель, settings.brain_diagnose_model/brain_respond_model)
  2. anthropic (запасная модель, settings.brain_*_model_alt — переживает
     деприкейт/400 КОНКРЕТНОЙ модели, тот же провайдер)
  3. gemini (settings.gemini_key) — ДРУГОЙ провайдер, под ТЕМ ЖЕ system-контрактом
     (build_diagnose_prompt/build_response_prompt из alena_persona), а не старым
     слабым SESSION_ARC — это и убивает корень галлюцинаций у фолбэка.
  4. openai (env OPENAI_API_KEY, ленивый импорт пакета `openai`) — слот, тихо
     пропускается без ключа.
  5. groq ИЛИ mistral (env GROQ_API_KEY/MISTRAL_API_KEY, тот же ленивый клиент
     OpenAI-совместимого API) — ОДИН слот, Groq первым при обоих ключах; тихо
     пропускается без обоих ключей.
  6. static — БЕЗОПАСНЫЙ СТАТИЧНЫЙ СЛОЙ (alena_persona.static_safe_reply /
     alena_brain._default_diagnosis): без сети, никогда не падает, никогда не
     выдумывает — короткий валидный ход по фазе метода, держит встречу живой,
     даже если ВСЕ сети легли разом.

Ретраи: 2 попытки на транзиентных ошибках (429/5xx/timeout) с экспон. бэкоффом
внутри ОДНОГО слоя; на 400/401/403 (или невалидном ответе — не прошёл validate)
— СРАЗУ failover, без ретрая (ретраить «сломанный контракт» бессмысленно).

Телеметрия (funnel_events, чтобы деградация была ВИДНА в D1, а не тихой):
  brain_layer=<purpose>:<layer>          — какой слой обслужил ход
  brain_failover=<purpose>:<from>-><to>  — каждый переход между слоями

BRAIN_DISABLE=anthropic,gemini,... (settings.brain_disable) — форс-отключение
слоёв ПО ПРОВАЙДЕРУ (не по конкретной модели) для теста неубиваемости воронки:
прогнать «упали все верхние» и убедиться, что нижние слои/static держат ход.
static отключить нельзя — это последний рубеж.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Callable, TypeVar

import aiohttp

from config import settings
from ai_quiz import BASE as _GEMINI_BASE, TEXT_MODEL as _GEMINI_MODEL

logger = logging.getLogger(__name__)

T = TypeVar("T")

_RETRY_ATTEMPTS = 2          # 1 попытка + 1 ретрай на транзиентных, потом failover
_RETRY_BASE_DELAY = 0.6      # секунды, экспон. бэкофф: 0.6, 1.2 (+джиттер)
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


class _HTTPStatusError(RuntimeError):
    """HTTP-ошибка провайдера без своего SDK-исключения (Gemini REST) — несёт
    status_code, чтобы _is_transient() классифицировал её как любую другую."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


# ── Классификация ошибок: ретраить транзиентную, сразу failover на остальном ──

def _status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    if resp is not None:
        v = getattr(resp, "status_code", None)
        if isinstance(v, int):
            return v
    return None


def _is_transient(exc: Exception) -> bool:
    code = _status_code(exc)
    if code is not None:
        return code in _TRANSIENT_STATUS
    name = type(exc).__name__.lower()
    return any(k in name for k in ("timeout", "connect", "overloaded", "temporar"))


async def _with_retry(call, system: str, history: list[dict], **kwargs):
    """До 2 попыток на транзиентных (429/5xx/timeout) с бэкоффом; на прочих
    (400/401/403, парс/валидация) — сразу бросает, каскад идёт к следующему слою."""
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return await call(system, history, **kwargs)
        except Exception as e:
            last_exc = e
            if not _is_transient(e) or attempt == _RETRY_ATTEMPTS - 1:
                raise
            delay = _RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.2)
            logger.info("brain cascade: транзиентная ошибка %s — ретрай через %.1fс",
                       type(e).__name__, delay)
            await asyncio.sleep(delay)
    raise last_exc  # pragma: no cover — недостижимо, страховка типов


# ── Конвертация «родной» истории бота (role 'model'/'user') под провайдера ────

def _to_role_messages(history: list[dict]) -> list[dict]:
    """История бота → messages в стиле Anthropic/OpenAI (роли user/assistant).

    Первая роль должна быть user, соседей одной роли склеиваем, пустые режем —
    так требуют оба SDK (Claude — жёстко, OpenAI — терпимее, но хуже не будет)."""
    msgs: list[dict] = []
    for m in (history or []):
        text = (m.get("content") or "").strip()
        if not text:
            continue
        role = "assistant" if m.get("role") == "model" else "user"
        if not msgs and role == "assistant":
            continue
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n\n" + text
        else:
            msgs.append({"role": role, "content": text})
    return msgs


def _to_gemini_contents(history: list[dict]) -> list[dict]:
    """История бота → contents Gemini (роли 'model'/'user' — уже родные)."""
    out = []
    for m in (history or []):
        text = (m.get("content") or "").strip()
        if not text:
            continue
        role = "model" if m.get("role") == "model" else "user"
        out.append({"role": role, "parts": [{"text": text}]})
    return out


# ── Anthropic: 2 слоя (основная + запасная модель), общий ленивый клиент ─────

_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic  # ленивый импорт: на Render пакет есть, локально не нужен
        _anthropic_client = anthropic.AsyncAnthropic()  # ANTHROPIC_API_KEY из env
    return _anthropic_client


def _make_anthropic_call(model: str, thinking: dict | None):
    async def _call(system: str, history: list[dict], *, max_tokens: int, timeout: float):
        messages = _to_role_messages(history)
        if not messages:
            raise RuntimeError(f"anthropic {model}: пустая история")
        kwargs = {"model": model, "max_tokens": max_tokens, "system": system,
                 "messages": messages}
        if thinking is not None:
            kwargs["thinking"] = thinking  # temperature с thinking не задаём
        client = _get_anthropic_client()
        message = await client.messages.create(**kwargs, timeout=timeout)
        text = "".join(
            b.text for b in message.content if getattr(b, "type", None) == "text"
        ).strip()
        if not text:
            raise RuntimeError(f"anthropic {model}: пустой ответ")
        return text
    return _call


# ── Gemini: REST (без SDK, как в alena_chat._generate) ────────────────────────

def _make_gemini_call(model: str):
    async def _call(system: str, history: list[dict], *, max_tokens: int, timeout: float):
        if not settings.gemini_key:
            raise RuntimeError("gemini: нет ключа (settings.gemini_key)")
        contents = _to_gemini_contents(history)
        if not contents:
            raise RuntimeError(f"gemini {model}: пустая история")
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {
                "temperature": 0.7,
                # maxOutputTokens ВКЛЮЧАЕТ токены мышления → должен быть заметно
                # больше thinkingBudget, иначе видимый ответ обрывается на полуслове.
                "maxOutputTokens": max(max_tokens * 2, 1024),
                "thinkingConfig": {"thinkingBudget": min(768, max_tokens)},
            },
        }
        url = f"{_GEMINI_BASE}/models/{model}:generateContent?key={settings.gemini_key}"
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload,
                              timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status >= 400:
                    body_text = await r.text()
                    raise _HTTPStatusError(
                        r.status, f"gemini {model}: HTTP {r.status} {body_text[:200]}")
                body = await r.json()
        if "candidates" not in body:
            raise RuntimeError(f"gemini {model}: {json.dumps(body)[:300]}")
        parts = body["candidates"][0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            raise RuntimeError(f"gemini {model}: пустой ответ")
        return text
    return _call


# ── OpenAI-совместимые (OpenAI/Groq/Mistral — одна реализация, три base_url) ──

_oai_clients: dict[str, object] = {}


def _openai_client_for(base_url: str, api_key: str):
    cache_key = f"{base_url}|{api_key[:8]}"
    client = _oai_clients.get(cache_key)
    if client is None:
        from openai import AsyncOpenAI  # ленивый импорт: нужен только активным слотам
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        _oai_clients[cache_key] = client
    return client


def _make_openai_compatible_call(base_url: str, api_key: str, model: str):
    async def _call(system: str, history: list[dict], *, max_tokens: int, timeout: float):
        messages = [{"role": "system", "content": system}] + _to_role_messages(history)
        if len(messages) <= 1:
            raise RuntimeError(f"{model}: пустая история")
        client = _openai_client_for(base_url, api_key)
        resp = await client.chat.completions.create(
            model=model, messages=messages, max_tokens=max_tokens, timeout=timeout)
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            raise RuntimeError(f"{model}: пустой ответ")
        return text
    return _call


def _openai_layers() -> list[tuple[str, Callable]]:
    key = (settings.openai_api_key or "").strip()
    if not key:
        return []  # слот тихо выключен — ключа нет
    model = settings.brain_openai_model
    return [(f"openai:{model}",
            _make_openai_compatible_call("https://api.openai.com/v1", key, model))]


def _groq_or_mistral_layers() -> list[tuple[str, Callable]]:
    groq_key = (settings.groq_api_key or "").strip()
    if groq_key:
        model = settings.brain_groq_model
        return [(f"groq:{model}",
                _make_openai_compatible_call("https://api.groq.com/openai/v1", groq_key, model))]
    mistral_key = (settings.mistral_api_key or "").strip()
    if mistral_key:
        model = settings.brain_mistral_model
        return [(f"mistral:{model}",
                _make_openai_compatible_call("https://api.mistral.ai/v1", mistral_key, model))]
    return []  # ни один ключ не задан — слот тихо выключен


# ── Сборка упорядоченного списка слоёв по назначению (diagnose/respond) ──────

def _layers_diagnose() -> list[tuple[str, Callable]]:
    layers = [
        (f"anthropic:{settings.brain_diagnose_model}",
         _make_anthropic_call(settings.brain_diagnose_model, thinking={"type": "adaptive"})),
        (f"anthropic:{settings.brain_diagnose_model_alt}",
         _make_anthropic_call(settings.brain_diagnose_model_alt, thinking={"type": "adaptive"})),
        (f"gemini:{_GEMINI_MODEL}", _make_gemini_call(_GEMINI_MODEL)),
    ]
    layers += _openai_layers()
    layers += _groq_or_mistral_layers()
    return layers


def _layers_respond() -> list[tuple[str, Callable]]:
    layers = [
        (f"anthropic:{settings.brain_respond_model}",
         _make_anthropic_call(settings.brain_respond_model, thinking=None)),
        (f"anthropic:{settings.brain_respond_model_alt}",
         _make_anthropic_call(settings.brain_respond_model_alt, thinking=None)),
        (f"gemini:{_GEMINI_MODEL}", _make_gemini_call(_GEMINI_MODEL)),
    ]
    layers += _openai_layers()
    layers += _groq_or_mistral_layers()
    return layers


def _provider_of(layer_name: str) -> str:
    return layer_name.split(":", 1)[0]


def _disabled_providers() -> set[str]:
    raw = (settings.brain_disable or "").strip()
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


# ── Телеметрия (funnel_events) — крэш-сейф, деградация видна в D1 ─────────────

async def _telemetry(tg_id: int | None, event: str, meta: str):
    if tg_id is None:
        return
    try:
        from database import log_event  # поздний импорт: без цикла на уровне модуля
        await log_event(tg_id, event, meta)
    except Exception:
        logger.warning("brain telemetry log_event failed (continuing)", exc_info=True)


# ── Общий бегунок каскада ──────────────────────────────────────────────────

async def run_cascade(purpose: str, system: str, history: list[dict], *,
                      layer_kwargs: dict, validate: Callable[[str], T | None],
                      safe_default_factory: Callable[[], T],
                      tg_id: int | None = None) -> T:
    """Идёт по упорядоченному списку слоёв purpose ('diagnose'|'respond'),
    пропуская отключенные (BRAIN_DISABLE). Слой «годится», только если его
    текст проходит validate(text) → не None — иначе тоже failover (сломанный
    контракт ретраить бессмысленно, к следующему слою). Если ВСЕ слои
    отказали/отключены — возвращает safe_default_factory() (static, без сети,
    никогда не бросает). НИКОГДА не бросает исключение сама."""
    all_layers = _layers_diagnose() if purpose == "diagnose" else _layers_respond()
    disabled = _disabled_providers()
    layers = [(n, f) for n, f in all_layers if _provider_of(n) not in disabled]

    last_failed: str | None = None
    for name, call in layers:
        if last_failed is not None:
            await _telemetry(tg_id, "brain_failover", f"{purpose}:{last_failed}->{name}")
        try:
            raw = await _with_retry(call, system, history, **layer_kwargs)
            result = validate(raw)
            if result is None:
                raise ValueError("ответ не прошёл validate (сломанный контракт)")
        except Exception as e:
            logger.warning("brain cascade[%s]: слой %s отказал: %s", purpose, name, e)
            last_failed = name
            continue
        await _telemetry(tg_id, "brain_layer", f"{purpose}:{name}")
        return result

    # Все сетевые слои отказали или отключены (тест неубиваемости) → статичный
    # безопасный слой — единственный, что НИКОГДА не падает и не выдумывает.
    if last_failed is not None:
        await _telemetry(tg_id, "brain_failover", f"{purpose}:{last_failed}->static")
    await _telemetry(tg_id, "brain_layer", f"{purpose}:static")
    return safe_default_factory()
