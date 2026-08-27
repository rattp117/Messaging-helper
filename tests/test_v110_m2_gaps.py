"""SPEC-v1.10.md "Never lose a log" -- module M2 (`core/reply_attribution.py`,
`core/discoverability.py:build_guide_text`, the `outage_honest_reply` copy,
and the sanctioned EN `{label}` fix) ADVERSARIAL gap suite.

Written by Vera against Luna's `IMPL-v1.10-m2.md`, on top of her own
`tests/test_reply_to_reminder.py` (25) + `tests/test_outage_honesty.py` (13)
+ `tests/test_guide.py` (19) + `tests/test_confirmations.py`'s updated
real-call assertions, which already cover the straightforward AC12/AC13/
AC14/AC15 cases well (70 M2-owned tests total, plus the sanctioned-fix's
own regression updates in test_confirmations.py/test_multi_habit_
integration.py/test_v17_integration.py/test_v17_release_gate.py).

This file probes the corners Archi's dispatch specifically flagged:
- `resolve_reply_value` conservatism at the margins (number+unit, prose
  containing a number, negative/zero boundaries, Thai/full-width numerals
  via the REAL `units.VALUE_RE` + `float()` path, the pinned affirmative-
  token vocabulary against `parser.py`'s own set, an affirmative token on a
  NUMERIC habit, whitespace/emoji-wrapped numbers).
- `build_guide_text`'s claimed commands/behaviors cross-checked against the
  real `commands.dispatch` and the real `resolve_reply_value`.
- Outage-copy format-injection safety (a classic `.format()`-on-user-text
  bug class) and the documented uncapped-quoted-length limitation.
- The sanctioned `{label}` fix: EN goal/no-goal real end-to-end renders
  (`test_confirmations.py` already covers goal EN+TH and no-goal EN via
  `handle_inbound_message`; this file adds the no-goal TH spot-check that
  was missing, plus a catalog-wide EN/TH placeholder-asymmetry sweep to
  confirm no OTHER key has the same bug).
- The `help_edit_cmd` item named in the dispatch note: originally flagged
  by this file as a discrepancy (it did not exist anywhere in the tree at
  the time this suite was written). Archi verified the gap directly and
  dispatched Luna to deliver it as a follow-up; `core/i18n.py:
  help_edit_cmd` and its `build_help_text` append now exist. The test
  below was updated in place (by Luna, per Archi's explicit follow-up
  instruction) from "pin the absence" to "verify presence + accuracy
  against the real `_EDIT_TRIGGER` matcher" -- see its own docstring.

Same "real DB/registry, no mocks for cheap deterministic things" posture as
`tests/test_v110_m1_gaps.py`. `core/routing.py`'s reply-attribution branch,
outage-message gate, and `/guide` dispatch are NOT exercised here -- per
Archi's dispatch note and `IMPL-v1.10-m2.md`'s own "Known limitations",
that wiring has not landed yet as of this tree state (verified directly:
`handle_inbound_message` accepts `reply_to_message_id` but its own
docstring says "plumbing ONLY... Not yet read by this function's own
body"; the deferral branch still sends bare `deferred_ack` unconditionally;
there is no `command.kind == "guide"` branch; `core/app.py` has no
`GUIDE_COMMAND_DESCRIPTIONS`). These are DEFERRED SLICES for the
integration seam, listed in TEST-v1.10-m2.md, not failures of this module.
"""

from __future__ import annotations

import re
import sys

import pytest

from habit_assistant.config import Config
from habit_assistant.core import commands, confirmation, discoverability, i18n
from habit_assistant.core import parser as parser_module
from habit_assistant.core import quicklog as quicklog_module
from habit_assistant.core import reply_attribution
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.render_budget import TELEGRAM_MESSAGE_BUDGET, truncate
from habit_assistant.core.routing import _OUTAGE_QUOTE_MAX_CHARS


