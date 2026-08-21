"""SPEC-v1.2.md "Multi-user support", module `preferences`: the
deterministic, LLM-free `/lang`/`/quiet` path -- `core/commands.dispatch`'s
`"lang"`/`"quiet"` kinds (R-P1/R-P2) and `core/preferences.execute_lang`/
`execute_quiet`.

Owned ACs (SPEC-v1.2.md §11): AC-P1 (language), AC-P2 (quiet hours). Both
are proven here by composing this module's WRITE side (`execute_lang`/
`execute_quiet`, writing `users.language_pref`/`users.quiet_hours_json`)
directly with the shared surface's already-landed READ side
(`core/i18n.resolve_reply_language`/`resolve_unprompted_language`'s
`user_pref` parameter, `core/reminders.effective_quiet_windows`) -- no
`main.py` involved, since routing `Command.kind in ("lang", "quiet")` to
these two functions is integration's job (SPEC-v1.2.md §11), landed after
all three parallel modules report done. This mirrors
`tests/test_targets.py`'s own convention: `commands.dispatch(text,
registry)` directly, `execute_*` called directly against a real on-disk
SQLite `Database` (no mocks for the DB)."""

from __future__ import annotations

import json
import sqlite3
from datetime import time as dt_time

import pytest

from habit_assistant.config import Config
from habit_assistant.core import commands, i18n
from habit_assistant.core.commands import Command
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.preferences import execute_lang, execute_quiet
from habit_assistant.core.reminders import _in_quiet_hours, effective_quiet_windows
from habit_assistant.storage.db import Database

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())

OWNER = "owner-chat"
USER_B = "member-chat-b"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "preferences.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(USER_B, role="member", status="active")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


# ===========================================================================
# dispatch() shape -- /lang
# ===========================================================================


@pytest.mark.parametrize(
    "text,expected_value",
    [
        ("/lang en", "en"),
        ("/lang th", "th"),
        ("/lang auto", "auto"),
        ("/LANG TH", "th"),  # case-insensitive slash trigger
        ("ภาษา th", "th"),
        ("ภาษา en", "en"),
        ("ภาษา auto", "auto"),
    ],
)
def test_dispatch_recognizes_lang_shape(text, expected_value):
    assert commands.dispatch(text, DEFAULT_REGISTRY) == Command(kind="lang", pref_value=expected_value)


def test_dispatch_recognizes_bare_lang_as_usage_shape():
    assert commands.dispatch("/lang", DEFAULT_REGISTRY) == Command(kind="lang", pref_value=None)


def test_dispatch_lang_carries_through_an_unrecognized_value_unvalidated():
    """Shape-only layer: `core/commands.py` doesn't validate the value --
    `execute_lang` is where an unrecognized code becomes `lang_usage`."""
    assert commands.dispatch("/lang thai", DEFAULT_REGISTRY) == Command(kind="lang", pref_value="thai")


def test_dispatch_bare_thai_alias_lang_does_not_match():
    """Unlike the slash form, `ภาษา` alone (no value) is NOT a recognized
    shape -- mirrors `/remind`'s Thai alias `เตือน`'s own "always requires
    a value" contract, reducing false-positive risk on a bare Thai word."""
    assert commands.dispatch("ภาษา", DEFAULT_REGISTRY) is None


# ===========================================================================
# dispatch() shape -- /quiet
# ===========================================================================


@pytest.mark.parametrize(
    "text,expected_value",
    [
        ("/quiet 22:00-07:00", "22:00-07:00"),
        ("/quiet 22:00-07:00,10:00-11:00", "22:00-07:00,10:00-11:00"),
        ("/quiet off", "off"),
        ("/QUIET OFF", "off"),
        ("เงียบ 22:00-07:00", "22:00-07:00"),
        ("เงียบ off", "off"),
    ],
)
def test_dispatch_recognizes_quiet_shape(text, expected_value):
    assert commands.dispatch(text, DEFAULT_REGISTRY) == Command(kind="quiet", pref_value=expected_value)


def test_dispatch_recognizes_bare_quiet_as_usage_shape():
    assert commands.dispatch("/quiet", DEFAULT_REGISTRY) == Command(kind="quiet", pref_value=None)


def test_dispatch_quiet_carries_through_a_malformed_window_unvalidated():
    assert commands.dispatch("/quiet 25:99", DEFAULT_REGISTRY) == Command(kind="quiet", pref_value="25:99")


