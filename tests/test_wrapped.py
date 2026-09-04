"""SPEC-v1.9.md §4 Rules 21-26 (module `wrapped`, owned ACs AC25-AC29):
`core/commands.py`'s `/wrapped`/`/recap`/`สรุปเดือน`/`การ์ดสรุป` grammar,
`core/wrapped.py`'s composite-PNG card (period totals, best day, cadence-
aware streak, biggest week-over-week mover, mini heatmap strip -- all reused
from `records.py`/`streaks.py`/`trends.py`/`heatmap.py`, no new aggregation),
the no-matplotlib/render-failure bilingual text fallback, the zero-logs
friendly empty state, and the zero-asset `celebration_burst` emoji rider.

Conventions borrowed from this codebase's own precedent: `DEFAULT_REGISTRY`/
`OWNER`/`MEMBER`/`_seed`/`FakeChannel` mirror `tests/test_heatmap.py`'s
copies almost verbatim -- both modules are read-only(-ish), LLM-free,
matplotlib-optional, per-user PNG commands with an identical fail-open
contract; `core/wrapped.py`'s own docstring cites `heatmap.py` as its
structural template throughout.

Live-environment rule: every DB in this file is a scratch tmp_path SQLite
file. No real Telegram or Ollama call is ever made."""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta

import pytest

from habit_assistant.config import Config
from habit_assistant.core import wrapped
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


def _seed(db: Database, user_id: str, ts: str, category: str, value_num, raw: str = "x") -> int:
    return db.insert_log(LogEntry(None, user_id, ts, category, value_num, None, raw, "reply"))


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


def _clock_at(dt: datetime):
    def clock():
        return dt

    return clock


TODAY = datetime(2026, 8, 26, 12, 0, 0)  # a Wednesday
CLOCK = _clock_at(TODAY)


# ===========================================================================
# Shared-surface regression: wrapped adds no migration of its own (it's a
# pure read/compose layer over records/streaks/trends/heatmap's existing
# aggregation -- Rule 21's "no new aggregation" extends to "no new tables").
# ===========================================================================


def test_wrapped_adds_no_migration_of_its_own(tmp_path):
    assert len(MIGRATIONS) == 15  # 012/013 are shared-surface migrations (lifecycle, unparsed-state), not wrapped's own
    d = Database(tmp_path / "fresh.db")
    assert d.schema_version == 15
    d.close()


# ===========================================================================
# AC25 grammar -- commands.dispatch's "wrapped" shape: /wrapped [month],
# /recap alias, Thai สรุปเดือน (always month)/การ์ดสรุป (default, optional
# month tail), and the adversarial corpus (zero-LLM, deterministic).
# ===========================================================================


@pytest.mark.parametrize(
    ("text", "expected_pref"),
    [
        ("/wrapped", None),
        ("/wrapped month", "month"),
        ("/WRAPPED MONTH", "month"),  # case-insensitive slash trigger
        ("/wrapped  month", "month"),  # extra whitespace tolerated
        ("/wrapped potato", None),  # unrecognized tail -- lenient, defaults to 4w
        ("/recap", None),  # alias
        ("/recap month", "month"),
        ("/RECAP", None),
        ("สรุปเดือน", "month"),  # always month, no tail needed
        ("สรุปเดือน มาก", "month"),  # สรุปเดือน is always month, even with a trailing tail this layer doesn't parse
        ("การ์ดสรุป", None),  # generic trigger, defaults to 4w
        ("การ์ดสรุป เดือน", "month"),  # explicit Thai month tail
        ("การ์ดสรุป month", "month"),  # explicit English month tail also accepted
    ],
)
def test_dispatch_recognizes_wrapped_shape(text, expected_pref):
    command = dispatch(text, DEFAULT_REGISTRY)
    assert command is not None
    assert command.kind == "wrapped"
    assert command.pref_value == expected_pref


