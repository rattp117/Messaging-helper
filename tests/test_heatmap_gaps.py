"""SPEC-v1.6.md §4 Feature 2 "Consistency heatmap" (R-H1-R-H4, module
`heatmap`) -- Vera's adversarial gap pass on top of Luna's own
`tests/test_heatmap.py` (70 tests, all green). Every test here targets a
specific angle the coordinator flagged as under-covered by the original
suite: full Thai-alias adversarial/collision sweep, intensity-math edge
cases (target overrides, clamping, goal-less/boolean/duration branches,
real Asia/Bangkok timezone conversion), PNG robustness under a full
dataset/max-weeks/zero-logs/matplotlib-ImportError conditions, and the
nested-failure return-contract edge (both the image AND the text fallback
fail).

Conventions match `tests/test_heatmap.py` exactly (same `DEFAULT_REGISTRY`/
`OWNER`/`MEMBER`/`_seed`/`_habit`/`FakeChannel`/`PNG_MAGIC` shapes) so this
file reads as a natural continuation, not a parallel dialect.

Live-environment rule: every DB in this file is a scratch tmp_path SQLite
file. No real Telegram or Ollama call is ever made."""

from __future__ import annotations

import importlib
import sys
from datetime import date, datetime, timezone

import pytest

from habit_assistant.config import Config
from habit_assistant.core import heatmap
from habit_assistant.core.commands import Command, dispatch
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.storage.db import Database
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
# 1. Thai alias -- extended adversarial corpus + full collision sweep
#    (both directions) vs every OTHER command kind's trigger text.
# ===========================================================================

_EXTRA_ADVERSARIAL_CORPUS = [
    "ปฏิทิน ปีหน้า",  # "calendar next year" -- explicit coordinator angle
    "ปฏิทินปีหน้า",  # glued variant of the above
    "ผมดูปฏิทินแล้ว",  # mid-sentence, not message-anchored at the start
    "เปิดปฏิทินให้หน่อย",  # mid-sentence request, ordinary prose
    "วันนี้ปฏิทินจีนตรงกับ",  # mid-sentence again, different sentence shape
    "ปฏิทินนะ",  # a real trailing particle, not a habit/number
    "ปฏิทิน นะ",  # same particle, spaced
    "ปฏิทิน จ้า",  # a different ordinary Thai sentence-final particle
    "ปฏิทิน ๆ",  # bare mai-yamok, no other prose
    "555 ปฏิทิน",  # trigger word NOT at the start -- must not match
    "/heatmapwater",  # no space between slash-trigger and habit -- not the documented grammar
]


@pytest.mark.parametrize("text", _EXTRA_ADVERSARIAL_CORPUS)
def test_dispatch_extended_adversarial_corpus_never_matches_heatmap(text):
    command = dispatch(text, DEFAULT_REGISTRY)
    assert command is None or command.kind != "heatmap"


# Every OTHER command's canonical bare trigger (slash + Thai alias where it
# has one) -- direction 1: none of these must ever be classified "heatmap".
_OTHER_COMMAND_TRIGGERS = [
    ("/undo", "undo"),
    ("ยกเลิก", "undo"),
    ("ลบ", "undo"),
    ("/history", "history"),
    ("ย้อนหลัง", "history"),
    ("/audit", "audit"),
    ("ประวัติ", "audit"),
    ("/lang th", "lang"),
    ("ภาษา th", "lang"),
    ("/quiet off", "quiet"),
    ("เงียบ off", "quiet"),
    ("/checkin on", "checkin"),
    ("เช็คอิน on", "checkin"),
    ("/dnd off", "quiet"),  # R-D5: /dnd is a pure alias of /quiet, same Command(kind="quiet", ...) shape
    ("งดรบกวน off", "quiet"),
    ("/dashboard on", "dashboard"),
    ("แดชบอร์ด on", "dashboard"),
    ("/records", "records"),
    ("สถิติ", "records"),
    ("/trends", "trends"),
    ("แนวโน้ม", "trends"),
    ("/help", "help"),
    ("ช่วยเหลือ", "help"),
    ("วิธีใช้", "help"),
    ("/habits", "habits"),
    ("นิสัย", "habits"),
]


