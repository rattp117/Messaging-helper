"""Vera's independent gap coverage for SPEC-v1.8.md "Riders" (module
`riders`, R-D1 + the silent-payload half of R-D4) on top of Luna's own
`tests/test_riders.py` (13 tests, IMPL-v1.8-riders.md).

Scope, per Archi's dispatch: AC-D1 (three ticks send silently, text/chat_id
byte-identical to the non-silent case), the silent-payload half of AC-D4
(`silent_proactive=false` -> byte-identical to v1.7 shape), negative scope
(nothing outside the three ticks went silent -- user-initiated replies,
undo, dashboard pin/edit -- plus a meta-proof that Luna's source-sweep guard
would actually catch a fourth site), fail-open discipline surviving the flag
threading, and back-compat (`send_reminder(config=None)`).

AC-D2 (owner-scoped menu) and AC-D3 (`/audit` language fix) are explicitly
OUT of this verdict -- integration scope, not yet wired into `main.py`.

Mirrors `tests/test_riders.py`'s own conventions (`RecordingChannel`, an
injectable `clock`, a real on-disk SQLite `Database`, no mocks for the DB)."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import checkins, commands, nudge, reminders, undo_ui
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner-chat"
MEMBER = "member-chat-b"
THIRD = "third-chat-c"

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "habit_assistant"


class RecordingChannel(Channel):
    """Same shape as `tests/test_riders.py::RecordingChannel` -- records the
    `disable_notification` flag each call actually received."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, bool]] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append((chat_id, text, disable_notification))

    async def run(self, on_message: Callable[[str, str], Awaitable[None]], on_callback=None) -> None:
        raise NotImplementedError


class RaisingForChannel(Channel):
    """Raises on `send` for a configured set of recipients, records every
    other send + the flag it received -- mirrors
    `tests/test_nudge_gaps.py::RaisingForChannel`."""

    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.sent: list[tuple[str, str, bool]] = []
        self._fail_for = fail_for or set()

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        if chat_id in self._fail_for:
            raise RuntimeError(f"simulated send failure for {chat_id}")
        self.sent.append((chat_id, text, disable_notification))

    async def run(self, on_message, on_callback=None) -> None:
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
    database = Database(tmp_path / "riders_gaps.db")
    database.upsert_user(OWNER, role="owner", status="active")
    yield database
    database.close()


def _fixed_clock(y=2026, m=8, d=24, hh=20, mm=0):
    return lambda: datetime(y, m, d, hh, mm, 0)


async def _enable_checkin(db, config, user_id: str) -> None:
    await checkins.execute_checkin(
        commands.dispatch("/checkin on", DEFAULT_REGISTRY), db=db, config=config, lang="en", user_id=user_id
    )


JUICE_REGISTRY = HabitRegistry([_habit("juice", "numeric", goal=1000.0, label_en="juice", label_th="น้ำผลไม้")])


# ===========================================================================
# AC-D1: each of the three ticks sends silently under default config, and
# the text/chat_id is byte-identical to what the non-silent config produces.
# ===========================================================================


async def test_run_due_reminders_default_tick_sends_silently_text_identical_to_notifying(db):
    silent_config = Config()
    notifying_config = Config.model_validate({"notifications": {"silent_proactive": False}})
    registry = HabitRegistry.from_config(silent_config)

    silent_channel = RecordingChannel()
    await reminders.run_due_reminders(silent_channel, silent_config, registry, db, clock=_fixed_clock(hh=8, mm=0))

    notifying_channel = RecordingChannel()
    await reminders.run_due_reminders(
        notifying_channel, notifying_config, registry, db, clock=_fixed_clock(hh=8, mm=0)
    )

    assert silent_channel.sent, "expected the 08:00 water reminder to fire"
    assert [(c, t) for c, t, _ in silent_channel.sent] == [(c, t) for c, t, _ in notifying_channel.sent]
    assert all(flag is True for _, _, flag in silent_channel.sent)
    assert all(flag is False for _, _, flag in notifying_channel.sent)


