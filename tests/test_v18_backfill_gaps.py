"""Vera's adversarial gap-fill suite for `core/backfill.py` (SPEC-v1.8.md
§2.4/§3.4, R-B1-R-B6, AC-C1/C4/C5/C6-building-block), on top of Luna's own
`tests/test_backfill.py` (71 tests, already reviewed and green).

Scope: module-level only, per the v1.8 parallel-module split -- AC-C2/C3/C6
and the "residual resolves through the real pipeline" slice of AC-C1 are
explicitly deferred to `main.py` integration (not yet wired) and are NOT
exercised or failed here. See TEST-v1.8-backfill.md for the AC boundary.

Four groups:
1. Leading/trailing coverage for every one of the six §2.4 phrase bodies
   (Luna covers most bodies in one direction only -- this fills the other
   direction for each).
2. A larger (40+) EN+TH zero-false-positive adversarial corpus (AC-C5),
   focused on habit-name/weekday-word collisions and other shapes Luna's
   25-case corpus didn't try.
3. Bounds (AC-C4) at a non-default cap, weekday-vs-bound interaction, and a
   full-week rotating invariant that no weekday-name resolution ever
   returns delta 0 regardless of which day is "today".
4. Purity (AST-based import check, stronger than a source substring scan),
   ts-format cross-check against the codebase's real log-write format, and
   bilingual rendering checks (no unresolved placeholders, real Thai text)
   for `confirmation_prefix`/`bounds_error_text`.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date, datetime, timedelta

import pytest

from habit_assistant.core import backfill
from habit_assistant.core.backfill import OutOfRange, extract_date


def _clock(dt: datetime):
    def clock():
        return dt

    return clock


# Monday 2026-08-24 .. Sunday 2026-08-30 (same week Luna's fixture anchors to,
# Tuesday 2026-08-25).
_MONDAY = date(2026, 8, 24)
_TODAY = datetime(2026, 8, 25, 9, 0, 0)  # Tuesday
_CLOCK = _clock(_TODAY)


# ===========================================================================
# 1. Leading vs. trailing coverage for every §2.4 phrase body.
# ===========================================================================


def test_en_days_ago_leading_position():
    assert extract_date("2 days ago diary", _CLOCK, max_days_back=14) == ("diary", date(2026, 8, 23))
    assert extract_date("1 day ago diary", _CLOCK, max_days_back=14) == ("diary", date(2026, 8, 24))


def test_en_weekday_leading_position():
    assert extract_date("on Monday water 500", _CLOCK, max_days_back=14) == ("water 500", date(2026, 8, 24))
    assert extract_date("last Friday water 500", _CLOCK, max_days_back=14) == ("water 500", date(2026, 8, 21))


def test_th_yesterday_trailing_position():
    assert extract_date("ดื่มน้ำ 500 เมื่อวาน", _CLOCK, max_days_back=14) == ("ดื่มน้ำ 500", date(2026, 8, 24))
    assert extract_date("ดื่มน้ำ 500 เมื่อวานนี้", _CLOCK, max_days_back=14) == ("ดื่มน้ำ 500", date(2026, 8, 24))


def test_th_days_ago_trailing_position():
    assert extract_date("ยืดเส้น 20 นาที 2 วันที่แล้ว", _CLOCK, max_days_back=14) == (
        "ยืดเส้น 20 นาที",
        date(2026, 8, 23),
    )
    assert extract_date("ยืดเส้น 20 นาที 2 วันก่อน", _CLOCK, max_days_back=14) == (
        "ยืดเส้น 20 นาที",
        date(2026, 8, 23),
    )


def test_th_weekday_leading_position():
    assert extract_date("วันจันทร์ ยืดเส้น 20 นาที", _CLOCK, max_days_back=14) == (
        "ยืดเส้น 20 นาที",
        date(2026, 8, 24),
    )
    assert extract_date("วันพฤหัสบดี ยืดเส้น 20 นาที", _CLOCK, max_days_back=14) == (
        "ยืดเส้น 20 นาที",
        date(2026, 8, 20),
    )


# ===========================================================================
# 2. Larger zero-false-positive adversarial corpus (AC-C5).
# ===========================================================================

MORE_ADVERSARIAL_NEGATIVES = [
    # --- Habit-name / weekday-word collisions (bare weekday, no on/last;
    # EN weekday recognition strictly requires the on/last trigger). ---
    "Monday 5 reps",
    "friday 3 reps",
    "saturday routine done",
    "Sunday roast 500g",
    "gym on Mondays every week",  # "Mondays" != "Monday", and trailing has more content
    "I depend on Monday deliveries",  # "on Monday" present but NOT the trailing/leading edge
    "last Monday's meeting notes",  # possessive suffix breaks the exact-token match
    # --- "on"/"last" grammatically present but not "(on|last) <weekday>". ---
    "last minute I logged 500ml",
    "on time today I did 500ml",
    "last call before bed, 500ml",
    "on the way home I stretched 10 min",
    # --- EN "yesterday"/"ago" as ordinary words, not edge phrases. ---
    "yesterday's leftovers, ate them today",
    "it feels like just yesterday I started this habit",
    "ages ago I used to run every day",
    "3 days from now I have a race",  # "days" present but no "ago", and it's future-shaped prose
    "a few days ago-ish, not sure exactly, I stretched",
    # --- TH habit-name / weekday-word collisions (no วัน prefix -> can't
    # match the TH weekday form at all). ---
    "จันทร์เต็มดวงคืนนี้",  # "จันทร์" (moon) with no วัน prefix
    "ศุกร์นี้มีนัดหมอ",  # bare "ศุกร์" no วัน prefix, glued
    "อังคารเป็นวันโปรดของฉัน",  # bare "อังคาร" no วัน prefix
    # --- "วัน" + non-weekday word (near miss on the TH weekday form). ---
    "วันหยุดยาวปีนี้สนุกมาก",
    "วันละ 2 ครั้งทุกวัน",
    "วันเกิดลูกชายอายุ 3 ขวบ",
    "วันจันทร์ดีมาก",  # glued: "วันจันทร์" + "ดีมาก" with no whitespace boundary after
    # --- TH days-ago near misses (wrong suffix / reversed order). ---
    "หยุดพัก 2 วัน",  # "2 วัน" with no ที่แล้ว/ก่อน suffix
    "2 วันนี้อากาศดี",  # "วันนี้" not "วันที่แล้ว"/"วันก่อน"
    "กินข้าว 2 มื้อวันนี้",
    # --- Ordinary logs / controls: no date cue anywhere. ---
    "500ml",
    "ยืดเส้น 20 นาที",
    "diary: just a normal day, nothing special",
    "ออกกำลังกาย 30 นาที เหนื่อยมาก",
    "coffee 2 cups",
    "กาแฟ 2 แก้ว",
    # --- Both-sides content (phrase in the true middle, not an edge). ---
    "training log 2 days ago felt great honestly",
    "เมื่อวานไปวิ่งมา แล้วก็กลับบ้านทำงานต่อ",
    # --- Trailing-looking phrase immediately followed by more content
    # (colon or plain continuation) -- the module's documented conservative
    # anchoring choice (see report note on the design deviation). ---
    "stretched 20 min on Monday, or was it Tuesday",
    "diary 2 days ago and also today",
    # --- Empty-ish / punctuation-only residual edge cases. ---
    "yesterday!!!",
    "...เมื่อวาน...",
]


@pytest.mark.parametrize("text", MORE_ADVERSARIAL_NEGATIVES)
def test_larger_ac_c5_adversarial_corpus_zero_false_positives(text):
    assert extract_date(text, _CLOCK, max_days_back=14) is None


def test_ac_c1_positive_strings_still_match_alongside_the_negative_corpus():
    """Sanity anchor: the positive AC-C1 phrases must keep matching even as
    the negative corpus above gets much larger and more varied — the two
    sets must never overlap in behavior."""
    positives = [
        "500ml yesterday",
        "stretched 20 min on Monday",
        "diary 2 days ago",
        "เมื่อวาน ดื่มน้ำ 500",
        "3 วันที่แล้ว 500ml",
        "ยืดเส้น 20 นาที วันจันทร์",
    ]
    for text in positives:
        assert extract_date(text, _CLOCK, max_days_back=14) is not None


# ===========================================================================
# 3. Bounds (AC-C4) at a non-default cap + weekday/bound interaction +
# full-week rotating invariant.
# ===========================================================================


def test_bounds_at_a_non_default_cap_exactly_and_one_past():
    assert extract_date("7 days ago diary", _CLOCK, max_days_back=7) == ("diary", date(2026, 8, 18))
    assert extract_date("8 days ago diary", _CLOCK, max_days_back=7) == OutOfRange("too_old")


def test_weekday_resolution_can_be_out_of_range_against_a_tight_cap():
    # Today is Tuesday 2026-08-25. "on Thursday" resolves to the most
    # recent PAST Thursday = 2026-08-20, which is 5 days back.
    result_ok = extract_date("on Thursday diary", _CLOCK, max_days_back=5)
    assert result_ok == ("diary", date(2026, 8, 20))
    result_too_old = extract_date("on Thursday diary", _CLOCK, max_days_back=4)
    assert result_too_old == OutOfRange("too_old")


def test_extract_date_never_produces_a_future_out_of_range():
    """Design note (see report): every §2.4 recognized phrase only ever
    subtracts days from `today`, so `extract_date` itself can never surface
    `OutOfRange("future")` — that branch is reachable only via
    `resolve_days_back` for the LLM's optional `date_offset`. This is a
    structural property of the recognized grammar, verified across every
    candidate phrase body."""
    probes = [
        "yesterday", "1 days ago", "14 days ago", "on Monday", "last Sunday",
        "เมื่อวาน", "1 วันที่แล้ว", "14 วันก่อน", "วันจันทร์", "วันอาทิตย์",
    ]
    for text in probes:
        result = extract_date(text, _CLOCK, max_days_back=14)
        assert result != OutOfRange("future")


@pytest.mark.parametrize("offset", range(7))
def test_weekday_resolution_never_returns_today_across_a_full_week_rotation(offset):
    """For every possible "today" across a full week, asking for the
    weekday name that IS today must resolve 7 days back, never 0 — the
    invariant behind R-B1's "most recent PAST occurrence" rule, generalized
    beyond the single Tuesday-anchored fixture Luna's suite uses."""
    today_date = _MONDAY + timedelta(days=offset)
    clock = _clock(datetime.combine(today_date, datetime.min.time()))

    en_name = [name for name, idx in backfill._EN_WEEKDAYS.items() if idx == today_date.weekday()][0]
    th_name = [name for name, idx in backfill._TH_WEEKDAYS.items() if idx == today_date.weekday() and name != "พฤหัส"]
    th_name = th_name[0] if th_name else [
        n for n, i in backfill._TH_WEEKDAYS.items() if i == today_date.weekday()
    ][0]

    en_result = extract_date(f"on {en_name} log", clock, max_days_back=14)
    assert en_result == ("log", today_date - timedelta(days=7))

    th_result = extract_date(f"วัน{th_name} log", clock, max_days_back=14)
    assert th_result == ("log", today_date - timedelta(days=7))


