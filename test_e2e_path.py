"""E2E-симуляция пути клиента AI-Алёны (мандат Кая 06.07: «путь клиента должен
проходить у нас в симуляции, а не ломаться на владельце»).

Интеграционный тест МЕХАНИКИ воронки БЕЗ СЕТИ: реальная SQLite (tmp-файл), а все
внешние вызовы — моки:
  • brain_turn / respond          — скриптованные ответы «мозга» (без LLM);
  • send_voice_reply / _to        — записывают (text, kbd), возвращают True/False;
  • send_kruzhok_to               — записывает кружки, возвращает True/False;
  • _transcribe_voice             — управляемый сбой;
  • bot / message                 — лёгкие фейки, копящие отправленное.

7 сценариев = сегодняшний боевой провал воронки, каждый — отдельный тест.
Паттерн как в test_brain.py: sync-обёртка + asyncio.run(_scenario()), чтобы файл
шёл и через pytest, и через `python3 test_e2e_path.py` (pytest_asyncio не нужен).

Запуск: python3 -m pytest test_e2e_path.py -q
"""

from __future__ import annotations

import asyncio
import os
import tempfile

# config.Settings() требует эти env при импорте (config через alena_chat). Сети нет.
os.environ.setdefault("TG_BOT_TOKEN", "test:token")
os.environ.setdefault("TG_ADMIN_ID", "0")

import database
import alena_chat
import alena_brain
from config import settings
from alena_chat import (
    _talk, run_dead_session_tick, run_orphan_turn_tick,
    on_alena_voice, on_after_offer,
    _OFFER_TEASER, CLUB_URL, ONE_ON_ONE_URL,
)
from alena_persona import CLOSE_MARK

_ADMIN_ID = 424242


# ── Лёгкие фейки Telegram ─────────────────────────────────────────────────────
class _FakeBot:
    def __init__(self):
        self.messages = []   # (chat_id, text, reply_markup)
        self.videos = []     # (chat_id, video)
        self.actions = []    # (chat_id, action)

    async def send_message(self, chat_id, text=None, reply_markup=None,
                           parse_mode=None, **kw):
        self.messages.append((chat_id, text, reply_markup))

    async def send_video(self, chat_id, video=None, **kw):
        self.videos.append((chat_id, video))

    async def send_chat_action(self, chat_id, action=None, **kw):
        self.actions.append((chat_id, action))


class _User:
    def __init__(self, uid, first_name="Аня"):
        self.id = uid
        self.first_name = first_name


class _Chat:
    def __init__(self, cid):
        self.id = cid


class _FakeMessage:
    def __init__(self, uid, text="", bot=None, first_name="Аня"):
        self.from_user = _User(uid, first_name)
        self.chat = _Chat(uid)
        self.text = text
        self.bot = bot or _FakeBot()
        self.voice = None
        self.video_note = None
        self.answers = []    # (text, reply_markup)

    async def answer(self, text, parse_mode=None, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))


# ── Рекордер голоса/кружка (моки alena_voice в неймспейсе alena_chat) ──────────
class _VoiceRec:
    def __init__(self):
        self.voice_reply = []   # (text, kbd)
        self.voice_to = []      # (chat_id, text, kbd)
        self.kruzhok = []       # (chat_id, text)
        self.voice_reply_ok = True
        self.voice_to_ok = True
        self.kruzhok_ok = True

    async def svr(self, message, text, reply_markup=None, protected=False):
        self.voice_reply.append((text, reply_markup))
        return self.voice_reply_ok

    async def svt(self, bot, chat_id, text, reply_markup=None, protected=False):
        self.voice_to.append((chat_id, text, reply_markup))
        return self.voice_to_ok

    async def skt(self, bot, chat_id, text, protected=False):
        self.kruzhok.append((chat_id, text))
        return self.kruzhok_ok


# ── Патч/восстановление модульных атрибутов ───────────────────────────────────
_PATCHES: list = []


def _patch(obj, name, val):
    _PATCHES.append((obj, name, getattr(obj, name)))
    setattr(obj, name, val)


def _unpatch():
    while _PATCHES:
        obj, name, old = _PATCHES.pop()
        setattr(obj, name, old)


