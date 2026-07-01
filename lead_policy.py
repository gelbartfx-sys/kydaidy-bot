"""Политика треков лида (Фаза 1 AI-Алёны) — ЧИСТАЯ логика, без БД.

«Мозг» скоринга: по 4 сигналам (heat/open/resist/value, каждый 0–3), которые
Алёна выставляет скрыто в конце каждой реплики, эта чистая логика решает:
  • на каком треке лид (T1..T4) — грубость/ценность контакта;
  • сколько HeyGen-кредитов не жалко потратить на персональные кружки (бюджет
    как % от цены Клуба — экономика Фазы 2);
  • можно ли прямо сейчас потратить кружок (ворота эскалации + потолок бюджета).

Всё — чистые функции без сайд-эффектов, легко тестируются
(`python3 test_lead_policy.py`). Никакого рендера видео здесь нет — это Фаза 2.
"""

from __future__ import annotations

# ── Экономика ────────────────────────────────────────────────────────────────
CLUB_PRICE_RUB = 990          # цена Клуба «Манифест» в месяц
FX_RUB_PER_USD = 90           # курс ₽/$ (грубо, для потолка трат)
CREDIT_USD = 0.048            # цена 1 HeyGen-кредита, $
CIRCLE_CREDITS = 8            # ≈ стоимость одного 16-сек Avatar V кружка, кредитов

# Потолок трат на лида как % от цены Клуба — чем горячее/ценнее трек, тем щедрее.
_TRACK_PCT = {
    "T1": 20,   # быстрый рез: тёплый, открытый, без стен — недорого доводим
    "T2": 30,   # тёплый думающий
    "T3": 45,   # сложный, но ценный — вкладываемся
    "T4": 60,   # кит — не жалеем
}


def budget_credits(track: str) -> int:
    """Потолок трат на лида этого трека, в HeyGen-кредитах.

    Переводит % от цены Клуба в кредиты:
        (CLUB_PRICE_RUB * pct/100) / FX_RUB_PER_USD / CREDIT_USD
    Неизвестный трек → потолок T2 (дефолт)."""
    pct = _TRACK_PCT.get(track, _TRACK_PCT["T2"])
    budget_usd = (CLUB_PRICE_RUB * pct / 100.0) / FX_RUB_PER_USD
    return int(budget_usd / CREDIT_USD)


def classify(signals: dict | None) -> str:
    """По сигналам {heat,open,resist,value} (0–3) → трек 'T1'..'T4'.

    Эвристика:
        readiness = heat + open - resist
      • value >= 3                     → 'T4' (кит: явно готова вложиться)
      • value >= 2 и resist >= 2       → 'T3' (сложная, но ценная — стоит усилий)
      • readiness >= 4 и resist <= 1   → 'T1' (быстрый рез: тёплая, открытая,
                                               без стен — доводим дёшево)
      • иначе                          → 'T2' (тёплая думающая — дефолт)

    Пустые/None сигналы → 'T2'.
    """
    if not signals:
        return "T2"
    heat = int(signals.get("heat") or 0)
    open_ = int(signals.get("open") or 0)
    resist = int(signals.get("resist") or 0)
    value = int(signals.get("value") or 0)

    readiness = heat + open_ - resist

    if value >= 3:
        return "T4"
    if value >= 2 and resist >= 2:
        return "T3"
    if readiness >= 4 and resist <= 1:
        return "T1"
    return "T2"


def remaining_budget_credits(track: str, credits_spent_so_far: int) -> int:
    """Сколько кредитов ещё в рамках потолка трека (не меньше 0)."""
    return max(0, budget_credits(track) - int(credits_spent_so_far or 0))


def should_spend_circle(signals: dict | None, track: str,
                        credits_spent_so_far: int) -> bool:
    """Ворота эскалации: тратить персональный кружок ТОЛЬКО если лид достаточно
    горяч/ценен И это влезает в потолок бюджета трека.

    Тратим, когда (heat >= 2 ИЛИ value >= 2)
        И credits_spent_so_far + CIRCLE_CREDITS <= budget_credits(track).
    Иначе False (холодная/шинник/превышение бюджета → остаётся на бесплатном слое).
    """
    if not signals:
        return False
    heat = int(signals.get("heat") or 0)
    value = int(signals.get("value") or 0)
    if heat < 2 and value < 2:
        return False
    spent = int(credits_spent_so_far or 0)
    return spent + CIRCLE_CREDITS <= budget_credits(track)
