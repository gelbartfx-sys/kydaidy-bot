"""Мини-тест мозга v2 (Фаза 1 ядра): parse_diagnose_json + безопасный дефолт.

Только чистая логика (без сети/БД). Запуск: python3 test_brain.py
"""

import os

# config.Settings() требует эти env при импорте (alena_brain → config). Для
# оффлайн-теста подставляем заглушки, если их нет в окружении — сети мы не трогаем.
os.environ.setdefault("TG_BOT_TOKEN", "test:token")
os.environ.setdefault("TG_ADMIN_ID", "0")

from alena_persona import parse_diagnose_json, METHOD_PHASES
from alena_brain import _default_diagnosis, score_to_signals
from lead_policy import classify

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


if __name__ == "__main__":
    test_valid()
    test_wrapped()
    test_broken()
    test_default_valid()
    test_score_to_signals()
    print("OK: parse_diagnose_json + safe default + score_to_signals→lead track wire")