def _install_voice(rec: _VoiceRec):
    _patch(alena_chat, "send_voice_reply", rec.svr)
    _patch(alena_chat, "send_voice_to", rec.svt)
    _patch(alena_chat, "send_kruzhok_to", rec.skt)


async def _noop(*a, **kw):
    return None


def _scripted_brain(replies, cms=None):
    """brain_turn-мок: пo вызову отдаёт следующий (reply, cm) из скрипта."""
    st = {"n": 0}

    async def _bt(history, name, archetype, cm, profile=None, **kw):
        i = st["n"]
        st["n"] += 1
        reply = replies[min(i, len(replies) - 1)]
        new_cm = (cms[i] if cms and i < len(cms) else {}) or {}
        return reply, new_cm, None, None
    return _bt


# ── DB-изоляция: свежий tmp-SQLite на сценарий ────────────────────────────────
async def _boot_db(uid, first_name="Аня"):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="e2e_")
    os.close(fd)
    os.unlink(path)          # пусть sqlite создаст чистый файл
    database.DB_PATH = path  # _exec/get_db читают модульный атрибут в рантайме
    await database.init_db()
    await database.upsert_user(uid, "user", first_name)
    return path


async def _drain():
    """Догнать фоновые create_task (offer-кружок, зеркало аналитики)."""
    for _ in range(20):
        pend = [t for t in asyncio.all_tasks()
                if t is not asyncio.current_task() and not t.done()]
        if not pend:
            break
        await asyncio.gather(*pend, return_exceptions=True)
        await asyncio.sleep(0)


# ── Утилиты по клавиатурам ────────────────────────────────────────────────────
def _doors(markup):
    """(set урлов, set callback_data) из InlineKeyboardMarkup | None."""
    urls, cbs = set(), set()
    if markup is None:
        return urls, cbs
    for row in markup.inline_keyboard:
        for b in row:
            if getattr(b, "url", None):
                urls.add(b.url)
            if getattr(b, "callback_data", None):
                cbs.add(b.callback_data)
    return urls, cbs


def _all_markups(rec: _VoiceRec, bot: _FakeBot, *msgs):
    out = [kbd for _, kbd in rec.voice_reply]
    out += [kbd for _, _, kbd in rec.voice_to]
    out += [kbd for _, _, kbd in bot.messages]
    for m in msgs:
        out += [kbd for _, kbd in m.answers]
    return out


async def _sql(q, params=()):
    return await database._exec(q, params, fetch="one")


async def _count_followups(uid):
    row = await _sql("SELECT COUNT(*) AS n FROM followups WHERE tg_id = ?", (uid,))
    return int((row or {}).get("n") or 0)


async def _count_events(uid, event):
    return await database.events_count_recent(uid, event, hours=24 * 365)


# ══════════════════════════════════════════════════════════════════════════════
# Сценарий 1 — полный путь до денег: 3 хода → закрытие с CLOSE_MARK+[[ЗАПРОС]]
# ══════════════════════════════════════════════════════════════════════════════
def test_full_path_to_money():
    async def _run():
        uid = 1001
        await _boot_db(uid)
        _patch(settings, "brain_v2_enabled", True)
        _patch(settings, "gemini_key", "test-key")
        rec = _VoiceRec()
        _install_voice(rec)
        _patch(alena_chat, "_nudge_channel", _noop)  # не тянем handlers/сеть
        replies = [
            "Слышу тебя. Что сейчас на самом деле происходит внутри?",
            "Ты держишь это одна. Как давно?",
            "Под этим — усталость быть сильной. Так?",
            "Ты услышана, этого сейчас достаточно. "
            "[[ЗАПРОС: боюсь близости, поэтому выбираю недоступных]] " + CLOSE_MARK,
        ]
        cms = [
            {"method_phase": "surface_facade"},
            {"method_phase": "catch_contradiction"},
            {"method_phase": "give_shift"},
            {"method_phase": "name_true_request",
             "true_request_hypothesis": "боюсь близости"},
        ]
        _patch(alena_chat, "brain_turn", _scripted_brain(replies, cms))
        await database.ai_open_session(uid)
        bot = _FakeBot()
        for i in range(4):
            msg = _FakeMessage(uid, text=f"реплика {i}", bot=bot)
            await _talk(msg, msg.text)
        await _drain()

        # (1) встреча закрыта
        assert await database.ai_active_session(uid) is None, "сессия не закрылась"
        # (2) тизер оффера ушёл (голосом) С клавиатурой
        assert any(t == _OFFER_TEASER for t, _ in rec.voice_reply), "тизер не отправлен"
        # (3) оффер-кружок реально собран (_offer_kruzhok прошёл насквозь)
        assert rec.kruzhok, "оффер-кружок не отправлен"
        # (4) карточка под кружком ушла текстом с клавиатурой
        assert any(t == alena_chat._OFFER_CARD for _, t, _ in bot.messages), \
            "карточка оффера не отправлена"
        # (5) в клавиатуре — обе двери (Клуб+1:1) и callback alena:more
        markups = _all_markups(rec, bot)
        full = [m for m in markups if m is not None
                and {CLUB_URL, ONE_ON_ONE_URL} <= _doors(m)[0]
                and "alena:more" in _doors(m)[1]]
        assert full, "нет клавиатуры с Клуб+1:1+alena:more"
        # (6) followup-серия поставлена
        assert await _count_followups(uid) > 0, "followups не поставлены"
    try:
        asyncio.run(_run())
    finally:
        _unpatch()


