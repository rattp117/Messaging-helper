"""Reminder scheduling tests (SPEC-v0.7.md §8 AC13/AC14, module M2):
`schedule_reminders(scheduler, channel, config, registry)` registers one
cron job per habit's `reminder_times` entry via AsyncIOScheduler, and
`send_reminder(channel, habit, language)` resolves the right copy --
built-in ids reuse their byte-identical v0.6.0 catalog entries, other
habits use their own `reminder_text` or the type-generic
`reminder_generic` template."""

from __future__ import annotations

from typing import Awaitable, Callable

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import i18n
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.reminders import schedule_reminders, send_reminder


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message: Callable[[str], Awaitable[None]]) -> None:
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


# ---------------------------------------------------------------------------
# AC13: default config -> same 9 cron jobs (6 water, 2 stretch, 1 diary) at
# the same times as v0.6.0; send_reminder(channel, water_habit, "th") sends
# the byte-identical v0.6.0 Thai water reminder.
# ---------------------------------------------------------------------------


async def test_send_reminder_sends_byte_identical_v060_text_for_each_builtin_habit():
    registry = _default_registry()
    channel = FakeChannel()

    await send_reminder(channel, registry.get("water"), "en")
    await send_reminder(channel, registry.get("stretch"), "en")
    await send_reminder(channel, registry.get("diary"), "en")

    assert channel.sent == [
        i18n.t("reminder_water", "en"),
        i18n.t("reminder_stretch", "en"),
        i18n.t("reminder_diary", "en"),
    ]


async def test_send_reminder_water_habit_thai_is_byte_identical_to_v060():
    registry = _default_registry()
    channel = FakeChannel()

    await send_reminder(channel, registry.get("water"), "th")

    assert channel.sent == [i18n.t("reminder_water", "th")]
    assert channel.sent == ["💧 ถึงเวลาดื่มน้ำแล้วนะ วันนี้ดื่มไปเท่าไหร่แล้ว?"]


def test_schedule_reminders_registers_one_job_per_configured_time():
    config = Config()  # defaults: 6 water + 2 stretch + 1 diary = 9
    registry = HabitRegistry.from_config(config)
    scheduler = AsyncIOScheduler()
    channel = FakeChannel()

    schedule_reminders(scheduler, channel, config, registry)

    jobs = scheduler.get_jobs()
    assert len(jobs) == 9
    water_jobs = [j for j in jobs if j.id.startswith("reminder_water_")]
    stretch_jobs = [j for j in jobs if j.id.startswith("reminder_stretch_")]
    diary_jobs = [j for j in jobs if j.id.startswith("reminder_diary_")]
    assert len(water_jobs) == 6
    assert len(stretch_jobs) == 2
    assert len(diary_jobs) == 1


def test_schedule_reminders_cron_times_match_config():
    config = Config.model_validate(
        {
            "habits": [
                {
                    "id": "water",
                    "type": "numeric",
                    "goal": 2500,
                    "reminder_times": ["08:00", "20:30"],
                    "label": {"en": "water", "th": "น้ำ"},
                    "unit": {"en": "ml", "th": "มล."},
                },
                {
                    "id": "stretch",
                    "type": "duration",
                    "reminder_times": ["11:00"],
                    "label": {"en": "stretch", "th": "ยืดเส้น"},
                    "unit": {"en": "min", "th": "นาที"},
                },
                {
                    "id": "diary",
                    "type": "text",
                    "reminder_times": ["21:30"],
                    "label": {"en": "diary", "th": "ไดอารี่"},
                },
            ]
        }
    )
    registry = HabitRegistry.from_config(config)
    scheduler = AsyncIOScheduler()
    channel = FakeChannel()

    schedule_reminders(scheduler, channel, config, registry)

    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {"reminder_water_08:00", "reminder_water_20:30", "reminder_stretch_11:00", "reminder_diary_21:30"}

    water_0800 = jobs["reminder_water_08:00"]
    trigger_fields = {f.name: f for f in water_0800.trigger.fields}
    assert str(trigger_fields["hour"]) == "8"
    assert str(trigger_fields["minute"]) == "0"