@pytest.mark.parametrize(("text", "expected_kind"), _OTHER_COMMAND_TRIGGERS)
def test_collision_sweep_other_commands_never_shadowed_as_heatmap(text, expected_kind):
    """Direction 1: no other command's own trigger text is ever
    misclassified as `heatmap`."""
    command = dispatch(text, DEFAULT_REGISTRY)
    assert command is not None
    assert command.kind == expected_kind
    assert command.kind != "heatmap"


@pytest.mark.parametrize(
    "text",
    ["ปฏิทิน", "/heatmap", "ปฏิทิน น้ำ", "/heatmap water", "ปฏิทิน 8", "/heatmap water 8"],
)
def test_collision_sweep_heatmap_is_never_shadowed_by_an_earlier_matcher(text):
    """Direction 2: every documented heatmap trigger shape actually reaches
    `_match_heatmap` and is not swallowed by an earlier-checked matcher in
    `dispatch`'s fixed order (undo/edit/snooze/target/remind/access/audit/
    lang/quiet/checkin/dnd/dashboard/history all run before heatmap)."""
    command = dispatch(text, DEFAULT_REGISTRY)
    assert command is not None
    assert command.kind == "heatmap"


# ===========================================================================
# 2. Intensity math -- goal-bearing duration, override + clamp combined,
#    goal-less rendering across habit kinds, real Asia/Bangkok timezone
#    conversion (not just naive local ts strings).
# ===========================================================================


def test_day_intensity_goal_bearing_duration_habit_uses_sum_like_numeric(db, config):
    """R-D2/R-H1's "goal-bearing" branch is type-agnostic (numeric OR
    duration, both go through `db.sum_value`) -- the original suite only
    exercised a goal-less duration habit; this locks the goal-bearing one."""
    habit = _habit("stretch", "duration", goal=20.0)
    _seed(db, OWNER, "2026-08-19T09:00:00", "stretch", 10.0)
    assert heatmap._day_intensity(db, config, habit, "2026-08-19", OWNER) == pytest.approx(0.5)


def test_day_intensity_override_and_clamp_combined(db, config):
    """A DB target override that is SMALLER than the logged total must
    still clamp at 1.0, not overflow past it -- override-respect and
    clamping are two separate code paths (`effective_goal` then `min(...,
    1.0)`) that must compose correctly together."""
    habit = _habit("water", "numeric", goal=2500.0)
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 800.0)
    db.set_target(OWNER, "water", 500.0)  # override well below both the config goal and the logged total
    intensity = heatmap._day_intensity(db, config, habit, "2026-08-19", OWNER)
    assert intensity == 1.0  # 800/500 = 1.6, clamped


def test_day_intensity_multiple_entries_sum_and_clamp(db, config):
    """A day with several separate log entries whose SUM exceeds goal must
    clamp to 1.0 -- not just a single over-goal entry (already covered) but
    the accumulation path itself."""
    habit = _habit("water", "numeric", goal=2500.0)
    _seed(db, OWNER, "2026-08-19T08:00:00", "water", 1200.0)
    _seed(db, OWNER, "2026-08-19T12:00:00", "water", 1200.0)
    _seed(db, OWNER, "2026-08-19T18:00:00", "water", 1200.0)  # sum = 3600 > 2500
    assert heatmap._day_intensity(db, config, habit, "2026-08-19", OWNER) == 1.0


def test_day_intensity_undone_entries_only_boolean_day_is_zero(db, config):
    """The coordinator's "undone-entries-only day -> 0" angle, extended to
    a boolean habit (the original suite only pinned this for numeric)."""
    habit = _habit("meds", "boolean", unit_en=None, unit_th=None)
    _seed(db, OWNER, "2026-08-19T09:00:00", "meds", 1.0, deleted=True)
    assert heatmap._day_intensity(db, config, habit, "2026-08-19", OWNER) == 0.0


