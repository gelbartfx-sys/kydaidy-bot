"""Квалификация лида: стадия покупательской готовности cold→warm→hot.

Планёрка воронки 10.07. Корень продаж=0 — продаём холодным: ступени лестницы
Ханта 2-3 нет, а оффер меряет катарсис (offer_readiness), не коммерческую
готовность. Этот модуль вводит ОТДЕЛЬНУЮ ось «покупательской температуры» и
гейт, который не даёт показать цену раньше, чем лид дозрел.

ВАЖНО: purchase_stage ≠ lead_track (T1-T4). lead_track = бюджет HeyGen-кредитов
на кружки («сколько тратим»). purchase_stage = «можно ли продавать». Не путать.

Гейт FAIL-OPEN: любая неопределённость (None/ошибка/флаг-off) → разрешить оффер.
Флаг purchase_stage_gate_enabled OFF по умолчанию → байт-в-байт как сейчас.
"""
import logging

from config import settings
from alena_persona import METHOD_PHASES
import database as db

logger = logging.getLogger(__name__)

STAGES = ("cold", "warm_entry", "warm_qualified", "hot")
# Индекс фазы, с которой считаем эмоциональное ядро пройденным (истинный запрос
# назван). Ниже — ещё «холодно» по методу.
_QUALIFY_PHASE_IDX = METHOD_PHASES.index("name_true_request")  # = 3


def compute_stage(*, total_sessions: int, turns_this_session: int,
                  method_phase: str | None, offer_readiness: float | None,
                  subscribe_confirmed: bool, lead_heat: int | None,
                  buy_click: bool, has_objection: bool,
                  lead_track: str | None) -> str:
    """Чистая функция (без БД/сети, тестируема). Сырые сигналы → одна из STAGES.

    HOT — прямой коммерческий сигнал (клик покупки / уже торгуется / горячий трек).
    WARM_QUALIFIED — метод дошёл до истинного запроса И offer_readiness≥0.5 И хотя
      бы одно микро-обязательство (подписка / повторный визит / heat≥2).
    WARM_ENTRY — встреча состоялась (turns≥1 в этой сессии или была раньше).
    COLD — иначе (в т.ч. никогда не встречалась).
    """
    if buy_click or has_objection or (lead_track in ("T3", "T4")):
        return "hot"

    phase_idx = METHOD_PHASES.index(method_phase) if method_phase in METHOD_PHASES else -1
    readiness = offer_readiness or 0.0
    micro_commit = bool(subscribe_confirmed) or total_sessions >= 2 or (lead_heat or 0) >= 2
    if phase_idx >= _QUALIFY_PHASE_IDX and readiness >= 0.5 and micro_commit:
        return "warm_qualified"

    if turns_this_session >= 1 or total_sessions >= 1:
        return "warm_entry"

    return "cold"


async def refresh_purchase_stage(tg_id: int) -> str | None:
    """Собрать сигналы (крэш-сейф) → compute_stage → записать. Возвращает стадию
    или None при сбое. Событийный вызов (после хода диалога / подписки / клика
    покупки) — без cron, как save_lead_signals/set_lead_track."""
    try:
        total = await db.ai_total_sessions(tg_id)
        cm = await db.get_client_model(tg_id) or {}
        sig = await db.get_lead_signals(tg_id) or {}
        # turns текущей активной сессии (если есть) — для WARM_ENTRY.
        turns = 0
        try:
            sess = await db.ai_active_session(tg_id)
            turns = int((sess or {}).get("turns") or 0)
        except Exception:
            turns = 0
        subscribed = await db.events_count_recent(tg_id, "subscribe_confirmed", hours=24 * 365) > 0
        bought = await db.events_count_recent(tg_id, "buy_click", hours=24 * 365) > 0
        objected = await db.events_count_recent(tg_id, "objection", hours=48) > 0
        stage = compute_stage(
            total_sessions=total,
            turns_this_session=turns,
            method_phase=cm.get("method_phase"),
            offer_readiness=cm.get("offer_readiness"),
            subscribe_confirmed=subscribed,
            lead_heat=sig.get("lead_heat"),
            buy_click=bought,
            has_objection=objected,
            lead_track=sig.get("lead_track"),
        )
        await db.set_purchase_stage(tg_id, stage)
        return stage
    except Exception:
        logger.warning("refresh_purchase_stage failed (continuing)", exc_info=True)
        return None


def _whitelist_ids() -> set[int]:
    out: set[int] = set()
    for part in str(settings.purchase_gate_whitelist or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    if settings.tg_admin_id:
        out.add(int(settings.tg_admin_id))
    return out


async def stage_allows_offer(tg_id: int) -> bool:
    """True — можно показывать цену/оффер-кнопку. FAIL-OPEN во всех сомнениях.

    Порядок: флаг OFF → True; whitelist → True; ошибка чтения → True; стадия
    неизвестна → True (+лог); стадия < warm_qualified → False; если требуем ступень
    сравнения и её нет → False; иначе True.
    """
    if not settings.purchase_stage_gate_enabled:
        return True
    try:
        if tg_id in _whitelist_ids():
            return True
        stage = await db.get_purchase_stage(tg_id)
        if stage is None:
            try:
                await db.log_event(tg_id, "gate_unknown_stage_allowed")
            except Exception:
                pass
            return True
        if stage not in ("warm_qualified", "hot"):
            return False
        if not settings.comparison_step_required:
            return True
        return await db.events_count_recent(tg_id, "hunt_comparison_shown", hours=24 * 365) > 0
    except Exception:
        logger.warning("stage_allows_offer failed → fail-open", exc_info=True)
        return True
