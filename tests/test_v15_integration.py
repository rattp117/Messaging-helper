"""SPEC-v1.5.md §11 integration step -- the final pass that wires the three
independently-shipped parallel modules (`checkins`, `preparse`, `announce`)
into `main.py`'s real closures: `deterministic_parse` ahead of the
health-monitor deferral and LLM path in `handle_inbound_message` (R-L2),
`run_due_checkins` on the minutely tick + `/checkin` command routing
(R-K1/R-K8), and `announce_release` at real startup (R-N2).

Every module's own test file (`test_preparse.py`/`test_preparse_gaps.py`,
`test_checkins.py`/`test_checkins_gaps.py`/`test_dnd.py`, `test_announce.py`/
`test_announce_gaps.py`) already proves its owned ACs in isolation, calling
its own functions directly. This file is different in kind: it drives the
REAL, wired `handle_inbound_message`/`async_main` closures (mirroring
`tests/test_v12_integration.py`'s own harness) so a genuine wiring mistake
would show up here even though every module's own unit tests stay green.

Covers the integration-owned ACs from SPEC-v1.5.md §11: AC-14/AC-15 (through
real wiring, not just `deterministic_parse` directly), AC-16 (works
Ollama-down, end-to-end -- not independently verified by `preparse`'s own
suite per its own explicit note), AC-24 (announce audience + DND-ignored +
latest-only, "verified at the startup-loop integration" per spec). AC-1,
AC-2, AC-10, AC-11, AC-12, AC-17, AC-18, AC-19 are already covered by the
shared-surface pass's own dedicated files (`test_migrations.py`,
`test_units.py`, `test_dnd_matrix.py`, `test_config.py`) and are not
re-verified here.

Live-environment rule (unchanged from every other integration test file):
every DB here is a scratch `tmp_path` SQLite file. Nothing in this file
ever opens `data/habits.db`, and no real Telegram/Ollama call is made."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config, load_config
from habit_assistant.core import checkins, i18n, target_nl
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.target_nl import TargetIntent
from habit_assistant.main import handle_inbound_message
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "1001"
MEMBER = "2002"


# ---------------------------------------------------------------------------
# Small local fakes (per this codebase's own convention: each integration-
# adjacent test file keeps its own copy rather than importing another test
# file's fixtures).
# ---------------------------------------------------------------------------


class _RaisingLLM:
    """Proves a code path never touches the LLM at all (mirrors
    `tests/test_commands.py::_NeverCalledLLM`) -- used for the "zero LLM
    calls" half of AC-14/AC-16."""

    async def chat_json(self, *args, **kwargs):
        raise AssertionError("LLM must never be called for a deterministically-parseable message")

    async def chat_text(self, *args, **kwargs):
        raise AssertionError("LLM must never be called for a deterministically-parseable message")


class _StaticLLM:
    """Serves one fixed extraction result regardless of prompt content --
    used for the "ambiguous text still goes to the LLM" half."""

    def __init__(self, content: str) -> None:
        self._content = content

    async def chat_json(self, system_prompt, user_prompt, json_schema, valid_categories):
        return self._content

    async def chat_text(self, system_prompt, user_prompt):
        return "noted"


class _FrozenHealthMonitor:
    """Minimal `health_monitor` stand-in exposing only `.ollama_up`
    (mirrors `tests/test_resilience.py`'s fixture of the same name)."""

    def __init__(self, ollama_up: bool) -> None:
        self.ollama_up = ollama_up


class _CapturingChannel(Channel):
    """Unlike a `send`-only fake, this one also overrides `send_actionable`
    so the undo-button payload is actually captured (mirrors
    `tests/test_preparse_gaps.py::ActionableFakeChannel`'s own reasoning
    about the base class's default silently dropping buttons)."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.actionable: list[tuple[str, str, list]] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append(text)

    async def send_actionable(self, chat_id: str, text: str, buttons) -> None:
        self.actionable.append((chat_id, text, buttons))
        self.sent.append(text)

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised in this section")


def _seed(db: Database, ts: str, category: str, value_num: float, user_id: str = OWNER) -> int:
    return db.insert_log(LogEntry(None, user_id, ts, category, value_num, None, "x", "reply"))


def _habit(id_: str, type_: str, **kw) -> Habit:
    """Mirrors `tests/test_units.py::_habit`'s own helper exactly (same
    project convention, kept local per this codebase's own "each
    integration-adjacent file keeps its own copy" rule)."""
    defaults = dict(
        label_en=id_, label_th=id_, unit_en=None, unit_th=None, goal=None,
        reminder_times=(), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )
    defaults.update(kw)
    return Habit(id=id_, type=type_, **defaults)


def _colliding_registry() -> HabitRegistry:
    """The exact collision shape `IMPL-v1.5-integration.md`'s own smoke
    test used: `water` (alias `min`, multiplier 1.0) registered before
    `stretch` (unit `min`) -- both numeric/duration habits claim the
    token `"min"`, so `core/units.py:build_unit_lookup` excludes it
    entirely (order-independent) rather than misattributing a "10 min"
    log to whichever habit happens to be registered first."""
    return HabitRegistry(
        [
            _habit("water", "numeric", label_en="Water", label_th="น้ำ", unit_en="ml", unit_th="มล.", unit_aliases={"min": 1.0}, goal=2500.0),
            _habit("stretch", "duration", label_en="Stretch", label_th="ยืดเส้น", unit_en="min", unit_th="นาที"),
        ]
    )


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


@pytest.fixture
def registry():
    return HabitRegistry.from_config(Config())


@pytest.fixture
def fixed_clock():
    def clock():
        return datetime(2026, 8, 24, 9, 0, 0)

    return clock


# ===========================================================================
# AC-14/AC-16 -- "500ml" logs instantly with zero LLM calls, through the
# real (unmodified) handle_inbound_message, incl. undo button + streak
# suffix; still works while Ollama is down.
# ===========================================================================


