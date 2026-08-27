"""SPEC-v1.8.md §4 "Feature -- routines / habit stacks (module `routines`)"
(R-R1-R-R6): `core/commands.py`'s `"routine"` kind (`_match_routine`),
`core/routines.py`'s `execute_routine`/`handle_routine_callback`, and
migration 011 (`routines`/`routine_items`).

Owned ACs (SPEC-v1.8.md §11): AC-B1 (create + validation), AC-B2 (list),
AC-B3 (run), AC-B4 (delete), AC-B5 (isolation), AC-B6 (migration 011),
AC-B7 (zero-LLM).

Mirrors `tests/test_habitdef.py`'s own convention (`commands.dispatch`
directly for the dispatch layer, `execute_*` against a real on-disk SQLite
`Database`, no mocks for the DB) and `tests/test_checkins.py`'s own
`FakeChannel` convention for send/dashboard-refresh assertions.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Button, Channel
from habit_assistant.config import Config
from habit_assistant.core import commands, i18n, routines
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.registry_provider import RegistryProvider
from habit_assistant.storage.db import Database

OWNER = "owner-chat"
MEMBER = "member-chat-b"

BASE_REGISTRY = HabitRegistry.from_config(Config())


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.actionable: list[tuple[str, str, list[Button]]] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append((chat_id, text))

    async def send_actionable(self, chat_id: str, text: str, buttons: list[Button]) -> None:
        self.actionable.append((chat_id, text, buttons))

    async def run(self, on_message: Callable[[str, str], Awaitable[None]], on_callback=None) -> None:
        raise NotImplementedError

    def sent_to(self, chat_id: str) -> list[str]:
        return [text for cid, text in self.sent if cid == chat_id]


def _habit(
    id_: str,
    type_: str = "numeric",
    *,
    label_en: str = "test",
    label_th: str = "ทดสอบ",
    unit_en: str | None = "u",
    unit_th: str | None = "ห",
    goal: float | None = None,
) -> Habit:
    return Habit(
        id=id_,
        type=type_,
        label_en=label_en,
        label_th=label_th,
        unit_en=unit_en if type_ in ("numeric", "duration") else None,
        unit_th=unit_th if type_ in ("numeric", "duration") else None,
        goal=goal,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "routines.db")
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


CREATE_EXAMPLE = "/routine morning = water 500, stretch 10"


# ===========================================================================
# Dispatch -- core/commands.py's _match_routine.
# ===========================================================================


def test_routine_slash_create_parses_name_and_items():
    cmd = commands.dispatch(CREATE_EXAMPLE, BASE_REGISTRY)
    assert cmd.kind == "routine"
    assert cmd.routine_action == "create"
    assert cmd.routine_name == "morning"
    assert cmd.routine_items == [("water", "500"), ("stretch", "10")]


def test_routine_slash_create_item_value_may_carry_a_space_separated_unit():
    cmd = commands.dispatch("/routine evening = stretch 20 min", BASE_REGISTRY)
    assert cmd.routine_items == [("stretch", "20 min")]


def test_routine_slash_bare_is_list():
    cmd = commands.dispatch("/routine", BASE_REGISTRY)
    assert cmd.kind == "routine"
    assert cmd.routine_action == "list"
    assert cmd.routine_name is None


def test_routine_slash_run_parses_name():
    cmd = commands.dispatch("/routine morning", BASE_REGISTRY)
    assert cmd.kind == "routine"
    assert cmd.routine_action == "run"
    assert cmd.routine_name == "morning"


def test_routine_slash_delete_parses_name():
    cmd = commands.dispatch("/routine delete morning", BASE_REGISTRY)
    assert cmd.kind == "routine"
    assert cmd.routine_action == "delete"
    assert cmd.routine_name == "morning"


def test_routine_slash_create_malformed_items_still_dispatches_with_none_items():
    # "=" present but no habit/value tokens on either side of a comma
    # segment -- shape parse fails, but the slash form stays permissive
    # (execute_routine replies with usage, not a dispatch failure), same
    # convention as /addhabit's own `fields=None`.
    cmd = commands.dispatch("/routine morning = water", BASE_REGISTRY)
    assert cmd.kind == "routine"
    assert cmd.routine_action == "create"
    assert cmd.routine_items is None


def test_routine_thai_create_parses_name_and_items():
    cmd = commands.dispatch("กิจวัตร morning = water 500, stretch 10", BASE_REGISTRY)
    assert cmd.kind == "routine"
    assert cmd.routine_action == "create"
    assert cmd.routine_name == "morning"
    assert cmd.routine_items == [("water", "500"), ("stretch", "10")]


def test_routine_thai_run_parses_name():
    cmd = commands.dispatch("กิจวัตร morning", BASE_REGISTRY)
    assert cmd.kind == "routine"
    assert cmd.routine_action == "run"
    assert cmd.routine_name == "morning"


def test_routine_thai_delete_tail_form_parses_name():
    cmd = commands.dispatch("กิจวัตร morning ลบ", BASE_REGISTRY)
    assert cmd.kind == "routine"
    assert cmd.routine_action == "delete"
    assert cmd.routine_name == "morning"


@pytest.mark.parametrize(
    "message",
    [
        "กิจวัตร",  # bare trigger, no argument -- SPEC-v1.8.md §2.3 never
        # annotates the bare-list line with a Thai alias.
        "กิจวัตรประจำวันของฉันคือดื่มน้ำเยอะๆ",  # ordinary prose, glued
        "อยากมีกิจวัตรที่ดีให้ชีวิต",  # ordinary prose, กิจวัตร mid-sentence
        "กิจวัตร ประจำวัน",  # spaced, but the tail isn't ASCII id-shaped
        "กิจวัตร ของฉันคือการออกกำลังกาย",  # spaced ordinary prose
        "routine",  # bare English word, no leading "/"
        "my morning routine is great",  # ordinary English prose
    ],
)
def test_routine_adversarial_corpus_never_false_positives(message):
    assert commands.dispatch(message, BASE_REGISTRY) is None


def test_v18_kind_routine_dispatches_now(commands_dispatch=commands.dispatch):
    # The v1.8 shared-surface skeleton only guaranteed the bare LITERAL
    # word "routine"/"กิจวัตร" stayed None (still true, see the adversarial
    # corpus above) -- this module's own matcher is what makes the real
    # "/routine ..." shapes dispatch, which is what this test proves.
    assert commands.dispatch("/routine", BASE_REGISTRY).kind == "routine"


# ===========================================================================
# execute_routine -- create (AC-B1).
# ===========================================================================


@pytest.mark.asyncio
async def test_execute_routine_create_success_inserts_and_confirms(db, config, provider):
    cmd = commands.dispatch(CREATE_EXAMPLE, BASE_REGISTRY)
    channel = FakeChannel()
    reply = await routines.execute_routine(
        cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    assert reply is not None
    assert "morning" in reply
    assert db.get_routine(OWNER, "morning") == [("water", 500.0), ("stretch", 10.0)]
    assert db.count_routines(OWNER) == 1

    row = db.recent_audit(1)[0]
    assert row["action"] == "routine_create"
    assert row["user_id"] == OWNER
    assert row["entity"] == "morning"


@pytest.mark.asyncio
async def test_execute_routine_create_bilingual(db, config, provider):
    cmd = commands.dispatch(CREATE_EXAMPLE, BASE_REGISTRY)
    channel = FakeChannel()
    reply_th = await routines.execute_routine(
        cmd, db=db, channel=channel, config=config, provider=provider, lang="th", user_id=OWNER
    )
    assert any(ch in reply_th for ch in "กิจวัตร")


@pytest.mark.asyncio
async def test_execute_routine_create_invalid_name_no_write(db, config, provider):
    cmd = commands.dispatch("/routine Morning Time = water 500", BASE_REGISTRY)
    # The name token itself is "Morning" (space-delimited) -- still invalid
    # (uppercase) regardless; use a name with an outright bad character.
    cmd = commands.dispatch("/routine mor-ning = water 500", BASE_REGISTRY)
    channel = FakeChannel()
    reply = await routines.execute_routine(
        cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    assert "Couldn't save" in reply
    assert db.count_routines(OWNER) == 0


@pytest.mark.asyncio
async def test_execute_routine_create_duplicate_name_no_write(db, config, provider):
    cmd = commands.dispatch(CREATE_EXAMPLE, BASE_REGISTRY)
    channel = FakeChannel()
    await routines.execute_routine(
        cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    reply = await routines.execute_routine(
        cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    assert "already have" in reply
    assert db.count_routines(OWNER) == 1  # unchanged -- no second write


@pytest.mark.asyncio
async def test_execute_routine_create_empty_items_shape_no_write(db, config, provider):
    cmd = commands.dispatch("/routine morning = water", BASE_REGISTRY)  # no value token
    channel = FakeChannel()
    reply = await routines.execute_routine(
        cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    assert "routine" in reply.lower() or "/routine" in reply
    assert db.count_routines(OWNER) == 0


@pytest.mark.asyncio
async def test_execute_routine_create_unknown_habit_no_write(db, config, provider):
    cmd = commands.dispatch("/routine morning = coffee 2", BASE_REGISTRY)
    channel = FakeChannel()
    reply = await routines.execute_routine(
        cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    assert '"coffee"' in reply
    assert db.count_routines(OWNER) == 0


@pytest.mark.asyncio
async def test_execute_routine_create_unparseable_value_no_write(db, config, provider):
    cmd = commands.dispatch("/routine morning = water lots", BASE_REGISTRY)
    channel = FakeChannel()
    reply = await routines.execute_routine(
        cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    assert "Couldn't save" in reply
    assert db.count_routines(OWNER) == 0


@pytest.mark.asyncio
async def test_execute_routine_create_cap_reached_no_write(db, config, provider):
    config.routines.max_per_user = 1
    channel = FakeChannel()
    first = commands.dispatch("/routine one = water 100", BASE_REGISTRY)
    await routines.execute_routine(
        first, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    second = commands.dispatch("/routine two = water 200", BASE_REGISTRY)
    reply = await routines.execute_routine(
        second, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    assert "limit" in reply.lower()
    assert db.count_routines(OWNER) == 1


@pytest.mark.asyncio
async def test_execute_routine_create_allows_boolean_and_text_items(db, config, provider):
    """R-R1/R-R3: a boolean or text habit item is ALLOWED at creation --
    R-R3's own run-time description ("boolean -> true; text item ->
    skipped, can't carry free text") presupposes both types can appear in
    a routine's stored items. `diary` is already a base text habit;
    `meditated` is added as a real custom habit so it's actually present
    in the OWNER's own live per-user registry (`execute_routine` resolves
    items against `provider.for_user`, not any registry `commands.dispatch`
    happened to be called with -- that call only shapes the command)."""
    db.add_user_habit(
        OWNER,
        {"id": "meditated", "type": "boolean", "label_en": "meditated", "label_th": "นั่งสมาธิ"},
    )
    provider.invalidate(OWNER)
    registry = provider.for_user(OWNER)
    cmd = commands.dispatch("/routine morning = meditated 1, diary 1", registry)
    channel = FakeChannel()
    reply = await routines.execute_routine(
        cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    assert reply is not None
    stored = db.get_routine(OWNER, "morning")
    assert stored is not None and len(stored) == 2


# ===========================================================================
# execute_routine -- list (AC-B2).
# ===========================================================================


@pytest.mark.asyncio
async def test_execute_routine_list_empty(db, config, provider):
    channel = FakeChannel()
    cmd = commands.dispatch("/routine", BASE_REGISTRY)
    reply = await routines.execute_routine(
        cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    assert reply is None  # list sends its own message
    assert len(channel.sent) == 1
    assert "don't have any routines" in channel.sent[0][1]


@pytest.mark.asyncio
async def test_execute_routine_list_shows_items_and_one_run_button_each(db, config, provider):
    channel = FakeChannel()
    create_cmd = commands.dispatch(CREATE_EXAMPLE, BASE_REGISTRY)
    await routines.execute_routine(
        create_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    create_cmd2 = commands.dispatch("/routine evening = stretch 5", BASE_REGISTRY)
    await routines.execute_routine(
        create_cmd2, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )

    list_cmd = commands.dispatch("/routine", BASE_REGISTRY)
    reply = await routines.execute_routine(
        list_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    assert reply is None
    assert len(channel.actionable) == 1
    chat_id, text, buttons = channel.actionable[0]
    assert chat_id == OWNER
    assert "morning" in text and "evening" in text
    button_data = {data for _, data in buttons}
    assert button_data == {"routine:run:morning", "routine:run:evening"}


@pytest.mark.asyncio
async def test_execute_routine_list_is_per_user(db, config, provider):
    channel = FakeChannel()
    create_cmd = commands.dispatch(CREATE_EXAMPLE, BASE_REGISTRY)
    await routines.execute_routine(
        create_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    list_cmd = commands.dispatch("/routine", BASE_REGISTRY)
    await routines.execute_routine(
        list_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=MEMBER
    )
    _, text, _ = channel.actionable[-1] if channel.actionable else (None, channel.sent[-1][1], None)
    assert "don't have any routines" in text


# ===========================================================================
# execute_routine -- run (AC-B3).
# ===========================================================================


def _fixed_clock(y=2026, m=8, d=25, hh=9, mm=0):
    return lambda: datetime(y, m, d, hh, mm, 0)


@pytest.mark.asyncio
async def test_execute_routine_run_logs_items_one_summary_one_refresh(db, config, provider):
    channel = FakeChannel()
    create_cmd = commands.dispatch(CREATE_EXAMPLE, BASE_REGISTRY)
    await routines.execute_routine(
        create_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    channel.sent.clear()

    clock = _fixed_clock()
    run_cmd = commands.dispatch("/routine morning", BASE_REGISTRY)
    reply = await routines.execute_routine(
        run_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER, clock=clock
    )
    assert reply is not None
    assert "2 of 2" in reply

    today = clock().date().isoformat()
    assert db.sum_value(OWNER, "water", today) == 500.0
    assert db.sum_value(OWNER, "stretch", today) == 10.0

    audit_row = db.recent_audit(1)[0]
    assert audit_row["action"] == "routine_run"

    # Dashboard refresh is fail-open/no-op for a user with no live
    # dashboard, but the underlying call site is what we're proving: no
    # exception, and no dashboard-only send was made (default: off).
    assert channel.sent == []  # execute_routine itself sends nothing here (caller sends the returned text)


@pytest.mark.asyncio
async def test_execute_routine_run_skips_archived_item_and_notes_it(db, config, provider):
    """`pushups` is added as a REAL custom habit (so creation resolves it
    against the OWNER's own live per-user registry, exactly like `execute_
    routine` does in production), then archived (it already has history
    from the routine run below would create otherwise) -- R-R3's own
    "skip and note" behavior for a since-archived item."""
    db.add_user_habit(
        OWNER,
        {"id": "pushups", "type": "numeric", "label_en": "pushups", "label_th": "วิดพื้น", "unit_en": "reps", "unit_th": "ครั้ง"},
    )
    provider.invalidate(OWNER)
    channel = FakeChannel()
    create_cmd = commands.dispatch("/routine morning = water 500, pushups 20", provider.for_user(OWNER))
    await routines.execute_routine(
        create_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )

    db.archive_user_habit(OWNER, "pushups")  # simulate the habit being removed later
    provider.invalidate(OWNER)

    run_cmd = commands.dispatch("/routine morning", BASE_REGISTRY)
    reply = await routines.execute_routine(
        run_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER,
        clock=_fixed_clock(),
    )
    assert "1 of 2" in reply
    assert "pushups" in reply  # noted as skipped
    assert "(removed)" in reply


@pytest.mark.asyncio
async def test_execute_routine_run_all_invalid_no_dashboard_churn(db, config, provider):
    db.add_user_habit(OWNER, {"id": "ghost", "type": "numeric", "label_en": "ghost", "label_th": "ผี", "unit_en": "u", "unit_th": "ห"})
    provider.invalidate(OWNER)
    channel = FakeChannel()
    create_cmd = commands.dispatch("/routine morning = ghost 1", provider.for_user(OWNER))
    await routines.execute_routine(
        create_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )

    db.delete_user_habit(OWNER, "ghost")  # hard-delete: no logs exist yet for it
    provider.invalidate(OWNER)
    db.set_dashboard_msg_id(OWNER, "42")  # enable a live dashboard, so a refresh would be observable

    run_cmd = commands.dispatch("/routine morning", BASE_REGISTRY)
    reply = await routines.execute_routine(
        run_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER,
        clock=_fixed_clock(),
    )
    assert "nothing to log" in reply.lower()
    today = _fixed_clock()().date().isoformat()
    assert db.sum_value(OWNER, "ghost", today) == 0.0  # nothing logged
    # No dashboard edit attempted -- FakeChannel has no edit_message
    # override, so a stray call would raise NotImplementedError via the
    # base ABC default (which just no-ops send) -- verified indirectly by
    # this test not raising and the message id being untouched.
    assert db.get_dashboard_msg_id(OWNER) == "42"


@pytest.mark.asyncio
async def test_execute_routine_run_not_found(db, config, provider):
    channel = FakeChannel()
    run_cmd = commands.dispatch("/routine ghost", BASE_REGISTRY)
    reply = await routines.execute_routine(
        run_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    assert "No routine named" in reply


@pytest.mark.asyncio
async def test_execute_routine_run_suppresses_celebration_but_updates_records(db, config, provider):
    """R-R3: `records.update_on_log`'s return is discarded -- no
    `record_broken_*` celebration text appears in the run summary -- but
    the stored record itself still reflects the new value."""
    channel = FakeChannel()
    create_cmd = commands.dispatch(CREATE_EXAMPLE, BASE_REGISTRY)
    await routines.execute_routine(
        create_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    # Seed an existing (lower) best_day record for water so this run's log
    # strictly exceeds it -- a genuine "break", which would normally emit
    # a celebration line outside a routine run.
    db.upsert_record(OWNER, "water", "best_day", 1.0, "2026-08-01")

    clock = _fixed_clock()
    run_cmd = commands.dispatch("/routine morning", BASE_REGISTRY)
    reply = await routines.execute_routine(
        run_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER, clock=clock
    )
    assert "record" not in reply.lower()
    assert db.get_record(OWNER, "water", "best_day") == 500.0  # still recomputed silently


# ===========================================================================
# execute_routine -- delete (AC-B4).
# ===========================================================================


@pytest.mark.asyncio
async def test_execute_routine_delete_success(db, config, provider):
    channel = FakeChannel()
    create_cmd = commands.dispatch(CREATE_EXAMPLE, BASE_REGISTRY)
    await routines.execute_routine(
        create_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    delete_cmd = commands.dispatch("/routine delete morning", BASE_REGISTRY)
    reply = await routines.execute_routine(
        delete_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    assert "Deleted" in reply
    assert db.get_routine(OWNER, "morning") is None
    assert db.count_routines(OWNER) == 0

    audit_row = db.recent_audit(1)[0]
    assert audit_row["action"] == "routine_delete"


@pytest.mark.asyncio
async def test_execute_routine_delete_not_found_no_write(db, config, provider):
    channel = FakeChannel()
    delete_cmd = commands.dispatch("/routine delete ghost", BASE_REGISTRY)
    reply = await routines.execute_routine(
        delete_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    assert "No routine named" in reply
    assert not any(row["action"] == "routine_delete" for row in db.recent_audit(50))


# ===========================================================================
# Isolation (AC-B5).
# ===========================================================================


@pytest.mark.asyncio
async def test_isolation_user_b_cannot_see_or_run_user_a_routine(db, config, provider):
    channel = FakeChannel()
    create_cmd = commands.dispatch(CREATE_EXAMPLE, BASE_REGISTRY)
    await routines.execute_routine(
        create_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )

    assert db.get_routine(MEMBER, "morning") is None

    run_cmd = commands.dispatch("/routine morning", BASE_REGISTRY)
    reply = await routines.execute_routine(
        run_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=MEMBER
    )
    assert "No routine named" in reply


@pytest.mark.asyncio
async def test_handle_routine_callback_malformed_data_ignored(db, config, provider):
    channel = FakeChannel()
    await routines.handle_routine_callback(
        OWNER, "routine:run:not valid!", "text", "cb1", db=db, channel=channel, config=config, provider=provider
    )
    assert channel.sent == []


@pytest.mark.asyncio
async def test_handle_routine_callback_not_owned_is_friendly_noop(db, config, provider):
    channel = FakeChannel()
    create_cmd = commands.dispatch(CREATE_EXAMPLE, BASE_REGISTRY)
    await routines.execute_routine(
        create_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )

    await routines.handle_routine_callback(
        MEMBER, "routine:run:morning", "text", "cb1", db=db, channel=channel, config=config, provider=provider
    )
    assert len(channel.sent) == 1
    assert "No routine named" in channel.sent[0][1]
    # Nothing was logged for MEMBER.
    assert db.sum_value(MEMBER, "water", datetime.now().date().isoformat()) == 0.0


@pytest.mark.asyncio
async def test_handle_routine_callback_runs_owned_routine(db, config, provider):
    channel = FakeChannel()
    create_cmd = commands.dispatch(CREATE_EXAMPLE, BASE_REGISTRY)
    await routines.execute_routine(
        create_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    channel.sent.clear()

    clock = _fixed_clock()
    await routines.handle_routine_callback(
        OWNER, "routine:run:morning", "text", "cb1", db=db, channel=channel, config=config, provider=provider,
        clock=clock,
    )
    assert len(channel.sent) == 1
    assert channel.sent[0][0] == OWNER
    assert "2 of 2" in channel.sent[0][1]
    today = clock().date().isoformat()
    assert db.sum_value(OWNER, "water", today) == 500.0


# ===========================================================================
# Migration 011 (AC-B6).
# ===========================================================================


def test_migration_011_creates_tables_idempotently(tmp_path):
    db_ = Database(tmp_path / "mig011.db")
    assert db_.schema_version >= 11
    tables = {r[0] for r in db_._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "routines" in tables
    assert "routine_items" in tables
    assert db_.count_routines("u1") == 0
    db_.close()

    # Reopening (re-running migrations) applies nothing further.
    reopened = Database(tmp_path / "mig011.db")
    assert reopened.schema_version_before == reopened.schema_version
    reopened.close()


def test_migration_011_touches_no_existing_data(tmp_path):
    """Hand-build a v10-shaped DB (migrations 001-010 already applied)
    with a real user + log row, then open it through the real `Database`
    (which runs every pending migration, including 011) -- proves 011 is
    purely additive."""
    db_path = tmp_path / "v10_copy.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE logs (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          ts          TEXT NOT NULL,
          category    TEXT NOT NULL,
          value_num   REAL,
          value_text  TEXT,
          raw_message TEXT NOT NULL,
          source      TEXT NOT NULL DEFAULT 'reply',
          created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          deleted_at  TEXT NULL,
          habit_type  TEXT NULL,
          user_id     TEXT NULL
        );
        CREATE TABLE users (
          chat_id                 TEXT PRIMARY KEY,
          role                    TEXT NOT NULL DEFAULT 'member',
          status                  TEXT NOT NULL DEFAULT 'pending',
          display_name            TEXT,
          language_pref           TEXT NOT NULL DEFAULT 'auto',
          quiet_hours_json        TEXT,
          snooze_default_minutes  INTEGER,
          checkin_window          TEXT NULL,
          last_announced_version  TEXT NULL,
          dashboard_msg_id        TEXT NULL,
          created_at              TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        INSERT INTO users (chat_id, role, status) VALUES ('legacy-owner', 'owner', 'active');
        INSERT INTO logs (ts, category, value_num, raw_message, user_id)
          VALUES ('2026-01-01T09:00:00', 'water', 500.0, '500ml', 'legacy-owner');
        """
    )
    conn.execute("PRAGMA user_version = 10")
    conn.commit()
    conn.close()

    db_ = Database(db_path)
    assert db_.schema_version == 13
    assert db_.schema_version_before == 10

    row = db_._conn.execute("SELECT * FROM logs WHERE category = 'water'").fetchone()
    assert row["value_num"] == 500.0
    assert row["user_id"] == "legacy-owner"
    user_row = db_.get_user("legacy-owner")
    assert user_row["role"] == "owner"
    assert db_.count_routines("legacy-owner") == 0
    db_.close()


# ===========================================================================
# Zero-LLM (AC-B7).
# ===========================================================================


def test_routines_module_never_imports_the_llm_client():
    import ast

    import habit_assistant.core.routines as routines_module

    tree = ast.parse(open(routines_module.__file__, encoding="utf-8").read())
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    assert not any("ollama" in mod.lower() or "llm" in mod.lower() for mod in imported_modules)
