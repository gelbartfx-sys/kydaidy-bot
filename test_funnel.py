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
from alena_chat import (
    _last_question, _ensure_reply, _EMPTY_REPLY_STUB,
    _should_binary_close, _offer_kbd, _member_offer_kbd, _request_from_cm,
    _offer_kbd_kind, CLUB_URL, ONE_ON_ONE_URL,
)
from alena_voice import _paid_touch_allowed, PAID_TOUCH_CAP_PER_MEETING


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


# ── Волна 1: бинарный дожим Шага 10 — только «подумаю» И готовность ≥0.6 ───────
def test_should_binary_close():
    # «подумаю» при высокой готовности → дожим
    assert _should_binary_close("think", 0.6) is True, "порог 0.6 включительно"
    assert _should_binary_close("think", 0.75) is True
    assert _should_binary_close("think", 1.0) is True
    assert _should_binary_close("think", "0.7") is True, "числовая строка парсится"
    # граница снизу и низкая готовность → без дожима
    assert _should_binary_close("think", 0.59) is False
    assert _should_binary_close("think", 0.0) is False
    assert _should_binary_close("think", None) is False
    assert _should_binary_close("think", "мусор") is False
    # чужие типы возражений → никогда не дожим (даже при высокой готовности)
    for other in ("price", "time", "trust", "other", None):
        assert _should_binary_close(other, 0.9) is False, other


# ── Волна 1: клавиатуры оффера — состав кнопок и URL ──────────────────────────
def test_offer_kbd():
    rows = _offer_kbd().inline_keyboard
    assert len(rows) == 3, "три двери"
    assert rows[0][0].url == CLUB_URL and "990" in rows[0][0].text
    assert rows[1][0].url == ONE_ON_ONE_URL and "1:1" in rows[1][0].text
    assert rows[2][0].callback_data == "alena:more"
    assert rows[2][0].url is None


def test_member_offer_kbd():
    rows = _member_offer_kbd().inline_keyboard
    assert len(rows) == 2, "член Клуба уже внутри → без двери Клуба"
    urls = [b.url for row in rows for b in row]
    assert CLUB_URL not in urls, "кнопки Клуба быть не должно"
    assert ONE_ON_ONE_URL in urls, "1:1 остаётся"
    cbs = [b.callback_data for row in rows for b in row]
    assert "alena:more" in cbs, "разбор «подробнее» остаётся"


# ── Волна 1: реконструкция запроса из модели клиентки ─────────────────────────
def test_request_from_cm():
    # берём ТОЛЬКО вскрытый настоящий запрос
    assert _request_from_cm(
        {"true_request_hypothesis": "боюсь близости", "facade_lie": "всё норм"}
    ) == "боюсь близости"
    # фасад-ложь НЕ подставляем (аудит W1 #1: это защита в 3-м лице, выдать её
    # за «вскрытую боль» = инверсия смысла) → None → мягкая ветка без цитаты
    assert _request_from_cm(
        {"true_request_hypothesis": "", "facade_lie": "всё норм"}) is None
    assert _request_from_cm({"facade_lie": "всё норм"}) is None
    # обрезаем пробелы
    assert _request_from_cm({"true_request_hypothesis": "  тревога  "}) == "тревога"
    # пусто/None/только пробелы → None (кружок не соберём из пустого)
    assert _request_from_cm({}) is None
    assert _request_from_cm(None) is None
    assert _request_from_cm({"true_request_hypothesis": "   "}) is None


# ── Волна 2: чистый селектор клавиатуры дожима bridge|club ────────────────────
def test_offer_kbd_kind():
    # дефолт (холодный, без свежих слов, до потолка) → club
    assert _offer_kbd_kind(None, False, False, "ну не знаю даже", 1) == "club"
    assert _offer_kbd_kind("T1", False, False, None, 0) == "club"
    # сегмент горячий/глубокий (события/трек) → bridge
    assert _offer_kbd_kind("T4", False, False, None, 0) == "bridge", "трек T4"
    assert _offer_kbd_kind(None, True, False, None, 0) == "bridge", "hot-событие"
    assert _offer_kbd_kind(None, False, True, None, 0) == "bridge", "depth-событие"
    # свежий depth/hot ПРЯМО в возражении → bridge (кросс-селл вверх)
    assert _offer_kbd_kind(None, False, False, "хочу с тобой вживую, глубже", 1) == "bridge"
    assert _offer_kbd_kind(None, False, False, "а сколько это стоит?", 1) == "bridge"
    # потолок отработок (последняя перед OBJECTION_CAP) → bridge (альтернатива 1:1)
    assert _offer_kbd_kind(None, False, False, "ну не знаю", OBJECTION_CAP - 1) == "bridge"
    assert _offer_kbd_kind(None, False, False, "ну не знаю", OBJECTION_CAP) == "bridge"
    # msg_text=None не роняет
    assert _offer_kbd_kind(None, False, False, None, 1) == "club"


