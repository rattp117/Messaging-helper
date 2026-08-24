"""SPEC-v1.6.md §4 Feature 5, "Almost there" end-of-day nudge (module
`nudge`, R-N1-R-N3): `core/nudge.py`'s `build_nudge_message`/`run_due_nudges`.

Owned ACs (SPEC-v1.6.md §11): AC-N1 (close, once/day), AC-N2 (opt-in + DND),
AC-N3 (registry-generic + bilingual).

Mirrors `tests/test_checkins.py`'s own conventions closely (a `FakeChannel`,
an injectable `clock`, a real on-disk SQLite `Database`, no mocks for the
DB) since `core/nudge.py` mirrors `core/checkins.py`'s own module shape."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import checkins, commands, nudge, i18n
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner-chat"
MEMBER = "member-chat-b"

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    async def run(self, on_message: Callable[[str, str], Awaitable[None]], on_callback=None) -> None:
        raise NotImplementedError

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
    database = Database(tmp_path / "nudge.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(MEMBER, role="member", status="active")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


def _fixed_clock(y=2026, m=8, d=24, hh=20, mm=0):
    return lambda: datetime(y, m, d, hh, mm, 0)


async def _enable_checkin(db, config, user_id: str, window: str | None = None) -> None:
    tail = "on" if window is None else window
    await checkins.execute_checkin(
        commands.dispatch(f"/checkin {tail}", DEFAULT_REGISTRY), db=db, config=config, lang="en", user_id=user_id
    )


def _log(db, user_id: str, habit_id: str, value: float, ts: str = "2026-08-24T09:00:00") -> None:
    db.insert_log(LogEntry(None, user_id, ts, habit_id, value, None, f"{value}", "reply"))


# Deliberately NOT id "water" -- `targets.config_goal` special-cases that
# exact id to the legacy `config.reminders.water.goal_ml` (SPEC-v0.7.md
# R15/`core/targets.py:config_goal`), which would silently override this
# constant's own `goal=1000.0` in every test below. Any non-builtin id
# reads its goal straight from `habit.goal` as normal.
JUICE = HabitRegistry([_habit("juice", "numeric", goal=1000.0, label_en="juice", label_th="น้ำผลไม้", unit_en="ml", unit_th="มล.")])


# ===========================================================================
# zero-LLM proof (AC-N3 posture, mirrors test_checkins.py's own technique)
# ===========================================================================


def test_nudge_module_never_imports_or_calls_an_llm():
    assert "habit_assistant.llm" not in inspect.getsource(nudge)
    for fn in (nudge.build_nudge_message, nudge.run_due_nudges):
        assert "llm" not in inspect.signature(fn).parameters


# ===========================================================================
# build_nudge_message -- threshold boundaries (AC-N1), goal-less habits
# (AC-N3/R-N3), target-override (R-N1), bilingual (AC-N3), isolation.
# ===========================================================================


def test_threshold_boundary_79_percent_is_not_close(db, config):
    _log(db, OWNER, "juice", 790.0)  # 79% of 1000, threshold 80
    assert nudge.build_nudge_message(db, config, JUICE, "en", OWNER, clock=_fixed_clock()) is None


def test_threshold_boundary_80_percent_is_close(db, config):
    _log(db, OWNER, "juice", 800.0)  # exactly 80% -- inclusive boundary
    message = nudge.build_nudge_message(db, config, JUICE, "en", OWNER, clock=_fixed_clock())
    assert message is not None
    assert "200" in message  # remaining = 1000 - 800


def test_threshold_boundary_99_percent_is_close(db, config):
    _log(db, OWNER, "juice", 990.0)
    message = nudge.build_nudge_message(db, config, JUICE, "en", OWNER, clock=_fixed_clock())
    assert message is not None
    assert "10" in message  # remaining = 1000 - 990


def test_threshold_boundary_100_percent_is_already_met_not_close(db, config):
    _log(db, OWNER, "juice", 1000.0)
    assert nudge.build_nudge_message(db, config, JUICE, "en", OWNER, clock=_fixed_clock()) is None


def test_far_from_goal_is_not_close(db, config):
    _log(db, OWNER, "juice", 100.0)  # 10% of goal
    assert nudge.build_nudge_message(db, config, JUICE, "en", OWNER, clock=_fixed_clock()) is None


def test_no_log_at_all_today_is_not_close(db, config):
    assert nudge.build_nudge_message(db, config, JUICE, "en", OWNER, clock=_fixed_clock()) is None


def test_configurable_threshold_pct_is_honored(db):
    config = Config.model_validate({"nudge": {"threshold_pct": 90, "time": "20:00"}})
    _log(db, OWNER, "juice", 850.0)  # 85% -- close at the default 80% but NOT at a configured 90%
    assert nudge.build_nudge_message(db, config, JUICE, "en", OWNER, clock=_fixed_clock()) is None

    _log(db, OWNER, "juice", 60.0, ts="2026-08-24T10:00:00")  # brings total to 910 = 91%
    assert nudge.build_nudge_message(db, config, JUICE, "en", OWNER, clock=_fixed_clock()) is not None


def test_goal_less_habits_never_nudge(db, config):
    """R-N3: a habit with no configured goal and no /target override is
    never goal-bearing -- `targets.effective_goal` returns None, so it
    never contributes to the nudge, no special-casing needed."""
    registry = HabitRegistry([_habit("stretch", "numeric", goal=None, label_en="stretch", label_th="ยืดเหยียด", unit_en="min", unit_th="นาที")])
    _log(db, OWNER, "stretch", 999.0)
    assert nudge.build_nudge_message(db, config, registry, "en", OWNER, clock=_fixed_clock()) is None


def test_boolean_and_text_habits_never_nudge(db, config):
    registry = HabitRegistry(
        [
            _habit("diary", "text", label_en="diary", label_th="ไดอารี่", unit_en=None, unit_th=None),
            _habit("meditate", "boolean", label_en="meditate", label_th="นั่งสมาธิ", unit_en=None, unit_th=None),
        ]
    )
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T09:00:00", "diary", None, "today was fine", "diary entry", "reply"))
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T09:00:00", "meditate", 1.0, None, "meditated", "reply"))
    assert nudge.build_nudge_message(db, config, registry, "en", OWNER, clock=_fixed_clock()) is None


def test_target_override_is_respected_over_the_config_default(db, config):
    """R-N1: a per-user `/target` override changes what "close" means --
    the effective goal, not the config default."""
    db.set_target(OWNER, "water", 500.0)  # override: config default is 2500 (Config().reminders.water.goal_ml)
    _log(db, OWNER, "water", 420.0)  # 84% of the OVERRIDDEN 500 goal, far from the config default's 2500

    message = nudge.build_nudge_message(db, config, DEFAULT_REGISTRY, "en", OWNER, clock=_fixed_clock())
    assert message is not None
    assert "80" in message  # remaining = 500 - 420


def test_multi_habit_close_folds_into_a_single_message(db, config):
    registry = HabitRegistry(
        [
            _habit("juice", "numeric", goal=1000.0, label_en="juice", label_th="น้ำผลไม้", unit_en="ml", unit_th="มล."),
            _habit("pushups", "numeric", goal=50.0, label_en="pushups", label_th="วิดพื้น", unit_en="reps", unit_th="ครั้ง"),
            _habit("stretch", "numeric", goal=30.0, label_en="stretch", label_th="ยืดเหยียด", unit_en="min", unit_th="นาที"),
        ]
    )
    _log(db, OWNER, "juice", 900.0)  # 90% -- close
    _log(db, OWNER, "pushups", 45.0)  # 90% -- close
    _log(db, OWNER, "stretch", 5.0)  # far -- not close

    message = nudge.build_nudge_message(db, config, registry, "en", OWNER, clock=_fixed_clock())
    assert message is not None
    assert message.count("\n") == 2  # one header + exactly two habit lines -- a SINGLE message
    assert "juice" in message
    assert "pushups" in message
    assert "stretch" not in message


def test_all_habits_met_or_far_yields_no_message(db, config):
    registry = HabitRegistry(
        [
            _habit("juice", "numeric", goal=1000.0, label_en="juice", label_th="น้ำผลไม้", unit_en="ml", unit_th="มล."),
            _habit("pushups", "numeric", goal=50.0, label_en="pushups", label_th="วิดพื้น", unit_en="reps", unit_th="ครั้ง"),
        ]
    )
    _log(db, OWNER, "juice", 1000.0)  # met
    _log(db, OWNER, "pushups", 5.0)  # far

    assert nudge.build_nudge_message(db, config, registry, "en", OWNER, clock=_fixed_clock()) is None


def test_bilingual(db, config):
    _log(db, OWNER, "juice", 800.0)
    en_message = nudge.build_nudge_message(db, config, JUICE, "en", OWNER, clock=_fixed_clock())
    th_message = nudge.build_nudge_message(db, config, JUICE, "th", OWNER, clock=_fixed_clock())
    assert en_message != th_message
    assert i18n.detect_language(th_message) == "th"


def test_isolated_per_user(db, config):
    _log(db, OWNER, "juice", 800.0)  # close
    _log(db, MEMBER, "juice", 100.0)  # far

    owner_message = nudge.build_nudge_message(db, config, JUICE, "en", OWNER, clock=_fixed_clock())
    member_message = nudge.build_nudge_message(db, config, JUICE, "en", MEMBER, clock=_fixed_clock())
    assert owner_message is not None
    assert member_message is None


# ===========================================================================
# run_due_nudges -- firing discipline (AC-N1), enablement (AC-N2), DND
# (AC-N2), config time + tz honored, isolation (AC-N3).
# ===========================================================================


async def test_fires_at_exactly_the_configured_time(db, config):
    await _enable_checkin(db, config, OWNER)
    _log(db, OWNER, "water", 2000.0)  # 80% of the default 2500ml goal

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))
    assert channel.sent_to(OWNER) != []


async def test_does_not_fire_a_minute_before_or_after(db, config):
    await _enable_checkin(db, config, OWNER)
    _log(db, OWNER, "water", 2000.0)  # close to the default 2500ml goal (80%)

    before = FakeChannel()
    await nudge.run_due_nudges(before, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=19, mm=59))
    assert before.sent_to(OWNER) == []

    after = FakeChannel()
    await nudge.run_due_nudges(after, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=1))
    assert after.sent_to(OWNER) == []


async def test_configured_nudge_time_is_honored_not_hardcoded(db):
    config = Config.model_validate({"nudge": {"threshold_pct": 80, "time": "21:30"}, "checkin": {"enabled": False}})
    await _enable_checkin(db, config, OWNER)
    _log(db, OWNER, "water", 2000.0)

    at_default_time = FakeChannel()
    await nudge.run_due_nudges(at_default_time, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))
    assert at_default_time.sent_to(OWNER) == []  # 20:00 is no longer the configured time

    at_configured_time = FakeChannel()
    await nudge.run_due_nudges(at_configured_time, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=21, mm=30))
    assert at_configured_time.sent_to(OWNER) != []


async def test_timezone_aware_clock_is_converted_to_app_timezone(db, config):
    """config.app.timezone defaults to Asia/Bangkok (UTC+7, no DST) --
    13:00 UTC is 20:00 Bangkok time, the configured nudge time."""
    await _enable_checkin(db, config, OWNER)
    _log(db, OWNER, "water", 2000.0)

    utc_clock = lambda: datetime(2026, 8, 24, 13, 0, 0, tzinfo=timezone.utc)
    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=utc_clock)
    assert channel.sent_to(OWNER) != []


async def test_checkin_off_by_default_means_no_nudge(db, config):
    """AC-N2: nobody has run /checkin on -- the default (checkins off)
    means no nudge, even for a squarely-close habit at the exact
    configured time."""
    _log(db, OWNER, "water", 2000.0)  # 80% of the default 2500ml goal

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))
    assert channel.sent == []


async def test_checkin_on_makes_the_user_nudge_eligible(db, config):
    await _enable_checkin(db, config, OWNER)
    _log(db, OWNER, "water", 2000.0)

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))
    assert channel.sent_to(OWNER) != []


async def test_checkin_explicitly_off_means_no_nudge(db, config):
    await _enable_checkin(db, config, OWNER)
    await _enable_checkin(db, config, OWNER, window="off")
    _log(db, OWNER, "water", 2000.0)

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))
    assert channel.sent_to(OWNER) == []


async def test_enablement_rides_checkin_on_off_regardless_of_the_checkin_window(db, config):
    """OQ2 RESOLVED: nudge enablement is gated on check-ins being ON at
    all -- NOT on whether "now" happens to also fall inside the user's own
    check-in WINDOW (a separate, unrelated setting). A narrow window that
    doesn't cover 20:00 must not suppress the 20:00 nudge."""
    await _enable_checkin(db, config, OWNER, window="01:00-02:00")  # never covers 20:00
    _log(db, OWNER, "water", 2000.0)

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))
    assert channel.sent_to(OWNER) != []