def test_dispatch_bare_thai_alias_quiet_does_not_match():
    assert commands.dispatch("เงียบ", DEFAULT_REGISTRY) is None


# ===========================================================================
# adversarial corpus -- normal messages that merely mention "lang"/"quiet"/
# "ภาษา"/"เงียบ" must reach the parser (dispatch returns None), same
# AC5.5 conservatism every other command in this router honors.
# ===========================================================================

ADVERSARIAL_MESSAGES = [
    "I'm learning a new language this year",  # contains "language", not "/lang"
    "the room went quiet after the announcement",  # contains "quiet", not "/quiet"
    "เรียนภาษาอังกฤษทุกวันนี้",  # "ภาษา" mid-word, no leading space -- not anchored at start
    "วันนี้เงียบมาก ไม่มีใครพูดอะไรเลย",  # "เงียบ" mid-sentence with trailing text after the space-separated word, not the whole message
]


@pytest.mark.parametrize("message", ADVERSARIAL_MESSAGES)
def test_dispatch_returns_none_for_normal_messages_mentioning_lang_or_quiet(message):
    assert commands.dispatch(message, DEFAULT_REGISTRY) is None


# ===========================================================================
# execute_lang -- writes users.language_pref, replies bilingually.
# ===========================================================================


async def test_execute_lang_th_writes_preference_and_confirms_in_thai(db):
    command = commands.dispatch("/lang th", DEFAULT_REGISTRY)
    reply = await execute_lang(command, db=db, lang="en", user_id=USER_B)

    assert db.get_user(USER_B)["language_pref"] == "th"
    assert reply == i18n.t("lang_set", "th", value="th")  # confirms in the NEWLY set language


async def test_execute_lang_en_confirms_in_english_even_if_caller_lang_was_thai(db):
    command = commands.dispatch("/lang en", DEFAULT_REGISTRY)
    reply = await execute_lang(command, db=db, lang="th", user_id=USER_B)

    assert db.get_user(USER_B)["language_pref"] == "en"
    assert reply == i18n.t("lang_set", "en", value="en")


async def test_execute_lang_auto_falls_back_to_caller_supplied_lang(db):
    command = commands.dispatch("/lang auto", DEFAULT_REGISTRY)
    reply = await execute_lang(command, db=db, lang="th", user_id=USER_B)

    assert db.get_user(USER_B)["language_pref"] == "auto"
    assert reply == i18n.t("lang_set", "th", value="auto")


async def test_execute_lang_invalid_value_writes_nothing_and_replies_usage(db):
    command = commands.dispatch("/lang thai", DEFAULT_REGISTRY)
    reply = await execute_lang(command, db=db, lang="en", user_id=USER_B)

    assert db.get_user(USER_B)["language_pref"] == "auto"  # unchanged from upsert_user default
    assert reply == i18n.t("lang_usage", "en")


async def test_execute_lang_bare_command_writes_nothing_and_replies_usage(db):
    command = commands.dispatch("/lang", DEFAULT_REGISTRY)
    reply = await execute_lang(command, db=db, lang="th", user_id=USER_B)

    assert db.get_user(USER_B)["language_pref"] == "auto"
    assert reply == i18n.t("lang_usage", "th")


async def test_execute_lang_db_failure_reports_save_failed_not_a_traceback(db, monkeypatch):
    def _boom(self, chat_id, pref):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(Database, "set_user_language", _boom)
    command = commands.dispatch("/lang th", DEFAULT_REGISTRY)
    reply = await execute_lang(command, db=db, lang="en", user_id=USER_B)

    assert reply == i18n.t("preferences_save_failed", "th")  # reply_lang is "th" (the value just set)


# ===========================================================================
# AC-P1 -- composed with the shared surface's READ side: B's replies
# resolve to Thai regardless of input language after /lang th; the owner
# (default "auto") is unaffected by B's change.
# ===========================================================================


async def test_ac_p1_lang_th_makes_replies_thai_regardless_of_input_language(db, config):
    command = commands.dispatch("/lang th", DEFAULT_REGISTRY)
    await execute_lang(command, db=db, lang="en", user_id=USER_B)

    b_pref = db.get_user(USER_B)["language_pref"]
    assert i18n.resolve_reply_language("500ml", config, user_pref=b_pref) == "th"
    assert i18n.resolve_reply_language("today was a great day", config, user_pref=b_pref) == "th"
    assert i18n.resolve_unprompted_language(config, user_pref=b_pref) == "th"


