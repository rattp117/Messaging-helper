"""Vera's adversarial gap coverage for SPEC-v1.7.md's `habitdef` track
(AC-H1-AC-H6), on top of Luna's own `tests/test_habitdef.py` (101 tests).

Does NOT duplicate what test_habitdef.py already covers -- this file
probes: pipe-grammar edge cases (duplicate keys, empty values, full-width
chars, whitespace variants), an EXHAUSTIVE sweep of every word in
`commands.reserved_trigger_words()` (not a sample), the unit-collision
interplay with a BASE habit's own preparse token, Thai-language
confirmation/error replies (test_habitdef.py's execute_* tests are
overwhelmingly `lang="en"`), audit old/new-value content, a larger
Thai-alias adversarial corpus, and the AC-5 byte-identical regression gate
re-checked at this module's own integration surface.

Same conventions as test_habitdef.py: `commands.dispatch` directly for
dispatch, `execute_*` against a real on-disk SQLite `Database`, no DB
mocks.
"""

from __future__ import annotations

import json

import pytest

from habit_assistant.config import Config
from habit_assistant.core import commands, discoverability, habitdef, i18n, units
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.registry_provider import RegistryProvider
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner-chat"
MEMBER = "member-chat-b"

BASE_REGISTRY = HabitRegistry.from_config(Config())

ADDHABIT_EXAMPLE = "/addhabit id=reading|type=duration|en=reading|th=อ่านหนังสือ|unit=min/นาที|goal=30"


def _habit(id_: str, type_: str = "text", **kw) -> Habit:
    defaults = dict(
        label_en="test",
        label_th="ทดสอบ",
        unit_en=None,
        unit_th=None,
        goal=None,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )
    defaults.update(kw)
    return Habit(id=id_, type=type_, **defaults)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habitdef_gaps.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(MEMBER, role="member", status="active")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def provider(db, config):
    return RegistryProvider(config, db)


def _fields(**overrides) -> dict[str, str]:
    base = {"id": "reading", "type": "duration", "en": "reading", "th": "อ่านหนังสือ", "unit": "min/นาที", "goal": "30"}
    base.update(overrides)
    return base


# ===========================================================================
# Pipe key=value grammar edge cases (dispatch layer, _parse_addhabit_fields).
# ===========================================================================


def test_addhabit_duplicate_key_last_value_wins():
    cmd = commands.dispatch("/addhabit id=first|type=text|en=a|id=second", BASE_REGISTRY)
    assert cmd.kind == "addhabit"
    assert cmd.fields["id"] == "second"  # last occurrence wins, no crash, no dedup error


def test_addhabit_empty_value_for_required_key_is_usage_error_no_write(db, config, provider):
    # "en=" with nothing after it -- key present, value empty. Must be
    # treated the same as a MISSING key (R-V1's own required-key check
    # strips before testing truthiness), not silently accepted as "".
    cmd = commands.dispatch("/addhabit id=x|type=text|en=", provider.for_user(MEMBER))
    assert cmd.fields == {"id": "x", "type": "text", "en": ""}
    row, msg_id, kwargs = habitdef.validate_and_normalize(cmd.fields, BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20)
    assert row is None
    assert msg_id == "addhabit_usage"


def test_addhabit_whitespace_only_value_for_required_key_is_usage_error():
    fields = {"id": "x", "type": "text", "en": "   "}
    row, msg_id, kwargs = habitdef.validate_and_normalize(fields, BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20)
    assert row is None
    assert msg_id == "addhabit_usage"


def test_addhabit_empty_segment_from_stray_pipes_is_skipped_not_an_error():
    cmd = commands.dispatch("/addhabit ||id=x|type=text|en=a||", BASE_REGISTRY)
    assert cmd.kind == "addhabit"
    assert cmd.fields == {"id": "x", "type": "text", "en": "a"}


def test_addhabit_tail_with_no_equals_sign_anywhere_has_no_fields_shape():
    cmd = commands.dispatch("/addhabit id x type text", BASE_REGISTRY)
    assert cmd.kind == "addhabit"
    assert cmd.fields is None  # no "=" at all -- shape parse fails, usage reply downstream


