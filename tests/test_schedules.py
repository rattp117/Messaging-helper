"""SPEC-v1.2.md "Multi-user support", module `schedules`: the deterministic,
LLM-free `/remind` set/show/default/off path -- `core/commands.dispatch`'s
`"remind"` kind (R-S5) and `core/schedules.execute_remind` (R-S5), plus
AC-S4's "no restart, no scheduler rebuild" proof exercised against the
REAL minutely tick (`core/reminders.run_due_reminders`, shared surface).

Companion to `tests/test_reminders.py` (the tick/resolver in isolation,
including the shared surface's own `/remind`-equivalent simulation via
`db.set_reminder_times` directly) and `tests/test_targets.py` (this
file's own template -- same recognize-shape/execute split, same
real-on-disk-SQLite-no-DB-mocks convention, `_execute` helper shape).
This file covers `commands.dispatch(text, registry)` directly (no
`main.py` involved -- that integration wiring is explicitly deferred,
per IMPL-v1.2-schedules.md) and `execute_remind` called directly against
a real on-disk SQLite `Database`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import commands, i18n
from habit_assistant.core.commands import Command
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.reminders import ReminderState, effective_reminder_times, run_due_reminders, send_reminder
from habit_assistant.core.schedules import execute_remind
from habit_assistant.storage.db import Database

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())

OWNER = "owner"


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "schedules.db")
    database.upsert_user(OWNER, role="owner", status="active")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


def _fixed_clock(hhmm: str):
    hour, minute = (int(x) for x in hhmm.split(":"))
    return lambda: datetime(2026, 8, 19, hour, minute, 0)


async def _execute(
    text: str,
    db: Database,
    config: Config,
    registry: HabitRegistry = DEFAULT_REGISTRY,
    lang: str = "en",
    user_id: str = OWNER,
) -> str:
    command = commands.dispatch(text, registry)
    assert command is not None and command.kind == "remind"
    return await execute_remind(command, db=db, config=config, registry=registry, lang=lang, user_id=user_id)


# ===========================================================================
# commands.dispatch shape -- slash form, Thai alias, show/default/off
# ===========================================================================


def test_dispatch_slash_set_shape():
    command = commands.dispatch("/remind water 08:00 12:00", DEFAULT_REGISTRY)
    assert command == Command(kind="remind", category="water", times=["08:00", "12:00"])


def test_dispatch_slash_is_case_insensitive():
    command = commands.dispatch("/REMIND water 08:00", DEFAULT_REGISTRY)
    assert command == Command(kind="remind", category="water", times=["08:00"])


def test_dispatch_thai_alias_with_space_set_shape():
    command = commands.dispatch("เตือน น้ำ 08:00 12:00", DEFAULT_REGISTRY)
    assert command == Command(kind="remind", category="water", times=["08:00", "12:00"])


def test_dispatch_thai_alias_without_space_does_not_match():
    """The Thai spelling convention writes words with no space between them
    ("เตือนตัวเอง" = "remind myself", one run of characters) -- a mandatory
    `\\s+` after the trigger word means this can never misfire as the
    remind command, so an ordinary Thai sentence beginning with "เตือน"
    stays untouched."""
    command = commands.dispatch("เตือนน้ำ 08:00", DEFAULT_REGISTRY)
    assert command is None or command.kind != "remind"


def test_dispatch_show_shape():
    command = commands.dispatch("/remind water", DEFAULT_REGISTRY)
    assert command == Command(kind="remind", category="water", times=[])


@pytest.mark.parametrize("word", ["default", "reset", "clear", "ค่าเริ่มต้น"])
def test_dispatch_default_synonyms_shape(word):
    command = commands.dispatch(f"/remind water {word}", DEFAULT_REGISTRY)
    assert command == Command(kind="remind", category="water", times=["default"])


def test_dispatch_off_shape():
    command = commands.dispatch("/remind water off", DEFAULT_REGISTRY)
    assert command == Command(kind="remind", category="water", times=["off"])


def test_dispatch_off_shape_is_case_insensitive():
    command = commands.dispatch("/remind water OFF", DEFAULT_REGISTRY)
    assert command == Command(kind="remind", category="water", times=["off"])


def test_dispatch_bare_slash_no_habit_returns_none():
    """SPEC-v1.2.md §2.3 lists only per-habit `/remind` forms -- no bare
    "show all" shape like `/target`'s -- so a habit-less `/remind` simply
    doesn't match."""
    assert commands.dispatch("/remind", DEFAULT_REGISTRY) is None


