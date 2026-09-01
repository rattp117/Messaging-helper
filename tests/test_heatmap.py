"""SPEC-v1.6.md §4 Feature 2 "Consistency heatmap" (R-H1-R-H4, module
`heatmap`) -- the whole feature, per §11's module-owned AC list (AC-H1
through AC-H4): `commands.dispatch`'s `/heatmap`/`ปฏิทิน` grammar,
`core/heatmap.py`'s data-bucketing (goal-fulfillment intensity, target
overrides, soft-deleted/`unparsed` exclusion, per-user isolation), the
no-matplotlib text fallback (R-H2), and real PNG generation.

Conventions borrowed from this codebase's own precedent: `DEFAULT_REGISTRY`/
`OWNER`/`MEMBER`/`_seed` mirror `tests/test_history.py`'s copies (the module
this one is structurally closest to -- both are read-only, LLM-free,
per-user "tail-grammar" commands); `PNG_MAGIC`/the matplotlib-absent
simulation mirror `tests/test_charts.py`'s copies exactly (same
`MATPLOTLIB_AVAILABLE`/`_warned_missing` monkeypatch shape).

Live-environment rule: every DB in this file is a scratch tmp_path SQLite
file. No real Telegram or Ollama call is ever made."""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta

import pytest

from habit_assistant.config import Config
from habit_assistant.core import heatmap
from habit_assistant.core.commands import Command, dispatch
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.migrations import MIGRATIONS
from habit_assistant.storage.models import LogEntry

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())

OWNER = "1001"
MEMBER = "2002"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


def _seed(db: Database, user_id: str, ts: str, category: str, value_num, raw: str = "x", deleted: bool = False) -> int:
    row_id = db.insert_log(LogEntry(None, user_id, ts, category, value_num, None, raw, "reply"))
    if deleted:
        db.soft_delete(row_id)
    return row_id


def _habit(id_: str, type_: str, **kw) -> Habit:
    defaults = dict(
        label_en=id_.capitalize(),
        label_th=id_,
        unit_en="ml" if type_ in ("numeric", "duration") else None,
        unit_th="มล." if type_ in ("numeric", "duration") else None,
        goal=None,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )
    defaults.update(kw)
    return Habit(id=id_, type=type_, **defaults)


class FakeChannel:
    def __init__(self, raise_on_send_image: bool = False):
        self.images: list[tuple[str, bytes, str]] = []
        self.sent: list[tuple[str, str]] = []
        self._raise = raise_on_send_image

    async def send_image(self, chat_id: str, image: bytes, caption: str) -> None:
        if self._raise:
            raise RuntimeError("simulated transport failure")
        self.images.append((chat_id, image, caption))

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


# ===========================================================================
# AC-1/shared-surface regression: heatmap adds no migration of its own
# (it's purely derived from the existing `logs`/`habit_targets` tables, per
# SPEC-v1.6.md §2.3 "Trends/heatmap/nudge/... derived ... no new storage").
# ===========================================================================


def test_heatmap_adds_no_migration_of_its_own(tmp_path):
    assert len(MIGRATIONS) == 15  # migration 009 is shared-surface (dashboard/records); 010/011/012/013 are habitdef/routines/lifecycle/unparsed-state, not heatmap's
    db = Database(tmp_path / "fresh.db")
    assert db.schema_version == 15
    db.close()


# ===========================================================================
# AC-H1 grammar -- commands.dispatch's "heatmap" shape, incl. the Thai
# alias and the adversarial corpus (zero-LLM, deterministic).
# ===========================================================================