async def test_ac_p1_owner_default_auto_is_unaffected_by_member_lang_change(db, config):
    command = commands.dispatch("/lang th", DEFAULT_REGISTRY)
    await execute_lang(command, db=db, lang="en", user_id=USER_B)

    owner_pref = db.get_user(OWNER)["language_pref"]
    assert owner_pref == "auto"
    # "auto" + no Thai characters in the inbound text -> English, exactly v1.1 behavior.
    assert i18n.resolve_reply_language("500ml", config, user_pref=owner_pref) == "en"
    assert i18n.resolve_reply_language("น้ำ 500 มล.", config, user_pref=owner_pref) == "th"


# ===========================================================================
# execute_quiet -- writes users.quiet_hours_json, replies bilingually.
# ===========================================================================


async def test_execute_quiet_single_window_writes_json_and_confirms(db):
    command = commands.dispatch("/quiet 22:00-07:00", DEFAULT_REGISTRY)
    reply = await execute_quiet(command, db=db, lang="en", user_id=USER_B)

    assert effective_quiet_windows(db, Config(), USER_B) == [("22:00", "07:00")]
    assert reply == i18n.t("quiet_set", "en", windows="22:00-07:00")


async def test_execute_quiet_multiple_windows_writes_all(db):
    command = commands.dispatch("/quiet 22:00-07:00,12:00-13:00", DEFAULT_REGISTRY)
    await execute_quiet(command, db=db, lang="en", user_id=USER_B)

    assert effective_quiet_windows(db, Config(), USER_B) == [("22:00", "07:00"), ("12:00", "13:00")]


async def test_execute_quiet_off_stores_explicit_empty_list_distinct_from_unset(db, config):
    # USER_B has never set a window -- inherits config default (empty here).
    assert effective_quiet_windows(db, config, USER_B) == config.quiet_hours.windows

    command = commands.dispatch("/quiet off", DEFAULT_REGISTRY)
    reply = await execute_quiet(command, db=db, lang="en", user_id=USER_B)

    assert db.get_user(USER_B)["quiet_hours_json"] == "[]"
    assert effective_quiet_windows(db, config, USER_B) == []
    assert reply == i18n.t("quiet_cleared", "en")


@pytest.mark.parametrize("bad_value", ["25:99-07:00", "22:00", "22:00-07:00-bad", "22:00-", "-07:00", "22:00-07:00,", ",22:00-07:00"])
async def test_execute_quiet_malformed_window_writes_nothing_and_replies_invalid(db, bad_value):
    command = commands.dispatch(f"/quiet {bad_value}", DEFAULT_REGISTRY)
    reply = await execute_quiet(command, db=db, lang="en", user_id=USER_B)

    assert db.get_user(USER_B)["quiet_hours_json"] is None  # never written
    assert reply == i18n.t("quiet_invalid_window", "en")


async def test_execute_quiet_bare_command_writes_nothing_and_replies_usage(db):
    command = commands.dispatch("/quiet", DEFAULT_REGISTRY)
    reply = await execute_quiet(command, db=db, lang="th", user_id=USER_B)

    assert db.get_user(USER_B)["quiet_hours_json"] is None
    assert reply == i18n.t("quiet_usage", "th")


async def test_execute_quiet_db_failure_reports_save_failed_not_a_traceback(db, monkeypatch):
    def _boom(self, chat_id, windows_json):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(Database, "set_user_quiet_hours", _boom)
    command = commands.dispatch("/quiet 22:00-07:00", DEFAULT_REGISTRY)
    reply = await execute_quiet(command, db=db, lang="en", user_id=USER_B)

    assert reply == i18n.t("preferences_save_failed", "en")


# ===========================================================================
# AC-P2 -- composed with the shared surface's READ side: B's quiet-hours
# window suppresses B's reminders in that window while A's (different/
# none) are not; /quiet off clears B's windows.
# ===========================================================================