def test_dispatch_bare_thai_no_habit_returns_none():
    assert commands.dispatch("เตือน", DEFAULT_REGISTRY) is None


def test_dispatch_unresolved_habit_still_produces_a_command_via_slash_form():
    """Mirrors `/target`'s AC16 pattern: the explicit slash form carries an
    unresolved habit token through as-is (raw, lowercased) so
    `execute_remind`'s own registry lookup is what reports
    `remind_invalid_habit` -- this layer never silently drops it."""
    command = commands.dispatch("/remind coffee 08:00", DEFAULT_REGISTRY)
    assert command == Command(kind="remind", category="coffee", times=["08:00"])


def test_thai_habit_label_resolves_via_slash_form_too():
    command = commands.dispatch("/remind น้ำ 08:00", DEFAULT_REGISTRY)
    assert command == Command(kind="remind", category="water", times=["08:00"])


def test_thai_habit_label_bare_form_is_still_show():
    """The registry-anchored habit token (audit fix, see below) does not
    regress the legitimate bare "show" shape -- "เตือน น้ำ" (real habit,
    no tail) still dispatches, same as it did before the fix."""
    command = commands.dispatch("เตือน น้ำ", DEFAULT_REGISTRY)
    assert command == Command(kind="remind", category="water", times=[])


def test_thai_habit_label_with_shape_like_but_semantically_invalid_time_still_dispatches():
    """The tail-shape gate (audit fix) accepts anything digits-and-colon
    shaped, even a semantically invalid time -- it defers the REAL HH:MM
    check to `execute_remind` (R-S5/AC-S5), same as the slash form."""
    command = commands.dispatch("เตือน น้ำ 25:99", DEFAULT_REGISTRY)
    assert command == Command(kind="remind", category="water", times=["25:99"])


# ===========================================================================
# Audit fix (post-landing, prompted by sibling module `preferences`'s own
# Vera-caught false-positive class on `ภาษา`/`เงียบ`): correctly-spelled
# Thai puts a space before particles like the mai-yamok "ๆ" and other
# trailing words, so a bare "mandatory space, then anything" gate on the
# Thai alias `เตือน` is not enough -- ordinary sentences like "เตือน ๆ
# หน่อยนะ" have a space right after "เตือน" too. Fixed by requiring (1) a
# REAL registry habit token right after the space and (2) any tail have
# the SHAPE of a valid remind argument -- see `_match_remind`'s own
# docstring-comment block in `core/commands.py` for the full analysis.
# These cases were empirically confirmed to misfire (dispatch as
# `kind="remind"` with a garbage/unresolved category) before the fix.
# ===========================================================================

THAI_ALIAS_FALSE_POSITIVE_CASES = [
    "เตือน ๆ หน่อยนะ",  # mai-yamok "ๆ" right after the trigger -- the canonical case
    "เตือน ฉันด้วยนะ",  # "remind me please" -- a real, plausible message
    "เตือน แล้วนะ",  # "[already] reminded/noted" -- colloquial acknowledgement
    "เตือน ลืมไปแล้วว่าต้องทำอะไร",  # "reminder -- I forgot what I had to do" -- diary-style reflection
    "เตือน น้ำ ท่วมด้วย",  # "เตือน" + a REAL habit word ("น้ำ"/water) + ordinary prose tail ("flooding too")
]


@pytest.mark.parametrize("message", THAI_ALIAS_FALSE_POSITIVE_CASES)
def test_thai_alias_does_not_misfire_on_common_space_separated_phrasing(message):
    command = commands.dispatch(message, DEFAULT_REGISTRY)
    assert command is None or command.kind != "remind"


# ===========================================================================
# Adversarial corpus -- ordinary logs/other commands must never dispatch as
# "remind" (AC5.5's zero-false-positive contract, applied to this kind).
# ===========================================================================

ADVERSARIAL_MESSAGES = [
    "ดื่มน้ำ 2 แก้ว",
    "500ml",
    "did 10 min stretch",
    "please remind me to call mom at 5pm",
    "I need a reminder for water",
    "remind water 8am",  # no leading "/" and not the Thai trigger word
    "เตือนตัวเองให้ออกกำลังกาย",  # "เตือน" glued to more text, no space
    "อย่าลืมเตือนแม่ด้วยนะ",  # "เตือน" mid-sentence, not anchored at the start
    "เลื่อน",
    "/target water 2000",
    "/help",
    "เตือน",
    *THAI_ALIAS_FALSE_POSITIVE_CASES,
]