def test_schedule_reminders_job_args_bind_correct_habit_and_language():
    """Reminders are unprompted, so "auto" resolves to
    `config.i18n.primary_language` (default Thai) -- job.args is
    `(channel, habit, "th")` under the default `Config()` used here, with
    `habit` the actual `Habit` object from the registry (not a bare
    category string, per SPEC-v0.7.md §5).

    CHANGED (ROADMAP.md v0.9.0 AC9.1/AC9.2/AC9.3): `schedule_reminders`
    now also binds `db`/`config`/`state` into every job's args so the
    adaptive quiet-hours/goal-met checks and the snooze `ReminderState`
    tracker work at fire time -- `send_reminder`'s 6-arg contract, not the
    old 3-arg one. `db`/`state` default `None` when `schedule_reminders`
    isn't given them (as here), `config` is always the real object."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    scheduler = AsyncIOScheduler()
    channel = FakeChannel()

    schedule_reminders(scheduler, channel, config, registry)

    job = scheduler.get_job("reminder_water_08:00")
    assert job.args == (channel, registry.get("water"), "th", None, config, None)


def test_schedule_reminders_uses_configured_timezone():
    config = Config.model_validate(
        {
            "app": {"timezone": "UTC"},
            "habits": [
                {
                    "id": "water",
                    "type": "numeric",
                    "goal": 2500,
                    "reminder_times": ["08:00"],
                    "label": {"en": "water", "th": "น้ำ"},
                    "unit": {"en": "ml", "th": "มล."},
                },
            ],
        }
    )
    registry = HabitRegistry.from_config(config)
    scheduler = AsyncIOScheduler()
    channel = FakeChannel()

    schedule_reminders(scheduler, channel, config, registry)

    job = scheduler.get_job("reminder_water_08:00")
    assert str(job.trigger.timezone) == "UTC"


async def test_schedule_reminders_replace_existing_does_not_duplicate():
    """AsyncIOScheduler only reconciles `replace_existing` once jobs leave
    the pending queue and hit the jobstore, which happens at scheduler
    start() -- so the scheduler must be started for this to be observable."""
    config = Config.model_validate(
        {
            "habits": [
                {
                    "id": "water",
                    "type": "numeric",
                    "goal": 2500,
                    "reminder_times": ["08:00"],
                    "label": {"en": "water", "th": "น้ำ"},
                    "unit": {"en": "ml", "th": "มล."},
                },
            ]
        }
    )
    registry = HabitRegistry.from_config(config)
    scheduler = AsyncIOScheduler()
    channel = FakeChannel()

    schedule_reminders(scheduler, channel, config, registry)
    schedule_reminders(scheduler, channel, config, registry)  # re-registering must replace, not duplicate
    scheduler.start()
    try:
        assert len(scheduler.get_jobs()) == 1
    finally:
        scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# AC14: a new habit's reminder_times are scheduled; firing sends its own
# reminder_text (or the reminder_generic template if unset); a habit with
# no reminder_times schedules nothing.
# ---------------------------------------------------------------------------


def test_schedule_reminders_adds_one_job_for_a_new_habit_with_reminder_times():
    sleep = _habit(
        "sleep",
        "numeric",
        goal=8,
        reminder_times=("07:00",),
        reminder_text_en="😴 How many hours did you sleep?",
        reminder_text_th="😴 เมื่อคืนนอนกี่ชั่วโมง?",
    )
    registry = HabitRegistry([sleep])
    config = Config()
    scheduler = AsyncIOScheduler()
    channel = FakeChannel()

    schedule_reminders(scheduler, channel, config, registry)

    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == "reminder_sleep_07:00"
    # CHANGED (ROADMAP.md v0.9.0): see the args-shape note on
    # test_schedule_reminders_job_args_bind_correct_habit_and_language above.
    assert jobs[0].args == (channel, sleep, "th", None, config, None)


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

    await send_reminder(channel, sleep, "en")
    await send_reminder(channel, sleep, "th")

    assert channel.sent == ["😴 How many hours did you sleep?", "😴 เมื่อคืนนอนกี่ชั่วโมง?"]


async def test_send_reminder_new_habit_without_reminder_text_uses_generic_template():
    steps = _habit("steps", "numeric", label_en="steps", label_th="ก้าวเดิน", reminder_times=("09:00",))
    channel = FakeChannel()

    await send_reminder(channel, steps, "en")
    await send_reminder(channel, steps, "th")

    assert channel.sent == [
        i18n.t("reminder_generic", "en", label="steps"),
        i18n.t("reminder_generic", "th", label="ก้าวเดิน"),
    ]


def test_schedule_reminders_habit_with_no_reminder_times_schedules_nothing():
    silent = _habit("meds", "boolean", unit_en=None, unit_th=None, reminder_times=())
    registry = HabitRegistry([silent])
    config = Config()
    scheduler = AsyncIOScheduler()
    channel = FakeChannel()

    schedule_reminders(scheduler, channel, config, registry)

    assert scheduler.get_jobs() == []


def test_schedule_reminders_mixed_registry_only_schedules_habits_with_times():
    water = HabitRegistry.from_config(Config()).get("water")
    silent = _habit("meds", "boolean", unit_en=None, unit_th=None, reminder_times=())
    sleep = _habit("sleep", "numeric", goal=8, reminder_times=("07:00",))
    registry = HabitRegistry([water, silent, sleep])
    config = Config()
    scheduler = AsyncIOScheduler()
    channel = FakeChannel()

    schedule_reminders(scheduler, channel, config, registry)

    job_ids = {j.id for j in scheduler.get_jobs()}
    assert all(not jid.startswith("reminder_meds_") for jid in job_ids)
    assert "reminder_sleep_07:00" in job_ids
    assert len([jid for jid in job_ids if jid.startswith("reminder_water_")]) == 6


# ---------------------------------------------------------------------------
# Weekly review job registration + schedule_reminders wiring, exercised
# through the real async_main -- SKIPPED, not an M2 bug. main.py's
# `schedule_reminders(scheduler, channel, config)` call site is still on
# the v0.6.0 3-arg contract (frozen shared-surface file per SPEC-v0.7.md
# §11's build order; IMPL.md "v0.7.0 — Multi-Habit Extensibility (shared
# surface only)" Known limitations #1 documents this exact boundary and
# explicitly defers the wiring flip to Archi's integration step). Calling
# the SPEC-v0.7.md §5 registry-aware `schedule_reminders` now raises
# TypeError (missing `registry`) from that frozen 3-arg call site.
# AC13/AC14 are already covered directly above at the unit level, which is
# this module's actual scope; this test only re-verifies the same wiring
# end-to-end through main.py, which is integration's job.
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

    def add_job(self, func, trigger=None, args=None, id=None, replace_existing=True):
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

    async def send(self, text: str) -> None:
        pass

    async def run(self, on_message):
        raise _StopAfterSchedulerStart()

    async def aclose(self) -> None:
        pass


# UNSKIPPED (ROADMAP.md v0.7.0 integration): main.py's
# `schedule_reminders(scheduler, channel, config, registry)` call site is
# now wired to the registry-aware contract (was the frozen v0.6.0 3-arg
# shape -- see IMPL.md's v0.7.0 "Known limitations" #1 / TEST-v0.7-M2.md
# item 3), so this end-to-end async_main path now passes.
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

    # Per-category reminder jobs (from schedule_reminders) are on the same
    # scheduler alongside the weekly review job.
    assert any(j.id.startswith("reminder_water_") for j in scheduler.get_jobs())
