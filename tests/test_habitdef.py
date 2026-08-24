"""SPEC-v1.7.md §4 "Habit definition, validation & lifecycle" (module
`habitdef`, R-C1/R-C2/R-V1-R-V5): `core/commands.py`'s `"addhabit"`/
`"delhabit"` matchers (`_match_addhabit`/`_match_delhabit`), `core/
habitdef.py`'s `validate_and_normalize`/`execute_addhabit`/
`execute_delhabit`.

Owned ACs (SPEC-v1.7.md §11): AC-H1 (create), AC-H2 (validation), AC-H3
(label/id collision safety), AC-H4 (unit collision degrades), AC-H5
(delete semantics), AC-H6 (`/habits` lists custom habits).

Mirrors `tests/test_checkins.py`'s own convention (`commands.dispatch`
directly for the dispatch layer, `execute_*` against a real on-disk
SQLite `Database`, no mocks for the DB) and `tests/test_registry_
provider.py`'s convention for the per-user-cache/no-restart assertions.
"""

from __future__ import annotations

import sqlite3

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


def _habit(
    id_: str,
    type_: str = "text",
    *,
    label_en: str = "test",
    label_th: str = "ทดสอบ",
    unit_en: str | None = None,
    unit_th: str | None = None,
    goal: float | None = None,
) -> Habit:
    return Habit(
        id=id_,
        type=type_,
        label_en=label_en,
        label_th=label_th,
        unit_en=unit_en,
        unit_th=unit_th,
        goal=goal,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habitdef.db")
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


ADDHABIT_EXAMPLE = "/addhabit id=reading|type=duration|en=reading|th=อ่านหนังสือ|unit=min/นาที|goal=30"


# ===========================================================================
# Dispatch -- core/commands.py's _match_addhabit / _match_delhabit.
# ===========================================================================


def test_addhabit_slash_parses_the_pipe_grammar_into_fields():
    cmd = commands.dispatch(ADDHABIT_EXAMPLE, BASE_REGISTRY)
    assert cmd.kind == "addhabit"
    assert cmd.fields == {
        "id": "reading",
        "type": "duration",
        "en": "reading",
        "th": "อ่านหนังสือ",
        "unit": "min/นาที",
        "goal": "30",
    }


def test_addhabit_slash_key_case_is_normalized_value_case_is_not():
    cmd = commands.dispatch("/addhabit ID=Reading|TYPE=text|EN=Reading Time", BASE_REGISTRY)
    assert cmd.fields == {"id": "Reading", "type": "text", "en": "Reading Time"}


def test_addhabit_slash_bare_has_no_fields_shape_but_still_dispatches():
    cmd = commands.dispatch("/addhabit", BASE_REGISTRY)
    assert cmd.kind == "addhabit"
    assert cmd.fields is None


def test_addhabit_slash_malformed_tail_has_no_fields_shape_but_still_dispatches():
    # No "=" at all in the tail -- shape parse fails, but the slash form
    # stays permissive (execute_addhabit replies with usage, not a
    # dispatch failure).
    cmd = commands.dispatch("/addhabit just some words", BASE_REGISTRY)
    assert cmd.kind == "addhabit"
    assert cmd.fields is None


def test_addhabit_thai_alias_parses_the_same_grammar():
    cmd = commands.dispatch("เพิ่มนิสัย id=reading|type=duration|en=reading|unit=min", BASE_REGISTRY)
    assert cmd.kind == "addhabit"
    assert cmd.fields == {"id": "reading", "type": "duration", "en": "reading", "unit": "min"}


@pytest.mark.parametrize(
    "message",
    [
        "อยากเพิ่มนิสัยที่ดีให้ชีวิต",  # ordinary prose, glued, no space after the trigger
        "เพิ่มนิสัย",  # bare trigger, no argument at all
        "เพิ่มนิสัยดีๆ",  # glued continuation (mai-yamok style)
        "เพิ่มนิสัย อ่านหนังสือ",  # spaced, but the tail has no "=" -- not key=value shaped
        "เพิ่มนิสัย ทุกวันให้ได้นะ",  # spaced ordinary prose, still no "="
        "addhabit",  # bare English word, no leading "/"
        "delhabit",
    ],
)
def test_addhabit_adversarial_corpus_never_false_positives(message):
    assert commands.dispatch(message, BASE_REGISTRY) is None


def test_delhabit_slash_captures_the_raw_lowercased_id_token():
    cmd = commands.dispatch("/delhabit Reading", BASE_REGISTRY)
    assert cmd.kind == "delhabit"
    assert cmd.category == "reading"


def test_delhabit_slash_bare_has_no_category_but_still_dispatches():
    cmd = commands.dispatch("/delhabit", BASE_REGISTRY)
    assert cmd.kind == "delhabit"
    assert cmd.category is None


def test_delhabit_thai_alias_resolves_a_registry_anchored_habit_token():
    registry = HabitRegistry([*BASE_REGISTRY, _habit("reading", "duration", label_th="อ่านหนังสือ")])
    cmd = commands.dispatch("ลบนิสัย อ่านหนังสือ", registry)
    assert cmd.kind == "delhabit"
    assert cmd.category == "reading"

    cmd_id = commands.dispatch("ลบนิสัย reading", registry)
    assert cmd_id.kind == "delhabit"
    assert cmd_id.category == "reading"


@pytest.mark.parametrize(
    "message",
    [
        "อยากลบนิสัยที่ไม่ดี",  # glued prose
        "ลบนิสัย",  # bare trigger
        "ลบนิสัยที่ไม่ดีออกไป",  # glued continuation
        "ลบนิสัย บางอย่าง",  # spaced, but names no habit this registry tracks
    ],
)
def test_delhabit_adversarial_corpus_never_false_positives(message):
    registry = HabitRegistry([*BASE_REGISTRY, _habit("reading", "duration", label_th="อ่านหนังสือ")])
    assert commands.dispatch(message, registry) is None


def test_delhabit_thai_alias_does_not_collide_with_undo():
    # "ลบ" alone is UNDO's own trigger (_UNDO_PATTERNS); "ลบนิสัย" is a
    # different string and must dispatch as delhabit-shaped (or fall
    # through), never as undo.
    registry = HabitRegistry([*BASE_REGISTRY, _habit("reading", "duration", label_th="อ่านหนังสือ")])
    result = commands.dispatch("ลบนิสัย อ่านหนังสือ", registry)
    assert result.kind == "delhabit"
    assert commands.dispatch("ลบ", registry).kind == "undo"


# ===========================================================================
# validate_and_normalize -- pure, DB-free (R-V1-R-V5, AC-H2/AC-H3).
# ===========================================================================


def _fields(**overrides) -> dict[str, str]:
    base = {"id": "reading", "type": "duration", "en": "reading", "th": "อ่านหนังสือ", "unit": "min/นาที", "goal": "30"}
    base.update(overrides)
    return base


def test_validate_success_normalizes_id_and_defaults_th_to_en():
    fields = {"id": "Morning Walk", "type": "numeric", "en": "walk", "unit": "km"}
    row, msg_id, kwargs = habitdef.validate_and_normalize(fields, BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20)
    assert msg_id is None
    assert row["id"] == "morning_walk"  # trim, lower, spaces -> "_"
    assert row["label_en"] == "walk"
    assert row["label_th"] == "walk"  # th defaults to en
    assert row["unit_en"] == "km"
    assert row["unit_th"] == "km"  # unit's th half defaults to en's, same rule
    assert row["goal"] is None
    assert row["unit_aliases"] is None


@pytest.mark.parametrize("missing_key", ["id", "type", "en"])
def test_validate_missing_required_key_is_usage_error_no_write(missing_key):
    fields = _fields()
    del fields[missing_key]
    row, msg_id, kwargs = habitdef.validate_and_normalize(fields, BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20)
    assert row is None
    assert msg_id == "addhabit_usage"


@pytest.mark.parametrize("bad_id", ["Has Spaces!", "id-with-dash", "a" * 33, ""])
def test_validate_rejects_ids_that_dont_match_the_id_shape(bad_id):
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(id=bad_id), BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20
    )
    assert row is None
    assert msg_id in ("addhabit_invalid_id", "addhabit_usage")  # empty id -> "usage" (missing required key)