def test_resolved_date_exactly_today_falls_through_for_th_days_ago_too():
    assert extract_date("0 วันที่แล้ว 500ml", _CLOCK, max_days_back=14) is None
    assert extract_date("๐ วันก่อน 500ml", _CLOCK, max_days_back=14) is None


# ===========================================================================
# 4. Purity (AST-based), ts-format cross-check, bilingual rendering.
# ===========================================================================

_FORBIDDEN_IMPORT_PREFIXES = (
    "sqlite3",
    "httpx",
    "requests",
    "socket",
    "urllib",
    "habit_assistant.storage",
    "habit_assistant.channels",
    "habit_assistant.llm",
    "habit_assistant.core.ollama_client",
)


def test_ast_verified_no_forbidden_imports():
    """Stronger than a source substring scan: parses the actual import
    statements via `ast` and checks every imported module/attribute against
    the forbidden DB/channel/LLM/network prefixes, so the guarantee holds
    even against an import written in an unusual style."""
    source = inspect.getsource(backfill)
    tree = ast.parse(source)
    imported_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported_names.append(module)
            imported_names.extend(f"{module}.{alias.name}" for alias in node.names)

    for name in imported_names:
        for forbidden in _FORBIDDEN_IMPORT_PREFIXES:
            assert not name.startswith(forbidden), f"forbidden import found: {name}"


