"""AI-Алёна «мозг v2» (Фаза 1 ядра) — 2-проход: ДИАГНОЗ → ОТВЕТ.

Из рефлекс-ответчика (1 вызов/ход) → в мыслящий агент:
  • ПРОХОД 1 — ДИАГНОЗ (adaptive thinking). Вход компактный (модель клиентки +
    ~6 последних реплик). Выход — строгий JSON: обновлённая модель клиентки +
    скоринг + фаза метода + директива хода. Человек его НЕ видит.
  • ПРОХОД 2 — ОТВЕТ (голос Алёны). Исполняет директиву её голосом.

Критичный рефактор 04.07 (мандат Кая: «воронка не ломается НИ ПРИ КАКИХ
обстоятельствах»): оба прохода больше НЕ завязаны на один провайдер — каждый
идёт через provider-агностичный каскад (brain_cascade.run_cascade): Anthropic
(основная + запасная модель) → Gemini → OpenAI/Groq/Mistral-слоты → безопасный
статичный слой (без сети, никогда не падает). ВСЕ сетевые слои используют ОДИН
и тот же сильный system-контракт (build_diagnose_prompt/build_response_prompt,
с ANTI_HALLUCINATION) — раньше фолбэк на Gemini шёл по слабому SESSION_ARC без
защит, это и было корнем галлюцинаций/перепрыжки. Подробности каскада,
ретраев и телеметрии — см. brain_cascade.py.

Всё крэш-сейф: diagnose()/respond() НИКОГДА не бросают исключение — на полном
отказе всех сетевых слоёв возвращают безопасный статичный дефолт. Подключается
в alena_chat._talk ТОЛЬКО за флагом settings.brain_v2_enabled.
См. docs/hermes/ai-coach-architecture.md.
"""

from __future__ import annotations

import logging
import re

import brain_cascade
from alena_persona import (
    build_diagnose_prompt, build_response_prompt, parse_diagnose_json,
    static_safe_reply, METHOD_PHASES, method_phase_to_step, clamp_readiness,
)

logger = logging.getLogger(__name__)

# Сколько последних реплик отдаём диагнозу (компактный вход → дёшево на Opus).
DIAGNOSE_HISTORY = 6