async def test_ac_p2_quiet_hours_scoped_to_the_setting_user_only(db, config):
    command = commands.dispatch("/quiet 22:00-07:00", DEFAULT_REGISTRY)
    await execute_quiet(command, db=db, lang="en", user_id=USER_B)

    assert effective_quiet_windows(db, config, USER_B) == [("22:00", "07:00")]
    # OWNER never ran /quiet -- still inherits the (empty, by default) global config.
    assert effective_quiet_windows(db, config, OWNER) == config.quiet_hours.windows
    assert effective_quiet_windows(db, config, OWNER) == []


async def test_ac_p2_quiet_off_clears_only_that_users_windows(db, config):
    set_command = commands.dispatch("/quiet 22:00-07:00", DEFAULT_REGISTRY)
    await execute_quiet(set_command, db=db, lang="en", user_id=USER_B)
    assert effective_quiet_windows(db, config, USER_B) == [("22:00", "07:00")]

    off_command = commands.dispatch("/quiet off", DEFAULT_REGISTRY)
    await execute_quiet(off_command, db=db, lang="en", user_id=USER_B)

    assert effective_quiet_windows(db, config, USER_B) == []
    assert effective_quiet_windows(db, config, OWNER) == config.quiet_hours.windows


# ===========================================================================
# Vera additions -- adversarial hardening beyond Luna's own 45 tests
# (dispatched scope: AC-P1, AC-P2; see TEST-v1.2-preferences.md).
# ===========================================================================

# ---------------------------------------------------------------------------
# Isolation -- member vs. member (not just "the owner is unaffected", which
# Luna's own AC-P1/AC-P2 tests already cover). A third active user must be
# untouched by B's preference change too.
# ---------------------------------------------------------------------------

USER_C = "member-chat-c"


async def test_isolation_lang_set_by_one_member_does_not_affect_another_member(db):
    db.upsert_user(USER_C, role="member", status="active")

    command = commands.dispatch("/lang th", DEFAULT_REGISTRY)
    await execute_lang(command, db=db, lang="en", user_id=USER_B)

    assert db.get_user(USER_B)["language_pref"] == "th"
    assert db.get_user(USER_C)["language_pref"] == "auto"
    assert db.get_user(OWNER)["language_pref"] == "auto"


async def test_isolation_quiet_set_by_one_member_does_not_affect_another_member(db, config):
    db.upsert_user(USER_C, role="member", status="active")

    command = commands.dispatch("/quiet 22:00-07:00", DEFAULT_REGISTRY)
    await execute_quiet(command, db=db, lang="en", user_id=USER_B)

    assert effective_quiet_windows(db, config, USER_B) == [("22:00", "07:00")]
    assert effective_quiet_windows(db, config, USER_C) == config.quiet_hours.windows
    assert db.get_user(USER_C)["quiet_hours_json"] is None


# ---------------------------------------------------------------------------
# Validation -- additional garbage/boundary values beyond Luna's own corpus.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "/lang eng",
        "/lang th-TH",
        "/lang 123",
        "/lang ENGLISH",
        "/lang th,en",
        "ภาษา ไทย",  # the native Thai *word* for "Thai", not the code "th"
        "ภาษา english",
    ],
)
async def test_execute_lang_rejects_unsupported_codes_writes_nothing(db, text):
    command = commands.dispatch(text, DEFAULT_REGISTRY)
    assert command is not None and command.kind == "lang"
    reply = await execute_lang(command, db=db, lang="en", user_id=USER_B)

    assert db.get_user(USER_B)["language_pref"] == "auto"
    assert reply == i18n.t("lang_usage", "en")


@pytest.mark.parametrize(
    "bad_value",
    [
        "24:00-07:00",  # hour out of range (00-23 only)
        "23:60-07:00",  # minute out of range (00-59 only)
        "9:00-17:00",  # missing leading zero on the hour
        "09:00-17:0",  # missing leading zero on the minute
        "09:00–17:00",  # en-dash instead of a hyphen (mobile-keyboard autocorrect)
    ],
)
async def test_execute_quiet_rejects_boundary_and_encoding_edge_cases(db, bad_value):
    command = commands.dispatch(f"/quiet {bad_value}", DEFAULT_REGISTRY)
    reply = await execute_quiet(command, db=db, lang="en", user_id=USER_B)

    assert db.get_user(USER_B)["quiet_hours_json"] is None
    assert reply == i18n.t("quiet_invalid_window", "en")