def test_addhabit_one_segment_missing_equals_among_valid_ones_fails_the_whole_parse():
    # Grammar-shape rule: EVERY non-empty segment must contain "=" -- one
    # bad segment invalidates the whole tail (mirrors _parse_addhabit_
    # fields's own docstring), not a partial/best-effort parse.
    cmd = commands.dispatch("/addhabit id=x|typetext|en=a", BASE_REGISTRY)
    assert cmd.fields is None


@pytest.mark.parametrize(
    "bad_id",
    [
        "id=１２３",  # full-width digits -- NOT in ASCII [0-9], must be rejected
        "id=ｒｅａｄｉｎｇ",  # full-width Latin letters -- NOT in ASCII [a-z]
        "id=abc！",  # full-width exclamation appended
    ],
)
def test_addhabit_full_width_characters_in_id_are_rejected(bad_id):
    key, _, raw_id = bad_id.partition("=")
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(id=raw_id), BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20
    )
    assert row is None
    assert msg_id == "addhabit_invalid_id"


def test_addhabit_id_normalization_collapses_tabs_and_newlines_too():
    # _normalize_id's `\s+` matches ALL whitespace classes, not just the
    # ASCII space _parse_addhabit_fields's own .strip() already removes at
    # the edges -- an internal tab/newline must still collapse to "_".
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(id="morning\twalk\ntoday"), BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20
    )
    assert msg_id is None
    assert row["id"] == "morning_walk_today"


def test_addhabit_overlong_label_and_unit_values_do_not_crash():
    # No spec'd length cap on label/unit (only id has one, 32 chars) --
    # this just proves a very long value degrades gracefully (accepted or
    # rejected via a normal msg_id) rather than raising.
    long_label = "x" * 5000
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(id="longlabel", en=long_label, th=long_label), BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20
    )
    assert msg_id is None
    assert row["label_en"] == long_label


def test_addhabit_unknown_extra_keys_are_silently_ignored_not_an_error():
    fields = _fields()
    fields["bogus_key"] = "whatever"
    row, msg_id, kwargs = habitdef.validate_and_normalize(fields, BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20)
    assert msg_id is None  # unrecognized keys aren't validated/rejected, just ignored


def test_addhabit_id_case_is_normalized_even_via_thai_alias_trigger():
    cmd = commands.dispatch("เพิ่มนิสัย id=UPPER|type=text|en=Upper Case", BASE_REGISTRY)
    assert cmd.kind == "addhabit"
    row, msg_id, kwargs = habitdef.validate_and_normalize(cmd.fields, BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20)
    assert msg_id is None
    assert row["id"] == "upper"


# ===========================================================================
# Exhaustive reserved-trigger-word sweep -- EVERY word in
# commands.reserved_trigger_words(), not a sample, tried as id AND as
# label (en/th path), including real command triggers.
# ===========================================================================


@pytest.mark.parametrize("word", sorted(commands.reserved_trigger_words()))
def test_every_reserved_trigger_word_is_rejected_as_a_label(word):
    reserved = commands.reserved_trigger_words()
    fields = _fields(id="zzz_unique_probe_id", en=word, th=word)
    row, msg_id, kwargs = habitdef.validate_and_normalize(fields, BASE_REGISTRY, BASE_REGISTRY, reserved, 20)
    assert row is None
    assert msg_id == "addhabit_reserved_word"
    assert kwargs["word"].strip().lower() == word.strip().lower()


@pytest.mark.parametrize("word", sorted(w for w in commands.reserved_trigger_words() if w.isascii()))
def test_every_ascii_reserved_trigger_word_is_rejected_as_an_id(word):
    reserved = commands.reserved_trigger_words()
    fields = _fields(id=word.lower(), en="harmless label", th="ป้ายกำกับ")
    row, msg_id, kwargs = habitdef.validate_and_normalize(fields, BASE_REGISTRY, BASE_REGISTRY, reserved, 20)
    assert row is None
    assert msg_id in ("addhabit_reserved_word", "addhabit_shadow_base", "addhabit_invalid_id")
    # every ASCII trigger word is a valid id SHAPE (^[a-z0-9_]+$) and none
    # of them is a base habit id, so it must specifically be the reserved-
    # word rejection, not one of the other two id-rejection paths.
    assert msg_id == "addhabit_reserved_word"