# ══════════════════════════════════════════════════════════════════════════════
# Сценарий 2 — каждый ход зовёт дальше: констатация без вопроса → дошив побуждения
# ══════════════════════════════════════════════════════════════════════════════
def test_every_turn_invites_further():
    async def _run():
        uid = 1002
        await _boot_db(uid)
        _patch(settings, "brain_v2_enabled", True)
        _patch(settings, "gemini_key", "test-key")
        rec = _VoiceRec()
        _install_voice(rec)
        # Голая констатация: ни «?», ни императива в финале.
        flat = "Ты научилась справляться одна. Это стало твоей бронёй."
        _patch(alena_chat, "brain_turn", _scripted_brain([flat], [{}]))
        await database.ai_open_session(uid)
        bot = _FakeBot()
        msg = _FakeMessage(uid, text="мне тяжело", bot=bot)
        await _talk(msg, msg.text)
        await _drain()

        assert rec.voice_reply, "реплика не ушла голосом"
        sent = rec.voice_reply[-1][0]
        from alena_chat import _PROMPT_CUES
        assert ("?" in sent) or any(c in sent for c in _PROMPT_CUES), \
            f"ход кончился голой констатацией без побуждения: {sent!r}"
        assert sent != flat, "_ensure_prompt не дошил приглашение"
    try:
        asyncio.run(_run())
    finally:
        _unpatch()


# ══════════════════════════════════════════════════════════════════════════════
# Сценарий 3 — закрытие БЕЗ [[ЗАПРОС]] (force_close) → жёсткая доводка через cm
# ══════════════════════════════════════════════════════════════════════════════
def test_hard_close_reconstructs_request():
    async def _run():
        uid = 1003
        await _boot_db(uid)
        _patch(settings, "brain_v2_enabled", True)
        _patch(settings, "gemini_key", "test-key")
        rec = _VoiceRec()
        _install_voice(rec)
        _patch(alena_chat, "_nudge_channel", _noop)
        # Голый текст без маркеров; закрытие сорвёт force_close (turns==TURN_CAP).
        # Настоящий запрос живёт в модели клиентки (brain отдаёт cm, _talk её пишет),
        # не в reply-маркере — реконструкция обязана вытащить его в оффер-путь.
        _patch(alena_chat, "brain_turn", _scripted_brain(
            ["На сегодня побудь с тем, что поднялось."],
            [{"true_request_hypothesis": "боюсь близости"}]))
        sess = await database.ai_open_session(uid)
        # Подводим turns к TURN_CAP-1, чтобы этот ход стал force_close.
        await database._exec("UPDATE ai_sessions SET turns = ? WHERE id = ?",
                             (alena_chat.TURN_CAP - 1, sess["id"]))
        bot = _FakeBot()
        msg = _FakeMessage(uid, text="ну не знаю", bot=bot)
        await _talk(msg, msg.text)
        await _drain()

        assert await database.ai_active_session(uid) is None, "встреча не закрылась"
        # Оффер-путь, а НЕ мягкая ветка: кружок собран из реконструированного запроса.
        assert rec.kruzhok, "жёсткая доводка не собрала оффер-кружок (ушла в soft?)"
        # offer_shown НЕ мягкий (club_soft = провал доводки).
        row = await database._exec(
            "SELECT meta FROM funnel_events WHERE tg_id=? AND event='offer_shown' "
            "ORDER BY id DESC LIMIT 1", (uid,), fetch="one")
        assert row and row.get("meta") != "club_soft", \
            f"доводка сорвалась в мягкую ветку: {row}"
        # В клавиатуре присутствует дверь 1:1 (оффер-путь), а не только Клуб.
        markups = _all_markups(rec, bot)
        assert any(ONE_ON_ONE_URL in _doors(m)[0] for m in markups), \
            "нет двери 1:1 — похоже на мягкое закрытие"
    try:
        asyncio.run(_run())
    finally:
        _unpatch()