async def test_ac14_bare_number_unit_logs_instantly_with_zero_llm_calls_and_an_undo_button(db, registry, fixed_clock):
    channel = _CapturingChannel()
    config = Config()

    await handle_inbound_message(
        "500ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )

    assert len(channel.actionable) == 1
    _, text, buttons = channel.actionable[0]
    assert "500" in text
    assert len(buttons) >= 1  # a real undo button, not silently dropped
    rows = db.logs_between(OWNER, "2000-01-01T00:00:00", "2100-01-01T00:00:00")
    assert len(rows) == 1 and rows[0]["category"] == "water" and rows[0]["value_num"] == 500.0


async def test_ac14_streak_milestone_suffix_appears_via_the_deterministic_path(db, registry, fixed_clock):
    """Same shape as `tests/test_streaks.py::
    test_milestone_crossing_sequence_3_then_no_repeat_then_7`'s first
    crossing, but through the pre-parser (a raising LLM double) instead
    of a mocked `parse_message` -- proves the milestone suffix isn't an
    LLM-path-only feature."""
    channel = _CapturingChannel()
    config = Config()  # gamification: enabled=True, milestones=[3, 7, 30]

    _seed(db, "2026-08-22T09:00:00", "water", 2500.0)
    _seed(db, "2026-08-23T09:00:00", "water", 2500.0)

    await handle_inbound_message(
        "2500ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )

    assert len(channel.actionable) == 1
    _, text, buttons = channel.actionable[0]
    assert "🔥" in text and "3" in text  # milestone_reached line present
    assert len(buttons) >= 1


async def test_ac15_ambiguous_text_still_goes_to_the_llm(db, registry, fixed_clock):
    """A message that is NOT a whole-message "NUMBER UNIT" shape must
    still reach the real extractor, unaffected by the pre-parser gate."""
    channel = _CapturingChannel()
    config = Config()
    llm = _StaticLLM(json.dumps({"category": "water", "value": 500, "confidence": 0.9}))

    await handle_inbound_message(
        "I drank some water just now", db=db, llm=llm, channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )

    assert len(channel.actionable) == 1
    rows = db.logs_between(OWNER, "2000-01-01T00:00:00", "2100-01-01T00:00:00")
    assert len(rows) == 1 and rows[0]["category"] == "water"


async def test_ac16_bare_number_unit_still_logs_while_ollama_is_down(db, registry, fixed_clock):
    channel = _CapturingChannel()
    config = Config()
    health_monitor = _FrozenHealthMonitor(ollama_up=False)

    await handle_inbound_message(
        "500ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER, health_monitor=health_monitor,
    )

    assert len(channel.actionable) == 1  # a real confirmation, not a deferred ack
    assert db.pending_unparsed() == []
    rows = db.logs_between(OWNER, "2000-01-01T00:00:00", "2100-01-01T00:00:00")
    assert len(rows) == 1 and rows[0]["category"] == "water"


async def test_ac16_ambiguous_text_still_defers_while_ollama_is_down(db, registry, fixed_clock):
    """Sanity counterweight: the pre-parser gate's new placement doesn't
    broaden the "skip the deferral" behavior to messages it shouldn't --
    an LLM-needing message during an outage is still deferred, unchanged.

    SPEC-v1.10.md §4 R15 (integration pass): `outage.honest_reply=False`
    keeps `channel.actionable == []` accurate -- by default the deferral
    ack now carries the `/log` keyboard (R15), which is this release's own
    unrelated concern (`tests/test_outage_honesty.py`'s scope)."""
    channel = _CapturingChannel()
    config = Config.model_validate({"outage": {"honest_reply": False}})
    health_monitor = _FrozenHealthMonitor(ollama_up=False)

    await handle_inbound_message(
        "I drank some water just now", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER, health_monitor=health_monitor,
    )

    assert channel.actionable == []
    pending = db.pending_unparsed()
    assert len(pending) == 1 and pending[0]["raw_message"] == "I drank some water just now"


# ===========================================================================
# Shared async_main harness for the checkins/announce sections below --
# mirrors tests/test_v12_integration.py's own copy.
# ===========================================================================


class _StopAfterSchedulerStart(Exception):
    pass


class _FakeScheduler:
    last_instance: "_FakeScheduler | None" = None

    def __init__(self, *args, **kwargs):
        self.jobs: dict[str, object] = {}
        _FakeScheduler.last_instance = self

    def add_job(self, func, trigger=None, args=None, id=None, replace_existing=True, **kwargs):
        self.jobs[id] = SimpleNamespace(func=func, trigger=trigger, args=args, id=id)

    def start(self):
        pass

    def shutdown(self, wait=False):
        pass


class _FakeOllamaClient:
    responses: list[str] = []
    # Vera's addition: counts probe_schema_support calls (AC-18's
    # probe_on_startup gate) without changing any other test's behavior
    # (default no-op counter, never asserted on unless a test reads it).
    probe_call_count = 0

    def __init__(self, *args, **kwargs):
        pass

    async def chat_text(self, system_prompt, user_prompt):
        return "noted"

    async def chat_json(self, system_prompt, user_prompt, json_schema, valid_categories):
        if _FakeOllamaClient.responses:
            return _FakeOllamaClient.responses.pop(0)
        return json.dumps({"category": "unknown", "value": None, "confidence": 0.1})

    async def probe_schema_support(self, *args, **kwargs) -> dict:
        _FakeOllamaClient.probe_call_count += 1
        return {}

    async def aclose(self) -> None:
        pass


class _RecordingHealthMonitor:
    """Vera's addition: a `HealthMonitor` stand-in that records its own
    constructor kwargs (for AC-17's health-interval wiring check) instead
    of making any real network call. `run()` blocks until cancelled,
    mirroring the real class's own long-lived background task shape."""

    last_kwargs: dict | None = None

    def __init__(self, *args, **kwargs) -> None:
        _RecordingHealthMonitor.last_kwargs = kwargs
        self.ollama_up = True
        self.telegram_up = True

    async def run(self) -> None:
        import asyncio

        await asyncio.sleep(3600)

    async def aclose(self) -> None:
        pass


class _ScriptedChannel(Channel):
    last_instance: "_ScriptedChannel | None" = None
    script: list[tuple] = []
    # Vera's addition: job ids (as registered on `_FakeScheduler`, e.g.
    # "daily_summary"/"weekly_review") to invoke directly -- awaited AFTER
    # the scripted messages but BEFORE raising `_StopAfterSchedulerStart`,
    # i.e. still INSIDE async_main's live `db` connection (its own
    # `finally` block closes `db` the instant `channel.run()` raises, so
    # calling a job's closure only after `_run()` returns would hit a
    # closed sqlite3 connection). Mirrors `tests/test_v12_integration.py`'s
    # own identical mechanism.
    run_jobs_before_stop: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        self.sent: list[tuple[str, str]] = []
        self.set_my_commands_calls: list[dict] = []
        _ScriptedChannel.last_instance = self

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append((chat_id, text))

    async def send_actionable(self, chat_id: str, text: str, buttons) -> None:
        self.sent.append((chat_id, text))

    async def set_my_commands(self, commands, *, scope_chat_id=None) -> None:
        # SPEC-v1.8.md R-D2: only records the default (global) menu
        # registration -- see test_discoverability.py's identical fake for
        # the full rationale.
        if scope_chat_id is None:
            self.set_my_commands_calls.append(commands)

    def sent_to(self, chat_id: str) -> list[str]:
        return [text for cid, text in self.sent if cid == chat_id]

    async def run(self, on_message, on_callback=None) -> None:
        for step in _ScriptedChannel.script:
            _, chat_id, text, display_name = step
            await on_message(chat_id, text, display_name)
        for job_id in _ScriptedChannel.run_jobs_before_stop:
            job = _FakeScheduler.last_instance.jobs.get(job_id)
            if job is not None:
                await job.func()
        raise _StopAfterSchedulerStart()

    async def aclose(self) -> None:
        pass


async def _run(
    monkeypatch, config, script, owner_chat_id=OWNER, responses=None, version=None,
    run_jobs=None, health_monitor_cls=None,
):
    from habit_assistant import main as main_module

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module, "load_secrets",
        lambda: SimpleNamespace(telegram_bot_token="fake-token", telegram_chat_id=owner_chat_id),
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", _ScriptedChannel)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClient)
    if health_monitor_cls is not None:
        monkeypatch.setattr(main_module, "HealthMonitor", health_monitor_cls)
    # SPEC-v1.5.md R-N2/IMPL-v1.5-announce.md's own documented note:
    # `__version__` has since been bumped for the real v1.5.0 release
    # (Archi's Phase 6.5 step, post-hand-off), so it now genuinely
    # matches a `RELEASE_NOTES` entry -- leaving it unpatched would make
    # `announce.announce_release`'s real startup call actually fire for
    # every test below that doesn't care about it, an extra leading
    # `channel.sent_to(...)` entry none of the checkin-focused tests were
    # written to expect. Default to an inert version (no catalog entry,
    # AC-22's own no-op) so every test gets deterministic, announce-free
    # behavior UNLESS it explicitly opts in via `version=`.
    #
    # Vera's own gotcha, worth recording: `__version__` is imported via
    # `from habit_assistant import __version__` in BOTH main.py (the
    # announce call) AND core/access.py (R-N5's own newly-approved
    # catch-up write) -- each binds its OWN separate name at import time,
    # so patching only `main_module.__version__` leaves `access.py`'s
    # copy unpatched, desyncing the two in a test even though a REAL
    # release bump (editing the one source file) keeps both consistent
    # in production. Patch both, always.
    from habit_assistant.core import access as access_module

    effective_version = version if version is not None else "0.0.0-test"
    monkeypatch.setattr(main_module, "__version__", effective_version)
    monkeypatch.setattr(access_module, "__version__", effective_version)
    _FakeScheduler.last_instance = None
    _ScriptedChannel.last_instance = None
    _ScriptedChannel.script = script
    _ScriptedChannel.run_jobs_before_stop = list(run_jobs or [])
    _FakeOllamaClient.responses = list(responses or [])
    _FakeOllamaClient.probe_call_count = 0
    _RecordingHealthMonitor.last_kwargs = None

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)
    return _ScriptedChannel.last_instance


# ===========================================================================
# Check-ins: /checkin on through real command routing, then the tick fires
# / is suppressed by DND / skips on all-goals-met -- through the real,
# persisted DB state the setter wrote. The tick itself is invoked directly
# with an explicit clock (not through the scheduler's zero-arg
# run-job-by-id mechanism, which doesn't apply here since run_due_checkins
# takes required positional args unlike the 0-arg daily_summary/
# weekly_review job closures) -- same "explicit clock=, never rely on
# monkeypatching a late-bound default" discipline already established for
# `in_dnd_now`/`send_reminder` elsewhere in this codebase.
#
# The scripted "2500ml"/"/checkin on" messages above are written through
# the REAL (unpatched) `handle_inbound_message` clock -- i.e. the actual
# wall-clock "today" -- so `_checkin_tick_clock()` below pins only the
# HOUR (09:00, inside the default 08:00-20:00 window) while keeping
# today's real DATE, so `run_due_checkins`' own "today" aligns with
# whatever date the scripted log was actually written under.
# ===========================================================================


def _checkin_tick_clock() -> datetime:
    return datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)


