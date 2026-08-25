"""Vera's adversarial gap-fill suite for `core/routines.py` (SPEC-v1.8.md
S4 "Feature -- routines / habit stacks (module `routines`)", R-R1-R-R6),
on top of Luna's own `tests/test_routines.py` (43 tests, already reviewed
and green).

Scope: module-level only, per the v1.8 parallel-module split -- `main.py`
wiring (routing the "routine" `CommandKind`, dispatching `routine:`
callbacks through the real Telegram channel) is the later integration
pass and is NOT exercised here; every call below goes straight through
`commands.dispatch` / `routines.execute_routine` / `routines.
handle_routine_callback` against a real on-disk SQLite `Database` and a
real `RegistryProvider`, no mocks for the DB. See TEST-v1.8-routines.md
for the AC boundary.

Six groups, mirroring the dispatch brief:
1. Create validation (AC-B1) -- every failure leg leaves NO rows.
2. Create success -- ordered items, fail-open audit, bilingual confirmation.
3. List (AC-B2) -- per-user, run-button payload size, render-budget discipline.
4. Run (AC-B3) -- one summary, one dashboard refresh, no celebration text
   but records ARE updated, all-invalid no-op.
5. Delete (AC-B4) -- removes routine + items, no orphans, friendly no-op.
6. Isolation (AC-B5), migration idempotency (AC-B6), zero-LLM (AC-B7),
   and `_match_routine` Thai zero-false-positive corpus + the "delete" as a
   routine name coherence check.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from habit_assistant.channels.base import Button, Channel
from habit_assistant.config import Config
from habit_assistant.core import audit, commands, dashboard as dashboard_module, routines
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.registry_provider import RegistryProvider
from habit_assistant.storage import migrations as migrations_module
from habit_assistant.storage.db import Database

OWNER = "vera-owner"
MEMBER = "vera-member-b"

BASE_REGISTRY = HabitRegistry.from_config(Config())


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.actionable: list[tuple[str, str, list[Button]]] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append((chat_id, text))

    async def send_actionable(self, chat_id: str, text: str, buttons: list[Button]) -> None:
        self.actionable.append((chat_id, text, buttons))

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "vera_routines.db")
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


def _fixed_clock(y=2026, m=8, d=25, hh=9, mm=0):
    return lambda: datetime(y, m, d, hh, mm, 0)


def _routine_item_rows(db, user_id, name):
    return db._conn.execute(
        "SELECT * FROM routine_items WHERE user_id = ? AND name = ?", (user_id, name)
    ).fetchall()


def _log_row_count(db, user_id):
    row = db._conn.execute("SELECT COUNT(*) AS n FROM logs WHERE user_id = ?", (user_id,)).fetchone()
    return int(row["n"])


async def _create(cmd, *, db, channel, config, provider, lang="en", user_id=OWNER):
    return await routines.execute_routine(
        cmd, db=db, channel=channel, config=config, provider=provider, lang=lang, user_id=user_id
    )


# ===========================================================================
# 1. Create validation (AC-B1) -- every failure leg leaves NO rows.
# ===========================================================================


@pytest.mark.asyncio
async def test_create_name_uppercase_is_normalized_and_succeeds(db, config, provider):
    """R-R1: name normalization includes lowercasing -- an uppercase name
    is NOT rejected, it's folded to lowercase before validation."""
    cmd = commands.dispatch("/routine MORNING = water 500", BASE_REGISTRY)
    channel = FakeChannel()
    reply = await _create(cmd, db=db, channel=channel, config=config, provider=provider)
    assert "morning" in reply.lower()
    assert db.get_routine(OWNER, "morning") == [("water", 500.0)]
    assert db.count_routines(OWNER) == 1


@pytest.mark.asyncio
async def test_create_name_exactly_32_chars_allowed(db, config, provider):
    name = "a" * 32
    cmd = commands.dispatch(f"/routine {name} = water 500", BASE_REGISTRY)
    channel = FakeChannel()
    reply = await _create(cmd, db=db, channel=channel, config=config, provider=provider)
    assert db.get_routine(OWNER, name) is not None
    assert db.count_routines(OWNER) == 1


@pytest.mark.asyncio
async def test_create_name_33_chars_rejected_no_write(db, config, provider):
    name = "a" * 33
    cmd = commands.dispatch(f"/routine {name} = water 500", BASE_REGISTRY)
    channel = FakeChannel()
    reply = await _create(cmd, db=db, channel=channel, config=config, provider=provider)
    assert "Couldn't save" in reply
    assert db.count_routines(OWNER) == 0
    assert _log_row_count(db, OWNER) == 0


@pytest.mark.asyncio
async def test_create_name_thai_chars_rejected_no_write(db, config, provider):
    # Thai script can't match the ASCII ^[a-z0-9_]+$ shape at all, so the
    # slash form's own name-token regex (\S+?) still captures it -- the
    # rejection happens in execute_routine's _name_valid check.
    cmd = commands.dispatch("/routine เช้า = water 500", BASE_REGISTRY)
    channel = FakeChannel()
    reply = await _create(cmd, db=db, channel=channel, config=config, provider=provider)
    assert "Couldn't save" in reply
    assert db.count_routines(OWNER) == 0