async def test_execute_quiet_start_equals_end_is_accepted_as_a_valid_zero_width_window(db, config):
    """SPEC-v1.2.md R-P2 adds no extra validation beyond HH:MM shape -- same
    posture as `QuietHoursConfig`'s own validator (config.py:113-119), which
    also allows start == end for the global config. `/quiet 08:00-08:00` is
    therefore accepted and stored; per the PRE-EXISTING engine's own
    `[start, end)` semantics (`core/reminders.py:_in_quiet_hours`) it never
    actually suppresses anything (a zero-width window) -- consistent with
    the engine's existing behavior for the global config, not a regression
    introduced by this module. Documented here, not filed as a defect."""
    command = commands.dispatch("/quiet 08:00-08:00", DEFAULT_REGISTRY)
    reply = await execute_quiet(command, db=db, lang="en", user_id=USER_B)

    assert db.get_user(USER_B)["quiet_hours_json"] == json.dumps([["08:00", "08:00"]])
    assert reply == i18n.t("quiet_set", "en", windows="08:00-08:00")
    windows = effective_quiet_windows(db, config, USER_B)
    assert _in_quiet_hours(dt_time(8, 0), windows) is False  # exclusive end -> zero-width, never quiet


# ---------------------------------------------------------------------------
# AC-P2 -- a midnight-crossing window set via /quiet must compose correctly
# with the pre-existing quiet-hours engine (`_in_quiet_hours`'s start > end
# branch), not just round-trip as opaque JSON.
# ---------------------------------------------------------------------------


async def test_ac_p2_midnight_crossing_window_set_via_quiet_actually_suppresses(db, config):
    command = commands.dispatch("/quiet 22:00-07:00", DEFAULT_REGISTRY)
    await execute_quiet(command, db=db, lang="en", user_id=USER_B)

    windows = effective_quiet_windows(db, config, USER_B)
    assert _in_quiet_hours(dt_time(23, 30), windows) is True  # late night
    assert _in_quiet_hours(dt_time(3, 0), windows) is True  # early morning
    assert _in_quiet_hours(dt_time(12, 0), windows) is False  # broad daylight
    assert _in_quiet_hours(dt_time(7, 0), windows) is False  # end is exclusive


# ---------------------------------------------------------------------------
# Thai-alias misfire corpus -- realistic Thai sentences that begin with the
# literal trigger word "เงียบ"/"ภาษา" followed by a SPACE and more text.
#
# `_QUIET_TH_RE`/`_LANG_TH_RE`'s "mandatory \s+" mitigation (core/commands.py,
# the comment block directly above them) explicitly claims protection only
# against the trigger glued to more text with NO space ("the ordinary Thai
# spelling convention") -- it does NOT claim protection against a
# space-separated continuation. "เงียบ ๆ" ("quiet"/"hush", reduplicated) is
# standard Thai orthography: the ๆ (mai yamok) mark is conventionally
# preceded by a space, so "เงียบ ๆ หน่อยนะ" ("keep it down, please") is a
# realistic, common message a user might send -- not a contrived edge case.
#
# This corpus originally demonstrated the mitigation did NOT fully hold,
# judged against (a) SPEC-v1.2.md §2.3's own framing of the new commands
# ("same discipline as /undo/target") and (b) this router's own stated
# design philosophy (core/commands.py's module docstring: "a false positive
# here would silently swallow a real habit log -- the worst failure mode
# for this router"). Luna's follow-up fix (see `core/commands.py:709-765`:
# a single-token `\S+` value capture + `_looks_like_th_prose` blacklist for
# ภาษา, `_QUIET_TH_VALUE_RE` shape-whitelist for เงียบ) closes every case in
# THIS corpus -- re-verified below, now GREEN. See
# `test_lang_th_alias_blacklist_residual_misfire_corpus` further down for
# newly-probed cases that show the blacklist choice for ภาษา still has an
# open residual gap the shape-whitelist approach for เงียบ does not share.
# `schedules`' own Thai alias เตือน (module `schedules`) had the identical
# root-cause bug (confirmed misfire: "เตือน น้ำ ท่วมด้วย" actually setting a
# water reminder) and has since been independently fixed with a
# registry-anchored-habit + argument-shape-whitelist approach (no
# blacklist) -- audited separately, see TEST-v1.2-preferences.md's
# "Schedules (เตือน) audit" addendum.
# ---------------------------------------------------------------------------