def test_recap_alias_routes_identically_to_wrapped():
    bare = dispatch("/wrapped", DEFAULT_REGISTRY)
    alias = dispatch("/recap", DEFAULT_REGISTRY)
    assert bare.kind == alias.kind == "wrapped"
    assert bare.pref_value == alias.pref_value is None

    with_month = dispatch("/wrapped month", DEFAULT_REGISTRY)
    alias_month = dispatch("/recap month", DEFAULT_REGISTRY)
    assert with_month.pref_value == alias_month.pref_value == "month"


_ADVERSARIAL_CORPUS = [
    "500ml",
    "ดื่มน้ำ 2 แก้ว",
    "10 min stretch",
    "felt good today",
    "wrapped please",  # no leading slash
    "please /wrapped the logs",  # not anchored at the start
    "recap please",  # no leading slash
    "การ์ดสรุปของฉัน",  # glued, no space -- must not partially match
    "สรุปเดือนที่แล้ว",  # glued, ordinary prose ("last month's summary")
]


@pytest.mark.parametrize("text", _ADVERSARIAL_CORPUS)
def test_dispatch_adversarial_corpus_never_matches_wrapped(text):
    command = dispatch(text, DEFAULT_REGISTRY)
    assert command is None or command.kind != "wrapped"


def test_other_track_commands_are_not_shadowed_by_wrapped_patterns():
    assert dispatch("/undo", DEFAULT_REGISTRY).kind == "undo"
    assert dispatch("/heatmap", DEFAULT_REGISTRY).kind == "heatmap"
    assert dispatch("/records", DEFAULT_REGISTRY).kind == "records"
    assert dispatch("/trends", DEFAULT_REGISTRY).kind == "trends"
    assert dispatch("/help", DEFAULT_REGISTRY).kind == "help"


# ===========================================================================
# _window_days -- "4w" fixed 28-day block ending today; "month" = day 1
# through today.
# ===========================================================================


def test_window_days_4w_is_28_days_ending_today():
    today = date(2026, 8, 26)
    days = wrapped._window_days(today, "4w")
    assert len(days) == 28
    assert len(set(days)) == 28
    assert days[-1] == today.isoformat()
    assert days[0] == (today - timedelta(days=27)).isoformat()


def test_window_days_month_starts_at_day_one():
    today = date(2026, 8, 5)
    days = wrapped._window_days(today, "month")
    assert days[0] == "2026-08-01"
    assert days[-1] == "2026-08-05"
    assert len(days) == 5


# ===========================================================================
# _best_day / _format_total / _streak_text / _biggest_mover -- the small
# per-habit helpers, each reusing records.py/streaks.py/trends.py math.
# ===========================================================================


def test_best_day_picks_the_highest_total_day(db, config):
    habit = _habit("water", "numeric", goal=2500.0)
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 1000.0)
    _seed(db, OWNER, "2026-08-20T09:00:00", "water", 3000.0)
    _seed(db, OWNER, "2026-08-21T09:00:00", "water", 500.0)
    best_day, best_value = wrapped._best_day(db, habit, OWNER, ["2026-08-19", "2026-08-20", "2026-08-21"])
    assert best_day == "2026-08-20"
    assert best_value == 3000.0


def test_best_day_none_when_window_is_empty(db, config):
    habit = _habit("water", "numeric", goal=2500.0)
    best_day, best_value = wrapped._best_day(db, habit, OWNER, ["2026-08-19", "2026-08-20"])
    assert best_day is None
    assert best_value == 0.0


def test_format_total_numeric_includes_unit(db, config):
    habit = _habit("water", "numeric", unit_en="ml", unit_th="มล.")
    assert wrapped._format_total(1500.0, habit, "en") == "1500 ml"


def test_format_total_boolean_uses_generic_count_key(db, config):
    habit = _habit("gym", "boolean", unit_en=None, unit_th=None)
    text = wrapped._format_total(3.0, habit, "en")
    assert "3" in text