@pytest.mark.asyncio
async def test_create_name_with_embedded_space_does_not_dispatch_as_routine(db, config, provider):
    """A space inside the name token breaks BOTH the create and run slash
    regexes (name is \\S+), so the whole message fails to dispatch as a
    routine command at all -- it falls through to `None` (the general
    log/LLM path), not into execute_routine's validation. Documented here
    as the actual, spec-consistent behavior -- no routine row is ever
    created either way."""
    cmd = commands.dispatch("/routine morning time = water 500", BASE_REGISTRY)
    assert cmd is None
    assert db.count_routines(OWNER) == 0


@pytest.mark.asyncio
async def test_create_name_delete_is_a_legal_name_but_bare_delete_runs_it(db, config, provider):
    """R-R1 places no reserved-word restriction on routine NAMES (unlike
    R-S5's reserved_trigger_words(), which only governs custom HABIT ids).
    A routine can legitimately be named "delete" via
    "/routine delete = <items>" (the create regex, tried after the delete
    regex, wins because the delete regex requires end-of-string right
    after a lone name token). Once such a routine exists, bare
    "/routine delete" (no further argument) falls through delete's own
    regex (which requires a name after "delete") into the RUN regex,
    treating "delete" as a routine NAME to run -- not an error about a
    missing delete argument. This is coherent with the letter of the spec
    (R-R1 doesn't reserve routine names) but is a real UX sharp edge worth
    flagging to Archi/Luna."""
    channel = FakeChannel()
    create_cmd = commands.dispatch("/routine delete = water 500", BASE_REGISTRY)
    assert create_cmd.routine_action == "create"
    assert create_cmd.routine_name == "delete"
    reply = await _create(create_cmd, db=db, channel=channel, config=config, provider=provider)
    assert "delete" in reply.lower()
    assert db.get_routine(OWNER, "delete") == [("water", 500.0)]

    # Bare "/routine delete" now RUNS the "delete" routine instead of
    # producing a delete-usage error.
    run_cmd = commands.dispatch("/routine delete", BASE_REGISTRY)
    assert run_cmd.routine_action == "run"
    assert run_cmd.routine_name == "delete"
    reply2 = await _create(run_cmd, db=db, channel=channel, config=config, provider=provider, user_id=OWNER)
    assert "1 of 1" in reply2
    assert db.get_routine(OWNER, "delete") is not None  # still exists -- run, not delete

    # "/routine delete delete" (explicit name) DOES delete it.
    delete_cmd = commands.dispatch("/routine delete delete", BASE_REGISTRY)
    assert delete_cmd.routine_action == "delete"
    assert delete_cmd.routine_name == "delete"
    reply3 = await _create(delete_cmd, db=db, channel=channel, config=config, provider=provider)
    assert "Deleted" in reply3
    assert db.get_routine(OWNER, "delete") is None


@pytest.mark.asyncio
async def test_create_zero_items_after_equals_no_write(db, config, provider):
    """REGRESSION GUARD, FIXED (was FINDING 1, dispatch-layer gap, AC-B1's
    "empty items" leg -- see TEST-v1.8-routines.md): a completely bare
    "/routine <name> = " (nothing at all after "=") used to NOT match
    `_ROUTINE_SLASH_CREATE_RE` at all -- its `items` group was `.+`
    (required >=1 char), so `commands.dispatch` returned `None` outright,
    meaning the message never reached `execute_routine` and the user got
    NO routine-specific "usage"/"empty items" reply (it silently fell
    through to the general log/LLM path instead).

    Archi-directed integration-pass fix (`core/commands.py`'s
    `_ROUTINE_SLASH_CREATE_RE`/`_ROUTINE_TH_CREATE_RE`, `.+` -> `.*`,
    per this finding's own "suggested fix" text): a fully bare tail now
    DOES dispatch, with `routine_items=None` (`_parse_routine_items("")`
    already returned `None` for an all-empty items string -- the same
    "no non-empty segments" branch a bare habit-token-with-no-value tail
    already hit), so it reaches the SAME `execute_routine`'s friendly
    `routine_create_usage` message "/routine morning = water" (habit
    token, no value) already produced. "No write" holds either way."""
    cmd = commands.dispatch("/routine morning = ", BASE_REGISTRY)
    assert cmd is not None
    assert cmd.kind == "routine"
    assert cmd.routine_action == "create"
    assert cmd.routine_name == "morning"
    assert cmd.routine_items is None
    channel = FakeChannel()
    reply = await _create(cmd, db=db, channel=channel, config=config, provider=provider)
    assert "/routine <name> = <habit> <value>" in reply  # routine_create_usage, not silence/fallthrough
    assert db.count_routines(OWNER) == 0