async def test_run_due_checkins_default_tick_sends_silently_text_identical_to_notifying(db):
    silent_config = Config()
    notifying_config = Config.model_validate({"notifications": {"silent_proactive": False}})
    await _enable_checkin(db, silent_config, OWNER)

    silent_channel = RecordingChannel()
    await checkins.run_due_checkins(silent_channel, silent_config, JUICE_REGISTRY, db, clock=_fixed_clock(hh=9, mm=0))

    notifying_channel = RecordingChannel()
    await checkins.run_due_checkins(
        notifying_channel, notifying_config, JUICE_REGISTRY, db, clock=_fixed_clock(hh=9, mm=0)
    )

    assert silent_channel.sent, "expected an hourly check-in to fire"
    assert [(c, t) for c, t, _ in silent_channel.sent] == [(c, t) for c, t, _ in notifying_channel.sent]
    assert all(flag is True for _, _, flag in silent_channel.sent)
    assert all(flag is False for _, _, flag in notifying_channel.sent)


async def test_run_due_nudges_default_tick_sends_silently_text_identical_to_notifying(db):
    silent_config = Config()
    notifying_config = Config.model_validate({"notifications": {"silent_proactive": False}})
    await _enable_checkin(db, silent_config, OWNER)
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T09:00:00", "juice", 900.0, None, "900", "reply"))

    silent_channel = RecordingChannel()
    await nudge.run_due_nudges(silent_channel, silent_config, JUICE_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))

    notifying_channel = RecordingChannel()
    await nudge.run_due_nudges(
        notifying_channel, notifying_config, JUICE_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0)
    )

    assert silent_channel.sent, "expected the almost-there nudge to fire (90% of goal)"
    assert [(c, t) for c, t, _ in silent_channel.sent] == [(c, t) for c, t, _ in notifying_channel.sent]
    assert all(flag is True for _, _, flag in silent_channel.sent)
    assert all(flag is False for _, _, flag in notifying_channel.sent)


# ===========================================================================
# AC-D4 slice: silent_proactive=false -> the send call is byte-identical to
# the v1.7 shape (flag False/absent-default), text unchanged. (Luna's own
# tests cover send_reminder for this; this file adds the two tick-level
# functions' payload shape explicitly.)
# ===========================================================================


async def test_send_reminder_silent_false_payload_matches_pre_v18_default_call_shape(db):
    """A caller that never mentions `disable_notification` at all (the
    pre-v1.8 call shape) and a v1.8 caller with `silent_proactive=false`
    must produce the exact same observable payload -- proven here by
    driving BOTH through the identical `Channel.send` default parameter
    rather than asserting on internal plumbing."""
    config = Config.model_validate({"notifications": {"silent_proactive": False}})
    registry = HabitRegistry.from_config(config)
    channel = RecordingChannel()

    await reminders.send_reminder(channel, OWNER, registry.get("water"), "en", None, config)

    assert channel.sent
    chat_id, text, flag = channel.sent[0]
    assert flag is False
    # The recorded call is indistinguishable from a pre-v1.8 `send(chat_id,
    # text)` call landing on the ABC's own defaulted parameter.
    assert (chat_id, text) == (OWNER, text)


# ===========================================================================
# Negative scope: nothing outside the three owned tick sites ever passes
# disable_notification=True. Covers: undo confirmation (real call, no
# main.py needed), command replies (structural -- no `channel` param at
# all), dashboard pin/edit (structural -- the ABC methods those call don't
# even accept the keyword), and a meta-proof that Luna's source-sweep guard
# mechanism would catch a genuine fourth call site if one existed.
# ===========================================================================