@pytest.mark.parametrize(
    ("text", "expected_category", "expected_weeks"),
    [
        ("/heatmap", None, None),
        ("/heatmap 8", None, 8),
        ("/heatmap water", "water", None),
        ("/heatmap water 8", "water", 8),
        ("/HEATMAP water 8", "water", 8),  # case-insensitive slash trigger
        ("/heatmap  water  8", "water", 8),  # extra whitespace tolerated
        ("/heatmap coffee", "coffee", None),  # unresolved habit -- carried through raw, execute_heatmap's job
        ("/heatmap coffee 5", "coffee", 5),
        ("ปฏิทิน", None, None),
        ("ปฏิทิน 8", None, 8),
        ("ปฏิทิน น้ำ", "water", None),
        ("ปฏิทิน น้ำ 8", "water", 8),
    ],
)
def test_dispatch_recognizes_heatmap_shape(text, expected_category, expected_weeks):
    command = dispatch(text, DEFAULT_REGISTRY)
    assert command is not None
    assert command.kind == "heatmap"
    assert command.category == expected_category
    assert command.limit == expected_weeks


_ADVERSARIAL_CORPUS = [
    "500ml",
    "ดื่มน้ำ 2 แก้ว",
    "10 min stretch",
    "did 10 min stretch",
    "felt good today",
    "เลิกงานแล้ว เหนื่อยมาก",
    "heatmap please",  # no leading slash
    "please /heatmap the logs",  # not anchored at the start
    "ปฏิทิน ๆ หน่อยนะ",  # mai-yamok reduplication, ordinary prose
    "ปฏิทินจีนปีนี้",  # glued, ordinary prose ("this year's Chinese calendar")
    "ปฏิทินของฉัน",  # glued, no space
    "ปฏิทิน มาก",  # a real word ("very") that isn't a habit or a number
    "ปฏิทิน 3 เดือนที่แล้ว",  # digits followed by more prose -- must NOT partially match
]


@pytest.mark.parametrize("text", _ADVERSARIAL_CORPUS)
def test_dispatch_adversarial_corpus_never_matches_heatmap(text):
    command = dispatch(text, DEFAULT_REGISTRY)
    assert command is None or command.kind != "heatmap"


def test_other_track_commands_are_not_shadowed_by_heatmap_patterns():
    assert dispatch("/undo", DEFAULT_REGISTRY).kind == "undo"
    assert dispatch("/target water 2000", DEFAULT_REGISTRY).kind == "target"
    assert dispatch("/history water 5", DEFAULT_REGISTRY).kind == "history"
    assert dispatch("/audit", DEFAULT_REGISTRY).kind == "audit"
    assert dispatch("/help", DEFAULT_REGISTRY).kind == "help"
    assert dispatch("how much water this week?", DEFAULT_REGISTRY).kind == "query"


def test_heatmap_thai_alias_does_not_collide_with_history_or_audit_thai_aliases():
    assert dispatch("ปฏิทิน", DEFAULT_REGISTRY).kind == "heatmap"
    assert dispatch("ย้อนหลัง", DEFAULT_REGISTRY).kind == "history"
    assert dispatch("ประวัติ", DEFAULT_REGISTRY).kind == "audit"


# ===========================================================================
# _effective_weeks -- default 12 / cap 52.
# ===========================================================================


@pytest.mark.parametrize(
    ("weeks", "expected"),
    [(None, 12), (0, 12), (-5, 12), (1, 1), (12, 12), (52, 52), (53, 52), (9999, 52)],
)
def test_effective_weeks_default_and_cap(weeks, expected):
    assert heatmap._effective_weeks(weeks) == expected


def test_day_grid_shape_and_ends_at_today():
    today = date(2026, 8, 24)
    grid = heatmap._day_grid(today, 3)
    assert len(grid) == 3
    assert all(len(week) == 7 for week in grid)
    assert grid[-1][-1] == today
    flat = heatmap._flatten_day_strs(grid)
    assert len(flat) == 21
    assert len(set(flat)) == 21  # no duplicate days
    assert flat[-1] == today.isoformat()
    assert flat[0] == (today - timedelta(days=20)).isoformat()


