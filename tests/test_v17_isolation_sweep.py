"""SPEC-v1.7.md §11 verification track `sweep` (Vera-owned): the
cross-feature two-user isolation checklist, AC-S1/AC-S2.

Given user A ("OWNER") has defined a custom habit ("reading", numeric,
unit "pages"/"หน้า", goal 20) and user B ("MEMBER") has none, this file
proves -- one assertion group per surface -- that A's custom habit WORKS
and is VISIBLE ONLY TO A, and B's every surface is UNCHANGED, across every
one of the 17 surfaces SPEC-v1.7.md §8's AC-S1 enumerates verbatim:
(1) free-text/LLM extraction, (2) preparse instant logging (zero-LLM),
(3) the undo button, (4) `/edit`, (5) `/target`, (6) `/remind` + the
reminder tick, (7) streaks + milestones, (8) daily summary, (9) weekly
review + charts, (10) `/habits`, (11) `/history`, (12) `/heatmap`,
(13) `/records`, (14) `/trends`, (15) check-ins, (16) the nudge, and
(17) the dashboard -- plus AC-S2 (the per-user extraction prompt/schema
stays bounded and never leaks another user's habit) and an AC-5
two-user-angle regression check (a user with NO custom habits renders
byte-identical to v1.6 even while another user, in the SAME database, has
one).

Per SPEC-v1.7.md §11's own design, this track exercises the
ALREADY-BUILT shared-surface registry rewiring (`HabitRegistry.for_user`,
`RegistryProvider`, and the per-user `registry`/`registry_for` threading
through every consumer) by inserting `user_habits` rows DIRECTLY via
`db.add_user_habit` -- it does NOT depend on `/addhabit`/`/delhabit`
(module `habitdef`'s own, separately-owned, parallel track), so it can
run (and did run) in parallel with that track.

Live-environment rule (matches every other integration test file in this
suite, e.g. `tests/test_v16_integration.py`): every DB here is a scratch
`tmp_path` SQLite file. Nothing in this file ever opens `data/habits.db`,
and no real Telegram/Ollama call is made -- LLM calls are answered by
small local fakes, exactly like every other integration test file already
does.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import (
    checkins,
    commands,
    dashboard,
    discoverability,
    heatmap,
    history_view,
    i18n,
    nudge,
    preparse,
    records,
    reminders,
    streaks,
    trends,
    undo_ui,
)
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.registry_provider import RegistryProvider
from habit_assistant.core.review import render_weekly_review_charts, run_weekly_review
from habit_assistant.llm.prompts import build_extraction_system_prompt
from habit_assistant.main import handle_inbound_message
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "9101"  # user A -- has the custom habit "reading"
MEMBER = "9202"  # user B -- has none


# ---------------------------------------------------------------------------
# Small local fakes (per this codebase's own convention: each
# integration-adjacent test file keeps its own copy rather than importing
# another test file's fixtures -- mirrors tests/test_v16_integration.py).
# ---------------------------------------------------------------------------


class _RaisingLLM:
    """Proves a code path never touches the LLM at all (mirrors
    `tests/test_v16_integration.py::_RaisingLLM` / `tests/test_commands.py::
    _NeverCalledLLM`)."""

    async def chat_json(self, *args, **kwargs):
        raise AssertionError("LLM must never be called for this path")

    async def chat_text(self, *args, **kwargs):
        raise AssertionError("LLM must never be called for this path")


class _ReadingClaimingLLM:
    """Always claims the message is user A's custom habit "reading",
    value 20, high confidence -- regardless of which user's registry the
    caller built the prompt/schema from. This is the adversarial case
    surface (1) must survive: even if an LLM response NAMES a category
    that only exists in ANOTHER user's registry, `core/parser.py:
    _validate`'s `category not in registry.ids()` check must still reject
    it for a user whose own registry doesn't have that habit (AC-S1
    surface #1) -- the isolation boundary is enforced by the registry
    passed in, not by trusting the model's own restraint."""

    async def chat_json(self, *args, **kwargs):
        return json.dumps({"category": "reading", "value": 20, "confidence": 0.9})

    async def chat_text(self, *args, **kwargs):
        return "noted"


class _UnknownLLM:
    """A well-behaved extractor that can't classify the message at all --
    used where a message must reach the LLM (because it can't be resolved
    deterministically for the acting user) but nothing should ever be
    logged."""

    async def chat_json(self, *args, **kwargs):
        return json.dumps({"category": "unknown", "value": None, "confidence": 0.1})

    async def chat_text(self, *args, **kwargs):
        return "noted"


class _CapturingChannel(Channel):
    """Direct-call-section fake (mirrors `tests/test_v16_integration.py::
    _CapturingChannel`): overrides `send_actionable`/`send_and_pin`/
    `edit_message`/`unpin`/`send_image` so dashboard/heatmap behavior is
    actually observable."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.actionable: list[tuple[str, str, list]] = []
        self.pinned: dict[str, str] = {}
        self.edits: list[tuple[str, str, str]] = []
        self.images: list[tuple[str, bytes, str]] = []
        self._next_msg_id = 6000

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append(text)

    async def send_actionable(self, chat_id: str, text: str, buttons) -> None:
        self.actionable.append((chat_id, text, buttons))
        self.sent.append(text)

    async def send_and_pin(self, chat_id: str, text: str) -> str | None:
        self._next_msg_id += 1
        msg_id = str(self._next_msg_id)
        self.pinned[chat_id] = msg_id
        self.sent.append(text)
        return msg_id

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> bool:
        self.edits.append((chat_id, message_id, text))
        return self.pinned.get(chat_id) == message_id

    async def unpin(self, chat_id: str, message_id: str) -> None:
        if self.pinned.get(chat_id) == message_id:
            del self.pinned[chat_id]

    async def send_image(self, chat_id: str, image: bytes, caption: str) -> None:
        self.images.append((chat_id, image, caption))

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised in this file")


# ---------------------------------------------------------------------------
# Shared setup helpers.
# ---------------------------------------------------------------------------


def _seed_two_users(db: Database) -> None:
    db.upsert_user(OWNER, role="owner", status="active")
    db.upsert_user(MEMBER, role="member", status="active")


def _add_reading_habit(db: Database, user_id: str, goal: float = 20.0) -> None:
    """Inserts A's custom habit DIRECTLY via `db.add_user_habit` -- no
    `/addhabit` involved (SPEC-v1.7.md §11's own "sweep... exercises the
    shared registry rewiring by inserting user_habits rows directly")."""
    db.add_user_habit(
        user_id,
        {
            "id": "reading",
            "type": "numeric",
            "label_en": "reading",
            "label_th": "อ่านหนังสือ",
            "unit_en": "pages",
            "unit_th": "หน้า",
            "goal": goal,
            "unit_aliases": None,
        },
    )


def _registries(db: Database, config: Config) -> tuple[HabitRegistry, HabitRegistry]:
    return (
        HabitRegistry.for_user(config, db, OWNER),
        HabitRegistry.for_user(config, db, MEMBER),
    )


def _unprompted_lang(config: Config) -> i18n.Language:
    return i18n.resolve_unprompted_language(config)


def _clock(d: date, hour: int = 9, minute: int = 0):
    fixed = datetime(d.year, d.month, d.day, hour, minute, 0)
    return lambda: fixed


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    _seed_two_users(database)
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def registries(db, config):
    _add_reading_habit(db, OWNER)
    return _registries(db, config)


# ===========================================================================
# AC-S1 surface (1): free-text / LLM extraction.
# ===========================================================================


async def test_surface_1_extraction_resolves_the_custom_habit_only_for_its_owner(db, config, registries):
    registry_a, registry_b = registries

    await handle_inbound_message(
        "I spent some time on it today", db=db, llm=_ReadingClaimingLLM(), channel=_CapturingChannel(),
        config=config, registry=registry_a, user_id=OWNER,
    )
    row = db.last_log(OWNER)
    assert row is not None and row["category"] == "reading" and row["value_num"] == 20.0

    channel_b = _CapturingChannel()
    await handle_inbound_message(
        "I spent some time on it today", db=db, llm=_ReadingClaimingLLM(), channel=channel_b,
        config=config, registry=registry_b, user_id=MEMBER,
    )
    # B's OWN registry has no "reading" habit -- `core/parser.py:_validate`'s
    # `category not in registry.ids()` check rejects the LLM's claim
    # regardless of what it returned, so nothing is logged for B and B
    # gets the clarifying question instead (the isolation boundary is the
    # registry passed in, not model restraint).
    assert db.last_log(MEMBER) is None
    assert channel_b.sent == [i18n.t("clarifying_question", i18n.resolve_reply_language("x", config))]


# ===========================================================================
# AC-S2: the per-user extraction prompt/schema.
# ===========================================================================


def test_ac_s2_extraction_prompt_and_schema_are_per_user(registries):
    registry_a, registry_b = registries

    prompt_a = build_extraction_system_prompt(registry_a)
    prompt_b = build_extraction_system_prompt(registry_b)
    assert "reading" in prompt_a
    assert "reading" not in prompt_b

    assert "reading" in registry_a.category_enum()
    assert "reading" not in registry_b.category_enum()
    # Bounded: B's schema is byte-identical to the pre-v1.7 base catalog.
    assert registry_b.category_enum() == HabitRegistry.from_config(Config()).category_enum()


# ===========================================================================
# AC-S1 surface (2): preparse instant logging (zero-LLM).
# ===========================================================================


async def test_surface_2_preparse_instant_log_only_resolves_for_the_owner(db, config, registries):
    registry_a, registry_b = registries

    assert preparse.deterministic_parse("20 pages", registry_a) is not None
    assert preparse.deterministic_parse("20 pages", registry_b) is None

    channel = _CapturingChannel()
    await handle_inbound_message(
        "20 pages", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_a, user_id=OWNER
    )
    row = db.last_log(OWNER)
    assert row is not None and row["category"] == "reading" and row["value_num"] == 20.0
    # The English "goal met" confirmation template only interpolates
    # {unit}, not {label} (pre-existing i18n.py behavior, unrelated to
    # v1.7) -- "pages" is the custom habit's own, non-colliding unit, a
    # reliable isolation marker no base habit could ever produce.
    assert "pages" in channel.actionable[0][1] and "20" in channel.actionable[0][1]


# ===========================================================================
# AC-S1 surface (3): the undo button.
# ===========================================================================


async def test_surface_3_undo_describes_and_removes_the_custom_habit_only_for_its_owner(db, config, registries):
    registry_a, registry_b = registries

    channel = _CapturingChannel()
    await handle_inbound_message(
        "20 pages", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_a, user_id=OWNER
    )
    row_id = db.last_log(OWNER)["id"]

    # Text /undo, A's own row -- resolves + describes the custom habit.
    await handle_inbound_message(
        "/undo", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_a, user_id=OWNER
    )
    assert "reading" in channel.sent[-1]
    assert db.get_log(row_id)["deleted_at"] is not None

    # Button path: a SECOND custom-habit log for A, then B attempts to tap
    # its undo button (SPEC-v1.2.md R-C3's pre-existing ownership check) --
    # must be refused regardless of B's own (habit-less) registry.
    await handle_inbound_message(
        "20 pages", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_a, user_id=OWNER
    )
    row_id_2 = db.last_log(OWNER)["id"]
    channel_b = _CapturingChannel()
    await undo_ui.handle_undo_callback(
        MEMBER, f"undo:{row_id_2}", "20 pages", "cb-1", db=db, channel=channel_b, config=config,
        clock=datetime.now, registry=registry_b,
    )
    assert channel_b.sent == [i18n.t("already_undone", i18n.resolve_reply_language("20 pages", config))]
    assert db.get_log(row_id_2)["deleted_at"] is None  # never touched


# ===========================================================================
# AC-S1 surface (4): /edit.
# ===========================================================================


async def test_surface_4_edit_resolves_the_custom_unit_only_for_its_owner(db, config, registries):
    registry_a, registry_b = registries

    channel = _CapturingChannel()
    await handle_inbound_message(
        "20 pages", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_a, user_id=OWNER
    )
    # Edit never touches the LLM either -- _RaisingLLM proves it.
    await handle_inbound_message(
        "make that 15 pages", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_a,
        user_id=OWNER,
    )
    assert db.last_log(OWNER)["value_num"] == 15.0
    assert "reading" in channel.sent[-1] or "15" in channel.sent[-1]

    # For B, "pages" resolves to no habit at all -- `_parse_edit_value`
    # returns None, `dispatch` falls through to the normal parser (NOT
    # treated as an edit), so it reaches the LLM as an ordinary message.
    channel_b = _CapturingChannel()
    await handle_inbound_message(
        "make that 15 pages", db=db, llm=_UnknownLLM(), channel=channel_b, config=config, registry=registry_b,
        user_id=MEMBER,
    )
    assert db.last_log(MEMBER) is None
    assert channel_b.sent == [i18n.t("clarifying_question", i18n.resolve_reply_language("make that 15 pages", config))]


# ===========================================================================
# AC-S1 surface (5): /target.
# ===========================================================================


async def test_surface_5_target_only_resolves_the_custom_habit_for_its_owner(db, config, registries):
    registry_a, registry_b = registries

    channel = _CapturingChannel()
    await handle_inbound_message(
        "/target reading 25", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_a,
        user_id=OWNER,
    )
    assert db.get_target(OWNER, "reading") == 25.0
    assert "25" in channel.sent[-1]

    channel_b = _CapturingChannel()
    await handle_inbound_message(
        "/target reading 25", db=db, llm=_RaisingLLM(), channel=channel_b, config=config, registry=registry_b,
        user_id=MEMBER,
    )
    assert db.get_target(MEMBER, "reading") is None
    # B's own habit list (echoed in the invalid-habit reply) never
    # includes the word "reading" as a REGISTERED habit -- it stays the
    # base three.
    assert "water" in channel_b.sent[-1] and "stretch" in channel_b.sent[-1] and "diary" in channel_b.sent[-1]


# ===========================================================================
# AC-S1 surface (6): /remind + the reminder tick.
# ===========================================================================


async def test_surface_6_remind_command_only_resolves_the_custom_habit_for_its_owner(db, config, registries):
    registry_a, registry_b = registries

    channel = _CapturingChannel()
    await handle_inbound_message(
        "/remind reading 09:00", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_a,
        user_id=OWNER,
    )
    assert db.get_reminder_times(OWNER, "reading") == ["09:00"]

    channel_b = _CapturingChannel()
    await handle_inbound_message(
        "/remind reading 09:00", db=db, llm=_RaisingLLM(), channel=channel_b, config=config, registry=registry_b,
        user_id=MEMBER,
    )
    assert db.get_reminder_times(MEMBER, "reading") == []


async def test_surface_6_reminder_tick_fires_the_custom_habit_reminder_only_to_its_owner(db, config, registries):
    registry_a, registry_b = registries
    db.set_reminder_times(OWNER, "reading", ["09:00"])

    provider = RegistryProvider(config, db)
    channel = _CapturingChannel()
    base_registry = HabitRegistry.from_config(config)
    await reminders.run_due_reminders(
        channel, config, base_registry, db, clock=_clock(date(2026, 8, 24), 9, 0), registry_for=provider.for_user
    )

    lang = _unprompted_lang(config)
    expected_label = registry_a.get("reading").label(lang)
    assert any(expected_label in t for t in channel.sent)
    # No base habit's default reminder_times includes 09:00 -- only OWNER's
    # own custom-habit override fires this minute, proving MEMBER's fan-out
    # entry was never polluted by A's registry.
    assert len(channel.sent) == 1


# ===========================================================================
# AC-S1 surface (7): streaks + milestones.
# ===========================================================================


async def test_surface_7_milestone_reached_only_for_the_habit_owner(db, config, registries):
    registry_a, registry_b = registries
    channel = _CapturingChannel()

    for d in (date(2026, 8, 20), date(2026, 8, 21), date(2026, 8, 22)):
        await handle_inbound_message(
            "20 pages", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_a,
            user_id=OWNER, clock=_clock(d),
        )

    # 3-day streak crosses the default milestone [3, 7, 30] -- the fire
    # emoji suffix + the habit's own label appear in the 3rd day's own
    # confirmation.
    last_confirmation = channel.actionable[-1][1]
    assert "🔥" in last_confirmation and "reading" in last_confirmation
    assert streaks.compute_streak(db, config, registry_a.get("reading"), date(2026, 8, 22), OWNER) == 3

    # B never logged "reading" at all -- there is no Habit object to even
    # compute a streak for in B's own registry.
    assert registry_b.get("reading") is None


# ===========================================================================
# AC-S1 surface (8): daily summary.
# ===========================================================================


async def test_surface_8_daily_summary_only_mentions_the_custom_habit_for_its_owner(db, config, registries):
    registry_a, registry_b = registries
    channel = _CapturingChannel()
    today = date(2026, 8, 24)

    await handle_inbound_message(
        "20 pages", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_a,
        user_id=OWNER, clock=_clock(today),
    )
    await handle_inbound_message(
        "300ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_b,
        user_id=MEMBER, clock=_clock(today),
    )

    lang = _unprompted_lang(config)
    summary_a = streaks.run_daily_summary(db, config, registry_a, lang, OWNER, today=today)
    summary_b = streaks.run_daily_summary(db, config, registry_b, lang, MEMBER, today=today)

    expected_label = registry_a.get("reading").label(lang)
    assert expected_label in summary_a
    assert expected_label not in summary_b


# ===========================================================================
# AC-S1 surface (9): weekly review + charts.
# ===========================================================================


async def test_surface_9_weekly_review_only_includes_the_custom_habit_for_its_owner(db, config, registries):
    registry_a, registry_b = registries
    channel = _CapturingChannel()
    today = date(2026, 8, 24)

    for offset in range(3):
        d = date(2026, 8, 24 - offset)
        await handle_inbound_message(
            "20 pages", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_a,
            user_id=OWNER, clock=_clock(d),
        )
        await handle_inbound_message(
            "300ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_b,
            user_id=MEMBER, clock=_clock(d),
        )

    lang = _unprompted_lang(config)
    expected_label = registry_a.get("reading").label(lang)

    text_a = await run_weekly_review(db, config, registry_a, _ReadingClaimingLLM(), lang, OWNER, today=today)
    text_b = await run_weekly_review(db, config, registry_b, _ReadingClaimingLLM(), lang, MEMBER, today=today)

    assert expected_label in text_a
    assert expected_label not in text_b

    # Charts: never raises for either user, registry-generic either way.
    charts_a = render_weekly_review_charts(db, config, registry_a, lang, OWNER, today=today)
    charts_b = render_weekly_review_charts(db, config, registry_b, lang, MEMBER, today=today)
    assert isinstance(charts_a, list) and isinstance(charts_b, list)


# ===========================================================================
# AC-S1 surface (10): /habits.
# ===========================================================================


async def test_surface_10_habits_listing_only_shows_the_custom_habit_to_its_owner(db, config, registries):
    registry_a, registry_b = registries
    channel = _CapturingChannel()

    await handle_inbound_message(
        "/habits", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_a, user_id=OWNER
    )
    await handle_inbound_message(
        "/habits", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_b, user_id=MEMBER
    )

    habits_a, habits_b = channel.sent[-2], channel.sent[-1]
    assert "reading" in habits_a
    assert "reading" not in habits_b
    assert "water" in habits_b and "stretch" in habits_b and "diary" in habits_b


# ===========================================================================
# AC-S1 surface (11): /history.
# ===========================================================================


async def test_surface_11_history_only_shows_and_resolves_the_custom_habit_for_its_owner(db, config, registries):
    registry_a, registry_b = registries
    channel = _CapturingChannel()

    await handle_inbound_message(
        "20 pages", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_a, user_id=OWNER
    )
    await handle_inbound_message(
        "/history", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_a, user_id=OWNER
    )
    assert "reading" in channel.sent[-1]

    # B trying to filter their own /history by A's habit id gets the
    # friendly invalid-habit reply -- B's registry has no such habit, and
    # this never leaks whether ANY user has ever logged "reading".
    lang = i18n.resolve_reply_language("x", config)
    reply = history_view.render_history(
        db, config, registry_b, lang, user_id=MEMBER, category="reading", limit=None
    )
    assert reply == i18n.t(
        "history_invalid_habit", lang, habit_id="reading", habit_list=", ".join(registry_b.ids())
    )


# ===========================================================================
# AC-S1 surface (12): /heatmap.
# ===========================================================================


def test_surface_12_heatmap_only_resolves_the_custom_habit_for_its_owner(db, config, registries):
    registry_a, registry_b = registries

    assert [h.id for h in heatmap._resolve_habits(registry_a, "reading")] == ["reading"]
    assert heatmap._resolve_habits(registry_b, "reading") == []

    all_a = [h.id for h in heatmap._resolve_habits(registry_a, None)]
    all_b = [h.id for h in heatmap._resolve_habits(registry_b, None)]
    assert "reading" in all_a
    assert "reading" not in all_b


async def test_surface_12_execute_heatmap_rejects_an_unresolved_habit_for_the_non_owner(db, config, registries):
    _registry_a, registry_b = registries
    channel_b = _CapturingChannel()
    lang = i18n.resolve_reply_language("x", config)
    command = commands.Command(kind="heatmap", category="reading")

    reply = await heatmap.execute_heatmap(
        command, db=db, channel=channel_b, config=config, registry=registry_b, lang=lang, user_id=MEMBER
    )
    assert reply == i18n.t("heatmap_invalid_habit", lang, habit_id="reading", habit_list=", ".join(registry_b.ids()))
    assert channel_b.images == []  # never even attempted to render/send


# ===========================================================================
# AC-S1 surface (13): /records.
# ===========================================================================


async def test_surface_13_records_only_resolve_the_custom_habit_for_its_owner(db, config, registries):
    registry_a, registry_b = registries
    channel = _CapturingChannel()

    await handle_inbound_message(
        "20 pages", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_a, user_id=OWNER
    )
    assert db.get_record(OWNER, "reading", "best_day") == 20.0
    assert db.get_record(MEMBER, "reading", "best_day") is None  # B never has a row for a habit they don't own

    lang = i18n.resolve_reply_language("x", config)
    block_a = records.render(db, config, registry_a, lang, OWNER, habit_id="reading")
    assert "reading" in block_a

    block_b = records.render(db, config, registry_b, lang, MEMBER, habit_id="reading")
    assert block_b == i18n.t("records_invalid_habit", lang, habit_id="reading", habit_list=", ".join(registry_b.ids()))


# ===========================================================================
# AC-S1 surface (14): /trends.
# ===========================================================================


async def test_surface_14_trends_only_resolve_the_custom_habit_for_its_owner(db, config, registries):
    registry_a, registry_b = registries
    channel = _CapturingChannel()

    await handle_inbound_message(
        "20 pages", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_a, user_id=OWNER
    )

    lang = i18n.resolve_reply_language("x", config)
    line_a = trends.render(db, config, registry_a, lang, OWNER, habit_id="reading")
    assert "reading" in line_a

    line_b = trends.render(db, config, registry_b, lang, MEMBER, habit_id="reading")
    assert line_b == i18n.t("trends_invalid_habit", lang, habit_id="reading", habit_list=", ".join(registry_b.ids()))


# ===========================================================================
# AC-S1 surface (15): check-ins.
# ===========================================================================


def test_surface_15_checkin_message_only_mentions_the_custom_habit_for_its_owner(db, config, registries):
    registry_a, registry_b = registries
    lang = _unprompted_lang(config)
    expected_label = registry_a.get("reading").label(lang)

    message_a = checkins.build_checkin_message(db, config, registry_a, lang, OWNER, clock=_clock(date(2026, 8, 24)))
    message_b = checkins.build_checkin_message(db, config, registry_b, lang, MEMBER, clock=_clock(date(2026, 8, 24)))

    assert message_a is not None and expected_label in message_a
    assert message_b is None or expected_label not in message_b


# ===========================================================================
# AC-S1 surface (16): the nudge.
# ===========================================================================


def test_surface_16_nudge_message_only_mentions_the_custom_habit_for_its_owner(db, config, registries):
    registry_a, registry_b = registries
    today = date(2026, 8, 24)
    today_str = today.isoformat()
    # 18/20 = 90% -- "close" (>= threshold_pct 80%) but not yet met.
    db.insert_log(
        LogEntry(None, OWNER, f"{today_str}T09:00:00", "reading", 18.0, None, "18 pages", "reply", habit_type="numeric")
    )

    lang = _unprompted_lang(config)
    expected_label = registry_a.get("reading").label(lang)

    message_a = nudge.build_nudge_message(db, config, registry_a, lang, OWNER, clock=_clock(today))
    message_b = nudge.build_nudge_message(db, config, registry_b, lang, MEMBER, clock=_clock(today))

    assert message_a is not None and expected_label in message_a
    assert message_b is None or expected_label not in message_b


# ===========================================================================
# AC-S1 surface (17): the dashboard.
# ===========================================================================


async def test_surface_17_dashboard_only_shows_the_custom_habit_to_its_owner(db, config, registries):
    registry_a, registry_b = registries
    channel = _CapturingChannel()

    db.set_dashboard_msg_id(OWNER, await channel.send_and_pin(OWNER, "seed-a"))
    db.set_dashboard_msg_id(MEMBER, await channel.send_and_pin(MEMBER, "seed-b"))

    await handle_inbound_message(
        "20 pages", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_a, user_id=OWNER
    )
    await handle_inbound_message(
        "300ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry_b, user_id=MEMBER
    )

    # The pinned board renders in its own resolved "board language"
    # (`dashboard._board_language`, independent of the inbound message's
    # own language) -- resolve the expected label dynamically rather than
    # assuming English, mirroring every other unprompted-send assertion
    # in this file.
    board_lang = dashboard._board_language(db, config, OWNER)
    expected_label = registry_a.get("reading").label(board_lang)

    owner_edits = [t for cid, _mid, t in channel.edits if cid == OWNER]
    member_edits = [t for cid, _mid, t in channel.edits if cid == MEMBER]
    assert owner_edits and any(expected_label in t for t in owner_edits)
    assert member_edits and all(expected_label not in t for t in member_edits)


# ===========================================================================
# AC-5, two-user angle: a user with NO custom habits stays byte-identical
# to v1.6 even while ANOTHER user, in the SAME database, has one.
# ===========================================================================


async def test_ac5_member_stays_byte_identical_to_v16_while_owner_has_a_custom_habit(db, config, registries):
    registry_a, registry_b = registries

    # B's own per-user registry is untouched by A's habit -- identical ids
    # to the plain base-config registry.
    assert registry_b.ids() == HabitRegistry.from_config(config).ids()

    channel_b = _CapturingChannel()
    await handle_inbound_message(
        "500ml", db=db, llm=_RaisingLLM(), channel=channel_b, config=config, registry=registry_b, user_id=MEMBER,
        clock=lambda: datetime(2026, 8, 24, 9, 0, 0),
    )
    # The exact pre-v1.7 (v1.6) byte-identical water confirmation --
    # SPEC-v1.6.md's own confirmed literal (tests/test_v16_integration.py::
    # test_fresh_users_first_ever_log_seeds_records_silently_no_celebration).
    assert channel_b.actionable[0][1] == "✅ 500 ml logged — today 500 / 2500 ml (20%)"

    # And the reverse holds too: registry_a is base + exactly one extra
    # habit, never more, never fewer, never mutating the base habits'
    # own definitions.
    assert registry_a.ids() == [*HabitRegistry.from_config(config).ids(), "reading"]
    for base_id in ("water", "stretch", "diary"):
        assert registry_a.get(base_id) == HabitRegistry.from_config(config).get(base_id)