def _habit(id_: str, type_: str, *, unit_en: str | None = "ml", unit_th: str | None = "มล.") -> Habit:
    return Habit(
        id=id_,
        type=type_,
        label_en=id_,
        label_th=id_,
        unit_en=unit_en if type_ in ("numeric", "duration") else None,
        unit_th=unit_th if type_ in ("numeric", "duration") else None,
        goal=2000 if type_ in ("numeric", "duration") else None,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


WATER = _habit("water", "numeric", unit_en="ml", unit_th="มล.")
STRETCH = _habit("stretch", "duration", unit_en="min", unit_th="นาที")
MEDS = _habit("meds", "boolean")
DIARY = _habit("diary", "text")


# ===========================================================================
# resolve_reply_value -- conservatism at the margins (AC13's teeth)
# ===========================================================================


@pytest.mark.parametrize("text", ["500ml", "500 ml", "500ml ", " 500ml", "500\tml"])
def test_number_plus_unit_never_resolves_regardless_of_spacing(text):
    """R14: a number+unit reply is deliberately left to the SAME preparse
    path a typed message would take, never resolved here -- re-asserted
    with more spacing/whitespace variants than the happy-path suite."""
    assert reply_attribution.resolve_reply_value(text, WATER) is None


@pytest.mark.parametrize(
    "text",
    ["ran 5k", "5 apples eaten", "I did 500", "500 today", "drank 500", "500!"],
)
def test_prose_containing_a_bare_number_falls_through(text):
    """The spec's own conservatism requirement is BARE number only --
    VALUE_RE is anchored (`^...$`), so any leading/trailing word (even
    just a trailing "!" ) breaks the anchor and the whole reply falls
    through, exactly like true prose."""
    assert reply_attribution.resolve_reply_value(text, WATER) is None


@pytest.mark.parametrize("text", ["0", "-0", "0.0", "-5", "-5.5", "00", "0.00"])
def test_zero_and_negative_boundary_values_all_fall_through(text):
    assert reply_attribution.resolve_reply_value(text, WATER) is None
    assert reply_attribution.resolve_reply_value(text, STRETCH) is None


# --- Thai / full-width numerals: VERIFY the real path, not just trust the
# claim. `python -c "float('๕๐๐')"` was independently confirmed (outside
# this file) to return 500.0 -- re.match's \d matches any Unicode Nd-
# category digit by default (VALUE_RE has no re.ASCII flag) and float()
# does the same, so no special-case normalization is needed. These tests
# pin that behavior against the REAL `units.VALUE_RE` + `float()` call
# inside `resolve_reply_value` itself (not a standalone snippet).
@pytest.mark.parametrize("text", ["๕๐๐", "５００", "  ๕๐๐  ", "\t５００\n"])
def test_thai_and_fullwidth_numerals_resolve_via_the_real_value_re_path(text):
    assert reply_attribution.resolve_reply_value(text, WATER) == 500.0


@pytest.mark.parametrize("text", ["๕๐๐มล.", "５００ml", "๕๐๐ ml"])
def test_thai_or_fullwidth_number_WITH_a_unit_still_falls_through(text):
    """Conservatism holds across scripts too -- a Thai/full-width bare
    number resolves, but the same number with a unit token attached does
    not, mirroring the ASCII "500ml" case exactly."""
    assert reply_attribution.resolve_reply_value(text, WATER) is None


def test_thai_numeral_resolves_for_a_duration_habit_too():
    assert reply_attribution.resolve_reply_value("๓๐", STRETCH) == 30.0


# --- Affirmative-token vocabulary: PIN the exact set and prove it mirrors
# `parser.py`'s own `_BOOL_TRUTHY` (the module's own docstring claims this
# mirror by inspection; verify it's not just claimed but actually equal).


def test_affirmative_tokens_mirror_parser_bool_truthy_exactly():
    assert reply_attribution._AFFIRMATIVE_TOKENS == parser_module._BOOL_TRUTHY == {
        "true",
        "1",
        "done",
        "yes",
        "ครบ",
        "แล้ว",
    }


@pytest.mark.parametrize("text", ["true", "TRUE", "True", "1", "done", "Done", "DONE", "yes", "Yes", "YES", "ครบ", "แล้ว"])
def test_every_pinned_affirmative_token_resolves_for_a_boolean_habit(text):
    assert reply_attribution.resolve_reply_value(text, MEDS) == 1.0


@pytest.mark.parametrize(
    "text",
    ["ok", "OK", "k", "sure", "yep", "yeah", "เสร็จ", "เอา", "อืม", "y", "affirmative", "roger"],
)
def test_plausible_but_unpinned_affirmative_words_do_NOT_resolve(text):
    """Documents the exact boundary R14 draws: these all read as "yes" in
    natural conversation but are NOT in the established
    `_AFFIRMATIVE_TOKENS`/`_BOOL_TRUTHY` vocabulary, so they fall through
    to the normal path (which may still ask the LLM) rather than being
    guessed. Not a bug -- R14's own "everything else -> None" -- but worth
    pinning explicitly since a user could reasonably expect "ok" to work."""
    assert reply_attribution.resolve_reply_value(text, MEDS) is None


def test_bare_digit_one_means_different_things_for_boolean_vs_numeric():
    """"1" is BOTH an affirmative token AND a valid bare positive number --
    the two branches never actually conflict because they're gated on
    `habit.type` first: for a boolean habit "1" -> 1.0 via the
    affirmative-token branch; for a numeric habit "1" -> 1.0 via the
    bare-number branch (logging 1 ml of water, not "yes"). Distinct code
    paths, same numeric result -- worth pinning so a future refactor can't
    silently swap which branch handles it without a test noticing."""
    assert reply_attribution.resolve_reply_value("1", MEDS) == 1.0
    assert reply_attribution.resolve_reply_value("1", WATER) == 1.0


@pytest.mark.parametrize("text", ["yes", "done", "true", "ครบ"])
def test_affirmative_tokens_do_NOT_resolve_for_a_numeric_habit(text):
    """A numeric/duration habit only accepts a bare NUMBER -- an
    affirmative word (that isn't also a valid number, unlike "1") has no
    leading digit, so `VALUE_RE.match` fails immediately and the reply
    falls through, regardless of how "affirmative" it reads."""
    assert reply_attribution.resolve_reply_value(text, WATER) is None
    assert reply_attribution.resolve_reply_value(text, STRETCH) is None


@pytest.mark.parametrize("text", ["no", "No", "NO", "false", "0", "ยัง"])
def test_falsy_tokens_never_resolve_for_a_boolean_habit(text):
    """R14's "not guessed as 0.0" -- a negative token is left to the
    normal path, never auto-resolved to a falsy log here."""
    assert reply_attribution.resolve_reply_value(text, MEDS) is None


# --- Whitespace / emoji-wrapped numbers.


@pytest.mark.parametrize("text", ["\t500\n", "\r\n 500 \r\n", " 500 "])
def test_whitespace_variants_around_a_bare_number_still_resolve(text):
    """Plain `str.strip()` (used internally) strips tabs/newlines and
    (on this Python) NBSP -- these should resolve the same as a plain
    "500"."""
    assert reply_attribution.resolve_reply_value(text, WATER) == 500.0


@pytest.mark.parametrize("text", ["\U0001F600500", "500\U0001F600", "\U0001F600 500 \U0001F600", "5️⃣00"])
def test_emoji_adjacent_to_a_number_breaks_the_bare_number_anchor(text):
    """An emoji glued to (or wrapping) the digits is not whitespace, so it
    either breaks the leading-digit anchor or is captured as a "unit"
    token -- either way, conservatively None, never a misparsed value."""
    assert reply_attribution.resolve_reply_value(text, WATER) is None


def test_empty_and_whitespace_only_text_falls_through():
    assert reply_attribution.resolve_reply_value("", WATER) is None
    assert reply_attribution.resolve_reply_value("   ", WATER) is None
    assert reply_attribution.resolve_reply_value("", MEDS) is None


def test_text_habit_rejects_everything_including_a_bare_number():
    assert reply_attribution.resolve_reply_value("500", DIARY) is None
    assert reply_attribution.resolve_reply_value("yes", DIARY) is None


# ===========================================================================
# build_guide_text -- cross-checked against the real commands module and
# the real resolve_reply_value behavior (not just its own copy).
# ===========================================================================


@pytest.mark.parametrize("cmd,expected_kind", [("/log", "log"), ("/undo", "undo"), ("/target", "target"), ("/habits", "habits"), ("/help", "help")])
def test_every_command_the_guide_claims_actually_dispatches(cmd, expected_kind):
    """`guide_key_commands`/`guide_how_to_log` name /log, /undo, /target,
    /habits, /help -- verify each is a REAL, currently-recognized command
    (not a stale/typo'd name) via the real `commands.dispatch`, not a
    hardcoded assumption."""
    registry = HabitRegistry([])
    command = commands.dispatch(cmd, registry)
    assert command is not None, f"{cmd!r} did not dispatch to any command at all"
    assert command.kind == expected_kind


@pytest.mark.parametrize("lang", ["en", "th"])
def test_guide_text_contains_no_replacement_characters(lang):
    """No tofu/mojibake -- the catalog entries are plain Python string
    literals in a UTF-8 source file, but this guards against an encoding
    mishap surviving into the rendered text."""
    text = discoverability.build_guide_text(Config(), lang)
    assert "�" not in text


def test_guide_message_syntax_claim_matches_real_resolve_reply_value_behavior():
    """`guide_message_syntax` tells the user: reply to a reminder with
    "just a number" to log it. Cross-check the claim against the REAL
    `resolve_reply_value` for the worked example it uses ("500") -- if
    this function's behavior ever drifted from the guide's own promise,
    this test would catch the documentation/behavior split."""
    text_en = discoverability.build_guide_text(Config(), "en")
    assert '"500"' in text_en
    assert reply_attribution.resolve_reply_value("500", WATER) == 500.0


# ===========================================================================
# Outage-copy adversarial: format-injection safety + the documented
# uncapped-length limitation.
# ===========================================================================


@pytest.mark.parametrize(
    "evil",
    ['{evil}', '{0}', '{', '}', '{{}}', '{text}', '{value:g}', '{missing!r}', '%s', '{0.__class__}'],
)
@pytest.mark.parametrize("lang", ["en", "th"])
def test_outage_honest_reply_is_format_injection_safe(evil, lang):
    """`i18n.t` does `TEMPLATE.format(**kwargs)` -- the user's raw text is
    a VALUE substituted into the template, never re-parsed as a format
    string itself, so brace-laden user text cannot raise KeyError/
    IndexError or trigger attribute access. Verified here against the
    REAL `i18n.t` call, not a description of `str.format`'s semantics --
    this is exactly the classic ".format() on attacker-controlled text"
    bug class the dispatch flagged, and it does NOT reproduce here."""
    rendered = i18n.t("outage_honest_reply", lang, text=evil)
    assert evil in rendered


def test_outage_honest_reply_format_injection_safe_for_closure_and_clarify_too():
    """Sibling M1 copy (`closure_notification`/`clarify_offer`) shares the
    exact same `{text}`-quotes-raw-message shape -- confirm the same
    safety property holds there (defense-in-depth spot-check, not a
    duplicate of M1's own suite)."""
    for msg_id in ("closure_notification", "clarify_offer"):
        for lang in ("en", "th"):
            rendered = i18n.t(msg_id, lang, text="{evil}")
            assert "{evil}" in rendered


@pytest.mark.parametrize("lang", ["en", "th"])
def test_outage_honest_reply_headroom_before_the_raw_message_is_added(lang):
    rendered = i18n.t("outage_honest_reply", lang, text="")
    assert len(rendered) < 250  # fixed overhead only, well under budget


# -----------------------------------------------------------------------
# FIXED at the integration pass (Archi-sanctioned extra, item 4):
# core/routing.py's outage-honesty branch now truncates the QUOTED portion
# of the message via `render_budget.truncate(text, max_chars=_OUTAGE_QUOTE_
# MAX_CHARS)` BEFORE calling `i18n.t` -- mirroring `core/clarify.py`'s
# identical `_QUOTE_MAX_CHARS`/`_quote` fix for the closure/offer messages
# (module M1, TEST-v1.10-m1.md's own round-2 finding 2). `i18n.t(
# "outage_honest_reply", ...)` itself is STILL uncapped when called
# directly with a raw, untruncated `text` -- correctly so, matching
# `tests/test_outage_honesty.py::test_outage_honest_reply_handles_a_long_
# raw_message_without_crashing`'s own "not truncated at the catalog layer"
# contract; the cap lives at routing.py's call site, not inside the copy.
# The two tests below were an `xfail` (documenting the gap) and a plain
# pinning assertion (measuring it) before this fix landed; both are now
# positive regression guards.
# -----------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "th"])
def test_outage_honest_reply_stays_within_telegram_budget_for_a_near_max_inbound_message(lang):
    """Was `xfail` (KNOWN LIMITATION) before the routing-side quote cap
    landed; now a passing regression guard, driven through the exact same
    mechanism `core/routing.py`'s outage branch calls."""
    near_max_inbound = "x" * 4000  # a realistic large Telegram inbound text
    quoted = truncate(near_max_inbound, max_chars=_OUTAGE_QUOTE_MAX_CHARS)
    rendered = i18n.t("outage_honest_reply", lang, text=quoted)
    assert len(rendered) <= TELEGRAM_MESSAGE_BUDGET


@pytest.mark.parametrize("lang", ["en", "th"])
def test_outage_honest_reply_overflow_is_fixed_by_the_routing_quote_cap(lang):
    """Was `test_outage_honest_reply_length_overflow_is_reproducible_and_
    measured`, pinning the CURRENT (pre-fix) overflow. Updated alongside
    the fix per its own docstring's instruction: the uncapped-copy overflow
    is still real and still measured (proving the cap, not a coincidence,
    is what fixes it), but the ACTUAL routing.py call site never sends the
    uncapped form."""
    near_max_inbound = "x" * 4000
    uncapped = i18n.t("outage_honest_reply", lang, text=near_max_inbound)
    overflow = len(uncapped) - TELEGRAM_MESSAGE_BUDGET
    assert overflow > 0  # i18n.t itself is deliberately not capped (see note above)
    assert overflow < 300  # sanity bound: overflow is bounded by the fixed-overhead size, not unbounded

    quoted = truncate(near_max_inbound, max_chars=_OUTAGE_QUOTE_MAX_CHARS)
    capped = i18n.t("outage_honest_reply", lang, text=quoted)
    assert len(capped) <= TELEGRAM_MESSAGE_BUDGET
    assert len(capped) < 500


# ===========================================================================
# The sanctioned {label} fix -- real-call gap (no-goal TH spot check was
# missing from test_confirmations.py) + a catalog-wide asymmetry sweep.
# ===========================================================================


def test_confirm_numeric_nogoal_th_byte_identical_pre_and_post_fix():
    """TH side of confirm_numeric_nogoal was NOT touched by the sanctioned
    fix (git diff confirms only the EN line changed) -- pin the exact TH
    string so a future edit can't silently drift it while "fixing" EN."""
    rendered = i18n.t("confirm_numeric_nogoal", "th", label="ก้าว", value=8000, unit="ก้าว")
    assert rendered == "✅ บันทึกก้าว 8000 ก้าว แล้ว วันนี้"


def test_confirm_numeric_goal_th_byte_identical_pre_and_post_fix():
    rendered = i18n.t("confirm_numeric_goal", "th", label="นอน", value=7, unit="ชม.", total=7, goal=8, pct=88)
    assert rendered == "✅ บันทึกนอน 7 ชม. แล้ว — วันนี้ 7 / 8 ชม. (88%)"


@pytest.mark.parametrize("msg_id", ["water_confirmation", "stretch_confirmation", "diary_confirmation"])
def test_base_habit_dedicated_keys_have_no_label_placeholder(msg_id):
    """Water/stretch/diary never need {label} -- the confirmation IS the
    habit (IMPL-v1.10-m2.md's own stated rationale for leaving these
    untouched). Confirms they weren't swept up by the fix."""
    assert "{label}" not in i18n.CATALOG[msg_id]["en"]
    assert "{label}" not in i18n.CATALOG[msg_id]["th"]


def test_confirm_duration_en_was_not_touched_it_already_had_label():
    assert i18n.CATALOG["confirm_duration"]["en"] == "✅ {value:g} {unit} {label} logged — {ordinal} today"


def test_quicklog_shares_the_same_confirmation_module_not_a_private_copy():
    """R9/parity: `core/quicklog.py` imports the `confirmation` MODULE
    (not a copied function), so the sanctioned fix is automatically
    shared by the button-tap path with zero quicklog.py edit needed --
    verified here as a real identity check, not by re-reading the
    import line."""
    assert quicklog_module.confirmation.confirmation_text is confirmation.confirmation_text
    assert quicklog_module.confirmation is sys.modules["habit_assistant.core.confirmation"]


_FIELD_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)")

