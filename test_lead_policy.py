"""Тесты чистой политики треков лида (Фаза 1). Запуск: python3 test_lead_policy.py"""

from lead_policy import (
    classify, should_spend_circle, budget_credits, remaining_budget_credits,
    CIRCLE_CREDITS,
)


def test_classify():
    # Кит: value=3 → T4 (даже при высоком сопротивлении).
    assert classify({"heat": 0, "open": 0, "resist": 3, "value": 3}) == "T4"
    # Сложный, но ценный: value>=2 и resist>=2 → T3.
    assert classify({"heat": 1, "open": 1, "resist": 2, "value": 2}) == "T3"
    # Быстрый рез: тёплая, открытая, без стен → T1.
    assert classify({"heat": 3, "open": 3, "resist": 0, "value": 1}) == "T1"
    # Тёплая думающая → T2.
    assert classify({"heat": 1, "open": 1, "resist": 2, "value": 0}) == "T2"
    # Пустое / None → дефолт T2.
    assert classify({}) == "T2"
    assert classify(None) == "T2"
    print("test_classify: PASS")


def test_should_spend_circle():
    # Горячий в рамках бюджета → True (heat>=2, трат ещё нет).
    hot = {"heat": 3, "open": 2, "resist": 0, "value": 2}
    assert should_spend_circle(hot, "T4", 0) is True
    # Ценный (value>=2), но не горячий — тоже проходит ворота → True.
    assert should_spend_circle({"heat": 0, "value": 2}, "T4", 0) is True
    # Холодный (heat=0, value=0) → False (не прошёл ворота эскалации).
    cold = {"heat": 0, "open": 0, "resist": 0, "value": 0}
    assert should_spend_circle(cold, "T4", 0) is False
    # Превышение бюджета → False, даже если лид горячий.
    over = budget_credits("T1")  # уже потрачено под потолок
    assert should_spend_circle(hot, "T1", over) is False
    # None-сигналы → False.
    assert should_spend_circle(None, "T4", 0) is False
    print("test_should_spend_circle: PASS")


def test_budget_credits():
    b1 = budget_credits("T1")
    b2 = budget_credits("T2")
    b3 = budget_credits("T3")
    b4 = budget_credits("T4")
    # Растут и монотонны: T1 < T2 < T3 < T4.
    assert b1 < b2 < b3 < b4, (b1, b2, b3, b4)
    # Кит-бюджет должен вмещать хотя бы один кружок.
    assert b4 >= CIRCLE_CREDITS
    # remaining не уходит в минус.
    assert remaining_budget_credits("T1", b1 + 100) == 0
    assert remaining_budget_credits("T4", 0) == b4
    print("test_budget_credits: PASS")


if __name__ == "__main__":
    test_classify()
    test_should_spend_circle()
    test_budget_credits()
    print("ALL PASS")
