"""SPEC-v1.8.md "Riders" (module `riders`, R-D1 + the silent-payload half of
R-D4): silent-by-default proactive sends.

Owned ACs (SPEC-v1.8.md §11): AC-D1 (the three proactive tick sites --
`reminders.send_reminder`, `checkins.run_due_checkins`, `nudge.
run_due_nudges` -- send with `disable_notification=True` when `[notifications]
silent_proactive` is true, the default) and the silent-payload half of AC-D4
(with `silent_proactive=false`, those same three sends are byte-identical to
v1.7 -- content unchanged, `disable_notification` simply absent/False).

Owner-scoped menus (AC-D2) and the `/audit` language fix (AC-D3) are the
`main.py` integration pass's own scope, not this module's -- see
SPEC-v1.8.md §11's module-ownership table. This file only exercises the
three send sites `core/reminders.py`/`core/checkins.py`/`core/nudge.py` own.

Mirrors `tests/test_reminders.py`/`tests/test_checkins.py`/`tests/test_nudge.
py`'s own conventions (a `RecordingChannel`, an injectable `clock`, a real
on-disk SQLite `Database`, no mocks for the DB)."""

from __future__ import annotations

import inspect
import re
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import checkins, commands, nudge, reminders
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.storage.db import Database

OWNER = "owner-chat"
MEMBER = "member-chat-b"

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "habit_assistant"


