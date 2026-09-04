"""SPEC-v1.6.md §4 Feature 1 "Live pinned Today dashboard" (module
`dashboard`, R-D1-R-D6): `core/commands.dispatch`'s `"dashboard"` kind,
`core/dashboard.py`'s `render`/`refresh`/`execute_dashboard`.

Owned ACs (SPEC-v1.6.md §11): AC-D1 (opt-in), AC-D2 (live silent edit +
unchanged-skip), AC-D3 (self-heal + fail-open), AC-D4 (day rollover),
AC-D5 (DND-exempt), AC-D6 (registry-generic content by type).

Mirrors `tests/test_checkins.py`'s own conventions: `commands.dispatch`
directly, a real on-disk SQLite `Database` (no mocks), a `FakeChannel`
implementing the new pin/edit/unpin methods, and an injectable `clock`."""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import commands, dashboard, i18n
from habit_assistant.core.commands import Command
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner-chat"
MEMBER = "member-chat-b"

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


@pytest.fixture(autouse=True)
def _clear_dashboard_cache():
    """`dashboard._last_rendered` (R-D3's own "in-process per-user cache")
    is module-level state that would otherwise leak between test cases
    sharing a `user_id` -- clear it before and after every test."""
    dashboard._last_rendered.clear()
    yield
    dashboard._last_rendered.clear()