@pytest.mark.parametrize("message", ADVERSARIAL_MESSAGES)
def test_adversarial_corpus_never_dispatches_as_remind(message):
    command = commands.dispatch(message, DEFAULT_REGISTRY)
    assert command is None or command.kind != "remind"


# ===========================================================================
# AC-S2 -- `/remind water 08:00 12:00`: A's water reminders fire at 08:00
# and 12:00 and NOT at the old config times; B's water reminders (no
# override) are unaffected; other habits are unaffected.
# ===========================================================================


async def test_ac_s2_set_writes_the_override_and_replies_remind_set(db, config):
    reply = await _execute("/remind water 08:00 12:00", db, config)

    assert db.get_reminder_times(OWNER, "water") == ["08:00", "12:00"]
    assert reply == i18n.t("remind_set", "en", label="water", times="08:00, 12:00")


async def test_ac_s2_custom_time_fires_and_old_config_time_does_not(db, config):
    water = DEFAULT_REGISTRY.get("water")
    await _execute("/remind water 12:00", db, config)

    channel_at_config_time = FakeChannel()
    await run_due_reminders(
        channel_at_config_time, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(water.reminder_times[0])
    )
    assert channel_at_config_time.sent == []

    channel_at_custom_time = FakeChannel()
    await run_due_reminders(channel_at_custom_time, config, DEFAULT_REGISTRY, db, clock=_fixed_clock("12:00"))
    assert any(chat_id == OWNER for chat_id, _ in channel_at_custom_time.sent)


async def test_ac_s2_other_user_without_an_override_still_fires_at_config_times(db, config):
    db.upsert_user("user-b", role="member", status="active")
    water = DEFAULT_REGISTRY.get("water")
    await _execute("/remind water 12:00", db, config)  # OWNER's override only

    channel = FakeChannel()
    await run_due_reminders(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(water.reminder_times[0]))

    chat_ids_sent = {chat_id for chat_id, _ in channel.sent}
    assert "user-b" in chat_ids_sent
    assert OWNER not in chat_ids_sent  # OWNER's water moved away from this old time


async def test_ac_s2_other_habit_is_unaffected_by_waters_override(db, config):
    stretch = DEFAULT_REGISTRY.get("stretch")
    await _execute("/remind water 12:00", db, config)

    channel = FakeChannel()
    await run_due_reminders(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(stretch.reminder_times[0]))

    stretch_text = i18n.t("reminder_stretch", i18n.resolve_unprompted_language(config))
    assert (OWNER, stretch_text) in channel.sent


# ===========================================================================
# AC-S3 -- show reports effective times + source (custom/default/off);
# default reverts to config; off suppresses only that user's reminders for
# that habit.
# ===========================================================================


async def test_ac_s3_show_with_no_override_reports_default_source_and_config_times(db, config):
    water = DEFAULT_REGISTRY.get("water")
    reply = await _execute("/remind water", db, config)

    expected_times = ", ".join(water.reminder_times)
    assert reply == i18n.t(
        "remind_show", "en", label="water", times=expected_times, source=i18n.t("remind_source_default", "en")
    )


async def test_ac_s3_show_after_set_reports_custom_source_and_times(db, config):
    await _execute("/remind water 08:00 12:00", db, config)
    reply = await _execute("/remind water", db, config)

    assert reply == i18n.t(
        "remind_show", "en", label="water", times="08:00, 12:00", source=i18n.t("remind_source_custom", "en")
    )


async def test_ac_s3_show_after_off_reports_off(db, config):
    await _execute("/remind water off", db, config)
    reply = await _execute("/remind water", db, config)

    assert reply == i18n.t("remind_show_off", "en", label="water")


async def test_ac_s3_default_clears_override_and_reverts_effective_times(db, config):
    water = DEFAULT_REGISTRY.get("water")
    await _execute("/remind water 08:00 12:00", db, config)
    assert db.get_reminder_times(OWNER, "water") == ["08:00", "12:00"]

    reply = await _execute("/remind water default", db, config)

    assert db.get_reminder_times(OWNER, "water") == []
    assert reply == i18n.t("remind_cleared", "en", label="water", times=", ".join(water.reminder_times))


@pytest.mark.parametrize("word", ["reset", "clear", "ค่าเริ่มต้น"])
async def test_ac_s3_default_synonyms_all_clear_the_override(db, config, word):
    await _execute("/remind water 08:00", db, config)
    await _execute(f"/remind water {word}", db, config)
    assert db.get_reminder_times(OWNER, "water") == []