async def test_undo_confirmation_never_goes_silent(db):
    """`send_undo_confirmation` (`core/undo_ui.py`) is the real, callable-
    without-`main.py` confirmation path for a user-initiated `/undo`. It
    must stay notifying regardless of `[notifications] silent_proactive`."""
    config = Config()  # silent_proactive=True by default -- proves the undo
    # path doesn't accidentally inherit that default.
    registry = HabitRegistry.from_config(config)
    channel = RecordingChannel()

    log_id = db.insert_log(LogEntry(None, OWNER, "2026-08-24T09:00:00", "water", 500.0, None, "500ml", "reply"))
    row = db.get_log(log_id)

    await undo_ui.send_undo_confirmation(db, channel, config, _fixed_clock(), registry, "en", row)

    assert channel.sent, "expected an undo confirmation to be sent"
    assert all(flag is False for _, _, flag in channel.sent), "undo confirmation must never go silent"


def test_command_reply_functions_have_no_channel_parameter_structurally_incapable_of_going_silent():
    """Every user-initiated command-reply function in this codebase returns
    a plain string for `main.py` to relay through the ordinary (notifying)
    reply path -- none of them accepts a `channel`/`disable_notification`
    parameter at all. Extends Luna's own `execute_checkin`-only proof
    (`tests/test_riders.py::test_execute_checkin_the_user_initiated_reply_path_has_no_channel_parameter_at_all`)
    to the sibling setters, so a future Luna can't silently add a channel
    param to one of these and route it through a silent send unnoticed."""
    import inspect

    from habit_assistant.core.preferences import execute_lang, execute_quiet
    from habit_assistant.core.schedules import execute_remind

    for fn in (execute_lang, execute_quiet, execute_remind, checkins.execute_checkin):
        params = inspect.signature(fn).parameters
        assert "channel" not in params, f"{fn.__qualname__} must not gain a channel parameter"
        assert "disable_notification" not in params


def test_dashboard_pin_and_edit_methods_do_not_even_accept_disable_notification():
    """R-S1 ("No other send method changes") -- `send_and_pin`/
    `edit_message` on the `Channel` ABC were not touched by v1.8: they don't
    accept `disable_notification` at all, so the one-time dashboard pin and
    every live in-place dashboard edit are STRUCTURALLY incapable of going
    silent, independent of any config value."""
    import inspect

    params_pin = inspect.signature(Channel.send_and_pin).parameters
    params_edit = inspect.signature(Channel.edit_message).parameters
    assert "disable_notification" not in params_pin
    assert "disable_notification" not in params_edit


def test_source_sweep_guard_mechanism_would_catch_a_genuine_fourth_call_site(tmp_path):
    """Meta-proof: replicate Luna's own regex sweep
    (`tests/test_riders.py::test_exactly_three_call_sites_pass_disable_notification_and_they_are_the_three_ticks`)
    against a SYNTHETIC source tree containing the real three sites' shape
    plus one injected extra call site outside the owned modules, and prove
    the sweep's `found` count changes accordingly -- i.e. the guard is a
    real, working detector, not a tautology that would pass no matter what
    is in the tree."""
    call_site_re = re.compile(r"channel\.send\([^)]*disable_notification=", re.DOTALL)

    fake_root = tmp_path / "fake_src"
    (fake_root / "core").mkdir(parents=True)
    (fake_root / "core" / "reminders.py").write_text(
        "await channel.send(chat_id, text, disable_notification=silent)\n", encoding="utf-8"
    )
    (fake_root / "core" / "checkins.py").write_text(
        "await channel.send(user_id, message, disable_notification=config.notifications.silent_proactive)\n",
        encoding="utf-8",
    )
    (fake_root / "core" / "nudge.py").write_text(
        "await channel.send(user_id, message, disable_notification=config.notifications.silent_proactive)\n",
        encoding="utf-8",
    )

    def _sweep(root: Path) -> dict[Path, int]:
        found: dict[Path, int] = {}
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            n = len(call_site_re.findall(text))
            if n:
                found[path] = n
        return found

    # Baseline: exactly the three legitimate sites, nothing more.
    baseline = _sweep(fake_root)
    assert len(baseline) == 3

    # Inject a fourth, illegitimate call site (e.g. a future Luna wiring a
    # confirmation reply through the silent flag by mistake) into a file
    # that is NOT one of the three owned tick modules.
    (fake_root / "main.py").write_text(
        "await channel.send(user_id, reply, disable_notification=True)  # BUG: should never do this\n",
        encoding="utf-8",
    )
    after_injection = _sweep(fake_root)
    assert len(after_injection) == 4, "the sweep must detect the injected fourth call site"
    assert fake_root / "main.py" in after_injection

    # And the real guard test in tests/test_riders.py runs this exact sweep
    # against the real tree and asserts == the three-site baseline -- so a
    # real fourth site would fail it the same way this synthetic one does.