# Безопасный дефолт директивы: при любом сбое диагноза встреча продолжается мягко.
_SAFE_DIRECTIVE = {
    "client_model": {},
    "score": {},
    "method_phase": METHOD_PHASES[0],   # contact
    "funnel_step": method_phase_to_step(METHOD_PHASES[0]),  # 2 (шаг воронки для contact)
    "offer_readiness": 0.0,             # готовность к офферу 0..1 (копится по ходу)
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


# Скоринг диагноза приходит русскими ключами (ж/о/с/ц) — схема build_diagnose_prompt.
# save_lead_signals ждёт EN (heat/open/resist/value). Маппим здесь, где живёт схема.
_SCORE_EN = {
    "ж": "heat", "о": "open", "с": "resist", "ц": "value",
    "heat": "heat", "open": "open", "resist": "resist", "value": "value",
}


def _valid_reply(raw) -> str | None:
    """Валидатор реплики каскада: пропускает только живую русскую речь Алёны.

    Отсекает мусор моделей — пусто, англ. отказ («I cannot…»/«I can't…»),
    JSON-огрызок ('{…'/'[…'/'"role":…'), слишком короткое (<20). Невалид → None
    (каскад берёт следующий слой, на дне — static_safe_reply). Модульная функция
    (тестируемая; аудит воронки 06.07)."""
    s = (raw or "").strip()
    if len(s) < 20:
        return None
    if s[0] in "{[":
        return None
    if not re.search(r"[а-яёА-ЯЁ]", s):
        return None
    low = s.lower()
    if "i cannot" in low or "i can't" in low or '"role":' in low:
        return None
    return s


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


async def diagnose(history: list[dict], name, archetype,
                   client_model: dict | None, profile: str | None = None,
                   fresh: bool = False, tg_id: int | None = None) -> dict:
    """ПРОХОД 1 — диагноз (adaptive thinking). → dict модели/директивы.

    profile — индивидуальная карта из теста (полное распределение Теней + досье):
    комбинации у всех разные, диагноз работает от её конкретной смеси.
    fresh=True — первый ход НОВОЙ сессии: модель клиентки трактуется как память
    прошлых встреч, метод-петля стартует заново (не смешиваем контексты сессий).
    tg_id — только для телеметрии каскада (brain_layer/brain_failover в D1);
    None — телеметрия молча пропускается (напр. вызов без известного юзера).
    Крэш-сейф: любой сбой (сеть/ключ/парс/пустой ответ, ВСЕ слои каскада) →
    безопасный дефолт — diagnose() НИКОГДА не бросает."""
    try:
        system = build_diagnose_prompt(name, archetype, client_model, profile, fresh)
        data = await brain_cascade.run_cascade(
            "diagnose", system, (history or [])[-DIAGNOSE_HISTORY:],
            layer_kwargs={"max_tokens": 3000, "timeout": 90.0},
            validate=parse_diagnose_json,
            safe_default_factory=_default_diagnosis,
            tg_id=tg_id,
        )
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
    # Готовность к офферу (0..1): копится по ходу, кормит следующий диагноз
    # (модель видит свою прошлую оценку — «не гадает заново»). Мусор → 0.0.
    out["offer_readiness"] = clamp_readiness(data.get("offer_readiness"))
    # Шаг воронки (1..12): берём валидный из модели, иначе выводим из фазы метода
    # (единый источник — маппинг method_phase_to_step, чтобы шаг не расходился с фазой).
    fs = data.get("funnel_step")
    if isinstance(fs, bool):
        fs = None
    if isinstance(fs, (int, float)) and 1 <= int(fs) <= 12:
        out["funnel_step"] = int(fs)
    else:
        out["funnel_step"] = method_phase_to_step(out["method_phase"])
    return out


async def respond(directive: str, method_phase: str, name, archetype,
                  history: list[dict], voice_mode: bool = False,
                  profile: str | None = None, tg_id: int | None = None) -> str:
    """ПРОХОД 2 — ответ голосом Алёны, исполняет директиву.

    voice_mode=True → ответ будет озвучен: промпт требует устную речь (коротко,
    без письменных конструкций, без тавтологии).
    profile — её анкета (смесь Теней + досье): Алёна опирается на неё ЯВНО.
    tg_id — телеметрия каскада (см. diagnose()). Крэш-сейф: respond() НИКОГДА
    не бросает — на полном отказе всех сетевых слоёв отдаёт статичный
    безопасный ход по фазе метода (alena_persona.static_safe_reply)."""
    try:
        system = build_response_prompt(name, archetype, directive, method_phase,
                                       voice_mode, profile)
    except Exception:
        logger.warning("build_response_prompt failed (static safe reply)", exc_info=True)
        return static_safe_reply(method_phase)
    # Голосовой ход: промпт целит 550–750 знаков (30–40 сек — тайминг Кая 03.07),
    # потолок токенов страхует от простыни (500 ток ≈ 900-1200 зн — TTS-гейт цел).
    # temperature НЕ задаём (Anthropic задепрекейтил параметр — 400 на каждом
    # вызове = обрыв воронки 03.07 19:12) — работаем на дефолте модели.
    try:
        return await brain_cascade.run_cascade(
            "respond", system, history,
            layer_kwargs={"max_tokens": (500 if voice_mode else 1500), "timeout": 60.0},
            validate=_valid_reply,
            safe_default_factory=lambda: static_safe_reply(method_phase),
            tg_id=tg_id,
        )
    except Exception:
        # Недостижимо на практике (run_cascade сама не бросает) — страховка типов.
        logger.error("brain cascade respond: неожиданный сбой (static safe reply)",
                    exc_info=True)
        return static_safe_reply(method_phase)


async def brain_turn(history: list[dict], name, archetype,
                     client_model: dict | None,
                     profile: str | None = None,
                     fresh: bool = False,
                     force_voice: bool = False,
                     tg_id: int | None = None) -> tuple[str, dict, dict, str | None]:
    """Полный ход мозга v2: диагноз → ответ.

    → (reply Алёны, обновлённая модель клиентки, сигналы лида {heat,open,resist,value},
       трек 'T1'..'T4'|None).
    Скоринг диагноза (score) РАНЬШЕ выбрасывался — теперь отдаём наверх, чтобы _talk
    записал lead-сигналы и трек (без этого закрытие на Клуб шло без топлива, а /sources
    был слеп к 🔥). diagnose() и respond() крэш-сейф внутри (провайдер-агностичный
    каскад, см. brain_cascade.py) — brain_turn() НИКОГДА не бросает исключение.
    tg_id — телеметрия каскада (brain_layer/brain_failover в funnel_events)."""
    dx = await diagnose(history, name, archetype, client_model, profile, fresh, tg_id=tg_id)
    # Канал хода решается ЗДЕСЬ (единая точка): голос — если диагноз попросил ИЛИ
    # фаза = истинный запрос/сдвиг (эмоц. пик по определению). Ответ тогда пишется
    # как устная речь, и _talk шлёт его голосовым (cm["medium"]).
    # Кай 02.07 (финальное): ГОЛОС — КАНАЛ ПО УМОЛЧАНИЮ («куча текста — клиент
    # готов сорваться, читать лень»). Каждый содержательный ход пишется как устная
    # речь и уходит голосовым; текст — только фолбэк при сбое TTS (в _talk).
    voice_out = True
    _ = force_voice  # сохранён в сигнатуре: зеркало канала теперь покрыто дефолтом
    reply = await respond(dx.get("directive"), dx.get("method_phase"),
                          name, archetype, history, voice_mode=voice_out,
                          profile=profile, tg_id=tg_id)

    # Собираем модель клиентки для сохранения: обновление от диагноза + служебка.
    cm = dict(client_model) if isinstance(client_model, dict) else {}
    new_cm = dx.get("client_model")
    if isinstance(new_cm, dict):
        cm.update(new_cm)
    cm["method_phase"] = dx.get("method_phase")
    cm["track"] = dx.get("track")
    # Явный шаг воронки (1..12) и готовность к офферу (0..1) — часть структурного
    # диагноза: хранятся в состоянии (client_model), кормят следующий ход диагноза
    # (континуитет «не гадаем заново») и доступны /sources как топливо конверсии.
    cm["funnel_step"] = dx.get("funnel_step")
    cm["offer_readiness"] = dx.get("offer_readiness")
    # Директива канала ответа (H1): "voice" → _talk шлёт голосовым Алёны.
    # Едет внутри cm, чтобы не менять сигнатуру brain_turn.
    cm["medium"] = "voice" if voice_out else "text"

    signals = score_to_signals(dx.get("score"))
    track = dx.get("track") if isinstance(dx.get("track"), str) and dx.get("track") else None
    return reply, cm, signals, track