@pytest.mark.asyncio
async def test_create_partial_valid_items_leaves_no_partial_write(db, config, provider):
    """First item resolves fine, second is an unknown habit -- the whole
    create must be atomic: no half-written routine."""
    cmd = commands.dispatch("/routine morning = water 500, coffee 2", BASE_REGISTRY)
    channel = FakeChannel()
    reply = await _create(cmd, db=db, channel=channel, config=config, provider=provider)
    assert '"coffee"' in reply
    assert db.count_routines(OWNER) == 0
    assert _routine_item_rows(db, OWNER, "morning") == []


@pytest.mark.asyncio
async def test_create_negative_value_rejected_no_write(db, config, provider):
    cmd = commands.dispatch("/routine morning = water -5", BASE_REGISTRY)
    channel = FakeChannel()
    reply = await _create(cmd, db=db, channel=channel, config=config, provider=provider)
    assert "Couldn't save" in reply
    assert db.count_routines(OWNER) == 0


@pytest.mark.asyncio
async def test_create_zero_value_rejected_no_write(db, config, provider):
    cmd = commands.dispatch("/routine morning = water 0", BASE_REGISTRY)
    channel = FakeChannel()
    reply = await _create(cmd, db=db, channel=channel, config=config, provider=provider)
    assert "Couldn't save" in reply
    assert db.count_routines(OWNER) == 0


@pytest.mark.asyncio
async def test_create_unparseable_value_variants_all_rejected_no_write(db, config, provider):
    for tail in ["water lots", "water -", "water ", "water abc"]:
        cmd = commands.dispatch(f"/routine morning = {tail}", BASE_REGISTRY)
        channel = FakeChannel()
        reply = await _create(cmd, db=db, channel=channel, config=config, provider=provider)
        assert db.count_routines(OWNER) == 0, f"tail={tail!r} left a row"


@pytest.mark.asyncio
async def test_create_huge_value_with_no_unit_is_accepted(db, config, provider):
    """No explicit magnitude cap in R-R1 -- a very large bare number (no
    unit token) is a legal positive NUMBER and is accepted."""
    cmd = commands.dispatch("/routine morning = water 999999999", BASE_REGISTRY)
    channel = FakeChannel()
    reply = await _create(cmd, db=db, channel=channel, config=config, provider=provider)
    assert db.get_routine(OWNER, "morning") == [("water", 999999999.0)]


@pytest.mark.asyncio
async def test_create_scientific_notation_value_rejected_as_unresolvable_unit(db, config, provider):
    """VALUE_RE has no exponent support -- "1e20" parses as num=1,
    unit="e20", which doesn't resolve -> routine_invalid_value, no write.
    Documents the actual (conservative) behavior rather than silently
    misinterpreting scientific notation."""
    cmd = commands.dispatch("/routine morning = water 1e20", BASE_REGISTRY)
    channel = FakeChannel()
    reply = await _create(cmd, db=db, channel=channel, config=config, provider=provider)
    assert "Couldn't save" in reply
    assert db.count_routines(OWNER) == 0


@pytest.mark.asyncio
async def test_create_colliding_unit_token_rejected_no_write(db, config, provider):
    """SPEC-v1.5.md's own R-L rule (reused verbatim by units.build_unit_lookup):
    a unit token claimed by two DIFFERENT habits is EXCLUDED from the
    lookup entirely, not "first one wins". A routine item using such a
    colliding token must fail validation (zero-LLM here -- there is no
    fallback path the way preparse has one), leaving no write."""
    db.add_user_habit(
        OWNER,
        {
            "id": "juice", "type": "numeric", "label_en": "juice", "label_th": "น้ำผลไม้",
            "unit_en": "ml", "unit_th": "มล.", "unit_aliases": json.dumps({"box": 200.0}),
        },
    )
    db.add_user_habit(
        OWNER,
        {
            "id": "soup", "type": "numeric", "label_en": "soup", "label_th": "ซุป",
            "unit_en": "ml", "unit_th": "มล.", "unit_aliases": json.dumps({"box": 300.0}),
        },
    )
    provider.invalidate(OWNER)
    registry = provider.for_user(OWNER)
    cmd = commands.dispatch("/routine morning = juice 1 box", registry)
    channel = FakeChannel()
    reply = await _create(cmd, db=db, channel=channel, config=config, provider=provider)
    assert "Couldn't save" in reply
    assert db.count_routines(OWNER) == 0


@pytest.mark.asyncio
async def test_create_cap_exactly_max_allowed_then_plus_one_rejected(db, config, provider):
    config.routines.max_per_user = 3
    channel = FakeChannel()
    for i in range(3):
        cmd = commands.dispatch(f"/routine r{i} = water {i + 1}", BASE_REGISTRY)
        reply = await _create(cmd, db=db, channel=channel, config=config, provider=provider)
        assert db.get_routine(OWNER, f"r{i}") is not None
    assert db.count_routines(OWNER) == 3

    over_cmd = commands.dispatch("/routine r3 = water 1", BASE_REGISTRY)
    reply = await _create(over_cmd, db=db, channel=channel, config=config, provider=provider)
    assert "limit" in reply.lower()
    assert db.count_routines(OWNER) == 3
    assert db.get_routine(OWNER, "r3") is None