# ══════════════════════════════════════════════════════════════════════════════
# Сценарий 4 — мёртвая встреча закрывается с оффером (run_dead_session_tick)
# ══════════════════════════════════════════════════════════════════════════════
def test_dead_session_closes_with_offer():
    async def _run():
        uid = 1004
        await _boot_db(uid)
        _patch(settings, "tg_admin_id", _ADMIN_ID)
        rec = _VoiceRec()
        _install_voice(rec)
        _patch(alena_chat, "_nudge_channel", _noop)
        sess = await database.ai_open_session(uid)
        sid = sess["id"]
        await database.ai_add_message(sid, uid, "user", "мне очень тяжело")
        await database.ai_add_message(sid, uid, "model", "Побудь с этим. Что чувствуешь?")
        # Диалог состоялся (turns), нудж уже слали, последняя реплика — от Алёны и старая.
        await database._exec("UPDATE ai_sessions SET turns = 3 WHERE id = ?", (sid,))
        await database.ai_mark_nudged(sid)
        await database._exec(
            "UPDATE ai_messages SET created_at = datetime('now','-40 minutes') "
            "WHERE session_id = ?", (sid,))
        await database.save_client_model(
            uid, '{"true_request_hypothesis": "боюсь близости"}')
        bot = _FakeBot()
        closed = await run_dead_session_tick(bot)
        await _drain()

        assert closed == 1, "мёртвая встреча не закрыта"
        assert await database.ai_active_session(uid) is None, "сессия осталась активной"
        assert await _count_events(uid, "session_died_silent") >= 1, "нет события смерти"
        # Оффер-тизер ушёл ВДОГОНКУ (фон → send_voice_to).
        assert any(t == _OFFER_TEASER for _, t, _ in rec.voice_to), "оффер не отправлен"
        assert rec.kruzhok, "оффер-кружок вдогонку не собран"
        assert await _count_followups(uid) > 0, "followups не поставлены"
        # Сирена админу.
        assert any(cid == _ADMIN_ID and t and "умерла" in t
                   for cid, t, _ in bot.messages), "нет алерта админу"
    try:
        asyncio.run(_run())
    finally:
        _unpatch()


# ══════════════════════════════════════════════════════════════════════════════
# Сценарий 5 — orphan-восстановление с текст-эхом (run_orphan_turn_tick)
# ══════════════════════════════════════════════════════════════════════════════
def test_orphan_recovery_with_echo():
    async def _run():
        uid = 1005
        await _boot_db(uid)
        _patch(settings, "brain_v2_enabled", True)
        rec = _VoiceRec()
        _install_voice(rec)
        question = "А что ты сейчас чувствуешь в теле?"
        _patch(alena_chat, "brain_turn",
               _scripted_brain([f"Я рядом. {question}"], [{}]))
        sess = await database.ai_open_session(uid)
        sid = sess["id"]
        # Последнее сообщение — от ЧЕЛОВЕКА, висит без ответа (редеплой убил ход).
        await database.ai_add_message(sid, uid, "user", "мне тяжело и пусто")
        await database._exec(
            "UPDATE ai_messages SET created_at = datetime('now','-5 minutes') "
            "WHERE session_id = ?", (sid,))
        bot = _FakeBot()

        healed = await run_orphan_turn_tick(bot)
        await _drain()
        assert healed == 1, "ход-сирота не восстановлен"
        # Ответ записан в ai_messages (последним — model).
        msgs = await database.ai_get_messages(sid, 40)
        assert msgs[-1]["role"] == "model", "ответ Алёны не записан"
        # Голос отправлен.
        assert any(cid == uid for cid, _, _ in rec.voice_to), "голос не отправлен"
        # ТЕКСТ-ЭХО вопроса отправлено (bot.send_message из _send_question_echo).
        assert any(t and question in t for _, t, _ in bot.messages), \
            "текст-эхо вопроса не отправлено"
        n_model = sum(1 for m in msgs if m["role"] == "model")

        # Повторный тик НЕ дублирует (последнее сообщение теперь model).
        healed2 = await run_orphan_turn_tick(bot)
        await _drain()
        assert healed2 == 0, "повторный тик задублировал ход"
        msgs2 = await database.ai_get_messages(sid, 40)
        assert sum(1 for m in msgs2 if m["role"] == "model") == n_model, \
            "ответ задублирован"
    try:
        asyncio.run(_run())
    finally:
        _unpatch()


