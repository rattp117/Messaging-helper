"""SPEC-v1.8.md `quicklog` module -- Vera's adversarial gap suite, on top of
Luna's own `tests/test_quicklog.py` (61) + `tests/test_reactions.py` (11).

Scope: module-level verification only (`core/quicklog.py`, `core/
reactions.py`, `commands._match_log`). `main.py` routing/dispatch wiring is
the later integration pass (out of scope here, per SPEC-v1.8.md §11) --
findings that require that wiring are called out in prose in TEST-v1.8-
quicklog.md, not asserted as module-test failures.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import commands, i18n, quicklog, reactions
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.main import handle_inbound_message
from habit_assistant.storage.db import Database

OWNER = "owner"
OTHER = "other-user"


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.actionable: list[tuple[str, list[tuple[str, str]]]] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.edits: list[tuple[str, str, str]] = []
        self.pins: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append(text)

    async def send_actionable(self, chat_id: str, text: str, buttons: list[tuple[str, str]]) -> None:
        self.actionable.append((text, buttons))
        self.sent.append(text)

    async def set_message_reaction(self, chat_id: str, message_id: str, emoji: str) -> None:
        self.reactions.append((chat_id, message_id, emoji))

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> bool:
        self.edits.append((chat_id, message_id, text))
        return True

    async def send_and_pin(self, chat_id: str, text: str) -> str | None:
        self.pins.append((chat_id, text))
        return "pinned-msg-id"

    async def run(self, on_message: Callable[[str, str], Awaitable[None]], on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


class FakeLLM:
    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return "noted"


@pytest.fixture
def fixed_clock():
    return lambda: datetime(2026, 8, 19, 9, 0, 0)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(OTHER, role="member", status="active")
    yield database
    database.close()


# ---------------------------------------------------------------------------
# AC-A1 -- keyboard generation, odd/boundary goals.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "goal,expected",
    [
        (7, [2.0, 4.0, 7.0]),      # odd goal, banker's rounding on the halfway rungs
        (1, [1.0]),                 # tiny goal -- both rungs floor to 1, dedup with G itself
        (2500, [625.0, 1250.0, 2500.0]),  # spec's own water-scale example
    ],
)
def test_goal_ladder_sane_roundings_for_odd_and_boundary_goals(goal, expected):
    assert quicklog._goal_ladder(goal) == expected


def test_goal_ladder_huge_goal_stays_ascending_and_positive():
    ladder = quicklog._goal_ladder(100_000_000)
    assert ladder == sorted(ladder)
    assert all(v > 0 for v in ladder)


def test_goal_ladder_negative_goal_produces_no_buttons_not_a_crash():
    """REGRESSION GUARD (TEST-v1.8-quicklog.md finding #3, fixed): a
    config-authored negative goal (a data bug, not user input -- nothing
    in this app validates `goal >= 0` at config-load time) must not
    produce a positive-amount button out of thin air. Originally,
    `_round_ladder_step` floored ANY non-positive `round(x)` result to
    `1.0` regardless of the sign of `x` itself, so `goal=-5` produced a
    spurious `log:negative_goal:1` button. Fixed: `_round_ladder_step`
    now only floors to `1.0` when `x` itself is positive (small-positive-
    goal case); a non-positive `x` passes through unchanged, so
    `_goal_ladder`'s own `value > 0` guard filters every rung out and the
    habit is skipped, same as a goal-less habit (R-Q1's "if neither, the
    habit is skipped")."""
    assert quicklog._goal_ladder(-5) == [], (
        f"got {quicklog._goal_ladder(-5)!r} -- _round_ladder_step floors a negative rung to 1.0 "
        "instead of treating a negative goal as unusable"
    )

    config = Config(
        habits=[
            *Config().habits,
            {"id": "negative_goal", "type": "numeric", "goal": -5, "label": {"en": "x", "th": "x"}, "unit": {"en": "x", "th": "x"}},
        ]
    )
    registry = HabitRegistry.from_config(config)
    db = Database(":memory:")
    try:
        keyboard = quicklog.build_keyboard(registry, config, db, "en", OWNER)
        assert all("negative_goal" not in payload for _label, payload in keyboard)
    finally:
        db.close()


def test_build_keyboard_huge_fractional_goal_produces_a_callback_payload_that_still_round_trips():
    """REGRESSION GUARD (TEST-v1.8-quicklog.md finding #4, fixed):
    `_format_amount` originally used Python's `%g` formatting for a
    non-integer amount. For a goal that is BOTH huge AND non-integer (a
    legitimate value per R-Q1's own "goal itself is kept exact ... a
    fractional configured/target goal is a legitimate tap amount"),
    `%g`'s default 6-significant-digit precision switched to scientific
    notation ("1.23457e+08"), which `_LOG_CALLBACK_RE` cannot match --
    the keyboard rendered a dead button whose own callback_data could
    never be tapped successfully. Fixed: `_format_amount` now renders
    fixed-point (`.6f`, trailing zeros/dot trimmed), matching
    `_LOG_CALLBACK_RE`'s own value grammar (up to 15 integer digits, up
    to 6 decimal digits) on the write side too."""
    config = Config(
        habits=[
            *Config().habits,
            {"id": "weird", "type": "numeric", "goal": 123456789.123, "label": {"en": "weird", "th": "weird"}, "unit": {"en": "x", "th": "x"}},
        ]
    )
    registry = HabitRegistry.from_config(config)
    db = Database(":memory:")
    try:
        keyboard = quicklog.build_keyboard(registry, config, db, "en", OWNER)
        weird_buttons = [(label, payload) for label, payload in keyboard if "weird" in payload]
        assert weird_buttons, "expected at least one weird-habit button"
        broken = [(label, payload) for label, payload in weird_buttons if quicklog._LOG_CALLBACK_RE.match(payload) is None]
        assert broken == [], (
            f"dead button(s) whose callback_data can never be tapped successfully: {broken!r} "
            "-- _format_amount emitted scientific notation for a huge fractional goal"
        )
    finally:
        db.close()


def test_alias_ladder_sorted_unique_deduplicates_equal_alias_values():
    """R-Q1: 'sorted-unique base-unit multipliers' -- two differently
    named aliases that happen to resolve to the SAME base-unit amount
    must contribute exactly one button, not two identical ones."""
    config = Config(
        habits=[
            *Config().habits,
            {
                "id": "tea",
                "type": "numeric",
                "label": {"en": "tea", "th": "tea"},
                "unit": {"en": "ml", "th": "ml"},
                "unit_aliases": {"cup": 200, "mug": 200, "large_cup": 350},
            },
        ]
    )
    registry = HabitRegistry.from_config(config)
    db = Database(":memory:")
    try:
        keyboard = quicklog.build_keyboard(registry, config, db, "en", OWNER)
        tea_payloads = [payload for _label, payload in keyboard if payload.startswith("log:tea:")]
        assert tea_payloads == ["log:tea:200", "log:tea:350"]  # deduped, sorted ascending
    finally:
        db.close()


def test_callback_data_stays_within_64_bytes_for_max_length_habit_id_and_large_value():
    """Telegram's own hard limit (SPEC-v1.8.md §2.1): callback_data must be
    <= 64 bytes. A 32-char custom habit id (the v1.7 R-V1 max) combined
    with a large ladder amount must still fit."""
    config = Config(
        habits=[
            *Config().habits,
            {
                "id": "a" * 32,
                "type": "numeric",
                "goal": 999_999_999,
                "label": {"en": "x", "th": "x"},
                "unit": {"en": "x", "th": "x"},
            },
        ]
    )
    registry = HabitRegistry.from_config(config)
    db = Database(":memory:")
    try:
        keyboard = quicklog.build_keyboard(registry, config, db, "en", OWNER)
        payloads = [payload for _label, payload in keyboard if payload.startswith(f"log:{'a' * 32}:")]
        assert payloads, "expected at least one button for the 32-char habit id"
        for payload in payloads:
            assert len(payload.encode("utf-8")) <= 64, f"{payload!r} exceeds Telegram's 64-byte callback_data limit"
    finally:
        db.close()


def test_build_keyboard_per_user_custom_alias_only_visible_to_its_owner(db):
    """Sharper per-user isolation check than the existing 'any pushups
    payload' assertion: user A's custom "pushups | alias=set:10" must
    yield EXACTLY the [dontexpect_10] button for A, and user B (no such
    habit) gets nothing named pushups at all -- not even a differently
    valued button."""
    import json

    db.add_user_habit(
        OWNER,
        {
            "id": "pushups",
            "type": "numeric",
            "label_en": "pushups",
            "label_th": "pushups",
            "unit_en": "",
            "unit_th": "",
            "goal": None,
            "unit_aliases": json.dumps({"set": 10}),
        },
    )
    config = Config()
    owner_registry = HabitRegistry.for_user(config, db, OWNER)
    other_registry = HabitRegistry.for_user(config, db, OTHER)

    owner_kb = quicklog.build_keyboard(owner_registry, config, db, "en", OWNER)
    other_kb = quicklog.build_keyboard(other_registry, config, db, "en", OTHER)

    assert ("💪 10", "log:pushups:10") in owner_kb
    assert other_kb == quicklog.build_keyboard(HabitRegistry.from_config(config), config, db, "en", OTHER)
    assert not any(payload.startswith("log:pushups:") for _label, payload in other_kb)


# ---------------------------------------------------------------------------
# AC-A3 -- callback safety, extended adversarial corpus.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_data",
    [
        "log:water:1e309",                       # scientific notation, would overflow a naive float() cast
        "log:น้ำ:500",                            # unicode (Thai) habit id -- not [a-z0-9_]
        "log:water\x00:500",                 # embedded NUL byte (Python escape) in the habit id
        "log:water';DROP TABLE logs;--:500",       # SQL-ish injection attempt
        "log:water:500;DROP TABLE logs;--",        # SQL-ish injection attempt, in the value position
        "log:water:99999999999999999999",          # far beyond int64 (2**63), also beyond the 15-digit regex bound
        "log:water:9223372036854775808",            # exactly 2**63
        "log:water:+500",                            # explicit leading plus -- not part of the accepted grammar
        "log:water:5_00",                            # underscore-separated numeric literal (valid Python, not valid here)
    ],
)
async def test_handle_log_callback_extended_malformed_corpus_no_write_no_send(db, fixed_clock, bad_data):
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()

    await quicklog.handle_log_callback(
        OWNER, bad_data, "text", "cb-gap-1", db=db, channel=channel, config=config, registry=registry, clock=fixed_clock
    )

    assert channel.sent == []
    assert db.sum_value(OWNER, "water", "2026-08-19") == 0


async def test_handle_log_callback_rejects_non_ascii_unicode_digits_in_the_value(db, fixed_clock):
    """REGRESSION GUARD (TEST-v1.8-quicklog.md finding #2, fixed):
    `_LOG_CALLBACK_RE`'s value group is written as `\\d{1,15}` -- but
    Python's `re` module treats bare `\\d` as "any Unicode decimal-digit
    character" (Unicode category Nd), NOT "ASCII 0-9 only", unless the
    pattern is compiled with `re.ASCII`. Arabic-Indic digits (U+0660-
    U+0669, e.g. "٥٠٠") were valid `\\d` matches, and Python's `float()`
    builtin happily converted them too -- so a forged callback_data using
    Arabic-Indic (or Thai, fullwidth, etc.) digits used to slip past the
    regex AND the numeric-bounds check, producing a REAL log write, not
    the "logged and ignored, no read/write" R-Q3 promises for a malformed
    payload. Fixed: `_LOG_CALLBACK_RE` is now compiled with `re.ASCII`."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()

    await quicklog.handle_log_callback(
        OWNER, "log:water:٥٠٠", "text", "cb-gap-1b", db=db, channel=channel, config=config, registry=registry, clock=fixed_clock
    )

    assert channel.sent == [], (
        f"expected the Arabic-Indic-digit payload to be rejected as malformed; instead got: {channel.sent!r}"
    )
    assert db.sum_value(OWNER, "water", "2026-08-19") == 0, "Arabic-Indic digits '٥٠٠' were parsed as 500 and written"


async def test_handle_log_callback_archived_habit_is_a_friendly_no_op_no_write(db, fixed_clock):
    """A habit that WAS a valid custom habit for this user but has since
    been archived must behave exactly like 'not in this user's registry
    at all' (`HabitRegistry.for_user` excludes archived habits by
    construction) -- friendly no-op, no write, no crash."""
    import json

    db.add_user_habit(
        OWNER,
        {
            "id": "pushups",
            "type": "numeric",
            "label_en": "pushups",
            "label_th": "pushups",
            "unit_en": "",
            "unit_th": "",
            "goal": None,
            "unit_aliases": json.dumps({"set": 10}),
        },
    )
    db.archive_user_habit(OWNER, "pushups")
    config = Config()
    registry = HabitRegistry.for_user(config, db, OWNER)  # archived -> not present
    channel = FakeChannel()

    await quicklog.handle_log_callback(
        OWNER, "log:pushups:10", "text", "cb-gap-2", db=db, channel=channel, config=config, registry=registry, clock=fixed_clock
    )

    assert channel.sent == [i18n.t("quicklog_unknown_habit", "en")]
    assert db.sum_value(OWNER, "pushups", "2026-08-19") == 0


async def test_handle_log_callback_valid_tap_writes_exactly_one_row_for_the_tapping_chat_only(db, fixed_clock):
    """A structural cross-user-contamination check: two users tap the
    SAME habit/value on their own keyboards -- each tap must write
    exactly one row, attributed to the correct `user_id`, and never
    leak into the other user's totals."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel_owner = FakeChannel()
    channel_other = FakeChannel()

    await quicklog.handle_log_callback(
        OWNER, "log:water:500", "text", "cb-gap-3a", db=db, channel=channel_owner, config=config, registry=registry, clock=fixed_clock
    )
    await quicklog.handle_log_callback(
        OTHER, "log:water:500", "text", "cb-gap-3b", db=db, channel=channel_other, config=config, registry=registry, clock=fixed_clock
    )

    assert db.sum_value(OWNER, "water", "2026-08-19") == 500
    assert db.sum_value(OTHER, "water", "2026-08-19") == 500  # each got their own row, not double-counted
    # exactly one row per user -- not e.g. two rows from a double-write bug
    rows_owner = db._conn.execute(
        "SELECT COUNT(*) FROM logs WHERE user_id = ? AND category = 'water'", (OWNER,)
    ).fetchone()[0]
    rows_other = db._conn.execute(
        "SELECT COUNT(*) FROM logs WHERE user_id = ? AND category = 'water'", (OTHER,)
    ).fetchone()[0]
    assert rows_owner == 1
    assert rows_other == 1


