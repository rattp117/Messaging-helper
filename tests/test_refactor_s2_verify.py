"""SPEC-REFACTOR.md Stage 2 ("main.py decomposition", v1.9.2) -- Vera's own
independent verification, additional to `tests/test_refactor_s2_gaps.py`
(Luna's own exit-bar checks) and the pre-existing 4306-test suite (which
Luna's IMPL-refactor-s2.md relies on as its own byte-identical evidence,
running UNMODIFIED against the new module layout).

This file specifically probes the seams Stage 2 actually touched, at the
WIRED level (through the real `async_main` -> `core/app.py` -> `core/
jobs.py` / `core/routing.py` plumbing, or through `core/routing.py`'s
`on_message`/`on_callback` directly), not just direct unit calls into
`core/confirmation.py`:

1. Full-stack `async_main` wiring: monkeypatch-compat for the load-bearing
   `habit_assistant.main` symbols (rule: the wrapper functions must read
   THIS module's globals at call time, not bind `core/app.py`'s/`core/
   routing.py`'s own), the 6 scheduler jobs actually registering and
   firing through `core/app.py`'s forwarding closures into `core/jobs.py`,
   and Luna's own two self-caught bugs (the dispatch-once sentinel, the
   `__main__` guard) staying dead.
2. Byte-identity spot checks through `core/routing.py`'s real `on_message`/
   `on_callback` (not a hand-rolled reimplementation): typed log
   confirmation (EN+TH, water/stretch/generic), quick-log tap-vs-typed
   parity (now that the mirror is an import, `core/confirmation.py`),
   /help, /audit, backfill's confirmation prefix, a routine run summary.
3. Import-discipline extras beyond `test_refactor_s2_gaps.py`'s own cycle
   test: the module line-count table from IMPL-refactor-s2.md, and
   `core/confirmation.py`'s own "leaf" claim (imports nothing from
   `core/routing.py`/`core/quicklog.py`).
4. An independent re-implementation of `tests/test_riders.py`'s
   `disable_notification` call-site sweep, to confirm it is NOT scoped to
   a hardcoded file list (it globs the whole `src/` tree), i.e. it
   genuinely guards the new files (`core/routing.py`/`core/jobs.py`/
   `core/confirmation.py`) against a 4th silent-send site creeping in.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Button, Channel
from habit_assistant.config import Config
from habit_assistant.core import commands, confirmation, i18n, quicklog, routing
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.registry_provider import RegistryProvider
from habit_assistant.core.reminders import ReminderState
from habit_assistant.storage.db import Database

OWNER = "owner-verify"
MEMBER = "member-verify"

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "habit_assistant"


# ===========================================================================
# Shared harness (mirrors tests/test_v19_integration.py's own
# `_FakeScheduler`/`_ScriptedChannel`/`_run` convention -- each
# integration-adjacent test file keeps its own copy, per this codebase's
# established pattern).
# ===========================================================================


class _StopAfterSchedulerStart(Exception):
    pass


class _FakeScheduler:
    last_instance: "_FakeScheduler | None" = None

    def __init__(self, *args, **kwargs):
        self.jobs: dict[str, SimpleNamespace] = {}
        _FakeScheduler.last_instance = self

    def add_job(self, func, trigger=None, args=None, kwargs=None, id=None, replace_existing=True, **extra):
        self.jobs[id] = SimpleNamespace(func=func, trigger=trigger, args=list(args or []), kwargs=dict(kwargs or {}), id=id)

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def start(self):
        pass

    def shutdown(self, wait=False):
        pass


class _FakeOllamaClientS2:
    responses: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def chat_text(self, system_prompt, user_prompt):
        return "noted"

    async def chat_json(self, system_prompt, user_prompt, json_schema, valid_categories):
        if _FakeOllamaClientS2.responses:
            return _FakeOllamaClientS2.responses.pop(0)
        return json.dumps({"category": "unknown", "value": None, "confidence": 0.1})

    async def probe_schema_support(self, *args, **kwargs) -> dict:
        return {}

    async def aclose(self) -> None:
        pass


class _ScriptedChannelS2(Channel):
    """Drives the REAL `on_message`/`on_callback` closures `core/app.py`
    registers (via `channel.run(_on_message, on_callback=_on_callback)`),
    then optionally fires any registered scheduler job by id, all still
    inside `async_main`'s own live `db`/`provider`/`registry` -- proves
    the wiring end to end, not a hand-assembled call."""

    last_instance: "_ScriptedChannelS2 | None" = None
    script: list[tuple] = []
    run_jobs_before_stop: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        self.sent: list[tuple[str, str, bool]] = []
        self.actionable: list[tuple[str, str, list[Button]]] = []
        self.images: list[tuple[str, bytes, str, bool]] = []
        _ScriptedChannelS2.last_instance = self

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append((chat_id, text, disable_notification))

    async def send_actionable(self, chat_id: str, text: str, buttons: list[Button]) -> None:
        self.actionable.append((chat_id, text, buttons))
        self.sent.append((chat_id, text, False))

    async def send_image(self, chat_id: str, image: bytes, caption: str, *, disable_notification: bool = False) -> None:
        self.images.append((chat_id, image, caption, disable_notification))

    async def set_my_commands(self, commands, *, scope_chat_id=None) -> None:
        pass

    def sent_to(self, chat_id: str) -> list[str]:
        return [text for cid, text, _ in self.sent if cid == chat_id]

    async def run(self, on_message, on_callback=None) -> None:
        for step in _ScriptedChannelS2.script:
            if step[0] == "message":
                _, chat_id, text = step
                await on_message(chat_id, text)
            else:
                _, chat_id, data, source_text, cb_id = step
                assert on_callback is not None
                await on_callback(chat_id, data, source_text, cb_id)
        for job_id in _ScriptedChannelS2.run_jobs_before_stop:
            job = _FakeScheduler.last_instance.jobs.get(job_id)
            if job is not None:
                await job.func(*job.args, **job.kwargs)
        raise _StopAfterSchedulerStart()

    async def aclose(self) -> None:
        pass


def _wire_async_main(monkeypatch, config, script, *, owner_chat_id=OWNER, run_jobs=None, responses=None):
    from habit_assistant import main as main_module

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module, "load_secrets", lambda: SimpleNamespace(telegram_bot_token="fake", telegram_chat_id=owner_chat_id)
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", _ScriptedChannelS2)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClientS2)
    monkeypatch.setattr(main_module, "__version__", "0.0.0-test")
    _FakeScheduler.last_instance = None
    _ScriptedChannelS2.last_instance = None
    _ScriptedChannelS2.script = script
    _ScriptedChannelS2.run_jobs_before_stop = list(run_jobs or [])
    _FakeOllamaClientS2.responses = list(responses or [])
    return main_module


async def _run_wired(monkeypatch, config, script, **kwargs):
    main_module = _wire_async_main(monkeypatch, config, script, **kwargs)
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)
    return main_module, _ScriptedChannelS2.last_instance, _FakeScheduler.last_instance


# ===========================================================================
# 1. Full-stack async_main wiring: the 6 scheduler jobs actually register
#    and fire through core/app.py's forwarding closures into core/jobs.py
#    (not a copy left behind), and monkeypatch back-compat for the
#    load-bearing habit_assistant.main symbols that thread INTO those jobs
#    (run_due_reminders, render_weekly_review_charts, __version__).
# ===========================================================================


async def test_all_six_jobs_register_with_expected_ids(monkeypatch, tmp_path):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _, _, scheduler = await _run_wired(monkeypatch, config, script=[])

    assert set(scheduler.jobs) == {
        "minutely_tick",
        "dashboard_day_rollover",
        "weekly_review",
        "daily_summary",
        "grace_tick",
        "wrapped_auto",
    }


async def test_minutely_tick_job_fires_through_core_jobs_minutely_tick(monkeypatch, tmp_path):
    """core/app.py registers a ZERO-ARG closure (`_minutely_tick`) for the
    scheduler; this proves that closure's body is a real call into
    `core/jobs.py:minutely_tick` (not a leftover copy of the old inline
    tick), by monkeypatching `core.jobs.minutely_tick` itself and
    confirming the scheduler-fired closure reaches it."""
    from habit_assistant.core import jobs as jobs_module

    calls = []
    real_minutely_tick = jobs_module.minutely_tick

    async def spy(*args, **kwargs):
        calls.append((args, kwargs))
        await real_minutely_tick(*args, **kwargs)

    monkeypatch.setattr(jobs_module, "minutely_tick", spy)
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})

    await _run_wired(monkeypatch, config, script=[], run_jobs=["minutely_tick"])

    assert len(calls) == 1, "core/app.py's registered closure must call core.jobs.minutely_tick exactly once"


async def test_run_due_reminders_monkeypatched_on_main_reaches_minutely_tick(monkeypatch, tmp_path):
    """Symbol-compatibility map claim: patching `habit_assistant.main.
    run_due_reminders` must still take effect, even though the real call
    site now lives in `core/jobs.py:minutely_tick`, three modules away
    from `main.py`. Proves the wrapper reads main.py's OWN globals at call
    time (rather than core/app.py's own import) end to end, through a
    REAL scheduler-fired job -- not a direct function call."""
    calls = []

    async def fake_run_due_reminders(*args, **kwargs):
        calls.append((args, kwargs))

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    main_module = _wire_async_main(monkeypatch, config, script=[], run_jobs=["minutely_tick"])
    monkeypatch.setattr(main_module, "run_due_reminders", fake_run_due_reminders)

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    assert len(calls) == 1, "monkeypatching main.run_due_reminders must reach core/jobs.py:minutely_tick's call site"


async def test_render_weekly_review_charts_monkeypatched_on_main_reaches_weekly_review_job(monkeypatch, tmp_path):
    """Same claim as above, for the weekly_review job's own overridable
    dependency -- the call site now lives in `core/jobs.py:
    weekly_review_job`."""
    calls = []

    def fake_render(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    db = Database(tmp_path / "habits.db")
    db.upsert_user(OWNER, role="owner", status="active")
    # weekly_review_job has no injectable clock (matches the pre-Stage-2
    # original -- date.today() is real wall-clock time), so seed a log
    # from just now to land inside its own 7-day window regardless of
    # when this test happens to run.
    db.insert_log(_water_log(datetime.now().isoformat(timespec="seconds"), OWNER))
    db.close()

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    main_module = _wire_async_main(monkeypatch, config, script=[], run_jobs=["weekly_review"], owner_chat_id=OWNER)
    monkeypatch.setattr(main_module, "render_weekly_review_charts", fake_render)

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    assert len(calls) == 1, "monkeypatching main.render_weekly_review_charts must reach core/jobs.py:weekly_review_job"


async def test_version_monkeypatched_on_main_threads_into_announce_release(monkeypatch, tmp_path):
    """`__version__` is forwarded as async_main's `version=` kwarg
    (core/app.py), read by main.py's wrapper from ITS OWN current global
    at call time -- proven here by spying on `core.app.announce.
    announce_release` and checking the exact patched string arrives."""
    from habit_assistant.core import app as app_module

    seen_versions = []
    real_announce = app_module.announce.announce_release

    async def spy(db, channel, config, version):
        seen_versions.append(version)
        await real_announce(db, channel, config, version)

    monkeypatch.setattr(app_module.announce, "announce_release", spy)
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    main_module = _wire_async_main(monkeypatch, config, script=[])
    monkeypatch.setattr(main_module, "__version__", "9.9.9-verify")

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    assert seen_versions == ["9.9.9-verify"]


async def test_parse_message_monkeypatched_on_main_reaches_routing_via_wrapper(monkeypatch, tmp_path):
    """Symbol-compatibility map claim for `parse_message`: patched on
    `habit_assistant.main`, must take effect through `main.py`'s
    `handle_inbound_message` wrapper -> `core/routing.py`'s real
    implementation, for an ordinary (non-command) message that reaches
    the LLM extraction path."""
    from habit_assistant import main as main_module
    from habit_assistant.llm.ollama_client import ExtractionResult

    calls = []

    async def fake_parse_message(text, llm, registry, confidence_threshold=None):
        calls.append(text)
        return ExtractionResult("water", 250.0, 0.9)

    monkeypatch.setattr(main_module, "parse_message", fake_parse_message)

    db = Database(tmp_path / "habits.db")
    db.upsert_user(OWNER, role="owner", status="active")
    config = Config()
    registry = HabitRegistry.from_config(config)

    class _RecChannel(Channel):
        def __init__(self):
            self.actionable = []

        async def send(self, chat_id, text, *, disable_notification=False):
            pass

        async def send_actionable(self, chat_id, text, buttons):
            self.actionable.append(text)

        async def run(self, on_message, on_callback=None):
            raise NotImplementedError

    channel = _RecChannel()
    try:
        await main_module.handle_inbound_message(
            "gibberish that isn't a deterministic parse",
            db=db,
            llm=None,
            channel=channel,
            config=config,
            registry=registry,
            clock=lambda: datetime(2026, 8, 24, 9, 0, 0),
            user_id=OWNER,
        )
    finally:
        db.close()

    assert calls, "patched main.parse_message must be invoked via core/routing.py:handle_inbound_message"
    assert channel.actionable, "the fake parse result should still have produced a real confirmation"


async def test_setup_logging_monkeypatched_on_main_is_called_by_async_main(monkeypatch, tmp_path):
    """`setup_logging` is defined (not just re-exported) in main.py and
    forwarded as an explicit kwarg into core/app.py:async_main -- proves
    a monkeypatched replacement is actually invoked, not skipped."""
    calls = []
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    main_module = _wire_async_main(monkeypatch, config, script=[])
    monkeypatch.setattr(main_module, "setup_logging", lambda level: calls.append(level))

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    assert calls == [config.app.log_level]


# ===========================================================================
# Monkeypatch compat on the NEW module paths -- tomorrow's tests will patch
# core.routing/core.jobs directly, not habit_assistant.main.
# ===========================================================================


async def test_monkeypatching_commands_dispatch_on_core_routing_module_takes_effect(db_owner, monkeypatch):
    """`core/routing.py` does `from habit_assistant.core import ... commands
    ...` and calls `commands.dispatch(...)` -- patching `core.commands.
    dispatch` (the real module attribute routing.py reads through the
    `commands` namespace) must be observed by `on_message`."""
    from habit_assistant.core import commands as commands_module

    calls = []
    real_dispatch = commands_module.dispatch

    def spy(text, registry):
        calls.append(text)
        return real_dispatch(text, registry)

    monkeypatch.setattr(commands_module, "dispatch", spy)

    config = Config.model_validate({})
    channel = _RecordingChannelBasic()
    provider = RegistryProvider(config, db_owner)

    await routing.on_message(
        OWNER,
        "/help",
        db=db_owner,
        llm=None,
        channel=channel,
        config=config,
        owner_chat_id=OWNER,
        provider=provider,
        scheduler=_NoopScheduler(),
        reminder_state=ReminderState(),
        health_monitor=_FakeHealthMonitorUp(),
    )

    assert calls == ["/help"]


async def test_monkeypatching_checkins_run_due_checkins_on_core_jobs_module_takes_effect(db_owner):
    """`core/jobs.py` imports `checkins` at module scope and calls
    `checkins.run_due_checkins(...)` inside `minutely_tick` -- confirms
    the call is reachable/spy-able through THAT module reference (the
    shape tomorrow's tests patching `core.jobs.checkins.run_due_checkins`
    -- not `core.checkins.run_due_checkins` -- would rely on), and that
    per-tick fan-out order (reminders -> checkins -> nudge) survived the
    move."""
    from habit_assistant.core import jobs as jobs_module

    order = []

    async def fake_reminders(*a, **k):
        order.append("reminders")

    async def fake_checkins(*a, **k):
        order.append("checkins")

    async def fake_nudge(*a, **k):
        order.append("nudge")

    import unittest.mock as mock

    with mock.patch.object(jobs_module.checkins, "run_due_checkins", fake_checkins), mock.patch.object(
        jobs_module.nudge, "run_due_nudges", fake_nudge
    ):
        config = Config()
        registry = HabitRegistry.from_config(config)
        provider = RegistryProvider(config, db_owner)
        await jobs_module.minutely_tick(
            _RecordingChannelBasic(), config, registry, db_owner, ReminderState(), provider, run_due_reminders=fake_reminders
        )

    assert order == ["reminders", "checkins", "nudge"], "Stage 1's fan-out order must survive the Stage 2 move"


async def test_minutely_tick_per_function_isolation_survived_the_move_to_core_jobs(db_owner):
    """TEST-refactor-s1.md's own finding, re-run against the RELOCATED
    `core/jobs.py:minutely_tick`: one function raising (e.g. a DB read
    error escaping `run_due_reminders`) must not abort the tick -- the
    other two still run, each wrapped in its own try/except (mirrors
    `core/jobs.py:minutely_tick`'s own docstring, "restoring the
    pre-consolidation per-tick isolation an earlier round of testing
    found this merge had dropped")."""
    from habit_assistant.core import jobs as jobs_module

    order = []

    async def raising_reminders(*a, **k):
        order.append("reminders")
        raise RuntimeError("boom")

    async def fake_checkins(*a, **k):
        order.append("checkins")

    async def fake_nudge(*a, **k):
        order.append("nudge")

    import unittest.mock as mock

    with mock.patch.object(jobs_module.checkins, "run_due_checkins", fake_checkins), mock.patch.object(
        jobs_module.nudge, "run_due_nudges", fake_nudge
    ):
        config = Config()
        registry = HabitRegistry.from_config(config)
        provider = RegistryProvider(config, db_owner)
        # Must not raise out of minutely_tick itself.
        await jobs_module.minutely_tick(
            _RecordingChannelBasic(), config, registry, db_owner, ReminderState(), provider, run_due_reminders=raising_reminders
        )

    assert order == ["reminders", "checkins", "nudge"], (
        f"a raising run_due_reminders must not prevent checkins/nudge from still running this tick, got {order}"
    )


# ===========================================================================
# 2. Dispatch-once bug (Luna's own self-caught bug #1) stays dead --
#    through an ordinary log message, a recognized command, AND a callback
#    (which must never re-parse the text as a command at all).
# ===========================================================================


class _RecordingChannelBasic(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.actionable: list[tuple[str, str, list[Button]]] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append((chat_id, text))

    async def send_actionable(self, chat_id: str, text: str, buttons: list[Button]) -> None:
        self.actionable.append((chat_id, text, buttons))
        self.sent.append((chat_id, text))

    async def set_my_commands(self, commands, *, scope_chat_id=None) -> None:
        pass

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError


class _NoopScheduler:
    def add_job(self, *a, **k):
        pass


class _FakeHealthMonitorUp:
    ollama_up = True


@pytest.fixture
def db_owner(tmp_path):
    database = Database(tmp_path / "dispatch_once.db")
    database.upsert_user(OWNER, role="owner", status="active")
    yield database
    database.close()


@pytest.mark.parametrize(
    "text",
    ["500ml", "/help"],
    ids=["ordinary_log_message", "a_recognized_command"],
)
async def test_dispatch_called_exactly_once_per_on_message_call(db_owner, monkeypatch, text):
    config = Config.model_validate({})
    channel = _RecordingChannelBasic()
    provider = RegistryProvider(config, db_owner)
    calls = []
    real_dispatch = commands.dispatch

    def counting_dispatch(t, registry):
        calls.append(t)
        return real_dispatch(t, registry)

    monkeypatch.setattr(commands, "dispatch", counting_dispatch)

    await routing.on_message(
        OWNER,
        text,
        db=db_owner,
        llm=None,
        channel=channel,
        config=config,
        owner_chat_id=OWNER,
        provider=provider,
        scheduler=_NoopScheduler(),
        reminder_state=ReminderState(),
        health_monitor=_FakeHealthMonitorUp(),
    )

    assert calls == [text], f"commands.dispatch must run exactly once per message, got {calls}"
    assert channel.sent, "the message should still have produced a reply"


async def test_on_callback_never_calls_commands_dispatch_at_all(db_owner, monkeypatch):
    """A button tap is routed by payload PREFIX (`log:`/`routine:run:`/
    else), never by re-parsing `source_text` through `commands.dispatch`
    -- confirms `on_callback`'s own routing didn't grow a spurious dispatch
    call during the Stage 2 move (the flip side of the dispatch-once bug:
    a callback that accidentally dispatched would silently risk
    misclassifying a stale/forged payload as a live command)."""
    config = Config.model_validate({})
    channel = _RecordingChannelBasic()
    provider = RegistryProvider(config, db_owner)
    calls = []
    real_dispatch = commands.dispatch

    def counting_dispatch(t, registry):
        calls.append(t)
        return real_dispatch(t, registry)

    monkeypatch.setattr(commands, "dispatch", counting_dispatch)

    await routing.on_callback(
        OWNER, "log:water:500", "👇 Tap to log:", "cb-1", db=db_owner, channel=channel, config=config, provider=provider
    )

    assert calls == [], f"on_callback must never call commands.dispatch, got {calls}"
    assert channel.sent, "the tap should still have produced a confirmation"


async def test_dunder_main_guard_runs_via_subprocess_against_scratch_config(tmp_path):
    """Luna's own self-caught bug #2: the rewritten main.py initially
    dropped `if __name__ == "__main__": main()`, making `python -m
    habit_assistant.main` a silent no-op. Re-run the exact regression
    shape via a real subprocess (not an in-process import, which would
    hit `if __name__ == "__main__"` == False and prove nothing) against a
    scratch `--dry-run`, cwd-scoped so it can never touch the real
    data/habits.db."""
    result = subprocess.run(
        [sys.executable, "-m", "habit_assistant.main", "--dry-run", "500ml"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "'category': 'water'" in result.stdout
    assert "'value': 500.0" in result.stdout

    # The scratch DB was created under this subprocess's OWN cwd (never
    # the real repo's data/habits.db), and --dry-run must write no log row.
    db_path = tmp_path / "data" / "habits.db"
    assert db_path.exists()
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        assert count == 0, "--dry-run must never write a logs row"
    finally:
        conn.close()


# ===========================================================================
# 3. Byte-identity spot checks through core/routing.py's REAL on_message/
#    on_callback -- typed EN+TH, tap-vs-typed parity (incl. stored /lang
#    pref), /help, /audit, backfill prefix, routine run summary.
# ===========================================================================


def _water_log(ts: str, user_id: str = OWNER):
    from habit_assistant.storage.models import LogEntry

    return LogEntry(None, user_id, ts, "water", 500.0, None, "seed", "reply")


async def _on_message_reply(db, config, provider, text: str, *, user_id=OWNER) -> str:
    channel = _RecordingChannelBasic()
    await routing.on_message(
        user_id,
        text,
        db=db,
        llm=None,
        channel=channel,
        config=config,
        owner_chat_id=OWNER,
        provider=provider,
        scheduler=_NoopScheduler(),
        reminder_state=ReminderState(),
        health_monitor=_FakeHealthMonitorUp(),
    )
    assert channel.sent, f"expected a reply for {text!r}"
    return channel.sent[-1][1]


async def test_typed_log_confirmation_water_en_via_on_message(tmp_path):
    db = Database(tmp_path / "a.db")
    db.upsert_user(OWNER, role="owner", status="active")
    try:
        config = Config()
        provider = RegistryProvider(config, db)
        reply = await _on_message_reply(db, config, provider, "500ml")
        assert i18n.detect_language(reply) == "en"
        assert "500" in reply
    finally:
        db.close()


async def test_typed_log_confirmation_water_th_via_on_message(tmp_path):
    db = Database(tmp_path / "a.db")
    db.upsert_user(OWNER, role="owner", status="active")
    try:
        config = Config()
        provider = RegistryProvider(config, db)
        reply = await _on_message_reply(db, config, provider, "500มล.")
        assert i18n.detect_language(reply) == "th"
        assert "500" in reply
    finally:
        db.close()


async def test_typed_log_confirmation_stretch_and_generic_custom_habit_via_on_message(tmp_path):
    config = Config(
        habits=[
            *Config().habits,
            {"id": "juice", "type": "numeric", "goal": 1000, "label": {"en": "juice", "th": "น้ำผลไม้"}, "unit": {"en": "jml", "th": "จมล."}},
        ]
    )
    db = Database(tmp_path / "a.db")
    db.upsert_user(OWNER, role="owner", status="active")
    try:
        provider = RegistryProvider(config, db)
        stretch_reply = await _on_message_reply(db, config, provider, "10min")
        assert "10" in stretch_reply

        juice_reply = await _on_message_reply(db, config, provider, "250jml")
        assert "250" in juice_reply and "1000" in juice_reply
    finally:
        db.close()


async def test_quicklog_tap_vs_typed_parity_via_on_message_and_on_callback_incl_stored_lang_pref(tmp_path):
    """Rule 11/AC8's own contract, re-proven at the WIRED level (through
    `on_message`/`on_callback`, not a direct `handle_inbound_message`/
    `handle_log_callback` call as `tests/test_quicklog.py` already does)
    -- including a stored `/lang` preference, which both paths must
    resolve identically via the SAME `user_prefs.stored_language_pref`
    call (rule 12a's dedup target, already consolidated pre-Stage-2)."""
    config = Config()

    db_typed = Database(tmp_path / "typed.db")
    db_typed.upsert_user(OWNER, role="owner", status="active")
    db_tapped = Database(tmp_path / "tapped.db")
    db_tapped.upsert_user(OWNER, role="owner", status="active")
    try:
        for db in (db_typed, db_tapped):
            provider = RegistryProvider(config, db)
            await routing.on_message(
                OWNER,
                "/lang th",
                db=db,
                llm=None,
                channel=_RecordingChannelBasic(),
                config=config,
                owner_chat_id=OWNER,
                provider=provider,
                scheduler=_NoopScheduler(),
                reminder_state=ReminderState(),
                health_monitor=_FakeHealthMonitorUp(),
            )

        provider_typed = RegistryProvider(config, db_typed)
        typed = await _on_message_reply(db_typed, config, provider_typed, "500ml")

        provider_tapped = RegistryProvider(config, db_tapped)
        channel = _RecordingChannelBasic()
        await routing.on_callback(
            OWNER, "log:water:500", "👇 Tap to log:", "cb-parity", db=db_tapped, channel=channel, config=config, provider=provider_tapped
        )
        tapped = channel.sent[-1][1]

        assert i18n.detect_language(typed) == "th", "the stored /lang th preference must apply to the typed path"
        assert i18n.detect_language(tapped) == "th", "the stored /lang th preference must apply to the tap path too"
        assert typed == tapped, "typed vs tapped confirmation must be byte-identical, including the stored language pref"
    finally:
        db_typed.close()
        db_tapped.close()


async def test_help_command_via_on_message_matches_discoverability_build_help_text(tmp_path):
    from habit_assistant.core import discoverability

    db = Database(tmp_path / "a.db")
    db.upsert_user(OWNER, role="owner", status="active")
    try:
        config = Config()
        provider = RegistryProvider(config, db)
        reply = await _on_message_reply(db, config, provider, "/help")
        assert reply == discoverability.build_help_text(config, "en")
    finally:
        db.close()


async def test_audit_command_via_on_message_owner_only_matches_audit_view_render_recent(tmp_path):
    from habit_assistant.core import audit_view

    db = Database(tmp_path / "a.db")
    db.upsert_user(OWNER, role="owner", status="active")
    db.upsert_user(MEMBER, role="member", status="active")
    try:
        config = Config()
        provider = RegistryProvider(config, db)
        # Generate one audited event (an edit) so /audit has a real row.
        await _on_message_reply(db, config, provider, "500ml")

        owner_reply = await _on_message_reply(db, config, provider, "/audit")
        expected = audit_view.render_recent(db, config, "en", limit=None, owner_chat_id=OWNER)
        assert owner_reply == expected

        # A non-owner /audit is a silent no-op (no reply at all) -- proves
        # the owner-only gate survived the move into core/routing.py.
        channel = _RecordingChannelBasic()
        await routing.on_message(
            MEMBER,
            "/audit",
            db=db,
            llm=None,
            channel=channel,
            config=config,
            owner_chat_id=OWNER,
            provider=provider,
            scheduler=_NoopScheduler(),
            reminder_state=ReminderState(),
            health_monitor=_FakeHealthMonitorUp(),
        )
        assert channel.sent == []
    finally:
        db.close()


async def test_backfill_confirmation_prefix_via_on_message(tmp_path):
    from habit_assistant.core import backfill

    db = Database(tmp_path / "a.db")
    db.upsert_user(OWNER, role="owner", status="active")
    try:
        config = Config()
        provider = RegistryProvider(config, db)
        channel = _RecordingChannelBasic()
        fixed_clock = lambda: datetime(2026, 8, 24, 9, 0, 0)  # noqa: E731

        # on_message itself doesn't take a clock override, so drive
        # handle_inbound_message directly for the fixed-clock backfill
        # case, through core/routing.py (the real relocated module), not
        # the main.py wrapper.
        await routing.handle_inbound_message(
            "500ml yesterday",
            db=db,
            llm=None,
            channel=channel,
            config=config,
            registry=HabitRegistry.from_config(config),
            clock=fixed_clock,
            user_id=OWNER,
        )
        assert channel.sent, "expected a backfill confirmation"
        reply = channel.sent[-1][1]
        yesterday = fixed_clock().date().replace(day=fixed_clock().date().day - 1)
        expected_prefix = backfill.confirmation_prefix(yesterday, "en")
        assert reply.startswith(expected_prefix), f"expected prefix {expected_prefix!r} in {reply!r}"
    finally:
        db.close()


async def test_routine_run_summary_via_on_message(tmp_path):
    db = Database(tmp_path / "a.db")
    db.upsert_user(OWNER, role="owner", status="active")
    try:
        config = Config()
        provider = RegistryProvider(config, db)
        await _on_message_reply(db, config, provider, "/routine morning = water 500, stretch 10")
        reply = await _on_message_reply(db, config, provider, "/routine morning")
        assert "2 of 2" in reply
        today = datetime.now().date().isoformat()
        assert db.sum_value(OWNER, "water", today) == 500.0
        assert db.sum_value(OWNER, "stretch", today) == 10.0
    finally:
        db.close()


# ===========================================================================
# 4. Import discipline extras -- line-count table (IMPL-refactor-s2.md) and
#    core/confirmation.py's own "cycle-free leaf" claim.
# ===========================================================================


def _ast_line_count(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    return max((getattr(node, "end_lineno", 0) or 0) for node in ast.walk(tree))


def test_module_line_counts_match_impl_refactor_s2_table():
    """IMPL-refactor-s2.md's own line-count table (AST-verified, `end_lineno`
    max, matching SPEC-REFACTOR.md §2's own methodology) -- re-derived
    independently rather than trusted at face value."""
    counts = {
        "main.py": _ast_line_count(SRC_ROOT / "main.py"),
        "core/app.py": _ast_line_count(SRC_ROOT / "core" / "app.py"),
        "core/jobs.py": _ast_line_count(SRC_ROOT / "core" / "jobs.py"),
        "core/routing.py": _ast_line_count(SRC_ROOT / "core" / "routing.py"),
        "core/confirmation.py": _ast_line_count(SRC_ROOT / "core" / "confirmation.py"),
    }
    assert counts["main.py"] < 150, counts
    # A generous tolerance band around IMPL's own claimed counts (574/205/
    # 900/190) -- proves the claim is in the right ballpark and main.py's
    # own hard AC6 hasn't regressed, without being brittle to a future
    # one-line comment edit.
    assert 400 <= counts["core/app.py"] <= 750, counts
    assert 100 <= counts["core/jobs.py"] <= 300, counts
    assert 700 <= counts["core/routing.py"] <= 1100, counts
    assert 100 <= counts["core/confirmation.py"] <= 300, counts


def test_confirmation_leaf_imports_nothing_from_routing_or_quicklog_or_main():
    """Rule 11/AC8's own precondition: `core/confirmation.py` must be a
    genuine LEAF -- importing it from both `core/routing.py` and `core/
    quicklog.py` is only cycle-safe if it doesn't import either of them
    (or main.py) back."""
    tree = ast.parse((SRC_ROOT / "core" / "confirmation.py").read_text(encoding="utf-8"))
    forbidden = {"habit_assistant.core.routing", "habit_assistant.core.quicklog", "habit_assistant.main"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module in forbidden or any(node.module.startswith(f + ".") for f in forbidden):
                found.add(node.module)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden:
                    found.add(alias.name)
    assert found == set(), f"core/confirmation.py must not import: {found}"


def test_quicklog_imports_confirmation_not_the_reverse():
    quicklog_imports = set()
    tree = ast.parse((SRC_ROOT / "core" / "quicklog.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "habit_assistant.core":
            for alias in node.names:
                quicklog_imports.add(alias.name)
    assert "confirmation" in quicklog_imports

    confirmation_imports = set()
    tree2 = ast.parse((SRC_ROOT / "core" / "confirmation.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree2):
        if isinstance(node, ast.ImportFrom) and node.module == "habit_assistant.core":
            for alias in node.names:
                confirmation_imports.add(alias.name)
    assert "quicklog" not in confirmation_imports


# ===========================================================================
# 5. Independent re-derivation of tests/test_riders.py's own
#    disable_notification sweep -- confirms it is NOT scoped to a
#    hardcoded file list (it globs the whole src/ tree), so it genuinely
#    guards the 3 new files Stage 2 introduced (routing.py/jobs.py/
#    confirmation.py) against a 4th silent-send site.
# ===========================================================================


def test_riders_sweep_globs_the_whole_tree_not_a_hardcoded_file_list():
    """Reads tests/test_riders.py's own sweep SOURCE to confirm the file
    enumeration is `SRC_ROOT.rglob("*.py")` (a live directory walk), not a
    literal list of paths -- if a future edit narrowed it to a fixed list,
    this test fails and flags the regression."""
    source = (Path(__file__).resolve().parent / "test_riders.py").read_text(encoding="utf-8")
    assert 'SRC_ROOT.rglob("*.py")' in source, (
        "tests/test_riders.py's disable_notification sweep no longer walks the whole src/ tree -- "
        "it may have been narrowed to a hardcoded file list, which would silently stop guarding "
        "core/routing.py, core/jobs.py, and core/confirmation.py"
    )


def test_independent_disable_notification_sweep_matches_test_riders_expectation():
    """A from-scratch re-implementation of the same sweep (not calling
    test_riders.py's function -- an independent regex pass), to catch a
    bug in that file's OWN sweep logic that a self-referential check
    couldn't. Confirms core/routing.py and core/confirmation.py (new
    Stage 2 territory the pre-Stage-2 sweep never scanned, since they
    didn't exist) carry ZERO disable_notification call sites, and
    core/jobs.py carries exactly 2 (grace_tick + wrapped_auto_job)."""
    call_site_re = re.compile(r"channel\.send\([^)]*disable_notification=", re.DOTALL)
    excluded_dirs = {SRC_ROOT / "channels"}

    found: dict[Path, int] = {}
    for path in SRC_ROOT.rglob("*.py"):
        if any(path.is_relative_to(d) for d in excluded_dirs):
            continue
        n = len(call_site_re.findall(path.read_text(encoding="utf-8")))
        if n:
            found[path] = n

    expected = {
        SRC_ROOT / "core" / "reminders.py": 1,
        SRC_ROOT / "core" / "checkins.py": 1,
        SRC_ROOT / "core" / "nudge.py": 1,
        SRC_ROOT / "core" / "jobs.py": 2,
    }
    assert found == expected, f"unexpected disable_notification call sites (independent re-check): {found}"
    assert SRC_ROOT / "core" / "routing.py" not in found
    assert SRC_ROOT / "core" / "confirmation.py" not in found
    assert SRC_ROOT / "core" / "app.py" not in found
    assert SRC_ROOT / "main.py" not in found