# ===========================================================================
# AC-X1/AC-X3-adjacent -- data bucketing correctness: goal-fulfillment
# intensity, target overrides, soft-deleted/unparsed exclusion, per-user
# isolation. Reuses the existing db.sum_value/count/count_true aggregation
# (same as core/streaks.py:day_qualifies), so these tests double as a
# regression guard that heatmap didn't invent a second, divergent
# aggregation path.
# ===========================================================================


def test_day_intensity_goal_bearing_is_fraction_of_effective_goal(db, config):
    habit = _habit("water", "numeric", goal=2500.0)
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 1250.0)
    intensity = heatmap._day_intensity(db, config, habit, "2026-08-19", OWNER)
    assert intensity == pytest.approx(0.5)


def test_day_intensity_clamps_at_1_when_goal_exceeded(db, config):
    habit = _habit("water", "numeric", goal=2500.0)
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 5000.0)
    intensity = heatmap._day_intensity(db, config, habit, "2026-08-19", OWNER)
    assert intensity == 1.0


def test_day_intensity_zero_when_nothing_logged(db, config):
    habit = _habit("water", "numeric", goal=2500.0)
    assert heatmap._day_intensity(db, config, habit, "2026-08-19", OWNER) == 0.0


def test_day_intensity_boolean_any_truthy_entry(db, config):
    habit = _habit("meds", "boolean", unit_en=None, unit_th=None)
    _seed(db, OWNER, "2026-08-19T09:00:00", "meds", 1.0)
    assert heatmap._day_intensity(db, config, habit, "2026-08-19", OWNER) == 1.0


def test_day_intensity_boolean_falsy_only_is_zero(db, config):
    habit = _habit("meds", "boolean", unit_en=None, unit_th=None)
    _seed(db, OWNER, "2026-08-19T09:00:00", "meds", 0.0)
    assert heatmap._day_intensity(db, config, habit, "2026-08-19", OWNER) == 0.0


def test_day_intensity_goalless_numeric_any_entry(db, config):
    habit = _habit("weight", "numeric", goal=None)
    _seed(db, OWNER, "2026-08-19T09:00:00", "weight", 70.0)
    assert heatmap._day_intensity(db, config, habit, "2026-08-19", OWNER) == 1.0


def test_day_intensity_duration_goalless_uses_count(db, config):
    habit = _habit("stretch", "duration", goal=None)
    _seed(db, OWNER, "2026-08-19T09:00:00", "stretch", 10.0)
    assert heatmap._day_intensity(db, config, habit, "2026-08-19", OWNER) == 1.0


def test_day_intensity_respects_db_target_override(db, config):
    """SPEC-v1.6.md task-brief: "goal-bearing: % of effective goal via
    targets.effective_goal" -- a `/target` override must change the
    intensity fraction immediately, same as every other goal-consuming
    module (R-T11)."""
    habit = _habit("water", "numeric", goal=2500.0)
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 1000.0)
    before = heatmap._day_intensity(db, config, habit, "2026-08-19", OWNER)
    db.set_target(OWNER, "water", 1000.0)
    after = heatmap._day_intensity(db, config, habit, "2026-08-19", OWNER)
    assert before == pytest.approx(0.4)  # 1000/2500
    assert after == 1.0  # 1000/1000


def test_day_intensity_excludes_soft_deleted_rows(db, config):
    habit = _habit("water", "numeric", goal=2500.0)
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0, deleted=True)
    assert heatmap._day_intensity(db, config, habit, "2026-08-19", OWNER) == 0.0


def test_day_intensity_excludes_unparsed_rows(db, config):
    """A row category='unparsed' can never contribute to any real habit's
    total -- `db.sum_value`/`count`/`count_true` filter by exact category,
    so this is really a regression pin on that shared behavior."""
    habit = _habit("water", "numeric", goal=2500.0)
    _seed(db, OWNER, "2026-08-19T09:00:00", "unparsed", None, raw="garbled")
    assert heatmap._day_intensity(db, config, habit, "2026-08-19", OWNER) == 0.0


