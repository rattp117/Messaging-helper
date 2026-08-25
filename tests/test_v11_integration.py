"""SPEC-v1.1.md "Undo menu + per-habit targets" -- the FINAL integration ACs
(SPEC-v1.1.md §11's "ACs verified during the shared-surface/integration
step" table) that only became testable once `main.py`'s wiring landed
(`IMPL-v1.1-integration.md`): AC1, AC2, AC3, AC4, AC5, AC6 end-to-end
(previously module-level-only per `IMPL-v1.1-undo-ui.md`), plus the
`main.py`-routing halves of AC29/AC30/AC31/AC33 that `tests/test_target_nl.py`
explicitly left to this pass (see its own module docstring).

AC21-AC26 (effective-goal unification) and the reminder-skip/streak half of
AC31 are already covered by `tests/test_v11_shared_surface.py` and are not
re-tested here. AC24 (byte-identical, no override) is the full ~860-test
suite passing unmodified, not re-derived here.

Conventions: real on-disk SQLite (`tmp_path`) everywhere; Ollama is always a
`FakeLLM`/`_FakeOllamaClient` or a monkeypatched `parse_message`/
`classify_target_intent` -- never a real network call; Telegram is always a
`Channel` subclass fake, never a real `TelegramChannel`/`httpx` call.
`async_main`-level tests mirror `tests/test_charts.py`'s own
`_run_async_main_and_capture_scheduler` pattern (patch `load_config`,
`load_secrets`, `AsyncIOScheduler`, `TelegramChannel`, `OllamaClient`; expect
`_StopAfterSchedulerStart` from the fake channel's `run()`). No test in this
file ever opens `data/habits.db` or touches the live Task Scheduler service.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Button, Channel
from habit_assistant.config import Config
from habit_assistant.core import i18n, streaks, target_nl
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.target_nl import TargetIntent
from habit_assistant.llm.ollama_client import ExtractionResult
from habit_assistant.main import handle_inbound_message
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry


# ---------------------------------------------------------------------------
# Shared helpers (mirrors tests/test_v11_shared_surface.py / test_streaks.py's
# own per-file copies, per this codebase's convention).
# ---------------------------------------------------------------------------


CHAT_ID = "owner-chat-id"


def _seed(db: Database, ts: str, category: str, value_num: float | None) -> int:
    return db.insert_log(LogEntry(None, CHAT_ID, ts, category, value_num, None, "x", "reply"))


def _log_count(db: Database) -> int:
    return db._conn.execute("SELECT COUNT(*) AS n FROM logs").fetchone()["n"]


class FakeChannel(Channel):
    """Tracks plain `send` and `send_actionable` calls separately, so a
    test can assert both "this send carried no button" (AC4) and "this
    send carried exactly one button with the right callback_data" (AC3)."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.actionable: list[tuple[str, list[Button]]] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append(text)

    async def send_actionable(self, chat_id: str, text: str, buttons: list[Button]) -> None:
        self.actionable.append((text, buttons))
        self.sent.append(text)

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised in the direct handle_inbound_message tests")


class FakeLLM:
    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return "noted"

    async def chat_json(self, *args, **kwargs):
        raise AssertionError("chat_json must not be called by these direct-handler tests")


def patch_parse_message(monkeypatch, result: ExtractionResult) -> None:
    async def fake_parse_message(text, llm, registry, confidence_threshold=None):
        return result

    monkeypatch.setattr("habit_assistant.main.parse_message", fake_parse_message)


def patch_parse_message_forbidden(monkeypatch) -> None:
    """For NL-hit ordering tests: proves the NL target step returns BEFORE
    `parse_message` is ever reached."""

    async def forbidden(*args, **kwargs):
        raise AssertionError("parse_message must not be called when the NL target step already handled the message")

    monkeypatch.setattr("habit_assistant.main.parse_message", forbidden)


class _FrozenHealthMonitor:
    """Minimal `health_monitor` stand-in exposing only `.ollama_up`
    (mirrors tests/test_commands.py / test_resilience.py's own fixture of
    the same name)."""

    def __init__(self, ollama_up: bool):
        self.ollama_up = ollama_up


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(CHAT_ID, role="owner", status="active")
    yield database
    database.close()


@pytest.fixture
def registry():
    return HabitRegistry.from_config(Config())


def _today_ts(hour: int = 9) -> str:
    """Real "today", not a hardcoded past date -- per the coordinator's
    explicit instruction to keep new tests date-drift-proof."""
    return f"{date.today().isoformat()}T{hour:02d}:00:00"


