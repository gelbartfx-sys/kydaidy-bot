"""Тесты глубокого диагноз-движка под 12-шаговую воронку (задача А, 06.07):
структурный диагноз (offer_readiness / funnel_step), классификатор возражений +
потолок 3, state-маркер [[PHASE]]. Чистая логика, без сети/БД.

Запуск: python3 test_funnel.py  (или pytest test_funnel.py)
"""

import os

# config.Settings() требует эти env при импорте (alena_brain → config).
os.environ.setdefault("TG_BOT_TOKEN", "test:token")
os.environ.setdefault("TG_ADMIN_ID", "0")

from alena_persona import (
    METHOD_PHASES, method_phase_to_step, clamp_readiness,
    extract_phase, classify_objection, objection_directive,
    OBJECTION_CAP, OBJECTION_TYPES, _OBJECTION_DIRECTIVE,
)
from alena_brain import _default_diagnosis, _valid_reply
from alena_chat import _last_question, _ensure_reply, _EMPTY_REPLY_STUB


# ── Механизм 3: маппинг фаза → шаг воронки (1..12) ────────────────────────────
def test_method_phase_to_step():
    # Все 6 фаз метода дают валидный шаг воронки 1..12.
    for ph in METHOD_PHASES:
        s = method_phase_to_step(ph)
        assert isinstance(s, int) and 1 <= s <= 12, (ph, s)
    # Точные якоря карты 12 шагов.
    assert method_phase_to_step("contact") == 2
    assert method_phase_to_step("catch_contradiction") == 4
    assert method_phase_to_step("give_shift") == 6
    assert method_phase_to_step("native_offer") == 8, "мост(7)+оффер(8) = native_offer"
    # Монотонность по метод-петле (шаг не убывает вдоль фаз).
    steps = [method_phase_to_step(p) for p in METHOD_PHASES]
    assert steps == sorted(steps), steps
    # Неизвестная/пустая фаза → 2 (contact), не падаем.
    assert method_phase_to_step("мусор") == 2
    assert method_phase_to_step(None) == 2


# ── Механизм 1: готовность к офферу — дробь [0,1], крэш-сейф ──────────────────
def test_clamp_readiness():
    assert clamp_readiness(0.5) == 0.5
    assert clamp_readiness(0) == 0.0
    assert clamp_readiness(1) == 1.0
    assert clamp_readiness(-3) == 0.0, "ниже нуля → 0"
    assert clamp_readiness(9) == 1.0, "выше единицы → 1"
    assert clamp_readiness("мусор") == 0.0, "нечисло → 0"
    assert clamp_readiness(None) == 0.0
    assert clamp_readiness("0.75") == 0.75, "числовая строка парсится"


def test_default_diagnosis_has_funnel_fields():
    d = _default_diagnosis()
    assert "offer_readiness" in d and d["offer_readiness"] == 0.0
    assert "funnel_step" in d and d["funnel_step"] == 2, "дефолт = шаг contact"
    # копии независимы
    d["funnel_step"] = 9
    assert _default_diagnosis()["funnel_step"] == 2, "дефолт не мутируется"


# ── Механизм 3: state-маркер [[PHASE]] вырезается, чтобы не утёк в TTS ────────
def test_extract_phase():
    clean, ph = extract_phase("Я здесь, слышу тебя. [[PHASE:contact]]")
    assert ph == "contact"
    assert "[[" not in clean and "PHASE" not in clean, clean
    assert clean == "Я здесь, слышу тебя."
    # регистр/пробелы гибкие
    _, ph2 = extract_phase("текст [[ PHASE: Give_Shift ]]")
    assert ph2 == "give_shift"
    # числовая форма [[PHASE:6]] тоже вырезается (защита от утечки в TTS)
    clean3, ph3 = extract_phase("Разберём это. [[PHASE:6]]")
    assert "[[" not in clean3 and "PHASE" not in clean3, clean3
    assert clean3 == "Разберём это."
    assert ph3 == "6"
    # нет маркера → (текст, None), текст не тронут
    assert extract_phase("просто живая реплика без служебки") == (
        "просто живая реплика без служебки", None)
    # пусто/None
    assert extract_phase("") == ("", None)
    assert extract_phase(None) == (None, None)


# ── Механизм 2: классификатор возражений ─────────────────────────────────────
def test_classify_objection():
    assert classify_objection("дорого, за что 990?") == "price"
    assert classify_objection("это же и в ютубе есть бесплатно") == "price"
    assert classify_objection("сейчас нет денег совсем") == "price"
    assert classify_objection("нет времени, я вся в работе") == "time"
    assert classify_objection("давай после отпуска") == "time"
    assert classify_objection("инфоцыганство, все коучи одинаковые") == "trust"
    assert classify_objection("а гарантии есть? точно поможет?") == "trust"
    assert classify_objection("была у трёх психологов, не помогло") == "trust"
    assert classify_objection("я подумаю") == "think"
    assert classify_objection("надо посоветоваться с мужем") == "think"
    assert classify_objection("не сейчас, попозже") == "think"
    # прочее / неопознанное → other (fallback, не падаем)
    assert classify_objection("я сама справлюсь, без тебя") == "other"
    assert classify_objection("ммм не знаю даже") == "other"
    assert classify_objection("") == "other"
    assert classify_objection(None) == "other"
    # все возвращаемые типы — из объявленного контракта
    for t in ("дорого", "нет времени", "гарантии", "подумаю", "непонятно"):
        assert classify_objection(t) in OBJECTION_TYPES