async def test_ac_s3_off_suppresses_reminders_for_that_user_only(db, config):
    db.upsert_user("user-b", role="member", status="active")
    water = DEFAULT_REGISTRY.get("water")
    reply = await _execute("/remind water off", db, config)
    assert reply == i18n.t("remind_off", "en", label="water")

    channel = FakeChannel()
    await run_due_reminders(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(water.reminder_times[0]))

    water_text = i18n.t("reminder_water", i18n.resolve_unprompted_language(config))
    chat_ids_that_got_water = {chat_id for chat_id, text in channel.sent if text == water_text}
    assert OWNER not in chat_ids_that_got_water
    assert "user-b" in chat_ids_that_got_water


# ===========================================================================
# AC-S5 -- HH:MM validation (reusing config._HHMM_RE): an invalid token is
# rejected with no write at all; duplicates are de-duped; a sane cap
# (<=24) is enforced.
# ===========================================================================


@pytest.mark.parametrize("bad_token", ["25:99", "12:60", "8:00", "24:00", "noon", "08-00", "8:0"])
async def test_ac_s5_invalid_time_token_rejected_with_no_write(db, config, bad_token):
    reply = await _execute(f"/remind water {bad_token}", db, config)

    assert db.get_reminder_times(OWNER, "water") == []
    assert reply == i18n.t("remind_invalid_time", "en", token=bad_token)


async def test_ac_s5_one_bad_token_rejects_the_whole_set_no_partial_write(db, config):
    """R-S5: "reject any invalid token (no write)" -- a set with one good
    and one bad token writes NEITHER, it doesn't partially save the good
    one."""
    reply = await _execute("/remind water 08:00 25:99", db, config)

    assert db.get_reminder_times(OWNER, "water") == []
    assert reply == i18n.t("remind_invalid_time", "en", token="25:99")


async def test_ac_s5_duplicate_times_are_deduped(db, config):
    await _execute("/remind water 08:00 08:00 12:00", db, config)
    assert db.get_reminder_times(OWNER, "water") == ["08:00", "12:00"]


async def test_ac_s5_cap_boundary_24_times_is_accepted(db, config):
    times = [f"{h:02d}:00" for h in range(24)]
    reply = await _execute("/remind water " + " ".join(times), db, config)

    assert db.get_reminder_times(OWNER, "water") == sorted(times)
    assert reply == i18n.t("remind_set", "en", label="water", times=", ".join(sorted(times)))


async def test_ac_s5_cap_exceeded_25_times_is_rejected_with_no_write(db, config):
    times = [f"{h:02d}:00" for h in range(24)] + ["23:30"]  # 25 distinct times
    reply = await _execute("/remind water " + " ".join(times), db, config)

    assert db.get_reminder_times(OWNER, "water") == []
    assert reply == i18n.t("remind_too_many_times", "en", max=24)


# ===========================================================================
# Invalid habit -- no write, lists the tracked ids (mirrors AC16's
# `target_invalid_habit` pattern).
# ===========================================================================


async def test_invalid_habit_writes_nothing_and_lists_tracked_ids(db, config):
    reply = await _execute("/remind coffee 08:00", db, config)

    assert db.get_reminder_times(OWNER, "coffee") == []
    assert reply == i18n.t("remind_invalid_habit", "en", habit_id="coffee", habit_list="water, stretch, diary")


# ===========================================================================
# A non-reminderable-by-default habit (a text habit with no config
# `reminder_times`) is still a valid `/remind` target (R-S5's own carve-out).
# ===========================================================================


async def test_text_habit_with_no_config_reminder_times_can_still_gain_custom_ones(db, config):
    diary = DEFAULT_REGISTRY.get("diary")
    assert diary.type == "text"

    reply = await _execute("/remind diary 21:30", db, config)

    assert db.get_reminder_times(OWNER, "diary") == ["21:30"]
    assert reply == i18n.t("remind_set", "en", label="diary", times="21:30")


# ===========================================================================
# DB write failures -- logged, replied to with remind_save_failed, never a
# traceback (mirrors AC28's `target_save_failed` pattern).
# ===========================================================================


class _ExplodingSetReminderTimes(Database):
    def set_reminder_times(self, user_id: str, habit_id: str, times: list[str]) -> None:
        raise sqlite3.OperationalError("disk I/O error")


class _ExplodingClearReminderTimes(Database):
    def clear_reminder_times(self, user_id: str, habit_id: str) -> None:
        raise sqlite3.OperationalError("disk I/O error")