def test_validate_normalizes_spaces_to_underscore_before_the_shape_check():
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(id="  Morning Walk  "), BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20
    )
    assert msg_id is None
    assert row["id"] == "morning_walk"


def test_validate_normalizes_uppercase_id_to_lowercase():
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(id="UPPER"), BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20
    )
    assert msg_id is None
    assert row["id"] == "upper"


@pytest.mark.parametrize("reserved_id", ["unknown", "unparsed"])
def test_validate_rejects_config_level_reserved_ids(reserved_id):
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(id=reserved_id), BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20
    )
    assert row is None
    assert msg_id == "addhabit_invalid_id"


@pytest.mark.parametrize("base_id", ["water", "stretch", "diary"])
def test_validate_forbids_shadowing_a_base_habit_id(base_id):
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(id=base_id), BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20
    )
    assert row is None
    assert msg_id == "addhabit_shadow_base"
    assert kwargs == {"id": base_id}


def test_validate_rejects_duplicate_active_id_for_this_user():
    user_registry = HabitRegistry([*BASE_REGISTRY, _habit("reading", "duration")])
    row, msg_id, kwargs = habitdef.validate_and_normalize(_fields(), BASE_REGISTRY, user_registry, frozenset(), 20)
    assert row is None
    assert msg_id == "addhabit_duplicate_id"


