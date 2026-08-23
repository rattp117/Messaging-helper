"""SPEC-v1.4.md §4 "Budget/truncation helper (shared surface)" -- unit
tests for `core/render_budget.py`, extracted from `core/audit_view.py`
(R-B1) so `core/history_view.py` can reuse the exact same machinery
instead of copy-pasting it.

`tests/test_audit_view.py` (unmodified, 82 tests, all still passing) is
the byte-identical regression guard for AC-3 -- this file instead pins
the extracted helpers' own contract in isolation, i18n-agnostically (no
`core/i18n.py` import anywhere below), so `core/history_view.py`'s own
test file can trust this module's behavior without re-deriving it.
"""

from __future__ import annotations

from habit_assistant.core.render_budget import MAX_VALUE_CHARS, TELEGRAM_MESSAGE_BUDGET, fit_within_budget, truncate

# ---------------------------------------------------------------------------
# Constants -- pinned to the exact spec values (R-B1's own interface).
# ---------------------------------------------------------------------------


def test_constants_match_spec_values():
    assert MAX_VALUE_CHARS == 60
    assert TELEGRAM_MESSAGE_BUDGET == 4096


# ---------------------------------------------------------------------------
# truncate()
# ---------------------------------------------------------------------------


def test_truncate_leaves_a_short_string_untouched():
    assert truncate("hello") == "hello"


def test_truncate_leaves_a_string_exactly_at_the_cap_untouched():
    text = "x" * MAX_VALUE_CHARS
    assert truncate(text) == text
    assert len(truncate(text)) == MAX_VALUE_CHARS


def test_truncate_cuts_and_appends_an_ellipsis_past_the_cap():
    text = "x" * (MAX_VALUE_CHARS + 20)
    result = truncate(text)
    assert len(result) == MAX_VALUE_CHARS
    assert result.endswith("…")
    assert result == "x" * (MAX_VALUE_CHARS - 1) + "…"


def test_truncate_honors_an_explicit_max_chars_override():
    assert truncate("abcdefghij", max_chars=5) == "abcd…"


def test_truncate_empty_string_is_untouched():
    assert truncate("") == ""


# ---------------------------------------------------------------------------
# fit_within_budget() -- the structural total-length guard.
# ---------------------------------------------------------------------------


def _footer(dropped: int) -> str:
    return f"… {dropped} more"


def test_fit_within_budget_returns_everything_unchanged_when_already_under_budget():
    header = "Header"
    rows = ["row one", "row two", "row three"]
    result = fit_within_budget(header, rows, render_footer=_footer)
    assert result == "Header\nrow one\nrow two\nrow three"
    assert "more" not in result  # no footer appended -- nothing was dropped


def test_fit_within_budget_drops_oldest_rows_first_when_over_budget():
    """`row_lines` is newest-first by every caller's own convention --
    dropping from the TAIL (the end of the list) is what "drop the
    oldest shown rows" means."""
    header = "H"
    # Each row is 100 chars; budget-sized so only a few fit.
    rows = [f"row-{i:03d}-" + "x" * 90 for i in range(60)]  # 60 rows * ~101 chars >> 4096
    result = fit_within_budget(header, rows, render_footer=_footer)
    assert len(result) <= 4096
    # The newest rows (front of the list) survive; the oldest (tail) are
    # the ones dropped.
    assert "row-000-" in result
    assert "row-059-" not in result
    assert "more" in result


def test_fit_within_budget_footer_reports_the_exact_dropped_count():
    header = "H"
    rows = [f"row-{i:03d}-" + "x" * 90 for i in range(60)]
    result = fit_within_budget(header, rows, render_footer=_footer)
    kept_rows = result.count("row-")
    dropped_reported = 60 - kept_rows
    assert f"… {dropped_reported} more" in result


def test_fit_within_budget_render_footer_is_called_with_the_dropped_count():
    calls = []

    def recording_footer(dropped):
        calls.append(dropped)
        return f"FOOTER:{dropped}"

    header = "H"
    rows = [f"row-{i:03d}-" + "x" * 90 for i in range(60)]
    result = fit_within_budget(header, rows, render_footer=recording_footer)
    assert calls  # footer was invoked at least once
    assert f"FOOTER:{calls[-1]}" in result  # the LAST call's rendering is what's actually kept


def test_fit_within_budget_stays_within_budget_for_a_realistic_worst_case():
    header = "🧾 Recent activity (last 50):"
    rows = [f"• 08-22 14:{i:02d} · you · target set · water · 2500 → 2000 (command)" for i in range(50)]
    result = fit_within_budget(header, rows, render_footer=_footer)
    assert len(result) <= 4096


def test_fit_within_budget_never_drops_below_zero_kept_rows_defensively():
    """Even a pathological case (a single row alone exceeding the budget)
    must not crash or infinite-loop -- the `not kept` floor returns
    whatever's left rather than looping forever."""
    header = "H"
    rows = ["x" * 10000]  # one absurdly long row, alone over budget
    result = fit_within_budget(header, rows, render_footer=_footer)
    assert isinstance(result, str)  # completed without raising/hanging


def test_fit_within_budget_i18n_agnostic_no_i18n_import_needed():
    """The whole point of R-B1: the footer text is entirely caller-
    supplied -- this module never needs to know a language or a catalog
    id. Proven structurally: a Thai footer works exactly the same way as
    an English one, passed as plain callables."""
    header = "หัวข้อ"
    rows = [f"แถวที่ {i}" + "ก" * 90 for i in range(60)]
    result = fit_within_budget(header, rows, render_footer=lambda n: f"… อีก {n} รายการ")
    assert len(result) <= 4096
    assert "อีก" in result