def test_ast_verified_allowed_imports_are_the_expected_small_set():
    """Positive complement to the forbidden-import check: assert the actual
    import surface is exactly what the module docstring claims (stdlib +
    `core.i18n` only)."""
    source = inspect.getsource(backfill)
    tree = ast.parse(source)
    top_level_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            top_level_modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top_level_modules.add(node.module.split(".")[0])
    assert top_level_modules <= {"__future__", "re", "dataclasses", "datetime", "typing", "habit_assistant"}


def test_backdated_ts_matches_the_live_log_write_format_exactly():
    """Cross-check against the codebase's own live-log ts stamping
    (`main.py` writes `now.isoformat(timespec="seconds")`, confirmed by
    inspection): a backdated ts must be indistinguishable in shape from a
    live one so every ts-prefix/BETWEEN aggregation treats it identically."""
    live_style = datetime(2026, 8, 18, 14, 32, 7).isoformat(timespec="seconds")
    backdated = backfill.backdated_ts(date(2026, 8, 18))
    assert len(backdated) == len(live_style) == 19
    assert backdated[10] == "T" == live_style[10]
    assert backdated == "2026-08-18T12:00:00"
    # Round-trips through the same parser main.py/aggregations would use.
    assert datetime.fromisoformat(backdated).date() == date(2026, 8, 18)