def test_day_intensity_undone_entries_only_goal_bearing_day_is_zero_not_negative(db, config):
    """A day where EVERY entry was undone must read as a clean 0.0, never
    a negative number or an error, even though `sum_value`'s SQL sums over
    zero matching (non-deleted) rows -- `COALESCE(SUM(...), 0)` guards this
    at the DB layer; this test pins the guarantee through heatmap's own
    call site."""
    habit = _habit("water", "numeric", goal=2500.0)
    _seed(db, OWNER, "2026-08-19T08:00:00", "water", 1000.0, deleted=True)
    _seed(db, OWNER, "2026-08-19T18:00:00", "water", 500.0, deleted=True)
    assert heatmap._day_intensity(db, config, habit, "2026-08-19", OWNER) == 0.0


def test_today_in_timezone_converts_aware_utc_clock_to_bangkok_local_date():
    """Asia/Bangkok is UTC+7 -- an aware UTC clock just after 17:00 UTC is
    already past midnight in Bangkok (next local day). The original suite
    only exercised NAIVE clocks (treated as already-local), never a real
    timezone conversion through `zoneinfo`."""
    def clock():
        return datetime(2026, 8, 19, 18, 0, 0, tzinfo=timezone.utc)  # 01:00 on Aug 20 in Bangkok

    today = heatmap._today_in_timezone(clock, "Asia/Bangkok")
    assert today == date(2026, 8, 20)


def test_today_in_timezone_utc_clock_still_previous_bangkok_day_before_17_utc():
    """The mirror case: 16:59 UTC is still Aug 19 in Bangkok (23:59 local)
    -- the boundary must not fire one hour early."""
    def clock():
        return datetime(2026, 8, 19, 16, 59, 0, tzinfo=timezone.utc)  # 23:59 on Aug 19 in Bangkok

    today = heatmap._today_in_timezone(clock, "Asia/Bangkok")
    assert today == date(2026, 8, 19)


def test_render_grid_last_cell_follows_real_bangkok_conversion_across_midnight(db, config):
    """End-to-end: an entry logged at Bangkok-local 00:05 (just after
    midnight) must land in the grid's LAST cell when `clock()` is an aware
    UTC value that is past the Bangkok midnight boundary, and NOT bleed
    into the second-to-last cell."""
    habit = _habit("water", "numeric", goal=2500.0)
    _seed(db, OWNER, "2026-08-20T00:05:00", "water", 2500.0)  # Bangkok-local wall clock, just past midnight

    def utc_clock():
        return datetime(2026, 8, 19, 18, 0, 0, tzinfo=timezone.utc)  # 01:00 Aug 20 Bangkok

    today = heatmap._today_in_timezone(utc_clock, config.app.timezone)
    grid = heatmap._day_grid(today, 2)
    assert grid[-1][-1] == date(2026, 8, 20)
    assert heatmap._day_intensity(db, config, habit, grid[-1][-1].isoformat(), OWNER) == 1.0
    assert heatmap._day_intensity(db, config, habit, grid[-2][-1].isoformat(), OWNER) == 0.0  # Aug 19, one column back


# ===========================================================================
# 3. Weeks-param garbage at the dispatch/tail-grammar layer (beyond
#    `_effective_weeks`'s own already-thorough unit coverage).
# ===========================================================================


@pytest.mark.parametrize(
    ("text", "expected_category", "expected_weeks"),
    [
        ("/heatmap water abc", "water", None),  # non-digit tail token after a resolved habit -- ignored, not an error
        ("/heatmap -5", "-5", None),  # a signed number isn't `.isdigit()` -- falls through to the (unresolved) category slot
        ("/heatmap water 8.5", "water", None),  # a float isn't `.isdigit()` either
        ("/heatmap water 8 9", "water", 8),  # a third token is ignored, not appended/summed
    ],
)
def test_dispatch_heatmap_garbage_weeks_tail_degrades_gracefully(text, expected_category, expected_weeks):
    command = dispatch(text, DEFAULT_REGISTRY)
    assert command is not None
    assert command.kind == "heatmap"
    assert command.category == expected_category
    assert command.limit == expected_weeks


