"""Мини-тест мозга v2 (Фаза 1 ядра): parse_diagnose_json + безопасный дефолт +
каскад мозга (критичный рефактор 04.07): failover/ретрай/статичный слой.

Только чистая логика (без сети/БД). Запуск: python3 test_brain.py
"""

import asyncio
import os

# config.Settings() требует эти env при импорте (alena_brain → config). Для
# оффлайн-теста подставляем заглушки, если их нет в окружении — сети мы не трогаем.
os.environ.setdefault("TG_BOT_TOKEN", "test:token")
os.environ.setdefault("TG_ADMIN_ID", "0")

from alena_persona import (
    parse_diagnose_json, METHOD_PHASES, static_safe_reply,
    build_response_prompt, PHASE_STREAK_BREAK, CLOSING_HINT_DIRECTIVE,
)
from alena_brain import (
    _default_diagnosis, score_to_signals, _build_diagnosis,
    _bump_streak, _merge_moves, PHASE_STREAK_LIMIT, _STREAK_BREAK_PHASES,
    _hold_before_shift, GIVE_SHIFT_MIN_HOLD,
)
from lead_policy import classify
import brain_cascade
import growth_agent
from config import settings

_VALID = '{"client_model":{"pattern":"избегает близости"},"score":{"ж":1,"о":2,"с":1,"ц":2},"method_phase":"contact","directive":"признай чувство","medium":"text","track":"T2"}'

_WRAPPED = "Вот диагноз:\n```json\n" + _VALID + "\n```\nготово."

_BROKEN = "это не json вообще, просто болтовня без скобок"

_BROKEN_BRACE = "{ это { сломано и не закрывается корректно "


def test_valid():
    d = parse_diagnose_json(_VALID)
    assert isinstance(d, dict), "valid → dict"
    assert d["method_phase"] == "contact"
    assert d["client_model"]["pattern"] == "избегает близости"
    assert d["score"]["о"] == 2


def test_wrapped():
    d = parse_diagnose_json(_WRAPPED)
    assert isinstance(d, dict), "обёрнутый в ```json → извлекается"
    assert d["directive"] == "признай чувство"
    assert d["track"] == "T2"


def test_broken():
    assert parse_diagnose_json(_BROKEN) is None, "мусор → None"
    assert parse_diagnose_json(_BROKEN_BRACE) is None, "битые скобки → None"
    assert parse_diagnose_json("") is None, "пусто → None"
    assert parse_diagnose_json(None) is None, "None → None"


def test_default_valid():
    d = _default_diagnosis()
    assert isinstance(d, dict)
    for k in ("client_model", "score", "method_phase", "directive", "medium", "track"):
        assert k in d, f"дефолт содержит {k}"
    assert d["method_phase"] in METHOD_PHASES, "дефолтная фаза валидна"
    assert isinstance(d["client_model"], dict) and d["client_model"] == {}
    assert isinstance(d["directive"], str) and d["directive"]
    # копии независимы (не мутируют общий словарь)
    d["client_model"]["x"] = 1
    assert _default_diagnosis()["client_model"] == {}, "дефолт не мутируется"


def test_score_to_signals():
    # русские ключи диагноза → EN для save_lead_signals
    s = score_to_signals({"ж": 3, "о": 2, "с": 0, "ц": 3})
    assert s == {"heat": 3, "open": 2, "resist": 0, "value": 3}, s
    # трек из этих сигналов: value=3 → T4 (кит)
    assert classify(s) == "T4", classify(s)
    # EN-ключи тоже принимаются (толерантность к схеме модели)
    assert score_to_signals({"heat": 1})["heat"] == 1
    # мусор/None/нечисло → пропускаем поле, не падаем
    assert score_to_signals(None) == {}
    assert score_to_signals({"ж": "нет", "о": 1}) == {"open": 1}
    # частичный скоринг: classify толерантен к отсутствующим полям
    assert classify(score_to_signals({"ж": 3, "о": 3})) in ("T1", "T2")


