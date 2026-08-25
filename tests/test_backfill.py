"""SPEC-v1.8.md §4 "Feature -- backfill / retroactive logging (module
`backfill`)", R-B1-R-B6 -- `core/backfill.py`'s deterministic, zero-LLM
EN+TH date-phrase extractor plus its small pure integration helpers.

Owned ACs at the unit level (see AC boundary note in this module's
IMPL report): AC-C1 (the extraction slice: each documented phrase
resolves to the right residual + date), AC-C4 (bounds), AC-C5 (zero
false positives -- the load-bearing guarantee, a large adversarial
EN+TH corpus), AC-C6's own building block (`backdated_ts`, proven
undo-compatible at integration since undo already works by row id
regardless of `ts`), plus every pure helper (`backdated_ts`,
`confirmation_prefix`, `bounds_error_text`, `resolve_days_back`).
AC-C1's full "logs to the correct past date" (through the real
extraction+insert pipeline) and AC-C2/AC-C3/AC-C6's full slice finish
at integration (main.py wiring, not yet built) -- this file proves
everything provable without it.

Four groups of coverage, mirroring `tests/test_preparse.py`'s own
established shape for this codebase's other zero-LLM deterministic
module:

1. The six SPEC-v1.8.md §2.4/AC-C1 documented phrases -- exact
   (residual, date) proof.
2. Broader EN+TH shape coverage: case-insensitivity, singular/plural
   "day(s) ago", every weekday (including "today IS that weekday"),
   Thai/full-width numeral normalization, leading vs trailing
   placement.
3. Zero-false-positive adversarial corpus (AC-C5): diary-style free
   text with a date word buried mid-message (spaced and Thai's own
   no-space prose), a colon-continued trailing phrase, a bare weekday
   with no on/last, bare numbers, ordinary sentences using
   "yesterday"/"เมื่อวาน" as real words, etc.
4. Bounds (AC-C4) + the pure helpers (`backdated_ts`,
   `confirmation_prefix`, `bounds_error_text`, `resolve_days_back`) +
   the structural zero-LLM/zero-DB/zero-channel proof.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime

import pytest

from habit_assistant.core import backfill
from habit_assistant.core.backfill import OutOfRange, extract_date


def _clock(dt: datetime):
    def clock():
        return dt

    return clock


# 2026-08-25 is a Tuesday.
_TODAY = datetime(2026, 8, 25, 9, 0, 0)
_CLOCK = _clock(_TODAY)


# ===========================================================================
# 1. The six SPEC-v1.8.md §2.4/AC-C1 documented phrases.
# ===========================================================================

AC_C1_PHRASES = [
    ("500ml yesterday", "500ml", date(2026, 8, 24)),
    ("stretched 20 min on Monday", "stretched 20 min", date(2026, 8, 24)),
    ("diary 2 days ago", "diary", date(2026, 8, 23)),
    ("เมื่อวาน ดื่มน้ำ 500", "ดื่มน้ำ 500", date(2026, 8, 24)),
    ("3 วันที่แล้ว 500ml", "500ml", date(2026, 8, 22)),
    ("ยืดเส้น 20 นาที วันจันทร์", "ยืดเส้น 20 นาที", date(2026, 8, 24)),
]


@pytest.mark.parametrize("text,residual,target", AC_C1_PHRASES)
def test_ac_c1_documented_phrases_resolve_exactly(text, residual, target):
    assert extract_date(text, _CLOCK, max_days_back=14) == (residual, target)


# ===========================================================================
# 2. Broader EN+TH shape coverage.
# ===========================================================================


def test_yesterday_is_case_insensitive():
    assert extract_date("Yesterday 500ml", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 24))
    assert extract_date("YESTERDAY 500ml", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 24))
    assert extract_date("500ml YeStErDaY", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 24))


def test_days_ago_singular_and_plural():
    assert extract_date("500ml 1 day ago", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 24))
    assert extract_date("500ml 2 days ago", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 23))


def test_on_and_last_weekday_both_recognized():
    assert extract_date("500ml on Monday", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 24))
    assert extract_date("500ml last Monday", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 24))


def test_weekday_case_insensitive():
    assert extract_date("500ml on monday", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 24))
    assert extract_date("500ml on MONDAY", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 24))


@pytest.mark.parametrize(
    "weekday_word,expected",
    [
        ("Monday", date(2026, 8, 24)),
        ("Tuesday", date(2026, 8, 18)),  # today IS Tuesday -> most recent PAST Tuesday, not today
        ("Wednesday", date(2026, 8, 19)),
        ("Thursday", date(2026, 8, 20)),
        ("Friday", date(2026, 8, 21)),
        ("Saturday", date(2026, 8, 22)),
        ("Sunday", date(2026, 8, 23)),
    ],
)
def test_every_en_weekday_resolves_to_the_most_recent_past_occurrence(weekday_word, expected):
    assert extract_date(f"500ml on {weekday_word}", _CLOCK, max_days_back=14) == ("500ml", expected)


@pytest.mark.parametrize(
    "weekday_word,expected",
    [
        ("จันทร์", date(2026, 8, 24)),
        ("อังคาร", date(2026, 8, 18)),  # today IS อังคาร (Tuesday)
        ("พุธ", date(2026, 8, 19)),
        ("พฤหัสบดี", date(2026, 8, 20)),
        ("พฤหัส", date(2026, 8, 20)),
        ("ศุกร์", date(2026, 8, 21)),
        ("เสาร์", date(2026, 8, 22)),
        ("อาทิตย์", date(2026, 8, 23)),
    ],
)
def test_every_th_weekday_resolves_to_the_most_recent_past_occurrence(weekday_word, expected):
    assert extract_date(f"500ml วัน{weekday_word}", _CLOCK, max_days_back=14) == ("500ml", expected)


def test_th_yesterday_with_and_without_trailing_nee():
    assert extract_date("เมื่อวาน 500ml", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 24))
    assert extract_date("เมื่อวานนี้ 500ml", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 24))


def test_th_days_ago_both_word_forms():
    assert extract_date("2 วันที่แล้ว 500ml", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 23))
    assert extract_date("2 วันก่อน 500ml", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 23))


def test_th_days_ago_thai_numerals_normalized():
    assert extract_date("๓ วันที่แล้ว 500ml", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 22))
    assert extract_date("๑๐ วันที่แล้ว 500ml", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 15))


def test_th_days_ago_fullwidth_numerals_normalized():
    assert extract_date("３วันก่อน 500ml", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 22))


def test_leading_and_trailing_placement_both_work_for_the_same_phrase():
    assert extract_date("yesterday 500ml", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 24))
    assert extract_date("500ml yesterday", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 24))


def test_bare_date_phrase_alone_returns_empty_residual():
    assert extract_date("yesterday", _CLOCK, max_days_back=14) == ("", date(2026, 8, 24))


def test_extra_internal_whitespace_in_residual_is_stripped():
    assert extract_date("500ml   yesterday", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 24))
    assert extract_date("yesterday   500ml", _CLOCK, max_days_back=14) == ("500ml", date(2026, 8, 24))


# ===========================================================================
# 3. Zero-false-positive adversarial corpus (AC-C5) -- the load-bearing
# guarantee. Every one of these must return None: no date-word anywhere
# in ordinary content may be stripped unless it is a genuine leading- or
# trailing-anchored whole clause.
# ===========================================================================

ADVERSARIAL_NEGATIVES = [
    # Date word buried mid-sentence, EN, spaced.
    "diary: yesterday was hard",
    "my diary entry: yesterday was a rough day but I pushed through",
    "I heard yesterday it might rain tomorrow",
    "yesterday's weather was nice but today is better",
    # A trailing-looking date phrase followed by MORE content past it
    # (colon-continued) -- not a genuine trailing clause.
    "diary 2 days ago: had a rough day but pushed through",
    "500ml yesterday, or maybe the day before, not sure",
    # Bare weekday, no on/last trigger (EN grammar requires one).
    "500ml Monday",
    "Monday was a good day",
    "gym Monday",
    # "on"/"last" without a real weekday, or a weekday-like word that
    # isn't actually one of the seven.
    "I depend on Monday deliveries followed by more text",
    "last week I went to the gym",
    "on the way home I stretched",
    # Bare numbers / no date cue at all.
    "500ml",
    "2 days",
    "just a normal diary entry about my day",
    # Thai: date word buried mid-sentence, spaced.
    "ไดอารี่: เมื่อวานเหนื่อยมาก",
    "เมื่อวานฝนตกหนักมาก แต่วันนี้อากาศดี",
    # Thai: date word glued with no spaces at all (realistic Thai prose
    # has none) -- can't satisfy the whitespace-boundary requirement.
    "ไดอารี่เมื่อวานเหนื่อยมาก",
    "วันนี้อากาศดีมากเลย",
    # "วัน" + a word that is not one of the seven recognized weekday names.
    "วันเกิดลูกสาวอายุครบ 5 ขวบ",
    "งานวันเกิดสนุกมาก",
    # A Thai date phrase genuinely in the MIDDLE of the message -- content
    # both before and after it, neither a leading nor a trailing clause.
    "ไปเที่ยวมา 3 วันที่แล้ว แล้วก็กลับบ้าน",
    # Empty / whitespace-only.
    "",
    "   ",
]


@pytest.mark.parametrize("text", ADVERSARIAL_NEGATIVES)
def test_ac_c5_zero_false_positives(text):
    assert extract_date(text, _CLOCK, max_days_back=14) is None


# ===========================================================================
# 4. Bounds (AC-C4) + pure helpers + the structural zero-dependency proof.
# ===========================================================================


def test_future_bound_returns_out_of_range_future():
    # `resolve_days_back` is the only way this module's own logic can
    # observe a future result (extract_date's deterministic EN/TH
    # patterns only ever subtract days), but the same bounds function
    # backs both -- proven here directly.
    result = backfill.resolve_days_back(_CLOCK, -1, max_days_back=14)
    assert result == OutOfRange("future")


def test_too_old_bound_returns_out_of_range_too_old():
    result = extract_date("500ml 20 days ago", _CLOCK, max_days_back=14)
    assert result == OutOfRange("too_old")


def test_exactly_at_max_days_back_is_in_bounds():
    result = extract_date("500ml 14 days ago", _CLOCK, max_days_back=14)
    assert result == ("500ml", date(2026, 8, 11))


def test_one_past_max_days_back_is_out_of_range():
    result = extract_date("500ml 15 days ago", _CLOCK, max_days_back=14)
    assert result == OutOfRange("too_old")


def test_resolved_date_exactly_today_falls_through_as_none():
    # "0 days ago" resolves to today -- R-B5: falls through unchanged,
    # same None as "no date phrase recognized at all".
    assert extract_date("500ml 0 days ago", _CLOCK, max_days_back=14) is None


def test_resolve_days_back_mirrors_extract_dates_bounds():
    assert backfill.resolve_days_back(_CLOCK, 0, max_days_back=14) is None
    assert backfill.resolve_days_back(_CLOCK, 1, max_days_back=14) == date(2026, 8, 24)
    assert backfill.resolve_days_back(_CLOCK, 14, max_days_back=14) == date(2026, 8, 11)
    assert backfill.resolve_days_back(_CLOCK, 15, max_days_back=14) == OutOfRange("too_old")
    assert backfill.resolve_days_back(_CLOCK, -5, max_days_back=14) == OutOfRange("future")


def test_backdated_ts_is_local_noon_in_the_established_ts_format():
    ts = backfill.backdated_ts(date(2026, 8, 18))
    assert ts == "2026-08-18T12:00:00"
    # Same shape `main.py` stamps for a live log (`now.isoformat(timespec="seconds")`):
    # parses cleanly and round-trips.
    assert datetime.fromisoformat(ts) == datetime(2026, 8, 18, 12, 0, 0)


def test_confirmation_prefix_bilingual_matches_spec_example_shape():
    en = backfill.confirmation_prefix(date(2026, 8, 18), "en")
    th = backfill.confirmation_prefix(date(2026, 8, 18), "th")
    assert en == "📅 Logged for Tue 18 Aug — "
    assert th == "📅 บันทึกสำหรับ อ. 18 ส.ค. — "


def test_confirmation_prefix_prepends_cleanly_to_a_normal_confirmation():
    prefix = backfill.confirmation_prefix(date(2026, 8, 18), "en")
    normal_confirmation = "💧 500 ml logged — today 500 / 2000 ml (25%)"
    assert prefix + normal_confirmation == "📅 Logged for Tue 18 Aug — 💧 500 ml logged — today 500 / 2000 ml (25%)"


@pytest.mark.parametrize("month,expected_en,expected_th", [
    (1, "Jan", "ม.ค."),
    (12, "Dec", "ธ.ค."),
])
def test_confirmation_prefix_month_abbreviations_at_the_edges(month, expected_en, expected_th):
    d = date(2026, month, 5)
    assert expected_en in backfill.confirmation_prefix(d, "en")
    assert expected_th in backfill.confirmation_prefix(d, "th")


def test_bounds_error_text_future_vs_too_old_bilingual():
    future_en = backfill.bounds_error_text(OutOfRange("future"), "en", 14)
    future_th = backfill.bounds_error_text(OutOfRange("future"), "th", 14)
    too_old_en = backfill.bounds_error_text(OutOfRange("too_old"), "en", 14)
    too_old_th = backfill.bounds_error_text(OutOfRange("too_old"), "th", 14)

    assert "14" in future_en and "14" in future_th
    assert "14" in too_old_en and "14" in too_old_th
    # The two reasons must produce genuinely different copy so a user
    # can tell which bound they hit.
    assert future_en != too_old_en
    assert future_th != too_old_th


def test_out_of_range_is_frozen_and_compares_by_value():
    assert OutOfRange("future") == OutOfRange("future")
    assert OutOfRange("future") != OutOfRange("too_old")
    with pytest.raises(AttributeError):
        OutOfRange("future").reason = "too_old"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Structural zero-LLM/zero-DB/zero-channel proof (mirrors
# tests/test_preparse.py's own "structural zero-LLM proof" group): this
# module must import nothing from llm/storage/channels, and `extract_date`'s
# signature must carry no such parameter.
# ---------------------------------------------------------------------------


def test_module_imports_no_llm_db_or_channel():
    source = inspect.getsource(backfill)
    for forbidden in ("ollama_client", "storage.db", "channels.base", "channels.telegram"):
        assert forbidden not in source


def test_extract_date_signature_carries_no_llm_db_channel_parameter():
    params = set(inspect.signature(extract_date).parameters)
    assert params == {"text", "clock", "max_days_back"}