async def test_execute_heatmap_negative_weeks_token_reported_as_invalid_habit(db, config):
    """`/heatmap -5` parses to `category="-5"` (per the grammar test above)
    -- `execute_heatmap` must treat it exactly like any other unresolved
    habit token (friendly reply, no image, no crash), not attempt to use
    it as a weeks count."""
    channel = FakeChannel()
    command = dispatch("/heatmap -5", DEFAULT_REGISTRY)
    reply = await heatmap.execute_heatmap(
        command, db=db, channel=channel, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )
    assert "-5" in reply
    assert channel.images == []


# ===========================================================================
# 4. PNG robustness -- full dataset byte-identity, max-weeks size sanity,
#    zero-logs-ever, habit-filter-with-no-logs, real ImportError through
#    the EXECUTE path in both languages.
# ===========================================================================


def _seed_full_grid(db: Database, user_id: str, today: date, weeks: int, habit_ids: list[str]) -> None:
    """Populates EVERY day in the `weeks`-wide grid for every given habit
    id, so a byte-identity check exercises a fully-populated image, not
    just a single sparse entry (the coordinator's "FULL dataset" angle)."""
    grid = heatmap._day_grid(today, weeks)
    for week in grid:
        for day in week:
            for habit_id in habit_ids:
                _seed(db, user_id, f"{day.isoformat()}T09:00:00", habit_id, 100.0)


def test_render_language_has_zero_effect_with_full_multi_habit_dataset(db, config):
    """R-H3, made concrete against a FULL dataset (every cell populated,
    three habit kinds: goal-bearing numeric, goal-less duration, goal-less
    boolean/text) -- not just the original suite's single sparse entry."""
    registry = HabitRegistry(
        [
            _habit("water", "numeric", goal=2500.0),
            _habit("stretch", "duration", goal=None),
            _habit("meds", "boolean", unit_en=None, unit_th=None),
        ]
    )

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    _seed_full_grid(db, OWNER, date(2026, 8, 24), 3, ["water", "stretch", "meds"])

    image_en = heatmap.render(db, config, registry, "en", OWNER, None, 3, clock)
    image_th = heatmap.render(db, config, registry, "th", OWNER, None, 3, clock)
    assert image_en is not None and image_th is not None
    assert image_en[:8] == PNG_MAGIC
    assert image_en == image_th


def test_render_max_weeks_produces_valid_sane_sized_png(db, config):
    """`weeks=52` (the documented cap, R-H1's own `MAX_WEEKS`) must still
    render a real, valid, reasonably-sized PNG -- not time out, not
    produce a degenerate/near-empty image, not blow up in size."""
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    image = heatmap.render(db, config, DEFAULT_REGISTRY, "en", OWNER, None, 52, clock)
    assert image is not None
    assert image[:8] == PNG_MAGIC
    assert 1_000 < len(image) < 5_000_000  # sane bounds -- not a stub, not runaway


def test_render_user_with_zero_logs_ever_still_produces_a_valid_png():
    """SPEC-v1.6.md §8 doesn't carve out a special "no logs yet" state for
    `/heatmap` (unlike `heatmap_no_habits`, which fires only when the
    REGISTRY itself is empty) -- a configured-but-never-logged user gets an
    honest all-empty calendar image, the same way GitHub's own contribution
    graph shows a blank grid rather than an error. This test locks that as
    the actual (and spec-conformant) behavior, not an oversight."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "empty.db")
        try:
            def clock():
                return datetime(2026, 8, 24, 12, 0, 0)

            image = heatmap.render(db, Config(), DEFAULT_REGISTRY, "en", "9999", None, 4, clock)
            assert image is not None
            assert image[:8] == PNG_MAGIC
        finally:
            db.close()


async def test_execute_heatmap_zero_logs_ever_sends_image_not_an_error_reply(db, config):
    channel = FakeChannel()

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    reply = await heatmap.execute_heatmap(
        Command(kind="heatmap", category=None, limit=4),
        db=db, channel=channel, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id="9999", clock=clock,
    )
    assert reply == ""
    assert len(channel.images) == 1


async def test_execute_heatmap_habit_filter_with_no_logs_for_that_habit_still_renders(db, config):
    """A habit filter for a habit that IS configured but this user never
    logged must still render (an all-empty single strip), not be treated
    as an "invalid habit" -- the invalid-habit path is reserved for a
    token that doesn't resolve against the registry at all."""
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)  # OWNER logs water, never stretch
    channel = FakeChannel()

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    reply = await heatmap.execute_heatmap(
        Command(kind="heatmap", category="stretch", limit=2),
        db=db, channel=channel, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=clock,
    )
    assert reply == ""
    assert len(channel.images) == 1
    assert channel.images[0][1][:8] == PNG_MAGIC