def test_static_safe_reply():
    # Каждая фаза метода даёт валидный, непустой ход — банк без сети никогда пуст.
    for phase in METHOD_PHASES:
        r = static_safe_reply(phase)
        assert isinstance(r, str) and r.strip(), f"static reply for {phase} пуст"
    assert static_safe_reply(None) == static_safe_reply("мусорная-фаза"), \
        "неизвестная/пустая фаза → дефолт"


def test_cascade_transient_classification():
    class _Err(Exception):
        def __init__(self, code):
            self.status_code = code
    assert brain_cascade._is_transient(_Err(429)) is True
    assert brain_cascade._is_transient(_Err(500)) is True
    assert brain_cascade._is_transient(_Err(400)) is False, "400 — сразу failover, не ретрай"
    assert brain_cascade._is_transient(_Err(401)) is False, "401 (auth) — сразу failover"
    assert brain_cascade._is_transient(TimeoutError("x")) is True


def test_cascade_retry_then_failover():
    async def _run():
        calls = []

        async def flaky(system, history, **kw):
            calls.append(1)
            if len(calls) < 2:
                raise TimeoutError("transient")
            return "ok"
        out = await brain_cascade._with_retry(flaky, "sys", [])
        assert out == "ok" and len(calls) == 2, "транзиентная — ретрай (2 попытки) потом успех"

        calls2 = []

        class _Auth(Exception):
            status_code = 401

        async def bad(system, history, **kw):
            calls2.append(1)
            raise _Auth()
        try:
            await brain_cascade._with_retry(bad, "sys", [])
            assert False, "должно было бросить"
        except _Auth:
            pass
        assert len(calls2) == 1, "400/401 — БЕЗ ретрая, сразу failover"
    asyncio.run(_run())


def test_cascade_optional_slots_silent_skip():
    prev = (settings.openai_api_key, settings.groq_api_key, settings.mistral_api_key)
    try:
        settings.openai_api_key = ""
        settings.groq_api_key = ""
        settings.mistral_api_key = ""
        assert brain_cascade._openai_layers() == []
        assert brain_cascade._groq_or_mistral_layers() == []
        settings.openai_api_key = "sk-test"
        names = [n for n, _ in brain_cascade._openai_layers()]
        assert names == [f"openai:{settings.brain_openai_model}"]
        settings.groq_api_key = "gsk-test"
        settings.mistral_api_key = "mk-test"
        names = [n for n, _ in brain_cascade._groq_or_mistral_layers()]
        assert names == [f"groq:{settings.brain_groq_model}"], "Groq первым при обоих ключах"
    finally:
        settings.openai_api_key, settings.groq_api_key, settings.mistral_api_key = prev


def test_cascade_all_network_layers_disabled_falls_to_static():
    """Мандат Кая: «воронка не ломается ни при каких обстоятельствах» —
    BRAIN_DISABLE=anthropic,gemini (+ без ключей openai/groq/mistral) → каскад
    ОБЯЗАН вернуть статичный безопасный дефолт, не бросить исключение."""
    async def _run():
        prev_disable = settings.brain_disable
        prev_keys = (settings.openai_api_key, settings.groq_api_key, settings.mistral_api_key)
        try:
            settings.brain_disable = "anthropic,gemini"
            settings.openai_api_key = settings.groq_api_key = settings.mistral_api_key = ""
            result = await brain_cascade.run_cascade(
                "respond", "SYSTEM", [{"role": "user", "content": "привет"}],
                layer_kwargs={"max_tokens": 100, "timeout": 5.0},
                validate=lambda raw: (raw or "").strip() or None,
                safe_default_factory=lambda: "STATIC-OK",
                tg_id=None,
            )
            assert result == "STATIC-OK", "все сетевые слои отключены → статичный слой"
        finally:
            settings.brain_disable = prev_disable
            (settings.openai_api_key, settings.groq_api_key,
             settings.mistral_api_key) = prev_keys
    asyncio.run(_run())


async def _async_true():
    return True


async def _async_false():
    return False


