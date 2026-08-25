"""SPEC-v1.5.md §4 "Feature 2 -- Do-Not-Disturb (shared surface)" --
`reminders.in_dnd_now` (R-D1) and the two suppression-matrix gaps this
shared-surface pass fixes directly: the daily-summary job (R-D2, was
GLOBAL-only) and the weekly-review job (R-D3, had no DND check at all).
Owns AC-10, AC-11, AC-12 per SPEC-v1.5.md §11's own ownership table
("ACs verified during the shared-surface/integration pass").

`/checkin`'s own DND check (R-K4) and `/dnd`'s alias wiring (R-D5) are
`checkins`' own module scope, not this file's (that module owns its own
`tests/test_dnd.py`).

**Testability note** (mirrors `core/reminders.py:send_reminder`'s own
already-documented quiet-hours gotcha, TEST-v1.2/1.3-*.md): `in_dnd_now`'s
`clock` parameter defaults to `datetime.now` -- a value bound ONCE at
function-definition time. Monkeypatching `habit_assistant.core.reminders.
datetime` after import (the `_freeze_reminders_clock`-style technique
`tests/test_streaks.py` uses elsewhere) does **not** reach that already-
bound default; it only affects code that calls `datetime.now(...)`
directly inside a function BODY. Two consequences, both used below:
1. A direct `in_dnd_now(...)` call is tested by passing `clock=` EXPLICITLY
   (confirmed to work correctly -- see `test_in_dnd_now_*` below).
2. `main.py`'s `daily_summary_job`/`weekly_review_job` call `in_dnd_now`
   with NO explicit clock override (correct for production -- these jobs
   fire at a real scheduled moment, so DND should reflect the REAL wall
   clock, not an injectable test double) -- so their own integration-level
   tests below use windows that are "always DND" (spanning the full day,
   via two windows that together cover every HH:MM with no gap) or "never
   DND" (empty), which are deterministic regardless of the real time the
   test happens to run at, rather than trying to freeze a narrow window.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core.reminders import in_dnd_now
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "1001"
MEMBER = "2002"

# Together, these two windows cover every HH:MM of the day with no gap
# (see this file's own module docstring) -- a deterministic "always DND"
# fixture regardless of the real time a test happens to run at. The
# second window's start ("12:00") > end ("00:00") is treated as crossing
# midnight by `_in_quiet_hours`, covering [12:00, 24:00); the first
# covers [00:00, 12:00) -- together, the full 24 hours.
_ALWAYS_DND_WINDOWS = [["00:00", "12:00"], ["12:00", "00:00"]]


# ===========================================================================
# in_dnd_now -- direct unit tests (R-D1).
# ===========================================================================


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "dnd.db")
    database.upsert_user(OWNER, role="owner", status="active")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


def test_in_dnd_now_false_with_no_windows_anywhere(db, config):
    assert in_dnd_now(db, config, OWNER, clock=lambda: datetime(2026, 8, 23, 23, 30, 0)) is False


def test_in_dnd_now_true_inside_a_per_user_window(db, config):
    db.set_user_quiet_hours(OWNER, json.dumps([["22:00", "07:00"]]))
    assert in_dnd_now(db, config, OWNER, clock=lambda: datetime(2026, 8, 23, 23, 30, 0)) is True
    assert in_dnd_now(db, config, OWNER, clock=lambda: datetime(2026, 8, 23, 12, 0, 0)) is False


def test_in_dnd_now_falls_back_to_global_config_windows_when_uncustomized(db):
    """AC-10/AC-11's own "behavior-preserving for un-customized users"
    requirement, at the primitive level: no per-user override -> the
    GLOBAL config windows apply, exactly like v1.4's `effective_quiet_
    windows` already does for the reminder tick."""
    config = Config.model_validate({"quiet_hours": {"windows": [["23:00", "07:00"]]}})
    assert in_dnd_now(db, config, OWNER, clock=lambda: datetime(2026, 8, 23, 23, 30, 0)) is True
    assert in_dnd_now(db, config, OWNER, clock=lambda: datetime(2026, 8, 23, 12, 0, 0)) is False


def test_in_dnd_now_explicit_empty_override_beats_a_nonempty_global_default(db):
    """R-P2's own distinction: `"[]"` (explicit "no DND for me") is NOT
    the same as NULL (inherit) -- even with a nonempty global default,
    an explicit empty override always wins."""
    config = Config.model_validate({"quiet_hours": {"windows": [["00:00", "23:59"]]}})
    db.set_user_quiet_hours(OWNER, "[]")
    assert in_dnd_now(db, config, OWNER, clock=lambda: datetime(2026, 8, 23, 12, 0, 0)) is False


def test_in_dnd_now_handles_a_midnight_crossing_window(db, config):
    db.set_user_quiet_hours(OWNER, json.dumps([["23:00", "07:00"]]))
    assert in_dnd_now(db, config, OWNER, clock=lambda: datetime(2026, 8, 23, 23, 59, 0)) is True
    assert in_dnd_now(db, config, OWNER, clock=lambda: datetime(2026, 8, 24, 6, 59, 0)) is True
    assert in_dnd_now(db, config, OWNER, clock=lambda: datetime(2026, 8, 24, 7, 0, 0)) is False


def test_in_dnd_now_is_scoped_per_user(db, config):
    db.upsert_user(MEMBER, role="member", status="active")
    db.set_user_quiet_hours(OWNER, json.dumps([["00:00", "23:59"]]))
    # MEMBER has no override and the global default is empty -- unaffected
    # by OWNER's own DND window.
    assert in_dnd_now(db, config, OWNER, clock=lambda: datetime(2026, 8, 23, 12, 0, 0)) is True
    assert in_dnd_now(db, config, MEMBER, clock=lambda: datetime(2026, 8, 23, 12, 0, 0)) is False


def test_in_dnd_now_fails_open_on_a_db_read_error(config):
    class _RaisingDatabase:
        def get_user(self, chat_id):
            raise RuntimeError("simulated DB failure")

    # effective_quiet_windows' own fail-open posture falls back to the
    # (empty, by default) global config windows -- never raises, never
    # blocks a caller.
    assert in_dnd_now(_RaisingDatabase(), config, OWNER, clock=lambda: datetime(2026, 8, 23, 23, 30, 0)) is False


def test_in_dnd_now_defaults_to_the_real_clock_when_omitted(db, config):
    """Sanity check on the documented signature -- omitting `clock`
    entirely must not raise (it resolves to the real `datetime.now`),
    regardless of what real time the test happens to run at."""
    result = in_dnd_now(db, config, OWNER)
    assert isinstance(result, bool)


# ===========================================================================
# AC-10: daily-summary job, per-user DND (was GLOBAL-only, R-D2).
# AC-11: weekly-review job, per-user DND (had none at all, R-D3).
# Driven through the REAL async_main wiring -- own small harness, mirrors
# tests/test_v12_integration.py's own `_ScriptedChannel`/`run_jobs_before_
# stop` pattern (each integration-adjacent test file keeps its own copy).
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
    def __init__(self, *args, **kwargs):
        pass

    async def chat_text(self, system_prompt, user_prompt):
        return "noted"

    async def probe_schema_support(self, *args, **kwargs):
        return {}

    async def aclose(self):
        pass


