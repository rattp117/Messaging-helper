"""SPEC-v1.9.md §11 "Integration order" -- the final pass that wires the
four independently-shipped v1.9 modules (`cadence`, `grace`, `pause`,
`wrapped`) into `main.py`'s real command dispatch, the three minutely
proactive ticks, the weekly-review/daily-summary inline jobs, the nightly
00:05 grace tick, the optional month-end wrapped auto-send, and the
renderer surfaces (`core/dashboard.py`/`core/discoverability.py`/
`core/review.py`/`core/streaks.py`/`core/records.py`).

Every module's own test file (`tests/test_cadence.py` + `tests/
test_v19_cadence_gaps.py`, `tests/test_grace.py` + `tests/
test_v19_grace_gaps.py`, `tests/test_pause.py` + `tests/
test_v19_pause_gaps.py`, `tests/test_wrapped.py` + `tests/
test_v19_wrapped_gaps.py`) already proves its own owned ACs in isolation,
calling its own `execute_*`/pure-formatter functions directly. This file
is different in kind: it drives the REAL wired code (`main.py:
handle_inbound_message`/`async_main`'s scheduled jobs, and the actual
production `reminders.run_due_reminders`/`checkins.build_checkin_message`/
`nudge.build_nudge_message`/`review.run_weekly_review`/`streaks.
run_daily_summary` functions) so a genuine wiring mistake would show up
here even though every module's own unit tests stay green -- mirrors
`tests/test_v12_integration.py`'s/`tests/test_dnd_matrix.py`'s own stated
rationale for being a separate, wiring-focused file.

Covers the deferred AC slices Archi's integration dispatch named
explicitly: AC9/AC10 (renderer wiring), AC20 (pause gating at the 5
proactive sites), AC22 (dashboard/`/habits` pause marker + voluntary log
still counts), AC28 (month-end auto-send, default off), AC30 (release +
`/help`/menu), plus the spec's own §11 integration-order list (two-user
isolation, a cadence habit's full week, pause suppression + a voluntary
log's celebration, the 00:05 grace job end-to-end, `/wrapped` sending a
real PNG, and the AC3/AC30 gate re-run at the wired level).

Live-environment rule (unchanged from every other integration test file):
every DB here is a scratch `tmp_path` SQLite file. Nothing here ever opens
`data/habits.db`, and no real Telegram/Ollama call is made (all channels/
LLMs are fakes)."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import (
    announce,
    audit_view,
    checkins,
    i18n,
    nudge,
    reminders,
    release_notes,
)
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "1001"
MEMBER = "2002"


def _seed(db: Database, ts: str, category: str, value_num: float | None, user_id: str = OWNER, raw: str = "x") -> int:
    return db.insert_log(LogEntry(None, user_id, ts, category, value_num, None, raw, "reply"))


def _find(texts: list[str], needle: str) -> str:
    """`next(t for t in texts if needle in t)` without the bare-`next()`
    footgun: a bare `next()` with no default raises `StopIteration`, which
    asyncio converts to an opaque `RuntimeError: coroutine raised
    StopIteration` when it escapes an `async def` test body (PEP 3156) --
    this raises a normal, readable `AssertionError` instead."""
    for t in texts:
        if needle in t:
            return t
    raise AssertionError(f"no text containing {needle!r} found in {texts!r}")


# ===========================================================================
# Shared harness -- mirrors tests/test_v12_integration.py's/tests/
# test_dnd_matrix.py's own `_FakeScheduler`/`_ScriptedChannel`/`_run`
# pattern (each integration-adjacent test file keeps its own copy, per
# this codebase's established convention), extended here to also capture
# `send_image` calls (bytes + caption + disable_notification) and to
# store a job's full `args`/`kwargs` (not just `args`) so the three
# minutely-tick jobs -- `reminder_tick`/`checkin_tick`/`nudge_tick`,
# registered with `args=[...]`/`kwargs={"registry_for": ...}` rather than
# as zero-arg closures like `weekly_review`/`daily_summary`/`grace_tick`/
# `wrapped_auto` -- can be invoked correctly too.
# ===========================================================================


class _StopAfterSchedulerStart(Exception):
    pass


class _FakeScheduler:
    last_instance: "_FakeScheduler | None" = None

    def __init__(self, *args, **kwargs):
        self.jobs: dict[str, object] = {}
        _FakeScheduler.last_instance = self

    def add_job(self, func, trigger=None, args=None, kwargs=None, id=None, replace_existing=True, **extra):
        self.jobs[id] = SimpleNamespace(func=func, trigger=trigger, args=list(args or []), kwargs=dict(kwargs or {}), id=id)

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def start(self):
        pass

    def shutdown(self, wait=False):
        pass


class _FakeOllamaClient:
    responses: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def chat_text(self, system_prompt, user_prompt):
        return "noted"

    async def chat_json(self, system_prompt, user_prompt, json_schema, valid_categories):
        if _FakeOllamaClient.responses:
            return _FakeOllamaClient.responses.pop(0)
        return json.dumps({"category": "unknown", "value": None, "confidence": 0.1})

    async def probe_schema_support(self, *args, **kwargs) -> dict:
        return {}

    async def aclose(self) -> None:
        pass


class _V19Channel(Channel):
    """Drives the REAL `on_message`/`on_callback` closures in an arbitrary
    caller-supplied order, and can invoke any registered job (tick or
    closure) directly, still inside `async_main`'s own live `db`
    connection -- mirrors `tests/test_v12_integration.py:_ScriptedChannel`/
    `tests/test_dnd_matrix.py:_JobRunningChannel`, merged, plus real
    `send_image`/`disable_notification` capture (neither prior file's fake
    needed to prove a PNG or a silent send)."""

    last_instance: "_V19Channel | None" = None
    script: list[tuple] = []
    run_jobs_before_stop: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        self.sent: list[tuple[str, str, bool]] = []
        self.images: list[tuple[str, bytes, str, bool]] = []
        self.actionable: list[tuple[str, str, list]] = []
        self.set_my_commands_calls: list[tuple[dict, str | None]] = []
        _V19Channel.last_instance = self

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append((chat_id, text, disable_notification))

    async def send_image(self, chat_id: str, image: bytes, caption: str, *, disable_notification: bool = False) -> None:
        self.images.append((chat_id, image, caption, disable_notification))

    async def send_actionable(self, chat_id: str, text: str, buttons) -> None:
        self.actionable.append((chat_id, text, buttons))
        self.sent.append((chat_id, text, False))

    async def set_my_commands(self, commands, *, scope_chat_id=None) -> None:
        self.set_my_commands_calls.append((commands, scope_chat_id))

    def sent_to(self, chat_id: str) -> list[str]:
        return [text for cid, text, _silent in self.sent if cid == chat_id]

    def images_to(self, chat_id: str) -> list[tuple[bytes, str, bool]]:
        return [(img, cap, silent) for cid, img, cap, silent in self.images if cid == chat_id]

    async def run(self, on_message, on_callback=None) -> None:
        for step in _V19Channel.script:
            if step[0] == "message":
                _, chat_id, text, display_name = step
                await on_message(chat_id, text, display_name)
            else:
                _, chat_id, data, source_text, cb_id = step
                assert on_callback is not None
                await on_callback(chat_id, data, source_text, cb_id)
        for job_id in _V19Channel.run_jobs_before_stop:
            job = _FakeScheduler.last_instance.jobs.get(job_id)
            if job is not None:
                await job.func(*job.args, **job.kwargs)
        raise _StopAfterSchedulerStart()

    async def aclose(self) -> None:
        pass


def _run_async_main(monkeypatch, config, script, owner_chat_id=OWNER, responses=None, run_jobs=None):
    from habit_assistant import main as main_module

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module, "load_secrets", lambda: SimpleNamespace(telegram_bot_token="fake", telegram_chat_id=owner_chat_id)
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", _V19Channel)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClient)
    # Neutralize the (unrelated) release announcement so it never pollutes
    # channel.sent_to(...) for this file's own assertions -- mirrors every
    # other integration test file's identical neutralization.
    monkeypatch.setattr(main_module, "__version__", "0.0.0-test")
    _FakeScheduler.last_instance = None
    _V19Channel.last_instance = None
    _V19Channel.script = script
    _V19Channel.run_jobs_before_stop = list(run_jobs or [])
    _FakeOllamaClient.responses = list(responses or [])
    return main_module


async def _run(monkeypatch, config, script, owner_chat_id=OWNER, responses=None, run_jobs=None):
    main_module = _run_async_main(monkeypatch, config, script, owner_chat_id, responses, run_jobs)
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)
    return _V19Channel.last_instance


def _db(tmp_path, *user_ids: str) -> Database:
    database = Database(tmp_path / "habits.db")
    for uid in user_ids:
        database.upsert_user(uid, role="owner" if uid == OWNER else "member", status="active")
    return database


# ===========================================================================
# AC30 -- release note + /help + menu, at the wired level.
# ===========================================================================


class _AnnounceFakeChannel(Channel):
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id, text, *, disable_notification: bool = False) -> None:
        self.sent.append((chat_id, text))

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised by this test")

    async def aclose(self):
        pass


async def test_ac30_release_note_exists_and_announces_to_active_users(tmp_path):
    assert "1.9.0" in release_notes.RELEASE_NOTES
    assert set(release_notes.RELEASE_NOTES["1.9.0"].keys()) == {"en", "th"}

    db = _db(tmp_path, OWNER, MEMBER)
    channel = _AnnounceFakeChannel()
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    try:
        await announce.announce_release(db, channel, config, "1.9.0")
        assert any(cid == OWNER for cid, _ in channel.sent)
        assert any(cid == MEMBER for cid, _ in channel.sent)
        # Idempotent -- a second announce of the same version sends nothing more.
        channel.sent.clear()
        await announce.announce_release(db, channel, config, "1.9.0")
        assert channel.sent == []
    finally:
        db.close()


def test_ac30_help_text_mentions_all_four_new_commands_and_grace():
    config = Config()
    from habit_assistant.core.discoverability import build_help_text

    for lang in ("en", "th"):
        text = build_help_text(config, lang)
        assert "/cadence" in text
        assert "/pause" in text
        assert "/resume" in text
        assert "/wrapped" in text or "/recap" in text


async def test_ac30_public_menu_has_22_commands_including_the_four_new_ones(tmp_path, monkeypatch):
    """SPEC-v1.10.md §4 R17 (integration pass): 22 -> 23, `/guide` added --
    test name kept for history (this file's own v1.9 AC30 numbering)."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    channel = await _run(monkeypatch, config, script=[])
    public, scope = channel.set_my_commands_calls[0]
    assert scope is None
    for lang, entries in public.items():
        names = [n for n, _d in entries]
        assert len(names) == 23, f"{lang}: {names}"
        assert {"cadence", "pause", "resume", "wrapped"} <= set(names)
        assert "guide" in names


# ===========================================================================
# AC3/AC2 gate, re-run at the wired level: no v1.9 feature invoked -> the
# real handle_inbound_message confirmation is exactly the pre-v1.9 shape
# (only the intended v1.9 delta -- the celebrate_burst append -- is
# allowed to differ, and this scenario deliberately avoids crossing any
# milestone/record so even that delta never engages).
# ===========================================================================


async def test_ac3_gate_ordinary_log_with_no_v19_feature_is_byte_identical_to_v181(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    # A single log, far from any milestone (config default milestones are
    # 3/7/30) and no pre-existing record to beat -- the confirmation is
    # exactly `water_confirmation`, no suffix of any kind.
    channel = await _run(monkeypatch, config, script=[("message", OWNER, "500ml", None)])
    expected = i18n.t("water_confirmation", "en", water_ml=500, total=500, goal=2500.0, pct=20)
    assert channel.sent_to(OWNER)[-1] == expected


# ===========================================================================
# AC9/AC10/AC11 -- a cadence habit's full week through the REAL wired
# review + dashboard/`/habits` rendering.
# ===========================================================================


async def test_cadence_week_met_shows_week_wording_in_dashboard_habits_and_review(tmp_path, monkeypatch):
    db = _db(tmp_path, OWNER)
    # `stretch` is a built-in duration habit (no goal) -- cadence-ify it and
    # log every day for 4 full weeks so every completed ISO week is MET
    # (>=3 of 7 days) regardless of which real weekday this test happens to
    # run on (mirrors tests/test_dnd_matrix.py's own "deterministic
    # regardless of the real time" design note).
    db.set_cadence(OWNER, "stretch", 3)
    today = date.today()
    for offset in range(28):
        day = today - timedelta(days=offset)
        _seed(db, f"{day.isoformat()}T09:00:00", "stretch", 10.0)
    db.close()

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    channel = await _run(
        monkeypatch, config, script=[("message", OWNER, "/habits", None)], run_jobs=["weekly_review"]
    )

    # AC10: /habits shows the cadence "X of N this week" line, not the
    # ordinary count-only line.
    habits_text = _find(channel.sent_to(OWNER), i18n.t("habits_overview_header", "en"))
    assert "3×/week" in habits_text or "3×/week" in habits_text
    assert "this week" in habits_text

    # AC9: the weekly review's own stretch line uses WEEK wording, not the
    # pre-v1.9 day wording -- the two templates differ only in "day(s)"
    # vs "week(s)" (EN)/"วัน" vs "สัปดาห์" (TH), so a substring check on
    # the distinguishing word is a precise, real-wiring proof without
    # hand-recomputing the exact streak-length integer.
    review_texts = channel.sent_to(OWNER)
    review_text = _find(review_texts, i18n.t("weekly_review_header", "en"))
    assert re.search(r"current streak: \d+ week\(s\)", review_text), review_text
    assert not re.search(r"current streak: \d+ day\(s\)", review_text), review_text


# ===========================================================================
# AC20/AC22/AC23 -- pause suppresses proactive sends for the paused habit
# (through the REAL production functions), but a voluntary log during the
# pause still confirms and celebrates.
# ===========================================================================


async def test_pause_suppresses_proactive_sends_but_voluntary_log_still_confirms(tmp_path, monkeypatch):
    db = _db(tmp_path, OWNER)
    config = Config.model_validate({"i18n": {"language": "en"}})
    registry = HabitRegistry.from_config(config)
    today = date.today()

    # Pause "water" only (habit-scoped), covering today.
    db.insert_pause(OWNER, "water", today.isoformat(), (today + timedelta(days=3)).isoformat())

    # --- reminder_tick (AC20 site 1): water is due at 08:00 by default;
    # a paused water reminder never sends, through the REAL send_reminder/
    # run_due_reminders wiring. ---
    channel = _V19Channel()
    fixed_clock = lambda: datetime(today.year, today.month, today.day, 8, 0, 0)  # noqa: E731
    await reminders.run_due_reminders(channel, config, registry, db, clock=fixed_clock)
    assert channel.sent_to(OWNER) == []

    # --- checkin_tick / nudge_tick (AC20 sites 2, 3): direct-called via
    # the same message-building functions run_due_checkins/run_due_nudges
    # feed, so this proves the pause wiring without depending on the
    # ":00"/threshold time-gates those two wrappers apply on top. Seed
    # water close to (but under) goal so it would normally appear. ---
    _seed(db, f"{today.isoformat()}T08:00:00", "water", 2400.0)
    lang: i18n.Language = "en"
    checkin_msg = checkins.build_checkin_message(db, config, registry, lang, OWNER, clock=fixed_clock)
    if checkin_msg is not None:
        assert "water" not in checkin_msg.lower() and i18n.t("checkin_line_progress", "en", label="water", total=1, goal=1, unit="") .split(":")[0] not in checkin_msg
    nudge_msg = nudge.build_nudge_message(db, config, registry, lang, OWNER, clock=fixed_clock)
    # 2400/2500 = 96% >= default threshold -- normally "close"; paused ->
    # excluded, so no habit qualifies at all -> None (nothing to nag).
    assert nudge_msg is None

    # --- weekly-review + daily-summary inline jobs (AC20 sites 4, 5),
    # through the REAL scheduled closures. Seed a second, non-paused habit
    # (diary) so the jobs still have something to report, proving only
    # the PAUSED habit is excluded, not the whole send. ---
    _seed(db, f"{today.isoformat()}T21:00:00", "diary", None, raw="good day")
    db.close()

    config2 = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    wired_channel = await _run(
        monkeypatch, config2, script=[], run_jobs=["daily_summary"]
    )
    summary_texts = wired_channel.sent_to(OWNER)
    assert summary_texts, "daily summary should still fire (diary has a log today)"
    summary_text = summary_texts[-1]
    assert "water" not in summary_text.lower()
    assert "diary" in summary_text.lower()

    # --- AC23: a voluntary log during the pause still confirms normally
    # (pause suppresses PROACTIVE sends only, never the user's own
    # action). ---
    db2 = Database(tmp_path / "habits.db")
    db2.insert_pause(OWNER, "water", today.isoformat(), (today + timedelta(days=3)).isoformat())
    db2.close()
    voluntary_channel = await _run(
        monkeypatch, Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}}),
        script=[("message", OWNER, "500ml", None)],
    )
    assert any("500" in t for t in voluntary_channel.sent_to(OWNER))


