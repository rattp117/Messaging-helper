"""Adversarial / gap-filling tests for v1.9.0 module `wrapped`
(SPEC-v1.9.md §4 Rules 21-26, AC25-AC29), written by Vera on top of
Luna's own `tests/test_wrapped.py` (65 tests, all green as of this
session's full-suite run). This file probes corners Luna's own suite
left thin per the test brief:

  - card composition: window-boundary exactness, cross-user/registry
    leak resistance through the REAL `RegistryProvider` pipeline
    (custom + archived habits), the window-scoped "best day" judgment
    call (locked in as a documented behavior, not just prose).
  - Thai rendering: the bundled Noto Sans Thai font is genuinely
    registered in matplotlib's own font manager (not just that a Thai
    `Text` object exists), and a geometry-level regression test for
    Luna's self-caught canvas-clipping bug (IMPL-v1.9-wrapped.md's
    "Known limitations"/smoke-test note).
  - the no-matplotlib text fallback: alias/Thai-trigger equivalence
    through THAT path specifically (Luna's own equivalence test only
    exercises the PNG path), a bilingual EN/TH diff, a practical
    "many habits" safety check (there is no `render_budget` call in
    this path -- confirmed to mirror `core/heatmap.py:_build_fallback_
    text`'s own identical, pre-existing lack of one, not a regression
    introduced here), and a stronger adversarial corpus.
  - `celebration_burst`'s append-composition contract.

Live-environment rule: every DB in this file is a scratch tmp_path
SQLite file. No real Telegram or Ollama call is ever made."""

from __future__ import annotations

import inspect
from datetime import datetime

import pytest

from habit_assistant.config import Config
from habit_assistant.core import wrapped
from habit_assistant.core.commands import Command, dispatch
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.registry_provider import RegistryProvider
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

REGISTRY = HabitRegistry.from_config(Config())

OWNER = "9001"
MEMBER = "9002"

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
# 4-week window: boundary correctness, backfilled logs, logs today.
# ===========================================================================


def test_4w_window_excludes_the_day_before_the_28_day_boundary(db, config):
    """SPEC-v1.9.md Rule 21 / `_window_days`'s own docstring: "4w" is a
    FIXED 28-day block ending at (and including) today -- 2026-07-30 is
    the FIRST day in the window (today minus 27 days); 2026-07-29 must
    NOT be counted. Not ISO-week-aligned (confirmed by reading
    `_window_days`'s own docstring, which explicitly rejects that
    design) -- this test proves the boundary against real card content,
    not just the `_window_days` unit list Luna's own suite already
    covers."""
    habit = _habit("gym", "boolean", unit_en=None, unit_th=None)
    registry = HabitRegistry([habit])
    _seed(db, OWNER, "2026-07-29T09:00:00", "gym", 1.0)  # one day BEFORE the window
    _seed(db, OWNER, "2026-07-30T09:00:00", "gym", 1.0)  # first day IN the window
    _seed(db, OWNER, "2026-08-26T09:00:00", "gym", 1.0)  # today, last day IN the window

    day_strs = wrapped._window_days(TODAY.date(), "4w")
    assert "2026-07-29" not in day_strs
    assert "2026-07-30" in day_strs
    assert "2026-08-26" in day_strs
    assert len(day_strs) == 28

    fig = wrapped._build_figure(db, config, registry, "en", OWNER, day_strs, "4w", TODAY.date(), CLOCK)
    try:
        all_text = " ".join(t.get_text() for ax in fig.axes for t in ax.texts)
        assert "2 entries" in all_text  # only the 07-30 and 08-26 logs count
        assert "3 entries" not in all_text
    finally:
        wrapped.plt.close(fig)


def test_backfilled_log_counts_by_its_own_date_not_insertion_recency(db, config):
    """A `/backfill`-style entry (v1.8) is just a `logs` row with a past
    `ts` -- `period_total` sums by the day string in `day_strs`, with no
    notion of "when was this row inserted". A backfilled entry dated
    INSIDE the window counts; one dated OUTSIDE it (even though inserted
    at the same wall-clock moment, right before this test's own render
    call) does not."""
    habit = _habit("gym", "boolean", unit_en=None, unit_th=None)
    registry = HabitRegistry([habit])
    _seed(db, OWNER, "2026-08-10T09:00:00", "gym", 1.0)  # backfilled, INSIDE the window
    _seed(db, OWNER, "2026-07-01T09:00:00", "gym", 1.0)  # backfilled, OUTSIDE the window

    day_strs = wrapped._window_days(TODAY.date(), "4w")
    fig = wrapped._build_figure(db, config, registry, "en", OWNER, day_strs, "4w", TODAY.date(), CLOCK)
    try:
        all_text = " ".join(t.get_text() for ax in fig.axes for t in ax.texts)
        assert "1 entries" in all_text
    finally:
        wrapped.plt.close(fig)