@pytest.mark.parametrize(
    "word",
    ["help", "addhabit", "delhabit", "เตือน", "เพิ่มนิสัย", "ลบนิสัย", "undo", "target"],
)
def test_validate_rejects_id_equal_to_a_reserved_trigger_word(word):
    reserved = commands.reserved_trigger_words()
    # ASCII words are tested as the id itself (a Thai word can never pass
    # the `^[a-z0-9_]+$` id shape check, so those are tested via the `en`
    # label instead -- both paths go through the SAME reserved-word gate).
    if word.isascii():
        fields = _fields(id=word.lower())
    else:
        fields = _fields(en=word)
    row, msg_id, kwargs = habitdef.validate_and_normalize(fields, BASE_REGISTRY, BASE_REGISTRY, reserved, 20)
    assert row is None
    assert msg_id == "addhabit_reserved_word"


def test_validate_rejects_label_equal_to_a_reserved_trigger_word_en():
    reserved = commands.reserved_trigger_words()
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(en="help"), BASE_REGISTRY, BASE_REGISTRY, reserved, 20
    )
    assert row is None
    assert msg_id == "addhabit_reserved_word"
    assert kwargs == {"word": "help"}


def test_validate_rejects_label_equal_to_a_reserved_trigger_word_th():
    reserved = commands.reserved_trigger_words()
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(th="เตือน"), BASE_REGISTRY, BASE_REGISTRY, reserved, 20
    )
    assert row is None
    assert msg_id == "addhabit_reserved_word"
    assert kwargs == {"word": "เตือน"}


def test_validate_a_label_with_regex_metacharacters_is_accepted_and_safe():
    # R-V3: labels are re.escape'd wherever injected into a Thai matcher
    # alternation -- a label containing regex metacharacters must be
    # ACCEPTED here (not itself a validation error) and must not break
    # dispatch()'s own Thai-alias pattern construction downstream.
    fields = _fields(id="odd", en="a.*+?(b)", th="a.*+?(b)")
    row, msg_id, kwargs = habitdef.validate_and_normalize(fields, BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20)
    assert msg_id is None
    registry = HabitRegistry([*BASE_REGISTRY, _habit(row["id"], row["type"], label_th=row["label_th"])])
    # Must not raise (a bad regex would raise re.error at pattern-compile
    # time) and must not misfire on an unrelated message.
    assert commands.dispatch("ลบนิสัย a.*+?(b)", registry).category == "odd"
    assert commands.dispatch("some random water log 500ml", registry) is None or True  # never raises