def _clock(hour: int = 9):
    today = date.today()
    return lambda: datetime(today.year, today.month, today.day, hour, 0, 0)


# ===========================================================================
# AC3: a log confirmation carries exactly one undo button with the right
# callback_data == "undo:<inserted row id>".
# ===========================================================================


async def test_ac3_log_confirmation_carries_exactly_one_undo_button_with_correct_id(db, registry, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult(category="water", value=500.0, confidence=0.9))
    channel = FakeChannel()

    await handle_inbound_message(
        "500ml",
        db=db,
        llm=FakeLLM(),
        channel=channel,
        config=Config(),
        registry=registry,
        clock=_clock(),
        user_id=CHAT_ID,
    )

    assert len(channel.actionable) == 1
    text, buttons = channel.actionable[0]
    assert len(buttons) == 1
    label, data = buttons[0]
    row = db.last_log(CHAT_ID)
    assert data == f"undo:{row['id']}"
    assert label == i18n.t("undo_button_label", "en")
    assert channel.sent == [text]  # the confirmation went out via send_actionable, not a second plain send


# ===========================================================================
# AC4: unprompted sends carry no button. Reminders/daily-summary/weekly-
# review/health-alerts are verified structurally (core/reminders.py,
# core/health.py, and main.py's weekly_review_job/daily_summary_job all
# call plain `channel.send`, never `send_actionable` -- confirmed by
# reading the source; see TEST-v1.1-integration.md). This file covers the
# two unprompted-send branches reachable from handle_inbound_message itself.
# ===========================================================================


async def test_ac4_clarifying_question_carries_no_button(db, registry, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult(category="not-a-real-habit", value=None, confidence=0.9))
    channel = FakeChannel()

    # Not command-shaped, not query-shaped ("?" would route through
    # commands.dispatch's query matcher instead) -- just an unparseable
    # message that reaches parse_message and resolves to no known habit.
    await handle_inbound_message(
        "zzz completely unrecognized nonsense",
        db=db,
        llm=FakeLLM(),
        channel=channel,
        config=Config(),
        registry=registry,
        clock=_clock(),
        user_id=CHAT_ID,
    )

    assert channel.actionable == []
    assert channel.sent == [i18n.t("clarifying_question", "en")]


async def test_ac4_deferred_ack_carries_no_button(db, registry):
    """SPEC-v1.5.md AC-14/AC-16: uses "drank some water" rather than a
    bare "500ml" -- a whole-message "NUMBER UNIT" shape now resolves via
    the pre-parser and logs successfully (WITH an undo button) even while
    Ollama is down, which would defeat this test's own point. See
    `test_ac16_bare_number_unit_while_ollama_down_logs_with_an_undo_
    button_not_deferred` right below for that new behavior's own coverage."""
    health_monitor = _FrozenHealthMonitor(ollama_up=False)
    channel = FakeChannel()

    await handle_inbound_message(
        "drank some water",
        db=db,
        llm=FakeLLM(),
        channel=channel,
        config=Config(),
        registry=registry,
        clock=_clock(),
        health_monitor=health_monitor,
        user_id=CHAT_ID,
    )

    assert channel.actionable == []
    assert channel.sent == [i18n.t("deferred_ack", "en")]
    assert _log_count(db) == 1  # persisted verbatim as 'unparsed', not lost


async def test_ac16_bare_number_unit_while_ollama_down_logs_with_an_undo_button_not_deferred(db, registry):
    """SPEC-v1.5.md AC-16: the pre-parser gate sits BEFORE the health-
    monitor deferral check -- "500ml" now logs successfully, WITH an undo
    button, even while Ollama is down. Companion to `test_ac4_deferred_
    ack_carries_no_button` just above."""
    health_monitor = _FrozenHealthMonitor(ollama_up=False)
    channel = FakeChannel()

    await handle_inbound_message(
        "500ml",
        db=db,
        llm=FakeLLM(),
        channel=channel,
        config=Config(),
        registry=registry,
        clock=_clock(),
        health_monitor=health_monitor,
        user_id=CHAT_ID,
    )

    assert len(channel.actionable) == 1  # a real confirmation with an undo button, not a deferred ack
    assert channel.sent != [i18n.t("deferred_ack", "en")]
    assert _log_count(db) == 1
    assert db.pending_unparsed() == []


# ===========================================================================
# AC5 end-to-end: a real milestone crossing through handle_inbound_message
# -- the milestone suffix is IN the text AND the single send_actionable call
# still carries exactly one undo button (not two sends).
# ===========================================================================