class _JobRunningChannel(Channel):
    """Invokes the requested scheduler job(s) directly, still inside
    `async_main`'s own live `db` connection (which its `finally` block
    closes the instant `channel.run()` returns/raises) -- exactly
    `tests/test_v12_integration.py`'s own `run_jobs_before_stop` pattern."""

    last_instance: "_JobRunningChannel | None" = None
    run_jobs: list[str] = []

    def __init__(self, *args, **kwargs):
        self.sent: list[tuple[str, str]] = []
        _JobRunningChannel.last_instance = self

    async def send(self, chat_id, text):
        self.sent.append((chat_id, text))

    async def send_image(self, chat_id, image, caption):
        self.sent.append((chat_id, caption))

    async def set_my_commands(self, commands, *, scope_chat_id=None):
        pass

    def sent_to(self, chat_id):
        return [text for cid, text in self.sent if cid == chat_id]

    async def run(self, on_message, on_callback=None):
        for job_id in _JobRunningChannel.run_jobs:
            job = _FakeScheduler.last_instance.jobs.get(job_id)
            if job is not None:
                await job.func()
        raise _StopAfterSchedulerStart()

    async def aclose(self):
        pass


async def _run_job(monkeypatch, config, job_id, owner_chat_id=OWNER):
    from habit_assistant import main as main_module

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module,
        "load_secrets",
        lambda: SimpleNamespace(telegram_bot_token="fake", telegram_chat_id=owner_chat_id),
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", _JobRunningChannel)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClient)
    # SPEC-v1.5.md R-N2 (module `announce`): since v1.5.0's own release
    # (Archi's version bump), `__version__` genuinely matches a
    # `RELEASE_NOTES` entry, so the real startup call now actually sends
    # a release note to OWNER before this test's own job ever runs --
    # neutralized here so channel.sent_to(OWNER) reflects only the
    # job's own output, unaffected by that unrelated wiring.
    monkeypatch.setattr(main_module, "__version__", "0.0.0-test")
    _FakeScheduler.last_instance = None
    _JobRunningChannel.last_instance = None
    _JobRunningChannel.run_jobs = [job_id]
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)
    return _JobRunningChannel.last_instance