# ---------------------------------------------------------------------------
# AC-A2/AC-A6 -- confirmation-parity gap: stored `/lang` preference.
# ---------------------------------------------------------------------------


async def test_handle_log_callback_honors_the_tapping_users_stored_lang_preference():
    """REGRESSION GUARD (TEST-v1.8-quicklog.md finding #1, fixed, the
    load-bearing one): the TYPED path resolves reply language via
    `i18n.resolve_reply_language(text, config, user_pref=
    _stored_language_pref(db, user_id))` (main.py:740) -- a user's stored
    `/lang` preference (`"th"`/`"en"`) FORCES that language regardless of
    what the inbound text itself looks like (`i18n.resolve_reply_language`'s
    own precedence: global config force > stored user_pref > text
    detection). `quicklog.handle_log_callback` originally called
    `i18n.resolve_reply_language(source_text, config)` with NO `user_pref`
    argument at all, so it could never honor a stored preference and fell
    back to detecting language purely from `source_text` (the keyboard-
    prompt message the button is attached to) -- diverging from the typed
    path whenever `source_text` didn't happen to carry the same language
    marker the stored preference would force.

    Fixed: `core/quicklog.py` now carries its own `_stored_language_pref`
    (mirroring `main.py:_stored_language_pref`'s small per-file-copy
    convention, e.g. `core/access.py`/`core/reminders.py`) and threads it
    through as `user_pref`. This test proves BOTH directions: a Thai
    stored preference wins over an English-only prompt (matching the
    typed path), and -- the reverse, not previously exercised -- an
    English stored preference still wins over a Thai-only prompt (i.e.
    the fix didn't just flip the default; the precedence order itself,
    forced-pref > text-detection, holds in both directions)."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    clock = lambda: datetime(2026, 8, 19, 9, 0, 0)
    db = Database(":memory:")
    try:
        db.upsert_user(OWNER, role="owner", status="active")
        db.set_user_language(OWNER, "th")  # stored preference: Thai, forced

        typed_channel = FakeChannel()
        await handle_inbound_message(
            "500ml", db=db, llm=FakeLLM(), channel=typed_channel, config=config, registry=registry, clock=clock, user_id=OWNER
        )
        typed_lang = i18n.detect_language(typed_channel.sent[0])
        assert typed_lang == "th"  # sanity: the typed path DOES honor the stored preference

        tapped_channel = FakeChannel()
        # source_text is an ENGLISH-only prompt -- no Thai characters for
        # auto-detection to key off, exactly the scenario where only a
        # stored user_pref (not text detection) can recover the right
        # language, the same way the typed path above just did.
        await quicklog.handle_log_callback(
            OWNER, "log:water:500", "Tap to log:", "cb-gap-4", db=db, channel=tapped_channel, config=config, registry=registry, clock=clock
        )
        tapped_lang = i18n.detect_language(tapped_channel.sent[0])

        assert tapped_lang == typed_lang == "th", (
            f"typed path replied in {typed_lang!r} (honoring the stored /lang preference) but the tap path "
            f"replied in {tapped_lang!r} -- handle_log_callback never consults the tapping user's stored "
            "language preference, only source_text detection"
        )

        # Reverse direction: an English stored preference must still win
        # over a Thai-only prompt -- confirms the fix threads user_pref
        # through resolve_reply_language's own precedence correctly,
        # rather than e.g. always preferring a detected Thai signal.
        db.set_user_language(OWNER, "en")
        reverse_channel = FakeChannel()
        await quicklog.handle_log_callback(
            OWNER,
            "log:water:500",
            "\U0001F447 แตะเพื่อบันทึก:",
            "cb-gap-4b",
            db=db,
            channel=reverse_channel,
            config=config,
            registry=registry,
            clock=clock,
        )
        assert i18n.detect_language(reverse_channel.sent[0]) == "en"
    finally:
        db.close()


async def test_handle_log_callback_missing_user_row_falls_back_to_auto_no_crash():
    """Defensive edge case for the new `_stored_language_pref` copy: a
    `chat_id` with no `users` row at all (shouldn't happen in practice --
    the access gate upserts a row before a tap can reach here -- but the
    helper's own contract, mirrored from `main.py`, is fail-open) must
    not crash `handle_log_callback`; it falls back to `"auto"` (pure
    `source_text` detection), same as before this fix existed."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    clock = lambda: datetime(2026, 8, 19, 9, 0, 0)
    db = Database(":memory:")
    try:
        channel = FakeChannel()
        await quicklog.handle_log_callback(
            "ghost-user-not-in-db", "log:water:500", "Tap to log:", "cb-gap-4c", db=db, channel=channel, config=config, registry=registry, clock=clock
        )
        assert len(channel.sent) == 1
        assert i18n.detect_language(channel.sent[0]) == "en"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# AC-A4/AC-A5 -- reactions: structural "never called from the tap path", and
# fail-open on a channel whose set_message_reaction explodes with something
# other than a plain RuntimeError.
# ---------------------------------------------------------------------------


class WeirdRaisingChannel(Channel):
    """Raises something other than a bare Exception subclass instance to
    make sure `react`'s fail-open `except Exception` really is broad
    enough for whatever a real transport layer might throw (e.g. a
    library-specific HTTP error, a JSON decode error, a TimeoutError)."""

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        raise NotImplementedError

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError

    async def set_message_reaction(self, chat_id: str, message_id: str, emoji: str) -> None:
        raise TimeoutError("simulated slow transport")


async def test_react_fail_open_on_a_non_runtimeerror_exception_type():
    channel = WeirdRaisingChannel()
    habit = Habit(
        id="water", type="numeric", label_en="water", label_th="water", unit_en="ml", unit_th="ml.",
        goal=None, reminder_times=(), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )
    await reactions.react(channel, "owner", "msg-x", habit)  # must not raise


def test_quicklog_module_never_imports_or_calls_reactions_react():
    """Structural check (AC-A5's module-owned half, reinforcing Luna's
    behavioral test): `core/quicklog.py` contains no reference to
    `reactions.react`/`react(` at all -- a tap must be structurally
    incapable of firing a reaction, not just "doesn't happen to" in the
    one scenario Luna's own test exercises."""
    import inspect

    source = inspect.getsource(quicklog)
    assert "reactions.react(" not in source
    assert ".react(" not in source


# ---------------------------------------------------------------------------
# _match_log -- extended adversarial Thai/English corpus.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "บันทึกไดอารี่วันนี้อากาศดีมาก",  # diary prose, no space after the trigger word
        "ขอบันทึกอะไรสักอย่างหน่อยนะ",     # "บันทึก" mid-sentence
        "กรุณาบันทึกด้วย",                  # "please record [this]" -- mid-sentence
        "/logg",                             # one extra trailing char on the slash form
        "log 500",                           # bare English word + a value, no slash -- not a recognized shape
        "logbook",
        "/log/",
        "//log",
        "​บันทึก",  # zero-width space before the Thai trigger word -- must NOT still match as bare
    ],
)
def test_match_log_extended_negative_corpus_never_misfires(message):
    registry = HabitRegistry.from_config(Config())
    result = commands.dispatch(message, registry)
    assert result is None or result.kind != "log", f"{message!r} unexpectedly dispatched as 'log': {result!r}"


@pytest.mark.parametrize("message", ["/log", "log", "บันทึก"])
def test_match_log_positive_forms(message):
    registry = HabitRegistry.from_config(Config())
    result = commands.dispatch(message, registry)
    if message == "log":
        # SPEC-v1.8.md §2.1 gives no bare-English alias -- "log" (no slash)
        # must NOT dispatch as the /log command.
        assert result is None or result.kind != "log"
    else:
        assert result == commands.Command(kind="log")


# ---------------------------------------------------------------------------
# AC-A6 -- zero-LLM, structural (reactions.py's own half).
# ---------------------------------------------------------------------------


def test_reactions_module_imports_no_llm_client():
    import inspect

    source = inspect.getsource(reactions)
    assert "ollama_client" not in source.lower()
    assert "OllamaClient" not in source
    assert not hasattr(reactions, "OllamaClient")