def test_streak_text_uses_day_wording_for_non_cadence_habit(db, config):
    # "weight" (not "water"): `targets.config_goal` special-cases the id
    # "water" to always resolve from `config.reminders.water.goal_ml`
    # regardless of the `Habit` object passed in (a pre-existing v0.7
    # quirk, unrelated to this module) -- "weight" is this codebase's own
    # established goal-less-numeric test habit (mirrors `tests/
    # test_heatmap.py::test_day_intensity_goalless_numeric_any_entry`).
    habit = _habit("weight", "numeric", goal=None)
    _seed(db, OWNER, "2026-08-25T09:00:00", "weight", 70.0)
    _seed(db, OWNER, "2026-08-26T09:00:00", "weight", 70.0)
    text = wrapped._streak_text(db, config, habit, date(2026, 8, 26), OWNER, "en")
    assert "2" in text and "d" in text


def test_streak_text_uses_week_wording_for_cadence_habit(db, config):
    habit = _habit("gym", "boolean", unit_en=None, unit_th=None)
    db.set_cadence(OWNER, "gym", 3)
    for d in ("2026-08-03", "2026-08-05", "2026-08-07"):  # Mon/Wed/Fri, ISO week met
        _seed(db, OWNER, f"{d}T09:00:00", "gym", 1.0)
    text = wrapped._streak_text(db, config, habit, date(2026, 8, 9), OWNER, "en")
    assert "1" in text and "w" in text


def test_streak_text_shows_living_streak_not_zero_when_today_partial(db, config):
    """v1.3.2+line bug fix: `_streak_text` now reads through `streaks.
    display_streak` -- the recap card's "current streak" is a live glance,
    same class as the dashboard row, so a goal met the two days before
    today shouldn't read "streak 0d" just because today is still partial."""
    habit = _habit("juice", "numeric", goal=1000.0)
    _seed(db, OWNER, "2026-08-24T09:00:00", "juice", 1000.0)
    _seed(db, OWNER, "2026-08-25T09:00:00", "juice", 1000.0)
    _seed(db, OWNER, "2026-08-26T09:00:00", "juice", 500.0)  # today: partial, below goal

    text = wrapped._streak_text(db, config, habit, date(2026, 8, 26), OWNER, "en")
    assert text == "streak 2d"


def test_biggest_mover_picks_largest_pct_change(db, config):
    water = _habit("water", "numeric", goal=None)
    gym = _habit("gym", "boolean", unit_en=None, unit_th=None)
    registry = HabitRegistry([water, gym])
    # water: flat week-over-week; gym: doubled.
    for offset in range(7):
        d = (date(2026, 8, 26) - timedelta(days=offset)).isoformat()
        _seed(db, OWNER, f"{d}T09:00:00", "water", 100.0)
        d_prev = (date(2026, 8, 19) - timedelta(days=offset)).isoformat()
        _seed(db, OWNER, f"{d_prev}T09:00:00", "water", 100.0)
    for offset in (0, 2, 4):
        d = (date(2026, 8, 26) - timedelta(days=offset)).isoformat()
        _seed(db, OWNER, f"{d}T09:00:00", "gym", 1.0)
    _seed(db, OWNER, "2026-08-19T09:00:00", "gym", 1.0)

    from habit_assistant.core import trends as trends_mod

    trend_list = trends_mod.compute(db, config, registry, OWNER, _clock_at(TODAY))
    mover = wrapped._biggest_mover(trend_list)
    assert mover is not None
    assert mover.habit.id == "gym"


def test_biggest_mover_none_when_nothing_has_history():
    assert wrapped._biggest_mover([]) is None


# ===========================================================================
# AC25/AC26 -- render(): real PNG bytes, one habit, many habits + a custom
# habit, cadence-aware, per-user isolation, lang affects the image (unlike
# heatmap.py -- wrapped DRAWS bilingual text, R-H3 does not apply here).
# ===========================================================================