async def test_set_db_failure_replies_save_failed_not_a_traceback(tmp_path, config):
    exploding = _ExplodingSetReminderTimes(tmp_path / "explode_set.db")
    exploding.upsert_user(OWNER, role="owner", status="active")
    try:
        reply = await _execute("/remind water 08:00", exploding, config)
        assert reply == i18n.t("remind_save_failed", "en")
        assert exploding.get_reminder_times(OWNER, "water") == []
    finally:
        exploding.close()


async def test_off_db_failure_replies_save_failed_not_a_traceback(tmp_path, config):
    exploding = _ExplodingSetReminderTimes(tmp_path / "explode_off.db")
    exploding.upsert_user(OWNER, role="owner", status="active")
    try:
        reply = await _execute("/remind water off", exploding, config)
        assert reply == i18n.t("remind_save_failed", "en")
    finally:
        exploding.close()


async def test_default_db_failure_replies_save_failed_not_a_traceback(tmp_path, config):
    exploding = _ExplodingClearReminderTimes(tmp_path / "explode_clear.db")
    exploding.upsert_user(OWNER, role="owner", status="active")
    try:
        reply = await _execute("/remind water default", exploding, config)
        assert reply == i18n.t("remind_save_failed", "en")
    finally:
        exploding.close()


# ===========================================================================
# AC-S4 -- a `/remind` change takes effect on the very next tick, with NO
# restart and NO scheduler-job rebuild. Proven by never constructing any
# scheduler/job object at all in this test: `run_due_reminders` is called
# directly, twice, around the write -- there is nothing to "rebuild".
# ===========================================================================


async def test_ac_s4_remind_write_is_picked_up_by_the_next_tick_with_no_scheduler_rebuild(db, config):
    water = DEFAULT_REGISTRY.get("water")
    old_time = water.reminder_times[0]

    channel_before = FakeChannel()
    await run_due_reminders(channel_before, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(old_time))
    assert any(chat_id == OWNER for chat_id, _ in channel_before.sent)  # fires at the OLD config time, pre-write

    await _execute("/remind water 12:00", db, config)  # the real write path: dispatch + execute_remind

    channel_at_old_time_after_write = FakeChannel()
    await run_due_reminders(channel_at_old_time_after_write, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(old_time))
    assert channel_at_old_time_after_write.sent == []  # old time no longer fires -- picked up immediately

    channel_at_new_time = FakeChannel()
    await run_due_reminders(channel_at_new_time, config, DEFAULT_REGISTRY, db, clock=_fixed_clock("12:00"))
    assert any(chat_id == OWNER for chat_id, _ in channel_at_new_time.sent)  # new custom time fires immediately


# ===========================================================================
# Vera additions -- gap-filling beyond Luna's own tests, per the dispatch's
# adversarial angles: isolation matrix, extra validation literals, cap-vs-
# dedupe ordering, replace-not-accumulate semantics, a wider adversarial
# corpus, AC-S6 interplay (custom time x quiet hours x goal-met x snooze)
# through the REAL `run_due_reminders`/`send_reminder`, and a cross-module
# dispatch-precedence check against `access`/`preferences`/v1.1 kinds.
# ===========================================================================


# ---------------------------------------------------------------------------
# Isolation matrix -- SPEC-v1.2.md R-D4/AC-U-ISO applied to this module's own
# surface: a set/off/default for (user, habit) must never leak to another
# user or another habit for the SAME user.
# ---------------------------------------------------------------------------