def test_growth_context_dossier_requires_memory():
    """growth_agent._context: dossier попадает в промпт ТОЛЬКО при memory_ok=True
    (гейт по реальному статусу покупки, не по имени сегмента) — находка аудита
    04.07: alena_no_buy = НЕ купившие, но был в auto-send без гейта памяти."""
    user = {"tg_id": 1, "first_name": "Аня", "dossier": "приносила тревогу про мать"}
    blocked = growth_agent._context(user, "alena_no_buy", memory_ok=False)
    assert "ДОСЬЕ" not in blocked, "memory_ok=False → без досье"
    assert "приносила тревогу про мать" not in blocked
    allowed = growth_agent._context(user, "alena_no_buy", memory_ok=True)
    assert "ДОСЬЕ" in allowed and "приносила тревогу про мать" in allowed, \
        "memory_ok=True (купившая) → досье в промпте"
    # без dossier вообще у юзера — оба варианта одинаково чисты
    user2 = {"tg_id": 2}
    assert "ДОСЬЕ" not in growth_agent._context(user2, "alena_no_buy", memory_ok=True)


def test_growth_make_draft_gates_dossier_by_real_purchase_status():
    """_make_draft_text вызывает database.memory_allowed(tg_id) и гейтует dossier
    ПЕРЕД генерацией — даже для auto-send сегмента alena_no_buy не купившая
    получает чистый лист, купившая — досье (мандат «чистый лист для не купивших»)."""
    async def _run():
        captured = {}

        async def fake_gen(text, **kw):
            captured["text"] = text
            return "черновик"

        orig_gen, orig_memory = growth_agent._gen, growth_agent.memory_allowed
        growth_agent._gen = fake_gen
        try:
            user = {"tg_id": 111, "first_name": "Ира", "dossier": "рассказывала про развод"}

            growth_agent.memory_allowed = lambda tg_id: _async_false()
            await growth_agent._make_draft_text(user, "alena_no_buy")
            assert "рассказывала про развод" not in captured["text"], \
                "не купившей (нет клуба, нет покупок) — dossier НЕ должен попасть в промпт"

            growth_agent.memory_allowed = lambda tg_id: _async_true()
            await growth_agent._make_draft_text(user, "alena_no_buy")
            assert "рассказывала про развод" in captured["text"], \
                "купившей — dossier должен попасть в промпт"
        finally:
            growth_agent._gen = orig_gen
            growth_agent.memory_allowed = orig_memory
    asyncio.run(_run())


# ── Батч А (мозг Алёны: фазы/пик/дубли) — аудит воронки 06.07 ────────────────
def test_diagnosis_peak_moves_parse():
    """_build_diagnosis разбирает peak/moves: валид/мусор/отсутствие → дефолты."""
    # валид: peak true + список приёмов (нормализуется к lower)
    d = _build_diagnosis({"method_phase": "catch_contradiction", "peak": True,
                          "moves": ["Побудь-с-этим", "океан-за-словами"]})
    assert d["peak"] is True
    assert d["moves"] == ["побудь-с-этим", "океан-за-словами"], d["moves"]
    # отсутствие полей → дефолты (peak False, moves [])
    d2 = _build_diagnosis({"method_phase": "contact"})
    assert d2["peak"] is False and d2["moves"] == []
    # мусор: peak строкой/числом = НЕ пик (только литеральный true), moves не список
    assert _build_diagnosis({"peak": "true", "moves": {"x": 1}})["peak"] is False
    assert _build_diagnosis({"peak": "true"})["moves"] == []
    d4 = _build_diagnosis({"peak": 1, "moves": ["дар-видеть", 5, "", "  "]})
    assert d4["peak"] is False, "только literal true = пик"
    assert d4["moves"] == ["дар-видеть"], d4["moves"]
    # дефолт диагноза несёт peak/moves
    base = _default_diagnosis()
    assert base["peak"] is False and base["moves"] == []