def test_matplotlib_is_actually_installed_in_this_venv():
    assert wrapped.MATPLOTLIB_AVAILABLE is True


def test_render_single_habit_produces_real_png_bytes(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0)])
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    image = wrapped.render(db, config, registry, "en", OWNER, "4w", CLOCK)
    assert image is not None
    assert image[:8] == PNG_MAGIC
    assert len(image) > 500


def test_render_many_habits_including_custom_and_cadence_habit(db, config):
    """AC26: registry-generic (a v1.7 custom habit) + cadence-aware (a
    habit_cadence row) all in one card."""
    registry = HabitRegistry(
        [
            _habit("water", "numeric", goal=2500.0),
            _habit("gym", "boolean", unit_en=None, unit_th=None),  # cadence habit
            _habit("mood", "text"),  # a synthetic, non-built-in habit -- AC26
        ]
    )
    db.set_cadence(OWNER, "gym", 3)
    for i in range(10):
        d = (date(2026, 8, 26) - timedelta(days=i)).isoformat()
        _seed(db, OWNER, f"{d}T09:00:00", "water", 2000.0)
        if i % 3 == 0:
            _seed(db, OWNER, f"{d}T09:00:00", "gym", 1.0)
    _seed(db, OWNER, "2026-08-20T09:00:00", "mood", None, raw="great day")

    image = wrapped.render(db, config, registry, "en", OWNER, "4w", CLOCK)
    assert image is not None and image[:8] == PNG_MAGIC


def test_render_month_period_produces_real_png_bytes(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0)])
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    image = wrapped.render(db, config, registry, "en", OWNER, "month", CLOCK)
    assert image is not None and image[:8] == PNG_MAGIC


def test_render_zero_logs_still_produces_a_png_friendly_empty_state(db, config):
    """Friendly empty state: a user with NO logs at all in the window still
    gets a real card back (never `None`, never a crash) -- `render()`
    itself only degrades to `None` for matplotlib-unavailable/registry-
    empty/render-exception, not for "nothing logged yet"."""
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0)])
    image = wrapped.render(db, config, registry, "en", OWNER, "4w", CLOCK)
    assert image is not None and image[:8] == PNG_MAGIC


def test_build_figure_zero_logs_draws_the_friendly_empty_message(db, config):
    """Object-level assertion (mirrors the task's own "test at the
    matplotlib-object level, not pixel level" instruction): the friendly
    empty-state string is a REAL Text artist on the figure, not just
    present somewhere in raw PNG bytes."""
    from habit_assistant.core import i18n as i18n_mod

    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0)])
    day_strs = wrapped._window_days(TODAY.date(), "4w")
    fig = wrapped._build_figure(db, config, registry, "en", OWNER, day_strs, "4w", TODAY.date(), CLOCK)
    try:
        all_text = " ".join(t.get_text() for ax in fig.axes for t in ax.texts)
        expected = wrapped._for_image(i18n_mod.t("wrapped_empty_period", "en"))
        assert expected in all_text
    finally:
        wrapped.plt.close(fig)


def test_build_figure_thai_labels_are_present_as_real_text_objects(db, config):
    """AC27: Thai renders as glyphs, not tofu -- proven at the matplotlib
    `Text`-object level (not pixel/byte level, which can't distinguish a
    tofu box from a real glyph): the habit's own Thai label string is
    present verbatim among the figure's `Text` artists."""
    registry = HabitRegistry(
        [
            _habit("water", "numeric", goal=2500.0, label_th="น้ำ"),
            _habit("gym", "boolean", unit_en=None, unit_th=None, label_th="ยิม"),
        ]
    )
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    _seed(db, OWNER, "2026-08-20T09:00:00", "gym", 1.0)
    day_strs = wrapped._window_days(TODAY.date(), "4w")
    fig = wrapped._build_figure(db, config, registry, "th", OWNER, day_strs, "4w", TODAY.date(), CLOCK)
    try:
        all_text = " ".join(t.get_text() for ax in fig.axes for t in ax.texts)
        assert "น้ำ" in all_text
        assert "ยิม" in all_text
        assert "สรุปของคุณ" in all_text  # the Thai card title itself
    finally:
        wrapped.plt.close(fig)


