"""SPEC-REFACTOR.md Stage 1, "tick" track (S1-B: `core/reminders.py`,
`core/checkins.py`, `core/nudge.py`, `main.py` job wiring). Owned ACs:
AC1 (idle reminder tick <= 3 queries, baseline 13), AC4 (the three
consolidated minutely ticks call `active_user_ids()` once, not 3x), AC5
(`build_checkin_message` reads `active_pauses` once per build, not once
per habit). AC2/AC3 (LIKE->range aggregations, `synchronous=NORMAL`) are
the parallel S1-A/db track's own ACs -- not covered here.

Query-counting harness: `_CountingConnProxy` wraps a real sqlite3
connection, counting `execute`/`executemany` calls, and is swapped onto a
scratch `Database`'s `_conn` AFTER construction (so schema-migration
queries at `Database.__init__` are never counted -- only queries the code
under test issues during the call being measured).

AC1 dependency note (see IMPL-refactor-s1-tick.md "Known limitations" for
the full writeup): rule 1(a)'s bulk `user_reminder_times` read needs a new
`storage/db.py` accessor (`Database.all_reminder_times()`) that
SPEC-REFACTOR.md SS11's shared-surface note assigns to the PARALLEL S1-A/
db track, not this one -- this track only *consumes* it (`core/reminders.
py:_bulk_reminder_time_overrides`, feature-detected via `getattr`, never
edits `storage/db.py`). `test_idle_reminder_tick_query_count_ac1` below
checks `hasattr(db, "all_reminder_times")` and asserts the STRICT AC1
floor (<=3) once that accessor exists, else the honest intermediate count
this track's OWN landed change (rule 1(b), lazy language resolution)
achieves on its own (13 -> 10) -- so this test tightens itself
automatically the moment the dependency lands, with no manual edit."""

from __future__ import annotations

from datetime import date, datetime
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import checkins, nudge, reminders
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner"
MEMBER_A = "member-a"
MEMBER_B = "member-b"