# ── Механизм 2: продающая ветка на тип + мягкий выход на потолке ──────────────
def test_objection_directive_per_type_and_cap():
    # у каждого типа своя ветка (директивы различаются)
    dirs = {t: objection_directive(t, "боюсь близости", 1) for t in OBJECTION_TYPES}
    assert len(set(dirs.values())) == len(OBJECTION_TYPES), "ветки не должны совпадать"
    # запрос вплетён; протокол вскрой→валидируй→механизм присутствует
    d = objection_directive("price", "я выбираю недоступных", 1)
    assert "я выбираю недоступных" in d
    assert "вскрой" in d.lower() and "валидируй" in d.lower()
    assert "цен" in d.lower(), "price-ветка про цену/ценность"
    # неизвестный тип → other-ветка (fallback, не падаем)
    assert objection_directive("боже-что-это", None, 0) == \
        objection_directive("other", None, 0)
    # мягкий выход: до потолка — без него, на потолке — есть
    below = objection_directive("time", "устала быть сильной", OBJECTION_CAP - 1)
    at_cap = objection_directive("time", "устала быть сильной", OBJECTION_CAP)
    assert "отпусти" not in below.lower(), "до потолка не отпускаем"
    assert "отпусти" in at_cap.lower(), "на потолке — мягкий выход"
    assert OBJECTION_CAP == 3, "потолок ровно 3 попытки суммарно"


# ── Волна 0: валидатор реплики каскада (мусор модели не уходит клиентке) ──────
def test_valid_reply():
    # нормальная русская реплика — проходит, вернулась стрипнутой
    good = "Я рядом. Скажи, что сейчас с тобой происходит на самом деле?"
    assert _valid_reply("  " + good + "  ") == good
    # пусто / только пробелы → None
    assert _valid_reply("") is None
    assert _valid_reply("   \n  ") is None
    assert _valid_reply(None) is None
    # слишком короткое (<20) → None
    assert _valid_reply("да, слышу") is None
    # JSON-огрызок ('{' / '[') → None
    assert _valid_reply('{"role": "assistant", "content": "…текст подлиннее…"}') is None
    assert _valid_reply('["элемент раз", "элемент два и ещё немного"]') is None
    # служебный "role": внутри → None (даже без ведущей скобки)
    assert _valid_reply('текст с утечкой "role": assistant и хвостом подлиннее') is None
    # английский отказ → None
    assert _valid_reply("I cannot help you with that request, sorry.") is None
    assert _valid_reply("Sorry, I can't do that for you right now.") is None
    # чистая латиница без кириллицы (не речь Алёны) → None
    assert _valid_reply("this is a long english sentence without cyrillic") is None


# ── Волна 0: текст-дублёр — без «?» дублируем ХВОСТ реплики, не шаблон ─────────
def test_last_question_fallback():
    # есть вопрос → возвращаем вопрос
    q = _last_question("Понимаю тебя. А что ты сейчас чувствуешь в теле?")
    assert q == "А что ты сейчас чувствуешь в теле?"
    # нет вопроса → хвост реплики (последнее предложение), НЕ None и НЕ шаблон
    reply = "Ты держишь это в себе давно. И тебе тяжело нести одной."
    tail = _last_question(reply)
    assert tail == "И тебе тяжело нести одной."
    assert not tail.endswith("?")
    # реплика без финальной пунктуации → возвращается сама (хвост = вся строка)
    assert _last_question("просто побудь здесь со мной") == "просто побудь здесь со мной"
    # совсем пусто → None (вызывающий даст ротацию форм)
    assert _last_question("") is None
    assert _last_question("   ") is None
    assert _last_question(None) is None
    # длинный хвост без «?» обрезается до 200 символов
    long_tail = "а" * 300
    out = _last_question(long_tail)
    assert out is not None and len(out) <= 200


# ── Волна 0: пустой reply → заглушка ВСЕГДА (в т.ч. на ходе закрытия) ─────────
def test_ensure_reply():
    assert _ensure_reply("") == _EMPTY_REPLY_STUB
    assert _ensure_reply("   \n ") == _EMPTY_REPLY_STUB
    assert _ensure_reply(None) == _EMPTY_REPLY_STUB
    # живой текст не трогаем
    assert _ensure_reply("Я тебя услышала.") == "Я тебя услышала."


if __name__ == "__main__":
    test_method_phase_to_step()
    test_clamp_readiness()
    test_default_diagnosis_has_funnel_fields()
    test_extract_phase()
    test_classify_objection()
    test_objection_directive_per_type_and_cap()
    test_valid_reply()
    test_last_question_fallback()
    test_ensure_reply()
    print("OK: funnel_step map + offer_readiness clamp + [[PHASE]] strip + "
          "objection classifier (5 типов) + cap 3 soft-exit + "
          "_valid_reply + _last_question fallback + _ensure_reply")