class FakeChannel(Channel):
    """Implements the full new pin/edit/unpin surface (unlike the base
    ABC's concrete defaults) so this module's own tests exercise
    `dashboard.py`'s real success/self-heal/fail-open branches, not just
    the degraded path (that's `tests/test_channels.py`'s own job, AC-2)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.pinned: list[tuple[str, str]] = []
        self.edited: list[tuple[str, str, str]] = []
        self.unpinned: list[tuple[str, str]] = []
        self._next_id = 1
        # Configurable per-test: a literal id/None, or an Exception
        # instance to raise instead.
        self.send_and_pin_result: object = "auto"
        self.edit_result: object = True

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError

    async def send_and_pin(self, chat_id: str, text: str) -> str | None:
        self.pinned.append((chat_id, text))
        if isinstance(self.send_and_pin_result, Exception):
            raise self.send_and_pin_result
        if self.send_and_pin_result == "auto":
            msg_id = str(self._next_id)
            self._next_id += 1
            return msg_id
        return self.send_and_pin_result  # type: ignore[return-value]

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> bool:
        self.edited.append((chat_id, message_id, text))
        if isinstance(self.edit_result, Exception):
            raise self.edit_result
        return self.edit_result  # type: ignore[return-value]

    async def unpin(self, chat_id: str, message_id: str) -> None:
        self.unpinned.append((chat_id, message_id))

    def sent_to(self, chat_id: str) -> list[str]:
        return [text for cid, text in self.sent if cid == chat_id]


def _habit(
    id_: str,
    type_: str = "numeric",
    *,
    label_en: str = "test",
    label_th: str = "ทดสอบ",
    unit_en: str | None = "u",
    unit_th: str | None = "ห",
    goal: float | None = None,
) -> Habit:
    return Habit(
        id=id_,
        type=type_,
        label_en=label_en,
        label_th=label_th,
        unit_en=unit_en if type_ in ("numeric", "duration") else None,
        unit_th=unit_th if type_ in ("numeric", "duration") else None,
        goal=goal,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "dashboard.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(MEMBER, role="member", status="active")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


def _fixed_clock(y=2026, m=8, d=24, hh=9, mm=0):  # 2026-08-24 is a Monday
    return lambda: datetime(y, m, d, hh, mm, 0)


# ===========================================================================
# dispatch() shape -- /dashboard (R-D1)
# ===========================================================================


@pytest.mark.parametrize(
    "text,expected_value",
    [
        ("/dashboard on", "on"),
        ("/dashboard off", "off"),
        ("/DASHBOARD ON", "on"),  # case-insensitive slash trigger
        ("แดชบอร์ด on", "on"),
        ("แดชบอร์ด off", "off"),
    ],
)
def test_dispatch_recognizes_dashboard_shape(text, expected_value):
    assert commands.dispatch(text, DEFAULT_REGISTRY) == Command(kind="dashboard", pref_value=expected_value)


def test_dispatch_bare_slash_dashboard_means_show_not_usage():
    """R-D1: unlike bare "/lang"/"/quiet", a bare "/dashboard" IS a
    recognized shape with its own meaning (show), not a usage error."""
    assert commands.dispatch("/dashboard", DEFAULT_REGISTRY) == Command(kind="dashboard", pref_value=None)


def test_dispatch_bare_thai_dashboard_also_means_show():
    assert commands.dispatch("แดชบอร์ด", DEFAULT_REGISTRY) == Command(kind="dashboard", pref_value=None)


def test_dispatch_dashboard_slash_carries_through_an_invalid_tail_unvalidated():
    """Shape-only layer, mirrors `/checkin`'s own permissive slash form --
    `execute_dashboard` is where an invalid tail becomes a usage reply."""
    assert commands.dispatch("/dashboard maybe", DEFAULT_REGISTRY) == Command(kind="dashboard", pref_value="maybe")


def test_dispatch_dashboard_thai_rejects_an_invalid_tail_shape_entirely():
    """The Thai alias, unlike the slash form, gates on valid-argument SHAPE
    (only "on"/"off" -- dashboard's grammar is strictly binary) -- an
    out-of-shape tail falls through to None rather than reaching
    execute_dashboard at all."""
    assert commands.dispatch("แดชบอร์ด default", DEFAULT_REGISTRY) is None
    assert commands.dispatch("แดชบอร์ด 09:00", DEFAULT_REGISTRY) is None


# ===========================================================================
# adversarial corpus -- แดชบอร์ด is a common transliterated loanword ("car
# dashboard", "data dashboard") that can open ordinary prose; a glued
# continuation or an out-of-shape spaced tail must fall through to None,
# same AC5.5/R-D1 conservatism every other command in this router honors.
# ===========================================================================

DASHBOARD_ADVERSARIAL_CORPUS = [
    "แดชบอร์ดรถเสีย",  # "[the] car dashboard is broken" -- glued, no space
    "แดชบอร์ดข้อมูลสวยมาก",  # "[the] data dashboard looks great" -- glued
    "แดชบอร์ด สวยมาก",  # spaced but not "on"/"off" shape
    "แดชบอร์ด ของฉันพัง",  # spaced ordinary prose, not "on"/"off"
    "I love this dashboard",  # English prose containing "dashboard", not the Thai trigger at all
    "the car's dashboard lights up",  # English, unrelated
]


@pytest.mark.parametrize("message", DASHBOARD_ADVERSARIAL_CORPUS)
def test_dispatch_returns_none_for_dashboard_adversarial_corpus(message):
    assert commands.dispatch(message, DEFAULT_REGISTRY) is None


def test_ordinary_habit_logs_still_fall_through_to_the_parser():
    assert commands.dispatch("500ml", DEFAULT_REGISTRY) is None
    assert commands.dispatch("10 min stretch", DEFAULT_REGISTRY) is None


def test_dashboard_does_not_shadow_other_command_kinds():
    assert commands.dispatch("/quiet 22:00-07:00", DEFAULT_REGISTRY).kind == "quiet"
    assert commands.dispatch("/checkin on", DEFAULT_REGISTRY).kind == "checkin"
    assert commands.dispatch("/target water 2000", DEFAULT_REGISTRY).kind == "target"
    assert commands.dispatch("/undo", DEFAULT_REGISTRY).kind == "undo"


# ===========================================================================
# render -- AC-D6: registry-generic content by type, + streaks, bilingual.
# ===========================================================================


def test_render_goal_bearing_shows_bar_and_pct(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_en="water", unit_en="ml")])
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "water", 1500.0, None, "1500ml", "reply"))

    text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())

    assert "water" in text
    assert "1500" in text and "2500" in text
    assert "60%" in text  # 1500/2500 = 60%
    assert "▓" in text and "░" in text


def test_render_bar_reflects_progress_proportionally(db, config):
    # A non-"water" id, deliberately -- `targets.config_goal` special-cases
    # `habit.id == "water"` to always read the LEGACY
    # `config.reminders.water.goal_ml` (R-T4), which would silently
    # override this test's own `goal=1000.0` and defeat the point.
    registry = HabitRegistry([_habit("hydration", "numeric", goal=1000.0, label_en="hydration", unit_en="ml")])
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "hydration", 500.0, None, "500ml", "reply"))

    text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    line = next(line for line in text.splitlines() if "hydration" in line)
    assert line.count("▓") == 5
    assert line.count("░") == 5


def test_render_boolean_shows_check_when_done(db, config):
    registry = HabitRegistry([_habit("meditate", "boolean", label_en="meditate", unit_en=None)])
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "meditate", 1.0, None, "meditated", "reply"))

    text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    line = next(line for line in text.splitlines() if "meditate" in line)
    assert "✓" in line


def test_render_boolean_shows_dash_when_not_done(db, config):
    registry = HabitRegistry([_habit("meditate", "boolean", label_en="meditate", unit_en=None)])

    text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    line = next(line for line in text.splitlines() if "meditate" in line)
    assert "–" in line
    assert "✓" not in line


def test_render_count_only_for_goal_less_duration_habit(db, config):
    registry = HabitRegistry([_habit("stretch", "duration", label_en="stretch", unit_en="min")])
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "stretch", 10.0, None, "10 min", "reply"))
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T09:00:00", "stretch", 5.0, None, "5 min", "reply"))

    text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    line = next(line for line in text.splitlines() if "stretch" in line)
    assert "2" in line  # two log rows today -- a count, not a sum


def test_render_count_only_for_text_habit(db, config):
    registry = HabitRegistry([_habit("diary", "text", label_en="diary", unit_en=None)])
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T21:00:00", "diary", None, "good day", "wrote about my day", "reply"))

    text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    line = next(line for line in text.splitlines() if "diary" in line)
    assert "1" in line


def test_render_target_override_changes_goal_bearing_math(db, config):
    """AC-D6/R-D2: `dashboard.render` reads through `targets.effective_goal`
    -- a `/target` override for this user changes the pct/bar shown, and
    a goal-less habit becomes goal-bearing once an override exists
    (R-T5b's own "an override may exist for any goal-able habit" rule)."""
    registry = HabitRegistry([_habit("stretch", "duration", label_en="stretch", unit_en="min")])
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "stretch", 10.0, None, "10 min", "reply"))

    before = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    assert "%" not in before  # no goal yet -- count-only line

    db.set_target(OWNER, "stretch", 20.0)
    after = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    line = next(line for line in after.splitlines() if "stretch" in line)
    assert "50%" in line  # 10/20 = 50%