THAI_ALIAS_MISFIRE_CORPUS = [
    "เงียบ ๆ หน่อยนะ",  # "keep it down please" -- standard mai-yamok spacing
    "เงียบ ๆ หน่อย",
    "เงียบ จังเลยวันนี้",  # "so quiet today" -- natural clause break after the word
    "ภาษา นี้ยากมาก",  # "this language is hard" -- plausible spaced phrasing
]


@pytest.mark.parametrize("message", THAI_ALIAS_MISFIRE_CORPUS)
def test_thai_alias_does_not_misfire_on_common_space_separated_phrasing(message):
    """FIXED (round 1, re-verified this pass) -- was failing as of the
    initial hand-off; Luna's single-token-capture fix (`\\S+` value group,
    core/commands.py:735-738) makes every case here fall through to `None`
    correctly regardless of which round-2 plausibility strategy each alias
    uses. Left in the suite, unmodified, as the regression guard for this
    exact corpus."""
    assert commands.dispatch(message, DEFAULT_REGISTRY) is None


# ---------------------------------------------------------------------------
# Residual risk audit, round 2 (post round-1 fix) -- ภาษา's ROUND-1 fix used
# a curated BLACKLIST of Thai discourse markers (`_TH_PROSE_MARKERS`/
# `_looks_like_th_prose`, since REMOVED), specifically so a near-miss
# command attempt like "ภาษา ไทย" (the native Thai WORD for "Thai", not the
# code "th") still reached `execute_lang`'s helpful `lang_usage` reply
# instead of silently falling through (see
# `test_execute_lang_rejects_unsupported_codes_writes_nothing` above). A
# blacklist's structural weakness is that it can only reject words someone
# thought to curate -- this round-2 corpus found 6 realistic, unremarkable
# single-word Thai messages that contained none of the curated markers and
# still misfired (all FAILED when first added).
#
# ROUND-3 UPDATE (this pass): Luna removed the blacklist entirely and
# replaced it with `_LANG_TH_VALID_VALUES = {"en", "th", "auto", "ไทย",
# "english"}` (core/commands.py:744) -- an explicit WHITELIST of exactly
# the tokens that should dispatch (the spec's own `en`/`th`/`auto` plus the
# two reviewed near-miss names `test_execute_lang_rejects_unsupported_
# codes_writes_nothing` requires). Re-verified: every case below now
# correctly falls through to `None` -- **all 6 now PASS, unmodified**.
# `core/commands.py` was independently checked and no longer references
# `_looks_like_th_prose`/`_TH_PROSE_MARKERS` anywhere.
#
# `เงียบ`'s fix, by contrast, has always used a SHAPE whitelist
# (`_QUIET_TH_VALUE_RE` -- "off" or an HH:MM-HH:MM-shaped token) rather
# than a word blacklist, because a quiet-hours value has an unambiguous
# mechanical shape a language name doesn't -- it was unaffected by either
# round. Probed the equivalent residual-risk angle for เงียบ below too
# (`test_quiet_th_alias_shape_whitelist_has_no_found_misfire`) and found no
# realistic misfire in either round -- kept as the positive control
# demonstrating a shape whitelist needs no such follow-up fix.
# ---------------------------------------------------------------------------

LANG_TH_ALIAS_ROUND2_RESIDUAL_CORPUS = [
    "ภาษา อังกฤษ",  # "English" -- e.g. noting a topic ("[studied] English")
    "ภาษา จีน",  # "Chinese" -- same shape, different language name
    "ภาษา ใหม่",  # "new language" -- e.g. "[started learning a] new language"
    "ภาษา ดี",  # "[my/the] language [is] good" -- a plausible mini reflection
    "ภาษา สวย",  # "[a] beautiful language"
    "ภาษา อะไร",  # "which/what language?" -- a genuine short question
]


@pytest.mark.parametrize("message", LANG_TH_ALIAS_ROUND2_RESIDUAL_CORPUS)
def test_lang_th_alias_round2_residual_corpus_now_falls_through(message):
    """FIXED (round 3, re-verified this pass) -- was failing under round
    1's blacklist; round 3's whitelist (`_LANG_TH_VALID_VALUES`,
    core/commands.py:744) rejects every one of these (none is `en`/`th`/
    `auto`/`ไทย`/`english`), so all correctly fall through to `None`."""
    assert commands.dispatch(message, DEFAULT_REGISTRY) is None


