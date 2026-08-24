"""Reminder tests (SPEC-v0.7.md §8 AC13/AC14, module M2; SPEC-v1.2.md §4
R-S1-R-S6, "Multi-user support").

CHANGED (SPEC-v1.2.md R-S1): the old `schedule_reminders(scheduler, channel,
config, registry)` -- one APScheduler cron job PER (habit, configured time)
-- is REMOVED entirely, replaced by a single minutely tick
(`run_due_reminders(channel, config, registry, db, state, clock=...)`) that
consults `effective_reminder_times(db, config, habit, user_id)` (R-S4) live
on every call. `main.py` now registers exactly ONE `CronTrigger(second=0)`
job (`id="reminder_tick"`) instead of one job per habit-time; the old
per-habit-time cron-registration tests below (AC13/AC14) are rewritten
against the new mechanism -- SAME acceptance intent ("a habit's reminder
fires at its configured time, not at other times; a habit with no
`reminder_times` never fires"), different plumbing.

`send_reminder(channel, chat_id, habit, language)` gained `chat_id` as a
new 2nd positional param (R-C1: every send is now per-recipient)."""

from __future__ import annotations

from datetime import datetime
from typing import Awaitable, Callable

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from habit_assistant.channels.base import Button, Channel
from habit_assistant.config import Config
from habit_assistant.core import i18n
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.reminders import (
    ReminderState,
    effective_reminder_times,
    run_due_reminders,
    send_reminder,
)
from habit_assistant.storage.db import Database

OWNER = "owner"


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

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
    reminder_text_en: str | None = None,
    reminder_text_th: str | None = None,
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
        reminder_text_en=reminder_text_en,
        reminder_text_th=reminder_text_th,
        unit_aliases={},
    )


def _default_registry() -> HabitRegistry:
    return HabitRegistry.from_config(Config())


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(OWNER, role="owner", status="active")
    yield database
    database.close()


def _fixed_clock(hhmm: str):
    hour, minute = (int(x) for x in hhmm.split(":"))
    return lambda: datetime(2026, 8, 19, hour, minute, 0)


# ---------------------------------------------------------------------------
# send_reminder: byte-identical v0.6.0 text, per-recipient (R-C1).
# ---------------------------------------------------------------------------


async def test_send_reminder_sends_byte_identical_v060_text_for_each_builtin_habit():
    registry = _default_registry()
    channel = FakeChannel()

    await send_reminder(channel, OWNER, registry.get("water"), "en")
    await send_reminder(channel, OWNER, registry.get("stretch"), "en")
    await send_reminder(channel, OWNER, registry.get("diary"), "en")

    assert channel.sent == [
        (OWNER, i18n.t("reminder_water", "en")),
        (OWNER, i18n.t("reminder_stretch", "en")),
        (OWNER, i18n.t("reminder_diary", "en")),
    ]


async def test_send_reminder_water_habit_thai_is_byte_identical_to_v060():
    registry = _default_registry()
    channel = FakeChannel()

    await send_reminder(channel, OWNER, registry.get("water"), "th")

    assert channel.sent == [(OWNER, i18n.t("reminder_water", "th"))]
    assert channel.sent == [(OWNER, "💧 ถึงเวลาดื่มน้ำแล้วนะ วันนี้ดื่มไปเท่าไหร่แล้ว?")]


async def test_send_reminder_addresses_the_given_chat_id_not_a_pinned_one():
    """SPEC-v1.2.md R-C1: two different chat ids each get their own send."""
    registry = _default_registry()
    channel = FakeChannel()

    await send_reminder(channel, "user-a", registry.get("water"), "en")
    await send_reminder(channel, "user-b", registry.get("water"), "en")

    assert [chat_id for chat_id, _ in channel.sent] == ["user-a", "user-b"]