# ── Волна 2: down-sell price + авторитет trust в директивах ───────────────────
def test_objection_directive_wave2_texts():
    price = _OBJECTION_DIRECTIVE["price"].lower()
    # механизм канала «остаться рядом» присутствует
    assert "канал" in price and "остаться рядом" in price, "down-sell: путь остаться рядом"
    assert "задёшево" in price, "парадокс «задёшево не дорожат»"
    # слова-запрета «бесплатно» в директиве нет (эталон №7)
    assert "бесплатно" not in price, "слово «бесплатно» запрещено"
    # trust опирается на «покажу» (созвучно соц-пруфу)
    assert "покажу" in _OBJECTION_DIRECTIVE["trust"].lower(), "trust: не убеждаю — покажу"


# ── Волна 3: бюджет «12 платных касаний на встречу» (Вариант А, деградация) ───
def test_paid_touch_cap_is_12():
    # Мандат Кая 06.07: ровно 12 платных касаний (аудио+кружки) на встречу.
    assert PAID_TOUCH_CAP_PER_MEETING == 12


def test_paid_touch_allowed_boundary():
    cap = PAID_TOUCH_CAP_PER_MEETING
    # Ниже потолка — можно; на потолке и выше — стоп (деградация в текст).
    assert _paid_touch_allowed(0, cap, protected=False) is True
    assert _paid_touch_allowed(11, cap, protected=False) is True
    assert _paid_touch_allowed(12, cap, protected=False) is False, "12 = потолок"
    assert _paid_touch_allowed(13, cap, protected=False) is False


def test_paid_touch_allowed_protected_always_true():
    # Вариант А: опенер и оффер-кружок — вне лимита, всегда True даже на потолке.
    assert _paid_touch_allowed(12, PAID_TOUCH_CAP_PER_MEETING, protected=True) is True
    assert _paid_touch_allowed(999, PAID_TOUCH_CAP_PER_MEETING, protected=True) is True


def test_paid_touch_allowed_none_count():
    # Свежая/домиграционная встреча (count None) = 0 касаний → можно.
    assert _paid_touch_allowed(None, PAID_TOUCH_CAP_PER_MEETING, protected=False) is True


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
    test_should_binary_close()
    test_offer_kbd()
    test_member_offer_kbd()
    test_request_from_cm()
    test_offer_kbd_kind()
    test_objection_directive_wave2_texts()
    test_paid_touch_cap_is_12()
    test_paid_touch_allowed_boundary()
    test_paid_touch_allowed_protected_always_true()
    test_paid_touch_allowed_none_count()
    print("OK: funnel_step map + offer_readiness clamp + [[PHASE]] strip + "
          "objection classifier (5 типов) + cap 3 soft-exit + "
          "_valid_reply + _last_question fallback + _ensure_reply + "
          "binary_close + offer_kbd/member_offer_kbd + request_from_cm + "
          "offer_kbd_kind (bridge|club) + wave2 down-sell/trust директивы")


# ── Мандат Кая 06.07: ни один ход не кончается голой констатацией ─────────────
def test_needs_prompt_and_ensure_prompt():
    from alena_chat import _needs_prompt, _ensure_prompt, _PROMPT_CUES
    # есть вопрос → побуждение уже есть
    assert _needs_prompt("Ты держишь всё сама. Что из этого твоё?") is False
    # императив в финале → побуждение есть
    assert _needs_prompt("Это броня. Расскажи, когда она появилась.") is False
    assert _needs_prompt("Побудь с этим секунду.") is False
    # голая констатация → нужен дошив (кейс Кая: «просто констатирует факты»)
    assert _needs_prompt("Ты научилась справляться одна. Это стало бронёй.") is True
    # пусто → не наше дело (закрывает _ensure_reply)
    assert _needs_prompt("") is False
    assert _needs_prompt(None) is False
    # дошив: констатация получает приглашение, ротация по turns
    base = "Ты научилась справляться одна. Это стало бронёй."
    out = _ensure_prompt(base, 3)
    assert out.startswith(base) and out != base
    assert any(c in out for c in _PROMPT_CUES)
    assert _ensure_prompt(base, 0) != _ensure_prompt(base, 1), "ротация форм"
    # реплика с вопросом не трогается
    q = "Что тут твоё на самом деле?"
    assert _ensure_prompt(q, 5) == q