@pytest.mark.asyncio
async def test_create_item_referencing_another_users_custom_habit_rejected(db, config, provider):
    """Per-user registry isolation: a custom habit MEMBER owns is simply
    not present in OWNER's own registry, so it's indistinguishable from an
    unknown habit token when OWNER tries to reference it."""
    db.add_user_habit(
        MEMBER, {"id": "yoga", "type": "numeric", "label_en": "yoga", "label_th": "โยคะ", "unit_en": "min", "unit_th": "นาที"}
    )
    provider.invalidate(MEMBER)
    cmd = commands.dispatch("/routine morning = yoga 10", BASE_REGISTRY)
    channel = FakeChannel()
    reply = await _create(cmd, db=db, channel=channel, config=config, provider=provider, user_id=OWNER)
    assert '"yoga"' in reply
    assert db.count_routines(OWNER) == 0


@pytest.mark.asyncio
async def test_create_referencing_habit_archived_before_creation_rejected(db, config, provider):
    """A habit archived BEFORE the routine is ever created is absent from
    the active registry -> unknown-habit rejection at create time (distinct
    from Luna's own "archived AFTER creation, skipped at run time" test)."""
    db.add_user_habit(
        OWNER, {"id": "pushups", "type": "numeric", "label_en": "pushups", "label_th": "วิดพื้น", "unit_en": "reps", "unit_th": "ครั้ง"}
    )
    provider.invalidate(OWNER)
    db.archive_user_habit(OWNER, "pushups")
    provider.invalidate(OWNER)
    registry = provider.for_user(OWNER)
    cmd = commands.dispatch("/routine morning = pushups 20", registry)
    channel = FakeChannel()
    reply = await _create(cmd, db=db, channel=channel, config=config, provider=provider)
    assert '"pushups"' in reply
    assert db.count_routines(OWNER) == 0


# ===========================================================================
# 2. Create success.
# ===========================================================================


@pytest.mark.asyncio
async def test_create_success_ordered_items_preserved_by_seq(db, config, provider):
    db.add_user_habit(
        OWNER, {"id": "pushups", "type": "numeric", "label_en": "pushups", "label_th": "วิดพื้น", "unit_en": "reps", "unit_th": "ครั้ง"}
    )
    provider.invalidate(OWNER)
    registry = provider.for_user(OWNER)
    cmd = commands.dispatch("/routine morning = stretch 10, water 500, pushups 20, diary 1", registry)
    channel = FakeChannel()
    await _create(cmd, db=db, channel=channel, config=config, provider=provider)
    stored = db.get_routine(OWNER, "morning")
    assert [h for h, _ in stored] == ["stretch", "water", "pushups", "diary"]
    rows = _routine_item_rows(db, OWNER, "morning")
    seqs = sorted(row["seq"] for row in rows)
    assert seqs == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_create_audit_write_is_fail_open_and_does_not_block_creation(db, config, provider, monkeypatch):
    """R-R1: 'record `routine_create` (fail-open)' -- a broken audit write
    must not prevent the routine from being saved."""
    def _boom(entry):
        raise sqlite3.OperationalError("simulated broken audit table")

    monkeypatch.setattr(db, "insert_audit", _boom)
    cmd = commands.dispatch("/routine morning = water 500", BASE_REGISTRY)
    channel = FakeChannel()
    reply = await _create(cmd, db=db, channel=channel, config=config, provider=provider)
    assert db.get_routine(OWNER, "morning") == [("water", 500.0)]
    assert db.count_routines(OWNER) == 1
    assert "morning" in reply.lower()


@pytest.mark.asyncio
async def test_create_confirmation_shape_en_and_th(db, config, provider):
    cmd_en = commands.dispatch("/routine morning = water 500, stretch 10", BASE_REGISTRY)
    channel = FakeChannel()
    reply_en = await _create(cmd_en, db=db, channel=channel, config=config, provider=provider, lang="en")
    assert reply_en.startswith("✅")
    assert "morning" in reply_en
    assert "500" in reply_en and "10" in reply_en
    assert "/routine morning" in reply_en

    cmd_th = commands.dispatch("/routine evening = water 300", BASE_REGISTRY)
    reply_th = await _create(cmd_th, db=db, channel=channel, config=config, provider=provider, lang="th")
    assert reply_th.startswith("✅")
    assert "กิจวัตร" in reply_th
    assert "evening" in reply_th


# ===========================================================================
# 3. List (AC-B2).
# ===========================================================================