def test_best_day_is_window_scoped_not_lifetime_record(db, config):
    """Documents and locks in Luna's own judgment call (IMPL-v1.9-
    wrapped.md "Known limitations": SPEC-v1.9.md Rule 21 lists "best
    day" among the card's pieces without specifying window-scoped vs.
    lifetime-record semantics). A day OUTSIDE the window with a far
    higher total than anything inside it must NOT be picked -- proving
    `_best_day` only ever looks at the passed-in `day_strs` (the window),
    never `db.get_record(..., "best_day")` (the lifetime record `/records`
    tracks).

    Vera's ruling: Rule 21's own text -- "It reuses records.period_total
    ... no new aggregation" -- names `period_total`, not `db.get_record`,
    as the reuse target, so this reading is a reasonable, spec-consistent
    interpretation of a genuinely underspecified point. PASS, with a note
    flagged to Archi (see TEST-v1.9-wrapped.md) in case the intended
    product behavior was actually the all-time record."""
    habit = _habit("water", "numeric", goal=None)
    _seed(db, OWNER, "2026-06-01T09:00:00", "water", 999999.0)  # huge, OUTSIDE any 4w/month window
    _seed(db, OWNER, "2026-08-20T09:00:00", "water", 500.0)  # inside the window -- the real max there

    day_strs = wrapped._window_days(TODAY.date(), "4w")
    best_day, best_value = wrapped._best_day(db, habit, OWNER, day_strs)
    assert best_day == "2026-08-20"
    assert best_value == 500.0


# ===========================================================================
# Card composition: registry-generic + per-user isolation through the REAL
# RegistryProvider pipeline (custom habits, archived habits) -- not just a
# hand-built HabitRegistry, which is all Luna's own suite exercises.
# ===========================================================================


def test_custom_habit_via_registry_provider_appears_and_never_leaks_across_users(db, config):
    """AC26: registry-generic via the actual `RegistryProvider.for_user`
    pipeline (v1.7's own per-user custom-habit machinery), plus per-user
    isolation -- user B's own registry never contains user A's custom
    habit at all (the FIRST, stronger guarantee: no leak even at the
    registry-construction level), and rendering A's vs B's card never
    produces the same bytes even for a habit id both COULD define."""
    provider = RegistryProvider(config, db)
    db.add_user_habit(
        OWNER,
        dict(
            id="journal", type="text", label_en="Journal", label_th="ไดอารี่",
            unit_en=None, unit_th=None, goal=None, unit_aliases="{}",
        ),
    )
    provider.invalidate(OWNER)
    registry_a = provider.for_user(OWNER)
    registry_b = provider.for_user(MEMBER)

    assert registry_a.get("journal") is not None
    assert registry_b.get("journal") is None  # never leaks into B's own registry construction

    _seed(db, OWNER, "2026-08-20T09:00:00", "journal", None, raw="secret entry")

    day_strs = wrapped._window_days(TODAY.date(), "4w")
    fig_a = wrapped._build_figure(db, config, registry_a, "en", OWNER, day_strs, "4w", TODAY.date(), CLOCK)
    try:
        text_a = " ".join(t.get_text() for ax in fig_a.axes for t in ax.texts)
        assert "Journal" in text_a
    finally:
        wrapped.plt.close(fig_a)

    image_a = wrapped.render(db, config, registry_a, "en", OWNER, "4w", CLOCK)
    image_b = wrapped.render(db, config, registry_b, "en", MEMBER, "4w", CLOCK)
    assert image_a != image_b