async def test_dnd_suppresses_a_due_nudge(db, config):
    await _enable_checkin(db, config, OWNER)
    _log(db, OWNER, "water", 2000.0)
    db.set_user_quiet_hours(OWNER, '[["00:00", "12:00"], ["12:00", "00:00"]]')  # always-DND, full-day coverage

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))
    assert channel.sent_to(OWNER) == []


async def test_dnd_is_scoped_to_the_user_in_it(db, config):
    await _enable_checkin(db, config, OWNER)
    await _enable_checkin(db, config, MEMBER)
    _log(db, OWNER, "water", 2000.0)
    _log(db, MEMBER, "water", 2000.0)
    db.set_user_quiet_hours(OWNER, '[["00:00", "12:00"], ["12:00", "00:00"]]')  # OWNER always-DND

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))
    assert channel.sent_to(OWNER) == []
    assert channel.sent_to(MEMBER) != []


async def test_no_message_when_nothing_qualifies(db, config):
    """Non-nagging: enabled, not-DND, at the exact configured time -- but
    every habit is already met or far -- yields silence, not a message."""
    await _enable_checkin(db, config, OWNER)
    _log(db, OWNER, "water", 2500.0)  # met (default goal 2500ml)

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))
    assert channel.sent_to(OWNER) == []