# ---------------------------------------------------------------------------
# Round-3 completeness probe (this pass) -- a few more single-token
# continuations beyond round 2's corpus: other real language names not on
# the whitelist (must still fall through), case-insensitivity of the
# whitelist tokens themselves (must still dispatch), and a near-whitelist
# token that only shares a PREFIX with a whitelisted value (must NOT
# dispatch -- confirms the whitelist check is an exact match, not a
# substring/prefix check).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    ["ภาษา ญี่ปุ่น", "ภาษา ฝรั่งเศส"],  # "Japanese", "French" -- other real language names, not whitelisted
)
def test_lang_th_alias_other_language_names_still_fall_through(message):
    assert commands.dispatch(message, DEFAULT_REGISTRY) is None


@pytest.mark.parametrize(
    "message,expected_value",
    [("ภาษา TH", "th"), ("ภาษา Auto", "auto"), ("ภาษา ENGLISH", "english")],
)
def test_lang_th_alias_whitelist_is_case_insensitive(message, expected_value):
    command = commands.dispatch(message, DEFAULT_REGISTRY)
    assert command == Command(kind="lang", pref_value=expected_value)


def test_lang_th_alias_whitelist_is_exact_match_not_prefix():
    """"ไทยมาก" ("very Thai"/"Thai-ish") shares the "ไทย" prefix with a
    whitelisted token but is a different, longer word -- must NOT dispatch
    (confirms `value not in _LANG_TH_VALID_VALUES` is a set membership
    check on the whole token, not `str.startswith`)."""
    assert commands.dispatch("ภาษา ไทยมาก", DEFAULT_REGISTRY) is None


QUIET_TH_ALIAS_SHAPE_CANDIDATES_CHECKED = [
    "เงียบ ๆ",
    "เงียบ เลย",
    "เงียบ นะ",
    "เงียบ จัง",
    "เงียบ มาก",
    "เงียบ สงบ",  # "quiet [and] peaceful" -- a plausible descriptive word, not on any blacklist
    "เงียบ แล้ว",  # "[it's] quiet now"
]


@pytest.mark.parametrize("message", QUIET_TH_ALIAS_SHAPE_CANDIDATES_CHECKED)
def test_quiet_th_alias_shape_whitelist_has_no_found_misfire(message):
    """Positive control for the audit above: unlike ภาษา's blacklist,
    เงียบ's shape-whitelist (`_QUIET_TH_VALUE_RE` -- "off" or an
    HH:MM-HH:MM-shaped token) rejects every ordinary Thai continuation
    tried here, including descriptive words never explicitly enumerated
    anywhere (e.g. "สงบ"/"แล้ว") -- because the check is about the value's
    MECHANICAL SHAPE, not membership in a curated word list, so no
    Thai-vocabulary word can ever accidentally satisfy it. PASSES."""
    assert commands.dispatch(message, DEFAULT_REGISTRY) is None


# ---------------------------------------------------------------------------
# Persistence -- a preference survives a fresh `Database` open (write, close,
# reopen), not just within the same connection/process.
# ---------------------------------------------------------------------------


async def test_lang_preference_persists_across_a_fresh_database_open(tmp_path):
    db_path = tmp_path / "persist_lang.db"
    db1 = Database(db_path)
    db1.upsert_user(USER_B, role="member", status="active")
    command = commands.dispatch("/lang th", DEFAULT_REGISTRY)
    await execute_lang(command, db=db1, lang="en", user_id=USER_B)
    db1.close()

    db2 = Database(db_path)
    try:
        assert db2.get_user(USER_B)["language_pref"] == "th"
    finally:
        db2.close()


async def test_quiet_hours_persist_across_a_fresh_database_open(tmp_path, config):
    db_path = tmp_path / "persist_quiet.db"
    db1 = Database(db_path)
    db1.upsert_user(USER_B, role="member", status="active")
    command = commands.dispatch("/quiet 22:00-07:00,12:00-13:00", DEFAULT_REGISTRY)
    await execute_quiet(command, db=db1, lang="en", user_id=USER_B)
    db1.close()

    db2 = Database(db_path)
    try:
        assert effective_quiet_windows(db2, config, USER_B) == [("22:00", "07:00"), ("12:00", "13:00")]
    finally:
        db2.close()