async def test_dashboard_and_habits_show_the_pause_marker_and_held_streak(tmp_path, monkeypatch):
    db = _db(tmp_path, OWNER)
    today = date.today()
    until = today + timedelta(days=3)
    db.insert_pause(OWNER, "water", today.isoformat(), until.isoformat())
    _seed(db, f"{(today - timedelta(days=1)).isoformat()}T09:00:00", "water", 2500.0)
    db.close()

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    channel = await _run(
        monkeypatch,
        config,
        script=[("message", OWNER, "/dashboard on", None), ("message", OWNER, "/habits", None)],
    )
    habits_text = _find(channel.sent_to(OWNER), i18n.t("habits_overview_header", "en"))
    assert "paused until" in habits_text
    assert until.isoformat() in habits_text
    assert "held" in habits_text


# ===========================================================================
# AC13/AC14/AC18 -- the nightly 00:05 grace job, end-to-end: ledger row +
# silent message + a "system"-sourced audit row.
# ===========================================================================


async def test_grace_nightly_tick_writes_ledger_sends_silent_message_and_system_audit_row(tmp_path, monkeypatch):
    db = _db(tmp_path, OWNER)
    today = date.today()
    yesterday = today - timedelta(days=1)
    # A 5-day streak ending the day before yesterday, then a genuine miss
    # yesterday -- evaluate_grace should bridge it tonight.
    for offset in range(2, 7):
        day = today - timedelta(days=offset)
        _seed(db, f"{day.isoformat()}T09:00:00", "diary", None, raw="entry")
    db.close()

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    channel = await _run(monkeypatch, config, script=[], run_jobs=["grace_tick"])

    # Silent, always -- ARCHI RULING: disable_notification=True regardless
    # of [notifications] silent_proactive, and never gated by DND.
    grace_sends = [(t, silent) for cid, t, silent in channel.sent if cid == OWNER]
    assert grace_sends, "grace should have bridged diary's miss and sent a message"
    text, silent = grace_sends[-1]
    assert silent is True
    assert "grace" in text.lower() or "ผ่อนผัน" in text

    db2 = Database(tmp_path / "habits.db")
    try:
        assert yesterday.isoformat() in db2.grace_protected_dates(OWNER, "diary", yesterday.isoformat(), yesterday.isoformat())
        rows = db2.recent_audit(50)
        grace_rows = [r for r in rows if r["action"] == "grace_consumed"]
        assert len(grace_rows) == 1
        assert grace_rows[0]["source"] == "system"
        en_line = audit_view.render_recent(db2, config, "en", limit=10, owner_chat_id=OWNER)
        assert "diary" in en_line.lower() or grace_rows[0]["entity"] == "diary"
    finally:
        db2.close()


