"""AC11 [-> AC7.2] (SPEC-v0.7.md SS8): "Given the shipped code and a
config.toml that adds a sleep habit (numeric, unit h, goal 8) with no code
change, When 'นอน 7 ชม.' arrives, Then it is parsed as sleep/7, stored
(category='sleep', habit_type='numeric', value_num=7), confirmed against
goal, and appears in the weekly review -- end-to-end through the real
parser/reminders/review."

SPEC-v0.7.md SS11 "Integration order" step 3 explicitly calls this out as
the final Vera pass's own deliverable ("with a temporary config.toml that
adds a sleep habit (no code change) ... also add a synthetic boolean habit
to exercise the untested-by-default boolean path"), and all three module
Vera reports (`IMPL-v0.7-M1.md`, `TEST-v0.7-M2.md`, `TEST-v0.7-M3.md`)
independently flag it as still outstanding. This file is that deliverable.

Every habit exercised below is added purely as `Config`/`HabitConfig` data
-- `SLEEP`/`MEDS` are ordinary `HabitConfig` instances built the same way
`config.toml`'s `[[habits]]` array would produce them, and nothing in
`src/habit_assistant/` is edited, subclassed, or monkeypatched to make this
pass. That is the concrete proof of AC7.2's "zero code changes" promise --
if any module still had a hardcoded water/stretch/diary branch, some test
below would fail (get no confirmation, no reminder job, no review line)
rather than silently degrade.

Covers, all through real (non-parser-bypassing) production code:
1. Extraction (mocked LLM boundary, real `parse_message`/registry-built
   schema+prompt) for both the new numeric habit (Thai) and the new
   boolean habit (English).
2. Logging + confirmation (real `handle_inbound_message`), generic
   templates, both languages, matching SPEC-v0.7.md SS3.2's own examples
   verbatim.
3. Reminder job registration (real `schedule_reminders`) for both new
   habits, using the generic `reminder_generic` fallback (no `reminder_text`
   configured for either -- that path is already covered for a habit WITH
   `reminder_text` by `test_reminders.py`'s own AC14 tests; this file
   deliberately covers the sibling no-`reminder_text` branch instead of
   duplicating it).
4. Weekly review inclusion (real `compute_weekly_stats`/
   `run_weekly_review`) for both new habits alongside the three built-ins.
5. Undo/edit where type-appropriate (numeric-with-goal supports both;
   boolean supports undo only -- editing a boolean/text habit is
   out-of-scope per SPEC-v0.7.md SS10, and `core/commands.py` correctly
   never classifies one as an edit target in the first place).
6. Migration 004: a copy of a v3-shaped DB (pre-v0.7, no `habit_type`
   column, real legacy water rows) opened through the real `Database`
   class (migrates 3->4, backfills `habit_type`), then new sleep/meds rows
   inserted through the same DB -- `compute_weekly_stats` aggregates the
   migrated-legacy and newly-added rows together in one call, proving the
   backfill doesn't just sit inert but actually participates in the same
   aggregation new-habit rows do.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Awaitable, Callable

import httpx
import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config, HabitConfig, HabitLabel
from habit_assistant.core import i18n
from habit_assistant.core.commands import dispatch
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.parser import parse_message
from habit_assistant.core.reminders import ReminderState, run_due_reminders, send_reminder
from habit_assistant.core.review import compute_weekly_stats, run_weekly_review
from habit_assistant.llm.ollama_client import OllamaClient
from habit_assistant.main import handle_inbound_message
from habit_assistant.storage.db import Database

# SPEC-v1.2.md: this file exercises the shared surface's user_id threading
# through the same real, non-parser-bypassing pipeline it always has -- a
# single owning chat id stands in for "the" user throughout, since AC7.2's
# "zero code changes for a config-only habit" claim is orthogonal to
# multi-user scoping (that's SPEC-v1.2.md's own AC-U*/AC-M3 territory,
# covered by test_reminders.py/test_migrations.py/test_v11_shared_surface.py).
OWNER = "owner"

# ---------------------------------------------------------------------------
# The added-via-config-only habits (SPEC-v0.7.md SS2.1's own "sleep" example,
# plus a synthetic boolean "meds" per SS11 integration step 3). Neither
# habit's id/type/label/unit/goal is referenced anywhere in
# src/habit_assistant/ -- if this file passes, the registry-driven pipeline
# genuinely needed zero code changes to support them.
# ---------------------------------------------------------------------------

SLEEP = HabitConfig(
    id="sleep",
    type="numeric",
    goal=8,
    reminder_times=["07:00"],
    label=HabitLabel(en="sleep", th="นอน"),
    unit=HabitLabel(en="h", th="ชม."),
)

MEDS = HabitConfig(
    id="meds",
    type="boolean",
    reminder_times=["09:00"],
    label=HabitLabel(en="meds", th="ยา"),
)


def multi_habit_config() -> Config:
    return Config(habits=[*Config().habits, SLEEP, MEDS])


CONFIG = multi_habit_config()
REGISTRY = HabitRegistry.from_config(CONFIG)


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append(text)

    async def run(self, on_message: Callable[[str, str], Awaitable[None]], on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


class FakeLLM:
    """Stand-in for the diary-reflection/narrative boundary only -- not
    used by any test in this file for extraction (those use `make_llm`,
    a real `OllamaClient` over a mock transport, so `parse_message`'s own
    registry-built schema/prompt genuinely runs)."""

    def __init__(self, reflection: str | None = "noted"):
        self._reflection = reflection

    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return self._reflection


@pytest.fixture
def fixed_clock():
    def clock():
        return datetime(2026, 8, 19, 14, 30, 0)

    return clock


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(OWNER, role="owner", status="active")
    yield database
    database.close()


def make_llm(extraction_json: str, reflection_text: str = "noted") -> OllamaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "format" in body:
            return httpx.Response(200, json={"message": {"content": extraction_json}})
        return httpx.Response(200, json={"message": {"content": reflection_text}})

    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(transport=transport)
    return OllamaClient("http://mac-mini:11434", "qwen3.5:9b-mlx", timeout_seconds=5.0, client=async_client)


def extraction(category: str, value, confidence: float = 0.95) -> str:
    return json.dumps({"category": category, "value": value, "confidence": confidence})


# ---------------------------------------------------------------------------
# 1. Extraction -- real parse_message, real registry-built schema/prompt,
# mocked LLM boundary only.
# ---------------------------------------------------------------------------


async def test_extraction_sleep_thai_message_parses_as_sleep_7():
    llm = make_llm(extraction("sleep", 7))

    result = await parse_message("นอน 7 ชม.", llm, REGISTRY)

    assert result.category == "sleep"
    assert result.value == 7.0


async def test_extraction_meds_boolean_message_parses_as_meds_true():
    llm = make_llm(extraction("meds", True))

    result = await parse_message("took my meds", llm, REGISTRY)

    assert result.category == "meds"
    assert result.value is True


async def test_extraction_prompt_contains_both_added_habits():
    """Sanity check on the same prompt the two tests above implicitly rely
    on: build_extraction_system_prompt(REGISTRY) actually carries category
    lines for both added habits (module M1, tests/test_prompts.py owns the
    detailed per-field coverage; this is a one-line integration sanity
    check that THIS registry -- with both sleep and meds -- is what the
    real client sends)."""
    captured: list[httpx.Request] = []
    llm = make_llm(extraction("sleep", 7))

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"message": {"content": extraction("sleep", 7)}})

    llm = OllamaClient(
        "http://mac-mini:11434",
        "qwen3.5:9b-mlx",
        timeout_seconds=5.0,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    await parse_message("นอน 7 ชม.", llm, REGISTRY)

    assert len(captured) == 1
    system_msg = json.loads(captured[0].content)["messages"][0]["content"]
    assert '"sleep"' in system_msg
    assert '"meds"' in system_msg


# ---------------------------------------------------------------------------
# 2. Logging + confirmation -- real handle_inbound_message, generic
# templates, both languages, matching SPEC-v0.7.md SS3.2 verbatim.
# ---------------------------------------------------------------------------


async def test_sleep_confirmation_english_matches_spec_example(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("sleep", 7))

    await handle_inbound_message(
        "slept 7 hours", db=db, llm=llm, channel=channel, config=CONFIG, clock=fixed_clock, registry=REGISTRY, user_id=OWNER
    )

    assert channel.sent == ["✅ 7 h sleep logged — today 7 / 8 h (88%)"]


async def test_sleep_confirmation_thai_matches_spec_example(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("sleep", 7))

    await handle_inbound_message(
        "นอน 7 ชม.", db=db, llm=llm, channel=channel, config=CONFIG, clock=fixed_clock, registry=REGISTRY, user_id=OWNER
    )

    assert channel.sent == ["✅ บันทึกนอน 7 ชม. แล้ว — วันนี้ 7 / 8 ชม. (88%)"]


async def test_sleep_row_stored_with_correct_category_habit_type_and_value(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("sleep", 7))

    await handle_inbound_message(
        "นอน 7 ชม.", db=db, llm=llm, channel=channel, config=CONFIG, clock=fixed_clock, registry=REGISTRY, user_id=OWNER
    )

    rows = db.logs_between(OWNER, "2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert len(rows) == 1
    assert rows[0]["category"] == "sleep"
    assert rows[0]["habit_type"] == "numeric"
    assert rows[0]["value_num"] == 7.0


async def test_meds_confirmation_english_matches_spec_example(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("meds", True))

    await handle_inbound_message(
        "took my meds", db=db, llm=llm, channel=channel, config=CONFIG, clock=fixed_clock, registry=REGISTRY, user_id=OWNER
    )

    assert channel.sent == ["✅ meds — done today"]


async def test_meds_confirmation_thai(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("meds", True))

    await handle_inbound_message(
        "กินยาแล้ว", db=db, llm=llm, channel=channel, config=CONFIG, clock=fixed_clock, registry=REGISTRY, user_id=OWNER
    )

    assert channel.sent == ["✅ ยา — ทำแล้ว วันนี้"]


async def test_meds_row_stored_as_boolean_zero_or_one(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("meds", False))

    await handle_inbound_message(
        "no meds yet", db=db, llm=llm, channel=channel, config=CONFIG, clock=fixed_clock, registry=REGISTRY, user_id=OWNER
    )

    rows = db.logs_between(OWNER, "2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert rows[0]["category"] == "meds"
    assert rows[0]["habit_type"] == "boolean"
    assert rows[0]["value_num"] == 0.0


# ---------------------------------------------------------------------------
# 3. Reminder scheduling -- real run_due_reminders (SPEC-v1.2.md R-S1's
# minutely tick replaces the removed per-config-time schedule_reminders),
# generic reminder_generic fallback (neither SLEEP nor MEDS sets
# reminder_text). AC7.2's "zero code changes" claim now means: a
# config-only habit's reminder_times are picked up by
# effective_reminder_times's config fallback with no per-habit code path.
# ---------------------------------------------------------------------------


async def test_run_due_reminders_fires_both_added_habits_at_their_configured_times(db):
    # run_due_reminders is an unprompted send -- it resolves its own
    # language via i18n.resolve_unprompted_language(config), which defaults
    # to Thai (ROADMAP.md v0.6.0 AC6.3, config.toml's primary_language="th"),
    # not whatever language a test happens to pass send_reminder directly.
    channel = FakeChannel()
    lang = i18n.resolve_unprompted_language(CONFIG)

    await run_due_reminders(channel, CONFIG, REGISTRY, db, clock=lambda: datetime(2026, 8, 19, 7, 0, 0))
    assert channel.sent == [i18n.t("reminder_generic", lang, label=REGISTRY.get("sleep").label(lang))]

    channel.sent.clear()
    await run_due_reminders(channel, CONFIG, REGISTRY, db, clock=lambda: datetime(2026, 8, 19, 9, 0, 0))
    assert channel.sent == [i18n.t("reminder_generic", lang, label=REGISTRY.get("meds").label(lang))]


async def test_run_due_reminders_still_fires_all_builtin_times_alongside_added_habits(db):
    channel = FakeChannel()

    # Built-ins untouched: still exactly 6 water + 2 stretch + 1 diary
    # fires across their configured times, unaffected by the two added
    # habits sharing the same registry/db/tick.
    fired_at: dict[str, int] = {}
    for hhmm in ["08:00", "10:30", "11:00", "13:00", "15:30", "16:00", "18:00", "20:30", "21:30"]:
        hour, minute = (int(p) for p in hhmm.split(":"))
        channel.sent.clear()
        await run_due_reminders(channel, CONFIG, REGISTRY, db, clock=lambda h=hour, m=minute: datetime(2026, 8, 19, h, m, 0))
        fired_at[hhmm] = len(channel.sent)

    assert sum(fired_at[t] for t in ["08:00", "10:30", "13:00", "15:30", "18:00", "20:30"]) == 6  # water
    assert sum(fired_at[t] for t in ["11:00", "16:00"]) == 2  # stretch
    assert fired_at["21:30"] == 1  # diary


async def test_send_reminder_sleep_uses_generic_template_english():
    channel = FakeChannel()
    sleep_habit = REGISTRY.get("sleep")

    await send_reminder(channel, OWNER, sleep_habit, "en")

    assert channel.sent == ["⏰ Time for sleep. How did it go?"]


async def test_send_reminder_meds_uses_generic_template_thai():
    channel = FakeChannel()
    meds_habit = REGISTRY.get("meds")

    await send_reminder(channel, OWNER, meds_habit, "th")

    assert channel.sent == ["⏰ ถึงเวลายาแล้วนะ วันนี้เป็นยังไงบ้าง?"]


# ---------------------------------------------------------------------------
# 4. Weekly review inclusion -- real compute_weekly_stats/run_weekly_review,
# both new habits alongside the three built-ins.
# ---------------------------------------------------------------------------


async def test_weekly_review_includes_sleep_and_meds_alongside_builtins(db, fixed_clock):
    end_date = date(2026, 8, 19)
    channel = FakeChannel()

    # Log one of each built-in plus both new habits, all on end_date. Each
    # message gets its own mocked client so the canned extraction actually
    # matches what that message should parse to.
    await handle_inbound_message(
        "500ml",
        db=db,
        llm=make_llm(extraction("water", 500)),
        channel=channel,
        config=CONFIG,
        clock=fixed_clock,
        registry=REGISTRY,
        user_id=OWNER,
    )
    await handle_inbound_message(
        "นอน 7 ชม.",
        db=db,
        llm=make_llm(extraction("sleep", 7)),
        channel=channel,
        config=CONFIG,
        clock=fixed_clock,
        registry=REGISTRY,
        user_id=OWNER,
    )
    await handle_inbound_message(
        "took my meds",
        db=db,
        llm=make_llm(extraction("meds", True)),
        channel=channel,
        config=CONFIG,
        clock=fixed_clock,
        registry=REGISTRY,
        user_id=OWNER,
    )

    stats = compute_weekly_stats(db, CONFIG, REGISTRY, end_date, OWNER)

    assert stats.get("water").total == 500.0
    assert stats.get("sleep").total == 7.0
    assert stats.get("sleep").avg == round(7.0 / 7, 1)
    assert stats.get("meds").total == 1  # one done-day

    # Default config resolves the unprompted weekly review to Thai
    # (ROADMAP.md v0.6.0's own resolution -- core/i18n.py's
    # `resolve_unprompted_language`); the generic per-habit lines for both
    # added habits render via `stats_generic_numeric_total`/
    # `stats_generic_count_summary` (core/i18n.py), alongside the
    # byte-identical built-in water/stretch/diary blocks. SPEC-v1.2.md:
    # `run_weekly_review` now takes `lang` pre-resolved by the caller
    # (main.py's per-user fan-out job does this once per active user).
    lang_th = i18n.resolve_unprompted_language(CONFIG)
    summary_th = await run_weekly_review(
        db, CONFIG, REGISTRY, FakeLLM("เก่งมากสัปดาห์นี้!"), lang_th, OWNER, today=end_date
    )
    assert "นอนรวม: 7 ชม. เฉลี่ยต่อวัน: 1 ชม." in summary_th
    assert "บันทึกยาสัปดาห์นี้: 1 ครั้ง" in summary_th
    assert "น้ำ (มล. / เป้าหมาย / %):" in summary_th  # built-in water block still present, unchanged

    # Forced-English config: same two new-habit lines via the English side
    # of the same generic templates.
    en_config = Config(habits=CONFIG.habits, i18n={"language": "en"})
    lang_en = i18n.resolve_unprompted_language(en_config)
    summary_en = await run_weekly_review(
        db, en_config, REGISTRY, FakeLLM("Great week!"), lang_en, OWNER, today=end_date
    )
    assert "sleep total: 7 h, average/day: 1 h" in summary_en
    assert "meds entries this week: 1" in summary_en


# ---------------------------------------------------------------------------
# 5. Undo/edit where type-appropriate.
# ---------------------------------------------------------------------------


async def test_undo_sleep_numeric_with_goal(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("sleep", 7))
    await handle_inbound_message(
        "นอน 7 ชม.", db=db, llm=llm, channel=channel, config=CONFIG, clock=fixed_clock, registry=REGISTRY, user_id=OWNER
    )

    await handle_inbound_message(
        "/undo", db=db, llm=llm, channel=channel, config=CONFIG, clock=fixed_clock, registry=REGISTRY, user_id=OWNER
    )

    assert channel.sent[-1] == "↩️ Undone — removed 7 h sleep. Today: 0 / 8 h (0%)"


async def test_edit_sleep_numeric_with_goal(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("sleep", 7))
    await handle_inbound_message(
        "นอน 7 ชม.", db=db, llm=llm, channel=channel, config=CONFIG, clock=fixed_clock, registry=REGISTRY, user_id=OWNER
    )

    await handle_inbound_message(
        "make that 6h", db=db, llm=llm, channel=channel, config=CONFIG, clock=fixed_clock, registry=REGISTRY, user_id=OWNER
    )

    assert channel.sent[-1] == "✏️ Updated to 6 h — today 6 / 8 h (75%)"


async def test_undo_meds_boolean(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("meds", True))
    await handle_inbound_message(
        "took my meds", db=db, llm=llm, channel=channel, config=CONFIG, clock=fixed_clock, registry=REGISTRY, user_id=OWNER
    )

    await handle_inbound_message(
        "/undo", db=db, llm=llm, channel=channel, config=CONFIG, clock=fixed_clock, registry=REGISTRY, user_id=OWNER
    )

    assert channel.sent[-1] == "↩️ Undone — removed meds"


def test_meds_boolean_is_never_classified_as_an_edit_target():
    """SPEC-v0.7.md SS10: editing a boolean/text habit is out of scope.
    core/commands.py's unit-lookup only ever includes numeric/duration
    habits (see `_build_unit_lookup`), so no phrasing can resolve an edit
    to a boolean habit -- confirmed here with the habit's own label as the
    "unit" token, the most plausible false-positive shape."""
    assert dispatch("make that ยา", REGISTRY) is None
    assert dispatch("edit that to meds", REGISTRY) is None


# ---------------------------------------------------------------------------
# 6. Migration 004+006 backfill + owner attribution + newly-added-habit
# rows aggregate together.
# ---------------------------------------------------------------------------


def test_migration_backfilled_legacy_rows_aggregate_alongside_new_habit_rows(tmp_path):
    db_path = tmp_path / "legacy_v3.db"

    # Hand-build a v3-shaped DB (pre-v0.7 shape, no habit_type column) with
    # real legacy water rows inside the review window, exactly mirroring
    # tests/test_migrations.py's own v3-shaped fixture.
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE logs (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          ts          TEXT NOT NULL,
          category    TEXT NOT NULL,
          value_num   REAL,
          value_text  TEXT,
          raw_message TEXT NOT NULL,
          source      TEXT NOT NULL DEFAULT 'reply',
          created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          deleted_at  TEXT NULL
        );
        CREATE INDEX idx_logs_ts_cat ON logs(ts, category);
        CREATE INDEX idx_logs_category ON logs(category);
        CREATE INDEX idx_logs_deleted_at ON logs(deleted_at);
        PRAGMA user_version = 3;
        """
    )
    legacy_rows = [
        ("2026-08-15T09:00:00", "water", 500.0, None, "500ml", "reply"),
        ("2026-08-16T09:00:00", "water", 300.0, None, "300ml", "reply"),
    ]
    for ts, category, value_num, value_text, raw_message, source in legacy_rows:
        conn.execute(
            "INSERT INTO logs (ts, category, value_num, value_text, raw_message, source) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, category, value_num, value_text, raw_message, source),
        )
    conn.commit()
    conn.close()

    # Open through the real Database class -- migration 004 runs here,
    # backfilling habit_type='numeric' for the two legacy water rows.
    # SPEC-v1.1.md's additive migration 005 (habit_targets), SPEC-v1.2.md's
    # migration 006 (multi-user), SPEC-v1.3.md's migration 007 (audit_log),
    # and SPEC-v1.5.md's migration 008 (checkin_window/last_announced_
    # version) all also run now that they're part of MIGRATIONS, so a
    # v3-shaped DB opened today lands on version 9, not 4. Migration 006
    # adds `logs.user_id` as NULL for these legacy rows -- mirroring
    # `async_main`'s real startup sequence, `attribute_legacy_to_owner`
    # backfills them to OWNER (AC-M2).
    db = Database(db_path)
    assert db.schema_version_before == 3
    assert db.schema_version == 14
    db.attribute_legacy_to_owner(OWNER)

    # Now log new-habit rows (sleep, meds) through the same, now-migrated
    # DB, as if the operator upgraded to v0.7.0 config.toml and kept using
    # the bot -- exactly what "aggregating alongside" means.
    from habit_assistant.storage.models import LogEntry

    db.insert_log(LogEntry(None, OWNER, "2026-08-17T22:00:00", "sleep", 7.0, None, "นอน 7 ชม.", "reply", habit_type="numeric"))
    db.insert_log(LogEntry(None, OWNER, "2026-08-18T22:00:00", "sleep", 6.0, None, "นอน 6 ชม.", "reply", habit_type="numeric"))
    db.insert_log(LogEntry(None, OWNER, "2026-08-19T08:00:00", "meds", 1.0, None, "took my meds", "reply", habit_type="boolean"))

    stats = compute_weekly_stats(db, CONFIG, REGISTRY, date(2026, 8, 19), OWNER)

    # The migrated legacy water rows are aggregated correctly (not
    # orphaned/dropped by the migration).
    assert stats.get("water").total == 800.0  # 500 + 300, backfilled rows
    # The brand-new sleep/meds rows, added post-migration, aggregate too.
    assert stats.get("sleep").total == 13.0  # 7 + 6
    assert stats.get("meds").total == 1  # one done-day

    # Direct confirmation the backfill actually happened, not just that
    # totals coincidentally match.
    water_habit_types = {
        row["habit_type"] for row in db._conn.execute("SELECT habit_type FROM logs WHERE category = 'water'")
    }
    assert water_habit_types == {"numeric"}

    db.close()