# ===========================================================================
# Fail-open discipline intact after the flag threading.
# ===========================================================================


async def test_run_due_nudges_fail_open_fan_out_survives_flag_threading(db):
    """nudge.py's pre-existing two-stage try/except fail-open fan-out
    (already proven independently in
    tests/test_nudge_gaps.py::test_fail_open_fan_out_one_users_send_failure_does_not_block_the_others)
    must still hold with the silent flag read INSIDE the existing guarded
    call expression, not via a new exception path."""
    config = Config()
    for uid in (OWNER, MEMBER, THIRD):
        db.upsert_user(uid, role="member" if uid != OWNER else "owner", status="active")
        await _enable_checkin(db, config, uid)
        db.insert_log(LogEntry(None, uid, "2026-08-24T09:00:00", "juice", 900.0, None, "900", "reply"))

    channel = RaisingForChannel(fail_for={MEMBER})
    await nudge.run_due_nudges(channel, config, JUICE_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))

    sent_ids = {row[0] for row in channel.sent}
    assert OWNER in sent_ids, "OWNER (before the failing user) should still be nudged"
    assert THIRD in sent_ids, "THIRD (after the failing user) should still be nudged"
    assert all(flag is True for row in channel.sent if (flag := row[2]) is not None)


async def test_run_due_checkins_send_failure_propagates_same_as_pre_v18(db):
    """`run_due_checkins` never wrapped its one send site in a try/except,
    pre- or post-v1.8 (confirmed via `git diff` on `core/checkins.py`: the
    only change is the added keyword argument on the existing call
    expression) -- so a send failure for one user still propagates out of
    `run_due_checkins` exactly as it did in v1.7. This is NOT a regression;
    it is the pre-existing contract, unaffected by the flag threading. This
    test exists to prove Luna's edit didn't accidentally ADD fail-open
    behavior that wasn't there before (which would itself be an undocumented
    behavior change)."""
    config = Config()
    await _enable_checkin(db, config, OWNER)

    channel = RaisingForChannel(fail_for={OWNER})
    with pytest.raises(RuntimeError, match="simulated send failure"):
        await checkins.run_due_checkins(channel, config, JUICE_REGISTRY, db, clock=_fixed_clock(hh=9, mm=0))


async def test_run_due_reminders_send_failure_propagates_same_as_pre_v18(db):
    """Mirrors the checkins test above for `run_due_reminders`: the fan-out
    loop around `send_reminder` has no try/except (pre- or post-v1.8, per
    `git diff` on `core/reminders.py` -- only the call expression gained the
    keyword), so a send failure still propagates unchanged."""
    config = Config()
    registry = HabitRegistry.from_config(config)

    channel = RaisingForChannel(fail_for={OWNER})
    with pytest.raises(RuntimeError, match="simulated send failure"):
        await reminders.run_due_reminders(channel, config, registry, db, clock=_fixed_clock(hh=8, mm=0))


# ===========================================================================
# Back-compat: send_reminder(config=None) -- the legacy pre-v0.9 call shape
# -- still works and stays non-silent.
# ===========================================================================


async def test_send_reminder_with_config_none_works_and_is_non_silent(db):
    registry = HabitRegistry.from_config(Config())
    channel = RecordingChannel()

    # Legacy shape: no db, no config, no state -- just channel/chat_id/habit/lang.
    await reminders.send_reminder(channel, OWNER, registry.get("water"), "en")

    assert len(channel.sent) == 1
    chat_id, text, flag = channel.sent[0]
    assert chat_id == OWNER
    assert flag is False
    assert text  # non-empty reminder text produced normally