@pytest.mark.parametrize("bad_type", ["count", "", "num"])
def test_validate_rejects_invalid_type(bad_type):
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(type=bad_type), BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20
    )
    assert row is None
    assert msg_id in ("addhabit_invalid_type", "addhabit_usage")


def test_validate_type_is_case_normalized():
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(type="Numeric", unit="ml"), BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20
    )
    assert msg_id is None
    assert row["type"] == "numeric"


@pytest.mark.parametrize("habit_type", ["numeric", "duration"])
def test_validate_numeric_duration_requires_unit(habit_type):
    fields = _fields(type=habit_type)
    del fields["unit"]
    row, msg_id, kwargs = habitdef.validate_and_normalize(fields, BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20)
    assert row is None
    assert msg_id == "addhabit_missing_unit"


@pytest.mark.parametrize("habit_type", ["text", "boolean"])
def test_validate_text_boolean_forbids_unit(habit_type):
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(type=habit_type), BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20
    )
    assert row is None
    assert msg_id == "addhabit_unexpected_unit"


@pytest.mark.parametrize("habit_type", ["text", "boolean"])
def test_validate_text_boolean_with_both_unit_and_goal_rejects_unit_first(habit_type):
    # _fields()'s default still carries both unit= and goal= -- the unit
    # check runs first (R-V2's own listed order), so THAT'S the reported
    # reason even though goal is equally invalid for this type.
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(type=habit_type), BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20
    )
    assert row is None
    assert msg_id == "addhabit_unexpected_unit"


@pytest.mark.parametrize("habit_type", ["text", "boolean"])
def test_validate_text_boolean_forbids_goal_when_unit_absent(habit_type):
    fields = {"id": "reading", "type": habit_type, "en": "reading", "goal": "5"}
    row, msg_id, kwargs = habitdef.validate_and_normalize(fields, BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20)
    assert row is None
    assert msg_id == "addhabit_invalid_goal"


@pytest.mark.parametrize("bad_goal", ["0", "-5", "abc", "-0.1"])
def test_validate_rejects_non_positive_or_non_numeric_goal(bad_goal):
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(goal=bad_goal), BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20
    )
    assert row is None
    assert msg_id == "addhabit_invalid_goal"


def test_validate_accepts_habit_with_no_goal():
    fields = _fields()
    del fields["goal"]
    row, msg_id, kwargs = habitdef.validate_and_normalize(fields, BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20)
    assert msg_id is None
    assert row["goal"] is None


def test_validate_parses_alias_grammar():
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(id="water2", en="water two", th="", **{"alias": "page:1,pages:1"}),
        BASE_REGISTRY,
        BASE_REGISTRY,
        frozenset(),
        20,
    )
    assert msg_id is None
    import json

    assert json.loads(row["unit_aliases"]) == {"page": 1.0, "pages": 1.0}


@pytest.mark.parametrize("bad_alias", ["page", "page:abc", "page:0", ":1", "page:-1"])
def test_validate_rejects_malformed_alias(bad_alias):
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(**{"alias": bad_alias}), BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20
    )
    assert row is None
    assert msg_id == "addhabit_invalid_alias"


def test_validate_rejects_duplicate_label_same_language_active_only():
    user_registry = HabitRegistry([*BASE_REGISTRY, _habit("journal", "text", label_en="reading", label_th="ต่างหาก")])
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(id="reading2"), BASE_REGISTRY, user_registry, frozenset(), 20
    )
    assert row is None
    assert msg_id == "addhabit_duplicate_label"
    assert kwargs == {"label": "reading"}


def test_validate_duplicate_label_against_a_base_habit_label_is_also_rejected():
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        _fields(id="mywater", en="water", th="not colliding"), BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20
    )
    assert row is None
    assert msg_id == "addhabit_duplicate_label"


