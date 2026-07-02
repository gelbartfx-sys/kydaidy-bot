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
# Строим при первом вызове; и сам пакет anthropic импортируем лениво — чтобы импорт
# модуля не падал там, где пакета/ключа нет (локальные офлайн-тесты, флаг OFF).
# Любой сбой импорта/конструктора ловится в вызывающих try → фолбэк на v1.
_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic  # ленивый импорт: на Render пакет есть, локально не нужен
        _client = anthropic.AsyncAnthropic()  # ANTHROPIC_API_KEY из env
    return _client


def _default_diagnosis() -> dict:
    """Свежая копия безопасного дефолта (без мутации общего словаря)."""
    d = dict(_SAFE_DIRECTIVE)
    d["client_model"] = {}
    d["score"] = {}
    return d


# Скоринг диагноза приходит русскими ключами (ж/о/с/ц) — схема build_diagnose_prompt.
# save_lead_signals ждёт EN (heat/open/resist/value). Маппим здесь, где живёт схема.
_SCORE_EN = {
    "ж": "heat", "о": "open", "с": "resist", "ц": "value",
    "heat": "heat", "open": "open", "resist": "resist", "value": "value",
}


def score_to_signals(score: dict | None) -> dict:
    """{ж,о,с,ц|heat,…}(0–3) → {heat,open,resist,value} (только валидные поля)."""
    out: dict = {}
    if isinstance(score, dict):
        for k, v in score.items():
            ek = _SCORE_EN.get(str(k).strip().lower())
            if not ek or ek in out:
                continue
            try:
                out[ek] = int(v)
            except (TypeError, ValueError):
                continue
    return out


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
                   client_model: dict | None, profile: str | None = None,
                   fresh: bool = False) -> dict:
    """ПРОХОД 1 — диагноз (Opus 4.8, adaptive thinking). → dict модели/директивы.

    profile — индивидуальная карта из теста (полное распределение Теней + досье):
    комбинации у всех разные, диагноз работает от её конкретной смеси.
    fresh=True — первый ход НОВОЙ сессии: модель клиентки трактуется как память
    прошлых встреч, метод-петля стартует заново (не смешиваем контексты сессий).
    Крэш-сейф: любой сбой (сеть/ключ/парс/пустой ответ) → безопасный дефолт."""
    try:
        system = build_diagnose_prompt(name, archetype, client_model, profile, fresh)
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
                  history: list[dict], voice_mode: bool = False,
                  profile: str | None = None) -> str:
    """ПРОХОД 2 — ответ голосом Алёны (Haiku 4.5), исполняет директиву.

    voice_mode=True → ответ будет озвучен: промпт требует устную речь (коротко,
    без письменных конструкций, без тавтологии).
    profile — её анкета (смесь Теней + досье): Алёна опирается на неё ЯВНО."""
    system = build_response_prompt(name, archetype, directive, method_phase,
                                   voice_mode, profile)
    messages = _to_claude_messages(history)
    return await _call_claude(
        settings.brain_respond_model, system, messages,
        max_tokens=1500, temperature=0.9, timeout=60)


async def brain_turn(history: list[dict], name, archetype,
                     client_model: dict | None,
                     profile: str | None = None,
                     fresh: bool = False,
                     force_voice: bool = False) -> tuple[str, dict, dict, str | None]:
    """Полный ход мозга v2: диагноз → ответ.

    → (reply Алёны, обновлённая модель клиентки, сигналы лида {heat,open,resist,value},
       трек 'T1'..'T4'|None).
    Скоринг диагноза (score) РАНЬШЕ выбрасывался — теперь отдаём наверх, чтобы _talk
    записал lead-сигналы и трек (без этого закрытие на Клуб шло без топлива, а /sources
    был слеп к 🔥). diagnose() крэш-сейф внутри. respond() может бросить — тогда бросаем
    наверх, вызывающий (_talk) фолбэчит на v1-путь в том же ходе."""
    dx = await diagnose(history, name, archetype, client_model, profile, fresh)
    # Канал хода решается ЗДЕСЬ (единая точка): голос — если диагноз попросил ИЛИ
    # фаза = истинный запрос/сдвиг (эмоц. пик по определению). Ответ тогда пишется
    # как устная речь, и _talk шлёт его голосовым (cm["medium"]).
    # force_voice — человек сам говорил голосом: зеркалим канал ЖЕЛЕЗНО (Кай 02.07:
    # на открытую боль пришёл длинный текст — доверять только решению диагноза нельзя).
    voice_out = (force_voice
                 or dx.get("medium") == "voice"
                 or dx.get("method_phase") in ("name_true_request", "give_shift"))
    reply = await respond(dx.get("directive"), dx.get("method_phase"),
                          name, archetype, history, voice_mode=voice_out,
                          profile=profile)

    # Собираем модель клиентки для сохранения: обновление от диагноза + служебка.
    cm = dict(client_model) if isinstance(client_model, dict) else {}
    new_cm = dx.get("client_model")
    if isinstance(new_cm, dict):
        cm.update(new_cm)
    cm["method_phase"] = dx.get("method_phase")
    cm["track"] = dx.get("track")
    # Директива канала ответа (H1): "voice" → _talk шлёт голосовым Алёны.
    # Едет внутри cm, чтобы не менять сигнатуру brain_turn.
    cm["medium"] = "voice" if voice_out else "text"

    signals = score_to_signals(dx.get("score"))
    track = dx.get("track") if isinstance(dx.get("track"), str) and dx.get("track") else None
    return reply, cm, signals, track