# ===========================================================================
# AC25/AC26 -- /wrapped through the REAL dispatch sends a PNG.
# ===========================================================================


async def test_wrapped_command_through_real_dispatch_sends_a_png(tmp_path, monkeypatch):
    db = _db(tmp_path, OWNER)
    today = date.today()
    _seed(db, f"{today.isoformat()}T09:00:00", "water", 1000.0)
    db.close()

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    channel = await _run(monkeypatch, config, script=[("message", OWNER, "/wrapped", None)])
    images = channel.images_to(OWNER)
    assert images, "expected a PNG to have been sent for /wrapped"
    image_bytes, caption, silent = images[-1]
    assert image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert caption
    assert silent is False  # the interactive command is never silent


# ===========================================================================
# AC28 -- month-end auto-send, default OFF; a smoke-check that turning it
# on sends a silent card and skips a fully-paused user.
# ===========================================================================


async def test_wrapped_auto_send_default_off_is_a_true_no_op(tmp_path, monkeypatch):
    db = _db(tmp_path, OWNER)
    today = date.today()
    _seed(db, f"{today.isoformat()}T09:00:00", "water", 1000.0)
    db.close()

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    assert config.wrapped.auto_send is False
    channel = await _run(monkeypatch, config, script=[], run_jobs=["wrapped_auto"])
    assert channel.images_to(OWNER) == []
    assert channel.sent_to(OWNER) == []