# The four keys with a KNOWN, benign EN/TH placeholder difference: EN uses
# `{ordinal}` ("3rd today"), TH uses `{count}` ("ครั้งที่ 3") for the exact
# same underlying occurrence-count -- `confirmation.py` always passes BOTH
# kwargs together (see `ordinal=ordinal(count), count=count` at every call
# site), so this is a language-appropriate phrasing choice, not information
# loss, and not the `{label}`-omission bug class the sanctioned fix
# addressed. Any OTHER asymmetry found by the sweep below is a NEW finding.
_KNOWN_BENIGN_ORDINAL_COUNT_ASYMMETRY = {
    "stretch_confirmation",
    "edit_updated_stretch",
    "confirm_duration",
    "edit_updated_duration",
}


def test_catalog_wide_en_th_placeholder_asymmetry_sweep():
    """The catalog-wide sweep the dispatch asked for: "any other confirm_*/
    celebr_* keys where EN lacks a placeholder TH has?" -- generalized to
    EVERY catalog key (393 as of this tree), not just the two prefixes,
    since a scan is equally cheap either way and a `{label}`-class bug
    could in principle hide under a different key prefix. Confirms the
    sanctioned fix's own claim ("exhaustive... found via grep for every
    non-water/stretch generic-numeric confirmation string") by an
    independent, structural (not grep-based) method: parse every {field}
    name out of both language variants of every key and diff the sets.

    Result at time of writing: exactly 4 keys differ, all in the known-
    benign ordinal/count set above (confirmed by inspecting `confirmation.
    py`'s call sites: `ordinal` and `count` are always passed together).
    `confirm_numeric_goal`/`confirm_numeric_nogoal` do NOT appear here --
    the sanctioned fix closed that asymmetry. If this test ever fails, a
    NEW key has drifted and needs the same kind of judgment call this
    comment documents: is it a benign phrasing choice (add to the allow-
    list, ideally with the corroborating call-site evidence) or a genuine
    EN-omits-information bug (send to Luna)."""
    unexpected: list[tuple[str, set[str], set[str]]] = []
    for key, variants in i18n.CATALOG.items():
        en_fields = set(_FIELD_RE.findall(variants.get("en", "")))
        th_fields = set(_FIELD_RE.findall(variants.get("th", "")))
        if en_fields != th_fields and key not in _KNOWN_BENIGN_ORDINAL_COUNT_ASYMMETRY:
            unexpected.append((key, en_fields, th_fields))

    assert unexpected == [], (
        "New EN/TH placeholder asymmetry found (possible {label}-class bug, "
        f"or a benign difference that needs adding to the allowlist with "
        f"call-site evidence): {unexpected}"
    )