@pytest.mark.asyncio
async def test_list_shows_only_the_acting_users_routines(db, config, provider):
    channel = FakeChannel()
    a_cmd = commands.dispatch("/routine morning = water 500", BASE_REGISTRY)
    await _create(a_cmd, db=db, channel=channel, config=config, provider=provider, user_id=OWNER)
    b_cmd = commands.dispatch("/routine noon = water 300", BASE_REGISTRY)
    await _create(b_cmd, db=db, channel=channel, config=config, provider=provider, user_id=MEMBER)

    list_cmd = commands.dispatch("/routine", BASE_REGISTRY)
    await routines.execute_routine(
        list_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    chat_id, text, buttons = channel.actionable[-1]
    assert chat_id == OWNER
    assert "morning" in text
    assert "noon" not in text
    assert {d for _, d in buttons} == {"routine:run:morning"}


@pytest.mark.asyncio
async def test_run_button_payload_fits_64_byte_telegram_limit_for_32_char_name(db, config, provider):
    name = "z" * 32
    cmd = commands.dispatch(f"/routine {name} = water 500", BASE_REGISTRY)
    channel = FakeChannel()
    await _create(cmd, db=db, channel=channel, config=config, provider=provider)

    list_cmd = commands.dispatch("/routine", BASE_REGISTRY)
    await routines.execute_routine(
        list_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    _, _, buttons = channel.actionable[-1]
    (label, payload), = buttons
    assert payload == f"routine:run:{name}"
    assert len(payload.encode("utf-8")) <= 64


@pytest.mark.asyncio
async def test_list_render_budget_discipline_for_20_routines_many_items(db, config, provider):
    """A user with `max_per_user` (20) routines, each with several items,
    must never produce a >4096-char send -- and the returned buttons stay
    in lockstep with whatever lines actually got kept."""
    channel = FakeChannel()
    for i in range(20):
        items = ", ".join(f"{h} {v}" for h, v in [("water", 100 + i), ("stretch", 5 + i), ("diary", 1)])
        cmd = commands.dispatch(f"/routine r{i:02d} = {items}", BASE_REGISTRY)
        reply = await _create(cmd, db=db, channel=channel, config=config, provider=provider)
        assert db.get_routine(OWNER, f"r{i:02d}") is not None, reply

    list_cmd = commands.dispatch("/routine", BASE_REGISTRY)
    await routines.execute_routine(
        list_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    _, text, buttons = channel.actionable[-1]
    assert len(text) <= 4096
    # Every button that made it in corresponds to a routine name actually
    # present in the rendered text (lockstep, no orphan buttons).
    for _, payload in buttons:
        name = payload.removeprefix("routine:run:")
        assert name in text


# ===========================================================================
# 4. Run (AC-B3).
# ===========================================================================


@pytest.mark.asyncio
async def test_run_all_valid_logs_every_item_for_today_for_acting_user(db, config, provider):
    db.add_user_habit(
        OWNER, {"id": "pushups", "type": "numeric", "label_en": "pushups", "label_th": "วิดพื้น", "unit_en": "reps", "unit_th": "ครั้ง"}
    )
    provider.invalidate(OWNER)
    registry = provider.for_user(OWNER)
    channel = FakeChannel()
    create_cmd = commands.dispatch("/routine full = water 500, stretch 10, pushups 20", registry)
    await _create(create_cmd, db=db, channel=channel, config=config, provider=provider)

    clock = _fixed_clock()
    run_cmd = commands.dispatch("/routine full", BASE_REGISTRY)
    reply = await routines.execute_routine(
        run_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER, clock=clock
    )
    assert "3 of 3" in reply
    today = clock().date().isoformat()
    assert db.sum_value(OWNER, "water", today) == 500.0
    assert db.sum_value(OWNER, "stretch", today) == 10.0
    assert db.sum_value(OWNER, "pushups", today) == 20.0


@pytest.mark.asyncio
async def test_run_text_habit_item_is_skipped_and_noted(db, config, provider):
    """`diary` is a text habit -- R-R3: "text item -> skipped, can't
    carry free text"."""
    channel = FakeChannel()
    create_cmd = commands.dispatch("/routine morning = water 500, diary hello", BASE_REGISTRY)
    await _create(create_cmd, db=db, channel=channel, config=config, provider=provider)

    clock = _fixed_clock()
    run_cmd = commands.dispatch("/routine morning", BASE_REGISTRY)
    reply = await routines.execute_routine(
        run_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER, clock=clock
    )
    assert "1 of 2" in reply
    assert "can't auto-log text" in reply
    today = clock().date().isoformat()
    assert db.sum_value(OWNER, "water", today) == 500.0
    assert _log_row_count(db, OWNER) == 1  # only water was ever logged


@pytest.mark.asyncio
async def test_run_calls_dashboard_refresh_exactly_once(db, config, provider, monkeypatch):
    calls: list[tuple] = []

    async def _fake_refresh(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(dashboard_module, "refresh", _fake_refresh)

    channel = FakeChannel()
    create_cmd = commands.dispatch("/routine morning = water 500, stretch 10", BASE_REGISTRY)
    await _create(create_cmd, db=db, channel=channel, config=config, provider=provider)

    run_cmd = commands.dispatch("/routine morning", BASE_REGISTRY)
    await routines.execute_routine(
        run_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER,
        clock=_fixed_clock(),
    )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_run_all_invalid_zero_dashboard_calls_and_zero_log_rows(db, config, provider, monkeypatch):
    calls: list[tuple] = []

    async def _fake_refresh(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(dashboard_module, "refresh", _fake_refresh)

    db.add_user_habit(OWNER, {"id": "ghost", "type": "numeric", "label_en": "ghost", "label_th": "ผี", "unit_en": "u", "unit_th": "ห"})
    provider.invalidate(OWNER)
    channel = FakeChannel()
    create_cmd = commands.dispatch("/routine morning = ghost 1", provider.for_user(OWNER))
    await _create(create_cmd, db=db, channel=channel, config=config, provider=provider)

    db.delete_user_habit(OWNER, "ghost")
    provider.invalidate(OWNER)

    run_cmd = commands.dispatch("/routine morning", BASE_REGISTRY)
    reply = await routines.execute_routine(
        run_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER,
        clock=_fixed_clock(),
    )
    assert "nothing to log" in reply.lower()
    assert calls == []
    assert _log_row_count(db, OWNER) == 0


@pytest.mark.asyncio
async def test_run_suppresses_celebration_text_but_record_row_is_updated(db, config, provider):
    channel = FakeChannel()
    create_cmd = commands.dispatch("/routine morning = water 500", BASE_REGISTRY)
    await _create(create_cmd, db=db, channel=channel, config=config, provider=provider)
    db.upsert_record(OWNER, "water", "best_day", 1.0, "2026-08-01")
    assert db.get_record(OWNER, "water", "best_day") == 1.0

    clock = _fixed_clock()
    run_cmd = commands.dispatch("/routine morning", BASE_REGISTRY)
    reply = await routines.execute_routine(
        run_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER, clock=clock
    )
    for banned in ("record", "🏆", "milestone", "personal best"):
        assert banned.lower() not in reply.lower()
    # The stored record strictly increased -- update_on_log's write DID
    # happen, only its celebration-text return value was discarded.
    assert db.get_record(OWNER, "water", "best_day") == 500.0


@pytest.mark.asyncio
async def test_run_audit_row_recorded_with_logged_count(db, config, provider):
    channel = FakeChannel()
    create_cmd = commands.dispatch("/routine morning = water 500, stretch 10", BASE_REGISTRY)
    await _create(create_cmd, db=db, channel=channel, config=config, provider=provider)

    run_cmd = commands.dispatch("/routine morning", BASE_REGISTRY)
    await routines.execute_routine(
        run_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER,
        clock=_fixed_clock(),
    )
    audit_rows = [r for r in db.recent_audit(10) if r["action"] == "routine_run"]
    assert len(audit_rows) == 1
    assert audit_rows[0]["entity"] == "morning"
    assert audit_rows[0]["new_value"] == "2"


# ===========================================================================
# 5. Delete (AC-B4).
# ===========================================================================


@pytest.mark.asyncio
async def test_delete_removes_routine_and_all_its_items_no_orphans(db, config, provider):
    channel = FakeChannel()
    create_cmd = commands.dispatch("/routine morning = water 500, stretch 10, diary 1", BASE_REGISTRY)
    await _create(create_cmd, db=db, channel=channel, config=config, provider=provider)
    assert len(_routine_item_rows(db, OWNER, "morning")) == 3

    delete_cmd = commands.dispatch("/routine delete morning", BASE_REGISTRY)
    reply = await routines.execute_routine(
        delete_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    assert "Deleted" in reply
    assert db.get_routine(OWNER, "morning") is None
    assert _routine_item_rows(db, OWNER, "morning") == []  # no orphan item rows


@pytest.mark.asyncio
async def test_delete_unknown_name_is_friendly_noop_no_write_no_audit(db, config, provider):
    channel = FakeChannel()
    delete_cmd = commands.dispatch("/routine delete ghost", BASE_REGISTRY)
    reply = await routines.execute_routine(
        delete_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    assert "No routine named" in reply
    assert db.count_routines(OWNER) == 0
    assert not any(r["action"] == "routine_delete" for r in db.recent_audit(50))


# ===========================================================================
# 6. Isolation (AC-B5), migration (AC-B6), zero-LLM (AC-B7), dispatch corpus.
# ===========================================================================


@pytest.mark.asyncio
async def test_isolation_handle_routine_callback_owned_by_a_tapped_from_b_zero_writes(db, config, provider):
    channel = FakeChannel()
    create_cmd = commands.dispatch("/routine morning = water 500", BASE_REGISTRY)
    await _create(create_cmd, db=db, channel=channel, config=config, provider=provider, user_id=OWNER)

    before_items_a = _routine_item_rows(db, OWNER, "morning")
    await routines.handle_routine_callback(
        MEMBER, "routine:run:morning", "text", "cb1", db=db, channel=channel, config=config, provider=provider
    )
    assert len(channel.sent) == 1
    assert "No routine named" in channel.sent[0][1]
    assert _log_row_count(db, MEMBER) == 0
    assert _routine_item_rows(db, OWNER, "morning") == before_items_a  # untouched
    assert db.get_routine(OWNER, "morning") is not None  # A's routine survives


@pytest.mark.asyncio
async def test_isolation_same_name_routines_for_two_users_run_independently(db, config, provider):
    channel = FakeChannel()
    a_create = commands.dispatch("/routine morning = water 500", BASE_REGISTRY)
    await _create(a_create, db=db, channel=channel, config=config, provider=provider, user_id=OWNER)
    b_create = commands.dispatch("/routine morning = stretch 15", BASE_REGISTRY)
    await _create(b_create, db=db, channel=channel, config=config, provider=provider, user_id=MEMBER)

    clock = _fixed_clock()
    run_cmd = commands.dispatch("/routine morning", BASE_REGISTRY)
    await routines.execute_routine(
        run_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER, clock=clock
    )
    await routines.execute_routine(
        run_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=MEMBER, clock=clock
    )
    today = clock().date().isoformat()
    assert db.sum_value(OWNER, "water", today) == 500.0
    assert db.sum_value(OWNER, "stretch", today) == 0.0
    assert db.sum_value(MEMBER, "stretch", today) == 15.0
    assert db.sum_value(MEMBER, "water", today) == 0.0


def test_migration_011_idempotent_across_two_explicit_runs(tmp_path):
    db_path = tmp_path / "double_mig.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    from_v, to_v = migrations_module.run_migrations(conn)
    assert to_v >= 11
    # Running again against the SAME already-migrated connection applies
    # nothing further and does not raise (CREATE TABLE IF NOT EXISTS).
    from_v2, to_v2 = migrations_module.run_migrations(conn)
    assert from_v2 == to_v2 == to_v
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "routines" in tables and "routine_items" in tables
    conn.close()


def test_migration_011_preexisting_v10_data_byte_identical_after_migration(tmp_path):
    """Build a v10-shaped DB with a real user + two log rows + a habit
    record, run every pending migration (including 011), and diff every
    field back -- proves 011 doesn't merely "not drop the table", it
    doesn't mutate a single existing value either."""
    db_path = tmp_path / "v10_diff.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, category TEXT NOT NULL,
          value_num REAL, value_text TEXT, raw_message TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT 'reply',
          created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          deleted_at TEXT NULL, habit_type TEXT NULL, user_id TEXT NULL
        );
        CREATE TABLE users (
          chat_id TEXT PRIMARY KEY, role TEXT NOT NULL DEFAULT 'member',
          status TEXT NOT NULL DEFAULT 'pending', display_name TEXT,
          language_pref TEXT NOT NULL DEFAULT 'auto', quiet_hours_json TEXT,
          snooze_default_minutes INTEGER, checkin_window TEXT NULL,
          last_announced_version TEXT NULL, dashboard_msg_id TEXT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE habit_records (
          user_id TEXT NOT NULL, habit_id TEXT NOT NULL, record_type TEXT NOT NULL,
          value REAL NOT NULL, achieved_on TEXT NOT NULL,
          PRIMARY KEY (user_id, habit_id, record_type)
        );
        INSERT INTO users (chat_id, role, status) VALUES ('legacy-owner', 'owner', 'active');
        INSERT INTO logs (ts, category, value_num, raw_message, user_id)
          VALUES ('2026-01-01T09:00:00', 'water', 500.0, '500ml', 'legacy-owner');
        INSERT INTO logs (ts, category, value_num, raw_message, user_id)
          VALUES ('2026-01-02T09:00:00', 'stretch', 15.0, 'stretch 15', 'legacy-owner');
        INSERT INTO habit_records (user_id, habit_id, record_type, value, achieved_on)
          VALUES ('legacy-owner', 'water', 'best_day', 500.0, '2026-01-01');
        """
    )
    conn.execute("PRAGMA user_version = 10")
    conn.commit()
    conn.close()

    before = {
        "logs": _dump_table(db_path, "logs"),
        "users": _dump_table(db_path, "users"),
        "habit_records": _dump_table(db_path, "habit_records"),
    }

    db_ = Database(db_path)
    assert db_.schema_version_before == 10
    assert db_.schema_version == 11
    db_.close()

    after = {
        "logs": _dump_table(db_path, "logs"),
        "users": _dump_table(db_path, "users"),
        "habit_records": _dump_table(db_path, "habit_records"),
    }
    assert before == after


def _dump_table(db_path, table):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
    conn.close()
    return rows


@pytest.mark.asyncio
async def test_routine_workflow_never_calls_the_ollama_client_network_method(db, config, provider, monkeypatch):
    """Complement to Luna's own AST-based check (which proves `routines.py`
    has no `import ollama`/`import llm` statement at all): this is a
    dynamic, BEHAVIORAL proof for the same claim -- poison
    `OllamaClient._post` (the one place every real Ollama network call in
    this codebase funnels through, per `llm/ollama_client.py`) so it
    raises if invoked, then drive create -> list -> run -> delete end to
    end. If any code path reachable from routines.py ever called into the
    LLM client (even indirectly, e.g. through a shared helper), this would
    raise; it doesn't, because no such path exists."""
    from habit_assistant.llm.ollama_client import OllamaClient

    async def _poisoned(self, *args, **kwargs):
        raise AssertionError("routines workflow must never call the Ollama client")

    monkeypatch.setattr(OllamaClient, "_post", _poisoned)

    channel = FakeChannel()
    create_cmd = commands.dispatch("/routine morning = water 500, stretch 10", BASE_REGISTRY)
    await _create(create_cmd, db=db, channel=channel, config=config, provider=provider)

    list_cmd = commands.dispatch("/routine", BASE_REGISTRY)
    await routines.execute_routine(
        list_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )

    run_cmd = commands.dispatch("/routine morning", BASE_REGISTRY)
    await routines.execute_routine(
        run_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER,
        clock=_fixed_clock(),
    )

    delete_cmd = commands.dispatch("/routine delete morning", BASE_REGISTRY)
    await routines.execute_routine(
        delete_cmd, db=db, channel=channel, config=config, provider=provider, lang="en", user_id=OWNER
    )
    # Reaching here without AssertionError proves zero LLM calls across the
    # entire create/list/run/delete workflow.


@pytest.mark.parametrize(
    "message",
    [
        "กิจวัตรวันนี้ดีมาก",  # glued prose, no leading match token possible
        "ฉันทำกิจวัตรตอนเช้าทุกวัน",  # mid-sentence prose
        "กิจวัตร",  # bare trigger word alone
        "กิจวัตร ตอนเช้า",  # spaced but tail isn't ASCII id-shaped
        "กิจวัตร ประจำวันคือน้ำ500",  # spaced Thai prose w/ digits glued in
        "my daily กิจวัตร is water",  # mixed-script sentence
        "routines are great for habits",  # ordinary English prose, plural
        "I love my morning routine so much",  # ordinary English prose
        "ROUTINE",  # bare uppercase English word, no slash
        "the routine = boring",  # contains "=" but not the slash form
        "/routine2 morning = water 500",  # similar-looking but different command
        "please /routine me an explanation",  # slash mid-sentence, not anchored
    ],
)
def test_match_routine_adversarial_corpus_extra_never_false_positives(message):
    assert commands.dispatch(message, BASE_REGISTRY) is None


def test_match_routine_thai_form_uppercase_latin_name_still_normalizes_correctly():
    """FINDING (informational, not a functional bug): the Thai-alias
    regexes (`_ROUTINE_TH_RUN_RE`/`_ROUTINE_TH_CREATE_RE`/
    `_ROUTINE_TH_DELETE_RE`) are compiled with `re.IGNORECASE`, which
    widens their `[a-z0-9_]+` name-shape class to also accept uppercase
    Latin letters -- e.g. "กิจวัตร Morning" dispatches with
    `routine_name="Morning"` rather than staying unmatched. This is a
    real deviation from the module's own documented invariant ("a routine
    name is BY DEFINITION restricted to ^[a-z0-9_]+$ (ASCII lower/
    digits/underscore)" -- core/commands.py's own comment above
    `_match_routine`). It is functionally harmless ONLY because
    `execute_routine` unconditionally re-normalizes via `_normalize_name`
    (`.strip().lower()`) before any DB lookup/write -- so
    "กิจวัตร Morning" and "กิจวัตร morning" resolve to the
    SAME routine. Documented here so Luna/Archi can decide whether to
    tighten the regex to `[a-z0-9_]+` without IGNORECASE (matching its own
    comment) as a follow-up -- not blocking, since no data-integrity or
    isolation issue results."""
    cmd = commands.dispatch("กิจวัตร Morning", BASE_REGISTRY)
    assert cmd is not None
    assert cmd.routine_name == "Morning"  # dispatch layer does NOT lowercase


def test_match_routine_bare_and_thai_forms_per_spec_23():
    assert commands.dispatch("/routine", BASE_REGISTRY).routine_action == "list"
    assert commands.dispatch("กิจวัตร morning = water 500", BASE_REGISTRY).routine_action == "create"
    assert commands.dispatch("กิจวัตร morning", BASE_REGISTRY).routine_action == "run"
    assert commands.dispatch("กิจวัตร morning ลบ", BASE_REGISTRY).routine_action == "delete"