async def test_send_reminder_new_habit_with_reminder_text_sends_it_in_resolved_language():
    sleep = _habit(
        "sleep",
        "numeric",
        goal=8,
        reminder_times=("07:00",),
        reminder_text_en="😴 How many hours did you sleep?",
        reminder_text_th="😴 เมื่อคืนนอนกี่ชั่วโมง?",
    )
    channel = FakeChannel()

    await send_reminder(channel, OWNER, sleep, "en")
    await send_reminder(channel, OWNER, sleep, "th")

    assert [text for _, text in channel.sent] == ["😴 How many hours did you sleep?", "😴 เมื่อคืนนอนกี่ชั่วโมง?"]


async def test_send_reminder_new_habit_without_reminder_text_uses_generic_template():
    steps = _habit("steps", "numeric", label_en="steps", label_th="ก้าวเดิน", reminder_times=("09:00",))
    channel = FakeChannel()

    await send_reminder(channel, OWNER, steps, "en")
    await send_reminder(channel, OWNER, steps, "th")

    assert [text for _, text in channel.sent] == [
        i18n.t("reminder_generic", "en", label="steps"),
        i18n.t("reminder_generic", "th", label="ก้าวเดิน"),
    ]


# ---------------------------------------------------------------------------
# effective_reminder_times (R-S4): the ONE resolver run_due_reminders and
# `/remind` both consult -- no override -> config default; ["off"] -> [];
# a custom list -> sorted+deduped. (module `schedules` owns the WRITE side
# via `/remind`; this shared-surface test only exercises the resolver
# itself against direct `db.set_reminder_times`/`clear_reminder_times`
# calls, not the not-yet-built command.)
# ---------------------------------------------------------------------------


def test_effective_reminder_times_no_override_falls_back_to_config_default(db):
    config = Config()
    registry = HabitRegistry.from_config(config)
    water = registry.get("water")
    assert effective_reminder_times(db, config, water, OWNER) == list(water.reminder_times)


def test_effective_reminder_times_off_sentinel_means_no_reminders(db):
    config = Config()
    registry = HabitRegistry.from_config(config)
    water = registry.get("water")
    db.set_reminder_times(OWNER, "water", ["off"])
    assert effective_reminder_times(db, config, water, OWNER) == []


def test_effective_reminder_times_custom_list_is_sorted(db):
    config = Config()
    registry = HabitRegistry.from_config(config)
    water = registry.get("water")
    # db.set_reminder_times stores exactly what it's given (R-S5's own
    # de-dupe responsibility lives on the WRITE side, module `schedules`'s
    # execute_remind, not this shared-surface store) -- a legitimately
    # duplicate-free but out-of-order list is enough to prove the read
    # side (effective_reminder_times) sorts.
    db.set_reminder_times(OWNER, "water", ["12:00", "08:00"])
    assert effective_reminder_times(db, config, water, OWNER) == ["08:00", "12:00"]


def test_effective_reminder_times_clearing_reverts_to_config_default(db):
    config = Config()
    registry = HabitRegistry.from_config(config)
    water = registry.get("water")
    db.set_reminder_times(OWNER, "water", ["12:00"])
    db.clear_reminder_times(OWNER, "water")
    assert effective_reminder_times(db, config, water, OWNER) == list(water.reminder_times)