@pytest.mark.parametrize(
    "trigger_message",
    [
        "/remind reading 08:00",
        "/target water 2000",
        "/checkin on",
        "/help",
        "/audit",
        "undo",
    ],
)
def test_real_command_trigger_words_dispatch_as_their_own_command_not_addhabit(trigger_message):
    # Sanity cross-check: the reserved words really are the SAME literals
    # dispatch() itself treats as commands (R-V3's own "single authoritative
    # source, can't drift" claim) -- each of these still dispatches to its
    # OWN kind, never accidentally shadowed by the addhabit/delhabit
    # matchers added in this track.
    cmd = commands.dispatch(trigger_message, BASE_REGISTRY)
    assert cmd is not None
    assert cmd.kind not in ("addhabit", "delhabit")


# ===========================================================================
# Unit-collision interplay: creating a colliding custom habit ALSO knocks
# the BASE habit's own token out of the per-user preparse lookup (the
# existing v1.5 two-way exclusion rule, units.py:build_unit_lookup) -- an
# important, easy-to-miss cross-effect of R-V4 worth locking down.
# ===========================================================================


async def test_colliding_custom_unit_also_disables_the_base_habits_own_preparse_token(db, config, provider):
    # water's own unit is "ml" (config.toml). A custom habit that ALSO
    # claims "ml" must degrade BOTH sides -- water's own preparse for "ml"
    # falls through to the LLM for this user too, not just the new habit's.
    baseline_lookup = units.build_unit_lookup(provider.for_user(MEMBER))
    assert baseline_lookup["ml"][0] == "water"  # sanity: uncontested before creation

    cmd = commands.dispatch("/addhabit id=juice|type=numeric|en=juice|unit=ml", provider.for_user(MEMBER))
    reply = await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    assert reply.startswith("✅")

    lookup = units.build_unit_lookup(provider.for_user(MEMBER))
    assert "ml" not in lookup  # BOTH water's and juice's "ml" excluded now

    # A second user who never created "juice" must be entirely unaffected
    # (per-user isolation) -- water's "ml" still preparses normally for them.
    other_lookup = units.build_unit_lookup(provider.for_user(OWNER))
    assert other_lookup["ml"][0] == "water"


async def test_colliding_unit_confirmation_still_succeeds_and_does_not_mention_failure(db, config, provider):
    cmd = commands.dispatch("/addhabit id=juice|type=numeric|en=juice|unit=ml", provider.for_user(MEMBER))
    reply = await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    # R-V4: creation is always ALLOWED despite the collision; spec says the
    # confirmation "may" note it -- Luna's IMPL.md flags she chose not to.
    # Not itself a failure (spec says "may", not "must") -- just confirming
    # the reply is the normal success shape, not an error-shaped one.
    assert reply.startswith("✅")
    assert "reading" not in reply  # sanity: not a stale/wrong-habit reply


# ===========================================================================
# Thai-language confirmation/error replies -- test_habitdef.py's execute_*
# tests are almost all lang="en"; verify the Thai half of the bilingual
# contract independently for both success and failure paths.
# ===========================================================================


async def test_execute_addhabit_success_reply_is_thai_when_lang_is_th(db, config, provider):
    cmd = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(MEMBER))
    reply = await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="th", user_id=MEMBER
    )
    assert reply == (
        '✅ เพิ่ม "อ่านหนังสือ" (reading) แล้ว — ระยะเวลา หน่วย นาที เป้าหมาย 30/วัน '
        'บันทึกได้แบบ "20 นาที" หรือใช้ /remind reading'
    )


