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

from alena_persona import parse_diagnose_json, METHOD_PHASES, static_safe_reply
from alena_brain import _default_diagnosis, score_to_signals
from lead_policy import classify
import brain_cascade
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
    print("OK: parse_diagnose_json + safe default + score_to_signals→lead track wire + "
         "brain_cascade (retry/failover/static/BRAIN_DISABLE)")
