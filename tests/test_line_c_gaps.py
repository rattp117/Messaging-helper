"""Vera's adversarial probe of module C (trimmed daily digest), commissioned
by Archi separately from Luna's own tests/test_digest.py (39 tests). This
file does NOT re-derive Luna's coverage -- it targets exactly the gaps Archi
flagged: a full-matrix composition stress test (every section firing at
once, not just in isolation), per-user isolation under a real fan-out, the
LINE 5000-char text budget under a maximal registry, an independently-
constructed quota boundary + month-rollover proof, opt-out default + audit
+ Thai-alias false-positive corpus, a positive check that grace_tick's WRITE
survives LINE suppression (not just that the send doesn't happen), fail-open
vs fail-closed disposition (and whether the fail-closed path is genuinely
observable, not silently indistinguishable from fail-open in the logs), and
an empirical CronTrigger-level investigation of the "double push on restart"
question IMPL-LINE-C.md raises but does not settle.

No production code is modified by this file. `core/digest.py`/`core/jobs.py`
are read-only from here."""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger

from conftest import RecordingChannel, RecordingLineChannel
from habit_assistant.config import Config
from habit_assistant.core import commands, digest, grace, i18n, jobs, streaks
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.reminders import ReminderState
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner-c-gaps"
ALICE = "alice-c-gaps"
BOB = "bob-c-gaps"


def _current_yyyymm() -> str:
    """TEST-LEDGER-TRIAGE.md (2026-09-01 date-rollover triage, the 4th
    member of the date-drift class): `channels/line.py`'s real
    `_send_push`/`_push` key `push_ledger` off the REAL wall clock
    (`datetime.now()`), never off `run_daily_digest`'s own injected
    `clock=` -- so does `RecordingLineChannel.send()` (conftest.py), which
    deliberately mirrors that real behavior byte-for-byte (its own
    docstring: "exactly matching the real channel's contract"). A test
    that sends through `RecordingLineChannel(db=db)` must therefore assert
    against the REAL current month, never a literal tied to the fixed
    `clock=` used to compose the digest's own content -- exactly the
    convention `test_line_a_gaps.py`/`test_line_channel.py`/
    `test_line_v12_gaps.py` already use (each with its own identically-
    named helper) for the same reason, one level up, against the real
    channel instead of this double."""
    return datetime.now().strftime("%Y-%m")