def test_day_intensity_isolated_per_user(db, config):
    habit = _habit("water", "numeric", goal=2500.0)
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    _seed(db, MEMBER, "2026-08-19T09:00:00", "water", 100.0)
    assert heatmap._day_intensity(db, config, habit, "2026-08-19", OWNER) == 1.0
    assert heatmap._day_intensity(db, config, habit, "2026-08-19", MEMBER) == pytest.approx(0.04)


def test_day_intensity_day_boundary_excludes_adjacent_days(db, config):
    """Data-bucketing correctness at the day boundary: an entry at
    23:59:59 belongs to ITS day only, and one at 00:00:00 the next day
    belongs to the next -- neither leaks into the other's bucket."""
    habit = _habit("water", "numeric", goal=2500.0)
    _seed(db, OWNER, "2026-08-19T23:59:59", "water", 2500.0)
    _seed(db, OWNER, "2026-08-20T00:00:00", "water", 2500.0)
    assert heatmap._day_intensity(db, config, habit, "2026-08-19", OWNER) == 1.0
    assert heatmap._day_intensity(db, config, habit, "2026-08-20", OWNER) == 1.0
    assert heatmap._day_intensity(db, config, habit, "2026-08-18", OWNER) == 0.0
    assert heatmap._day_intensity(db, config, habit, "2026-08-21", OWNER) == 0.0


# ===========================================================================
# AC-X1 registry-generic: an extra configured habit shows up automatically.
# ===========================================================================


def test_render_includes_every_registry_habit_with_no_filter(db, config):
    registry = HabitRegistry(
        [
            _habit("water", "numeric", goal=2500.0),
            _habit("mood", "text"),  # a synthetic, non-built-in habit -- AC-X1
        ]
    )
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    _seed(db, OWNER, "2026-08-19T09:00:00", "mood", None)

    def clock():
        return datetime(2026, 8, 19, 12, 0, 0)

    image = heatmap.render(db, config, registry, "en", OWNER, None, 2, clock)
    assert image is not None and image[:8] == PNG_MAGIC
    habits = heatmap._resolve_habits(registry, None)
    assert [h.id for h in habits] == ["water", "mood"]


# ===========================================================================
# AC-H1/AC-H2/AC-H3 -- render(): real PNG bytes, matplotlib-absent
# fallback, and language has zero effect on the image itself (R-H3's own
# "no bilingual text in the PNG" guarantee, made concrete: identical
# inputs at two different `lang`s must byte-for-byte match).
# ===========================================================================


def test_matplotlib_is_actually_installed_in_this_venv():
    assert heatmap.MATPLOTLIB_AVAILABLE is True


def test_render_single_habit_produces_real_png_bytes(db, config):
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    image = heatmap.render(db, config, DEFAULT_REGISTRY, "en", OWNER, "water", 4, clock)
    assert image is not None
    assert image[:8] == PNG_MAGIC
    assert len(image) > 500  # not a stub/placeholder-sized blob


def test_render_all_habits_produces_real_png_bytes(db, config):
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    image = heatmap.render(db, config, DEFAULT_REGISTRY, "en", OWNER, None, None, clock)
    assert image is not None
    assert image[:8] == PNG_MAGIC


def test_render_unresolved_habit_id_returns_none(db, config):
    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    assert heatmap.render(db, config, DEFAULT_REGISTRY, "en", OWNER, "coffee", None, clock) is None


def test_render_language_has_zero_effect_on_image_bytes(db, config):
    """R-H3, made concrete: with every other input held fixed, `lang` must
    not change a single byte of the PNG -- proves no bilingual text (which
    WOULD differ between "en"/"th") is ever drawn into the image."""
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    image_en = heatmap.render(db, config, DEFAULT_REGISTRY, "en", OWNER, "water", 4, clock)
    image_th = heatmap.render(db, config, DEFAULT_REGISTRY, "th", OWNER, "water", 4, clock)
    assert image_en == image_th