def test_build_figure_cadence_habit_shows_week_wording(db, config):
    registry = HabitRegistry([_habit("gym", "boolean", unit_en=None, unit_th=None)])
    db.set_cadence(OWNER, "gym", 3)
    for d in ("2026-08-03", "2026-08-05", "2026-08-07"):
        _seed(db, OWNER, f"{d}T09:00:00", "gym", 1.0)
    day_strs = wrapped._window_days(date(2026, 8, 9), "4w")
    fig = wrapped._build_figure(db, config, registry, "en", OWNER, day_strs, "4w", date(2026, 8, 9), CLOCK)
    try:
        all_text = " ".join(t.get_text() for ax in fig.axes for t in ax.texts)
        assert "1w" in all_text  # week-unit streak wording, not "1d"
    finally:
        wrapped.plt.close(fig)


def test_render_unresolved_registry_returns_none(db, config):
    empty_registry = HabitRegistry([])
    assert wrapped.render(db, config, empty_registry, "en", OWNER, "4w", CLOCK) is None


def test_render_lang_changes_the_image_bytes(db, config):
    """UNLIKE `core/heatmap.py` (which draws no bilingual text at all,
    R-H3), `core/wrapped.py` draws habit labels/streak/mover text directly
    onto the figure -- `lang` MUST change the rendered bytes."""
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_th="น้ำ")])
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    image_en = wrapped.render(db, config, registry, "en", OWNER, "4w", CLOCK)
    image_th = wrapped.render(db, config, registry, "th", OWNER, "4w", CLOCK)
    assert image_en != image_th


def test_render_isolated_per_user(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0)])
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    _seed(db, MEMBER, "2026-08-19T09:00:00", "water", 100.0)
    image_owner = wrapped.render(db, config, registry, "en", OWNER, "4w", CLOCK)
    image_member = wrapped.render(db, config, registry, "en", MEMBER, "4w", CLOCK)
    assert image_owner != image_member


def test_render_matplotlib_absent_returns_none_and_warns_once(db, config, monkeypatch, caplog):
    monkeypatch.setattr(wrapped, "MATPLOTLIB_AVAILABLE", False)
    monkeypatch.setattr(wrapped, "_warned_missing", False)
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0)])

    import logging

    with caplog.at_level(logging.WARNING, logger="habit_assistant.core.wrapped"):
        image1 = wrapped.render(db, config, registry, "en", OWNER, "4w", CLOCK)
        image2 = wrapped.render(db, config, registry, "en", OWNER, "4w", CLOCK)

    assert image1 is None
    assert image2 is None
    warning_records = [r for r in caplog.records if "matplotlib is not installed" in r.message]
    assert len(warning_records) == 1  # not per-call spam


def test_render_import_guard_never_raises_when_matplotlib_hidden(monkeypatch):
    import importlib
    import sys

    monkeypatch.setitem(sys.modules, "matplotlib", None)
    monkeypatch.setitem(sys.modules, "matplotlib.pyplot", None)
    try:
        reloaded = importlib.reload(wrapped)
        assert reloaded.MATPLOTLIB_AVAILABLE is False
    finally:
        monkeypatch.undo()
        importlib.reload(wrapped)
        assert wrapped.MATPLOTLIB_AVAILABLE is True


def test_render_rendering_failure_is_caught_and_returns_none(db, config, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated matplotlib failure")

    monkeypatch.setattr(wrapped, "_render_png", boom)
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0)])
    image = wrapped.render(db, config, registry, "en", OWNER, "4w", CLOCK)
    assert image is None