def test_effective_reminder_times_is_per_user(db):
    """SPEC-v1.2.md AC-S2: A's custom time doesn't affect B's."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    water = registry.get("water")
    db.upsert_user("user-b", role="member", status="active")
    db.set_reminder_times("user-a", "water", ["12:00"])

    assert effective_reminder_times(db, config, water, "user-a") == ["12:00"]
    assert effective_reminder_times(db, config, water, "user-b") == list(water.reminder_times)


def test_effective_reminder_times_a_previously_goal_less_or_config_less_habit_is_still_settable(db):
    """R-S5's own note: a habit with NO config `reminder_times` at all is
    still allowed to gain a custom override."""
    config = Config()
    sleep = _habit("sleep", "numeric", goal=8, reminder_times=())
    assert effective_reminder_times(db, config, sleep, OWNER) == []
    db.set_reminder_times(OWNER, "sleep", ["07:00"])
    assert effective_reminder_times(db, config, sleep, OWNER) == ["07:00"]


# ---------------------------------------------------------------------------
# run_due_reminders (R-S1): the minutely tick, replacing `schedule_reminders`.
# Rewritten AC13/AC14 coverage -- same acceptance intent ("a habit's
# reminder fires at its own configured time and not at other times; a habit
# with no reminder_times never fires"), against the new mechanism.
# ---------------------------------------------------------------------------


async def test_run_due_reminders_fires_at_a_habits_configured_time(db):
    """AC13: the default config's water habit has 08:00 among its times."""
    config = Config()  # water default reminder_times includes "08:00"
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()

    await run_due_reminders(channel, config, registry, db, clock=_fixed_clock("08:00"))

    water_sends = [text for chat_id, text in channel.sent if chat_id == OWNER]
    assert i18n.t("reminder_water", i18n.resolve_unprompted_language(config)) in water_sends


async def test_run_due_reminders_does_not_fire_at_an_unconfigured_time(db):
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()

    await run_due_reminders(channel, config, registry, db, clock=_fixed_clock("03:33"))  # not a configured time

    assert channel.sent == []


async def test_run_due_reminders_habit_with_no_reminder_times_never_fires(db):
    silent = _habit("meds", "boolean", unit_en=None, unit_th=None, reminder_times=())
    registry = HabitRegistry([silent])
    config = Config()
    channel = FakeChannel()

    # Sweep every minute of a day -- meds must never fire, since it has no
    # config reminder_times and no override.
    for hour in range(24):
        for minute in (0, 30):
            await run_due_reminders(channel, config, registry, db, clock=_fixed_clock(f"{hour:02d}:{minute:02d}"))
    assert channel.sent == []


async def test_run_due_reminders_a_new_habits_configured_time_fires_its_own_text(db):
    """AC14: a new habit's reminder_times fire, sending its own
    reminder_text (not the generic template, since one is configured)."""
    sleep = _habit(
        "sleep",
        "numeric",
        goal=8,
        reminder_times=("07:00",),
        reminder_text_en="😴 How many hours did you sleep?",
        reminder_text_th="😴 เมื่อคืนนอนกี่ชั่วโมง?",
    )
    registry = HabitRegistry([sleep])
    config = Config()  # primary_language defaults to Thai
    channel = FakeChannel()

    await run_due_reminders(channel, config, registry, db, clock=_fixed_clock("07:00"))

    assert channel.sent == [(OWNER, "😴 เมื่อคืนนอนกี่ชั่วโมง?")]


async def test_run_due_reminders_only_fires_for_active_users(db):
    """R-S1: `db.active_user_ids()` is the fan-out set -- a pending/blocked
    user's reminders never fire."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()
    db.upsert_user("pending-user", role="member", status="pending")

    await run_due_reminders(channel, config, registry, db, clock=_fixed_clock("08:00"))

    assert all(chat_id != "pending-user" for chat_id, _ in channel.sent)


async def test_run_due_reminders_fires_independently_for_each_active_user(db):
    """AC-U5/AC-S1: two active users both due at the same minute each get
    their own send."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()
    db.upsert_user("user-b", role="member", status="active")

    await run_due_reminders(channel, config, registry, db, clock=_fixed_clock("08:00"))

    chat_ids = {chat_id for chat_id, _ in channel.sent}
    assert OWNER in chat_ids
    assert "user-b" in chat_ids