@pytest.mark.parametrize("lang", ["en", "th"])
async def test_execute_heatmap_real_import_error_path_falls_back_in_both_languages(db, config, monkeypatch, lang):
    """The coordinator's "simulated missing matplotlib (ImportError path)
    through the real execute path" angle: hides `matplotlib`/`matplotlib.
    pyplot` from `sys.modules`, RELOADS `core.heatmap` (a genuine
    import-guard exercise, not just an attribute monkeypatch), then drives
    the reloaded module's own `execute_heatmap` end-to-end -- confirming
    the fallback text fires correctly in BOTH languages and the module
    never raises. Restores the real module afterward so it doesn't leak
    into other tests."""
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    channel = FakeChannel()

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    monkeypatch.setitem(sys.modules, "matplotlib", None)
    monkeypatch.setitem(sys.modules, "matplotlib.pyplot", None)
    try:
        reloaded = importlib.reload(heatmap)
        assert reloaded.MATPLOTLIB_AVAILABLE is False

        reply = await reloaded.execute_heatmap(
            Command(kind="heatmap", category="water", limit=2),
            db=db, channel=channel, config=config, registry=DEFAULT_REGISTRY, lang=lang, user_id=OWNER, clock=clock,
        )
        assert reply  # non-empty fallback text
        assert channel.images == []
    finally:
        monkeypatch.undo()
        importlib.reload(heatmap)
        assert heatmap.MATPLOTLIB_AVAILABLE is True


# ===========================================================================
# 5. Return contract -- the NESTED failure edge: the image path fails AND
#    the text-fallback builder itself fails. `execute_heatmap` must still
#    return a non-empty string (the bare header), never raise or return "".
# ===========================================================================


async def test_execute_heatmap_never_raises_when_both_send_image_and_fallback_text_fail(db, config, monkeypatch):
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    channel = FakeChannel(raise_on_send_image=True)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated fallback-text builder failure")

    monkeypatch.setattr(heatmap, "_build_fallback_text", boom)

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    reply = await heatmap.execute_heatmap(
        Command(kind="heatmap", category="water", limit=2),
        db=db, channel=channel, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=clock,
    )
    assert reply  # still non-empty -- the bare heatmap_fallback_header, never a crash
    assert channel.images == []


# ===========================================================================
# 6. Render-budget sanity -- captions stay short (never a runaway string
#    that would be a real render-budget concern downstream).
# ===========================================================================


async def test_execute_heatmap_caption_stays_reasonably_short(db, config):
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    channel = FakeChannel()

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    await heatmap.execute_heatmap(
        Command(kind="heatmap", category=None, limit=12),
        db=db, channel=channel, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=clock,
    )
    caption = channel.images[0][2]
    assert len(caption) < 300  # Telegram caption limit is 1024; well under it with margin to spare


# ===========================================================================
# 7. Isolation -- an extra pass at the render() (not just execute_heatmap())
#    level: two users, same habit, same day, disjoint data.
# ===========================================================================


def test_render_bytes_differ_between_two_users_with_different_data(db, config):
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    _seed(db, MEMBER, "2026-08-19T09:00:00", "water", 0.0)

    def clock():
        return datetime(2026, 8, 24, 12, 0, 0)

    image_owner = heatmap.render(db, config, DEFAULT_REGISTRY, "en", OWNER, "water", 2, clock)
    image_member = heatmap.render(db, config, DEFAULT_REGISTRY, "en", MEMBER, "water", 2, clock)
    assert image_owner is not None and image_member is not None
    assert image_owner != image_member