def test_archived_habit_is_excluded_from_the_card(db, config):
    """A habit the user archived (v1.7 soft-delete, `db.archive_user_
    habit`) drops out of `HabitRegistry.for_user` entirely on the next
    `provider.invalidate` rebuild -- `wrapped` only ever iterates
    `list(registry)` (never reads `user_habits` itself), so an archived
    habit's label/data can never appear on the card, per Rule 21's own
    "the acting user's active registry" framing."""
    provider = RegistryProvider(config, db)
    db.add_user_habit(
        OWNER,
        dict(
            id="oldhabit", type="boolean", label_en="OldHabit", label_th="เก่า",
            unit_en=None, unit_th=None, goal=None, unit_aliases="{}",
        ),
    )
    provider.invalidate(OWNER)
    _seed(db, OWNER, "2026-08-20T09:00:00", "oldhabit", 1.0)

    db.archive_user_habit(OWNER, "oldhabit")
    provider.invalidate(OWNER)
    registry = provider.for_user(OWNER)
    assert registry.get("oldhabit") is None

    day_strs = wrapped._window_days(TODAY.date(), "4w")
    fig = wrapped._build_figure(db, config, registry, "en", OWNER, day_strs, "4w", TODAY.date(), CLOCK)
    try:
        all_text = " ".join(t.get_text() for ax in fig.axes for t in ax.texts)
        assert "OldHabit" not in all_text
    finally:
        wrapped.plt.close(fig)


# ===========================================================================
# Thai rendering: the font is genuinely ENGAGED (not just a Thai Text
# object present), and a geometry-level regression test for Luna's own
# self-caught canvas-clipping bug.
# ===========================================================================


def test_noto_sans_thai_is_actually_registered_in_the_font_manager():
    """AC27's font-path half: not just that a Thai `Text` OBJECT exists
    with the right characters (Luna's own
    `test_build_figure_thai_labels_are_present_as_real_text_objects`),
    but that the bundled Noto Sans Thai font FILE is genuinely engaged --
    an unregistered family name sitting in `rcParams` alone would still
    render tofu; registration into `font_manager.fontManager.ttflist` is
    what actually lets matplotlib's per-glyph fallback find the glyphs."""
    import matplotlib
    from matplotlib import font_manager

    assert wrapped.MATPLOTLIB_AVAILABLE is True
    assert matplotlib.rcParams["font.family"] == ["DejaVu Sans", "Noto Sans Thai"]
    family_names = {f.name for f in font_manager.fontManager.ttflist}
    assert "Noto Sans Thai" in family_names

    from habit_assistant.core.fonts import FONT_PATH

    assert FONT_PATH.exists()
    assert FONT_PATH.suffix == ".ttf"


def test_thai_row_text_falls_within_figure_canvas_bounds_luna_clipping_regression(db, config):
    """Regression for Luna's own self-caught bug (IMPL-v1.9-wrapped.md
    "Known limitations" / the module docstring's clipping note): the
    FIRST draft placed each habit's summary text via `ax.text(x=1.03,
    ...)` -- past the axes' own unit square -- which is silently clipped
    at the FIGURE's canvas edge on `savefig`, not just the axes, even
    though the `Text` object existed happily in `ax.texts`. Luna's own
    tests never assert geometry, only text CONTENT, so they could not
    have caught this class of bug (and reportedly didn't -- it was found
    by visual inspection instead).

    This test asserts the actual GEOMETRY, using `matplotlib`'s own
    coordinate transforms (figure geometry, not rendered pixels): every
    text artist's position, converted through ITS OWN transform to
    figure-fraction coordinates, must land inside the figure's [0,1]x
    [0,1] canvas. This is precisely the invariant the x=1.03 bug
    violated."""
    registry = HabitRegistry(
        [
            _habit("water", "numeric", goal=2500.0, label_th="น้ำ"),
            _habit("gym", "boolean", unit_en=None, unit_th=None, label_th="ยิม"),
        ]
    )
    db.set_cadence(OWNER, "gym", 3)
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    _seed(db, OWNER, "2026-08-20T09:00:00", "gym", 1.0)
    day_strs = wrapped._window_days(TODAY.date(), "4w")
    fig = wrapped._build_figure(db, config, registry, "th", OWNER, day_strs, "4w", TODAY.date(), CLOCK)
    try:
        checked_any = False
        for ax in fig.axes:
            for t in ax.texts:
                checked_any = True
                x, y = t.get_position()
                display_xy = t.get_transform().transform((x, y))
                fig_x, fig_y = fig.transFigure.inverted().transform(display_xy)
                assert -1e-6 <= fig_x <= 1.0 + 1e-6, f"text {t.get_text()!r} escapes figure at fig_x={fig_x}"
                assert -1e-6 <= fig_y <= 1.0 + 1e-6, f"text {t.get_text()!r} escapes figure at fig_y={fig_y}"
        assert checked_any  # sanity: the figure actually drew some text
    finally:
        wrapped.plt.close(fig)