async def test_at_most_one_nudge_message_per_user_at_the_fixed_minute(db, config):
    from habit_assistant.core.preferences import execute_lang

    registry = HabitRegistry(
        [
            _habit("juice", "numeric", goal=1000.0, label_en="juice", label_th="น้ำผลไม้", unit_en="ml", unit_th="มล."),
            _habit("pushups", "numeric", goal=50.0, label_en="pushups", label_th="วิดพื้น", unit_en="reps", unit_th="ครั้ง"),
        ]
    )
    await _enable_checkin(db, config, OWNER)
    await execute_lang(commands.dispatch("/lang en", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)
    _log(db, OWNER, "juice", 900.0)
    _log(db, OWNER, "pushups", 45.0)

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, registry, db, clock=_fixed_clock(hh=20, mm=0))
    sent = channel.sent_to(OWNER)
    assert len(sent) == 1  # both close habits folded into ONE send, not two
    assert "juice" in sent[0] and "pushups" in sent[0]


async def test_isolation_a_disabled_far_or_dnd_user_never_leaks_into_an_enabled_users_send(db, config):
    await _enable_checkin(db, config, OWNER)
    _log(db, OWNER, "water", 400.0, ts="2026-08-24T07:00:00")  # far from goal alone...
    _log(db, OWNER, "water", 2000.0, ts="2026-08-24T08:00:00")  # ...total 2400/2500 = 96%, close
    _log(db, MEMBER, "water", 9999.0)  # MEMBER never opted in

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))

    assert channel.sent_to(MEMBER) == []
    owner_sent = channel.sent_to(OWNER)
    assert len(owner_sent) == 1
    assert "9999" not in owner_sent[0]