def test_bump_streak_increment_and_reset():
    """phase_streak: та же фаза → +1, смена/пусто/мусор → сброс в 1 (чистая функция)."""
    # первая фаза при пустой модели → 1 (прежней фазы нет)
    cm = {}
    assert _bump_streak(cm, "surface_facade") == 1 and cm["phase_streak"] == 1
    # та же фаза подряд — инкремент
    assert _bump_streak({"method_phase": "surface_facade", "phase_streak": 1},
                        "surface_facade") == 2
    assert _bump_streak({"method_phase": "surface_facade", "phase_streak": 2},
                        "surface_facade") == 3
    # смена фазы — сброс
    assert _bump_streak({"method_phase": "surface_facade", "phase_streak": 3},
                        "catch_contradiction") == 1
    # мусорный прежний streak трактуется как 0 → 1
    assert _bump_streak({"method_phase": "contact", "phase_streak": "oops"},
                        "contact") == 1
    # пустая новая фаза → сброс
    assert _bump_streak({"method_phase": "contact", "phase_streak": 5}, None) == 1


def test_streak_break_directive_carries_shift():
    """На залипании (streak≥2, копающая фаза) директива несёт «сдвиг» и попадает
    в промпт голоса; ниже лимита / на некопающей фазе — брейк НЕ клеится (task 1)."""
    assert "сдвиг" in PHASE_STREAK_BREAK.lower()
    phase = "catch_contradiction"
    directive = "поймай противоречие"
    # ровно логика brain_turn
    if 2 >= PHASE_STREAK_LIMIT and phase in _STREAK_BREAK_PHASES:
        directive = f"{directive} {PHASE_STREAK_BREAK}".strip()
    assert "сдвиг" in directive.lower()
    prompt = build_response_prompt("Аня", None, directive, phase)
    assert "сдвиг" in prompt.lower() and "СТОП-ЗАЛИПАНИЕ" in prompt
    # ниже лимита — не клеим
    d2 = "веди мягко"
    if 1 >= PHASE_STREAK_LIMIT and phase in _STREAK_BREAK_PHASES:
        d2 = f"{d2} {PHASE_STREAK_BREAK}"
    assert "СТОП-ЗАЛИПАНИЕ" not in d2


def test_hold_before_shift_holds_tension():
    """Держим напряжение: give_shift/native_offer не раньше выдержки на
    name_true_request и НИКОГДА на свежем пике; closing_hint (bypass) отпускает
    к резолюции у TURN_CAP (task 1: «мозг рано идёт в give_shift»)."""
    # первое вскрытие: диагноз рвётся в give_shift, держим на назывании
    cm = {}
    assert _hold_before_shift(cm, "give_shift", peak=False) == "name_true_request"
    assert cm["request_hold"] == 1
    # ещё ход на назывании — набираем выдержку
    assert _hold_before_shift(cm, "give_shift", peak=False) == "name_true_request"
    assert cm["request_hold"] == GIVE_SHIFT_MIN_HOLD
    # выдержано → сдвиг разрешён
    assert _hold_before_shift(cm, "give_shift", peak=False) == "give_shift"
    # свежий пик НИКОГДА не резолвим, даже если выдержано
    assert _hold_before_shift({"request_hold": 5}, "give_shift", peak=True) \
        == "name_true_request"
    # native_offer держим так же
    assert _hold_before_shift({}, "native_offer", peak=False) == "name_true_request"
    # bypass (closing_hint у TURN_CAP) — даём встрече свернуться
    assert _hold_before_shift({}, "give_shift", peak=False, bypass=True) == "give_shift"
    # откат на раннюю фазу сбрасывает счётчик (напряжение ещё не собрано)
    cm2 = {"request_hold": 2}
    assert _hold_before_shift(cm2, "surface_facade", peak=False) == "surface_facade"
    assert cm2["request_hold"] == 0


def test_build_response_prompt_peak_injects_protocol():
    """peak=True инъектирует протокол пика с запретом копающего вопроса-в-бездну."""
    plain = build_response_prompt("Аня", None, "веди", "catch_contradiction", peak=False)
    assert "ПИК БОЛИ" not in plain
    peaked = build_response_prompt("Аня", None, "веди", "catch_contradiction", peak=True)
    assert "ПИК БОЛИ" in peaked
    assert "в каком именно моменте" in peaked, "запрет копающего вопроса на пике"