async def test_ac5_milestone_crossing_confirmation_carries_both_suffix_and_button(db, registry, monkeypatch):
    config = Config()  # gamification: enabled=True, milestones=[3, 7, 30]
    today = date.today()
    # Days 1 & 2 of the streak, seeded directly (goal-met, config default 2500).
    _seed(db, f"{(today - timedelta(days=2)).isoformat()}T09:00:00", "water", 2500.0)
    _seed(db, f"{(today - timedelta(days=1)).isoformat()}T09:00:00", "water", 2500.0)

    patch_parse_message(monkeypatch, ExtractionResult(category="water", value=2500.0, confidence=0.9))
    channel = FakeChannel()

    # Day 3 (today): this log crosses the 3-day milestone.
    await handle_inbound_message(
        "2500ml",
        db=db,
        llm=FakeLLM(),
        channel=channel,
        config=config,
        registry=registry,
        clock=_clock(),
        user_id=CHAT_ID,
    )

    assert len(channel.actionable) == 1  # ONE send, not a milestone line as a second message
    text, buttons = channel.actionable[0]
    assert "2500" in text
    assert i18n.t("milestone_reached", "en", streak=3, label=registry.get("water").label("en")) in text
    assert len(buttons) == 1
    row = db.last_log(CHAT_ID)
    assert buttons[0][1] == f"undo:{row['id']}"


# ===========================================================================
# AC29/AC30 (main.py routing half, explicitly deferred to this pass by
# tests/test_target_nl.py's own docstring): the full-NL target step returns
# BEFORE parse_message is ever called, and writes no `logs` row.
# ===========================================================================


async def test_nl_target_hit_short_circuits_before_parser_and_writes_no_log_row(db, registry, monkeypatch):
    patch_parse_message_forbidden(monkeypatch)

    async def fake_classify(text, llm, registry_, config):
        return TargetIntent(habit_id="water", goal_base_unit=2500.0)

    monkeypatch.setattr(target_nl, "classify_target_intent", fake_classify)
    channel = FakeChannel()

    await handle_inbound_message(
        "from now on I want to drink 2.5L a day",
        db=db,
        llm=FakeLLM(),
        channel=channel,
        config=Config(),
        registry=registry,
        clock=_clock(),
        health_monitor=_FrozenHealthMonitor(ollama_up=True),
        user_id=CHAT_ID,
    )

    assert _log_count(db) == 0  # AC29/AC30: no logs row written
    assert db.get_target(CHAT_ID, "water") == 2500.0
    assert channel.actionable == []  # a target reply is not a log confirmation (R-T10) -- no button
    assert len(channel.sent) == 1


async def test_gate_miss_falls_through_to_parser_and_logs_normally(db, registry, monkeypatch):
    """Sanity converse of the above: an ordinary log message (gate miss)
    is NOT short-circuited -- parse_message IS reached and the row IS
    written, proving the NL step doesn't swallow real logs."""
    patch_parse_message(monkeypatch, ExtractionResult(category="water", value=500.0, confidence=0.9))
    channel = FakeChannel()

    await handle_inbound_message(
        "500ml",
        db=db,
        llm=FakeLLM(),
        channel=channel,
        config=Config(),
        registry=registry,
        clock=_clock(),
        health_monitor=_FrozenHealthMonitor(ollama_up=True),
        user_id=CHAT_ID,
    )

    assert _log_count(db) == 1
    assert len(channel.actionable) == 1


# ===========================================================================
# AC31 full round trip (main.py routing half): a full-NL intent sets a goal
# on the previously goal-less `stretch` habit through the REAL
# handle_inbound_message routing, and the very next log against it
# immediately reflects target-driven goal-met semantics via the real
# consumer modules (streaks/daily summary) -- not just target_nl in
# isolation (already covered by test_target_nl.py) or reminders/streaks
# called directly (already covered by test_v11_shared_surface.py).
# ===========================================================================


async def test_ac31_nl_set_goal_on_goalless_habit_then_consumers_reflect_it_end_to_end(db, registry, monkeypatch):
    config = Config()
    stretch = registry.get("stretch")
    assert stretch.goal is None  # config-goal-less, by shipped config

    async def fake_classify(text, llm, registry_, config_):
        return TargetIntent(habit_id="stretch", goal_base_unit=20.0)

    monkeypatch.setattr(target_nl, "classify_target_intent", fake_classify)
    channel = FakeChannel()

    await handle_inbound_message(
        "from now on I want to stretch 20 minutes a day",
        db=db,
        llm=FakeLLM(),
        channel=channel,
        config=config,
        registry=registry,
        clock=_clock(),
        health_monitor=_FrozenHealthMonitor(ollama_up=True),
        user_id=CHAT_ID,
    )
    assert _log_count(db) == 0  # the NL step itself writes no log row

    # A subsequent stretch log of exactly 20 min now qualifies against the
    # freshly-set target, through the real streaks module.
    _seed(db, _today_ts(), "stretch", 20.0)
    assert streaks.day_qualifies(db, config, stretch, date.today().isoformat(), CHAT_ID) is True

    lines = streaks.compute_daily_summary(db, config, registry, date.today(), CHAT_ID)
    stretch_line = next(line for line in lines if line.habit.id == "stretch")
    assert stretch_line.goal == 20.0