def test_render_includes_streak(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=1000.0, label_en="water", unit_en="ml")])
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T08:00:00", "water", 1000.0, None, "1000ml", "reply"))
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "water", 1000.0, None, "1000ml", "reply"))

    text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    line = next(line for line in text.splitlines() if "water" in line)
    assert "2" in line  # a 2-day streak ending today


def test_render_streak_shows_living_streak_not_zero_when_today_is_partial(db, config):
    """v1.3.2+line bug fix -- the exact live-data scenario Archi root-caused
    on a real user's data: goal-bearing habit met the two days before
    today, today logged but still below goal. Before the fix this line
    read "streak 0d" (`compute_streak(end=today)` breaks on today's own
    not-yet-met day, discarding the real, unbroken run through yesterday).

    This IS "the log-confirmation streak surface" from the user's report:
    `dashboard.refresh` sends exactly this render right after every log
    (`core/routing.py`/`core/quicklog.py` both call it immediately after
    `confirmation.suffix`), so it's the reply a user actually reads to
    answer "did my streak register?" -- `core/confirmation.py` itself
    carries no bare streak number of its own (only a milestone/record
    celebration suffix, which only ever fires once `today` already
    qualifies, so it was never exposed to this bug in the first place --
    see IMPL-STREAK-DISPLAY.md's caller-by-caller table)."""
    registry = HabitRegistry([_habit("juice", "numeric", goal=2500.0, label_en="juice", unit_en="ml")])
    db.insert_log(LogEntry(None, OWNER, "2026-08-22T08:00:00", "juice", 2500.0, None, "2500ml", "reply"))
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T08:00:00", "juice", 2500.0, None, "2500ml", "reply"))
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "juice", 1250.0, None, "1250ml", "reply"))  # partial

    text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    line = next(line for line in text.splitlines() if "juice" in line)
    assert "streak 2d" in line

    # A later log the same day crosses the goal -- the streak really did
    # extend today, so it should now read 3, not just fall back to 2.
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T18:00:00", "juice", 1250.0, None, "1250ml", "reply"))
    text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    line = next(line for line in text.splitlines() if "juice" in line)
    assert "streak 3d" in line