def test_render_matplotlib_absent_returns_none_and_warns_once(db, config, monkeypatch, caplog):
    monkeypatch.setattr(heatmap, "MATPLOTLIB_AVAILABLE", False)
    monkeypatch.setattr(heatmap, "_warned_missing", False)

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    import logging

    with caplog.at_level(logging.WARNING, logger="habit_assistant.core.heatmap"):
        image1 = heatmap.render(db, config, DEFAULT_REGISTRY, "en", OWNER, "water", None, clock)
        image2 = heatmap.render(db, config, DEFAULT_REGISTRY, "en", OWNER, "water", None, clock)

    assert image1 is None
    assert image2 is None
    warning_records = [r for r in caplog.records if "matplotlib is not installed" in r.message]
    assert len(warning_records) == 1  # not per-call spam


def test_render_import_guard_never_raises_when_matplotlib_hidden(monkeypatch):
    """AC-H2's "actually fires gracefully" claim at the import-guard level
    itself, mirrors tests/test_charts.py's identical module-reload check."""
    import importlib
    import sys

    monkeypatch.setitem(sys.modules, "matplotlib", None)
    monkeypatch.setitem(sys.modules, "matplotlib.pyplot", None)
    try:
        reloaded = importlib.reload(heatmap)
        assert reloaded.MATPLOTLIB_AVAILABLE is False
    finally:
        monkeypatch.undo()
        importlib.reload(heatmap)
        assert heatmap.MATPLOTLIB_AVAILABLE is True