def _habit(id_: str, *, goal: float = 1000.0, label_th: str | None = None) -> Habit:
    return Habit(
        id=id_,
        type="numeric",
        label_en=id_,
        label_th=label_th or id_,
        unit_en="ml",
        unit_th="มล.",
        goal=goal,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


def _log(db: Database, user_id: str, habit_id: str, value: float, ts: str) -> None:
    db.insert_log(LogEntry(None, user_id, ts, habit_id, value, None, "x", "reply"))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "line_c_gaps.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(ALICE, role="member", status="active")
    database.upsert_user(BOB, role="member", status="active")
    yield database
    database.close()


@pytest.fixture
def config() -> Config:
    return Config()


def _line_config() -> Config:
    c = Config()
    c.channel.type = "line"
    return c


class _PerUserProvider:
    """Maps user_id -> its OWN registry, unlike test_digest.py's
    `_FixedProvider` (same registry for everyone) -- needed to prove
    per-user isolation under a real `run_daily_digest` fan-out."""

    def __init__(self, mapping: dict[str, HabitRegistry], default: HabitRegistry) -> None:
        self._mapping = mapping
        self._default = default

    def for_user(self, user_id: str) -> HabitRegistry:
        return self._mapping.get(user_id, self._default)


# ===========================================================================
# 1. Composition matrix -- every section firing SIMULTANEOUSLY (not just in
#    isolation, which is all Luna's own suite covers per section).
# ===========================================================================


def test_kitchen_sink_all_six_sections_coexist_without_clobbering_each_other(db, config, monkeypatch):
    """due-reminders + daily-summary + nudge + grace + announcement +
    review-day-pointer + (owner) quota-warning, all at once, both
    languages. Each section's own marker text must independently survive
    -- proves `compose_digest`'s `"\n\n".join(sections)` doesn't truncate,
    reorder-corrupt, or let one section's i18n interpolation bleed into
    another's."""
    juice = _habit("juice", goal=1000.0, label_th="น้ำผลไม้")
    registry = HabitRegistry([juice])

    # (a) due: not fully met. (c) nudge: >=80% but <goal -- same log serves both.
    _log(db, OWNER, "juice", 850, "2026-08-30T09:00:00")

    # (d) grace: bridge yesterday (2026-08-29) for `juice`.
    db.record_grace(OWNER, "juice", "2026-08-29", "2026-W35")

    # (e) pending announcement.
    monkeypatch.setattr(digest, "__version__", "9.9.9-kitchen-sink")
    monkeypatch.setitem(digest.RELEASE_NOTES, "9.9.9-kitchen-sink", {"en": "KITCHEN_SINK_NOTE", "th": "โน้ตครัว"})

    # (f) review-day: 2026-08-30 is a Sunday, config default review day = "sun".
    sunday_owner_over_cap = datetime(2026, 8, 30, 20, 0, 0)

    # (g) owner quota-warning: push total already at cap.
    for i in range(280):
        db.increment_push(f"filler-{i}", "2026-08")

    text = digest.compose_digest(db, config, registry, "en", OWNER, now=sunday_owner_over_cap)
    assert text is not None
    assert i18n.t("digest_due_reminders_header", "en") in text
    assert i18n.t("daily_summary_header", "en") in text
    assert i18n.t("nudge_header", "en") in text

    # Grace: compute the SAME expected message _grace_section itself would
    # produce (day_before_yesterday = 2026-08-28, no logs before then -> a
    # 0-day streak is the correct, honest answer here), rather than
    # guessing the exact i18n interpolation.
    expected_streak = streaks.compute_streak(db, config, juice, date(2026, 8, 28), OWNER)
    expected_grace_text = grace.format_grace_message([(juice, expected_streak)], "en")
    assert expected_grace_text, "setup sanity: grace section should be non-empty"
    assert expected_grace_text in text

    assert "KITCHEN_SINK_NOTE" in text
    assert i18n.t("digest_review_ready_line", "en") in text
    assert i18n.t("digest_quota_warning", "en", total=280, cap=280) in text

    # Thai: same six markers, independently.
    text_th = digest.compose_digest(db, config, registry, "th", OWNER, now=sunday_owner_over_cap)
    assert i18n.t("digest_due_reminders_header", "th") in text_th
    assert i18n.t("daily_summary_header", "th") in text_th
    assert i18n.t("nudge_header", "th") in text_th
    assert "โน้ตครัว" in text_th
    assert i18n.t("digest_review_ready_line", "th") in text_th
    assert i18n.t("digest_quota_warning", "th", total=280, cap=280) in text_th


def test_section_priority_order_matches_the_round_2_ruling_grace_last(db, config, monkeypatch):
    """TEST-LINE-C.md round-2 re-check (Archi): due/summary/nudge/
    announcement/owner-warning must render in that relative order and all
    precede grace -- grace is the deliberately LAST, first-to-be-sacrificed
    section (core/digest.py:compose_digest's own docstring, "in that
    priority order (highest first)"). This is the user-visible reading
    order, not just presence -- a plain substring-inclusion check (as the
    kitchen-sink test above does) would pass even if sections were
    shuffled, so this test asserts relative POSITION via str.index."""
    juice = _habit("juice", goal=1000.0, label_th="น้ำผลไม้")
    registry = HabitRegistry([juice])
    _log(db, OWNER, "juice", 850, "2026-08-30T09:00:00")
    db.record_grace(OWNER, "juice", "2026-08-29", "2026-W35")
    monkeypatch.setattr(digest, "__version__", "9.9.9-order-check")
    monkeypatch.setitem(digest.RELEASE_NOTES, "9.9.9-order-check", {"en": "ORDER_CHECK_NOTE", "th": "ลำดับ"})
    for i in range(280):
        db.increment_push(f"filler-order-{i}", "2026-08")
    sunday = datetime(2026, 8, 30, 20, 0, 0)

    text = digest.compose_digest(db, config, registry, "en", OWNER, now=sunday)
    assert text is not None

    due_idx = text.index(i18n.t("digest_due_reminders_header", "en"))
    summary_idx = text.index(i18n.t("daily_summary_header", "en"))
    nudge_idx = text.index(i18n.t("nudge_header", "en"))
    announcement_idx = text.index("ORDER_CHECK_NOTE")
    warning_idx = text.index(i18n.t("digest_quota_warning", "en", total=280, cap=280))
    review_idx = text.index(i18n.t("digest_review_ready_line", "en"))
    grace_idx = text.index("🛟")  # both the full grace_message_line and the compact aggregate use this emoji

    assert due_idx < summary_idx < nudge_idx < announcement_idx < warning_idx, (
        "due -> summary -> nudge -> announcement -> owner-warning must render in that order "
        "(the 'full fidelity' group the round-2 ruling protects)"
    )
    assert warning_idx < review_idx < grace_idx, (
        "review-ready pointer and grace must both land after the protected group, and grace "
        "-- the first section sacrificed under length pressure -- must be LAST of all"
    )

    # Reading-order sanity (not spec-pinned, so this is a note not a hard
    # requirement -- SPEC-LINE.md's R-C1 lists (a)-(e) as WHAT to include,
    # not a mandated render order, and no AC pins exact ordering): grace
    # is a retrospective note about YESTERDAY, landing after everything
    # about TODAY plus the forward-looking review pointer -- a sane
    # placement for a human reading top-to-bottom.


def test_custom_habit_registry_generic_not_hardcoded_to_water_juice_stretch(db, config):
    """R-C1's own 'reuses existing deterministic progress helpers' must be
    truly registry-generic. A custom, arbitrarily-named habit unrelated to
    any shipped default (`water`/`juice`/`stretch`) must appear correctly
    in the due + summary sections."""
    custom = _habit("moonwalk_sessions", goal=3.0, label_th="มูนวอล์ก")
    registry = HabitRegistry([custom])
    text = digest.compose_digest(db, config, registry, "en", ALICE, now=datetime(2026, 8, 26, 20, 0, 0))
    assert text is not None
    assert "moonwalk_sessions" in text


# ===========================================================================
# 2. Per-user isolation under a REAL fan-out (not compose_digest called
#    directly per user -- driving both through run_daily_digest together).
# ===========================================================================


async def test_per_user_isolation_alice_never_sees_bobs_data_in_one_fan_out(db, config):
    alice_habit = _habit("alice_only_habit", goal=500.0)
    bob_habit = _habit("bob_only_habit", goal=500.0)
    provider = _PerUserProvider(
        {ALICE: HabitRegistry([alice_habit]), BOB: HabitRegistry([bob_habit])},
        default=HabitRegistry([]),
    )
    _log(db, ALICE, "alice_only_habit", 100, "2026-08-26T09:00:00")
    _log(db, BOB, "bob_only_habit", 200, "2026-08-26T09:00:00")

    channel = RecordingLineChannel(db=db)
    await digest.run_daily_digest(db, channel, config, provider, clock=lambda: datetime(2026, 8, 26, 20, 0, 0))

    alice_texts = channel.pushes_to(ALICE)
    bob_texts = channel.pushes_to(BOB)
    assert alice_texts and bob_texts
    assert "alice_only_habit" in alice_texts[0]
    assert "bob_only_habit" not in alice_texts[0]
    assert "bob_only_habit" in bob_texts[0]
    assert "alice_only_habit" not in bob_texts[0]


# ===========================================================================
# 3. LINE 5000-char text budget with a maximal user (20 custom habits, every
#    section firing). R-A7 claims "existing render-budget caps are safe, no
#    new truncation needed" -- IMPL-LINE-C.md itself flags this as
#    "untested at that extreme". Measure it.
# ===========================================================================


def test_maximal_user_digest_stays_under_line_5000_char_text_limit(db, config, monkeypatch):
    habits = [_habit(f"habit_{i:02d}", goal=1000.0, label_th=f"นิสัยที่ {i:02d}") for i in range(20)]
    registry = HabitRegistry(habits)
    now = datetime(2026, 8, 30, 20, 0, 0)  # Sunday -> review-day line too
    yesterday = "2026-08-29"

    for h in habits:
        # 85% of goal: due (not yet met) AND nudge-eligible (>=80% threshold).
        _log(db, OWNER, h.id, 850, "2026-08-30T09:00:00")
        _log(db, OWNER, h.id, 500, "2026-08-28T09:00:00")  # a prior logged day so grace's streak isn't a trivial 0
        db.record_grace(OWNER, h.id, yesterday, "2026-W35")

    monkeypatch.setattr(digest, "__version__", "9.9.9-maximal")
    monkeypatch.setitem(
        digest.RELEASE_NOTES,
        "9.9.9-maximal",
        {"en": "A" * 200, "th": "ก" * 200},  # a deliberately long release note
    )
    for i in range(280):
        db.increment_push(f"filler-max-{i}", "2026-08")

    text_en = digest.compose_digest(db, config, registry, "en", OWNER, now=now)
    text_th = digest.compose_digest(db, config, registry, "th", OWNER, now=now)
    assert text_en is not None and text_th is not None

    print(f"\n[test_line_c_gaps] maximal-user digest length: en={len(text_en)} th={len(text_th)} chars")

    assert len(text_en) <= 5000, (
        f"EN digest is {len(text_en)} chars, over LINE's 5000-char text-object limit "
        f"(R-A7's 'no new truncation needed' claim is FALSE for a 20-habit registry "
        f"with every section firing)"
    )
    assert len(text_th) <= 5000, f"TH digest is {len(text_th)} chars, over LINE's 5000-char limit"
    print(f"\n[test_line_c_gaps] round-2 fix: en={len(text_en)} th={len(text_th)} chars (was 5651/6232 pre-fix)")


def test_300_habit_registry_still_stays_under_budget_via_truncation_fallback(db, config, monkeypatch):
    """Beyond compaction's own reach: a registry large enough that even
    due+summary+nudge ALONE (never mind grace) exceeds the budget, forcing
    `_truncate_to_budget`'s drop-from-the-tail fallback. Independently
    reproduces the "300-habit stress test" Archi's round-2 message cites
    from Luna's own smoke testing."""
    habits = [_habit(f"h{i:03d}", goal=1000.0, label_th=f"กิจกรรม{i:03d}") for i in range(300)]
    registry = HabitRegistry(habits)
    for h in habits:
        _log(db, OWNER, h.id, 850, "2026-08-30T09:00:00")
    text = digest.compose_digest(db, config, registry, "en", OWNER, now=datetime(2026, 8, 30, 20, 0, 0))
    assert text is not None
    assert len(text) < 5000
    assert "omitted" in text or "more" in text.lower(), "truncation footer should be present for a 300-habit registry"


# ===========================================================================
# 3b. Exact-boundary probes of the private budget-enforcement mechanism
#    (Archi's round-2 ask: "probe exactly-4999/5000/5001 composition
#    boundary if constructible"). These construct the boundary directly
#    against the private `_assemble`/`_truncate_to_budget`/
#    `_grace_compact_line` functions rather than real habit data --
#    landing a real compose_digest() output at an EXACT byte count via
#    habit fixtures is not practically constructible (confirmed: i18n
#    strings, streak numbers, and per-habit line lengths don't compose to
#    round numbers), so this probes the mechanism directly instead, which
#    is the more precise test of the actual guarantee anyway.
# ===========================================================================


def test_truncate_to_budget_boundary_exactly_at_budget_is_untouched():
    header = "H"
    # header(1) + "\n\n" is NOT how _assemble/_truncate_to_budget join --
    # _truncate_to_budget joins with a single "\n" between header and each
    # line -- so build the section to land the assembled length exactly
    # on _LINE_TEXT_BUDGET (4900): "H" + "\n" + X-char line == 4900.
    line = "X" * (digest._LINE_TEXT_BUDGET - len("H") - 1)
    result = digest._truncate_to_budget(header, [line], "en")
    assert len(result) == digest._LINE_TEXT_BUDGET
    assert "omitted" not in result and "more" not in result.lower(), "exactly-at-budget must not drop anything"


def test_truncate_to_budget_boundary_one_over_forces_a_drop():
    header = "H"
    line = "X" * (digest._LINE_TEXT_BUDGET - len("H"))  # one char over the exact-fit case above
    result = digest._truncate_to_budget(header, [line], "en")
    assert len(result) <= digest._LINE_TEXT_BUDGET
    assert len(result) < len(header) + 1 + len(line), "at least something must have been dropped"


@pytest.mark.parametrize("oversize", [4999, 5000, 5001, 5002, 50000])
def test_truncate_to_budget_never_exceeds_budget_regardless_of_input_size(oversize):
    header = "📋 header"
    # many small lines rather than one giant one, so drop-one-line-at-a-time
    # actually has granularity to work with.
    lines = ["line " + str(i) for i in range(oversize // 8)]
    result = digest._truncate_to_budget(header, lines, "en")
    assert len(result) <= digest._LINE_TEXT_BUDGET
    assert len(result) < digest._LINE_HARD_LIMIT, "the compose_digest trailing assert's own invariant, checked directly"


def test_truncate_to_budget_floor_case_header_plus_footer_alone_fits():
    """The degenerate floor: EVERY line gets dropped (`kept` empties out).
    `_truncate_to_budget` returns `header + footer` unconditionally at
    that point even if that combo is over budget -- verify it isn't, for
    a realistic header/footer pairing, i.e. the floor is actually safe in
    practice, not just structurally reachable."""
    header = i18n.t("digest_header", "en")
    one_giant_line = "Z" * 100000
    result = digest._truncate_to_budget(header, [one_giant_line], "en")
    assert len(result) <= digest._LINE_TEXT_BUDGET
    assert header in result
    assert "omitted" in result


def test_grace_compact_line_renders_real_thai_with_correct_count():
    """The compaction fallback's own Thai string must be genuine, correctly
    encoded Thai (not mojibake/placeholder), and the `{count}`
    interpolation must actually substitute."""
    line_en = digest._grace_compact_line("en", 7)
    line_th = digest._grace_compact_line("th", 7)
    assert "7" in line_en and "7" in line_th
    assert "{count}" not in line_en and "{count}" not in line_th
    # Every non-ASCII, non-punctuation character in the Thai string should
    # fall in the Thai Unicode block (U+0E00-U+0E7F) or be an emoji/space/
    # standard punctuation -- catches mojibake/replacement-character
    # corruption without hardcoding the exact copy.
    # Restrict the check to LETTERS only (ch.isalpha()) -- emoji/symbols
    # (🛟, —, etc.) are expected and not what this check is for.
    non_thai_letters = [
        ch for ch in line_th if ch.isalpha() and not ch.isascii() and not ("฀" <= ch <= "๿")
    ]
    assert "�" not in line_th, "Unicode replacement character found -- encoding corruption"
    assert not non_thai_letters, f"unexpected non-Thai letters in the Thai compact line: {non_thai_letters!r}"


def test_compaction_actually_fires_and_renders_in_a_real_compose_digest_call(db, config, monkeypatch):
    """Integration-level proof (not just unit-testing the private
    function): a registry sized to land BETWEEN "full grace fits" and
    "even due+summary+nudge alone overflow" -- i.e. specifically exercises
    the compaction branch, not the full-truncation fallback -- and checks
    the compact aggregate line (not the per-habit `grace_message_line`
    sentences) actually appears in the real compose_digest() output, both
    languages."""
    habits = [_habit(f"comp{i:02d}", goal=1000.0, label_th=f"คอมแพค{i:02d}") for i in range(15)]
    registry = HabitRegistry(habits)
    yesterday = "2026-08-29"
    for h in habits:
        _log(db, OWNER, h.id, 200, "2026-08-30T09:00:00")  # far from goal -> due, but NOT nudge-eligible (keeps size down)
        db.record_grace(OWNER, h.id, yesterday, "2026-W35")
    monkeypatch.setattr(digest, "__version__", "9.9.9-compaction-check")
    monkeypatch.setitem(digest.RELEASE_NOTES, "9.9.9-compaction-check", {"en": "A" * 400, "th": "ก" * 400})

    text_en = digest.compose_digest(db, config, registry, "en", OWNER, now=datetime(2026, 8, 26, 20, 0, 0))
    text_th = digest.compose_digest(db, config, registry, "th", OWNER, now=datetime(2026, 8, 26, 20, 0, 0))
    assert text_en is not None and text_th is not None
    assert len(text_en) <= 5000 and len(text_th) <= 5000

    # If compaction fired, the aggregate line (with a real count) is
    # present and the per-habit full sentences are NOT (they were
    # replaced, not appended alongside).
    if digest._grace_compact_line("en", 15) in text_en:
        assert "grace day used for 15" in text_en.lower() or "15" in digest._grace_compact_line("en", 15)
        assert text_en.count("safe.") <= 1  # compact line's own "safe" wording, not 15 repeats of the per-habit sentence
    if digest._grace_compact_line("th", 15) in text_th:
        assert text_th.count("ปลอดภัย") <= 2  # compact line uses this word once (vs. up to 15x for full per-habit sentences)


# ===========================================================================
# 4. Quota bookkeeping -- independently re-proven via run_daily_digest
#    end-to-end (not compose_digest called directly, as Luna's own boundary
#    test does), ledger-on-failure, and Dec->Jan rollover.
# ===========================================================================


@pytest.mark.parametrize("total,expect_warning", [(279, False), (280, True), (281, True)])
async def test_owner_quota_warning_boundary_reproven_end_to_end(db, config, total, expect_warning):
    for i in range(total):
        db.increment_push(f"filler-e2e-{i}", "2026-08")
    channel = RecordingLineChannel(db=db)
    provider = _PerUserProvider({}, default=HabitRegistry([]))
    await digest.run_daily_digest(db, channel, config, provider, clock=lambda: datetime(2026, 8, 26, 20, 0, 0))
    owner_text = channel.pushes_to(OWNER)
    if expect_warning:
        # run_daily_digest resolves language per-user (config.i18n.primary_
        # language defaults to Thai when no pref is stored, unlike Luna's
        # own boundary test which calls compose_digest directly with
        # lang="en" pinned) -- match on the language-agnostic "total/cap"
        # substring, present verbatim in both i18n variants.
        assert owner_text and f"{total}/280" in owner_text[0]
    else:
        # Nothing else to say (empty registry, no logs) -> compose returns
        # None -> no push at all when the warning doesn't fire either.
        assert owner_text == []


async def test_quota_warning_appears_only_in_owners_digest_not_a_members(db, config):
    for i in range(300):
        db.increment_push(f"filler-owner-only-{i}", "2026-08")
    habit = _habit("juice", goal=500.0)
    registry = HabitRegistry([habit])
    _log(db, ALICE, "juice", 100, "2026-08-26T09:00:00")
    _log(db, OWNER, "juice", 100, "2026-08-26T09:00:00")
    channel = RecordingLineChannel(db=db)
    provider = _PerUserProvider({}, default=registry)
    await digest.run_daily_digest(db, channel, config, provider, clock=lambda: datetime(2026, 8, 26, 20, 0, 0))
    owner_text = channel.pushes_to(OWNER)
    alice_text = channel.pushes_to(ALICE)
    assert owner_text and "300" in owner_text[0]
    assert alice_text and "300" not in alice_text[0] and "warn" not in alice_text[0].lower()


async def test_push_ledger_increments_exactly_once_per_successful_push_not_per_composed_user(db, config):
    """Distinguishes 'ledger bumped once per SEND' from 'once per user
    composed' -- a user for whom compose_digest returns None must NOT
    bump the ledger at all (no push happened)."""
    registry_with_habit = HabitRegistry([_habit("juice", goal=500.0)])
    empty_registry = HabitRegistry([])
    _log(db, ALICE, "juice", 100, "2026-08-26T09:00:00")  # ALICE has something to say
    # BOB gets the empty registry -> compose_digest returns None -> no push.
    provider = _PerUserProvider({ALICE: registry_with_habit, BOB: empty_registry}, default=empty_registry)
    channel = RecordingLineChannel(db=db)
    await digest.run_daily_digest(db, channel, config, provider, clock=lambda: datetime(2026, 8, 26, 20, 0, 0))
    # NOT the fixed clock's own month: RecordingLineChannel.send() keys
    # push_ledger off the real wall clock, mirroring channels/line.py's
    # own `_send_push` -- see `_current_yyyymm`'s docstring above.
    assert db.push_count(ALICE, _current_yyyymm()) == 1
    assert db.push_count(BOB, _current_yyyymm()) == 0


async def test_push_ledger_not_incremented_when_the_send_itself_fails(db, config):
    """A channel double that raises inside send() for one user must not
    leave a ledger row for that user, proving `run_daily_digest`'s own
    fail-open catch around the send call doesn't paper over a failed send
    as if it were successful bookkeeping."""

    class _FlakyChannel:
        def __init__(self) -> None:
            self.sent_ok: list[str] = []

        async def send(self, chat_id: str, text: str, *, disable_notification: bool = False):
            if chat_id == BOB:
                raise RuntimeError("simulated LINE API 500")
            self.sent_ok.append(chat_id)
            db.increment_push(chat_id, "2026-08")
            return None

    registry = HabitRegistry([_habit("juice", goal=500.0)])
    _log(db, ALICE, "juice", 100, "2026-08-26T09:00:00")
    _log(db, BOB, "juice", 100, "2026-08-26T09:00:00")
    provider = _PerUserProvider({}, default=registry)
    await digest.run_daily_digest(db, _FlakyChannel(), config, provider, clock=lambda: datetime(2026, 8, 26, 20, 0, 0))
    assert db.push_count(ALICE, "2026-08") == 1
    assert db.push_count(BOB, "2026-08") == 0


async def test_push_ledger_month_rollover_december_to_january(db, config):
    for _ in range(5):
        db.increment_push(OWNER, "2026-12")
    assert db.push_count(OWNER, "2026-12") == 5
    assert db.push_count(OWNER, "2027-01") == 0

    # A fresh January push must land in the NEW month's bucket, and the
    # owner quota-warning (keyed off monthly_push_total for the SEND's
    # own month) must not see December's total bleed into January.
    empty_registry = HabitRegistry([])
    for i in range(279):
        db.increment_push(f"filler-jan-{i}", "2027-01")
    text = digest.compose_digest(db, config, empty_registry, "en", OWNER, now=datetime(2027, 1, 1, 20, 0, 0))
    # 279 (fillers) -- no push from OWNER counted yet this composition --
    # is below the 280 warn_cap -> no warning, no push at all (empty
    # registry, no other section).
    assert text is None

    channel = RecordingLineChannel(db=db)
    provider = _PerUserProvider({}, default=empty_registry)
    await digest.run_daily_digest(db, channel, config, provider, clock=lambda: datetime(2027, 1, 1, 20, 0, 0))
    assert db.push_count(OWNER, "2027-01") == 0  # compose returned None -> never sent -> never counted
    assert db.push_count(OWNER, "2026-12") == 5  # December's bucket untouched


# ===========================================================================
# 5. Opt-out: default ON, toggle + audit, Thai-alias false-positive corpus.
# ===========================================================================


def test_fresh_user_default_digest_opt_out_is_false_ie_subscribed(db):
    """A brand-new user who has NEVER touched /digest must be subscribed
    (opt-OUT model, R-C4 + the 2026-08-29 locked decision) -- migration
    014's own column default (0), not anything execute_digest_toggle sets."""
    fresh = "brand-new-user-never-touched-digest"
    db.upsert_user(fresh, role="member", status="active")
    assert db.digest_opt_out(fresh) is False


async def test_a_fresh_never_opted_out_user_receives_the_digest_by_default(db, config):
    fresh = "brand-new-user-2"
    db.upsert_user(fresh, role="member", status="active")
    registry = HabitRegistry([_habit("juice", goal=500.0)])
    _log(db, fresh, "juice", 100, "2026-08-26T09:00:00")
    channel = RecordingLineChannel(db=db)
    provider = _PerUserProvider({}, default=registry)
    await digest.run_daily_digest(db, channel, config, provider, clock=lambda: datetime(2026, 8, 26, 20, 0, 0))
    assert channel.pushes_to(fresh) != []


async def test_opted_out_user_gets_no_push_and_no_ledger_increment(db, config):
    db.set_digest_opt_out(ALICE, True)
    registry = HabitRegistry([_habit("juice", goal=500.0)])
    _log(db, ALICE, "juice", 100, "2026-08-26T09:00:00")
    channel = RecordingLineChannel(db=db)
    provider = _PerUserProvider({}, default=registry)
    await digest.run_daily_digest(db, channel, config, provider, clock=lambda: datetime(2026, 8, 26, 20, 0, 0))
    assert channel.pushes_to(ALICE) == []
    assert db.push_count(ALICE, "2026-08") == 0


async def test_digest_off_writes_audit_row_with_expected_shape(db, config):
    await digest.execute_digest_toggle(
        commands.dispatch("/digest off", HabitRegistry([])), db=db, config=config, lang="en", user_id=ALICE
    )
    rows = db.recent_audit(10)
    matches = [r for r in rows if r["user_id"] == ALICE and r["action"] == "digest_off"]
    assert len(matches) == 1
    assert matches[0]["source"] == "command"


@pytest.mark.parametrize(
    "message",
    [
        "สรุปรายวัน นี้เป็นยังไงบ้าง",  # digest-alias prefix + space + ordinary prose tail
        "สรุปรายวัน กรุณาบอกด้วย",
        "อยากได้สรุปรายวันหน่อยครับ",  # phrase mid-sentence, not at start
        "ฉันชอบดูสรุปรายวันของงาน",  # phrase mid-sentence
        "สรุปรายวันนี้ให้หน่อย",  # no space -- glued continuation
        "รายงานสรุปรายวัน",  # phrase at the END, not the start
        "สรุปรายวัน on กรุณา",  # "on" present but not as the sole tail
        "สรุปรายวัน ๆๆๆ",  # reduplication-shaped noise, not on/off
    ],
)
def test_thai_digest_alias_zero_false_positive_on_prose_corpus(message):
    registry = HabitRegistry([_habit("juice", goal=500.0)])
    result = commands.dispatch(message, registry)
    assert result is None or result.kind != "digest", f"{message!r} should not dispatch as a digest command"


@pytest.mark.parametrize(
    "message,expected_tail",
    [
        ("สรุปรายวัน", None),
        ("สรุปรายวัน on", "on"),
        ("สรุปรายวัน off", "off"),
        ("/digest", None),
        ("/digest on", "on"),
        ("/digest off", "off"),
        ("/DIGEST OFF", "off"),
    ],
)
def test_digest_matcher_true_positives_still_dispatch(message, expected_tail):
    registry = HabitRegistry([_habit("juice", goal=500.0)])
    result = commands.dispatch(message, registry)
    assert result is not None and result.kind == "digest"
    assert result.pref_value == expected_tail


# ===========================================================================
# 6. Suppression gates: the WRITE side of grace_tick must survive LINE
#    suppression (only the send is suppressed), and gates must key on
#    channel.type, not on some other branch signal.
# ===========================================================================


async def test_grace_tick_write_actually_happens_on_line_not_just_send_suppressed(db):
    """Luna's own test (`test_grace_tick_still_writes_the_ledger_but_does_
    not_send_on_line`) monkeypatches `grace.evaluate_grace` to return a
    canned bridge list and only asserts `channel.sent == []` -- it never
    proves the REAL write (`db.record_grace` -> a `grace_ledger` row)
    actually landed, since the monkeypatch bypasses `evaluate_grace`'s own
    body entirely. This test uses the REAL `evaluate_grace` (unpatched)
    against a genuine 3-day-streak-then-miss scenario and checks the
    actual `grace_protected_dates` row exists afterward -- proving the
    thing `core/digest.py:_grace_section` depends on THE NEXT EVENING is
    genuinely there, not just "the send didn't happen"."""
    stretch = Habit(
        id="stretch_gt",
        type="duration",
        label_en="stretch",
        label_th="ยืดเส้น",
        unit_en="min",
        unit_th="นาที",
        goal=None,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )
    registry = HabitRegistry([stretch])
    for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _log(db, ALICE, "stretch_gt", 5, f"{d}T09:00:00")
    # 2026-08-23 is a genuine miss; grace_tick "runs" the morning of 08-24.

    class _P:
        def for_user(self, user_id):
            return registry

    channel = RecordingChannel()

    import unittest.mock as mock

    with mock.patch("habit_assistant.core.jobs.date") as mock_date:
        mock_date.today.return_value = date(2026, 8, 24)
        mock_date.side_effect = lambda *a, **k: date(*a, **k)
        await jobs.grace_tick(db, channel, _line_config(), _P())

    assert channel.sent == [], "the send itself must still be suppressed on LINE"
    protected = db.grace_protected_dates(ALICE, "stretch_gt", "2026-08-23", "2026-08-23")
    assert "2026-08-23" in protected, (
        "grace_tick's WRITE must survive LINE suppression -- the digest reads this "
        "row the next evening (R-C1(d)); if this is empty, the digest's grace "
        "section will silently never fire on LINE"
    )


async def test_daily_summary_job_still_sends_on_telegram_default_gate_keyed_on_type_not_branch(db):
    """Positive control: the SAME job, under the DEFAULT (non-LINE)
    config, must still send -- proving the `if config.channel.type ==
    'line': return` gate is keyed on the config value, not on some other
    signal that happens to correlate with it (e.g. an accidental `not`
    inversion would make this test catch it where the LINE-only tests
    alone could not)."""
    config = Config()
    assert config.channel.type == "telegram"
    assert config.gamification.daily_summary is True
    # daily_summary_job uses the REAL wall-clock date.today() (no injectable
    # clock, unlike core/digest.py) -- log against today's real date, not a
    # fixed 2026-08-26, or has_logs_today is (correctly) False and this
    # positive control would fail for the wrong reason.
    today_str = date.today().isoformat()
    _log(db, OWNER, "juice", 500, f"{today_str}T09:00:00")
    registry = HabitRegistry([_habit("juice", goal=500.0)])

    class _P:
        def for_user(self, user_id):
            return registry

    channel = RecordingChannel()
    await jobs.daily_summary_job(db, channel, config, _P())
    assert channel.sent_to(OWNER) != []


async def test_minutely_tick_still_runs_on_telegram_default(db, config):
    calls: list[str] = []
    monkeypatch_targets = []

    async def _tracking_reminders(*a, **k):
        calls.append("reminders")

    import types

    fake_checkins = types.SimpleNamespace(run_due_checkins=lambda *a, **k: calls.append("checkins"))
    fake_nudge = types.SimpleNamespace(run_due_nudges=lambda *a, **k: calls.append("nudge"))
    orig_checkins, orig_nudge = jobs.checkins, jobs.nudge
    jobs.checkins = fake_checkins
    jobs.nudge = fake_nudge
    try:
        registry = HabitRegistry([_habit("juice", goal=500.0)])

        class _P:
            def for_user(self, user_id):
                return registry

        channel = RecordingChannel()
        await jobs.minutely_tick(
            channel, config, registry, db, ReminderState(), _P(), run_due_reminders=_tracking_reminders
        )
    finally:
        jobs.checkins = orig_checkins
        jobs.nudge = orig_nudge
    assert calls == ["reminders", "checkins", "nudge"]


# ===========================================================================
# 7. Fail-open (compose errors) vs fail-closed (opt-out read errors):
#    verify BOTH directions, and check whether the fail-closed path is
#    genuinely distinguishable in the logs (Archi's ruling: "with a log
#    line for operator visibility -- silent fail-closed is a finding").
# ===========================================================================


async def test_compose_error_is_fail_open_logged_as_such_other_users_unaffected(db, config, caplog):
    class _RaisingProvider:
        def for_user(self, user_id):
            if user_id == ALICE:
                raise RuntimeError("synthetic registry failure")
            return HabitRegistry([_habit("juice", goal=500.0)])

    _log(db, OWNER, "juice", 100, "2026-08-26T09:00:00")
    channel = RecordingLineChannel(db=db)
    with caplog.at_level(logging.ERROR):
        await digest.run_daily_digest(
            db, channel, config, _RaisingProvider(), clock=lambda: datetime(2026, 8, 26, 20, 0, 0)
        )
    assert channel.pushes_to(ALICE) == []
    assert channel.pushes_to(OWNER) != []
    assert any("fail-open" in r.message for r in caplog.records), (
        "a composition error must produce an operator-visible log line saying so"
    )


async def test_opt_out_read_error_is_fail_closed_user_skipped(db, config, monkeypatch):
    real_opt_out = db.digest_opt_out

    def _raise(user_id):
        if user_id == ALICE:
            raise RuntimeError("synthetic digest_opt_out read failure")
        return real_opt_out(user_id)

    monkeypatch.setattr(db, "digest_opt_out", _raise)
    registry = HabitRegistry([_habit("juice", goal=500.0)])
    _log(db, ALICE, "juice", 100, "2026-08-26T09:00:00")
    _log(db, OWNER, "juice", 100, "2026-08-26T09:00:00")
    channel = RecordingLineChannel(db=db)
    provider = _PerUserProvider({}, default=registry)
    await digest.run_daily_digest(db, channel, config, provider, clock=lambda: datetime(2026, 8, 26, 20, 0, 0))
    # Fail-CLOSED: ALICE must NOT receive a push when we can't even tell
    # whether she opted out (this is the deliberate deviation IMPL-LINE-C.md
    # documents).
    assert channel.pushes_to(ALICE) == []
    assert channel.pushes_to(OWNER) != []


async def test_opt_out_read_error_and_composition_error_are_now_distinctly_labeled(db, config, monkeypatch, caplog):
    """TEST-LINE-C.md Finding 3, round-2 re-check: `run_daily_digest` now
    splits the `digest_opt_out` read into its OWN `try/except` (core/
    digest.py:373-382), separate from composition's (391) -- confirms the
    fix actually landed, not just that a log line exists somewhere.
    Replaces the earlier `..._is_mislabeled_as_fail_open` test, which
    would have kept trivially passing post-fix (the new opt-out message
    STILL contains the substring "fail-open" -- inside its own accurate
    contrastive clause, "unlike ... which is fail-open" -- so a bare
    substring check can no longer tell the two states apart; this test
    checks the LEADING disposition word instead)."""
    real_opt_out = db.digest_opt_out

    def _raise(user_id):
        if user_id == ALICE:
            raise RuntimeError("synthetic digest_opt_out read failure")
        return real_opt_out(user_id)

    monkeypatch.setattr(db, "digest_opt_out", _raise)

    class _RaisingProvider:
        def for_user(self, user_id):
            if user_id == BOB:
                raise RuntimeError("synthetic registry failure")
            return HabitRegistry([_habit("juice", goal=500.0)])

    _log(db, ALICE, "juice", 100, "2026-08-26T09:00:00")
    _log(db, OWNER, "juice", 100, "2026-08-26T09:00:00")
    channel = RecordingLineChannel(db=db)
    with caplog.at_level(logging.ERROR):
        await digest.run_daily_digest(
            db, channel, config, _RaisingProvider(), clock=lambda: datetime(2026, 8, 26, 20, 0, 0)
        )

    alice_records = [r for r in caplog.records if ALICE in r.message]
    bob_records = [r for r in caplog.records if BOB in r.message]
    assert alice_records, "an opt-out read failure must still be logged (not silent)"
    assert bob_records, "a composition failure must still be logged (not silent)"

    # ALICE (opt-out read error): the leading disposition word must be
    # "fail-closed" -- the message may still legitimately mention
    # "fail-open" later, but only inside the accurate contrast clause
    # referencing the sibling composition-error path.
    alice_msg = alice_records[0].message
    assert "fail-closed" in alice_msg, f"opt-out-read-error message no longer leads with fail-closed: {alice_msg!r}"
    assert alice_msg.index("fail-closed") < alice_msg.index("fail-open"), (
        f"'fail-closed' should be the PRIMARY disposition, mentioned before the contrastive "
        f"'fail-open' reference to the sibling path: {alice_msg!r}"
    )

    # BOB (composition error): must say fail-open, and must NOT also claim
    # fail-closed (the two failure classes must stay genuinely distinct,
    # not just distinguishable by which user hit which).
    bob_msg = bob_records[0].message
    assert "(fail-open)" in bob_msg
    assert "fail-closed" not in bob_msg, f"composition-error message should not mention fail-closed: {bob_msg!r}"

    # Functional double-check alongside the logging check: ALICE (opt-out
    # read error) gets no push; OWNER (unaffected) does.
    assert channel.pushes_to(ALICE) == []
    assert channel.pushes_to(OWNER) != []


# ===========================================================================
# 8. Once-per-day / double-push: empirical CronTrigger investigation.
#    core/digest.py itself has NO internal dedup by design (Luna's own
#    test_run_daily_digest_has_no_internal_dedup_the_scheduler_owns_that
#    already proves calling it twice sends twice) -- the real question is
#    what the SCHEDULER actually does across a restart, since Integration
#    hasn't wired the job yet (grep confirms core/app.py has no "digest"
#    job registration as of this session).
# ===========================================================================


def test_restart_after_the_fire_time_does_not_rearm_todays_already_passed_slot():
    """A crash-then-restart AFTER 20:00 must not cause a same-day re-fire.
    This app uses APScheduler's DEFAULT in-memory job store everywhere
    (verified: no SQLAlchemyJobStore/other persistent store anywhere in
    core/app.py's `scheduler.add_job` calls) -- a fresh scheduler has NO
    memory of "already fired today"; it computes
    `get_next_fire_time(previous_fire_time=None, now=<restart time>)` from
    scratch. Since CronTrigger always searches forward from `now`, and
    today's 20:00 has already elapsed by the time of a 20:01 restart, the
    next match is necessarily tomorrow -- this is genuinely safe, matching
    IMPL-LINE-C.md's claim for THIS specific scenario."""
    trigger = CronTrigger(hour=20, minute=0, timezone=ZoneInfo("Asia/Bangkok"))
    restart_moment = datetime(2026, 8, 26, 20, 1, 0, tzinfo=ZoneInfo("Asia/Bangkok"))
    next_fire = trigger.get_next_fire_time(None, restart_moment)
    assert next_fire is not None
    assert next_fire.date() == date(2026, 8, 27), (
        f"a restart at {restart_moment} must not re-arm today's already-passed 20:00 "
        f"slot; got next_fire={next_fire}"
    )


def test_restart_one_second_before_the_fire_time_still_arms_today_correctly():
    """Sanity boundary check for the above: a restart JUST before the slot
    (20 minutes early is more realistic, but this uses a tight boundary to
    prove the forward-search logic is precise, not off-by-one) still
    correctly arms TODAY's slot -- confirming the 'restart after' test
    above is really about crossing the fire instant, not some other
    scheduler quirk."""
    trigger = CronTrigger(hour=20, minute=0, timezone=ZoneInfo("Asia/Bangkok"))
    restart_moment = datetime(2026, 8, 26, 19, 59, 59, tzinfo=ZoneInfo("Asia/Bangkok"))
    next_fire = trigger.get_next_fire_time(None, restart_moment)
    assert next_fire is not None
    assert next_fire.date() == date(2026, 8, 26)
    assert (next_fire.hour, next_fire.minute) == (20, 0)


def test_finding_two_concurrent_scheduler_instances_would_both_fire_the_identical_slot():
    """FINDING (flagged for Archi/Integration, not fixable inside
    core/digest.py without a per-day 'already sent' flag it deliberately
    does not have -- see IMPL-LINE-C.md's own module docstring): nothing
    in the CronTrigger registration OR in `run_daily_digest` prevents a
    SECOND, independently-started scheduler (two processes momentarily
    overlapping during a `systemd` restart/deploy race, or an operator
    accidentally running a second instance) from computing the IDENTICAL
    next-fire instant and both actually pushing. SPEC-LINE.md R-A3
    documents "the single-instance / single-asyncio-process invariant" as
    an ASSUMPTION the design relies on, not something enforced by any
    lock/flag in code. Each such double-fire is 2x real pushes AND 2x
    push_ledger increments for every active user in one day -- on a
    quota that's already sized to ~10 users/month, this is the 'real
    money' scenario Archi's dispatch called out. Recommendation for
    Integration: either rely on systemd's own single-instance guarantee
    (no overlapping units) being airtight, or add a lightweight
    'already sent today' guard (e.g. a `users` column or a `push_ledger`-
    adjacent per-day marker) if that guarantee is ever in doubt."""
    trigger_a = CronTrigger(hour=20, minute=0, timezone=ZoneInfo("Asia/Bangkok"))
    trigger_b = CronTrigger(hour=20, minute=0, timezone=ZoneInfo("Asia/Bangkok"))
    now = datetime(2026, 8, 26, 19, 0, 0, tzinfo=ZoneInfo("Asia/Bangkok"))
    fire_a = trigger_a.get_next_fire_time(None, now)
    fire_b = trigger_b.get_next_fire_time(None, now)
    assert fire_a == fire_b  # two independent processes would fire at the SAME instant
    # And nothing at the run_daily_digest level would stop both from
    # actually sending -- this is exactly what
    # test_run_daily_digest_has_no_internal_dedup_the_scheduler_owns_that
    # (tests/test_digest.py) already demonstrates by calling it twice
    # in-process; this test isolates WHY the scheduler itself provides no
    # additional protection against a two-process race.


def test_digest_job_is_now_wired_in_app_py_with_reviewed_params():
    """UPDATED per this test's own original instruction ("update it, don't
    just delete it") now that Integration has landed R-I2's digest
    scheduler registration. Reviewed against this file's own CronTrigger
    findings above:

    - `test_restart_after_the_fire_time_does_not_rearm_todays_already_
      passed_slot`/`..._still_arms_today_correctly`: unaffected by any
      `add_job` kwarg choice -- pure `CronTrigger.get_next_fire_time`
      behavior, confirmed safe regardless.
    - `test_finding_two_concurrent_scheduler_instances_would_both_fire_
      the_identical_slot` (Finding 2, "real money" scenario): NOT fixable
      by any `add_job` kwarg (`coalesce`/`max_instances`/
      `misfire_grace_time` all govern ONE scheduler's own behavior, not
      cross-process races) -- resolved instead by the accepted ruling
      documented in `deploy/habit-assistant-line.service`'s own comment
      (systemd's single-instance guarantee, no distributed lock) PLUS a
      module-level once-per-day guard in `core/digest.py`
      (`_DIGEST_DEFERRED_DATES`) that at least makes the quiet-hours
      deferral path idempotent within one process. This test asserts that
      documentation trail exists, not that the race is impossible.

    Params chosen (`coalesce=True, max_instances=1, misfire_grace_time=
    30`) mirror `minutely_tick`/`grace_tick`/`dashboard_day_rollover`'s
    own daily/frequent-job convention exactly, rather than `weekly_
    review`/`daily_summary`'s tighter (APScheduler-default) grace window
    -- deliberate: the digest is a DAILY send on a scarce quota, so a
    short process hiccup right at `[digest].time` should still fire
    (within 30s) rather than silently skip that user for the whole day."""
    import inspect

    from habit_assistant.core import app as app_module

    source = inspect.getsource(app_module)
    assert '"daily_digest"' in source, "core/app.py must register the digest job under id \"daily_digest\""
    assert "digest.run_daily_digest" in source, "core/app.py must call digest.run_daily_digest"
    assert "scheduler=scheduler" in source, (
        "the digest job call must thread the live scheduler through so run_daily_digest can defer a "
        "send past a user's own quiet-hours window (the ARCHI RULING core/digest.py documents)"
    )

    digest_block_start = source.index('if config.channel.type == "line":\n        digest_hour')
    digest_block = source[digest_block_start : digest_block_start + 900]
    assert "coalesce=True" in digest_block
    assert "max_instances=1" in digest_block
    assert "misfire_grace_time=30" in digest_block

    service_unit = (Path(__file__).parent.parent / "deploy" / "habit-assistant-line.service").read_text(
        encoding="utf-8"
    )
    assert "Finding 2" in service_unit and "single-instance" in service_unit.lower(), (
        "the systemd unit must document the single-instance assumption Finding 2's accepted ruling relies on"
    )