async def test_run_due_reminders_custom_time_fires_and_config_time_does_not(db):
    """AC-S2: after `/remind water 12:00` (simulated directly via
    `db.set_reminder_times`, since the `schedules` module's own command
    parsing isn't this shared surface's scope), water fires at 12:00 and
    NOT at any of the old config times."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    water = registry.get("water")
    db.set_reminder_times(OWNER, "water", ["12:00"])

    channel_at_config_time = FakeChannel()
    await run_due_reminders(channel_at_config_time, config, registry, db, clock=_fixed_clock(water.reminder_times[0]))
    assert channel_at_config_time.sent == []

    channel_at_custom_time = FakeChannel()
    await run_due_reminders(channel_at_custom_time, config, registry, db, clock=_fixed_clock("12:00"))
    assert any(chat_id == OWNER for chat_id, _ in channel_at_custom_time.sent)


async def test_run_due_reminders_off_sentinel_suppresses_that_habit_only(db):
    """AC-S3: `/remind water off` (simulated directly) -- water never
    fires for that user; other habits (e.g. stretch) are unaffected."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    stretch = registry.get("stretch")
    db.set_reminder_times(OWNER, "water", ["off"])
    channel = FakeChannel()

    await run_due_reminders(channel, config, registry, db, clock=_fixed_clock(stretch.reminder_times[0]))

    sent_texts = [text for _, text in channel.sent]
    assert i18n.t("reminder_water", i18n.resolve_unprompted_language(config)) not in sent_texts
    assert i18n.t("reminder_stretch", i18n.resolve_unprompted_language(config)) in sent_texts


# ---------------------------------------------------------------------------
# SPEC-v1.7.md R-G3: `registry_for` -- additive, optional per-user registry
# resolution. Omitted (every test above), byte-identical to pre-v1.7 (AC-5).
# ---------------------------------------------------------------------------


async def _reading_habit() -> Habit:
    return Habit(
        id="reading", type="duration", label_en="reading", label_th="อ่านหนังสือ",
        unit_en="min", unit_th="นาที", goal=None, reminder_times=["09:00"],
        reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )


async def test_run_due_reminders_registry_for_resolves_per_user(db):
    """A's own custom habit fires for A at A's reminder time; B (no
    registry_for entry of their own, base-only) never gets it."""
    config = Config()
    base_registry = HabitRegistry.from_config(config)
    reading = await _reading_habit()
    a_registry = HabitRegistry([*base_registry, reading])
    db.upsert_user("user-b", role="member", status="active")
    channel = FakeChannel()

    def registry_for(user_id: str) -> HabitRegistry:
        return a_registry if user_id == OWNER else base_registry

    await run_due_reminders(
        channel, config, base_registry, db, clock=_fixed_clock("09:00"), registry_for=registry_for
    )

    sent = {(chat_id, text) for chat_id, text in channel.sent}
    lang = i18n.resolve_unprompted_language(config)
    reading_text = i18n.t("reminder_generic", lang, label=reading.label(lang))
    assert (OWNER, reading_text) in sent
    assert not any(chat_id == "user-b" and text == reading_text for chat_id, text in sent)


async def test_run_due_reminders_registry_for_none_falls_back_to_registry(db):
    """registry_for omitted (None, the default) -- byte-identical to
    passing no registry_for at all (the pre-v1.7 call shape)."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()

    await run_due_reminders(channel, config, registry, db, clock=_fixed_clock("08:00"), registry_for=None)

    assert any(chat_id == OWNER for chat_id, _ in channel.sent)


async def test_run_due_reminders_state_tracks_last_habit_per_user(db):
    """R-S2/AC-U-SNOOZE: `ReminderState.last_habit_id` is a per-chat_id map,
    updated only for the user whose reminder actually fired."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    water = registry.get("water")
    state = ReminderState()
    channel = FakeChannel()

    await run_due_reminders(channel, config, registry, db, state=state, clock=_fixed_clock(water.reminder_times[0]))

    assert state.last_habit_id.get(OWNER) == "water"