def test_render_rendering_failure_is_caught_and_returns_none(db, config, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated matplotlib failure")

    monkeypatch.setattr(heatmap, "_render_png", boom)

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    image = heatmap.render(db, config, DEFAULT_REGISTRY, "en", OWNER, "water", None, clock)
    assert image is None


# ===========================================================================
# AC-H1/AC-H2/AC-H3 -- execute_heatmap(): invalid habit, successful
# send_image (bilingual caption), fallback text, fail-open on send/render
# failure.
# ===========================================================================


async def test_execute_heatmap_invalid_habit_returns_friendly_reply_and_sends_no_image(db, config):
    channel = FakeChannel()
    command = Command(kind="heatmap", category="coffee", limit=None)

    reply = await heatmap.execute_heatmap(
        command, db=db, channel=channel, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )

    assert "coffee" in reply
    assert channel.images == []


async def test_execute_heatmap_success_sends_image_and_returns_empty_string(db, config):
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    channel = FakeChannel()
    command = Command(kind="heatmap", category="water", limit=4)

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    reply = await heatmap.execute_heatmap(
        command, db=db, channel=channel, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=clock
    )

    assert reply == ""
    assert len(channel.images) == 1
    chat_id, image, caption = channel.images[0]
    assert chat_id == OWNER
    assert image[:8] == PNG_MAGIC
    assert "water" in caption.lower() or "Water" in caption
    assert "4" in caption


async def test_execute_heatmap_caption_is_bilingual(db, config):
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    channel_en = FakeChannel()
    await heatmap.execute_heatmap(
        Command(kind="heatmap", category="water", limit=4),
        db=db, channel=channel_en, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=clock,
    )
    channel_th = FakeChannel()
    await heatmap.execute_heatmap(
        Command(kind="heatmap", category="water", limit=4),
        db=db, channel=channel_th, config=config, registry=DEFAULT_REGISTRY, lang="th", user_id=OWNER, clock=clock,
    )

    caption_en = channel_en.images[0][2]
    caption_th = channel_th.images[0][2]
    assert caption_en != caption_th
    assert "น้ำ" in caption_th  # Thai label appears in the CAPTION (fine -- only the image itself must avoid Thai)


async def test_execute_heatmap_multi_habit_caption_lists_every_habit(db, config):
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    channel = FakeChannel()
    await heatmap.execute_heatmap(
        Command(kind="heatmap", category=None, limit=2),
        db=db, channel=channel, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=clock,
    )
    caption = channel.images[0][2]
    for habit in DEFAULT_REGISTRY:
        assert habit.label_en in caption


async def test_execute_heatmap_matplotlib_absent_falls_back_to_text_and_sends_no_image(db, config, monkeypatch):
    monkeypatch.setattr(heatmap, "MATPLOTLIB_AVAILABLE", False)
    monkeypatch.setattr(heatmap, "_warned_missing", False)
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    channel = FakeChannel()

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    reply = await heatmap.execute_heatmap(
        Command(kind="heatmap", category="water", limit=2),
        db=db, channel=channel, config=config, registry=DEFAULT_REGISTRY, lang="th", user_id=OWNER, clock=clock,
    )

    assert reply  # non-empty friendly text
    assert channel.images == []
    assert "น้ำ" in reply  # bilingual fallback, per lang


async def test_execute_heatmap_never_raises_when_send_image_fails(db, config):
    """R-3.4/AC-H2's "never crashes" posture extended to the delivery step:
    a transport failure inside send_image must still leave the user with
    the text fallback, not an unhandled exception."""
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    channel = FakeChannel(raise_on_send_image=True)

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    reply = await heatmap.execute_heatmap(
        Command(kind="heatmap", category="water", limit=2),
        db=db, channel=channel, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=clock,
    )

    assert reply  # fell back to the text summary instead of raising
    assert channel.images == []


async def test_execute_heatmap_never_raises_when_render_itself_raises(db, config, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated total render-layer failure")

    monkeypatch.setattr(heatmap, "render", boom)
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    channel = FakeChannel()

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    reply = await heatmap.execute_heatmap(
        Command(kind="heatmap", category="water", limit=2),
        db=db, channel=channel, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=clock,
    )

    assert reply
    assert channel.images == []


async def test_execute_heatmap_no_habits_configured_returns_friendly_reply(db, config):
    empty_registry = HabitRegistry([])
    channel = FakeChannel()

    reply = await heatmap.execute_heatmap(
        Command(kind="heatmap", category=None, limit=None),
        db=db, channel=channel, config=config, registry=empty_registry, lang="en", user_id=OWNER,
    )

    assert reply
    assert channel.images == []


async def test_execute_heatmap_isolated_per_user(db, config):
    """AC-X3: user A's heatmap reflects only A's data."""
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    _seed(db, MEMBER, "2026-08-19T09:00:00", "water", 100.0)
    channel_owner = FakeChannel()
    channel_member = FakeChannel()

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    await heatmap.execute_heatmap(
        Command(kind="heatmap", category="water", limit=1),
        db=db, channel=channel_owner, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=clock,
    )
    await heatmap.execute_heatmap(
        Command(kind="heatmap", category="water", limit=1),
        db=db, channel=channel_member, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=MEMBER, clock=clock,
    )

    assert channel_owner.images[0][0] == OWNER
    assert channel_member.images[0][0] == MEMBER
    # Different underlying data -> different rendered bytes (owner met the
    # goal, member barely logged anything).
    assert channel_owner.images[0][1] != channel_member.images[0][1]


# ===========================================================================
# Zero-LLM proof -- SPEC-v1.6.md §1: "zero Ollama calls anywhere in these
# five features" (R-X2). Mirrors tests/test_history.py's own
# `test_render_history_is_synchronous_and_has_no_llm_or_channel_dependency`.
# ===========================================================================


def test_render_has_no_llm_dependency():
    params = inspect.signature(heatmap.render).parameters
    assert "llm" not in params


def test_execute_heatmap_has_no_llm_dependency():
    params = inspect.signature(heatmap.execute_heatmap).parameters
    assert "llm" not in params


def test_heatmap_module_never_imports_ollama():
    source = inspect.getsource(heatmap)
    assert "ollama" not in source.lower()
    assert "OllamaClient" not in source
