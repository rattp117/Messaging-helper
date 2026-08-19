"""Weekly review narrative tests (AC8): run_weekly_review composes the
factual stats block with an LLM narrative, falls back gracefully if the LLM
call fails, and the result is what main.py sends over the channel. The
aggregation math itself (adherence %, totals, streak, diary count) is
covered in test_db.py per Archi's file split; this file covers the
narrative + delivery layer on top of it."""

from __future__ import annotations

from datetime import date

import pytest

from habit_assistant.config import Config
from habit_assistant.core.review import run_weekly_review
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry


class FakeLLM:
    def __init__(self, text: str | None):
        self._text = text
        self.calls: list[tuple[str, str]] = []

    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        self.calls.append((system_prompt, user_prompt))
        return self._text


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.insert_log(LogEntry(None, "2026-08-19T09:00:00", "water", 2500.0, None, "seed", "reply"))
    database.insert_log(LogEntry(None, "2026-08-19T11:00:00", "stretch", 10.0, None, "seed", "reply"))
    database.insert_log(LogEntry(None, "2026-08-19T21:30:00", "diary", None, "seed", "seed", "reply"))
    yield database
    database.close()


async def test_run_weekly_review_includes_narrative_and_stats(db):
    llm = FakeLLM("Great week! Keep up the water habit.")

    text = await run_weekly_review(db, Config(), llm, today=date(2026, 8, 19))

    assert "📊 Weekly Review" in text
    assert "Great week! Keep up the water habit." in text
    assert "Water total: 2500 ml" in text
    assert "Stretch sessions this week: 1" in text
    assert "Diary entries this week: 1" in text


async def test_run_weekly_review_falls_back_when_llm_returns_none(db):
    llm = FakeLLM(None)

    text = await run_weekly_review(db, Config(), llm, today=date(2026, 8, 19))

    assert "Here is your weekly summary." in text
    assert "Water total: 2500 ml" in text  # stats block is still present


async def test_run_weekly_review_falls_back_when_llm_returns_empty_string(db):
    llm = FakeLLM("")

    text = await run_weekly_review(db, Config(), llm, today=date(2026, 8, 19))

    assert "Here is your weekly summary." in text


async def test_run_weekly_review_passes_stats_summary_to_llm_prompt(db):
    llm = FakeLLM("narrative")

    await run_weekly_review(db, Config(), llm, today=date(2026, 8, 19))

    assert len(llm.calls) == 1
    _system_prompt, user_prompt = llm.calls[0]
    assert "Water total: 2500 ml" in user_prompt


async def test_run_weekly_review_system_prompt_forbids_medical_advice(db):
    """Sanity check the narrative system prompt actually encodes SPEC.md
    §6's "no medical advice" constraint -- a prompt-engineering contract,
    not model behavior we can unit test directly."""
    llm = FakeLLM("narrative")

    await run_weekly_review(db, Config(), llm, today=date(2026, 8, 19))

    system_prompt, _user_prompt = llm.calls[0]
    assert "medical advice" in system_prompt.lower()


async def test_run_weekly_review_defaults_to_today_when_not_given(db, monkeypatch):
    """today=None should default to date.today() -- patch the module's
    `date` so this is deterministic."""
    import habit_assistant.core.review as review_module

    class FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 8, 19)

    monkeypatch.setattr(review_module, "date", FixedDate)
    llm = FakeLLM("narrative")

    text = await run_weekly_review(db, Config(), llm, today=None)

    assert "Water total: 2500 ml" in text


async def test_weekly_review_job_sends_result_over_channel(db):
    """Mirrors main.py's weekly_review_job closure: run_weekly_review's
    output must be exactly what gets sent through the Channel."""
    from typing import Awaitable, Callable

    from habit_assistant.channels.base import Channel

    class FakeChannel(Channel):
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, text: str) -> None:
            self.sent.append(text)

        async def run(self, on_message: Callable[[str], Awaitable[None]]) -> None:
            raise NotImplementedError

    channel = FakeChannel()
    llm = FakeLLM("Solid week overall.")

    text = await run_weekly_review(db, Config(), llm, today=date(2026, 8, 19))
    await channel.send(text)

    assert channel.sent == [text]
    assert "Solid week overall." in channel.sent[0]