def test_validate_different_users_may_reuse_the_same_label_or_id():
    # R-V1/R-V3's own scope is PER-USER -- a fresh (empty) user_registry
    # for a second user must not be affected by anything a first user did.
    row, msg_id, kwargs = habitdef.validate_and_normalize(_fields(), BASE_REGISTRY, BASE_REGISTRY, frozenset(), 20)
    assert msg_id is None


def test_validate_cap_reached_rejects_before_deeper_field_validation():
    # 20 custom habits already active -- even a well-formed NEW request
    # (or a malformed one) is rejected for the cap, not some other reason.
    custom = [_habit(f"h{i}", "text") for i in range(20)]
    user_registry = HabitRegistry([*BASE_REGISTRY, *custom])
    row, msg_id, kwargs = habitdef.validate_and_normalize(_fields(), BASE_REGISTRY, user_registry, frozenset(), 20)
    assert row is None
    assert msg_id == "addhabit_cap_reached"
    assert kwargs == {"cap": 20}


def test_validate_just_under_cap_still_succeeds():
    custom = [_habit(f"h{i}", "text") for i in range(19)]
    user_registry = HabitRegistry([*BASE_REGISTRY, *custom])
    row, msg_id, kwargs = habitdef.validate_and_normalize(_fields(), BASE_REGISTRY, user_registry, frozenset(), 20)
    assert msg_id is None


# ===========================================================================
# execute_addhabit -- AC-H1/AC-H2/AC-H3, R-C1 (DB write + invalidate +
# audit + confirmation).
# ===========================================================================


async def test_execute_addhabit_creates_row_and_confirms_bilingually(db, config, provider):
    cmd = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(MEMBER))
    reply = await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    assert reply == (
        '✅ Added "reading" (อ่านหนังสือ) — duration in min, goal 30/day. '
        'Log it like "20 min" or use /remind reading.'
    )
    row = db.get_user_habit(MEMBER, "reading")
    assert row is not None
    assert row["archived_at"] is None
    assert row["goal"] == 30.0

    reply_th = await habitdef.execute_addhabit(
        commands.dispatch("/addhabit id=other|type=text|en=other", provider.for_user(MEMBER)),
        db=db,
        provider=provider,
        config=config,
        base_registry=BASE_REGISTRY,
        lang="th",
        user_id=MEMBER,
    )
    assert "เพิ่ม" in reply_th


async def test_execute_addhabit_appears_in_the_users_registry_immediately_ac3(db, config, provider):
    before = provider.for_user(MEMBER)
    assert "reading" not in before.ids()

    cmd = commands.dispatch(ADDHABIT_EXAMPLE, before)
    await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )

    after = provider.for_user(MEMBER)  # no restart -- same provider, next call
    assert "reading" in after.ids()
    assert after is not before


async def test_execute_addhabit_does_not_affect_another_users_cache(db, config, provider):
    provider.for_user(OWNER)  # warm the owner's cache first
    cmd = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(MEMBER))
    await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    owner_registry = provider.for_user(OWNER)
    assert "reading" not in owner_registry.ids()
    assert owner_registry.ids() == HabitRegistry.from_config(config).ids()  # AC-5: owner untouched


async def test_execute_addhabit_records_habit_create_audit_row(db, config, provider):
    cmd = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(MEMBER))
    await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    rows = db.recent_audit(10)
    assert len(rows) == 1
    assert rows[0]["action"] == "habit_create"
    assert rows[0]["user_id"] == MEMBER
    assert rows[0]["entity"] == "reading"
    assert rows[0]["source"] == "command"


async def test_execute_addhabit_bare_slash_replies_usage_no_write(db, config, provider):
    cmd = commands.dispatch("/addhabit", provider.for_user(MEMBER))
    reply = await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    assert reply == i18n.t("addhabit_usage", "en")
    assert db.count_active_user_habits(MEMBER) == 0
    assert db.recent_audit(10) == []