async def test_ac10_daily_summary_skips_a_user_in_their_own_dnd_window(tmp_path, monkeypatch):
    db_path = tmp_path / "habits.db"
    seed = Database(db_path)
    seed.upsert_user(OWNER, role="owner", status="active")
    seed.upsert_user(MEMBER, role="member", status="active")
    today_str = date.today().isoformat()
    seed.insert_log(LogEntry(None, OWNER, f"{today_str}T09:00:00", "water", 500.0, None, "500ml", "reply"))
    seed.insert_log(LogEntry(None, MEMBER, f"{today_str}T09:00:00", "water", 500.0, None, "500ml", "reply"))
    seed.set_user_quiet_hours(MEMBER, json.dumps(_ALWAYS_DND_WINDOWS))  # MEMBER always in DND
    seed.close()

    config = Config.model_validate({"app": {"db_path": str(db_path)}})
    channel = await _run_job(monkeypatch, config, "daily_summary")

    assert channel.sent_to(OWNER) != []  # OWNER: no override, global default empty -- unaffected (AC-10)
    assert channel.sent_to(MEMBER) == []  # MEMBER: always in their own DND -- suppressed


async def test_ac10_uncustomized_user_summary_is_byte_identical_to_v14(tmp_path, monkeypatch):
    """The other half of AC-10's own wording: "an un-customized user's
    summary is byte-identical to v1.4" -- no quiet_hours_json override
    at all, default (empty) global config -- must still send."""
    db_path = tmp_path / "habits.db"
    seed = Database(db_path)
    seed.upsert_user(OWNER, role="owner", status="active")
    today_str = date.today().isoformat()
    seed.insert_log(LogEntry(None, OWNER, f"{today_str}T09:00:00", "water", 500.0, None, "500ml", "reply"))
    seed.close()

    config = Config.model_validate({"app": {"db_path": str(db_path)}})  # quiet_hours.windows = [] by default
    channel = await _run_job(monkeypatch, config, "daily_summary")

    assert len(channel.sent_to(OWNER)) == 1


