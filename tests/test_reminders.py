"""Reminder scheduling tests (AC7): jobs are registered from config.toml
times via AsyncIOScheduler cron triggers, and send_reminder pushes the
right text through the Channel."""

from __future__ import annotations

from typing import Awaitable, Callable

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core.reminders import REMINDER_TEXTS, schedule_reminders, send_reminder


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message: Callable[[str], Awaitable[None]]) -> None:
        raise NotImplementedError


async def test_send_reminder_sends_correct_text_per_category():
    channel = FakeChannel()

    await send_reminder(channel, "water")
    await send_reminder(channel, "stretch")
    await send_reminder(channel, "diary")

    assert channel.sent == [REMINDER_TEXTS["water"], REMINDER_TEXTS["stretch"], REMINDER_TEXTS["diary"]]


async def test_send_reminder_unknown_category_raises_value_error():
    channel = FakeChannel()
    with pytest.raises(ValueError):
        await send_reminder(channel, "nonsense")


def test_schedule_reminders_registers_one_job_per_configured_time():
    config = Config()  # defaults: 6 water + 2 stretch + 1 diary = 9
    scheduler = AsyncIOScheduler()
    channel = FakeChannel()

    schedule_reminders(scheduler, channel, config)

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
            "reminders": {
                "water": {"times": ["08:00", "20:30"], "goal_ml": 2500},
                "stretch": {"times": ["11:00"]},
                "diary": {"times": ["21:30"]},
            }
        }
    )
    scheduler = AsyncIOScheduler()
    channel = FakeChannel()

    schedule_reminders(scheduler, channel, config)

    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {"reminder_water_08:00", "reminder_water_20:30", "reminder_stretch_11:00", "reminder_diary_21:30"}

    water_0800 = jobs["reminder_water_08:00"]
    trigger_fields = {f.name: f for f in water_0800.trigger.fields}
    assert str(trigger_fields["hour"]) == "8"
    assert str(trigger_fields["minute"]) == "0"


def test_schedule_reminders_job_args_bind_correct_category_and_channel():
    config = Config.model_validate({"reminders": {"water": {"times": ["08:00"]}, "stretch": {"times": []}, "diary": {"times": []}}})
    scheduler = AsyncIOScheduler()
    channel = FakeChannel()

    schedule_reminders(scheduler, channel, config)

    job = scheduler.get_job("reminder_water_08:00")
    assert job.args == (channel, "water")


def test_schedule_reminders_uses_configured_timezone():
    config = Config.model_validate(
        {"app": {"timezone": "UTC"}, "reminders": {"water": {"times": ["08:00"]}, "stretch": {"times": []}, "diary": {"times": []}}}
    )
    scheduler = AsyncIOScheduler()
    channel = FakeChannel()

    schedule_reminders(scheduler, channel, config)

    job = scheduler.get_job("reminder_water_08:00")
    assert str(job.trigger.timezone) == "UTC"


async def test_schedule_reminders_replace_existing_does_not_duplicate():
    """AsyncIOScheduler only reconciles `replace_existing` once jobs leave
    the pending queue and hit the jobstore, which happens at scheduler
    start() -- so the scheduler must be started for this to be observable."""
    config = Config.model_validate({"reminders": {"water": {"times": ["08:00"]}, "stretch": {"times": []}, "diary": {"times": []}}})
    scheduler = AsyncIOScheduler()
    channel = FakeChannel()

    schedule_reminders(scheduler, channel, config)
    schedule_reminders(scheduler, channel, config)  # re-registering must replace, not duplicate
    scheduler.start()
    try:
        assert len(scheduler.get_jobs()) == 1
    finally:
        scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Weekly review job registration from config.toml, exercised through
# async_main with everything mocked (no network, no real Telegram/Ollama) --
# AC7's "weekly review Sunday 20:00; all times from config.toml".
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