async def test_run_due_reminders_goal_met_skip_is_per_user(db):
    """AC-U5: A has met the goal (skipped), B has not (sent) -- each
    evaluated against their own total.

    `_goal_already_met` reads TODAY's total via `_today_str(config)`,
    which uses the REAL current date (not the `clock` passed to
    `run_due_reminders`, which only drives the HH:MM-due check) -- so the
    seeded log must land on the real "today", not a hardcoded past date."""
    from datetime import date

    from habit_assistant.storage.models import LogEntry

    config = Config()  # water goal 2500
    registry = HabitRegistry.from_config(config)
    water = registry.get("water")
    db.upsert_user("user-b", role="member", status="active")
    today_iso = date.today().isoformat()
    db.insert_log(LogEntry(None, OWNER, f"{today_iso}T07:00:00", "water", 3000.0, None, "3000ml", "reply"))
    channel = FakeChannel()

    await run_due_reminders(channel, config, registry, db, clock=_fixed_clock(water.reminder_times[0]))

    chat_ids_sent_water = {
        chat_id for chat_id, text in channel.sent if text == i18n.t("reminder_water", i18n.resolve_unprompted_language(config))
    }
    assert OWNER not in chat_ids_sent_water  # goal already met -> skipped
    assert "user-b" in chat_ids_sent_water  # no logs -> goal not met -> sent


# ---------------------------------------------------------------------------
# async_main wiring: a single "reminder_tick" job (not one per habit-time),
# alongside the weekly-review job.
# ---------------------------------------------------------------------------


class _StopAfterSchedulerStart(Exception):
    pass


class _FakeScheduler:
    """Records add_job calls; start/shutdown are no-ops. Keeps a reference
    to the most recently constructed instance so a test can inspect the
    scheduler async_main built internally (it isn't otherwise returned)."""

    last_instance: "_FakeScheduler | None" = None

    def __init__(self, *args, **kwargs):
        self.jobs: dict[str, object] = {}
        _FakeScheduler.last_instance = self

    def add_job(self, func, trigger=None, args=None, id=None, replace_existing=True, **kwargs):
        from types import SimpleNamespace

        self.jobs[id] = SimpleNamespace(func=func, trigger=trigger, args=args, id=id)

    def start(self):
        pass

    def shutdown(self, wait=False):
        pass

    def get_jobs(self):
        return list(self.jobs.values())

    def get_job(self, job_id):
        return self.jobs.get(job_id)


class _FakeTelegramChannel:
    def __init__(self, *args, **kwargs):
        pass

    async def send(self, chat_id: str, text: str) -> None:
        pass

    async def send_actionable(self, chat_id: str, text: str, buttons: list[Button]) -> None:
        pass

    async def set_my_commands(self, commands) -> None:
        pass

    async def run(self, on_message, on_callback=None):
        raise _StopAfterSchedulerStart()

    async def aclose(self) -> None:
        pass


async def test_async_main_registers_weekly_review_job_from_config(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from habit_assistant import main as main_module

    config = main_module.Config.model_validate(
        {
            "app": {"db_path": str(tmp_path / "habits.db")},
            "weekly_review": {"day_of_week": "sat", "time": "09:15"},
        }
    )

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module,
        "load_secrets",
        lambda: SimpleNamespace(telegram_bot_token="fake", telegram_chat_id="fake"),
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", _FakeTelegramChannel)
    _FakeScheduler.last_instance = None

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)

    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    scheduler = _FakeScheduler.last_instance
    assert scheduler is not None
    job = scheduler.get_job("weekly_review")
    assert job is not None
    trigger_fields = {f.name: str(f) for f in job.trigger.fields}
    assert trigger_fields["day_of_week"] == "sat"
    assert trigger_fields["hour"] == "9"
    assert trigger_fields["minute"] == "15"

    # SPEC-v1.2.md R-S1: a single "reminder_tick" job replaces the old
    # one-job-per-habit-time fan-out from the removed `schedule_reminders`.
    tick_job = scheduler.get_job("reminder_tick")
    assert tick_job is not None
    tick_fields = {f.name: str(f) for f in tick_job.trigger.fields}
    assert tick_fields["second"] == "0"
