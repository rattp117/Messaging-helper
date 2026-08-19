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
from habit_assistant.core import i18n
from habit_assistant.core.review import run_weekly_review
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

# ROADMAP.md v0.6.0 AC6.4: the weekly review is an unprompted send, so
# `run_weekly_review(db, Config(), ...)` -- the default `Config()` used
# throughout this file (i18n.language="auto", i18n.primary_language="th")
# -- now resolves to Thai, not English. Every test below that previously
# asserted a literal English label/fallback string is CHANGED to assert
# the Thai catalog entry instead (same catalog id, `lang="th"`), per the
# task's "list every changed expectation" instruction. Tests that only
# assert the LLM-supplied narrative passes through verbatim, or that
# "medical advice" appears in the (always-English, LLM-facing) system
# prompt, are unaffected and unchanged.
LANG = "th"


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
    """CHANGED (v0.6.0): default Config() now resolves to Thai for this
    unprompted send -- header/labels were English ("📊 Weekly Review",
    "Water total: 2500 ml", "Stretch sessions this week: 1", "Diary
    entries this week: 1"), now their Thai catalog equivalents. The
    LLM-supplied narrative always passes through verbatim regardless of
    language, so that assertion is unchanged."""
    llm = FakeLLM("Great week! Keep up the water habit.")

    text = await run_weekly_review(db, Config(), llm, today=date(2026, 8, 19))

    assert i18n.t("weekly_review_header", LANG) in text
    assert "Great week! Keep up the water habit." in text
    assert i18n.t("stats_water_total", LANG, water_total_ml=2500, water_avg_ml=357.1) in text
    assert i18n.t("stats_stretch_summary", LANG, stretch_total=1, stretch_streak=1) in text
    assert i18n.t("stats_diary_summary", LANG, diary_count=1) in text


async def test_run_weekly_review_falls_back_when_llm_returns_none(db):
    """CHANGED (v0.6.0): fallback narrative + stats block are now Thai
    under the default Config() (was "Here is your weekly summary." /
    "Water total: 2500 ml")."""
    llm = FakeLLM(None)

    text = await run_weekly_review(db, Config(), llm, today=date(2026, 8, 19))

    assert i18n.t("weekly_review_fallback_narrative", LANG) in text
    assert i18n.t("stats_water_total", LANG, water_total_ml=2500, water_avg_ml=357.1) in text  # stats block still present


async def test_run_weekly_review_falls_back_when_llm_returns_empty_string(db):
    """CHANGED (v0.6.0): same fallback-narrative change as the None case
    above -- was "Here is your weekly summary."."""
    llm = FakeLLM("")

    text = await run_weekly_review(db, Config(), llm, today=date(2026, 8, 19))

    assert i18n.t("weekly_review_fallback_narrative", LANG) in text


async def test_run_weekly_review_passes_stats_summary_to_llm_prompt(db):
    """CHANGED (v0.6.0): the stats block fed to the LLM prompt is now
    localized too (was the English "Water total: 2500 ml" line)."""
    llm = FakeLLM("narrative")

    await run_weekly_review(db, Config(), llm, today=date(2026, 8, 19))

    assert len(llm.calls) == 1
    _system_prompt, user_prompt = llm.calls[0]
    assert i18n.t("stats_water_total", LANG, water_total_ml=2500, water_avg_ml=357.1) in user_prompt


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
    `date` so this is deterministic.

    CHANGED (v0.6.0): stats block is now Thai under the default Config()
    (was "Water total: 2500 ml")."""
    import habit_assistant.core.review as review_module

    class FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 8, 19)

    monkeypatch.setattr(review_module, "date", FixedDate)
    llm = FakeLLM("narrative")

    text = await run_weekly_review(db, Config(), llm, today=None)

    assert i18n.t("stats_water_total", LANG, water_total_ml=2500, water_avg_ml=357.1) in text


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