# ══════════════════════════════════════════════════════════════════════════════
# Сценарий 6 — голос-вход не молчит при сбое расшифровки
# ══════════════════════════════════════════════════════════════════════════════
def test_voice_input_not_silent_on_failure():
    async def _run():
        uid = 1006
        await _boot_db(uid)
        _patch(settings, "gemini_key", "test-key")  # иначе ранняя «напиши текстом»

        async def _boom(bot, voice):
            raise RuntimeError("transcribe down")
        _patch(alena_chat, "_transcribe_voice", _boom)
        bot = _FakeBot()
        msg = _FakeMessage(uid, text="", bot=bot)
        msg.voice = object()

        await on_alena_voice(msg)
        # Сбой расшифровки → клиентке ушёл fallback-текст (не тишина).
        assert msg.answers, "клиентка осталась в тишине на сбое голоса"
        joined = " ".join(t or "" for t, _ in msg.answers)
        assert ("Не расслышала" in joined) or ("текстом" in joined), \
            f"нет внятного fallback на сбой голоса: {msg.answers!r}"
    try:
        asyncio.run(_run())
    finally:
        _unpatch()


# ══════════════════════════════════════════════════════════════════════════════
# Сценарий 7 — возражение trust → соц-пруф видео (один раз), обе двери
# ══════════════════════════════════════════════════════════════════════════════
def test_trust_objection_shows_soc_proof_once():
    async def _run():
        uid = 1007
        await _boot_db(uid)
        rec = _VoiceRec()
        _install_voice(rec)
        await database.set_meta("socproof_video_file_id", "X")
        await database.ai_set_last_request(uid, "боюсь близости")

        async def _fake_respond(*a, **kw):
            return "Слышу твоё сомнение. Что именно держит?"
        _patch(alena_brain, "respond", _fake_respond)

        bot = _FakeBot()
        msg = _FakeMessage(uid, text="а гарантии есть? точно поможет?", bot=bot)

        # 1-й раз: соц-пруф-видео показывается.
        await on_after_offer(msg)
        await _drain()
        assert ("X" in [v for _, v in bot.videos]), "соц-пруф видео не показано"
        assert await _count_events(uid, "soc_proof_shown") >= 1, "нет события soc_proof_shown"
        # Послесловие с обеими дверьми (_bridge_kbd: Клуб + 1:1).
        both = [kbd for _, kbd in rec.voice_reply
                if kbd is not None and {CLUB_URL, ONE_ON_ONE_URL} <= _doors(kbd)[0]]
        assert both, "послесловие соц-пруфа без обеих дверей"

        # 2-й раз: видео НЕ шлётся повторно (флаг soc_proof_shown).
        msg2 = _FakeMessage(uid, text="всё равно не верю, гарантии?", bot=bot)
        await on_after_offer(msg2)
        await _drain()
        assert len(bot.videos) == 1, "соц-пруф видео показано повторно"
    try:
        asyncio.run(_run())
    finally:
        _unpatch()


if __name__ == "__main__":
    test_full_path_to_money()
    test_every_turn_invites_further()
    test_hard_close_reconstructs_request()
    test_dead_session_closes_with_offer()
    test_orphan_recovery_with_echo()
    test_voice_input_not_silent_on_failure()
    test_trust_objection_shows_soc_proof_once()
    print("OK: e2e client path — full-to-money + invite-further + hard-close "
          "reconstruct + dead-session offer + orphan echo + voice-fail fallback + "
          "trust soc-proof once")