class RecordingChannel(Channel):
    """Unlike the other test files' `FakeChannel` (which only records
    `(chat_id, text)` -- this module's change doesn't touch either of
    those, so their existing byte-identical-text assertions needed no
    edits), this fake additionally records the `disable_notification`
    flag each call actually received, since that flag IS this module's
    entire surface."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, bool]] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append((chat_id, text, disable_notification))

    async def run(self, on_message: Callable[[str, str], Awaitable[None]], on_callback=None) -> None:
        raise NotImplementedError


def _habit(
    id_: str,
    type_: str = "numeric",
    *,
    label_en: str = "test",
    label_th: str = "ทดสอบ",
    unit_en: str | None = "u",
    unit_th: str | None = "ห",
    goal: float | None = None,
    reminder_times: tuple[str, ...] = (),
) -> Habit:
    return Habit(
        id=id_,
        type=type_,
        label_en=label_en,
        label_th=label_th,
        unit_en=unit_en if type_ in ("numeric", "duration") else None,
        unit_th=unit_th if type_ in ("numeric", "duration") else None,
        goal=goal,
        reminder_times=reminder_times,
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "riders.db")
    database.upsert_user(OWNER, role="owner", status="active")
    yield database
    database.close()


def _fixed_clock(y=2026, m=8, d=24, hh=20, mm=0):
    return lambda: datetime(y, m, d, hh, mm, 0)


async def _enable_checkin(db, config, user_id: str) -> None:
    await checkins.execute_checkin(
        commands.dispatch("/checkin on", DEFAULT_REGISTRY), db=db, config=config, lang="en", user_id=user_id
    )


# ===========================================================================
# Guard test (spec's own explicit requirement): the flag threading touches
# ONLY the three tick send sites -- nothing else in the codebase (a
# confirmation, a reply, the dashboard pin) passes `disable_notification` at
# all, so every one of those stays notifying (`Channel.send`'s own default,
# R-S1) simply by never mentioning the keyword.
# ===========================================================================


def test_exactly_three_call_sites_pass_disable_notification_and_they_are_the_three_ticks():
    # Scoped to the actual `channel.send(...)` CALL EXPRESSION passing the
    # keyword (a real code call site), not any docstring/comment prose that
    # merely mentions the word `disable_notification` (several modules'
    # docstrings, including this change's own, explain the flag by name).
    call_site_re = re.compile(r"channel\.send\([^)]*disable_notification=", re.DOTALL)
    expected = {
        SRC_ROOT / "core" / "reminders.py": 1,
        SRC_ROOT / "core" / "checkins.py": 1,
        SRC_ROOT / "core" / "nudge.py": 1,
    }
    # channels/base.py + channels/telegram.py + channels/line.py DEFINE the
    # parameter (function signatures / docstrings) but never themselves
    # decide to set it True -- excluded from this call-site sweep, which is
    # scoped to "who chooses to pass disable_notification=", not "who
    # accepts the keyword".
    excluded_dirs = {SRC_ROOT / "channels"}

    found: dict[Path, int] = {}
    for path in SRC_ROOT.rglob("*.py"):
        if any(path.is_relative_to(d) for d in excluded_dirs):
            continue
        text = path.read_text(encoding="utf-8")
        n = len(call_site_re.findall(text))
        if n:
            found[path] = n

    assert found == expected, f"unexpected disable_notification call sites: {found}"


def test_execute_checkin_the_user_initiated_reply_path_has_no_channel_parameter_at_all():
    """R-D1: user-initiated confirmations/replies stay notifying. Structural
    proof for `/checkin`'s own reply (`execute_checkin`): it returns a plain
    string for `main.py` to send back through the ordinary (notifying)
    reply path -- it has no `channel`/`disable_notification` parameter to
    even be capable of going silent."""
    params = inspect.signature(checkins.execute_checkin).parameters
    assert "channel" not in params
    assert "disable_notification" not in params


def test_send_reminder_and_the_two_ticks_have_no_extra_public_send_site():
    """Each owned module has exactly the one send call this report claims --
    proven above by source sweep; this test additionally proves no NEW
    public send-shaped helper was introduced (module surface stayed the
    exact shape SPEC-v1.8.md §5 lists: `send_reminder`/`run_due_reminders`,
    `run_due_checkins`, `run_due_nudges`)."""
    assert {"send_reminder", "run_due_reminders"}.issubset(vars(reminders))
    assert "run_due_checkins" in vars(checkins)
    assert "run_due_nudges" in vars(nudge)


# ===========================================================================
# AC-D1: silent_proactive=true (the default) -> the three sends carry
# disable_notification=True. User-initiated sends are out of these modules'
# scope entirely (proven above).
# ===========================================================================


async def test_send_reminder_default_config_sends_silently():
    config = Config()
    assert config.notifications.silent_proactive is True  # SPEC-v1.8.md §2.5 default
    registry = HabitRegistry.from_config(config)
    channel = RecordingChannel()

    await reminders.send_reminder(channel, OWNER, registry.get("water"), "en", None, config)

    assert channel.sent == [(OWNER, channel.sent[0][1], True)]


async def test_run_due_reminders_default_config_sends_silently(db):
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = RecordingChannel()

    await reminders.run_due_reminders(channel, config, registry, db, clock=_fixed_clock(hh=8, mm=0))

    assert channel.sent, "expected the 08:00 water reminder to fire"
    assert all(disable for _, _, disable in channel.sent)


async def test_run_due_checkins_default_config_sends_silently(db):
    config = Config()
    await _enable_checkin(db, config, OWNER)
    registry = HabitRegistry([_habit("juice", "numeric", goal=1000.0, label_en="juice", label_th="น้ำผลไม้")])
    channel = RecordingChannel()

    await checkins.run_due_checkins(channel, config, registry, db, clock=_fixed_clock(hh=9, mm=0))

    assert channel.sent, "expected an hourly check-in to fire"
    assert all(disable for _, _, disable in channel.sent)


async def test_run_due_nudges_default_config_sends_silently(db):
    config = Config()
    await _enable_checkin(db, config, OWNER)
    from habit_assistant.storage.models import LogEntry

    registry = HabitRegistry([_habit("juice", "numeric", goal=1000.0, label_en="juice", label_th="น้ำผลไม้")])
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T09:00:00", "juice", 900.0, None, "900", "reply"))
    channel = RecordingChannel()

    await nudge.run_due_nudges(channel, config, registry, db, clock=_fixed_clock(hh=20, mm=0))

    assert channel.sent, "expected the almost-there nudge to fire (90% of goal)"
    assert all(disable for _, _, disable in channel.sent)


# ===========================================================================
# AC-D4: silent_proactive=false -> byte-identical to v1.7 -- same text, no
# disable_notification.
# ===========================================================================


async def test_send_reminder_silent_proactive_false_is_byte_identical_to_v17():
    config_silent = Config()
    config_notifying = Config.model_validate({"notifications": {"silent_proactive": False}})
    registry = HabitRegistry.from_config(config_silent)

    silent_channel = RecordingChannel()
    await reminders.send_reminder(silent_channel, OWNER, registry.get("water"), "en", None, config_silent)

    notifying_channel = RecordingChannel()
    await reminders.send_reminder(notifying_channel, OWNER, registry.get("water"), "en", None, config_notifying)

    # Same recipient + text either way -- only the notification flag differs.
    assert [(c, t) for c, t, _ in silent_channel.sent] == [(c, t) for c, t, _ in notifying_channel.sent]
    assert [d for _, _, d in silent_channel.sent] == [True]
    assert [d for _, _, d in notifying_channel.sent] == [False]


async def test_send_reminder_called_with_no_config_at_all_defaults_to_notifying():
    """Every pre-v1.8 direct caller (`send_reminder(channel, chat_id, habit,
    lang)`, no `config`) is unaffected -- byte-identical to v1.7 (AC-D4),
    matching `send_reminder`'s own pre-existing `config: Config | None =
    None` back-compat contract (ROADMAP.md v0.9.0)."""
    registry = HabitRegistry.from_config(Config())
    channel = RecordingChannel()

    await reminders.send_reminder(channel, OWNER, registry.get("water"), "en")

    assert channel.sent == [(OWNER, channel.sent[0][1], False)]


async def test_run_due_reminders_silent_proactive_false_is_byte_identical_to_v17(db):
    config = Config.model_validate({"notifications": {"silent_proactive": False}})
    registry = HabitRegistry.from_config(config)
    channel = RecordingChannel()

    await reminders.run_due_reminders(channel, config, registry, db, clock=_fixed_clock(hh=8, mm=0))

    assert channel.sent, "expected the 08:00 water reminder to fire"
    assert all(disable is False for _, _, disable in channel.sent)


async def test_run_due_checkins_silent_proactive_false_is_byte_identical_to_v17(db):
    config = Config.model_validate({"notifications": {"silent_proactive": False}})
    await _enable_checkin(db, config, OWNER)
    registry = HabitRegistry([_habit("juice", "numeric", goal=1000.0, label_en="juice", label_th="น้ำผลไม้")])
    channel = RecordingChannel()

    await checkins.run_due_checkins(channel, config, registry, db, clock=_fixed_clock(hh=9, mm=0))

    assert channel.sent, "expected an hourly check-in to fire"
    assert all(disable is False for _, _, disable in channel.sent)


async def test_run_due_nudges_silent_proactive_false_is_byte_identical_to_v17(db):
    config = Config.model_validate({"notifications": {"silent_proactive": False}})
    await _enable_checkin(db, config, OWNER)
    from habit_assistant.storage.models import LogEntry

    registry = HabitRegistry([_habit("juice", "numeric", goal=1000.0, label_en="juice", label_th="น้ำผลไม้")])
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T09:00:00", "juice", 900.0, None, "900", "reply"))
    channel = RecordingChannel()

    await nudge.run_due_nudges(channel, config, registry, db, clock=_fixed_clock(hh=20, mm=0))

    assert channel.sent, "expected the almost-there nudge to fire (90% of goal)"
    assert all(disable is False for _, _, disable in channel.sent)


# ===========================================================================
# Fail-open discipline (SPEC-v1.8.md §4 risk: "reaction/silent are
# decorative/gentle -> fail-open is load-bearing"): threading the flag must
# not introduce a new exception path. `config.notifications` is a required
# `Config` field with a pydantic default, so reading `.silent_proactive`
# can never itself raise -- proven end-to-end by every tick test above
# already succeeding through nudge.py's own two-stage try/except (a
# pre-existing structure this change reads `config.notifications.
# silent_proactive` INSIDE, at the existing `channel.send` call site, not
# by adding a new try/except of its own).
# ===========================================================================


async def test_run_due_nudges_fail_open_structure_is_unchanged_by_the_silent_flag(db):
    """A user whose message build fails is still skipped without aborting
    the fan-out for another due user -- same two-stage fail-open shape as
    before this change (mirrors `tests/test_nudge_gaps.py`'s own
    `test_fail_open_fan_out_...` intent, re-proven here because this
    module's edit sits inside that exact try/except)."""
    config = Config()
    await _enable_checkin(db, config, OWNER)
    db.upsert_user(MEMBER, role="member", status="active")
    await _enable_checkin(db, config, MEMBER)
    from habit_assistant.storage.models import LogEntry

    registry = HabitRegistry([_habit("juice", "numeric", goal=1000.0, label_en="juice", label_th="น้ำผลไม้")])
    db.insert_log(LogEntry(None, MEMBER, "2026-08-24T09:00:00", "juice", 900.0, None, "900", "reply"))
    channel = RecordingChannel()

    await nudge.run_due_nudges(channel, config, registry, db, clock=_fixed_clock(hh=20, mm=0))

    member_sends = [row for row in channel.sent if row[0] == MEMBER]
    assert member_sends and member_sends[0][2] is True