async def test_execute_addhabit_reserved_word_error_reply_is_thai(db, config, provider):
    cmd = commands.dispatch("/addhabit id=x|type=text|en=เตือน", provider.for_user(MEMBER))
    reply = await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="th", user_id=MEMBER
    )
    assert reply == i18n.t("addhabit_reserved_word", "th", word="เตือน")
    assert "เพิ่มไม่ได้นะ" in reply


async def test_execute_addhabit_cap_reached_error_reply_is_thai(db, config, provider):
    for i in range(20):
        cmd = commands.dispatch(f"/addhabit id=h{i}|type=text|en=h{i}", provider.for_user(MEMBER))
        await habitdef.execute_addhabit(
            cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
        )
    over = commands.dispatch("/addhabit id=over|type=text|en=over", provider.for_user(MEMBER))
    reply = await habitdef.execute_addhabit(
        over, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="th", user_id=MEMBER
    )
    assert reply == i18n.t("addhabit_cap_reached", "th", cap=20)


async def test_execute_delhabit_archived_reply_is_thai(db, config, provider):
    add = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(MEMBER))
    await habitdef.execute_addhabit(
        add, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    db.insert_log(
        LogEntry(
            id=None,
            user_id=MEMBER,
            ts="2026-08-24T10:00:00",
            category="reading",
            value_num=20.0,
            value_text=None,
            raw_message="20 min",
            source="nl",
            habit_type="duration",
        )
    )
    delete = commands.dispatch("/delhabit reading", provider.for_user(MEMBER))
    reply = await habitdef.execute_delhabit(delete, db=db, provider=provider, lang="th", user_id=MEMBER)
    assert reply == i18n.t("delhabit_archived", "th", id="reading")
    assert "เก็บ" in reply and "คลัง" in reply


async def test_execute_delhabit_deleted_reply_is_thai(db, config, provider):
    add = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(MEMBER))
    await habitdef.execute_addhabit(
        add, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    delete = commands.dispatch("/delhabit reading", provider.for_user(MEMBER))
    reply = await habitdef.execute_delhabit(delete, db=db, provider=provider, lang="th", user_id=MEMBER)
    assert reply == i18n.t("delhabit_deleted", "th", id="reading")


async def test_habits_overview_shows_thai_label_for_thai_input_th_lang(db, config, provider):
    from datetime import datetime

    cmd = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(MEMBER))
    await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    overview_th = discoverability.build_habits_overview(
        db, config, provider.for_user(MEMBER), lambda: datetime(2026, 8, 24), "th", MEMBER
    )
    assert "อ่านหนังสือ" in overview_th


# ===========================================================================
# Audit content: old_value/new_value shape for all three actions -- the
# spec's AC-7 only requires the ACTION + localized label, but Archi's
# brief specifically asked to verify "correct old->new"; check what's
# actually stored so any gap is visible rather than assumed.
# ===========================================================================


async def test_habit_create_audit_row_new_value_is_the_habit_type(db, config, provider):
    cmd = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(MEMBER))
    await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    row = db.recent_audit(10)[0]
    assert row["action"] == "habit_create"
    assert row["old_value"] is None
    assert row["new_value"] == "duration"  # the habit's type -- the one thing that WAS captured