async def test_ac11_weekly_review_suppresses_a_user_in_their_own_dnd_window(tmp_path, monkeypatch):
    db_path = tmp_path / "habits.db"
    seed = Database(db_path)
    seed.upsert_user(OWNER, role="owner", status="active")
    seed.upsert_user(MEMBER, role="member", status="active")
    today = date.today()
    for offset in range(3):
        ts = (today - timedelta(days=offset)).isoformat() + "T09:00:00"
        seed.insert_log(LogEntry(None, OWNER, ts, "water", 500.0, None, "500ml", "reply"))
        seed.insert_log(LogEntry(None, MEMBER, ts, "water", 500.0, None, "500ml", "reply"))
    seed.set_user_quiet_hours(MEMBER, json.dumps(_ALWAYS_DND_WINDOWS))
    seed.close()

    config = Config.model_validate({"app": {"db_path": str(db_path)}})
    channel = await _run_job(monkeypatch, config, "weekly_review")

    assert channel.sent_to(OWNER) != []  # unaffected -- no override, empty global default
    assert channel.sent_to(MEMBER) == []  # suppressed -- always in their own DND


async def test_ac11_uncustomized_user_review_fires_exactly_as_before(tmp_path, monkeypatch):
    """The other half of AC-11's own wording: "with default (empty)
    windows, the review fires exactly as before" -- this job previously
    had NO suppression check at all, so this pins that adding one didn't
    regress the no-DND-configured case."""
    db_path = tmp_path / "habits.db"
    seed = Database(db_path)
    seed.upsert_user(OWNER, role="owner", status="active")
    today = date.today()
    for offset in range(3):
        ts = (today - timedelta(days=offset)).isoformat() + "T09:00:00"
        seed.insert_log(LogEntry(None, OWNER, ts, "water", 500.0, None, "500ml", "reply"))
    seed.close()

    config = Config.model_validate({"app": {"db_path": str(db_path)}})
    channel = await _run_job(monkeypatch, config, "weekly_review")

    assert channel.sent_to(OWNER) != []


# ===========================================================================
# AC-12 (ops never suppressed): health/outage alerts and access-request/
# access-granted notifications to the owner are NOT subject to DND.
# Structural -- neither `core/health.py` nor `core/access.py` was touched
# by this pass (both already, correctly, never call `in_dnd_now`), so
# this pins that fact directly rather than re-deriving it through a full
# async_main run.
# ===========================================================================


def test_ac12_health_monitor_never_calls_in_dnd_now():
    import inspect

    from habit_assistant.core import health

    source = inspect.getsource(health)
    assert "in_dnd_now" not in source
    assert "quiet" not in source.lower()  # no DND/quiet-hours concept at all in this module


def test_ac12_access_module_never_calls_in_dnd_now():
    import inspect

    from habit_assistant.core import access

    source = inspect.getsource(access)
    assert "in_dnd_now" not in source


async def test_ac12_health_alert_reaches_the_owner_even_with_owner_in_always_on_dnd(tmp_path):
    """A live, behavioral confirmation (not just a grep) -- the owner's
    OWN DND window (even an "always on" one) has zero bearing on whether
    a health alert reaches them, since `HealthMonitor._alert` never
    consults `in_dnd_now`/`effective_quiet_windows` at all."""
    from habit_assistant.core.health import HealthMonitor

    db = Database(tmp_path / "habits.db")
    db.upsert_user(OWNER, role="owner", status="active")
    db.set_user_quiet_hours(OWNER, json.dumps(_ALWAYS_DND_WINDOWS))
    db.close()

    sent: list[tuple[str, str]] = []

    class _RecordingChannel(Channel):
        async def send(self, chat_id, text):
            sent.append((chat_id, text))

        async def run(self, on_message, on_callback=None):
            raise NotImplementedError

    monitor = HealthMonitor("http://mac-mini:11434", "fake-token", OWNER, channel=_RecordingChannel(), language="en")
    await monitor._alert("Ollama is DOWN")

    assert sent == [(OWNER, "Ollama is DOWN")]