def test_render_registry_generic_extra_habit_appears_automatically(db, config):
    """AC-X1/AC-D6: an extra configured habit with no special-casing
    anywhere in `dashboard.py` renders correctly -- no hardcoded habit
    ids (R-X1)."""
    registry = HabitRegistry(
        [
            _habit("water", "numeric", goal=2500.0, label_en="water", unit_en="ml"),
            _habit("pushups", "numeric", goal=50.0, label_en="pushups", unit_en="reps"),
        ]
    )
    text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    assert "water" in text
    assert "pushups" in text


def test_render_bilingual(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_en="water", label_th="น้ำ", unit_en="ml", unit_th="มล.")])
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "water", 1200.0, None, "1200ml", "reply"))

    en_text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    th_text = dashboard.render(db, config, registry, "th", OWNER, clock=_fixed_clock())
    assert en_text != th_text
    assert i18n.detect_language(th_text) == "th"


def test_render_isolated_per_user(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_en="water", unit_en="ml")])
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "water", 1200.0, None, "1200ml", "reply"))
    db.insert_log(LogEntry(None, MEMBER, "2026-08-24T08:00:00", "water", 300.0, None, "300ml", "reply"))

    owner_text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    member_text = dashboard.render(db, config, registry, "en", MEMBER, clock=_fixed_clock())

    assert "1200" in owner_text and "1200" not in member_text
    assert "300" in member_text and "300" not in owner_text


def test_render_header_shows_todays_date(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0)])
    text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    assert "24 Aug" in text.splitlines()[0]


def test_render_never_imports_or_calls_an_llm():
    assert "habit_assistant.llm" not in inspect.getsource(dashboard)
    for fn in (dashboard.render, dashboard.refresh, dashboard.execute_dashboard):
        assert "llm" not in inspect.signature(fn).parameters


# ===========================================================================
# execute_dashboard -- AC-D1: on/off/show/usage/db-failure/audit.
# ===========================================================================


async def test_execute_dashboard_on_sends_pins_stores_id_and_audits(db, config):
    channel = FakeChannel()
    command = commands.dispatch("/dashboard on", DEFAULT_REGISTRY)
    reply = await dashboard.execute_dashboard(
        command, db=db, channel=channel, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER,
        clock=_fixed_clock(),
    )

    assert len(channel.pinned) == 1
    assert channel.pinned[0][0] == OWNER
    stored_id = db.get_dashboard_msg_id(OWNER)
    assert stored_id is not None
    assert reply == i18n.t("dashboard_set_on", "en")

    audit_rows = db.recent_audit(limit=10)
    assert any(row["action"] == "dashboard_set" and row["user_id"] == OWNER for row in audit_rows)