async def test_checkin_on_via_real_command_routing_then_the_tick_fires_at_the_top_of_the_hour(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("message", OWNER, "/checkin on", None)]
    channel = await _run(monkeypatch, config, script)

    assert any("checkin" in t.lower() or "08:00" in t for t in channel.sent_to(OWNER))

    db = Database(tmp_path / "habits.db")
    registry = HabitRegistry.from_config(config)
    try:
        await checkins.run_due_checkins(channel, config, registry, db, clock=_checkin_tick_clock)
    finally:
        db.close()

    assert any(i18n_checkin_marker in t for t in channel.sent_to(OWNER) for i18n_checkin_marker in ("🌤️",))


async def test_checkin_suppressed_by_dnd(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("message", OWNER, "/checkin on", None), ("message", OWNER, "/quiet 00:00-23:59", None)]
    channel = await _run(monkeypatch, config, script)
    before = len(channel.sent_to(OWNER))

    db = Database(tmp_path / "habits.db")
    registry = HabitRegistry.from_config(config)
    try:
        await checkins.run_due_checkins(channel, config, registry, db, clock=_checkin_tick_clock)
    finally:
        db.close()

    assert len(channel.sent_to(OWNER)) == before  # tick added nothing -- suppressed by DND


async def test_checkin_skipped_when_all_goals_met(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("message", OWNER, "/checkin on", None), ("message", OWNER, "2500ml", None)]
    channel = await _run(monkeypatch, config, script)
    before = len(channel.sent_to(OWNER))

    db = Database(tmp_path / "habits.db")
    registry = HabitRegistry.from_config(config)
    try:
        await checkins.run_due_checkins(channel, config, registry, db, clock=_checkin_tick_clock)
    finally:
        db.close()

    assert len(channel.sent_to(OWNER)) == before  # water goal already met today -- skipped, not a nag


# ===========================================================================
# Announce: startup sends the release note to active users once (AC-20/
# R-N2, verified through the real async_main startup sequence this time,
# not announce_release called directly), AC-24 (pending/blocked excluded,
# DND ignored, latest-version-only).
# ===========================================================================