def test_backdated_ts_handles_leap_day_and_year_boundary():
    assert backfill.backdated_ts(date(2028, 2, 29)) == "2028-02-29T12:00:00"
    assert backfill.backdated_ts(date(2026, 12, 31)) == "2026-12-31T12:00:00"
    assert backfill.backdated_ts(date(2027, 1, 1)) == "2027-01-01T12:00:00"


def test_confirmation_prefix_has_no_unresolved_placeholders_either_language():
    en = backfill.confirmation_prefix(date(2026, 8, 18), "en")
    th = backfill.confirmation_prefix(date(2026, 8, 18), "th")
    for text in (en, th):
        assert "{day}" not in text
        assert "{" not in text and "}" not in text


def test_bounds_error_text_has_no_unresolved_placeholders_either_language():
    for reason in ("future", "too_old"):
        en = backfill.bounds_error_text(OutOfRange(reason), "en", 14)
        th = backfill.bounds_error_text(OutOfRange(reason), "th", 14)
        for text in (en, th):
            assert "{max_days}" not in text
            assert "{" not in text and "}" not in text
        assert "14" in en and "14" in th


def test_th_output_contains_real_thai_text_not_tofu_or_placeholder():
    """Guards against a stub/placeholder catalog entry (e.g. an English
    string reused verbatim for "th", or literal '???'/'TODO')."""
    th_prefix = backfill.confirmation_prefix(date(2026, 8, 18), "th")
    th_future = backfill.bounds_error_text(OutOfRange("future"), "th", 14)
    th_too_old = backfill.bounds_error_text(OutOfRange("too_old"), "th", 14)
    for text in (th_prefix, th_future, th_too_old):
        assert any("฀" <= ch <= "๿" for ch in text), f"no Thai characters found in: {text!r}"
        assert "?" not in text
        assert "TODO" not in text.upper()


def test_th_weekday_and_month_abbreviations_are_distinct_from_english():
    en = backfill.confirmation_prefix(date(2026, 8, 18), "en")
    th = backfill.confirmation_prefix(date(2026, 8, 18), "th")
    assert en != th
    assert "Aug" in en
    assert "ส.ค." in th


# ===========================================================================
# Design deviation ruling: the colon/continuation-suffixed trailing phrase
# is a deliberate conservative choice, not a spec violation. AC-C1 lists
# only the bare form ("diary 2 days ago"); nothing in §2.4/AC-C1 requires
# matching a date phrase followed by additional trailing content. Recorded
# here as an explicit, named regression pin rather than folded silently
# into the adversarial corpus above.
# ===========================================================================


def test_design_deviation_bare_trailing_phrase_matches_but_continuation_does_not():
    assert extract_date("diary 2 days ago", _CLOCK, max_days_back=14) == ("diary", date(2026, 8, 23))
    assert extract_date("diary 2 days ago: had a rough day", _CLOCK, max_days_back=14) is None