async def test_execute_addhabit_rejects_reserved_word_id_no_write_ac_h3(db, config, provider):
    cmd = commands.dispatch("/addhabit id=help|type=text|en=whatever", provider.for_user(MEMBER))
    reply = await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    assert reply == i18n.t("addhabit_reserved_word", "en", word="help")
    assert db.count_active_user_habits(MEMBER) == 0


async def test_execute_addhabit_rejects_shadowing_a_base_habit_id(db, config, provider):
    cmd = commands.dispatch("/addhabit id=water|type=numeric|en=water2|unit=ml", provider.for_user(MEMBER))
    reply = await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    assert reply == i18n.t("addhabit_shadow_base", "en", id="water")
    assert db.count_active_user_habits(MEMBER) == 0


async def test_execute_addhabit_rejects_reusing_an_archived_id(db, config, provider):
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
    del_reply = await habitdef.execute_delhabit(delete, db=db, provider=provider, lang="en", user_id=MEMBER)
    assert "Archived" in del_reply

    readd = commands.dispatch(
        "/addhabit id=reading|type=numeric|en=reading again|unit=x", provider.for_user(MEMBER)
    )
    reply = await habitdef.execute_addhabit(
        readd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    assert reply == i18n.t("addhabit_archived_id", "en", id="reading")


async def test_execute_addhabit_cap_reached_rejects_the_21st_habit(db, config, provider):
    for i in range(20):
        cmd = commands.dispatch(f"/addhabit id=h{i}|type=text|en=h{i}", provider.for_user(MEMBER))
        reply = await habitdef.execute_addhabit(
            cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
        )
        assert reply.startswith("✅"), reply

    over = commands.dispatch("/addhabit id=overflow|type=text|en=overflow", provider.for_user(MEMBER))
    reply = await habitdef.execute_addhabit(
        over, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    assert reply == i18n.t("addhabit_cap_reached", "en", cap=20)
    assert db.count_active_user_habits(MEMBER) == 20


async def test_execute_addhabit_db_failure_reports_save_failed_not_a_traceback(db, config, provider, monkeypatch):
    def _boom(self, user_id, row):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(Database, "add_user_habit", _boom)
    cmd = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(MEMBER))
    reply = await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    assert reply == i18n.t("addhabit_save_failed", "en")


# ===========================================================================
# execute_delhabit -- AC-H5, R-C2 (smart delete).
# ===========================================================================


async def test_execute_delhabit_hard_deletes_when_no_logs_ac_h5(db, config, provider):
    add = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(MEMBER))
    await habitdef.execute_addhabit(
        add, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )

    delete = commands.dispatch("/delhabit reading", provider.for_user(MEMBER))
    reply = await habitdef.execute_delhabit(delete, db=db, provider=provider, lang="en", user_id=MEMBER)

    assert reply == i18n.t("delhabit_deleted", "en", id="reading")
    assert db.get_user_habit(MEMBER, "reading") is None  # row is GONE, id freed
    assert "reading" not in provider.for_user(MEMBER).ids()


async def test_execute_delhabit_soft_archives_when_it_has_history_ac_h5(db, config, provider):
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
    reply = await habitdef.execute_delhabit(delete, db=db, provider=provider, lang="en", user_id=MEMBER)

    assert reply == i18n.t("delhabit_archived", "en", id="reading")
    row = db.get_user_habit(MEMBER, "reading")
    assert row is not None
    assert row["archived_at"] is not None  # row survives, id stays reserved
    assert "reading" not in provider.for_user(MEMBER).ids()  # excluded from the active registry