async def test_habit_archive_and_delete_audit_rows_old_and_new_value_content(db, config, provider):
    """Documents observed behavior: execute_delhabit's audit.record() call
    passes neither old_value nor new_value (core/habitdef.py:412-414) --
    both columns come back None/'-' for habit_archive/habit_delete rows.
    AC-7's own text only requires the action + localized label (both
    present), so this is NOT a spec violation, but it means the audit
    trail can't show what a deleted/archived habit actually WAS (its type,
    label) from this row alone -- flagged in the test report as a
    observation for Luna/Archi, not a failure."""
    add = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(MEMBER))
    await habitdef.execute_addhabit(
        add, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    delete = commands.dispatch("/delhabit reading", provider.for_user(MEMBER))
    await habitdef.execute_delhabit(delete, db=db, provider=provider, lang="en", user_id=MEMBER)

    rows = {r["action"]: r for r in db.recent_audit(10)}
    assert rows["habit_delete"]["old_value"] is None
    assert rows["habit_delete"]["new_value"] is None
    assert rows["habit_delete"]["entity"] == "reading"  # the id, at least, IS captured


# ===========================================================================
# Larger Thai-alias zero-false-positive corpus (เพิ่มนิสัย/ลบนิสัย) --
# ordinary Thai sentences that happen to contain เพิ่ม/ลบ/นิสัย as
# substrings or separate words must never trigger either command.
# ===========================================================================


@pytest.mark.parametrize(
    "message",
    [
        "วันนี้ฉันลบรูปภาพที่ไม่ต้องการออกจากโทรศัพท์",  # "ลบ" (delete) in an unrelated sentence
        "นิสัยของฉันคือการตื่นเช้า",  # "นิสัย" (habit) alone, no ลบ/เพิ่ม
        "เพิ่มเงินในกระเป๋า",  # "เพิ่ม" (add/increase) unrelated to habits
        "ฉันอยากลบนิสัยขี้เกียจแต่ไม่รู้จะทำยังไง",  # glued "ลบนิสัย" mid-sentence, prose continues
        "เพิ่มนิสัยที่ดีๆ ให้กับตัวเอง",  # spaced-out prose after the trigger stem
        "นิสัยเพิ่มขึ้นเรื่อยๆ",  # "นิสัย" then "เพิ่ม" in reverse order, glued differently
        "ลบ นิสัย",  # trigger stem split across two SEPARATE tokens (space between)
    ],
)
def test_thai_alias_large_adversarial_corpus_never_false_positives(message):
    registry = HabitRegistry([*BASE_REGISTRY, _habit("reading", "duration", label_th="อ่านหนังสือ")])
    result = commands.dispatch(message, registry)
    assert result is None or result.kind not in ("addhabit", "delhabit")


def test_delhabit_th_prefix_ลบนิสัย_never_matches_as_plain_undo_ลบ_either():
    # Cross-check both directions of the ลบ/ลบนิสัย non-collision.
    registry = HabitRegistry([*BASE_REGISTRY, _habit("reading", "duration", label_th="อ่านหนังสือ")])
    delhabit_cmd = commands.dispatch("ลบนิสัย อ่านหนังสือ", registry)
    assert delhabit_cmd.kind == "delhabit"
    undo_cmd = commands.dispatch("ลบ อ่านหนังสือ", registry)
    # "ลบ <text>" is undo's own pattern with an optional target -- must
    # dispatch as undo (or fall through), never delhabit.
    assert undo_cmd is None or undo_cmd.kind != "delhabit"


# ===========================================================================
# AC-5 byte-identical regression gate re-checked at this module's own
# integration surface: a user with zero user_habits rows sees the addhabit/
# delhabit matchers add ZERO behavioral surface area to ordinary dispatch.
# ===========================================================================


@pytest.mark.parametrize(
    "ordinary_message",
    [
        "500ml",
        "20 min stretch",
        "just a normal diary entry about my day",
        "น้ำ 500 มล",
        "๕๐๐ มล",  # Thai numerals -- AC-6 lock, unaffected by this track
        "５００ml",  # full-width digits -- AC-6 lock
    ],
)
def test_ordinary_messages_are_unaffected_by_the_new_addhabit_delhabit_matchers(ordinary_message):
    before = commands.dispatch(ordinary_message, BASE_REGISTRY)
    # Whatever dispatch() already did for this message (None, or some
    # OTHER command kind) must be identical -- specifically never
    # "addhabit"/"delhabit".
    assert before is None or before.kind not in ("addhabit", "delhabit")


def test_owner_with_no_user_habits_rows_registry_is_still_byte_identical(db, config, provider):
    owner_registry = provider.for_user(OWNER)
    assert owner_registry.ids() == HabitRegistry.from_config(config).ids()
    assert len(owner_registry) == 3  # water/stretch/diary only