def test_known_benign_asymmetry_keys_really_do_get_both_kwargs_at_every_call_site():
    """Don't just trust the allowlist's own comment -- prove `ordinal` and
    `count` are structurally coupled by rendering both templates with
    only ONE of the two kwargs omitted and confirming it raises (i.e.
    `confirmation.py` really must supply both, so neither language variant
    can ever be called with a missing kwarg in production)."""
    with pytest.raises(KeyError):
        i18n.t("confirm_duration", "en", label="x", value=1, unit="u", count=1)  # missing ordinal
    with pytest.raises(KeyError):
        i18n.t("confirm_duration", "th", label="x", value=1, unit="u", ordinal="1st")  # missing count


# ===========================================================================
# help_edit_cmd -- RESOLVED. Originally named in Archi's dispatch note but
# absent from the tree when this suite was first written (this test used
# to pin that absence -- see git history / IMPL-v1.10-m2.md's iteration
# log for the full "gap caught by Vera, closed by Luna on Archi's direct
# follow-up" story). Now verifies presence + accuracy instead.
# ===========================================================================


def test_help_edit_cmd_now_exists_and_is_wired_into_build_help_text():
    """SPEC-v1.10.md's AC list (section 8) has no acceptance criterion for
    a `/help` line describing `/edit` -- R16/R17/AC15 are `/guide` only --
    so this remains a discoverability nicety, not an AC gate. But it was
    named explicitly in Archi's dispatch note and IS now delivered:
    `help_edit_cmd` exists with both languages and renders inside
    `build_help_text` for both."""
    assert "help_edit_cmd" in i18n.CATALOG
    assert set(i18n.CATALOG["help_edit_cmd"]) == {"en", "th"}

    config = Config()
    for lang in ("en", "th"):
        text = discoverability.build_help_text(config, lang)
        assert i18n.t("help_edit_cmd", lang) in text


def test_help_edit_cmd_phrases_are_real_edit_triggers_not_invented_copy():
    """Cross-checks the quoted example phrases against the REAL
    `core/commands.py:_EDIT_TRIGGER` regex -- the failure mode this test
    guards against is plausible-sounding but wrong help copy (e.g. a typo'd
    trigger phrase that would silently never actually work)."""
    from habit_assistant.core.commands import _EDIT_TRIGGER

    en_text = i18n.t("help_edit_cmd", "en")
    th_text = i18n.t("help_edit_cmd", "th")

    for phrase in ("make that 500ml", "change it to 500ml", "/edit 500ml"):
        assert phrase in en_text
        match = _EDIT_TRIGGER.match(phrase)
        assert match is not None, f"{phrase!r} (quoted in help_edit_cmd EN) is not a real /edit trigger"
        assert match.group("value") == "500ml"

    for phrase in ("แก้เป็น 500 มล.", "แก้ไขเป็น 500 มล."):
        assert phrase in th_text
        assert _EDIT_TRIGGER.match(phrase) is not None, f"{phrase!r} (quoted in help_edit_cmd TH) is not a real /edit trigger"