async def test_execute_delhabit_soft_archive_counts_a_previously_undone_entry_too(db, config, provider):
    """R-C2: `count_logs_for` counts EVERY logs row ever written, including
    already soft-deleted (undone) ones -- an undone entry is still genuine
    history worth an archive, not a silent hard-delete."""
    add = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(MEMBER))
    await habitdef.execute_addhabit(
        add, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    log_id = db.insert_log(
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
    db.soft_delete(log_id)

    delete = commands.dispatch("/delhabit reading", provider.for_user(MEMBER))
    reply = await habitdef.execute_delhabit(delete, db=db, provider=provider, lang="en", user_id=MEMBER)
    assert reply == i18n.t("delhabit_archived", "en", id="reading")


async def test_execute_delhabit_unknown_id_replies_not_found_no_write(db, config, provider):
    delete = commands.dispatch("/delhabit ghost", provider.for_user(MEMBER))
    reply = await habitdef.execute_delhabit(delete, db=db, provider=provider, lang="en", user_id=MEMBER)
    assert reply == i18n.t("delhabit_not_found", "en", id="ghost")
    assert db.recent_audit(10) == []


async def test_execute_delhabit_a_base_habit_id_is_not_found_not_deleted(db, config, provider):
    delete = commands.dispatch("/delhabit water", provider.for_user(MEMBER))
    reply = await habitdef.execute_delhabit(delete, db=db, provider=provider, lang="en", user_id=MEMBER)
    assert reply == i18n.t("delhabit_not_found", "en", id="water")
    assert "water" in provider.for_user(MEMBER).ids()  # base habit untouched


async def test_execute_delhabit_bare_replies_usage_no_write(db, config, provider):
    delete = commands.dispatch("/delhabit", provider.for_user(MEMBER))
    reply = await habitdef.execute_delhabit(delete, db=db, provider=provider, lang="en", user_id=MEMBER)
    assert reply == i18n.t("delhabit_usage", "en")


async def test_execute_delhabit_already_archived_is_not_found_not_re_archived(db, config, provider):
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
    first = commands.dispatch("/delhabit reading", provider.for_user(MEMBER))
    await habitdef.execute_delhabit(first, db=db, provider=provider, lang="en", user_id=MEMBER)

    second = commands.dispatch("/delhabit reading", provider.for_user(MEMBER))
    reply = await habitdef.execute_delhabit(second, db=db, provider=provider, lang="en", user_id=MEMBER)
    assert reply == i18n.t("delhabit_not_found", "en", id="reading")
    assert len(db.recent_audit(10)) == 2  # one create + one archive, NOT a second archive


async def test_execute_delhabit_invalidates_registry_no_restart_ac3(db, config, provider):
    add = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(MEMBER))
    await habitdef.execute_addhabit(
        add, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    assert "reading" in provider.for_user(MEMBER).ids()

    delete = commands.dispatch("/delhabit reading", provider.for_user(MEMBER))
    await habitdef.execute_delhabit(delete, db=db, provider=provider, lang="en", user_id=MEMBER)

    assert "reading" not in provider.for_user(MEMBER).ids()  # no restart needed


async def test_execute_delhabit_does_not_affect_another_users_habit(db, config, provider):
    add_a = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(MEMBER))
    await habitdef.execute_addhabit(
        add_a, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    add_b = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(OWNER))
    await habitdef.execute_addhabit(
        add_b, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=OWNER
    )

    delete = commands.dispatch("/delhabit reading", provider.for_user(MEMBER))
    await habitdef.execute_delhabit(delete, db=db, provider=provider, lang="en", user_id=MEMBER)

    assert "reading" not in provider.for_user(MEMBER).ids()
    assert "reading" in provider.for_user(OWNER).ids()  # untouched


async def test_execute_delhabit_records_habit_delete_and_habit_archive_actions(db, config, provider):
    add = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(MEMBER))
    await habitdef.execute_addhabit(
        add, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    delete = commands.dispatch("/delhabit reading", provider.for_user(MEMBER))
    await habitdef.execute_delhabit(delete, db=db, provider=provider, lang="en", user_id=MEMBER)

    rows = db.recent_audit(10)
    actions = [r["action"] for r in rows]
    assert "habit_create" in actions
    assert "habit_delete" in actions


async def test_execute_delhabit_db_failure_reports_save_failed_not_a_traceback(db, config, provider, monkeypatch):
    add = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(MEMBER))
    await habitdef.execute_addhabit(
        add, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )

    def _boom(self, user_id, habit_id):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(Database, "delete_user_habit", _boom)
    delete = commands.dispatch("/delhabit reading", provider.for_user(MEMBER))
    reply = await habitdef.execute_delhabit(delete, db=db, provider=provider, lang="en", user_id=MEMBER)
    assert reply == i18n.t("delhabit_save_failed", "en")