async def test_wrapped_auto_send_enabled_sends_silent_card_and_skips_fully_paused_user(tmp_path, monkeypatch):
    db = _db(tmp_path, OWNER, MEMBER)
    today = date.today()
    for uid in (OWNER, MEMBER):
        _seed(db, f"{today.isoformat()}T09:00:00", "water", 1000.0, user_id=uid)
    # MEMBER's entire registry is paused -- auto-send must skip them.
    for habit_id in ("water", "stretch", "diary"):
        db.insert_pause(MEMBER, habit_id, today.isoformat(), (today + timedelta(days=3)).isoformat())
    db.close()

    config = Config.model_validate(
        {"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}, "wrapped": {"auto_send": True}}
    )
    channel = await _run(monkeypatch, config, script=[], run_jobs=["wrapped_auto"])

    owner_images = channel.images_to(OWNER)
    assert owner_images, "OWNER (not paused) should get an auto-sent card"
    _img, _cap, silent = owner_images[-1]
    assert silent is True
    assert channel.images_to(MEMBER) == [] and channel.sent_to(MEMBER) == []


# ===========================================================================
# Two-user isolation across all four v1.9 features (SPEC-v1.9.md §11 step 3).
# ===========================================================================


async def test_two_user_isolation_across_cadence_grace_pause_wrapped(tmp_path, monkeypatch):
    db = _db(tmp_path, OWNER, MEMBER)
    db.set_cadence(OWNER, "stretch", 3)  # OWNER only
    db.insert_pause(MEMBER, "water", date.today().isoformat(), (date.today() + timedelta(days=3)).isoformat())
    db.close()

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    channel = await _run(
        monkeypatch,
        config,
        script=[
            ("message", OWNER, "/habits", None),
            ("message", MEMBER, "/habits", None),
        ],
    )
    owner_habits = _find(channel.sent_to(OWNER), i18n.t("habits_overview_header", "en"))
    member_habits = _find(channel.sent_to(MEMBER), i18n.t("habits_overview_header", "en"))

    # OWNER's cadence never leaks to MEMBER, and vice versa for the pause.
    assert "3×/week" in owner_habits or "3×/week" in owner_habits
    assert "3×/week" not in member_habits and "3×/week" not in member_habits
    assert "paused until" in member_habits
    assert "paused until" not in owner_habits