async def test_isolation_matrix_set_and_off_never_cross_user_or_habit_boundaries(db, config):
    db.upsert_user("user-b", role="member", status="active")

    await _execute("/remind water 08:00", db, config, user_id=OWNER)
    await _execute("/remind stretch off", db, config, user_id=OWNER)
    await _execute("/remind water 09:00", db, config, user_id="user-b")
    # user-b's stretch is left untouched (no override at all).

    assert db.get_reminder_times(OWNER, "water") == ["08:00"]
    assert db.get_reminder_times(OWNER, "stretch") == ["off"]
    assert db.get_reminder_times("user-b", "water") == ["09:00"]
    assert db.get_reminder_times("user-b", "stretch") == []

    # diary was never touched by anyone -- effective times still fall back
    # to config for both users.
    diary = DEFAULT_REGISTRY.get("diary")
    assert effective_reminder_times(db, config, diary, OWNER) == list(diary.reminder_times)
    assert effective_reminder_times(db, config, diary, "user-b") == list(diary.reminder_times)

    # Now clear ONLY the owner's water override -- must not disturb the
    # owner's stretch=off, or either of user-b's rows.
    await _execute("/remind water default", db, config, user_id=OWNER)
    water = DEFAULT_REGISTRY.get("water")
    assert db.get_reminder_times(OWNER, "water") == []
    assert effective_reminder_times(db, config, water, OWNER) == list(water.reminder_times)
    assert db.get_reminder_times(OWNER, "stretch") == ["off"]
    assert db.get_reminder_times("user-b", "water") == ["09:00"]
    assert db.get_reminder_times("user-b", "stretch") == []

    # And the tick reflects exactly this matrix: at 08:00 nobody's water
    # fires anymore (owner cleared back to config default, whose first slot
    # is not 08:00; user-b is on 09:00); at 09:00 only user-b's water fires;
    # stretch never fires for the owner at either of its config times.
    channel_08 = FakeChannel()
    await run_due_reminders(channel_08, config, DEFAULT_REGISTRY, db, clock=_fixed_clock("08:00"))
    assert all(chat_id != "user-b" for chat_id, _ in channel_08.sent)

    channel_09 = FakeChannel()
    await run_due_reminders(channel_09, config, DEFAULT_REGISTRY, db, clock=_fixed_clock("09:00"))
    assert ("user-b", i18n.t("reminder_water", i18n.resolve_unprompted_language(config))) in channel_09.sent
    assert all(chat_id != OWNER for chat_id, _ in channel_09.sent)

    stretch = DEFAULT_REGISTRY.get("stretch")
    for stretch_time in stretch.reminder_times:
        channel = FakeChannel()
        await run_due_reminders(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(stretch_time))
        assert all(chat_id != OWNER for chat_id, _ in channel.sent)


# ---------------------------------------------------------------------------
# Extra AC-S5 validation literals called out explicitly in the dispatch
# (belt-and-suspenders alongside Luna's own "25:99"/"12:60"/"8:00"/"8:0").
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_token", ["25:00", "7:5", "07:60"])
async def test_ac_s5_additional_invalid_time_literals_rejected_with_no_write(db, config, bad_token):
    reply = await _execute(f"/remind water {bad_token}", db, config)

    assert db.get_reminder_times(OWNER, "water") == []
    assert reply == i18n.t("remind_invalid_time", "en", token=bad_token)


async def test_ac_s5_invalid_token_leading_still_rejects_whole_set(db, config):
    """Same whole-set-reject contract as Luna's invalid-token-trailing test,
    but with the bad token FIRST -- position must not matter."""
    reply = await _execute("/remind water 25:99 08:00", db, config)

    assert db.get_reminder_times(OWNER, "water") == []
    assert reply == i18n.t("remind_invalid_time", "en", token="25:99")


async def test_ac_s5_all_tokens_identical_dedupes_to_a_single_time(db, config):
    await _execute("/remind water 08:00 08:00 08:00", db, config)
    assert db.get_reminder_times(OWNER, "water") == ["08:00"]


async def test_ac_s5_cap_applies_after_dedupe_not_to_raw_token_count(db, config):
    """R-S5's stated order is validate -> de-dupe -> enforce the <=24 cap.
    30 raw tokens that collapse to 10 distinct times must be ACCEPTED --
    the cap must not fire on the pre-dedupe count."""
    distinct = [f"{h:02d}:00" for h in range(10)]
    raw_tokens = distinct * 3  # 30 raw tokens, 10 distinct
    reply = await _execute("/remind water " + " ".join(raw_tokens), db, config)

    assert db.get_reminder_times(OWNER, "water") == sorted(distinct)
    assert reply == i18n.t("remind_set", "en", label="water", times=", ".join(sorted(distinct)))


async def test_setting_new_times_replaces_previous_override_entirely(db, config):
    """`db.set_reminder_times` is delete-then-insert (R-S5) -- a second
    `/remind` set must REPLACE the prior override, not accumulate on top
    of it."""
    await _execute("/remind water 08:00 12:00", db, config)
    assert db.get_reminder_times(OWNER, "water") == ["08:00", "12:00"]

    await _execute("/remind water 09:00", db, config)
    assert db.get_reminder_times(OWNER, "water") == ["09:00"]  # NOT ["08:00", "09:00", "12:00"]


# ---------------------------------------------------------------------------
# Wider adversarial corpus -- prefix look-alikes and mid-sentence mentions
# that must never be swallowed as "remind", beyond Luna's own corpus.
# ---------------------------------------------------------------------------