# ===========================================================================
# No-matplotlib fallback: bilingual text, alias/Thai-trigger equivalence
# through THIS path specifically, a "many habits" safety check, and a
# stronger adversarial corpus.
# ===========================================================================


async def test_fallback_text_is_bilingual_en_vs_th_differs(db, config, monkeypatch):
    monkeypatch.setattr(wrapped, "MATPLOTLIB_AVAILABLE", False)
    monkeypatch.setattr(wrapped, "_warned_missing", False)
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_th="น้ำ")])
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)

    reply_en = await wrapped.execute_wrapped(
        Command(kind="wrapped", pref_value=None),
        db=db, channel=FakeChannel(), config=config, registry=registry, lang="en", user_id=OWNER, clock=CLOCK,
    )
    reply_th = await wrapped.execute_wrapped(
        Command(kind="wrapped", pref_value=None),
        db=db, channel=FakeChannel(), config=config, registry=registry, lang="th", user_id=OWNER, clock=CLOCK,
    )
    assert reply_en != reply_th
    assert "น้ำ" in reply_th


async def test_recap_and_thai_alias_route_identically_through_the_fallback_path(db, config, monkeypatch):
    """AC25's alias/Thai-trigger equivalence, specifically exercised
    through the NO-matplotlib fallback path -- Luna's own equivalence
    test (`test_execute_wrapped_recap_alias_command_behaves_identically`)
    only exercises the PNG path, where byte-identical output is easy;
    the text-fallback path is built independently (`_build_fallback_
    text`) and deserves its own equivalence proof."""
    monkeypatch.setattr(wrapped, "MATPLOTLIB_AVAILABLE", False)
    monkeypatch.setattr(wrapped, "_warned_missing", False)
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0)])
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)

    commands = {
        "wrapped": dispatch("/wrapped", registry),
        "recap": dispatch("/recap", registry),
        "การ์ดสรุป": dispatch("การ์ดสรุป", registry),
    }
    replies = {}
    for name, cmd in commands.items():
        assert cmd is not None and cmd.kind == "wrapped"
        replies[name] = await wrapped.execute_wrapped(
            cmd, db=db, channel=FakeChannel(), config=config, registry=registry, lang="en", user_id=OWNER, clock=CLOCK,
        )
    assert replies["wrapped"] == replies["recap"] == replies["การ์ดสรุป"]

    month_cmd = dispatch("สรุปเดือน", registry)
    assert month_cmd.kind == "wrapped" and month_cmd.pref_value == "month"


async def test_fallback_many_habits_does_not_crash_and_produces_one_line_each(db, config, monkeypatch):
    """No `render_budget.TELEGRAM_MESSAGE_BUDGET` call exists on this
    path -- confirmed by reading `core/wrapped.py:_build_fallback_text`
    AND `core/heatmap.py:_build_fallback_text` side by side: NEITHER
    calls `core/render_budget.py`. This mirrors an existing, accepted
    precedent (`heatmap.py`'s own fallback has the identical gap) rather
    than a `wrapped`-specific regression, so this is not filed as a
    failure -- but the practical safety property (no crash, no silent
    data loss, proportional growth) is worth proving directly rather
    than assuming from the mirrored-precedent argument alone."""
    monkeypatch.setattr(wrapped, "MATPLOTLIB_AVAILABLE", False)
    monkeypatch.setattr(wrapped, "_warned_missing", False)
    habits = [_habit(f"h{i}", "boolean", unit_en=None, unit_th=None) for i in range(20)]
    registry = HabitRegistry(habits)
    for h in habits:
        _seed(db, OWNER, "2026-08-20T09:00:00", h.id, 1.0)
    channel = FakeChannel()

    reply = await wrapped.execute_wrapped(
        Command(kind="wrapped", pref_value=None),
        db=db, channel=channel, config=config, registry=registry, lang="en", user_id=OWNER, clock=CLOCK,
    )

    assert channel.images == []
    assert reply
    assert reply.count("\n") == len(habits)  # header + 20 per-habit lines => 20 newlines
    for h in habits:
        assert h.label("en") in reply