# ===========================================================================
# AC25/AC26/AC27/AC29 -- execute_wrapped(): bilingual caption, text
# fallback, no-habits reply, fail-open on send/render failure, celebration
# burst gating.
# ===========================================================================


async def test_execute_wrapped_no_habits_configured_returns_friendly_reply(db, config):
    empty_registry = HabitRegistry([])
    channel = FakeChannel()
    reply = await wrapped.execute_wrapped(
        Command(kind="wrapped", pref_value=None),
        db=db, channel=channel, config=config, registry=empty_registry, lang="en", user_id=OWNER,
    )
    assert reply
    assert channel.images == []


async def test_execute_wrapped_success_sends_image_and_returns_empty_string(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0), _habit("gym", "boolean", unit_en=None, unit_th=None)])
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    channel = FakeChannel()

    reply = await wrapped.execute_wrapped(
        Command(kind="wrapped", pref_value=None),
        db=db, channel=channel, config=config, registry=registry, lang="en", user_id=OWNER, clock=CLOCK,
    )

    assert reply == ""
    assert len(channel.images) == 1
    chat_id, image, caption = channel.images[0]
    assert chat_id == OWNER
    assert image[:8] == PNG_MAGIC
    assert "Water" in caption and "Gym" in caption


async def test_execute_wrapped_month_period_uses_month_caption(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0)])
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    channel = FakeChannel()

    await wrapped.execute_wrapped(
        Command(kind="wrapped", pref_value="month"),
        db=db, channel=channel, config=config, registry=registry, lang="en", user_id=OWNER, clock=CLOCK,
    )
    caption = channel.images[0][2]
    assert "August 2026" in caption


async def test_execute_wrapped_caption_is_bilingual(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_th="น้ำ")])
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)

    channel_en = FakeChannel()
    await wrapped.execute_wrapped(
        Command(kind="wrapped", pref_value=None),
        db=db, channel=channel_en, config=config, registry=registry, lang="en", user_id=OWNER, clock=CLOCK,
    )
    channel_th = FakeChannel()
    await wrapped.execute_wrapped(
        Command(kind="wrapped", pref_value=None),
        db=db, channel=channel_th, config=config, registry=registry, lang="th", user_id=OWNER, clock=CLOCK,
    )

    caption_en = channel_en.images[0][2]
    caption_th = channel_th.images[0][2]
    assert caption_en != caption_th
    assert "น้ำ" in caption_th


async def test_execute_wrapped_recap_alias_command_behaves_identically(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0)])
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)

    wrapped_cmd = dispatch("/wrapped", registry)
    recap_cmd = dispatch("/recap", registry)

    channel_a = FakeChannel()
    channel_b = FakeChannel()
    reply_a = await wrapped.execute_wrapped(
        wrapped_cmd, db=db, channel=channel_a, config=config, registry=registry, lang="en", user_id=OWNER, clock=CLOCK
    )
    reply_b = await wrapped.execute_wrapped(
        recap_cmd, db=db, channel=channel_b, config=config, registry=registry, lang="en", user_id=OWNER, clock=CLOCK
    )

    assert reply_a == reply_b == ""
    assert channel_a.images[0][1] == channel_b.images[0][1]  # byte-identical output for the same input


async def test_execute_wrapped_matplotlib_absent_falls_back_to_text_and_sends_no_image(db, config, monkeypatch):
    monkeypatch.setattr(wrapped, "MATPLOTLIB_AVAILABLE", False)
    monkeypatch.setattr(wrapped, "_warned_missing", False)
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_th="น้ำ")])
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    channel = FakeChannel()

    reply = await wrapped.execute_wrapped(
        Command(kind="wrapped", pref_value=None),
        db=db, channel=channel, config=config, registry=registry, lang="th", user_id=OWNER, clock=CLOCK,
    )

    assert reply
    assert channel.images == []
    assert "น้ำ" in reply