class _CountingConnProxy:
    """Wraps a real sqlite3 connection, counting every `execute`/
    `executemany` call while delegating everything (including the actual
    query execution) to the real connection -- swapped onto `db._conn`
    AFTER `Database.__init__` has already run its own migration queries,
    so only the call under test is measured."""

    def __init__(self, real) -> None:
        self._real = real
        self.count = 0

    def execute(self, *args, **kwargs):
        self.count += 1
        return self._real.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        self.count += 1
        return self._real.executemany(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append((chat_id, text))

    async def run(self, on_message: Callable[[str, str], Awaitable[None]], on_callback=None) -> None:
        raise NotImplementedError


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(MEMBER_A, role="member", status="active")
    database.upsert_user(MEMBER_B, role="member", status="active")
    yield database
    database.close()


def _fixed_clock(hhmm: str):
    hour, minute = (int(x) for x in hhmm.split(":"))
    return lambda: datetime(2026, 8, 24, hour, minute, 0)


def _default_registry() -> HabitRegistry:
    return HabitRegistry.from_config(Config())


def _count_queries(db: Database) -> _CountingConnProxy:
    proxy = _CountingConnProxy(db._conn)
    db._conn = proxy
    return proxy


# ===========================================================================
# AC1 -- idle reminder tick query count (baseline 13 = 1 + U*(1+H), U=3/H=3).
# ===========================================================================


async def test_idle_reminder_tick_query_count_ac1(db):
    """The seeded scenario mirrors SPEC-REFACTOR.md's measured baseline
    exactly: 3 active users (the `db` fixture), the default H=3 registry
    (water/stretch/diary), a clock minute that matches none of the
    default `reminder_times` for any habit -- so nothing is due and every
    query issued this call is pure idle-tick overhead."""
    channel = FakeChannel()
    registry = _default_registry()
    assert not any("03:33" in habit.reminder_times for habit in registry)

    proxy = _count_queries(db)
    await reminders.run_due_reminders(channel, Config(), registry, db, clock=_fixed_clock("03:33"))

    assert channel.sent == []
    if hasattr(db, "all_reminder_times"):
        # SPEC-REFACTOR.md Stage 1 rule 1(a)'s bulk accessor has landed
        # (parallel S1-A/db track) -- the full AC1 floor now applies.
        assert proxy.count <= 3, (
            f"AC1: idle reminder tick should issue <=3 queries once "
            f"Database.all_reminder_times() exists, got {proxy.count}"
        )
    else:
        # Not landed yet: this track's OWN change (rule 1(b), lazy
        # language resolution) already removed the 3 `get_user` reads an
        # idle tick used to make (one per active user) -- 13 -> 10. The
        # remaining 9 (`get_reminder_times`, one per user*habit) collapse
        # to the single bulk read the moment the accessor lands, and the
        # branch above takes over automatically (no edit needed here).
        assert proxy.count == 10, (
            f"expected the rule-1(b)-only intermediate count of 10 "
            f"(13 baseline - 3 deferred get_user reads), got {proxy.count}"
        )


async def test_idle_reminder_tick_sends_nothing_and_is_repeatable(db):
    """Sanity companion to the count assertion above: an idle tick truly
    sends nothing (not just "fewer queries but still fires"), and running
    it twice in a row is stable (no accumulating state)."""
    channel = FakeChannel()
    registry = _default_registry()
    config = Config()

    await reminders.run_due_reminders(channel, config, registry, db, clock=_fixed_clock("03:33"))
    await reminders.run_due_reminders(channel, config, registry, db, clock=_fixed_clock("03:34"))

    assert channel.sent == []


# ===========================================================================
# Rule 1(a) correctness -- `_reminder_times_from_overrides` (the in-memory
# resolver the bulk-read path will feed) is byte-identical to
# `effective_reminder_times` (the per-query resolver), independent of
# whether `Database.all_reminder_times()` exists yet. `overrides` is built
# here the same way `_bulk_reminder_time_overrides` builds it from real
# rows -- just via N `get_reminder_times` calls instead of 1 bulk query,
# which is exactly the "same data, different query shape" the two paths
# are supposed to agree on.
# ===========================================================================


def _overrides_from_get_reminder_times(db: Database, pairs) -> dict:
    overrides: dict = {}
    for user_id, habit_id in pairs:
        rows = db.get_reminder_times(user_id, habit_id)
        if rows:
            overrides[(user_id, habit_id)] = rows
    return overrides


def test_reminder_times_from_overrides_no_override_falls_back_to_config(db):
    config = Config()
    water = _default_registry().get("water")
    overrides = _overrides_from_get_reminder_times(db, [(OWNER, "water")])

    assert reminders._reminder_times_from_overrides(overrides, water, OWNER) == list(water.reminder_times)
    assert reminders._reminder_times_from_overrides(overrides, water, OWNER) == reminders.effective_reminder_times(
        db, config, water, OWNER
    )


def test_reminder_times_from_overrides_off_sentinel_means_empty(db):
    config = Config()
    water = _default_registry().get("water")
    db.set_reminder_times(OWNER, "water", ["off"])
    overrides = _overrides_from_get_reminder_times(db, [(OWNER, "water")])

    assert reminders._reminder_times_from_overrides(overrides, water, OWNER) == []
    assert reminders._reminder_times_from_overrides(overrides, water, OWNER) == reminders.effective_reminder_times(
        db, config, water, OWNER
    )


def test_reminder_times_from_overrides_custom_list_is_sorted(db):
    """`user_reminder_times` carries a `UNIQUE(user_id, habit_id, time)`
    constraint (no legitimately-duplicated row can ever be stored) --
    `sorted(set(rows))`'s `set()` half is therefore a defensive no-op in
    practice, and what actually matters is that an out-of-insertion-order
    write still reads back sorted, identically on both paths."""
    config = Config()
    water = _default_registry().get("water")
    db.set_reminder_times(OWNER, "water", ["12:00", "08:00"])
    overrides = _overrides_from_get_reminder_times(db, [(OWNER, "water")])

    assert reminders._reminder_times_from_overrides(overrides, water, OWNER) == ["08:00", "12:00"]
    assert reminders._reminder_times_from_overrides(overrides, water, OWNER) == reminders.effective_reminder_times(
        db, config, water, OWNER
    )


def test_reminder_times_from_overrides_is_per_user(db):
    config = Config()
    water = _default_registry().get("water")
    db.set_reminder_times(MEMBER_A, "water", ["12:00"])
    overrides = _overrides_from_get_reminder_times(db, [(OWNER, "water"), (MEMBER_A, "water")])

    assert reminders._reminder_times_from_overrides(overrides, water, MEMBER_A) == ["12:00"]
    assert reminders._reminder_times_from_overrides(overrides, water, OWNER) == list(water.reminder_times)
    for user_id in (OWNER, MEMBER_A):
        assert reminders._reminder_times_from_overrides(overrides, water, user_id) == reminders.effective_reminder_times(
            db, config, water, user_id
        )


async def test_run_due_reminders_bulk_path_byte_identical_to_fallback(db):
    """`Database.all_reminder_times()` has now landed for real (the
    parallel S1-A/db track) -- this proves `run_due_reminders`' bulk-read
    consumption path sends the EXACT same (chat_id, text) pairs, in the
    exact same order, as the pre-Stage-1 per-(user, habit) fallback path.
    The fallback path is forced by temporarily shadowing the accessor
    with an instance attribute set to `None`: `_bulk_reminder_time_
    overrides` feature-detects via `getattr(db, "all_reminder_times",
    None)`, and a plain function is a non-data descriptor, so an
    instance-`__dict__` entry (even `None`) takes precedence over the
    class method for attribute lookup -- exactly what "accessor not
    available" looked like before it landed. `del` afterward removes the
    instance override, reverting attribute lookup to the real class
    method for the bulk-path call below."""
    config = Config()
    registry = _default_registry()
    db.set_reminder_times(OWNER, "water", ["12:00"])
    db.set_reminder_times(MEMBER_A, "stretch", ["off"])
    # MEMBER_B: no override at all -- falls back to config defaults.

    assert hasattr(db, "all_reminder_times"), "Database.all_reminder_times() should exist now (S1-A landed)"

    db.all_reminder_times = None  # shadow the class method -> getattr(...) sees "not available"
    try:
        fallback_channel = FakeChannel()
        await reminders.run_due_reminders(fallback_channel, config, registry, db, clock=_fixed_clock("12:00"))
    finally:
        del db.all_reminder_times  # restore the real accessor

    bulk_channel = FakeChannel()
    await reminders.run_due_reminders(bulk_channel, config, registry, db, clock=_fixed_clock("12:00"))

    assert bulk_channel.sent == fallback_channel.sent
    assert any(chat_id == OWNER for chat_id, _ in bulk_channel.sent), "owner's custom 12:00 water reminder should fire"


# ===========================================================================
# AC4 -- the consolidated minutely tick fetches `active_user_ids()` once
# and threads it into all three tick functions; each function's own
# internal `db.active_user_ids()` call is skipped whenever the caller
# already supplies the list.
# ===========================================================================


class _CountingActiveUserIdsDB:
    """Wraps a real `Database`, counting calls to `active_user_ids()`
    specifically while delegating every other method through -- mirrors
    `tests/test_v19_release_gate.py:_ActivePausesRaisingDB`'s identical
    narrow-wrap-one-accessor convention."""

    def __init__(self, real: Database) -> None:
        self._real = real
        self.calls = 0

    def active_user_ids(self):
        self.calls += 1
        return self._real.active_user_ids()

    def __getattr__(self, name):
        return getattr(self._real, name)


async def test_active_user_ids_param_short_circuits_internal_call_ac4(db):
    """Unit-level proof that each of the three tick functions skips its
    own `db.active_user_ids()` call entirely whenever the caller already
    supplies `active_user_ids` -- the mechanism `main.py`'s consolidated
    job (below) relies on to make exactly one call per minute instead of
    up to three."""
    counting_db = _CountingActiveUserIdsDB(db)
    config = Config()
    registry = _default_registry()
    active_ids = [OWNER, MEMBER_A, MEMBER_B]

    await reminders.run_due_reminders(FakeChannel(), config, registry, counting_db, clock=_fixed_clock("03:33"), active_user_ids=active_ids)
    await checkins.run_due_checkins(FakeChannel(), config, registry, counting_db, clock=_fixed_clock("09:00"), active_user_ids=active_ids)
    await nudge.run_due_nudges(FakeChannel(), config, registry, counting_db, clock=_fixed_clock("20:00"), active_user_ids=active_ids)

    assert counting_db.calls == 0, "active_user_ids() must not be re-read when the caller already supplied the list"


async def test_consolidated_minutely_tick_calls_active_user_ids_once_ac4(tmp_path, monkeypatch):
    """End-to-end proof through the REAL `main.py` wiring: `async_main`
    now registers exactly one `id="minutely_tick"` job (SPEC-REFACTOR.md
    Stage 1 rule 2), which, when fired, calls `Database.active_user_ids()`
    exactly once total -- not once per the (now-merged) reminder/checkin/
    nudge tick, matching the pre-Stage-1 baseline `checkin_tick`/
    `nudge_tick` firing on the very same `:00`/`config.nudge.time` minute
    reminder_tick also runs on (worst case, all three guards pass)."""
    from types import SimpleNamespace

    from habit_assistant import main as main_module

    calls = []
    original = Database.active_user_ids

    def counting(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(Database, "active_user_ids", counting)

    class _StopAfterSchedulerStart(Exception):
        pass

    class _FakeScheduler:
        last_instance = None

        def __init__(self, *a, **kw):
            self.jobs: dict[str, object] = {}
            _FakeScheduler.last_instance = self

        def add_job(self, func, trigger=None, args=None, kwargs=None, id=None, replace_existing=True, **extra):
            self.jobs[id] = SimpleNamespace(func=func, trigger=trigger, args=list(args or []), kwargs=dict(kwargs or {}))

        def start(self):
            pass

        def shutdown(self, wait=False):
            pass

        def get_job(self, job_id):
            return self.jobs.get(job_id)

    class _FakeTelegramChannel:
        def __init__(self, *a, **kw):
            pass

        async def send(self, chat_id, text, *, disable_notification=False):
            pass

        async def send_actionable(self, chat_id, text, buttons):
            pass

        async def set_my_commands(self, commands, *, scope_chat_id=None):
            pass

        async def run(self, on_message, on_callback=None):
            # Mirrors tests/test_v19_integration.py:_V19Channel.run's own
            # pattern: the job must be invoked WHILE async_main is still
            # running (`db` is closed in async_main's own `finally` right
            # after `channel.run` raises) -- not after `async_main`
            # returns/raises out to the caller.
            tick_job = _FakeScheduler.last_instance.jobs.get("minutely_tick")
            calls.clear()
            await tick_job.func(*tick_job.args, **tick_job.kwargs)
            raise _StopAfterSchedulerStart()

        async def aclose(self):
            pass

    config = main_module.Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module, "load_secrets", lambda: SimpleNamespace(telegram_bot_token="fake", telegram_chat_id=OWNER)
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", _FakeTelegramChannel)

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    scheduler = _FakeScheduler.last_instance
    assert scheduler is not None
    assert scheduler.get_job("reminder_tick") is None, "the 3 separate tick jobs must be gone (AC4)"
    assert scheduler.get_job("checkin_tick") is None
    assert scheduler.get_job("nudge_tick") is None
    assert scheduler.get_job("minutely_tick") is not None, "a single consolidated minutely_tick job must be registered (AC4)"

    assert calls == [1], f"active_user_ids() must be called exactly once per consolidated tick, got {len(calls)}"


# ===========================================================================
# AC5 -- build_checkin_message reads active_pauses once per build, not
# once per habit (H). Extended (rule 7's own stated scope) to build_nudge_
# message's identical remedy.
# ===========================================================================


class _CountingActivePausesDB:
    """Mirrors `_CountingActiveUserIdsDB` above, narrowed to `active_
    pauses` -- the accessor `core/pause.py:is_paused` (and this track's
    own `_is_paused_in` mirrors, in `checkins.py`/`nudge.py`) reads."""

    def __init__(self, real: Database) -> None:
        self._real = real
        self.calls = 0

    def active_pauses(self, user_id: str):
        self.calls += 1
        return self._real.active_pauses(user_id)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _registry_with_n_goal_bearing_habits(n: int) -> HabitRegistry:
    from habit_assistant.core.habits import Habit

    habits = [
        Habit(
            id=f"habit{i}",
            type="numeric",
            label_en=f"habit{i}",
            label_th=f"habit{i}",
            unit_en="u",
            unit_th="u",
            goal=10.0,
            reminder_times=(),
            reminder_text_en=None,
            reminder_text_th=None,
            unit_aliases={},
        )
        for i in range(n)
    ]
    return HabitRegistry(habits)


async def test_build_checkin_message_reads_active_pauses_once_ac5(db):
    config = Config()
    registry = _registry_with_n_goal_bearing_habits(4)  # H=4, mirrors AC5's "H habits" wording
    counting_db = _CountingActivePausesDB(db)

    checkins.build_checkin_message(counting_db, config, registry, "en", OWNER, clock=_fixed_clock("09:00"))

    assert counting_db.calls == 1, f"AC5: expected exactly 1 active_pauses() read (baseline H=4), got {counting_db.calls}"


async def test_build_nudge_message_reads_active_pauses_once(db):
    """Same remedy (rule 7), same file-ownership rationale, applied to
    `nudge.build_nudge_message` -- not itself the literal AC5 wording, but
    explicitly named as rule 7's own second call site."""
    config = Config()
    registry = _registry_with_n_goal_bearing_habits(4)
    counting_db = _CountingActivePausesDB(db)
    # Seed all 4 habits close to (but under) goal so build_nudge_message
    # actually reaches the pause check for every one of them, not just
    # short-circuiting on an early "not close" skip.
    for habit in registry:
        counting_db._real.insert_log(
            LogEntry(None, OWNER, "2026-08-24T08:00:00", habit.id, 9.0, None, "9", "reply")
        )

    nudge.build_nudge_message(counting_db, config, registry, "en", OWNER, clock=_fixed_clock("20:00"))

    assert counting_db.calls == 1, f"expected exactly 1 active_pauses() read (baseline H=4), got {counting_db.calls}"


async def test_pause_reuse_still_excludes_paused_habit_byte_identical(db):
    """Behavior-preservation companion to the two counting tests above:
    fetching `active_pauses` once and reusing it (`_is_paused_in`) must
    exclude a paused habit from the message exactly like the old
    per-habit `pause.is_paused` calls did."""
    config = Config()
    registry = _registry_with_n_goal_bearing_habits(2)
    today = date(2026, 8, 24)
    db.insert_pause(OWNER, "habit0", today.isoformat(), today.isoformat())
    for habit in registry:
        db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", habit.id, 9.0, None, "9", "reply"))

    message = checkins.build_checkin_message(db, config, registry, "en", OWNER, clock=_fixed_clock("09:00"))

    assert message is not None
    assert "habit0" not in message
    assert "habit1" in message