_EXTRA_ADVERSARIAL_CORPUS = [
    "การ์ดสรุปสวย",  # glued Thai, ordinary prose ("a pretty summary card")
    "ฉันอยากได้การ์ดสรุป",  # not anchored at the start (mid-sentence)
    "สรุปเดือนสิงหาคมเยี่ยมมาก",  # glued, ordinary prose using สรุปเดือน mid-sentence
    "please give me a recap card",
    "/wrappedmonth",  # glued, no space -- must not silently match "wrapped" then "month"
    "/re cap",  # split alias must not match
    "wrapped/recap",  # not slash-anchored at position 0
]


@pytest.mark.parametrize("text", _EXTRA_ADVERSARIAL_CORPUS)
def test_extra_adversarial_corpus_never_fires_wrapped_mid_sentence(text):
    command = dispatch(text, REGISTRY)
    assert command is None or command.kind != "wrapped"


# ===========================================================================
# execute_wrapped: caption shape per spec sample, graceful send-failure
# fallback carries real content (not just the bare header).
# ===========================================================================


async def test_execute_wrapped_caption_matches_spec_sample_shape(db, config):
    registry = HabitRegistry(
        [
            _habit("water", "numeric", goal=2500.0),
            _habit("gym", "boolean", unit_en=None, unit_th=None),
            _habit("diary", "text"),
        ]
    )
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    channel = FakeChannel()
    await wrapped.execute_wrapped(
        Command(kind="wrapped", pref_value=None),
        db=db, channel=channel, config=config, registry=registry, lang="en", user_id=OWNER, clock=CLOCK,
    )
    caption = channel.images[0][2]
    assert caption == "🎉 Your last 4 weeks — Water, Gym, Diary. Nice work!"


async def test_send_image_failure_falls_back_to_full_habit_data_not_just_the_header(db, config):
    """SPEC-v1.9.md §3's "wrapped render failure -> text fallback"
    established pattern, mirroring `execute_heatmap`'s identical
    fall-through: a delivery failure must leave the user with the SAME
    genuine per-habit content the text fallback would have shown anyway,
    not just a bare "something went wrong" header."""
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0)])
    _seed(db, OWNER, "2026-08-19T09:00:00", "water", 2500.0)
    channel = FakeChannel(raise_on_send_image=True)
    reply = await wrapped.execute_wrapped(
        Command(kind="wrapped", pref_value=None),
        db=db, channel=channel, config=config, registry=registry, lang="en", user_id=OWNER, clock=CLOCK,
    )
    assert channel.images == []
    assert "Water" in reply


# ===========================================================================
# celebration_burst: append-composition contract (Rule 25/AC29).
# ===========================================================================


def test_celebration_burst_appended_leaves_base_text_as_exact_prefix(config):
    """R25's own sample shape: `"...keep it going!\\n🎉🎊🥳"` -- appending
    the burst must never reformat/mutate the base celebration line, only
    add bytes after it; and when the burst is disabled, the composed
    result must be byte-identical to the base (no trailing newline
    left dangling). Simulates the composition `main.py`'s
    `confirmation_suffix` integration will perform -- proving the
    MODULE's own contract holds for that composition, independent of
    the (not-yet-wired, explicitly out of this module's scope per
    IMPL-v1.9-wrapped.md) integration step itself."""
    base = "🔥 7-day water streak — nice work, keep it going!"

    burst = wrapped.celebration_burst(config, "en")
    composed = base + "\n" + burst if burst else base
    assert composed.startswith(base)
    assert burst == wrapped._CELEBRATION_BURST

    config.wrapped.celebrate_burst = False
    burst_off = wrapped.celebration_burst(config, "en")
    composed_off = base + "\n" + burst_off if burst_off else base
    assert composed_off == base  # absent when nothing to append -- no trailing newline garbage either


# ===========================================================================
# Zero-LLM structural check (mirrors Luna's own, extended to the dispatch
# grammar's own source, not just render()/execute_wrapped()'s signatures).
# ===========================================================================


def test_match_wrapped_source_has_no_llm_dependency():
    from habit_assistant.core import commands

    source = inspect.getsource(commands._match_wrapped)
    assert "ollama" not in source.lower()
    assert "OllamaClient" not in source