async def test_execute_wrapped_fallback_text_zero_logs_is_the_friendly_empty_message(db, config, monkeypatch):
    monkeypatch.setattr(wrapped, "MATPLOTLIB_AVAILABLE", False)
    monkeypatch.setattr(wrapped, "_warned_missing", False)
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0)])
    channel = FakeChannel()

    reply = await wrapped.execute_wrapped(
        Command(kind="wrapped", pref_value=None),
        db=db, channel=channel, config=config, registry=registry, lang="en", user_id=OWNER, clock=CLOCK,
    )

    assert "get started" in reply
    assert "Water" not in reply  # no misleadingly-blank per-habit line for a genuinely empty window


async def test_execute_wrapped_never_raises_when_send_image_fails(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0)])
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    channel = FakeChannel(raise_on_send_image=True)

    reply = await wrapped.execute_wrapped(
        Command(kind="wrapped", pref_value=None),
        db=db, channel=channel, config=config, registry=registry, lang="en", user_id=OWNER, clock=CLOCK,
    )

    assert reply
    assert channel.images == []


async def test_execute_wrapped_never_raises_when_render_itself_raises(db, config, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated total render-layer failure")

    monkeypatch.setattr(wrapped, "render", boom)
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0)])
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    channel = FakeChannel()

    reply = await wrapped.execute_wrapped(
        Command(kind="wrapped", pref_value=None),
        db=db, channel=channel, config=config, registry=registry, lang="en", user_id=OWNER, clock=CLOCK,
    )

    assert reply
    assert channel.images == []


async def test_execute_wrapped_isolated_per_user(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0)])
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    _seed(db, MEMBER, "2026-08-19T09:00:00", "water", 100.0)
    channel_owner = FakeChannel()
    channel_member = FakeChannel()

    await wrapped.execute_wrapped(
        Command(kind="wrapped", pref_value=None),
        db=db, channel=channel_owner, config=config, registry=registry, lang="en", user_id=OWNER, clock=CLOCK,
    )
    await wrapped.execute_wrapped(
        Command(kind="wrapped", pref_value=None),
        db=db, channel=channel_member, config=config, registry=registry, lang="en", user_id=MEMBER, clock=CLOCK,
    )

    assert channel_owner.images[0][0] == OWNER
    assert channel_member.images[0][0] == MEMBER
    assert channel_owner.images[0][1] != channel_member.images[0][1]


# ===========================================================================
# AC29 -- celebration_burst: gated by [wrapped] celebrate_burst, zero-asset.
# ===========================================================================


def test_celebration_burst_enabled_by_default_returns_the_bundled_emoji(config):
    assert config.wrapped.celebrate_burst is True
    burst = wrapped.celebration_burst(config, "en")
    assert burst == wrapped._CELEBRATION_BURST
    assert burst != ""


def test_celebration_burst_disabled_returns_empty_string(config):
    config.wrapped.celebrate_burst = False
    assert wrapped.celebration_burst(config, "en") == ""


def test_celebration_burst_is_language_agnostic(config):
    assert wrapped.celebration_burst(config, "en") == wrapped.celebration_burst(config, "th")


# ===========================================================================
# Zero-LLM proof -- SPEC-v1.9.md §4 Rule 27: "Zero-LLM everywhere". Mirrors
# tests/test_heatmap.py's own identical checks.
# ===========================================================================


def test_render_has_no_llm_dependency():
    params = inspect.signature(wrapped.render).parameters
    assert "llm" not in params


def test_execute_wrapped_has_no_llm_dependency():
    params = inspect.signature(wrapped.execute_wrapped).parameters
    assert "llm" not in params


def test_wrapped_module_never_imports_ollama():
    source = inspect.getsource(wrapped)
    assert "ollama" not in source.lower()
    assert "OllamaClient" not in source