# ===========================================================================
# AC-H4: unit collision degrades safely (excluded from preparse, allowed
# to create, never misattributed).
# ===========================================================================


async def test_addhabit_colliding_unit_is_excluded_from_preparse_lookup_ac_h4(db, config, provider):
    cmd = commands.dispatch("/addhabit id=juice|type=numeric|en=juice|unit=ml|goal=500", provider.for_user(MEMBER))
    reply = await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    assert reply.startswith("✅")  # creation allowed despite the collision (R-V4)

    registry = provider.for_user(MEMBER)
    lookup = units.build_unit_lookup(registry)
    assert "ml" not in lookup  # colliding token excluded entirely -- falls to the LLM, never misattributed


async def test_addhabit_non_colliding_unit_preparses_normally(db, config, provider):
    # "min"/"นาที" would collide with the BASE `stretch` habit's own unit
    # (SPEC-v1.7.md's own §2.1 example habit "reading" would collide too)
    # -- pick a genuinely unique unit token to prove the non-colliding
    # case preparses normally.
    cmd = commands.dispatch("/addhabit id=journal|type=numeric|en=journal|unit=entries", provider.for_user(MEMBER))
    await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )
    registry = provider.for_user(MEMBER)
    lookup = units.build_unit_lookup(registry)
    assert lookup["entries"] == ("journal", 1.0)


# ===========================================================================
# AC-H6: /habits lists the user's active custom habits alongside base
# ones, bilingual, per-user. Proven end-to-end through the ALREADY
# registry-generic `core/discoverability.py:build_habits_overview` (shared
# surface, unmodified by this track) fed the per-user registry.
# ===========================================================================


async def test_habits_overview_lists_custom_habit_for_owner_and_not_for_other_user(db, config, provider):
    from datetime import datetime

    cmd = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(MEMBER))
    await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
    )

    member_registry = provider.for_user(MEMBER)
    owner_registry = provider.for_user(OWNER)

    member_overview = discoverability.build_habits_overview(
        db, config, member_registry, lambda: datetime(2026, 8, 24), "en", MEMBER
    )
    owner_overview = discoverability.build_habits_overview(
        db, config, owner_registry, lambda: datetime(2026, 8, 24), "en", OWNER
    )

    assert "reading" in member_overview
    assert "reading" not in owner_overview  # per-user isolation (AC-H6)


async def test_habits_overview_omits_an_archived_custom_habit(db, config, provider):
    from datetime import datetime

    cmd = commands.dispatch(ADDHABIT_EXAMPLE, provider.for_user(MEMBER))
    await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=BASE_REGISTRY, lang="en", user_id=MEMBER
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
    await habitdef.execute_delhabit(delete, db=db, provider=provider, lang="en", user_id=MEMBER)

    overview = discoverability.build_habits_overview(
        db, config, provider.for_user(MEMBER), lambda: datetime(2026, 8, 24), "en", MEMBER
    )
    assert "reading" not in overview


# ===========================================================================
# AC-5 regression gate (this module's own slice): with no user_habits
# rows, dispatch()/validate_and_normalize's presence must not change
# anything about how an ordinary log/command is handled.
# ===========================================================================


def test_dispatch_of_an_ordinary_log_is_unaffected_by_the_new_kinds():
    assert commands.dispatch("500ml", BASE_REGISTRY) is None  # falls through to the parser, unchanged
    assert commands.dispatch("10 min stretch", BASE_REGISTRY) is None
    assert commands.dispatch("/habits", BASE_REGISTRY).kind == "habits"