async def test_execute_dashboard_off_records_a_dashboard_off_audit_row(db, config):
    channel = FakeChannel()
    await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )
    await dashboard.execute_dashboard(
        commands.dispatch("/dashboard off", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )
    audit_rows = db.recent_audit(limit=10)
    assert any(row["action"] == "dashboard_off" and row["user_id"] == OWNER for row in audit_rows)


async def test_execute_dashboard_off_unpins_clears_and_audits(db, config):
    channel = FakeChannel()
    await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )
    stored_id = db.get_dashboard_msg_id(OWNER)

    reply = await dashboard.execute_dashboard(
        commands.dispatch("/dashboard off", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )

    assert channel.unpinned == [(OWNER, stored_id)]
    assert db.get_dashboard_msg_id(OWNER) is None
    assert reply == i18n.t("dashboard_set_off", "en")


async def test_execute_dashboard_off_when_never_on_is_a_safe_noop(db, config):
    channel = FakeChannel()
    reply = await dashboard.execute_dashboard(
        commands.dispatch("/dashboard off", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )
    assert channel.unpinned == []  # nothing to unpin
    assert db.get_dashboard_msg_id(OWNER) is None
    assert reply == i18n.t("dashboard_set_off", "en")


async def test_execute_dashboard_bare_shows_off_by_default(db, config):
    channel = FakeChannel()
    reply = await dashboard.execute_dashboard(
        commands.dispatch("/dashboard", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )
    assert reply == i18n.t("dashboard_show_off", "en")
    assert db.get_dashboard_msg_id(OWNER) is None  # read-only, no write


async def test_execute_dashboard_bare_shows_on_after_enabling(db, config):
    channel = FakeChannel()
    await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )
    reply = await dashboard.execute_dashboard(
        commands.dispatch("/dashboard", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )
    assert reply == i18n.t("dashboard_show_on", "en")


async def test_execute_dashboard_invalid_tail_replies_usage_and_writes_nothing(db, config):
    channel = FakeChannel()
    command = commands.dispatch("/dashboard maybe", DEFAULT_REGISTRY)
    reply = await dashboard.execute_dashboard(
        command, db=db, channel=channel, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER,
        clock=_fixed_clock(),
    )
    assert reply == i18n.t("dashboard_usage", "en")
    assert db.get_dashboard_msg_id(OWNER) is None
    assert channel.pinned == []


async def test_execute_dashboard_default_is_a_default_user_has_no_dashboard(db, config):
    """AC-D1: a default user (NULL, nobody ever ran /dashboard on) has no
    dashboard and gets no updates -- `refresh` is a silent no-op."""
    channel = FakeChannel()
    assert db.get_dashboard_msg_id(OWNER) is None
    await dashboard.refresh(db, channel, config, DEFAULT_REGISTRY, OWNER, clock=_fixed_clock())
    assert channel.edited == []
    assert channel.pinned == []


async def test_execute_dashboard_on_reports_unsupported_when_channel_cant_pin(db, config):
    channel = FakeChannel()
    channel.send_and_pin_result = None  # concrete-default degradation shape
    reply = await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )
    assert reply == i18n.t("dashboard_unsupported", "en")
    assert db.get_dashboard_msg_id(OWNER) is None


async def test_execute_dashboard_send_failure_reports_save_failed_not_a_traceback(db, config):
    channel = FakeChannel()
    channel.send_and_pin_result = RuntimeError("network down")
    reply = await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )
    assert reply == i18n.t("dashboard_save_failed", "en")


async def test_execute_dashboard_db_failure_reports_save_failed_not_a_traceback(db, config, monkeypatch):
    def _boom(self, chat_id, message_id):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(Database, "set_dashboard_msg_id", _boom)
    channel = FakeChannel()
    reply = await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )
    assert reply == i18n.t("dashboard_save_failed", "en")


async def test_execute_dashboard_bilingual_replies(db, config):
    channel = FakeChannel()
    reply_th = await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="th", user_id=OWNER, clock=_fixed_clock(),
    )
    assert reply_th == i18n.t("dashboard_set_on", "th")


async def test_execute_dashboard_isolated_per_user(db, config):
    channel = FakeChannel()
    await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )
    assert db.get_dashboard_msg_id(OWNER) is not None
    assert db.get_dashboard_msg_id(MEMBER) is None


# ===========================================================================
# refresh -- AC-D2 (live silent edit + unchanged-skip), AC-D3 (self-heal +
# fail-open), AC-D4 (day rollover), AC-D5 (DND-exempt).
# ===========================================================================


async def _enable(db, config, channel, user_id) -> str:
    await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=user_id, clock=_fixed_clock(),
    )
    return db.get_dashboard_msg_id(user_id)


async def test_refresh_edits_in_place_reflecting_new_progress(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_en="water", unit_en="ml")])
    channel = FakeChannel()
    msg_id = await _enable(db, config, channel, OWNER)
    channel.pinned.clear()

    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "water", 1500.0, None, "1500ml", "reply"))
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())

    assert len(channel.edited) == 1
    edited_chat, edited_id, edited_text = channel.edited[0]
    assert edited_chat == OWNER
    assert edited_id == msg_id
    assert "1500" in edited_text
    assert channel.pinned == []  # no new message -- edited in place (AC-D2)


async def test_refresh_skips_a_redundant_edit_when_render_is_unchanged(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_en="water", unit_en="ml")])
    channel = FakeChannel()
    await _enable(db, config, channel, OWNER)
    channel.edited.clear()

    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "water", 1500.0, None, "1500ml", "reply"))
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())
    assert len(channel.edited) == 1  # first refresh after the change -- one edit

    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())
    assert len(channel.edited) == 1  # second refresh, nothing changed -- skipped (R-D3)


async def test_refresh_self_heals_when_the_pinned_message_was_deleted(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_en="water", unit_en="ml")])
    channel = FakeChannel()
    old_id = await _enable(db, config, channel, OWNER)
    channel.pinned.clear()
    channel.edit_result = False  # simulates "message to edit not found"

    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "water", 1500.0, None, "1500ml", "reply"))
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())

    assert len(channel.pinned) == 1  # AC-D3: recreated + re-pinned
    new_id = db.get_dashboard_msg_id(OWNER)
    assert new_id is not None
    assert new_id != old_id  # a genuinely new id was stored