# ===========================================================================
# AC33: Ollama DOWN -- zero NL-classification calls (not even the gate is
# reached), the target-phrased message follows the existing deferral path
# unchanged, and the deterministic `/target` path still works.
# ===========================================================================


async def test_ac33_ollama_down_skips_nl_step_entirely_and_defers_as_unparsed_log(db, registry, monkeypatch):
    def forbidden_gate(text):
        raise AssertionError("looks_like_target_phrasing must not even be called while Ollama is down (R-T16)")

    async def forbidden_classify(*args, **kwargs):
        raise AssertionError("classify_target_intent must not be called while Ollama is down (R-T16)")

    monkeypatch.setattr(target_nl, "looks_like_target_phrasing", forbidden_gate)
    monkeypatch.setattr(target_nl, "classify_target_intent", forbidden_classify)
    channel = FakeChannel()
    text = "from now on I want to drink 2.5L a day"

    await handle_inbound_message(
        text,
        db=db,
        llm=FakeLLM(),
        channel=channel,
        config=Config(),
        registry=registry,
        clock=_clock(),
        health_monitor=_FrozenHealthMonitor(ollama_up=False),
        user_id=CHAT_ID,
    )

    assert channel.sent == [i18n.t("deferred_ack", "en")]
    assert db.get_target(CHAT_ID, "water") is None  # no target was set
    row = db.pending_unparsed()
    assert len(row) == 1
    assert row[0]["raw_message"] == text
    assert row[0]["category"] == "unparsed"


async def test_ac33_deterministic_target_command_still_works_during_ollama_outage(db, registry):
    channel = FakeChannel()

    await handle_inbound_message(
        "/target water 2500",
        db=db,
        llm=FakeLLM(),
        channel=channel,
        config=Config(),
        registry=registry,
        clock=_clock(),
        health_monitor=_FrozenHealthMonitor(ollama_up=False),
        user_id=CHAT_ID,
    )

    assert db.get_target(CHAT_ID, "water") == 2500.0
    assert len(channel.sent) == 1
    assert "2500" in channel.sent[0]


# ===========================================================================
# AC1/AC2/AC6: async_main-level wiring. Mirrors tests/test_charts.py's own
# `_run_async_main_and_capture_scheduler` pattern (patch load_config/
# load_secrets/AsyncIOScheduler/TelegramChannel/OllamaClient; the fake
# channel's run() raises _StopAfterSchedulerStart to unwind cleanly through
# async_main's own try/finally, which closes its own `db`/`llm`/`channel`).
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

    def get_job(self, job_id):
        return self.jobs.get(job_id)


class _FakeOllamaClient:
    """No real network I/O whatsoever (mirrors tests/test_charts.py's own
    `_FakeOllamaClient`)."""

    def __init__(self, *args, **kwargs):
        pass

    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return "noted"

    async def chat_json(self, *args, **kwargs):
        return None

    async def probe_schema_support(self, *args, **kwargs) -> dict:
        return {}

    async def aclose(self) -> None:
        pass


