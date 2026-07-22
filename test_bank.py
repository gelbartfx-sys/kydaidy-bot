"""Self-check спины сквозного Банка 5:1 (Шаг 1): resolve_couple / bank_add /
get_bank / merge_banks / render_bank_card. Без фреймворков — assert под __main__.

DB-изоляция как в test_e2e_path.py: свежий tmp-SQLite (init_db докатывает couples
и bank_ledger из SCHEMA). На проде тот же SQL идёт в D1 через _exec — логика одна.

Запуск: python3 test_bank.py
"""

import asyncio
import os
import tempfile

# config/handlers могут требовать env при импорте цепочки database — не тянем их,
# database.py импортируется чисто (aiohttp/aiosqlite), но подстрахуемся.
os.environ.setdefault("TG_BOT_TOKEN", "test:token")
os.environ.setdefault("TG_ADMIN_ID", "0")

import database


async def _boot_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="bank_")
    os.close(fd)
    os.unlink(path)              # пусть sqlite создаст чистый файл
    database.DB_PATH = path      # _exec/get_db читают модульный атрибут в рантайме
    database.USE_D1 = False      # тест на локальном sqlite-бэкенде
    await database.init_db()
    return path


async def _main():
    await _boot_db()

    # ── resolve_couple: соло → свой tg_id, partner_id NULL ───────────────────
    solo = 1001
    cid = await database.resolve_couple(solo)
    assert cid == solo, f"соло couple_id должен быть свой tg_id, got {cid}"
    row = await database._exec(
        "SELECT partner_id FROM couples WHERE couple_id = ?", (solo,), fetch="one")
    assert row is not None and row.get("partner_id") is None, f"соло partner_id NULL, got {row}"

    # ── resolve_couple: с pair_src → парный couple_id = инициатор ─────────────
    initiator, partner = 2001, 2002
    await database.atm_save_result(partner, "{}", "{}", "teplo", pair_src=initiator)
    cid_p = await database.resolve_couple(partner)
    assert cid_p == initiator, f"парный couple_id = pair_src, got {cid_p}"
    row = await database._exec(
        "SELECT partner_id FROM couples WHERE couple_id = ?", (initiator,), fetch="one")
    assert row is not None and row.get("partner_id") == partner, f"partner_id проставлен, got {row}"

    # ── bank_add идемпотентность: дважды один (couple,kind,day_key) → plus=1 ──
    c = 3001
    first = await database.bank_add(c, "checkin", +1, day_key="2026-07-22")
    dup = await database.bank_add(c, "checkin", +1, day_key="2026-07-22")
    assert first is True, "первый add реальный"
    assert dup is False, "повтор за тот же день — не дубль (idempotent)"
    b = await database.get_bank(c)
    assert b["plus"] == 1, f"идемпотентность: plus=1, got {b}"
    ledger_n = await database._exec(
        "SELECT COUNT(*) AS n FROM bank_ledger WHERE couple_id = ?", (c,), fetch="one")
    assert int(ledger_n["n"]) == 1, f"в ledger ровно 1 строка, got {ledger_n}"

    # ── get_bank ratio_str '4:1' на 4 плюса + 1 минус ────────────────────────
    r = 4001
    for i in range(4):
        await database.bank_add(r, f"plus{i}", +1, day_key="2026-07-22")
    await database.bank_add(r, "minus0", -1, day_key="2026-07-22")
    b = await database.get_bank(r)
    assert b["plus"] == 4 and b["minus"] == 1, f"4 плюса / 1 минус, got {b}"
    assert b["ratio_str"] == "4:1", f"ratio_str '4:1', got {b['ratio_str']!r}"
    assert b["turns"] == 4, f"turns=plus, got {b}"
    assert 0.0 <= b["progress_to_5"] <= 1.0

    # minus=0 → 'N:0', без деления на ноль
    z = 4002
    await database.bank_add(z, "checkin", +3, day_key="2026-07-22")
    bz = await database.get_bank(z)
    assert bz["ratio_str"] == "3:0", f"minus=0 → 'N:0', got {bz['ratio_str']!r}"
    assert bz["progress_to_5"] == 1.0, f"minus=0,plus>0 → progress 1.0, got {bz}"
    # сокращение дроби: 10:2 → '5:1'
    q = 4003
    await database.bank_add(q, "a", +10, day_key="2026-07-22")
    await database.bank_add(q, "b", -2, day_key="2026-07-22")
    assert (await database.get_bank(q))["ratio_str"] == "5:1"

    # ── merge_banks: два соло → суммы в canonical, other пуст ─────────────────
    a, bb = 5001, 5002
    await database.bank_add(a, "x", +2, day_key="2026-07-22")
    await database.bank_add(a, "y", +1, day_key="2026-07-22")
    await database.bank_add(bb, "z", +2, day_key="2026-07-22")
    await database.bank_add(bb, "w", -1, day_key="2026-07-22")
    await database._exec("INSERT OR IGNORE INTO couples (couple_id) VALUES (?)", (bb,))
    await database.merge_banks(a, bb)
    ba = await database.get_bank(a)
    assert ba["plus"] == 5 and ba["minus"] == 1, f"суммы сложились в canonical, got {ba}"
    other = await database.get_bank(bb)
    assert other["plus"] == 0 and other["minus"] == 0, f"other-банк пуст, got {other}"
    gone = await database._exec(
        "SELECT 1 FROM couples WHERE couple_id = ?", (bb,), fetch="one")
    assert gone is None, "соло-строка couples(other) удалена"
    part = await database._exec(
        "SELECT partner_id FROM couples WHERE couple_id = ?", (a,), fetch="one")
    assert part and part.get("partner_id") == bb, f"partner_id=other, got {part}"
    # идемпотентность merge: повтор — no-op, суммы не меняются
    await database.merge_banks(a, bb)
    assert (await database.get_bank(a))["plus"] == 5, "повтор merge не задваивает"

    # merge с коллизией kind+day_key: обе стороны сделали один kind в один день →
    # не задваивается и обе не теряются (canonical-строка остаётся).
    m1, m2 = 6001, 6002
    await database.bank_add(m1, "checkin", +1, day_key="2026-07-22")
    await database.bank_add(m2, "checkin", +1, day_key="2026-07-22")
    await database.merge_banks(m1, m2)
    bm = await database.get_bank(m1)
    assert bm["plus"] == 1, f"коллизия kind+day_key даёт 1 строку, got {bm}"

    # ── render_bank_card: единый текст, содержит цифру и N поворотов ──────────
    card = await database.render_bank_card(r)  # r = банк 4:1
    assert "4:1" in card and "Поворотов-к: 4" in card, f"карточка: {card!r}"
    assert "5:1" in card, "прогресс к якорю 5:1 упомянут"

    print("test_bank OK: resolve_couple (соло/пара) + bank_add идемпотентность + "
          "get_bank ratio '4:1'/'N:0'/сокращение + merge_banks (суммы/коллизия/"
          "идемпотентность) + render_bank_card")


if __name__ == "__main__":
    asyncio.run(_main())