def test_build_response_prompt_used_moves_antidubl():
    """Прошлые приёмы → правило анти-дубля в промпте; без них — лимит «побудь с этим»."""
    with_moves = build_response_prompt("Аня", None, "веди", "contact",
                                       used_moves=["счёт-слов", "океан-за-словами"])
    assert "АНТИ-ДУБЛЬ" in with_moves
    assert "счёт-слов" in with_moves and "океан-за-словами" in with_moves
    none_moves = build_response_prompt("Аня", None, "веди", "contact")
    assert "не чаще ОДНОГО раза" in none_moves


def test_merge_moves_accumulate_dedup():
    """used_moves копится между ходами: дедуп с порядком, мусор/None отсеиваются."""
    cm = {}
    _merge_moves(cm, ["Счёт-слов"])
    assert cm["used_moves"] == ["счёт-слов"]
    _merge_moves(cm, ["счёт-слов", "дар-видеть", 5, ""])
    assert cm["used_moves"] == ["счёт-слов", "дар-видеть"], cm["used_moves"]
    _merge_moves(cm, None)
    assert cm["used_moves"] == ["счёт-слов", "дар-видеть"]
    cm2 = {"used_moves": ["A", 3, "B"]}
    _merge_moves(cm2, ["c"])
    assert cm2["used_moves"] == ["a", "b", "c"]


def test_closing_hint_directive_folds_meeting():
    """closing_hint → директива свёртывания (task 7): «свёртывание»/«последний» в промпте."""
    low = CLOSING_HINT_DIRECTIVE.lower()
    assert "свёртывание" in low and "последний" in low
    directive = f"собери узел {CLOSING_HINT_DIRECTIVE}".strip()
    prompt = build_response_prompt("Аня", None, directive, "name_true_request").lower()
    assert "свёртывание" in prompt or "последний" in prompt


if __name__ == "__main__":
    test_valid()
    test_wrapped()
    test_broken()
    test_default_valid()
    test_score_to_signals()
    test_static_safe_reply()
    test_cascade_transient_classification()
    test_cascade_retry_then_failover()
    test_cascade_optional_slots_silent_skip()
    test_cascade_all_network_layers_disabled_falls_to_static()
    test_growth_context_dossier_requires_memory()
    test_growth_make_draft_gates_dossier_by_real_purchase_status()
    test_diagnosis_peak_moves_parse()
    test_bump_streak_increment_and_reset()
    test_streak_break_directive_carries_shift()
    test_hold_before_shift_holds_tension()
    test_build_response_prompt_peak_injects_protocol()
    test_build_response_prompt_used_moves_antidubl()
    test_merge_moves_accumulate_dedup()
    test_closing_hint_directive_folds_meeting()
    print("OK: parse_diagnose_json + safe default + score_to_signals→lead track wire + "
         "brain_cascade (retry/failover/static/BRAIN_DISABLE) + "
         "growth_agent dossier memory gate + "
         "batch-A brain (peak/moves parse, phase_streak, streak-break shift, "
         "peak protocol, used_moves anti-dubl, merge_moves, closing_hint)")


def test_brain_peak_persisted_to_client_model():
    """Аудит финал-батча: peak обязан доехать до client_model — его читают
    страховка дошива (_talk/orphan) и ускоренный 7-мин надж stale-тика."""
    import asyncio
    import alena_brain as ab

    async def fake_diagnose(*a, **k):
        d = ab._default_diagnosis()
        d["peak"] = True
        d["method_phase"] = "catch_contradiction"
        return d

    async def fake_respond(*a, **k):
        return "Я рядом. Напиши мне одно слово — что сейчас в груди."

    orig_d, orig_r = ab.diagnose, ab.respond
    ab.diagnose, ab.respond = fake_diagnose, fake_respond
    try:
        reply, cm, signals, track = asyncio.run(
            ab.brain_turn([{"role": "user", "content": "он уйдёт"}],
                          None, None, {}, None))
        assert cm.get("peak") is True, "peak потерян по пути в client_model"
    finally:
        ab.diagnose, ab.respond = orig_d, orig_r