async def test_run_due_nudges_respects_each_users_own_language_preference(db, config):
    from habit_assistant.core.preferences import execute_lang

    await _enable_checkin(db, config, OWNER)
    await execute_lang(commands.dispatch("/lang th", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)
    _log(db, OWNER, "water", 2000.0)

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))
    sent = channel.sent_to(OWNER)
    assert len(sent) == 1
    assert i18n.detect_language(sent[0]) == "th"


async def test_run_due_nudges_never_calls_ollama_end_to_end(db, config):
    await _enable_checkin(db, config, OWNER)
    _log(db, OWNER, "water", 2000.0)

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))
    assert channel.sent_to(OWNER) != []


# ===========================================================================
# Interplay: the nudge minute coincides with a check-in hour. Neither tick
# suppresses the other -- SPEC-v1.6.md R-N1 says the nudge "runs on the SAME
# minutely job as the check-in/reminder ticks" (i.e. alongside them, wired
# as an independent sibling call, exactly like run_due_checkins is already
# a sibling of run_due_reminders on that same job) -- it does not say the
# nudge REPLACES or is suppressed by a coinciding check-in tick. With the
# default check-in window (08:00-20:00, inclusive end -- R-K2) and the
# default nudge time (20:00), 20:00 is the one minute both ticks are
# simultaneously due for an enabled user: both fire, independently,
# producing two separate messages -- a check-in progress ping AND the
# end-of-day nudge -- since they are two distinct calls in main.py's
# minutely job, not a single merged send.
# ===========================================================================