MORE_ADVERSARIAL_MESSAGES = [
    "/reminder water 08:00",  # "remind" + "er" glued -- must NOT match (no \s+ right after "remind")
    "/remindful",
    "remind",  # bare English word, no slash, no Thai trigger -- must not match either
    "remind water 08:00",  # no leading "/" -- English has no bare-word alias, only the slash form
    "I'll remind you later",
    "Please set a reminder for water",
    "เตือนความจำ",  # "reminder"/"memory aid" as one glued word, no space after เตือน
    "กรุณาเตือน น้ำ 08:00",  # "เตือน" mid-sentence (please remind...), not anchored at the start
]


@pytest.mark.parametrize("message", MORE_ADVERSARIAL_MESSAGES)
def test_wider_adversarial_corpus_never_dispatches_as_remind(message):
    command = commands.dispatch(message, DEFAULT_REGISTRY)
    assert command is None or command.kind != "remind"


def test_thai_alias_tolerates_multiple_internal_whitespace():
    """The mandatory `\\s+`/`.split()` machinery matches any whitespace
    run, not just a single space -- confirms the false-positive mitigation
    isn't accidentally over-fitted to exactly one space character."""
    command = commands.dispatch("เตือน   น้ำ    08:00", DEFAULT_REGISTRY)
    assert command == Command(kind="remind", category="water", times=["08:00"])


# ---------------------------------------------------------------------------
# AC-S6 interplay -- a custom time (this module's write) still honors the
# ASKING user's own quiet-hours and goal-met suppression, and a subsequent
# snooze targets only that user -- exercised through the REAL
# `run_due_reminders`/`send_reminder` (shared surface), not a mock. AC-S6
# itself is owned by the shared-surface/integration pass, but since the
# write half is entirely this module's code, sanity-checking the interplay
# here catches any divergence between the two resolvers early.
# ---------------------------------------------------------------------------


class _FixedDatetime(datetime):
    """Same technique as `tests/test_v09_gaps.py`'s own `_FixedDatetime`:
    `send_reminder`'s quiet-hours check reads the REAL `datetime.now(tz)`
    directly (`core/reminders.py`), NOT the `clock` callable
    `run_due_reminders` is given (that `clock` only drives the "which
    HH:MM is due" tick check, `_now_hhmm`) -- so a quiet-hours test must
    freeze `habit_assistant.core.reminders.datetime` itself to be
    deterministic regardless of when the suite actually runs."""

    _fixed: datetime

    @classmethod
    def now(cls, tz=None):
        return cls._fixed.replace(tzinfo=tz) if tz is not None else cls._fixed


def _freeze_reminders_clock(monkeypatch, hour: int, minute: int) -> None:
    from datetime import date as _date

    today = _date.today()
    fixed = _FixedDatetime(today.year, today.month, today.day, hour, minute, 0)
    frozen = type("_Frozen", (_FixedDatetime,), {"_fixed": fixed})
    monkeypatch.setattr("habit_assistant.core.reminders.datetime", frozen)


async def test_ac_s6_custom_time_reminder_is_suppressed_during_the_users_own_quiet_hours(monkeypatch, db, config):
    await _execute("/remind water 12:00", db, config)  # OWNER's real /remind write
    db.set_user_quiet_hours(OWNER, '[["11:00", "13:00"]]')  # window covers the custom time
    _freeze_reminders_clock(monkeypatch, 12, 0)  # real wall-clock used by the quiet-hours check

    channel = FakeChannel()
    await run_due_reminders(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock("12:00"))
    assert channel.sent == []  # suppressed by the user's OWN quiet hours, not the global config default (empty)


async def test_ac_s6_custom_time_reminder_fires_outside_the_users_quiet_hours(monkeypatch, db, config):
    await _execute("/remind water 12:00", db, config)
    db.set_user_quiet_hours(OWNER, '[["11:00", "13:00"]]')
    _freeze_reminders_clock(monkeypatch, 12, 0)  # still 12:00 -- inside the window

    channel = FakeChannel()
    await run_due_reminders(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock("09:00"))
    assert channel.sent == []  # not due at 09:00 at all (custom time is 12:00)

    # Clearing the quiet window lets the SAME custom time fire again, same
    # frozen wall-clock moment (12:00) that was suppressed a moment ago.
    db.set_user_quiet_hours(OWNER, None)
    channel_after = FakeChannel()
    await run_due_reminders(channel_after, config, DEFAULT_REGISTRY, db, clock=_fixed_clock("12:00"))
    assert any(chat_id == OWNER for chat_id, _ in channel_after.sent)