# ---------------------------------------------------------------------------
# DB-failure fallback -- a non-sqlite3 exception type must be caught just as
# broadly as Luna's own `sqlite3.OperationalError` cases (confirms the
# `except Exception` in both functions is genuinely broad, not accidentally
# narrowed to sqlite3 errors specifically).
# ---------------------------------------------------------------------------


async def test_execute_lang_non_sqlite_exception_is_still_caught(db, monkeypatch):
    def _boom(self, chat_id, pref):
        raise RuntimeError("unexpected failure mode")

    monkeypatch.setattr(Database, "set_user_language", _boom)
    command = commands.dispatch("/lang en", DEFAULT_REGISTRY)
    reply = await execute_lang(command, db=db, lang="en", user_id=USER_B)

    assert reply == i18n.t("preferences_save_failed", "en")


async def test_execute_quiet_non_sqlite_exception_is_still_caught(db, monkeypatch):
    def _boom(self, chat_id, windows_json):
        raise RuntimeError("unexpected failure mode")

    monkeypatch.setattr(Database, "set_user_quiet_hours", _boom)
    command = commands.dispatch("/quiet off", DEFAULT_REGISTRY)
    reply = await execute_quiet(command, db=db, lang="en", user_id=USER_B)

    assert reply == i18n.t("preferences_save_failed", "en")


# ---------------------------------------------------------------------------
# Cross-track dispatch precedence -- /lang and /quiet are checked after
# access/remind/target/undo/edit/snooze in `commands.dispatch`'s fixed order
# (core/commands.py:702-786). Confirms the new shape-matching added for this
# module does not shadow any pre-existing v1.1/v1.2 command kind, and that
# near-miss slash commands don't accidentally match.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_kind",
    [
        ("/approve 88899900", "approve"),
        ("/block 88899900", "block"),
        ("/users", "users"),
        ("/invite 88899900", "invite"),
        ("/start", "start"),
        ("/remind water 08:00 12:00", "remind"),
        ("/target water 2000", "target"),
        ("/undo", "undo"),
    ],
)
def test_lang_quiet_additions_do_not_shadow_other_v11_v12_command_kinds(text, expected_kind):
    command = commands.dispatch(text, DEFAULT_REGISTRY)
    assert command is not None
    assert command.kind == expected_kind


def test_lang_slash_does_not_accidentally_match_a_longer_similar_word():
    assert commands.dispatch("/language", DEFAULT_REGISTRY) is None
    assert commands.dispatch("/languages en", DEFAULT_REGISTRY) is None


def test_quiet_slash_does_not_accidentally_match_a_longer_similar_word():
    assert commands.dispatch("/quietly", DEFAULT_REGISTRY) is None
    assert commands.dispatch("/quietude off", DEFAULT_REGISTRY) is None


def test_ordinary_habit_logs_still_fall_through_to_the_parser():
    assert commands.dispatch("500ml", DEFAULT_REGISTRY) is None
    assert commands.dispatch("10 min stretch", DEFAULT_REGISTRY) is None


# ---------------------------------------------------------------------------
# A user with no `users` row yet (never onboarded through `access`) calling
# `/lang`/`/quiet` must not silently self-grant elevated role/status --
# `set_user_language`/`set_user_quiet_hours`'s upsert-on-conflict creates the
# row with the schema's own safe defaults, same as `upsert_user`'s own
# posture. In production the `access` gate (R-A1) would refuse this user
# before dispatch is ever reached; this test documents that even bypassing
# that gate, this module's own writes can't be used to self-escalate.
# ---------------------------------------------------------------------------


async def test_execute_lang_for_a_brand_new_user_creates_row_with_safe_defaults(tmp_path):
    db_path = tmp_path / "new_user.db"
    database = Database(db_path)
    try:
        assert database.get_user("brand-new-chat") is None

        command = commands.dispatch("/lang th", DEFAULT_REGISTRY)
        await execute_lang(command, db=database, lang="en", user_id="brand-new-chat")

        row = database.get_user("brand-new-chat")
        assert row is not None
        assert row["language_pref"] == "th"
        assert row["role"] == "member"  # never silently escalated to owner
        assert row["status"] == "pending"  # never silently activated
    finally:
        database.close()
