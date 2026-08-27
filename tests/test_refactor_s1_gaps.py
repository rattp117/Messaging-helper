"""SPEC-REFACTOR.md Stage 1 -- Vera's own ADVERSARIAL round, additional to
(not a replacement for) `tests/test_refactor_s1_db.py` (S1-A/db track,
Luna's own 22 tests) and `tests/test_refactor_stage1_tick.py` (S1-B/tick
track, Luna's own 12 tests). Both of those files already prove their own
authors' claims; this file goes looking for what they did NOT prove --
hostile shapes beyond the db track's own boundary corpus, suppression-layer
interplay beyond the tick track's own single-scenario bulk/fallback parity
test, and the one dimension neither IMPL doc addresses at all: whether the
three-jobs-into-one consolidation (rule 2/AC4) preserved the OLD per-job
failure isolation, or silently changed it.

Four sections, each mapped to Archi's own dispatch brief:

A. LIKE -> range rewrite (AC2) -- hostile shapes: cross-user/cross-category
   contamination at the exact boundary instant, a day with ZERO of its own
   rows sandwiched between two boundary-adjacent days, timestamps written
   through the REAL production write path (not hand-typed strings), the
   tightest possible soft-delete boundary, and a larger multi-month fuzz
   sweep with a different (seeded-random) shape than the db track's own
   fixed-corner-time fuzz.
B. Reminder parity (AC1/AC4) -- bulk-vs-fallback byte-identity under
   suppression-layer interplay (pause/quiet-hours/goal-met/stored /lang
   pref) the tick track's own parity test never combined with override
   resolution; the strict <=3 query floor re-proven at a LARGER U*H shape
   (proving the bound is O(1), not just correct for the specific 3x3
   baseline); the fallback path's own correctness re-confirmed with the
   real accessor hidden.
C. Single minutely_tick (AC4) -- explicit reminders->checkins->nudge
   ordering proof, scheduler job-count/trigger coherence, and the
   error-isolation question Archi flagged: does one tick function raising
   still let the other two run, matching the pre-Stage-1 three-independent-
   scheduler-jobs behavior? (Spoiler, proven below: NO -- see that test's
   own docstring for the finding and the pointer to main.py's exact lines.)
D. Byte-identity user-level spot-checks -- a typed log confirmation, a
   /history render, and a check-in message, each asserted against a
   HAND-PINNED literal (copied from a live run of the current code, not
   re-derived from `i18n.t(...)` at test time -- mirrors
   `test_ac17_v060_byte_identical_composite.py`'s own "hand-pin, don't
   re-derive" convention, so a catalog+test drift together can't silently
   agree with itself).
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import checkins, history_view, nudge, reminders
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner"


# ===========================================================================
# Section A -- LIKE -> range rewrite, hostile shapes beyond the db track's
# own boundary corpus (tests/test_refactor_s1_db.py).
# ===========================================================================


def make_db(tmp_path) -> Database:
    return Database(tmp_path / "gaps.db")


def _like_sum(db: Database, user_id: str, habit_id: str, day: str) -> float:
    row = db._conn.execute(
        "SELECT COALESCE(SUM(value_num), 0) AS total FROM logs "
        "WHERE user_id = ? AND category = ? AND deleted_at IS NULL AND ts LIKE ?",
        (user_id, habit_id, f"{day}%"),
    ).fetchone()
    return float(row["total"])


def _like_count(db: Database, user_id: str, habit_id: str, day: str) -> int:
    row = db._conn.execute(
        "SELECT COUNT(*) AS n FROM logs WHERE user_id = ? AND category = ? AND deleted_at IS NULL AND ts LIKE ?",
        (user_id, habit_id, f"{day}%"),
    ).fetchone()
    return int(row["n"])


def _like_count_true(db: Database, user_id: str, habit_id: str, day: str) -> int:
    row = db._conn.execute(
        "SELECT COUNT(*) AS n FROM logs "
        "WHERE user_id = ? AND category = ? AND deleted_at IS NULL AND ts LIKE ? AND value_num != 0",
        (user_id, habit_id, f"{day}%"),
    ).fetchone()
    return int(row["n"])


def test_cross_user_cross_category_contamination_at_exact_midnight_boundary(tmp_path):
    """Two users, two categories, ALL four rows written at the exact same
    two boundary instants (23:59:59 / 00:00:00) -- proves the new range
    filter's `user_id = ? AND category = ?` scoping isn't accidentally
    widened or narrowed by the ts predicate rewrite, for every one of the
    2x2 combinations, not just a single user/category the db track's own
    tests happened to use throughout."""
    db = make_db(tmp_path)
    for user in ("owner", "member-a"):
        for category in ("water", "stretch"):
            db.insert_log(LogEntry(None, user, "2026-08-19T23:59:59", category, 111.0, None, "late", "reply"))
            db.insert_log(LogEntry(None, user, "2026-08-20T00:00:00", category, 222.0, None, "early", "reply"))

    for user in ("owner", "member-a"):
        for category in ("water", "stretch"):
            for day, expected in (("2026-08-19", 111.0), ("2026-08-20", 222.0)):
                assert db.sum_value(user, category, day) == _like_sum(db, user, category, day) == expected
                assert db.count(user, category, day) == _like_count(db, user, category, day) == 1
    db.close()


def test_day_with_zero_rows_sandwiched_between_boundary_adjacent_days(tmp_path):
    """The middle day itself has NO rows at all -- only its neighbors do,
    right at the boundary -- so a range filter with an off-by-one error in
    either direction would leak a neighbor's row into this day's total
    instead of correctly returning zero."""
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, OWNER, "2026-08-18T23:59:59", "water", 500.0, None, "day before, last second", "reply"))
    db.insert_log(LogEntry(None, OWNER, "2026-08-20T00:00:00", "water", 500.0, None, "day after, first second", "reply"))

    assert db.sum_value(OWNER, "water", "2026-08-19") == _like_sum(db, OWNER, "water", "2026-08-19") == 0.0
    assert db.count(OWNER, "water", "2026-08-19") == _like_count(db, OWNER, "water", "2026-08-19") == 0
    db.close()


async def test_real_write_site_timestamps_respect_day_boundaries(tmp_path):
    """Every ts the db track's own tests compare against was hand-typed as
    a Python string literal. This drives the REAL production write path
    (`main.handle_inbound_message`'s zero-LLM '500ml' preparse fast path,
    the same call bench_baseline.py section F uses) with an injected clock
    at the exact boundary instants, so the `ts` string under test is
    whatever `now.isoformat(timespec='seconds')` (main.py:1319) actually
    produces -- not a hand-typed approximation of it."""
    from habit_assistant import main as main_module

    class _FakeChannel(Channel):
        def __init__(self):
            self.sent = []

        async def send(self, chat_id, text, *, disable_notification=False):
            self.sent.append(text)

        async def run(self, on_message, on_callback=None):
            raise NotImplementedError

    class _StubLLM:
        async def chat_text(self, *a, **k):
            return ""

    db = make_db(tmp_path)
    db.upsert_user(OWNER, role="owner", status="active")
    config = Config()
    registry = HabitRegistry.from_config(config)

    def clock_at(dt):
        return lambda: dt

    await main_module.handle_inbound_message(
        "500ml", db=db, llm=_StubLLM(), channel=_FakeChannel(), config=config, user_id=OWNER,
        registry=registry, provider=None, health_monitor=None, clock=clock_at(datetime(2026, 8, 19, 23, 59, 59)),
    )
    await main_module.handle_inbound_message(
        "300ml", db=db, llm=_StubLLM(), channel=_FakeChannel(), config=config, user_id=OWNER,
        registry=registry, provider=None, health_monitor=None, clock=clock_at(datetime(2026, 8, 20, 0, 0, 1)),
    )

    # Confirm the rows really did land with the real write-site ts shape
    # (T-separator, second precision, no hand-typed artifact).
    rows = db._conn.execute("SELECT ts FROM logs WHERE user_id = ? ORDER BY ts", (OWNER,)).fetchall()
    assert [r["ts"] for r in rows] == ["2026-08-19T23:59:59", "2026-08-20T00:00:01"]

    assert db.sum_value(OWNER, "water", "2026-08-19") == _like_sum(db, OWNER, "water", "2026-08-19") == 500.0
    assert db.sum_value(OWNER, "water", "2026-08-20") == _like_sum(db, OWNER, "water", "2026-08-20") == 300.0
    db.close()


def test_soft_deleted_row_at_the_tightest_possible_last_second_boundary(tmp_path):
    """The soft-deleted row sits at the LAST possible second of the day
    (23:59:59) -- the single instant closest to the exclusive next-day
    bound -- rather than the db track's own mid-morning soft-delete test,
    so an off-by-one in the upper bound would be most likely to
    accidentally resurrect exactly this row into the wrong day."""
    db = make_db(tmp_path)
    kept_id = db.insert_log(LogEntry(None, OWNER, "2026-08-19T12:00:00", "water", 400.0, None, "kept", "reply"))
    deleted_id = db.insert_log(LogEntry(None, OWNER, "2026-08-19T23:59:59", "water", 999.0, None, "undone at the wire", "reply"))
    db.soft_delete(deleted_id)
    assert kept_id != deleted_id

    assert db.sum_value(OWNER, "water", "2026-08-19") == _like_sum(db, OWNER, "water", "2026-08-19") == 400.0
    assert db.count(OWNER, "water", "2026-08-19") == _like_count(db, OWNER, "water", "2026-08-19") == 1
    db.close()


def test_multi_month_seeded_random_fuzz_matches_like_reference(tmp_path):
    """A differently-shaped fuzz than the db track's own (which used 5
    FIXED days x 6 FIXED corner times): 90 consecutive days spanning two
    month rollovers and a leap-year February (2028), each day getting a
    seeded-random number of rows (0-4) at seeded-random second-precision
    times -- including days with ZERO rows, which the db track's own fuzz
    never exercised (every one of its 5 days had exactly 12 rows).
    Deterministic (fixed seed) so a failure is reproducible."""
    db = make_db(tmp_path)
    rng = random.Random(20260827)
    start = date(2028, 1, 15)
    days = [(start + timedelta(days=i)).isoformat() for i in range(90)]

    for day in days:
        n_rows = rng.randint(0, 4)
        for _ in range(n_rows):
            h, m, s = rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59)
            value = float(rng.randint(1, 999))
            is_true = rng.random() < 0.5
            db.insert_log(LogEntry(None, OWNER, f"{day}T{h:02d}:{m:02d}:{s:02d}", "water", value, None, "fuzz", "reply"))
            db.insert_log(
                LogEntry(None, OWNER, f"{day}T{h:02d}:{m:02d}:{s:02d}", "meds", 1.0 if is_true else 0.0, None, "fuzz", "reply", habit_type="boolean")
            )

    for day in days:
        assert db.sum_value(OWNER, "water", day) == _like_sum(db, OWNER, "water", day)
        assert db.count(OWNER, "water", day) == _like_count(db, OWNER, "water", day)
        assert db.count_true(OWNER, "meds", day) == _like_count_true(db, OWNER, "meds", day)
    db.close()


# ===========================================================================
# Section B -- Reminder parity (AC1/AC4): bulk-vs-fallback under
# suppression-layer interplay, the strict query floor at a larger shape,
# and fallback correctness with the accessor deliberately hidden.
# ===========================================================================


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append((chat_id, text))

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError


class _CountingConnProxy:
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


def _fixed_clock(hhmm: str, day=(2026, 8, 19)):
    hour, minute = (int(x) for x in hhmm.split(":"))
    return lambda: datetime(*day, hour, minute, 0)


def _hide_bulk_accessor(db):
    """Force `_bulk_reminder_time_overrides`'s `getattr(db, "all_reminder_
    times", None)` to see "not available", exactly like the pre-Stage-1
    (or a not-yet-upgraded) `Database` -- see
    `tests/test_refactor_stage1_tick.py`'s identical technique for why an
    instance-attribute override (even `None`) shadows the class method."""
    db.all_reminder_times = None


def _restore_bulk_accessor(db):
    del db.all_reminder_times


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user("member-a", role="member", status="active")
    database.upsert_user("member-b", role="member", status="active")
    database.upsert_user("member-c", role="member", status="active")
    yield database
    database.close()


def _default_registry() -> HabitRegistry:
    return HabitRegistry.from_config(Config())


async def test_bulk_and_fallback_agree_under_full_suppression_interplay(db):
    """Beyond the tick track's own single-scenario parity test (custom
    time / off-sentinel / no-override only): layers EVERY suppression
    mechanism `send_reminder` applies on top of override resolution, all
    in the SAME tick, and proves the bulk path and the forced-fallback
    path send the exact same (chat_id, text) list, in the exact same
    order --

    Both `pause.is_paused`'s date check AND `_goal_already_met`'s date
    check (`_today_str`) read the REAL current date (`datetime.now()`),
    NOT the injectable `clock` param `run_due_reminders` itself uses for
    "is this minute due" -- a pre-existing (pre-Stage-1) design quirk, not
    something this test is trying to prove; it just means the pause/log
    rows below are seeded against `date.today()` rather than a fixed
    historical date, so the suppression actually engages regardless of
    which real day this suite happens to run on. Likewise the quiet-hours
    window is built from `datetime.now()`'s own real wall-clock TIME, a
    few minutes wide around "right now" (handles a midnight-crossing
    "now" correctly too, via `_in_quiet_hours`'s own wraparound branch),
    rather than a fixed clock-independent tautology.

    - owner: custom override time, PAUSED for that habit (today) -> must
      not send.
    - member-a: custom override time, a quiet-hours window covering the
      actual current moment -> must not send.
    - member-b: custom override time, already met today's REAL-date water
      goal (`_goal_already_met` reads `_today_str`, which is NOT
      clock-injectable -- uses the real current date) -> must not send.
    - member-c: custom override time, stored `/lang th` preference, no
      suppression -> MUST send, in Thai (proves rule 1(b)'s lazy language
      resolution still honors a stored per-user pref on the bulk path,
      not just the config default)."""
    from zoneinfo import ZoneInfo

    config = Config()
    registry = _default_registry()
    real_today = date.today().isoformat()

    db.set_reminder_times(OWNER, "water", ["09:00"])
    db.insert_pause(OWNER, "water", real_today, real_today)

    db.set_reminder_times("member-a", "water", ["09:00"])
    now_local = datetime.now(ZoneInfo(config.app.timezone))
    window_start = (now_local - timedelta(minutes=2)).strftime("%H:%M")
    window_end = (now_local + timedelta(minutes=2)).strftime("%H:%M")
    db.set_user_quiet_hours("member-a", f'[["{window_start}","{window_end}"]]')

    db.set_reminder_times("member-b", "water", ["09:00"])
    db.insert_log(LogEntry(None, "member-b", f"{real_today}T07:00:00", "water", 3000.0, None, "already over goal", "reply"))

    db.set_reminder_times("member-c", "water", ["09:00"])
    db.set_user_language("member-c", "th")

    clock = _fixed_clock("09:00")

    _hide_bulk_accessor(db)
    try:
        fallback_channel = FakeChannel()
        await reminders.run_due_reminders(fallback_channel, config, registry, db, clock=clock)
    finally:
        _restore_bulk_accessor(db)

    bulk_channel = FakeChannel()
    await reminders.run_due_reminders(bulk_channel, config, registry, db, clock=clock)

    assert bulk_channel.sent == fallback_channel.sent
    senders = {chat_id for chat_id, _ in bulk_channel.sent}
    assert senders == {"member-c"}, (
        f"only member-c should receive a water reminder this tick (paused/quiet/goal-met suppress the other "
        f"three on BOTH paths identically), got senders={senders}"
    )
    _, text = bulk_channel.sent[0]
    assert text != reminders.REMINDER_TEXTS["water"], "member-c's stored /lang=th pref must resolve to Thai, not the English default, on the bulk path"


async def test_idle_tick_query_bound_holds_at_a_larger_user_habit_shape(db):
    """AC1's own floor (<=3) is proven at the spec's literal 3-user/3-habit
    baseline shape by `test_refactor_stage1_tick.py`. This proves the bound
    is actually O(1) -- independent of U*H -- by scaling to 4 users (the
    `db` fixture's own default) x a 6-habit custom registry, several with
    stored overrides, none of which should move the query count at all
    once the bulk accessor is in play."""
    from habit_assistant.core.habits import Habit

    habits = [
        Habit(
            id=f"habit{i}", type="numeric", label_en=f"habit{i}", label_th=f"habit{i}",
            unit_en="u", unit_th="u", goal=None, reminder_times=("07:00",),
            reminder_text_en=None, reminder_text_th=None, unit_aliases={},
        )
        for i in range(6)
    ]
    registry = HabitRegistry(habits)
    config = Config()

    for user in (OWNER, "member-a", "member-b", "member-c"):
        db.set_reminder_times(user, "habit0", ["12:00"])
    assert hasattr(db, "all_reminder_times")

    proxy = _CountingConnProxy(db._conn)
    db._conn = proxy
    channel = FakeChannel()
    await reminders.run_due_reminders(channel, config, registry, db, clock=_fixed_clock("03:33"))

    assert channel.sent == []
    assert proxy.count <= 3, f"AC1: query count must stay O(1) regardless of U*H shape, got {proxy.count} for 4 users x 6 habits"


async def test_fallback_path_is_still_functionally_correct_with_accessor_hidden(db):
    """Companion to the query-count tests: proves the FALLBACK code path
    (pre-Stage-1, unchanged per-(user,habit) `effective_reminder_times`
    reads) still produces the functionally correct due/not-due decision
    for every override shape, when the bulk accessor is unavailable --
    not just that it agrees with the bulk path in one already-covered
    scenario, but that it is independently correct against the config
    defaults and a hand-verified expected send set."""
    config = Config()
    registry = _default_registry()
    db.set_reminder_times(OWNER, "water", ["09:00"])
    db.set_reminder_times("member-a", "water", ["off"])
    # member-b/member-c: no override -> config default water times (which
    # do NOT include 09:00, so neither should fire this tick).

    _hide_bulk_accessor(db)
    try:
        channel = FakeChannel()
        await reminders.run_due_reminders(channel, config, registry, db, clock=_fixed_clock("09:00"))
    finally:
        _restore_bulk_accessor(db)

    senders = {chat_id for chat_id, _ in channel.sent}
    assert senders == {OWNER}, f"only owner's explicit 09:00 override should fire; off-sentinel and config-default users must not, got {senders}"


# ===========================================================================
# Section C -- Single minutely_tick (AC4): ordering, scheduler coherence,
# and the error-isolation question neither IMPL doc addresses.
# ===========================================================================


def _minutely_tick_harness(monkeypatch, tmp_path):
    """Shared scaffolding for driving the REAL `main.py:async_main` wiring
    end-to-end (mirrors `test_refactor_stage1_tick.py`'s own
    `test_consolidated_minutely_tick_calls_active_user_ids_once_ac4`
    pattern) -- returns `(main_module, run, get_job)` where `run(...)`
    executes `async_main` under a fake scheduler/channel and fires the
    captured `minutely_tick` job exactly once, then stops."""
    from types import SimpleNamespace

    from habit_assistant import main as main_module

    class _StopAfterSchedulerStart(Exception):
        pass

    class _FakeScheduler:
        last_instance = None

        def __init__(self, *a, **kw):
            self.jobs: dict[str, object] = {}
            _FakeScheduler.last_instance = self

        def add_job(self, func, trigger=None, args=None, kwargs=None, id=None, replace_existing=True, **extra):
            self.jobs[id] = SimpleNamespace(
                func=func, trigger=trigger, args=list(args or []), kwargs=dict(kwargs or {}), extra=extra
            )

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
            tick_job = _FakeScheduler.last_instance.jobs.get("minutely_tick")
            try:
                await tick_job.func(*tick_job.args, **tick_job.kwargs)
            except Exception:
                pass
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

    async def run():
        args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
        with pytest.raises(_StopAfterSchedulerStart):
            await main_module.async_main(args)
        return _FakeScheduler.last_instance

    return main_module, run


async def test_minutely_tick_runs_reminders_checkins_nudge_in_order(monkeypatch, tmp_path):
    """Explicit ordering proof (rule 2's own stated "same order the three
    jobs used to register in"): a shared call-order list, one spy per
    tick function, on a normal (non-raising) tick."""
    main_module, run = _minutely_tick_harness(monkeypatch, tmp_path)
    from habit_assistant.core import checkins, nudge

    order: list[str] = []

    async def spy_reminders(*a, **k):
        order.append("reminders")

    async def spy_checkins(*a, **k):
        order.append("checkins")

    async def spy_nudge(*a, **k):
        order.append("nudge")

    monkeypatch.setattr(main_module, "run_due_reminders", spy_reminders)
    monkeypatch.setattr(checkins, "run_due_checkins", spy_checkins)
    monkeypatch.setattr(nudge, "run_due_nudges", spy_nudge)

    await run()

    assert order == ["reminders", "checkins", "nudge"], f"AC4/rule 2: tick order must be reminders->checkins->nudge, got {order}"


async def test_scheduler_registers_exactly_one_coherent_minutely_job(monkeypatch, tmp_path):
    """AC4's scheduler-shape half: exactly one job (`minutely_tick`), the
    three old ids gone, and the trigger/coalesce/max_instances knobs
    unchanged from the pre-Stage-1 per-job settings (`CronTrigger(second=0,
    ...)`, `coalesce=True`, `max_instances=1`) -- a consolidation that
    silently dropped `coalesce`/`max_instances` would reintroduce the
    double-send-on-restart / overlapping-tick risk those params exist to
    prevent (main.py's own reminder_tick docstring, R-S1)."""
    main_module, run = _minutely_tick_harness(monkeypatch, tmp_path)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(main_module, "run_due_reminders", noop)
    from habit_assistant.core import checkins, nudge

    monkeypatch.setattr(checkins, "run_due_checkins", noop)
    monkeypatch.setattr(nudge, "run_due_nudges", noop)

    scheduler = await run()

    assert scheduler.get_job("reminder_tick") is None
    assert scheduler.get_job("checkin_tick") is None
    assert scheduler.get_job("nudge_tick") is None
    job = scheduler.get_job("minutely_tick")
    assert job is not None
    assert job.extra.get("coalesce") is True
    assert job.extra.get("max_instances") == 1


async def test_one_ticks_exception_does_not_suppress_the_other_two_ticks_same_minute(monkeypatch, tmp_path):
    """ADVERSARIAL FINDING (Archi's own dispatch brief: "if the merge
    changed error isolation, that IS a behavior change -- check what the
    spec requires and fail if unproven").

    Pre-Stage-1: `reminder_tick`/`checkin_tick`/`nudge_tick` were THREE
    INDEPENDENT APScheduler jobs on the same `CronTrigger(second=0)`.
    APScheduler's default executor catches an exception raised by one
    job's callable, logs an `EVENT_JOB_ERROR`, and does NOT propagate it
    to (or skip) any OTHER independently-scheduled job due to fire the
    same tick -- a bug that made `reminder_tick` raise would still leave
    `checkin_tick`/`nudge_tick` running normally that same minute.

    Post-Stage-1 (`main.py:1963-1985`, `_minutely_tick`): the three calls
    are sequential plain `await`s inside ONE job function, with NO
    try/except between them:

        await run_due_reminders(...)
        await checkins.run_due_checkins(...)
        await nudge.run_due_nudges(...)

    If `run_due_reminders` raises anything that escapes its own internal
    fail-open helpers (e.g. rule 1(a)'s new `_bulk_reminder_time_
    overrides` / `db.all_reminder_times()` is NOT wrapped in a try/except
    the way `_goal_already_met`/`effective_quiet_windows` are -- a DB
    read error there propagates straight out of `run_due_reminders`),
    `checkins.run_due_checkins`/`nudge.run_due_nudges` are NEVER CALLED
    for that tick -- proven directly below via spies. This asserts the
    OLD (pre-Stage-1) isolation semantics; it is expected to FAIL against
    the current implementation, which is the point: rule 2's own text
    ("Byte-identical: the three fan-outs are independent and order-free")
    is not actually true under a raising scenario, and this codebase's own
    pervasive fail-open philosophy (every other per-user/per-habit loop in
    reminders.py/checkins.py/nudge.py is individually try/except-wrapped)
    makes this an unintentional-looking regression, not a deliberate
    design choice. Minimal fix: wrap each of the three `await`s in
    `_minutely_tick` in its own try/except (log + continue), restoring the
    pre-Stage-1 per-tick isolation -- see TEST-refactor-s1.md for the full
    writeup and recommendation."""
    main_module, run = _minutely_tick_harness(monkeypatch, tmp_path)
    from habit_assistant.core import checkins, nudge

    calls: list[str] = []

    async def raising_reminders(*a, **k):
        calls.append("reminders")
        raise RuntimeError("simulated reminder-tick failure (e.g. a DB read error inside run_due_reminders)")

    async def spy_checkins(*a, **k):
        calls.append("checkins")

    async def spy_nudge(*a, **k):
        calls.append("nudge")

    monkeypatch.setattr(main_module, "run_due_reminders", raising_reminders)
    monkeypatch.setattr(checkins, "run_due_checkins", spy_checkins)
    monkeypatch.setattr(nudge, "run_due_nudges", spy_nudge)

    await run()

    assert "checkins" in calls and "nudge" in calls, (
        "REGRESSION vs pre-Stage-1 isolation: run_due_reminders raising suppressed checkins/nudge for this "
        f"whole tick (only {calls} ran) -- under the old 3-independent-scheduler-jobs design, checkin_tick/"
        "nudge_tick would still have fired this same minute regardless of reminder_tick's own failure. "
        "See main.py:1963-1985 (_minutely_tick) -- no try/except separates the three awaits."
    )


# ===========================================================================
# Section D -- Byte-identity user-level spot-checks. Every expected string
# below is a HAND-PINNED literal, copied verbatim from a live run of the
# current (post-Stage-1) code -- not re-derived by calling `i18n.t(...)` at
# test time -- mirroring `test_ac17_v060_byte_identical_composite.py`'s own
# "hand-pin, don't re-derive" convention (SPEC-REFACTOR.md's own invariant,
# section 3: "No change to any i18n catalog string emitted" -- Stage 1
# touched zero i18n.py lines, confirmed by grep, so "current" and "the
# correct pre-refactor value" are the same string by construction; this
# guards against a FUTURE drift silently changing both the catalog and any
# test that re-derives its own expectation from it)."""
# ===========================================================================


async def test_typed_log_confirmation_is_byte_identical(tmp_path):
    from habit_assistant import main as main_module

    class _FakeChannel(Channel):
        def __init__(self):
            self.sent = []

        async def send(self, chat_id, text, *, disable_notification=False):
            self.sent.append(text)

        async def run(self, on_message, on_callback=None):
            raise NotImplementedError

    class _StubLLM:
        async def chat_text(self, *a, **k):
            return ""

    db = Database(tmp_path / "spot1.db")
    db.upsert_user(OWNER, role="owner", status="active")
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = _FakeChannel()

    await main_module.handle_inbound_message(
        "500ml", db=db, llm=_StubLLM(), channel=channel, config=config, user_id=OWNER,
        registry=registry, provider=None, health_monitor=None, clock=lambda: datetime(2026, 8, 19, 14, 30, 0),
    )

    assert channel.sent == ["✅ 500 ml logged — today 500 / 2500 ml (20%)"]
    db.close()


def test_history_render_is_byte_identical(tmp_path):
    db = Database(tmp_path / "spot2.db")
    db.upsert_user(OWNER, role="owner", status="active")
    db.insert_log(LogEntry(None, OWNER, "2026-08-19T08:00:00", "water", 500.0, None, "500ml", "reply"))
    db.insert_log(LogEntry(None, OWNER, "2026-08-19T09:00:00", "stretch", 10.0, None, "10 min stretch", "reply"))
    config = Config()
    registry = HabitRegistry.from_config(config)

    rendered = history_view.render_history(db, config, registry, "en", user_id=OWNER, category=None, limit=5)

    expected = (
        "\U0001f9fe Your last 5 entries:\n"
        '• 08-19 09:00 · 10 min stretch · "10 min stretch"\n'
        '• 08-19 08:00 · 500 ml water · "500ml"'
    )
    assert rendered == expected
    db.close()


def test_checkin_message_is_byte_identical(tmp_path):
    db = Database(tmp_path / "spot3.db")
    db.upsert_user(OWNER, role="owner", status="active")
    db.insert_log(LogEntry(None, OWNER, "2026-08-19T08:00:00", "water", 500.0, None, "500ml", "reply"))
    config = Config()
    registry = HabitRegistry.from_config(config)

    message = checkins.build_checkin_message(
        db, config, registry, "en", OWNER, clock=lambda: datetime(2026, 8, 19, 14, 30, 0)
    )

    expected = "\U0001f324️ Quick check-in\n• water: 500 / 2500 ml\nLog anything you've done? \U0001f4ac"
    assert message == expected
    db.close()
