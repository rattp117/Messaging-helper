"""v1.9.0 FINAL RELEASE GATE (Vera, independent of every module/integration
Vera that came before). Archi's dispatch: probe beyond `tests/
test_v19_integration.py`'s 12 wired tests with fresh, independently-designed
tests that specifically target (1) whether the AC3 byte-identical gate holds
at the WIRED confirmation-text level once the one documented, default-on
v1.9 delta (the celebrate_burst append, AC29) is accounted for, (2)
cross-feature interactions no single module's own Vera could exercise
(cadence x pause, grace x cadence, pause x grace x wrapped, backfill x
pause/grace, undo x cadence), (3) the actual (not assumed) fail-open
behavior of all 5 pause-gating call sites under a genuine DB read error,
(4) the new "system" audit source's bilingual rendering alongside every
pre-existing action, (5) the exact public/owner menu counts + `/help`
content + render-budget headroom, (6) the grace message's unconditional
`disable_notification=True`, and (7) `RELEASE_NOTES["1.9.0"]` announce-
readiness -- all WITHOUT bumping the shipped version (that is Archi's own
Phase 6.5 step, not this file's).

Live-environment rule (same as every other v1.9 test file): every DB here
is a scratch `tmp_path` SQLite file. Nothing here ever opens
`data/habits.db`, and no real Telegram/Ollama call is made."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import (
    audit,
    audit_view,
    cadence,
    checkins,
    grace,
    i18n,
    nudge,
    pause,
    release_notes,
    reminders,
    review,
    streaks,
    targets,
)
from habit_assistant.core.discoverability import build_habits_overview, build_help_text
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.render_budget import TELEGRAM_MESSAGE_BUDGET
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "9001"
MEMBER = "9002"


def _seed(db: Database, ts: str, category: str, value_num: float | None, user_id: str = OWNER, raw: str = "x") -> int:
    return db.insert_log(LogEntry(None, user_id, ts, category, value_num, None, raw, "reply"))


def _db(tmp_path, *user_ids: str) -> Database:
    database = Database(tmp_path / "habits.db")
    for uid in user_ids:
        database.upsert_user(uid, role="owner" if uid == OWNER else "member", status="active")
    return database


def _find(texts: list[str], needle: str) -> str:
    for t in texts:
        if needle in t:
            return t
    raise AssertionError(f"no text containing {needle!r} found in {texts!r}")


def _iso_week_monday(day: date) -> date:
    """Local re-derivation of `core/streaks.py:_iso_week_bounds`'s own
    Monday math -- kept independent (not imported) so this gate file never
    depends on the production module's private helper, mirroring this
    codebase's own established "each test file re-derives trivial date
    math rather than importing a private name" convention (e.g. `core/
    grace.py:_iso_week_bounds` itself is a documented duplicate of the
    same streaks.py helper, for the identical reason)."""
    return day - timedelta(days=day.isoweekday() - 1)


class _FakeOllamaClient:
    responses: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def chat_text(self, system_prompt, user_prompt):
        return "noted"

    async def chat_json(self, system_prompt, user_prompt, json_schema, valid_categories):
        return '{"category": "unknown", "value": null, "confidence": 0.1}'

    async def probe_schema_support(self, *args, **kwargs) -> dict:
        return {}

    async def aclose(self) -> None:
        pass


# ===========================================================================
# Shared async_main harness -- an independent copy of tests/test_v19_
# integration.py's own _V19Channel/_FakeScheduler/_run pattern, per this
# codebase's established "each integration-adjacent file keeps its own
# copy" convention (see that file's own module docstring).
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


class _GateChannel(Channel):
    last_instance: "_GateChannel | None" = None
    script: list[tuple] = []
    run_jobs_before_stop: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        self.sent: list[tuple[str, str, bool]] = []
        self.images: list[tuple[str, bytes, str, bool]] = []
        self.actionable: list[tuple[str, str, list]] = []
        self.set_my_commands_calls: list[tuple[dict, str | None]] = []
        _GateChannel.last_instance = self

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
        for step in _GateChannel.script:
            _, chat_id, text, display_name = step
            await on_message(chat_id, text, display_name)
        for job_id in _GateChannel.run_jobs_before_stop:
            job = _FakeScheduler.last_instance.jobs.get(job_id)
            if job is not None:
                await job.func(*job.args, **job.kwargs)
        raise _StopAfterSchedulerStart()

    async def aclose(self) -> None:
        pass


async def _run(monkeypatch, config, script, owner_chat_id=OWNER, run_jobs=None):
    from habit_assistant import main as main_module

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module, "load_secrets", lambda: SimpleNamespace(telegram_bot_token="fake", telegram_chat_id=owner_chat_id)
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", _GateChannel)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClient)
    monkeypatch.setattr(main_module, "__version__", "0.0.0-test")
    _FakeScheduler.last_instance = None
    _GateChannel.last_instance = None
    _GateChannel.script = script
    _GateChannel.run_jobs_before_stop = list(run_jobs or [])
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)
    return _GateChannel.last_instance


class _ActivePausesRaisingDB:
    """Wraps a real `Database`, raising ONLY on `active_pauses()` -- the
    accessor `core/pause.py:is_paused` reads. Every other method delegates
    straight through, so a call site's behavior under a genuine pause-read
    failure can be observed in isolation, without corrupting a real SQLite
    file (mirrors the established `_RaisingDatabase` convention -- e.g.
    `tests/test_adaptive_reminders.py` -- narrowed to one accessor)."""

    def __init__(self, real: Database) -> None:
        self._real = real

    def active_pauses(self, user_id: str):
        raise sqlite3.OperationalError("database is locked")

    def __getattr__(self, name):
        return getattr(self._real, name)


# ===========================================================================
# SECTION 1 -- AC3 hard byte-identical gate at the WIRED confirmation-text
# level. A "v1.8.1-shaped" user is one for whom habit_cadence/grace_ledger/
# pauses are all still empty (no v1.9 feature has engaged yet) -- the exact
# precondition Rule 24/AC3 states. The ONLY permitted delta for such a user
# is the one AC29 explicitly documents (default-on celebrate_burst appended
# to an ALREADY-firing milestone/record celebration line) -- never a delta
# on an ordinary, non-celebratory log, and never any delta at all once
# celebrate_burst is turned off.
# ===========================================================================


async def test_ac3_ordinary_log_stays_byte_identical_even_with_celebrate_burst_enabled(tmp_path, monkeypatch):
    """celebrate_burst defaults to True, but it must never leak onto a log
    that crosses neither a milestone nor a record -- burst only appends
    when `confirmation_suffix` is ALREADY non-empty (main.py's own gate)."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    assert config.wrapped.celebrate_burst is True
    channel = await _run(monkeypatch, config, script=[("message", OWNER, "500ml", None)])
    expected = i18n.t("water_confirmation", "en", water_ml=500, total=500, goal=2500.0, pct=20)
    assert channel.sent_to(OWNER)[-1] == expected
    assert "🎉" not in channel.sent_to(OWNER)[-1]


async def test_ac3_milestone_confirmation_delta_is_exactly_the_documented_burst_append(tmp_path, monkeypatch):
    db = _db(tmp_path, OWNER)
    today = date.today()
    # Pre-seed every RECORD_TYPE sky-high so this 3rd stretch log breaks NO
    # record -- isolating the milestone line (and its burst) as the ONLY
    # possible delta source.
    for record_type in ("best_day", "best_week", "longest_streak"):
        db.upsert_record(OWNER, "stretch", record_type, 999999.0, "2000-01-01")
    _seed(db, f"{(today - timedelta(days=2)).isoformat()}T09:00:00", "stretch", 10.0)
    _seed(db, f"{(today - timedelta(days=1)).isoformat()}T09:00:00", "stretch", 10.0)
    db.close()

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    channel = await _run(monkeypatch, config, script=[("message", OWNER, "10 min", None)])
    actual = channel.sent_to(OWNER)[-1]

    base = i18n.t("stretch_confirmation", "en", stretch_min=10, ordinal="1st", count=1)
    milestone = i18n.t("milestone_reached", "en", streak=3, label="stretch")
    without_burst = base + "\n\n" + milestone
    with_burst = without_burst + "\n" + "🎉🎊🥳"
    assert actual == with_burst, actual
    # And the record lines genuinely never fired (pre-seeded sky-high) --
    # confirms the delta really is JUST the burst, not an unaccounted one.
    assert "record" not in actual.lower() or "🔥" in actual


async def test_ac3_milestone_confirmation_fully_byte_identical_when_celebrate_burst_disabled(tmp_path, monkeypatch):
    """The mirror-image control: with `[wrapped] celebrate_burst=false`,
    the SAME milestone-crossing scenario produces EXACTLY the pre-v1.9
    wording -- zero delta, not even a stray newline."""
    db = _db(tmp_path, OWNER)
    today = date.today()
    for record_type in ("best_day", "best_week", "longest_streak"):
        db.upsert_record(OWNER, "stretch", record_type, 999999.0, "2000-01-01")
    _seed(db, f"{(today - timedelta(days=2)).isoformat()}T09:00:00", "stretch", 10.0)
    _seed(db, f"{(today - timedelta(days=1)).isoformat()}T09:00:00", "stretch", 10.0)
    db.close()

    config = Config.model_validate(
        {
            "app": {"db_path": str(tmp_path / "habits.db")},
            "i18n": {"language": "en"},
            "wrapped": {"celebrate_burst": False},
        }
    )
    channel = await _run(monkeypatch, config, script=[("message", OWNER, "10 min", None)])
    actual = channel.sent_to(OWNER)[-1]

    base = i18n.t("stretch_confirmation", "en", stretch_min=10, ordinal="1st", count=1)
    milestone = i18n.t("milestone_reached", "en", streak=3, label="stretch")
    expected = base + "\n\n" + milestone
    assert actual == expected, actual
    assert "🎉" not in actual and "🎊" not in actual and "🥳" not in actual


def test_ac3_dashboard_review_summary_records_carry_no_v19_marker_for_an_unengaged_user(tmp_path):
    """No `⏸`, no "×/week", no `🛟` (grace balance), no week-wording escape
    into a user who has never triggered cadence/pause/grace -- the four
    renderer surfaces named in Rule 24 stay silent about v1.9 entirely."""
    db = _db(tmp_path, OWNER)
    today = date.today()
    _seed(db, f"{today.isoformat()}T08:00:00", "water", 1200.0)
    _seed(db, f"{(today - timedelta(days=1)).isoformat()}T21:00:00", "diary", None, raw="fine day")
    config = Config.model_validate({"i18n": {"language": "en"}})
    registry = HabitRegistry.from_config(config)

    from habit_assistant.core.dashboard import render as dashboard_render

    dash = dashboard_render(db, config, registry, "en", OWNER, clock=lambda: datetime(today.year, today.month, today.day))
    habits = build_habits_overview(db, config, registry, lambda: datetime(today.year, today.month, today.day), "en", OWNER)
    summary = streaks.run_daily_summary(db, config, registry, "en", OWNER, today=today)
    from habit_assistant.core.records import render as records_render

    records_text = records_render(db, config, registry, "en", OWNER, habit_id=None)

    for surface_name, text in (("dashboard", dash), ("habits", habits), ("daily_summary", summary), ("records", records_text)):
        assert "⏸" not in text, f"{surface_name} leaked a pause marker: {text!r}"
        assert "×/week" not in text, f"{surface_name} leaked cadence wording: {text!r}"
    # AC17 makes `/habits`' grace balance line an INTENTIONAL, always-on
    # delta ("each daily habit shows its grace balance") -- unlike cadence/
    # pause, it is not gated on the feature ever having been "invoked", and
    # Rule 24's own named-function list (compute_streak/crossed_milestone/
    # run_daily_summary/weekly-review-stats/records.update_on_log/
    # dashboard/heatmap-intensity) never names `build_habits_overview` --
    # so `/habits` alone is allowed to differ here; dashboard/daily_summary/
    # records (all named or reused by a named function) must not.
    assert "🛟" in habits, "AC17: /habits must show a grace balance line for every daily habit by default"
    for surface_name, text in (("dashboard", dash), ("daily_summary", summary), ("records", records_text)):
        assert "🛟" not in text, f"{surface_name} leaked a grace balance line: {text!r}"
    db.close()


# ---------------------------------------------------------------------------
# AC9 hole-check: no test anywhere in the v1.9 suite (module or integration)
# actually drives a cadence habit's WEEK-count streak across a milestone
# through the real confirmation flow and checks `milestone_reached_weeks`
# wording fires instead of `milestone_reached` -- this closes that gap.
# `per_week=1` sidesteps any dependency on which real weekday the suite
# happens to run on (a single qualifying day per week is always enough to
# MET that week, so no "Monday and Wednesday must already be in the past
# relative to today" fragility is needed).
# ---------------------------------------------------------------------------


async def test_ac9_cadence_habit_milestone_crossing_uses_week_wording_at_the_wired_level(tmp_path, monkeypatch):
    db = _db(tmp_path, OWNER)
    db.set_cadence(OWNER, "stretch", 1)
    today = date.today()
    this_week_monday = _iso_week_monday(today)
    two_weeks_ago_monday = this_week_monday - timedelta(days=14)
    one_week_ago_monday = this_week_monday - timedelta(days=7)
    _seed(db, f"{two_weeks_ago_monday.isoformat()}T09:00:00", "stretch", 10.0)  # MET (N=1)
    _seed(db, f"{one_week_ago_monday.isoformat()}T09:00:00", "stretch", 10.0)  # MET (N=1)
    db.close()

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    channel = await _run(monkeypatch, config, script=[("message", OWNER, "10 min", None)])
    actual = channel.sent_to(OWNER)[-1]

    expected_milestone = i18n.t("milestone_reached_weeks", "en", streak=3, label="stretch")
    assert expected_milestone in actual, actual
    assert i18n.t("milestone_reached", "en", streak=3, label="stretch") not in actual
    # This is the very first-ever `longest_streak` observation for this
    # habit (no prior record row existed) -- it seeds silently rather than
    # celebrating (records.py's own documented rule), so the milestone line
    # is the ONLY celebration text expected here, not a compound one.
    base = i18n.t("stretch_confirmation", "en", stretch_min=10, ordinal="1st", count=1)
    assert actual.startswith(base + "\n\n" + expected_milestone)


async def test_ac17_habits_line_transitions_from_available_to_used_after_a_real_grace_bridge(tmp_path, monkeypatch):
    db = _db(tmp_path, OWNER)
    today = date.today()
    for offset in range(2, 7):
        day = today - timedelta(days=offset)
        _seed(db, f"{day.isoformat()}T09:00:00", "diary", None, raw="entry")
    db.close()

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})

    # Run 1: /habits BEFORE grace has bridged anything (a fresh async_main
    # run processes its scripted messages before its run_jobs, so this
    # captures the genuinely pre-bridge state).
    before_channel = await _run(monkeypatch, config, script=[("message", OWNER, "/habits", None)])
    before_text = _find(before_channel.sent_to(OWNER), i18n.t("habits_overview_header", "en"))
    assert i18n.t("grace_status_available", "en") in before_text

    # Run 2: grace_tick alone -- bridges yesterday's genuine diary miss.
    await _run(monkeypatch, config, script=[], run_jobs=["grace_tick"])

    # Run 3: /habits again, now genuinely AFTER the bridge was persisted.
    after_channel = await _run(monkeypatch, config, script=[("message", OWNER, "/habits", None)])
    after_text = _find(after_channel.sent_to(OWNER), i18n.t("habits_overview_header", "en"))
    yesterday_weekday = (today - timedelta(days=1)).strftime("%a")
    assert i18n.t("grace_status_used", "en", weekday=yesterday_weekday) in after_text, after_text


# ===========================================================================
# SECTION 2 -- cross-feature interactions integration alone could create.
# ===========================================================================


def test_paused_cadence_habit_week_is_held_neutral_not_broken(tmp_path):
    """A cadence habit's PRIOR completed week, fully paused with zero logs,
    must be NEUTRAL (held) -- an identical week with no pause is a genuine
    MISS and breaks the streak. Immune to today's real weekday: the "MET"
    week and the "held/broken" week are both fully-completed PAST weeks,
    never the partial current one."""
    db = _db(tmp_path, OWNER)
    db.set_cadence(OWNER, "stretch", 3)
    today = date.today()
    this_week_monday = _iso_week_monday(today)
    two_weeks_ago_monday = this_week_monday - timedelta(days=14)
    one_week_ago_monday = this_week_monday - timedelta(days=7)

    # Two weeks ago: MET (Mon/Wed/Fri logged).
    for offset in (0, 2, 4):
        day = two_weeks_ago_monday + timedelta(days=offset)
        _seed(db, f"{day.isoformat()}T09:00:00", "stretch", 10.0)
    # One week ago: fully paused, zero logs -- must be NEUTRAL, not MISSED.
    one_week_ago_sunday = one_week_ago_monday + timedelta(days=6)
    db.insert_pause(OWNER, "stretch", one_week_ago_monday.isoformat(), one_week_ago_sunday.isoformat())
    config = Config.model_validate({"i18n": {"language": "en"}})
    habit = HabitRegistry.from_config(config).get("stretch")

    streak_held = streaks.compute_streak(db, config, habit, today, OWNER)
    assert streak_held == 1, "the paused week must be HELD, so the MET week 2 weeks ago still counts"

    # Control: the identical shape WITHOUT the pause is a genuine miss and
    # breaks the streak at the very first (unpaused) prior-week check.
    db.clear_pauses(OWNER, "stretch")
    streak_broken = streaks.compute_streak(db, config, habit, today, OWNER)
    assert streak_broken == 0, "without the pause, the same empty week is a genuine MISS and breaks the streak"
    db.close()


async def test_paused_cadence_habit_suppresses_proactive_but_a_voluntary_log_still_counts_toward_the_week(tmp_path, monkeypatch):
    db = _db(tmp_path, OWNER)
    today = date.today()
    db.set_cadence(OWNER, "stretch", 3)
    db.insert_pause(OWNER, "stretch", today.isoformat(), (today + timedelta(days=3)).isoformat())
    db.close()

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    registry = HabitRegistry.from_config(config)

    # Proactive suppression: a cadence habit is not special-cased away from
    # the ordinary is_paused gate -- reminders.send_reminder skips it.
    with_db = Database(tmp_path / "habits.db")
    channel = _GateChannel()
    fixed_clock = lambda: datetime(today.year, today.month, today.day, 8, 0, 0)  # noqa: E731
    await reminders.send_reminder(channel, OWNER, registry.get("stretch"), "en", with_db, config)
    assert channel.sent_to(OWNER) == []

    # A voluntary log during the pause still confirms AND still counts
    # toward this week's cadence progress -- pause never hides genuine
    # progress (Rule 16's spirit, expressed through day_qualifies being
    # pause-agnostic for the weekly walk's own qualifying-day count).
    with_db.close()
    channel2 = await _run(monkeypatch, config, script=[("message", OWNER, "10 min", None)])
    assert any("10" in t for t in channel2.sent_to(OWNER)), "the voluntary log must still confirm despite the pause"

    verify_db = Database(tmp_path / "habits.db")
    done, n = cadence.weekly_progress(verify_db, config, registry.get("stretch"), OWNER, today)
    assert done == 1 and n == 3, "the paused-but-logged day must still count toward this week's cadence progress"
    verify_db.close()


async def test_grace_bridge_stays_historical_after_the_habit_later_gets_a_cadence_no_new_bridges(tmp_path):
    db = _db(tmp_path, OWNER)
    today = date.today()
    yesterday = today - timedelta(days=1)
    for offset in range(2, 7):
        day = today - timedelta(days=offset)
        _seed(db, f"{day.isoformat()}T09:00:00", "diary", None, raw="entry")
    registry = HabitRegistry.from_config(Config())
    config = Config.model_validate({"i18n": {"language": "en"}})

    bridged = grace.evaluate_grace(db, config, registry, OWNER, today)
    assert any(habit.id == "diary" for habit, _streak in bridged)
    before = db.grace_protected_dates(OWNER, "diary", yesterday.isoformat(), yesterday.isoformat())
    assert before == {yesterday.isoformat()}

    # diary now becomes a cadence habit.
    db.set_cadence(OWNER, "diary", 3)
    after_cadence_set = db.grace_protected_dates(OWNER, "diary", yesterday.isoformat(), yesterday.isoformat())
    assert after_cadence_set == before, "an existing grace bridge must not be deleted when cadence is added later"
    assert streaks.streak_unit(db, registry.get("diary"), OWNER) == "week"

    # Simulate a later genuine miss (day today+2's own "yesterday" is
    # today+1, left unlogged) -- evaluate_grace must NOT bridge it now that
    # diary is cadence (R6/AC16), and must not touch the historical row.
    later_today = today + timedelta(days=3)
    later_bridged = grace.evaluate_grace(db, config, registry, OWNER, later_today)
    assert not any(habit.id == "diary" for habit, _streak in later_bridged), "a cadence habit must never be bridged"
    window_end = later_today - timedelta(days=1)
    still_only_historical = db.grace_protected_dates(OWNER, "diary", yesterday.isoformat(), window_end.isoformat())
    assert still_only_historical == before, "no NEW grace row may appear for a habit that is now cadence-tracked"
    db.close()


async def test_wrapped_card_for_a_user_with_paused_and_cadence_and_custom_habit_simultaneously(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    db = _db(tmp_path, OWNER)
    today = date.today()
    _seed(db, f"{today.isoformat()}T09:00:00", "diary", None, raw="ok day")
    db.close()

    channel = await _run(
        monkeypatch,
        config,
        script=[
            ("message", OWNER, "/addhabit id=meditate|type=boolean|en=meditate|th=นั่งสมาธิ|cadence=3w", None),
            ("message", OWNER, "/pause water 5d", None),
            ("message", OWNER, "/wrapped", None),
        ],
    )
    images = channel.images_to(OWNER)
    assert images, "expected /wrapped to send a real PNG even with pause+cadence+custom all active at once"
    image_bytes, caption, silent = images[-1]
    assert image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert caption
    assert silent is False


def test_grace_tick_never_bridges_a_paused_daily_habit_no_ledger_row_no_message(tmp_path):
    db = _db(tmp_path, OWNER)
    today = date.today()
    yesterday = today - timedelta(days=1)
    for offset in range(2, 7):
        day = today - timedelta(days=offset)
        _seed(db, f"{day.isoformat()}T09:00:00", "diary", None, raw="entry")
    # diary is paused right through yesterday -- a paused "miss" is NEUTRAL,
    # never a genuine miss, so grace must have nothing to bridge.
    db.insert_pause(OWNER, "diary", (yesterday - timedelta(days=1)).isoformat(), (today + timedelta(days=1)).isoformat())
    registry = HabitRegistry.from_config(Config())
    config = Config.model_validate({"i18n": {"language": "en"}})

    bridged = grace.evaluate_grace(db, config, registry, OWNER, today)
    assert bridged == [], "a paused habit's miss must never be bridged by grace"
    assert db.grace_protected_dates(OWNER, "diary", yesterday.isoformat(), yesterday.isoformat()) == set()
    assert grace.format_grace_message(bridged, "en") == ""
    db.close()


async def test_backfill_into_an_already_paused_day_counts_as_qualified_not_neutral(tmp_path, monkeypatch):
    db = _db(tmp_path, OWNER)
    today = date.today()
    paused_start = today - timedelta(days=5)
    paused_end = today - timedelta(days=1)
    target_day = today - timedelta(days=3)  # inside the paused window
    db.insert_pause(OWNER, "water", paused_start.isoformat(), paused_end.isoformat())
    db.close()

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    registry = HabitRegistry.from_config(config)
    habit = registry.get("water")
    goal = targets.effective_goal(Database(tmp_path / "habits.db"), habit, config, OWNER)
    assert goal == 2500.0

    # Baseline: before any log, target_day is paused with nothing logged ->
    # NEUTRAL under classify_day.
    baseline_db = Database(tmp_path / "habits.db")
    paused_dates = baseline_db.paused_dates(OWNER, "water", target_day.isoformat(), target_day.isoformat())
    state_before = streaks.classify_day(
        baseline_db, config, habit, target_day.isoformat(), OWNER, goal=goal, paused_dates=paused_dates, grace_dates=set()
    )
    assert state_before == "neutral"
    baseline_db.close()

    # A real backfilled log lands ON that exact paused day (a goal-meeting
    # 3000ml, so it qualifies unambiguously): "3 days ago" from today.
    channel = await _run(monkeypatch, config, script=[("message", OWNER, "3000ml 3 days ago", None)])
    assert any("3000" in t or "3,000" in t for t in channel.sent_to(OWNER))

    after_db = Database(tmp_path / "habits.db")
    paused_dates_after = after_db.paused_dates(OWNER, "water", target_day.isoformat(), target_day.isoformat())
    state_after = streaks.classify_day(
        after_db, config, habit, target_day.isoformat(), OWNER, goal=goal, paused_dates=paused_dates_after, grace_dates=set()
    )
    assert state_after == "qualified", "a real backfilled entry must beat the paused-neutral default (Rule 16)"
    after_db.close()


def test_backfill_style_insert_into_a_grace_protected_day_counts_as_qualified_not_neutral(tmp_path):
    """Same Rule-16 precedence, proven through a REAL `grace_ledger` row
    `evaluate_grace` itself wrote (not a hand-built `grace_dates` set),
    then a real backdated `db.insert_log` (mirrors exactly how `main.py`
    times a backfilled row -- `backfill.backdated_ts`) landing on that
    already-bridged date."""
    from habit_assistant.core import backfill

    db = _db(tmp_path, OWNER)
    today = date.today()
    yesterday = today - timedelta(days=1)
    for offset in range(2, 7):
        day = today - timedelta(days=offset)
        _seed(db, f"{day.isoformat()}T09:00:00", "diary", None, raw="entry")
    registry = HabitRegistry.from_config(Config())
    config = Config.model_validate({"i18n": {"language": "en"}})
    habit = registry.get("diary")

    bridged = grace.evaluate_grace(db, config, registry, OWNER, today)
    assert any(h.id == "diary" for h, _s in bridged)
    streak_while_bridged = streaks.compute_streak(db, config, habit, yesterday, OWNER)
    assert streak_while_bridged >= 1, "the bridged day must already read as part of a held streak"

    # A real backfilled log for yesterday (a genuine, later-arriving entry).
    ts = backfill.backdated_ts(yesterday)
    _seed(db, ts, "diary", None, raw="backfilled entry")

    goal = targets.effective_goal(db, habit, config, OWNER)
    grace_dates = db.grace_protected_dates(OWNER, "diary", yesterday.isoformat(), yesterday.isoformat())
    assert yesterday.isoformat() in grace_dates, "the ledger row must still exist (grace never deletes its own row)"
    state = streaks.classify_day(
        db, config, habit, yesterday.isoformat(), OWNER, goal=goal, paused_dates=set(), grace_dates=grace_dates
    )
    assert state == "qualified", "a real backfilled entry on an already-bridged date must read QUALIFIED, not NEUTRAL"
    db.close()


async def test_undo_of_a_log_that_had_made_the_cadence_week_met_drops_the_streak(tmp_path, monkeypatch):
    db = _db(tmp_path, OWNER)
    db.set_cadence(OWNER, "stretch", 3)
    today = date.today()
    this_week_monday = _iso_week_monday(today)
    last_week_monday = this_week_monday - timedelta(days=7)
    mon, wed, fri = last_week_monday, last_week_monday + timedelta(days=2), last_week_monday + timedelta(days=4)
    _seed(db, f"{mon.isoformat()}T09:00:00", "stretch", 10.0)
    _seed(db, f"{wed.isoformat()}T09:00:00", "stretch", 10.0)
    _seed(db, f"{fri.isoformat()}T09:00:00", "stretch", 10.0)
    config = Config.model_validate({"i18n": {"language": "en"}})
    habit = HabitRegistry.from_config(config).get("stretch")
    before = streaks.compute_streak(db, config, habit, today, OWNER)
    assert before == 1, "last week's Mon/Wed/Fri should have MET the 3x/week cadence"
    db.close()

    wired_config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    channel = await _run(monkeypatch, wired_config, script=[("message", OWNER, "/undo", None)])
    assert channel.sent_to(OWNER), "expected an undo confirmation"

    after_db = Database(tmp_path / "habits.db")
    after = streaks.compute_streak(after_db, config, habit, today, OWNER)
    assert after == 0, "undoing the Friday log should drop the week below N=3, un-meeting it, dropping the streak to 0"
    after_db.close()


# ===========================================================================
# SECTION 3 -- 5-site pause gating: normal behavior (excludes ONLY the
# paused habit) plus the actual (not assumed) fail-open posture per site
# under a genuine `active_pauses` read error.
# ===========================================================================


async def test_pause_gating_reminders_site_excludes_only_the_paused_habit(tmp_path):
    db = _db(tmp_path, OWNER)
    today = date.today()
    db.insert_pause(OWNER, "water", today.isoformat(), (today + timedelta(days=1)).isoformat())
    config = Config.model_validate({"i18n": {"language": "en"}})
    registry = HabitRegistry.from_config(config)
    channel = _GateChannel()
    fixed_clock = lambda: datetime(today.year, today.month, today.day, 8, 0, 0)  # noqa: E731

    await reminders.send_reminder(channel, OWNER, registry.get("water"), "en", db, config)
    await reminders.send_reminder(channel, OWNER, registry.get("stretch"), "en", db, config)
    sent = channel.sent_to(OWNER)
    assert len(sent) == 1, f"expected exactly the (non-paused) stretch reminder, got {sent!r}"
    db.close()


async def test_pause_gating_checkins_site_excludes_only_the_paused_habit(tmp_path):
    db = _db(tmp_path, OWNER)
    db.add_user_habit(
        OWNER, {"id": "steps", "type": "numeric", "label_en": "steps", "label_th": "ก้าว", "unit_en": "steps", "unit_th": "ก้าว", "goal": 5000}
    )
    today = date.today()
    db.insert_pause(OWNER, "water", today.isoformat(), (today + timedelta(days=1)).isoformat())
    _seed(db, f"{today.isoformat()}T08:00:00", "steps", 2000.0)  # under goal -> would show
    config = Config.model_validate({"i18n": {"language": "en"}})
    registry = HabitRegistry.for_user(config, db, OWNER)
    fixed_clock = lambda: datetime(today.year, today.month, today.day, 12, 0, 0)  # noqa: E731

    msg = checkins.build_checkin_message(db, config, registry, "en", OWNER, clock=fixed_clock)
    assert msg is not None
    assert "steps" in msg.lower(), f"the non-paused goal habit should still appear: {msg!r}"
    assert "water" not in msg.lower(), f"the paused habit must not appear: {msg!r}"
    db.close()


async def test_pause_gating_nudge_site_excludes_only_the_paused_habit(tmp_path):
    db = _db(tmp_path, OWNER)
    db.add_user_habit(
        OWNER, {"id": "steps", "type": "numeric", "label_en": "steps", "label_th": "ก้าว", "unit_en": "steps", "unit_th": "ก้าว", "goal": 5000}
    )
    today = date.today()
    db.insert_pause(OWNER, "water", today.isoformat(), (today + timedelta(days=1)).isoformat())
    _seed(db, f"{today.isoformat()}T08:00:00", "water", 2400.0)  # 96% of goal -> would normally be "close"
    _seed(db, f"{today.isoformat()}T08:00:00", "steps", 4600.0)  # 92% of goal -> "close"
    config = Config.model_validate({"i18n": {"language": "en"}})
    registry = HabitRegistry.for_user(config, db, OWNER)
    fixed_clock = lambda: datetime(today.year, today.month, today.day, 12, 0, 0)  # noqa: E731

    msg = nudge.build_nudge_message(db, config, registry, "en", OWNER, clock=fixed_clock)
    assert msg is not None
    assert "steps" in msg.lower()
    assert "water" not in msg.lower()
    db.close()


def test_pause_gating_daily_summary_site_excludes_only_the_paused_habit(tmp_path):
    db = _db(tmp_path, OWNER)
    today = date.today()
    db.insert_pause(OWNER, "diary", today.isoformat(), (today + timedelta(days=1)).isoformat())
    _seed(db, f"{today.isoformat()}T09:00:00", "water", 500.0)
    _seed(db, f"{today.isoformat()}T21:00:00", "diary", None, raw="entry")
    config = Config.model_validate({"i18n": {"language": "en"}})
    registry = HabitRegistry.from_config(config)

    lines = streaks.compute_daily_summary(db, config, registry, today, OWNER)
    ids = [line.habit.id for line in lines]
    assert "water" in ids and "diary" not in ids
    db.close()


def test_pause_gating_weekly_review_site_excludes_only_the_paused_habit(tmp_path):
    db = _db(tmp_path, OWNER)
    today = date.today()
    db.insert_pause(OWNER, "diary", today.isoformat(), (today + timedelta(days=1)).isoformat())
    _seed(db, f"{today.isoformat()}T09:00:00", "water", 500.0)
    _seed(db, f"{today.isoformat()}T21:00:00", "diary", None, raw="entry")
    config = Config.model_validate({"i18n": {"language": "en"}})
    registry = HabitRegistry.from_config(config)

    stats = review.compute_weekly_stats(db, config, registry, today, OWNER)
    ids = [hs.habit.id for hs in stats.habits]
    assert "water" in ids and "diary" not in ids
    db.close()


async def test_pause_gating_weekly_review_trends_block_also_excludes_only_the_paused_habit(tmp_path):
    """The stats section (previous test) and the embedded `trends.
    review_block` section are TWO SEPARATE call sites inside `run_weekly_
    review` (review.py:158 and review.py:310) -- neither module nor
    integration Vera exercised the second one directly; this closes that
    hole."""
    db = _db(tmp_path, OWNER)
    today = date.today()
    db.insert_pause(OWNER, "diary", today.isoformat(), (today + timedelta(days=1)).isoformat())
    _seed(db, f"{today.isoformat()}T09:00:00", "water", 500.0)
    _seed(db, f"{(today - timedelta(days=7)).isoformat()}T09:00:00", "water", 500.0)
    _seed(db, f"{today.isoformat()}T21:00:00", "diary", None, raw="entry")
    _seed(db, f"{(today - timedelta(days=7)).isoformat()}T21:00:00", "diary", None, raw="entry")
    config = Config.model_validate({"i18n": {"language": "en"}})
    registry = HabitRegistry.from_config(config)

    text = await review.run_weekly_review(db, config, registry, _FakeOllamaClient(), "en", OWNER, today=today)
    trends_header = i18n.t("trends_review_header", "en")
    assert trends_header in text, text
    trends_section = text[text.index(trends_header) :]
    assert "water" in trends_section.lower()
    assert "diary" not in trends_section.lower()
    db.close()


async def test_pause_gating_fail_open_posture_actually_observed_at_each_site(tmp_path):
    """Not an assumption -- the ACTUAL behavior of each of the 5 R15 sites
    when `db.active_pauses` (what `pause.is_paused`/`is_paused_safe` reads)
    raises. Findings are asserted explicitly so a future change that alters
    this posture shows up as a failing assertion here, not as silent drift.

    SPEC-v1.10.md §4 R18/R-SS9 (module `riders`): this v1.9-era test
    originally locked in an INCONSISTENT posture (site 1 explicitly
    fail-open; sites 2/4/5 raised outright; site 3 raised internally but
    was masked one level up by `run_due_nudges`' own try/except) -- exactly
    the finding `PROGRESS.md`'s v1.9.0 changelog row flagged as a
    non-blocking follow-up. R18 unifies all 5 sites on the shared
    `pause.is_paused_safe`/`active_pauses_safe` helpers (R-SS9), so every
    site now fails OPEN the same way site 1 always did -- updated below to
    match; see `tests/test_pause_failopen.py` for the fuller per-site
    AC16 coverage (habit-inclusion + multi-user fan-out continuation)."""
    db = _db(tmp_path, OWNER)
    today = date.today()
    _seed(db, f"{today.isoformat()}T09:00:00", "diary", None, raw="entry")
    config = Config.model_validate({"i18n": {"language": "en"}})
    registry = HabitRegistry.from_config(config)
    raising_db = _ActivePausesRaisingDB(db)
    fixed_clock = lambda: datetime(today.year, today.month, today.day, 8, 0, 0)  # noqa: E731

    # Site 1 -- reminders.send_reminder: EXPLICITLY fail-open (now via the
    # shared `pause.is_paused_safe`, R-SS9) -- a pause-read error must
    # never suppress a reminder.
    channel = _GateChannel()
    await reminders.send_reminder(channel, OWNER, registry.get("diary"), "en", raising_db, config)
    assert channel.sent_to(OWNER), "reminders.send_reminder must fail OPEN (send anyway) on a pause-read error"

    # Site 2 -- checkins.build_checkin_message: now routed through
    # `pause.active_pauses_safe` (R18) -- a read error no longer propagates;
    # the habit is treated as not-paused and the check-in still builds.
    message = checkins.build_checkin_message(raising_db, config, registry, "en", OWNER, clock=fixed_clock)
    assert message is not None, "checkins.build_checkin_message must fail OPEN, not raise, on a pause-read error"

    # Site 3 -- nudge.build_nudge_message: also now routed through
    # `pause.active_pauses_safe` (R18) -- no longer raises internally at
    # all (previously masked one level up by run_due_nudges' own
    # try/except, R18's own "rather than dropping the whole user's nudge"
    # nuance: with a genuinely close habit, the user now gets their nudge
    # the same day the read fails, not merely "the tick survives").
    _seed(db, f"{today.isoformat()}T20:00:00", "water", 2000.0)  # 2000/2500 = 80% -- close
    nudge_channel = _GateChannel()
    nudge_clock = lambda: datetime(today.year, today.month, today.day, 20, 0, 0)  # noqa: E731
    db.set_checkin_window(OWNER, "08:00-20:00")  # nudge rides check-in enablement
    await nudge.run_due_nudges(nudge_channel, config, registry, raising_db, clock=nudge_clock)
    assert nudge_channel.sent_to(OWNER), "nudge must fail OPEN: a genuinely close habit still gets its nudge"

    # Site 4 -- streaks.compute_daily_summary: now routed through
    # `pause.is_paused_safe` (R18) -- no longer propagates.
    lines = streaks.compute_daily_summary(raising_db, config, registry, today, OWNER)
    assert lines, "streaks.compute_daily_summary must fail OPEN, not raise, on a pause-read error"

    # Site 5 -- review.compute_weekly_stats: now routed through
    # `pause.is_paused_safe` (R18) -- no longer propagates.
    stats = review.compute_weekly_stats(raising_db, config, registry, today, OWNER)
    assert stats.habits, "review.compute_weekly_stats must fail OPEN, not raise, on a pause-read error"

    db.close()


# ===========================================================================
# SECTION 4 -- "system" audit source: bilingual rendering, and no
# regression to any pre-existing action's rendering.
# ===========================================================================


def test_system_audit_source_renders_bilingually_and_existing_actions_are_unchanged(tmp_path):
    db = _db(tmp_path, OWNER)
    # One pre-v1.9 action (command-sourced) alongside the new system-sourced one.
    audit.record(db, actor=OWNER, action="target_set", source="command", entity="water", old_value=None, new_value=2000.0)
    audit.record(db, actor=OWNER, action="grace_consumed", source="system", entity="diary", old_value=None, new_value="2026-08-20")

    for lang in ("en", "th"):
        text = audit_view.render_recent(db, Config(), lang, limit=10, owner_chat_id=OWNER)
        assert "(system)" in text, f"{lang}: the raw source token must render verbatim, unlocalized"
        assert "(command)" in text, f"{lang}: a pre-existing command-sourced row must render exactly as before"
        assert i18n.t("audit_action_grace_consumed", lang) in text
        assert i18n.t("audit_action_target_set", lang) in text
        assert "diary" in text
        assert "water" in text
    db.close()


# ===========================================================================
# SECTION 5 -- menu counts (both languages) + /help content + render budget.
# ===========================================================================


async def test_public_and_owner_menu_counts_both_languages(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    channel = await _run(monkeypatch, config, script=[])
    assert len(channel.set_my_commands_calls) == 2
    public, public_scope = channel.set_my_commands_calls[0]
    owner, owner_scope = channel.set_my_commands_calls[1]
    assert public_scope is None
    assert owner_scope == OWNER

    # SPEC-v1.10.md §4 R17 (integration pass): 22 -> 23 / 27 -> 28, `/guide` added.
    for lang in ("en", "th"):
        public_names = [n for n, _d in public[lang]]
        owner_names = [n for n, _d in owner[lang]]
        assert len(public_names) == 23, f"{lang}: public menu drifted from 23: {public_names!r}"
        assert len(owner_names) == 28, f"{lang}: owner menu drifted from 28: {owner_names!r}"
        assert {"cadence", "pause", "resume", "wrapped", "guide"} <= set(public_names)
        assert set(public_names) <= set(owner_names), "the owner menu must be a strict superset of the public one"
        assert {"invite", "approve", "block", "users", "audit"} <= (set(owner_names) - set(public_names))


def test_help_text_has_the_four_new_command_lines_and_grace_capability_line_bilingual_within_budget():
    config = Config()
    for lang in ("en", "th"):
        text = build_help_text(config, lang)
        assert i18n.t("help_cadence_cmd", lang) in text
        assert i18n.t("help_pause_cmd", lang) in text
        assert i18n.t("help_resume_cmd", lang) in text
        assert i18n.t("help_wrapped_cmd", lang) in text
        assert i18n.t("help_grace", lang) in text
        assert len(text) <= TELEGRAM_MESSAGE_BUDGET, f"{lang}: /help text ({len(text)} chars) exceeds the Telegram budget"


# ===========================================================================
# SECTION 6 -- grace message: disable_notification is ALWAYS True, even
# with [notifications] silent_proactive explicitly false (Archi's stated
# ruling, main.py's grace_tick).
# ===========================================================================


async def test_grace_message_is_always_silent_even_when_silent_proactive_is_false(tmp_path, monkeypatch):
    db = _db(tmp_path, OWNER)
    today = date.today()
    for offset in range(2, 7):
        day = today - timedelta(days=offset)
        _seed(db, f"{day.isoformat()}T09:00:00", "diary", None, raw="entry")
    db.close()

    config = Config.model_validate(
        {
            "app": {"db_path": str(tmp_path / "habits.db")},
            "i18n": {"language": "en"},
            "notifications": {"silent_proactive": False},
        }
    )
    assert config.notifications.silent_proactive is False
    channel = await _run(monkeypatch, config, script=[], run_jobs=["grace_tick"])
    grace_sends = [(t, silent) for cid, t, silent in channel.sent if cid == OWNER]
    assert grace_sends, "expected the grace bridge to fire and send a message"
    for _text, silent in grace_sends:
        assert silent is True, "the grace message must be silent regardless of silent_proactive=false"


# ===========================================================================
# SECTION 7 -- RELEASE_NOTES["1.9.0"] announce-readiness, unit level only.
# Deliberately does NOT touch VERSION/pyproject.toml -- Archi's own Phase
# 6.5 owns the actual version bump/commit/tag.
# ===========================================================================


def test_release_notes_1_9_0_is_announce_ready_bilingual_and_non_empty():
    assert "1.9.0" in release_notes.RELEASE_NOTES
    note = release_notes.RELEASE_NOTES["1.9.0"]
    assert set(note.keys()) == {"en", "th"}
    for lang, text in note.items():
        assert isinstance(text, str) and text.strip(), f"{lang}: release note text must be non-empty"
        assert len(text) <= TELEGRAM_MESSAGE_BUDGET, f"{lang}: release note ({len(text)} chars) exceeds the Telegram budget"


async def test_release_notes_1_9_0_actually_announces_once_per_active_user(tmp_path):
    from habit_assistant.core import announce

    db = _db(tmp_path, OWNER, MEMBER)
    channel = _GateChannel()
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "i18n": {"language": "en"}})
    await announce.announce_release(db, channel, config, "1.9.0")
    assert any(cid == OWNER for cid, _t, _s in channel.sent)
    assert any(cid == MEMBER for cid, _t, _s in channel.sent)
    channel.sent.clear()
    await announce.announce_release(db, channel, config, "1.9.0")
    assert channel.sent == [], "a second announce of the same version must send nothing more (idempotent)"
    db.close()