async def test_startup_announce_sends_the_release_note_to_active_users_once(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(MEMBER, role="member", status="active")
    seed_db.close()

    channel = await _run(monkeypatch, config, script=[], version="1.5.0")

    assert any("1.5.0" in t or "v1.5.0" in t for t in channel.sent_to(OWNER))
    assert any("1.5.0" in t or "v1.5.0" in t for t in channel.sent_to(MEMBER))

    db = Database(tmp_path / "habits.db")
    try:
        assert db.get_last_announced_version(OWNER) == "1.5.0"
        assert db.get_last_announced_version(MEMBER) == "1.5.0"
    finally:
        db.close()


async def test_ac24_pending_and_blocked_users_receive_nothing(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user("3003", role="member", status="pending")
    seed_db.upsert_user("3004", role="member", status="blocked")
    seed_db.close()

    channel = await _run(monkeypatch, config, script=[], version="1.5.0")

    assert channel.sent_to("3003") == []
    assert channel.sent_to("3004") == []
    assert any("1.5.0" in t or "v1.5.0" in t for t in channel.sent_to(OWNER))  # the owner still gets it


async def test_ac24_dnd_is_ignored_active_user_in_always_dnd_still_receives_the_note(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.set_user_quiet_hours(OWNER, json.dumps([["00:00", "23:59"]]))  # covers essentially the whole day
    seed_db.close()

    channel = await _run(monkeypatch, config, script=[], version="1.5.0")

    assert any("1.5.0" in t or "v1.5.0" in t for t in channel.sent_to(OWNER))  # R-N4: announcements ignore DND


async def test_ac24_a_user_already_caught_up_to_the_latest_version_gets_no_duplicate(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.set_last_announced_version(OWNER, "1.5.0")
    seed_db.close()

    channel = await _run(monkeypatch, config, script=[], version="1.5.0")

    assert channel.sent_to(OWNER) == []


# ===========================================================================
# Vera's integration-gate adversarial additions (coordinator's punch list,
# 2026-08-23). Everything below drives the REAL wiring (`handle_inbound_
# message` directly, or the full `async_main`), same conventions as the
# tests above -- tmp_path-only SQLite, mocked LLM/Telegram, never
# `data/habits.db`.
# ===========================================================================


# ---------------------------------------------------------------------------
# 1. Pre-parser in production position: target-override rendering, the
# NL-target gate undisturbed by a prior preparse hit, and the deferral
# queue never capturing a preparsed message even in a MIXED sequence.
# ---------------------------------------------------------------------------


async def test_preparse_confirmation_reflects_a_target_override_not_the_config_default(db, registry, fixed_clock):
    """AC-14's byte-identical guarantee extended to target-override
    rendering: the deterministic path must read the SAME `targets.
    effective_goal` the LLM path does, not the bare config default."""
    channel = _CapturingChannel()
    config = Config()
    db.set_target(OWNER, "water", 3000.0)

    await handle_inbound_message(
        "500ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )

    _, text, _buttons = channel.actionable[0]
    assert "500 / 3000" in text or ("500" in text and "3000" in text)
    assert "2500" not in text  # the config default must not leak through


async def test_preparse_hit_does_not_disturb_a_subsequent_full_nl_target_message(db, registry, fixed_clock, monkeypatch):
    """A preparse hit for one message must not consume, cache, or
    otherwise perturb any state the full-NL target gate relies on for a
    LATER, different message -- the two paths are fully independent per
    call, not sharing any mutable state."""
    channel = _CapturingChannel()
    config = Config()

    async def fake_classify(text, llm, registry_, config_):
        return TargetIntent(habit_id="water", goal_base_unit=2500.0)

    monkeypatch.setattr(target_nl, "classify_target_intent", fake_classify)

    # 1. A preparse hit -- logs instantly, zero LLM calls.
    await handle_inbound_message(
        "500ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )
    rows_after_first = db.logs_between(OWNER, "2000-01-01T00:00:00", "2100-01-01T00:00:00")
    assert len(rows_after_first) == 1

    # 2. A full-NL target-setting message right after -- must still reach
    #    the NL-target gate normally (a genuinely non-"NUMBER UNIT" shape,
    #    so preparse itself returns None for it; the LLM here is a
    #    STATIC fake serving chat_text only, since classify_target_intent
    #    is monkeypatched directly and the gate must never fall through
    #    to parse_message for this message).
    await handle_inbound_message(
        "from now on I want to drink 2.5L a day", db=db, llm=_RaisingLLM(), channel=channel, config=config,
        registry=registry, clock=fixed_clock, user_id=OWNER,
    )
    assert db.get_target(OWNER, "water") == 2500.0
    # No SECOND logs row was written -- the NL-target hit takes the
    # "set a target, don't log" branch, unaffected by the earlier preparse hit.
    rows_after_second = db.logs_between(OWNER, "2000-01-01T00:00:00", "2100-01-01T00:00:00")
    assert len(rows_after_second) == 1


async def test_ollama_down_mixed_sequence_only_ambiguous_messages_enter_the_deferral_queue(db, registry, fixed_clock):
    channel = _CapturingChannel()
    config = Config()
    health_monitor = _FrozenHealthMonitor(ollama_up=False)

    # "10min" (glued, whole-message NUMBER+UNIT) is a genuine preparse hit
    # for stretch; "10 min stretch" (three tokens) is NOT -- R-L1's own
    # whole-message anchoring requires exactly NUMBER [UNIT], nothing more.
    for text in ("500ml", "I drank some water just now", "10min", "another vague message here"):
        await handle_inbound_message(
            text, db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
            clock=fixed_clock, user_id=OWNER, health_monitor=health_monitor,
        )

    pending = db.pending_unparsed()
    pending_texts = sorted(row["raw_message"] for row in pending)
    # Only the two genuinely ambiguous messages were deferred; both
    # "NUMBER UNIT"-shaped ones logged instantly via preparse and were
    # never queued at all.
    assert pending_texts == sorted(["I drank some water just now", "another vague message here"])
    # Deferred rows are themselves persisted as `logs` rows with
    # category='unparsed' (the deferral mechanism itself) -- excluded
    # here since this assertion is about which REAL habit categories got
    # confirmed via preparse.
    logged_rows = db.logs_between(OWNER, "2000-01-01T00:00:00", "2100-01-01T00:00:00")
    real_categories = {r["category"] for r in logged_rows if r["category"] != "unparsed"}
    assert real_categories == {"water", "stretch"}
    assert sum(1 for r in logged_rows if r["category"] == "unparsed") == 2


# ---------------------------------------------------------------------------
# 2. Check-ins live: opt-in default holds through a real multi-user
# startup with nobody enrolled, and /checkin changes write the new audit
# vocabulary, rendered bilingually by /audit.
# ---------------------------------------------------------------------------


async def test_checkin_opt_in_default_holds_through_real_startup_nobody_enrolled(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(MEMBER, role="member", status="active")
    seed_db.upsert_user("3003", role="member", status="active")
    seed_db.close()

    # A real startup, no /checkin command ever sent by anyone.
    await _run(monkeypatch, config, script=[])

    db = Database(tmp_path / "habits.db")
    registry = HabitRegistry.from_config(config)
    try:
        assert db.get_checkin_window(OWNER) is None
        assert db.get_checkin_window(MEMBER) is None
        assert db.get_checkin_window("3003") is None
        channel = _CapturingChannel()
        # A top-of-hour tick, squarely inside the config default window --
        # if opt-in weren't absolute, this is exactly when it would leak.
        await checkins.run_due_checkins(
            channel, config, registry, db, clock=lambda: datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        )
        assert channel.sent == []  # nobody -- owner included -- ever gets a checkin unasked
    finally:
        db.close()


async def test_checkin_setting_changes_write_audit_rows_rendered_bilingually_in_audit(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script_en = [
        ("message", OWNER, "/checkin on", None),
        ("message", OWNER, "/checkin off", None),
        ("message", OWNER, "/checkin default", None),
        ("message", OWNER, "/audit", None),
    ]
    channel = await _run(monkeypatch, config, script_en)

    db = Database(tmp_path / "habits.db")
    try:
        rows = db.recent_audit(10)
        actions = [r["action"] for r in rows]
        # newest-first: default, off, on.
        assert actions == ["checkin_default", "checkin_off", "checkin_set"]
    finally:
        db.close()

    audit_reply_en = channel.sent_to(OWNER)[-1]
    assert i18n.t("audit_action_checkin_set", "en") in audit_reply_en
    assert i18n.t("audit_action_checkin_off", "en") in audit_reply_en
    assert i18n.t("audit_action_checkin_default", "en") in audit_reply_en

    # Same sequence, but read back with the Thai alias `ประวัติ` this time
    # -- a fresh DB/run so the Thai reply isn't polluted by the English one.
    #
    # FIXED (SPEC-v1.8.md R-D3/AC-D3, `main.py` integration pass): this
    # test used to lock in a pre-existing bug -- `/audit`'s own reply
    # language was resolved by `main.py:on_message`'s `lang = i18n.
    # resolve_reply_language(text, config)` call WITHOUT `user_pref=
    # _stored_language_pref(...)`, unlike every OTHER command reply (which
    # all route through `handle_inbound_message`, whose own `lang`
    # resolution DOES thread the user's stored `/lang` preference). An
    # owner who ran `/lang th` got an ENGLISH `/audit` reply if they typed
    # the (all-ASCII) "/audit" trigger -- only the Thai alias `ประวัติ`
    # (which itself contains Thai characters) auto-detected Thai. R-D3
    # now resolves the stored preference into `/audit`'s own reply
    # language BEFORE the interception, so BOTH trigger shapes now render
    # in the owner's chosen language.
    config2 = Config.model_validate({"app": {"db_path": str(tmp_path / "habits2.db")}})
    script_th = [
        ("message", OWNER, "/checkin on", None),
        ("message", OWNER, "/checkin off", None),
        ("message", OWNER, "/lang th", None),
        ("message", OWNER, "/audit", None),  # English trigger -> now Thai too, per /lang th (R-D3 fix)
        ("message", OWNER, "ประวัติ", None),  # Thai alias trigger -> auto-detects Thai
    ]
    channel2 = await _run(monkeypatch, config2, script_th)
    audit_replies = channel2.sent_to(OWNER)[-2:]
    audit_reply_via_slash, audit_reply_via_thai_alias = audit_replies
    assert i18n.t("audit_action_checkin_set", "th") in audit_reply_via_slash  # R-D3: stored /lang now wins
    assert i18n.t("audit_action_checkin_set", "th") in audit_reply_via_thai_alias
    assert i18n.t("audit_action_checkin_off", "th") in audit_reply_via_thai_alias


# ---------------------------------------------------------------------------
# 3. Announce at startup: per-user language, a newly-approved user mid-
# session stays caught up across two real consecutive startups, a user
# several versions behind gets only the current note once, and today's
# ACTUAL pinned `__version__` (pre-release-bump) announces nothing.
# ---------------------------------------------------------------------------


async def test_announce_sends_each_user_their_own_language(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(MEMBER, role="member", status="active")
    seed_db.set_user_language(MEMBER, "th")
    seed_db.close()

    channel = await _run(monkeypatch, config, script=[], version="1.5.0")

    from habit_assistant.core.release_notes import get_release_note

    assert channel.sent_to(OWNER) == [get_release_note("1.5.0", "th")]  # owner's default pref is "auto" -> primary_language (th)
    assert channel.sent_to(MEMBER) == [get_release_note("1.5.0", "th")]
    # And they're genuinely different-language-capable -- an English-
    # preferring member gets the English variant instead.
    seed_db2 = Database(tmp_path / "habits.db")
    seed_db2.set_user_language(MEMBER, "en")
    seed_db2.close()
    channel2 = await _run(monkeypatch, config, script=[], version="1.5.0")
    # OWNER already marked from the first run -- only MEMBER (re-fetched
    # fresh below) is meaningfully re-checked here for language content;
    # re-seed a brand new, never-announced member to prove the "en" path.
    db3 = Database(tmp_path / "habits.db")
    db3.upsert_user("9009", role="member", status="active")
    db3.set_user_language("9009", "en")
    db3.close()
    channel3 = await _run(monkeypatch, config, script=[], version="1.5.0")
    assert channel3.sent_to("9009") == [get_release_note("1.5.0", "en")]
    del channel2  # unused beyond the setup step above


async def test_announce_newly_approved_user_mid_session_stays_caught_up_across_two_real_startups(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    newcomer = "4004"

    # Run 1: a real startup at v1.5.0 (announces to the owner), then
    # DURING that same session the owner approves a brand-new chat --
    # R-N5 catches them up to the current version immediately, so they
    # must NOT receive v1.5.0's note on THIS run (they weren't active
    # when announce_release itself ran, at the top of startup) nor on
    # any later one.
    script = [("message", OWNER, f"/approve {newcomer}", None)]
    channel = await _run(monkeypatch, config, script, version="1.5.0")
    assert any("1.5.0" in t or "v1.5.0" in t for t in channel.sent_to(OWNER))
    # The newcomer DOES get a reply (the ordinary `access_granted`
    # "you're in!" welcome, v1.2-era behavior, unrelated to announce) --
    # just never the RELEASE NOTE itself, since they weren't active yet
    # when `announce_release` ran at the top of this same startup.
    assert not any("1.5.0" in t or "v1.5.0" in t for t in channel.sent_to(newcomer))

    db = Database(tmp_path / "habits.db")
    try:
        assert db.get_last_announced_version(newcomer) == "1.5.0"  # caught up by /approve, R-N5
    finally:
        db.close()

    # Run 2: a genuinely SECOND real async_main startup over the same
    # persisted DB (the closest this harness gets to "the process
    # restarted") -- the newcomer is now active AND already caught up,
    # so they receive nothing this time either.
    channel2 = await _run(monkeypatch, config, script=[], version="1.5.0")
    assert channel2.sent_to(newcomer) == []
    assert channel2.sent_to(OWNER) == []  # owner already marked from run 1 too


async def test_announce_user_several_versions_behind_gets_only_the_current_note_once(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.set_last_announced_version(OWNER, "1.2.0")  # several versions behind
    seed_db.close()

    channel = await _run(monkeypatch, config, script=[], version="1.5.0")

    from habit_assistant.core.release_notes import get_release_note

    owner_sends = channel.sent_to(OWNER)
    assert len(owner_sends) == 1  # exactly one send -- no rollup/backfill of 1.3.0/1.4.0
    assert owner_sends[0] == get_release_note("1.5.0", "th")

    db = Database(tmp_path / "habits.db")
    try:
        assert db.get_last_announced_version(OWNER) == "1.5.0"
    finally:
        db.close()


def _latest_entry_bearing_version() -> str:
    """The newest key in `release_notes.RELEASE_NOTES`, by SemVer tuple
    order -- NOT necessarily today's `__version__` itself. A patch/gap-fix
    release can (and, per R-N1's own "patch releases shouldn't message
    users" convention, sometimes deliberately does -- v1.8.1 is exactly
    this shape) ship with no `RELEASE_NOTES` entry of its own, in which
    case the latest ENTRY-bearing version is an older one. Every version
    key in this catalog is a plain "X.Y.Z" string (no pre-release/build
    suffixes), so a plain per-component integer tuple compare is safe."""
    from habit_assistant.core import release_notes

    return max(release_notes.RELEASE_NOTES, key=lambda v: tuple(int(p) for p in v.split(".")))


async def test_current_pinned_version_announces_to_active_users_today(tmp_path, monkeypatch):
    """UPDATED post-release (Archi's Phase 6.5 version bump landed,
    v1.7.0 tag exists): `src/habit_assistant/__init__.py:__version__` is
    now genuinely "1.7.0", matching a real `RELEASE_NOTES` entry -- so a
    real startup TODAY, using the app's actual current constant (not a
    synthetic one, and not this file's own default `_run` neutralization
    -- passed explicitly as `version=current_version` below to exercise
    the literal constant), correctly announces to every active user and
    marks them caught up. This was originally written as the mirror-image
    "announces nothing" sanity check before the release shipped; its own
    docstring at the time predicted exactly this update would be needed
    the moment `__version__` was bumped -- that's expected, not a bug.
    UPDATED AGAIN at v1.7.0 (same reason, found stale during SPEC-v1.8.md's
    shared-surface pass -- confirmed pre-existing/unrelated to that work,
    same "each Phase 6.5 bump moves this pin forward by construction"
    pattern IMPL-v1.7-shared.md's own prior fix of this exact test already
    documented).

    UPDATED AGAIN at v1.8.1 (Vera, release-prep for that gap-fix patch):
    the previous shape hard-pinned `current_version == "1.8.0"` and
    asserted ONLY the "announces" half -- both would have broken the
    moment Archi bumped `__version__` to "1.8.1", because that patch
    deliberately ships with NO `RELEASE_NOTES` entry (an invisible
    `/help`-copy fix isn't worth interrupting users for, same R-N1
    convention `core/announce.py:announce_release`'s own docstring
    documents: "a version with no catalog entry at all announces
    nothing"). Restructured into two halves so the suite stays green
    through THIS bump and every future patch-without-notes bump, with no
    literal edit required unless a new notes entry actually ships:
    - Half A pins the "announces + marks caught up" behavior against the
      latest ENTRY-bearing version (`_latest_entry_bearing_version()`,
      currently "1.8.0"'s own real entry) -- not `__version__` itself, so
      this half is stable across a no-entry patch bump.
    - Half B drives the REAL `__version__` constant, whatever it is right
      now, through the same wired `async_main` startup, and asserts
      whichever shape matches: announced-and-marked if `__version__` has
      an entry, or silently-nothing if it doesn't -- derived from
      `current_version in release_notes.RELEASE_NOTES`, never a hardcoded
      equality. A future release that ships v1.9.0 WITH a notes entry
      exercises Half B's "announces" branch automatically; a future
      patch that ships v1.8.2 WITHOUT one exercises the "silent no-op"
      branch automatically -- no test edit needed either way, only a new
      RELEASE_NOTES entry (or its absence) drives which branch runs."""
    from habit_assistant import __version__ as current_version
    from habit_assistant.core import release_notes

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(MEMBER, role="member", status="active")
    seed_db.close()

    # Half A: a current version WITH a RELEASE_NOTES entry announces to
    # every active user and marks them caught up -- pinned against the
    # latest entry-bearing version, not `__version__` itself (see the
    # docstring above).
    entry_version = _latest_entry_bearing_version()
    channel = await _run(monkeypatch, config, script=[], version=entry_version)

    assert channel.sent_to(OWNER) != []
    assert channel.sent_to(MEMBER) != []
    db = Database(tmp_path / "habits.db")
    try:
        assert db.get_last_announced_version(OWNER) == entry_version
        assert db.get_last_announced_version(MEMBER) == entry_version
    finally:
        db.close()

    # Half B: today's ACTUAL `__version__` constant, through the SAME real
    # startup wiring, against a freshly-active user who was not caught up
    # by Half A above (so either branch below is a genuine end-to-end
    # probe, not a tautology carried over from Half A's state). Whether it
    # announces or stays silent is derived from the real catalog, not
    # asserted as a fixed expectation -- this is what lets the test survive
    # a future bump either way.
    newcomer = "5005"
    seed_db2 = Database(tmp_path / "habits.db")
    seed_db2.upsert_user(newcomer, role="member", status="active")
    seed_db2.close()

    channel2 = await _run(monkeypatch, config, script=[], version=current_version)

    db2 = Database(tmp_path / "habits.db")
    try:
        if current_version in release_notes.RELEASE_NOTES:
            assert channel2.sent_to(newcomer) != []
            assert db2.get_last_announced_version(newcomer) == current_version
        else:
            # The v1.8.1 shape: no catalog entry -> silent no-op, R-N1.
            assert channel2.sent_to(newcomer) == []
            assert db2.get_last_announced_version(newcomer) is None
    finally:
        db2.close()


# ---------------------------------------------------------------------------
# 4. DND matrix final state: daily summary + weekly review per-user DND
# through the REAL scheduled-job closures, and ops sends (health alert,
# access-request notification) proven NOT suppressed even for an owner in
# permanent DND.
# ---------------------------------------------------------------------------


async def test_daily_summary_and_weekly_review_honor_per_user_dnd_through_the_real_jobs(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(MEMBER, role="member", status="active")
    seed_db.set_user_quiet_hours(MEMBER, json.dumps([["00:00", "23:59"]]))  # MEMBER in permanent DND
    seed_db.close()

    script = [
        # Both resolve via the deterministic pre-parser (AC-14) -- no LLM
        # response queue needed for either.
        ("message", OWNER, "500ml", None),
        ("message", MEMBER, "300ml", None),
    ]
    channel = await _run(monkeypatch, config, script, run_jobs=["daily_summary", "weekly_review"])

    owner_summary = [t for t in channel.sent_to(OWNER) if i18n.t("daily_summary_header", "th") in t]
    member_summary = [t for t in channel.sent_to(MEMBER) if i18n.t("daily_summary_header", "th") in t]
    assert owner_summary  # un-customized owner: DND empty by default -- summary fires (AC-10)
    assert not member_summary  # MEMBER's own DND suppresses their summary

    owner_review = [t for t in channel.sent_to(OWNER) if i18n.t("weekly_review_header", "th") in t]
    member_review = [t for t in channel.sent_to(MEMBER) if i18n.t("weekly_review_header", "th") in t]
    assert owner_review  # AC-11: un-customized owner's review still fires
    assert not member_review  # MEMBER's own DND suppresses their review too


async def test_health_alert_and_access_request_notification_not_suppressed_by_owner_dnd(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.set_user_quiet_hours(OWNER, json.dumps([["00:00", "23:59"]]))  # owner in permanent DND
    seed_db.close()

    # Ops alert: R-D4 says health/outage alerts are never subject to DND.
    from habit_assistant.core.health import HealthMonitor

    sent: list[tuple[str, str]] = []

    class _RecordingChannel(Channel):
        async def send(self, chat_id, text, *, disable_notification: bool = False):
            sent.append((chat_id, text))

        async def run(self, on_message, on_callback=None):
            raise NotImplementedError

    monitor = HealthMonitor("http://mac-mini:11434", "fake-token", OWNER, channel=_RecordingChannel(), language="en")
    await monitor._alert("Ollama is DOWN")
    assert sent == [(OWNER, "Ollama is DOWN")]  # delivered despite the owner's own permanent DND

    # Access-request: R-D4 also excludes access-request/-granted
    # notifications to the owner. Real gate, real DND-in-effect owner.
    db = Database(tmp_path / "habits.db")
    try:
        registry = HabitRegistry.from_config(config)
        from habit_assistant.core import access

        channel = _CapturingChannel()
        proceed = await access.handle_gate(db, channel, config, OWNER, "5005", "Stranger", "hi", lang="en")
        assert proceed is False
        assert any("5005" in t for t in channel.sent)  # the owner's access_request notification went through
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 5. Unit-collision fix: a colliding-unit registry makes preparse fall
# through to the LLM through the real wiring; /target and the edit-trigger
# both reject the same colliding token consistently (no OTHER consumer
# silently changed while the other didn't).
# ---------------------------------------------------------------------------


async def test_colliding_unit_registry_falls_through_to_the_llm_through_real_wiring(db, fixed_clock):
    channel = _CapturingChannel()
    config = Config()
    registry = _colliding_registry()
    llm = _StaticLLM(json.dumps({"category": "stretch", "value": 10, "confidence": 0.9}))

    await handle_inbound_message(
        "10 min", db=db, llm=llm, channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )

    # Reached the LLM (not silently misattributed to "water" via a
    # first-registered-wins collision) -- the static fake's own answer
    # (stretch) is what got logged.
    rows = db.logs_between(OWNER, "2000-01-01T00:00:00", "2100-01-01T00:00:00")
    assert len(rows) == 1 and rows[0]["category"] == "stretch"


async def test_colliding_unit_target_set_rejects_with_usage_through_real_wiring(db, fixed_clock):
    """Locks in IMPL-v1.5-integration.md's own documented behavior
    change: `/target <habit> <value><colliding-unit>` now ALWAYS returns
    a usage reply, regardless of which habit is explicitly named or its
    registration order -- through the real command-dispatch wiring, not
    just a direct `_parse_target_value` call."""
    channel = _CapturingChannel()
    config = Config()
    registry = _colliding_registry()

    await handle_inbound_message(
        "/target stretch 10min", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )

    assert channel.sent  # a reply was sent (usage), not a silent drop
    assert db.get_target(OWNER, "stretch") is None  # never set to the wrong/ambiguous value


async def test_colliding_unit_edit_trigger_also_falls_through_consistently(db, fixed_clock):
    """The OTHER `core/units.py` consumer (`commands._parse_edit_value`)
    must reject the same colliding token too -- confirming the fix landed
    for both consumers together, not just `/target`'s own path, per
    IMPL-v1.5-integration.md's own "report, don't silently change"
    framing (both consumers share the one function, R-L5)."""
    channel = _CapturingChannel()
    config = Config()
    registry = _colliding_registry()
    llm = _StaticLLM(json.dumps({"category": "unknown", "value": None, "confidence": 0.1}))

    _seed(db, "2026-08-22T09:00:00", "stretch", 5.0)

    await handle_inbound_message(
        "make that 10min", db=db, llm=llm, channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )

    # Never silently edited the pre-existing stretch row to a
    # misattributed value -- the ambiguous unit falls all the way through
    # (dispatch returns None for the unparseable edit value, and preparse
    # itself doesn't match "make that ..." either) to the LLM/clarifying
    # path, which here reports "unknown".
    row = db.last_log(OWNER, category="stretch")
    assert row["value_num"] == 5.0  # unchanged


# ---------------------------------------------------------------------------
# 6. Health-probe config: the 300s default flows into the real
# HealthMonitor construction, the LIVE config.toml's pinned 60s still
# parses correctly, and `probe_on_startup` genuinely gates the schema probe.
# ---------------------------------------------------------------------------


async def test_health_interval_default_300_flows_into_healthmonitor_through_real_startup(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    assert config.health.interval_seconds == 300.0  # AC-17's own stated default

    await _run(monkeypatch, config, script=[], health_monitor_cls=_RecordingHealthMonitor)

    assert _RecordingHealthMonitor.last_kwargs["interval_seconds"] == 300.0


def test_live_config_toml_health_interval_is_pinned_to_60():
    """AC-17: a shorter pinned value in the LIVE, real `config.toml` (not
    a synthetic `Config.model_validate({...})`) still loads and parses
    correctly -- `load_config()` here is genuinely unpatched, reading the
    actual deployed file from disk."""
    config = load_config()
    assert config.health.interval_seconds == 60.0
    assert config.ollama.probe_on_startup is True
    assert config.checkin.enabled is False


@pytest.mark.parametrize("probe_on_startup,expect_called", [(True, True), (False, False)])
async def test_probe_on_startup_gate(tmp_path, monkeypatch, probe_on_startup, expect_called):
    config = Config.model_validate(
        {"app": {"db_path": str(tmp_path / "habits.db")}, "ollama": {"probe_on_startup": probe_on_startup}}
    )
    await _run(monkeypatch, config, script=[])
    called = _FakeOllamaClient.probe_call_count > 0
    assert called == expect_called


# ---------------------------------------------------------------------------
# 7. AC-M3-style regression: the exact pre-v1.5 (v1.2-era) confirmation
# string, byte-identical, now produced via the preparse path instead of a
# mocked LLM extraction.
# ---------------------------------------------------------------------------


async def test_preparse_confirmation_byte_identical_to_known_v1_2_era_string(db, registry, fixed_clock):
    channel = _CapturingChannel()
    config = Config()

    await handle_inbound_message(
        "500ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )

    _, text, _buttons = channel.actionable[0]
    # The exact string tests/test_v12_integration.py's own AC-M3 test
    # pins for the LLM path -- unchanged now that "500ml" resolves via
    # preparse instead.
    assert text == "✅ 500 ml logged — today 500 / 2500 ml (20%)"


# ---------------------------------------------------------------------------
# 8. Migration 008 rehearsal: a v7-shaped (v1.4-era) scratch DB with real
# pre-existing data, opened through the REAL async_main startup (the same
# migration-008-then-attribution/prune/announce sequence production runs
# on upgrade day).
# ---------------------------------------------------------------------------


async def test_migration_008_rehearsal_on_a_v1_4_shaped_scratch_db(tmp_path, monkeypatch):
    db_path = tmp_path / "upgrade_rehearsal_v15.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE logs (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          ts          TEXT NOT NULL,
          category    TEXT NOT NULL,
          value_num   REAL,
          value_text  TEXT,
          raw_message TEXT NOT NULL,
          source      TEXT NOT NULL DEFAULT 'reply',
          created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          deleted_at  TEXT NULL,
          habit_type  TEXT NULL,
          user_id     TEXT NULL
        );
        CREATE TABLE habit_targets (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id    TEXT,
          habit_id   TEXT NOT NULL,
          goal       REAL NOT NULL,
          updated_at TEXT,
          UNIQUE(user_id, habit_id)
        );
        CREATE TABLE users (
          chat_id                TEXT PRIMARY KEY,
          role                   TEXT NOT NULL DEFAULT 'member',
          status                 TEXT NOT NULL DEFAULT 'pending',
          display_name           TEXT,
          language_pref          TEXT NOT NULL DEFAULT 'auto',
          quiet_hours_json       TEXT,
          snooze_default_minutes INTEGER,
          created_at             TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE user_reminder_times (
          user_id  TEXT NOT NULL,
          habit_id TEXT NOT NULL,
          time     TEXT NOT NULL,
          PRIMARY KEY (user_id, habit_id, time)
        );
        CREATE TABLE audit_log (
          id             INTEGER PRIMARY KEY AUTOINCREMENT,
          ts             TEXT NOT NULL,
          user_id        TEXT NOT NULL,
          action         TEXT NOT NULL,
          entity         TEXT,
          old_value      TEXT,
          new_value      TEXT,
          source         TEXT NOT NULL,
          target_user_id TEXT,
          created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        PRAGMA user_version = 7;
        """
    )
    conn.execute("INSERT INTO users (chat_id, role, status) VALUES (?, 'owner', 'active')", (OWNER,))
    today_ts = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO logs (ts, category, value_num, value_text, raw_message, source, habit_type, user_id) "
        "VALUES (?, 'water', 500.0, NULL, '500ml', 'reply', 'numeric', ?)",
        (today_ts, OWNER),
    )
    conn.execute("INSERT INTO habit_targets (user_id, habit_id, goal) VALUES (?, 'water', 3000.0)", (OWNER,))
    conn.commit()
    conn.close()

    config = Config.model_validate({"app": {"db_path": str(db_path)}})
    script = [
        ("message", OWNER, "/habits", None),  # pre-existing v1.4-era data still works
        ("message", OWNER, "/checkin on", None),  # a genuinely new-in-v1.5 write, post-upgrade
    ]
    channel = await _run(monkeypatch, config, script, owner_chat_id=OWNER)

    # "/habits" is a REPLY (auto-detected from the inbound, all-ASCII
    # trigger text itself) -> English, unlike an UNPROMPTED send (daily
    # summary/review/reminders), which defaults to Thai for an unset pref.
    habits_reply = next(t for t in channel.sent_to(OWNER) if i18n.t("habits_overview_header", "en") in t)
    assert "500" in habits_reply  # the pre-existing legacy log is still readable/correct

    db = Database(db_path)
    try:
        assert db.schema_version == 13
        cols = {row[1] for row in db._conn.execute("PRAGMA table_info(users)").fetchall()}
        assert {"checkin_window", "last_announced_version"} <= cols
        assert db.get_last_announced_version(OWNER) is None  # no backfill (migration's own contract)
        assert db.get_checkin_window(OWNER) == "08:00-20:00"  # the post-upgrade /checkin on write succeeded
        assert db.get_target(OWNER, "water") == 3000.0  # the legacy target override carried over too
    finally:
        db.close()