async def test_nudge_and_checkin_both_fire_independently_when_due_at_the_same_minute(db, config):
    from habit_assistant.core.preferences import execute_lang

    await _enable_checkin(db, config, OWNER)  # default window 08:00-20:00, covers 20:00 inclusively
    await execute_lang(commands.dispatch("/lang en", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)
    _log(db, OWNER, "water", 2000.0)  # 80% of 2500 -- unmet (checkin) AND close (nudge)

    channel = FakeChannel()
    clock = _fixed_clock(hh=20, mm=0)
    await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=clock)
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=clock)

    sent = channel.sent_to(OWNER)
    assert len(sent) == 2  # neither tick suppressed the other
    assert any(i18n.t("checkin_header", "en") in msg for msg in sent)
    assert any(i18n.t("nudge_header", "en") in msg for msg in sent)


async def test_nudge_still_fires_when_the_checkin_window_excludes_20_00(db, config):
    """The interplay is about the MINUTE coinciding, not the window --
    proven separately above (enablement rides on/off only) but re-asserted
    here in combination with a real check-in tick call at the same minute:
    a narrow check-in window that EXCLUDES 20:00 means the check-in tick
    itself stays silent at 20:00, while the nudge (gated on enablement
    only) still fires."""
    from habit_assistant.core.preferences import execute_lang

    await _enable_checkin(db, config, OWNER, window="08:00-09:00")  # excludes 20:00
    await execute_lang(commands.dispatch("/lang en", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)
    _log(db, OWNER, "water", 2000.0)

    channel = FakeChannel()
    clock = _fixed_clock(hh=20, mm=0)
    await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=clock)
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=clock)

    sent = channel.sent_to(OWNER)
    assert len(sent) == 1
    assert i18n.t("nudge_header", "en") in sent[0]