async def test_ac_s6_custom_time_reminder_still_honors_goal_met_skip_per_user(db, config):
    from datetime import date

    from habit_assistant.storage.models import LogEntry

    db.upsert_user("user-b", role="member", status="active")
    await _execute("/remind water 12:00", db, config, user_id=OWNER)  # OWNER: custom time
    await _execute("/remind water 12:00", db, config, user_id="user-b")  # user-b: SAME custom time

    today_iso = date.today().isoformat()
    db.insert_log(LogEntry(None, OWNER, f"{today_iso}T07:00:00", "water", 3000.0, None, "3000ml", "reply"))
    # OWNER's water goal (2500ml default) is already met; user-b has no logs at all.

    channel = FakeChannel()
    await run_due_reminders(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock("12:00"))

    chat_ids_sent = {chat_id for chat_id, _ in channel.sent}
    assert OWNER not in chat_ids_sent  # goal-met skip applies to a CUSTOM time exactly like a config time
    assert "user-b" in chat_ids_sent  # user-b's own goal state is untouched by OWNER's


async def test_ac_s6_snooze_after_a_custom_time_reminder_targets_the_asking_user_only(db, config):
    """Mirrors `main.py:_execute_snooze`'s own mechanism (re-invoking
    `send_reminder` for `state.last_habit_id[asking_user]`) without needing
    a real scheduler -- confirms a snooze reschedule after a CUSTOM-time
    fire is addressed only to the user who asked, never another active
    user, and that it independently re-applies quiet-hours/goal-met at its
    own fire time (same `send_reminder` code path)."""
    db.upsert_user("user-b", role="member", status="active")
    await _execute("/remind water 12:00", db, config, user_id=OWNER)

    state = ReminderState()
    tick_channel = FakeChannel()
    await run_due_reminders(tick_channel, config, DEFAULT_REGISTRY, db, state=state, clock=_fixed_clock("12:00"))
    assert state.last_habit_id.get(OWNER) == "water"
    assert "user-b" not in state.last_habit_id  # user-b never had a reminder fire this tick

    # Simulate the snoozed one-off firing later, addressed to OWNER only --
    # exactly what `_execute_snooze`'s scheduled `send_reminder` call does.
    snooze_channel = FakeChannel()
    water = DEFAULT_REGISTRY.get("water")
    await send_reminder(snooze_channel, OWNER, water, "en", db, config, state)

    assert snooze_channel.sent == [(OWNER, i18n.t("reminder_water", "en"))]


# ---------------------------------------------------------------------------
# Cross-track: the combined dispatch table (schedules + access + preferences
# + the pre-existing v1.1 kinds, all landed in the same `core/commands.py`)
# has no precedence conflicts -- every trigger below must dispatch as its
# OWN kind, never misfire as "remind" or vice versa.
# ---------------------------------------------------------------------------


CROSS_MODULE_DISPATCH_TABLE = [
    ("/remind water 08:00", "remind"),
    ("เตือน น้ำ 08:00", "remind"),
    ("/lang en", "lang"),
    ("ภาษา th", "lang"),
    ("/quiet 22:00-07:00", "quiet"),
    ("เงียบ off", "quiet"),
    ("/start", "start"),
    ("/users", "users"),
    ("/approve 88899900", "approve"),
    ("/block 88899900", "block"),
    ("/invite 88899900", "invite"),
    ("/target water 2000", "target"),
    ("/undo", "undo"),
    ("/help", "help"),
    ("/habits", "habits"),
    ("snooze 30", "snooze"),
]


@pytest.mark.parametrize("text,expected_kind", CROSS_MODULE_DISPATCH_TABLE)
def test_combined_dispatch_table_has_no_precedence_conflicts(text, expected_kind):
    command = commands.dispatch(text, DEFAULT_REGISTRY)
    assert command is not None
    assert command.kind == expected_kind


def test_remind_trigger_never_shadowed_by_any_other_v12_module():
    """The inverse check: none of the other modules' triggers accidentally
    claim a `/remind`/`เตือน` message before `_match_remind` gets to it (or
    vice versa) -- `dispatch()` places `remind` right after `target`, ahead
    of `access`/`lang`/`quiet`; this asserts that ordering doesn't matter in
    practice because the trigger texts are genuinely disjoint."""
    for text, _ in CROSS_MODULE_DISPATCH_TABLE:
        command = commands.dispatch(text, DEFAULT_REGISTRY)
        is_remind_trigger = text.startswith("/remind") or text.startswith("เตือน")
        if is_remind_trigger:
            assert command.kind == "remind"
        else:
            assert command is None or command.kind != "remind"