class _AsyncMainFakeChannel(Channel):
    """Records `set_my_commands` calls; optionally replays a queued
    on_message/on_callback pair from inside `run()` before raising
    `_StopAfterSchedulerStart` -- lets a test drive the real closures
    `async_main` wires (AC6), not a re-implementation of them."""

    last_instance: "_AsyncMainFakeChannel | None" = None
    raise_on_set_my_commands: bool = False
    queued_message: str | None = None
    second_message: str | None = None

    def __init__(self, *args, **kwargs) -> None:
        self.sent: list[str] = []
        self.actionable: list[tuple[str, list[Button]]] = []
        self.set_my_commands_calls: list[dict] = []
        _AsyncMainFakeChannel.last_instance = self

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append(text)

    async def send_actionable(self, chat_id: str, text: str, buttons: list[Button]) -> None:
        self.actionable.append((text, buttons))
        self.sent.append(text)

    async def set_my_commands(self, commands, *, scope_chat_id=None) -> None:
        # SPEC-v1.8.md R-D2: only records the default (global) menu
        # registration -- see test_discoverability.py's identical fake for
        # the full rationale. The transport-error simulation below still
        # applies to EITHER call (public or owner-scoped), matching
        # `async_main`'s own independent try/except around each.
        if scope_chat_id is None:
            self.set_my_commands_calls.append(commands)
        if _AsyncMainFakeChannel.raise_on_set_my_commands:
            raise ConnectionError("simulated setMyCommands transport failure")

    async def run(self, on_message, on_callback=None) -> None:
        if _AsyncMainFakeChannel.queued_message is not None:
            # SPEC-v1.2.md R-C2: on_message now takes chat_id first. Uses
            # the same chat id load_secrets() mocks as telegram_chat_id
            # ("fake") -- async_main's real attribute_legacy_to_owner call
            # registers that exact id as the active owner before channel.
            # run() is ever reached, so this is the one user id guaranteed
            # to already exist and be active by this point.
            await on_message("fake", _AsyncMainFakeChannel.queued_message)
            if on_callback is not None and self.actionable:
                # AC6: drive the exact button main.py just attached, proving
                # the on_callback closure round-trips through the real
                # undo_ui.handle_undo_callback with the real db/config/registry.
                _, buttons = self.actionable[-1]
                _, data = buttons[0]
                await on_callback("fake", data, self.actionable[-1][0], "cb-integration-1")
            if _AsyncMainFakeChannel.second_message is not None:
                await on_message("fake", _AsyncMainFakeChannel.second_message)
        raise _StopAfterSchedulerStart()

    async def aclose(self) -> None:
        pass


def _run_async_main(monkeypatch, config):
    from habit_assistant import main as main_module

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module, "load_secrets", lambda: SimpleNamespace(telegram_bot_token="fake", telegram_chat_id="fake")
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", _AsyncMainFakeChannel)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClient)
    _FakeScheduler.last_instance = None
    _AsyncMainFakeChannel.last_instance = None
    _AsyncMainFakeChannel.raise_on_set_my_commands = False
    _AsyncMainFakeChannel.queued_message = None
    _AsyncMainFakeChannel.second_message = None
    return main_module


async def test_ac1_startup_registers_undo_and_target_in_both_languages(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    main_module = _run_async_main(monkeypatch, config)
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)

    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    channel = _AsyncMainFakeChannel.last_instance
    assert channel is not None
    assert len(channel.set_my_commands_calls) == 1
    commands = channel.set_my_commands_calls[0]
    assert set(commands.keys()) == {"en", "th"}
    for lang, entries in commands.items():
        names = [name for name, _desc in entries]
        assert "undo" in names
        assert "target" in names
    # Actually localized, not copy-pasted between languages.
    en_undo_desc = dict(commands["en"])["undo"]
    th_undo_desc = dict(commands["th"])["undo"]
    assert en_undo_desc != th_undo_desc


async def test_ac2_set_my_commands_transport_error_at_startup_never_crashes(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    main_module = _run_async_main(monkeypatch, config)
    _AsyncMainFakeChannel.raise_on_set_my_commands = True
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)

    # Startup must continue past the failure and reach channel.run() --
    # proven by the fact that _StopAfterSchedulerStart (raised from inside
    # run()) is what we observe, not the ConnectionError itself.
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    channel = _AsyncMainFakeChannel.last_instance
    assert channel is not None
    assert len(channel.set_my_commands_calls) == 1  # it was attempted


async def test_ac6_callback_query_routes_through_real_on_callback_and_soft_deletes(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    main_module = _run_async_main(monkeypatch, config)
    _AsyncMainFakeChannel.queued_message = "500ml"
    _AsyncMainFakeChannel.second_message = "500ml"  # AC6 tail: normal message handling still works afterward
    patch_parse_message(monkeypatch, ExtractionResult(category="water", value=500.0, confidence=0.9))
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)

    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    channel = _AsyncMainFakeChannel.last_instance
    assert channel is not None
    # 1st message -> confirmation w/ button; callback -> undo confirmation;
    # 2nd message -> another confirmation w/ button (proves on_callback
    # wiring didn't break normal on_message handling afterward).
    assert len(channel.actionable) == 2
    assert any("Undone" in text or "ยกเลิก" in text for text in channel.sent)

    db = Database(tmp_path / "habits.db")
    try:
        first_row_id = int(channel.actionable[0][1][0][1].split(":")[1])
        row = db.get_log(first_row_id)
        assert row["deleted_at"] is not None  # really soft-deleted through the real db
    finally:
        db.close()