async def test_refresh_self_heal_that_also_cant_pin_disables_gracefully(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_en="water", unit_en="ml")])
    channel = FakeChannel()
    await _enable(db, config, channel, OWNER)
    channel.edit_result = False
    channel.send_and_pin_result = None  # channel now can't pin at all either

    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())
    assert db.get_dashboard_msg_id(OWNER) is None  # honestly falls back to disabled


async def test_refresh_is_fail_open_when_the_channel_raises(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_en="water", unit_en="ml")])
    channel = FakeChannel()
    await _enable(db, config, channel, OWNER)
    channel.edit_result = RuntimeError("boom")
    channel.send_and_pin_result = RuntimeError("boom too")

    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "water", 1500.0, None, "1500ml", "reply"))
    # Must not raise -- R-D4's own "never breaks the triggering log" contract.
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())


async def test_refresh_is_fail_open_when_the_db_raises(config):
    class _RaisingDatabase:
        def get_dashboard_msg_id(self, chat_id):
            raise RuntimeError("simulated DB failure")

    channel = FakeChannel()
    # Must not raise.
    await dashboard.refresh(_RaisingDatabase(), channel, config, DEFAULT_REGISTRY, OWNER, clock=_fixed_clock())
    assert channel.edited == []


async def test_refresh_day_rollover_shows_the_new_day_zeroed(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_en="water", unit_en="ml")])
    channel = FakeChannel()
    msg_id = await _enable(db, config, channel, OWNER)
    channel.edited.clear()

    db.insert_log(LogEntry(None, OWNER, "2026-08-24T20:00:00", "water", 2500.0, None, "2500ml", "reply"))
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock(hh=23, mm=59))
    last_night_text = channel.edited[-1][2]
    assert "2500" in last_night_text

    # A refresh after local midnight, no new log yet today -- AC-D4:
    # yesterday's total is cleared, the new day starts at 0.
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock(d=25, hh=0, mm=0))
    new_day_text = channel.edited[-1][2]
    assert "25 Aug" in new_day_text.splitlines()[0]
    assert "0 / 2500" in new_day_text


async def test_refresh_is_dnd_exempt(db, config):
    """AC-D5: a user in DND still gets their dashboard edited -- structural
    check (mirrors test_checkins.py's own AC-4 LLM-free technique): this
    module never even imports/calls the DND primitive."""
    assert "in_dnd_now" not in inspect.getsource(dashboard)
    assert "dnd" not in inspect.signature(dashboard.refresh).parameters

    db.set_user_quiet_hours(OWNER, '[["00:00", "12:00"], ["12:00", "00:00"]]')  # always-DND
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_en="water", unit_en="ml")])
    channel = FakeChannel()
    await _enable(db, config, channel, OWNER)
    channel.edited.clear()

    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "water", 500.0, None, "500ml", "reply"))
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())
    assert len(channel.edited) == 1  # edited despite DND


async def test_refresh_isolated_per_user(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_en="water", unit_en="ml")])
    owner_channel = FakeChannel()
    member_channel = FakeChannel()
    await _enable(db, config, owner_channel, OWNER)
    await _enable(db, config, member_channel, MEMBER)
    owner_channel.edited.clear()
    member_channel.edited.clear()

    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "water", 400.0, None, "400ml", "reply"))
    await dashboard.refresh(db, owner_channel, config, registry, OWNER, clock=_fixed_clock())
    await dashboard.refresh(db, member_channel, config, registry, MEMBER, clock=_fixed_clock())

    assert "400" in owner_channel.edited[-1][2]
    assert "400" not in member_channel.edited[-1][2]  # MEMBER's own board, unaffected


async def test_refresh_respects_the_users_own_language_preference(db, config):
    from habit_assistant.core.preferences import execute_lang

    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_en="water", label_th="น้ำ", unit_en="ml", unit_th="มล.")])
    channel = FakeChannel()
    await _enable(db, config, channel, OWNER)
    await execute_lang(commands.dispatch("/lang th", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)
    channel.edited.clear()

    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "water", 500.0, None, "500ml", "reply"))
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())

    assert i18n.detect_language(channel.edited[-1][2]) == "th"
