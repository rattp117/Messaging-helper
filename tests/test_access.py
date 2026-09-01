"""SPEC-v1.2.md §4 "Access control & onboarding (module `access`)" -- module
tests for the seven ACs this parallel module owns (SPEC-v1.2.md §11):
AC-A1, AC-A2, AC-A3, AC-A4, AC-A5, AC-A6, AC-A7.

This module is self-contained (does not touch `main.py` -- the access gate
is not yet wired into `on_message`, per SPEC-v1.2.md §11's integration
order), so tests exercise `core/access.py`'s public functions
(`classify`/`handle_gate`/`execute_admin`) and `core/commands.py:dispatch`'s
five new anchored kinds directly, against a real on-disk SQLite DB
(tmp_path) -- no mocks for the DB, mirroring tests/test_undo_ui.py's own
convention.
"""

from __future__ import annotations

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import access, commands, i18n
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.storage.db import Database

OWNER = "1574572064"
MEMBER = "88899900"
STRANGER = "55544433"


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str) -> str | None:
        self.sent.append((chat_id, text))
        # Integration item 4 (TEST-PORTAL-users.md Finding 1): a non-None
        # return signals a confirmed send, mirroring `LineChannel.send`'s
        # own updated contract -- this double always "succeeds", so it
        # always confirms.
        return "sent"

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")

    def sent_to(self, chat_id: str) -> list[str]:
        return [text for cid, text in self.sent if cid == chat_id]


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.attribute_legacy_to_owner(OWNER)
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def channel():
    return FakeChannel()


@pytest.fixture
def registry():
    return HabitRegistry.from_config(Config())


# ---------------------------------------------------------------------------
# commands.dispatch -- shape recognition for the five new kinds. Not itself
# an owned AC, but the seam AC-A1/AC-A4/AC-A5/AC-A6 build on.
# ---------------------------------------------------------------------------


def test_dispatch_start(registry):
    assert commands.dispatch("/start", registry) == commands.Command(kind="start")


def test_dispatch_users(registry):
    assert commands.dispatch("/users", registry) == commands.Command(kind="users")


def test_dispatch_approve_with_chat_id(registry):
    cmd = commands.dispatch("/approve 88899900", registry)
    assert cmd == commands.Command(kind="approve", target_chat="88899900")


def test_dispatch_approve_no_chat_id(registry):
    cmd = commands.dispatch("/approve", registry)
    assert cmd == commands.Command(kind="approve", target_chat=None)


def test_dispatch_block_with_chat_id(registry):
    cmd = commands.dispatch("/block 88899900", registry)
    assert cmd == commands.Command(kind="block", target_chat="88899900")


def test_dispatch_invite_with_chat_id(registry):
    cmd = commands.dispatch("/invite 88899900", registry)
    assert cmd == commands.Command(kind="invite", target_chat="88899900")


def test_dispatch_approve_captures_full_tail_not_just_first_token(registry):
    """Archi ruling (line/v1.1.0 readable-approval hardening, finding F4
    in TEST-LINE-1.1.0.md): SUPERSEDES this test's own original name/
    assertion -- /approve (and /block) now capture the FULL,
    outer-trimmed tail as `target_chat`, not just the first
    whitespace-delimited token. `core/access.py:_resolve_admin_target_
    chat`'s name-match step needs the WHOLE typed string to correctly
    name-match a multi-word display name ("Som Chai") -- capturing only
    the first word ("Som") could silently exact-match a different,
    unrelated pending user sharing that first word (F4's own CRITICAL
    repro). /invite is UNCHANGED (still first-token only, see
    `core/commands.py:_match_access`'s own comment) since it targets a
    chat id that's never contacted the bot -- no pending row to
    name-match against, so no equivalent risk."""
    cmd = commands.dispatch("/approve 88899900 please", registry)
    assert cmd == commands.Command(kind="approve", target_chat="88899900 please")


def test_dispatch_block_captures_full_tail_not_just_first_token(registry):
    """Archi ruling (F4) -- the /block mirror of the test above."""
    cmd = commands.dispatch("/block Som Chai", registry)
    assert cmd == commands.Command(kind="block", target_chat="Som Chai")


def test_dispatch_invite_extra_garbage_still_takes_first_token(registry):
    """Pins the deliberate asymmetry: /invite's capture is UNCHANGED by
    the F4 ruling above (still `_first_token`, not `_full_tail`) --
    see `core/commands.py:_match_access`'s own comment for why."""
    cmd = commands.dispatch("/invite 88899900 please", registry)
    assert cmd == commands.Command(kind="invite", target_chat="88899900")


@pytest.mark.parametrize(
    "text",
    [
        "/started",
        "/approved of this",
        "please /approve someone",
        "start",
        "users",
        "I approve of this plan",
    ],
)
def test_dispatch_does_not_false_positive_on_near_misses(text, registry):
    """AC5.5's own conservatism, extended to the five new kinds: nothing
    that merely resembles the trigger word (not anchored at the start, or
    missing the leading slash) is swallowed."""
    cmd = commands.dispatch(text, registry)
    assert cmd is None or cmd.kind not in ("start", "users", "approve", "block", "invite")


# ---------------------------------------------------------------------------
# classify() -- R-A1, including AC-A7's fail-safe.
# ---------------------------------------------------------------------------


def test_classify_owner(db):
    assert access.classify(db, OWNER) == "owner"


def test_classify_unknown(db):
    assert access.classify(db, STRANGER) == "unknown"


def test_classify_pending(db):
    db.upsert_user(MEMBER, status="pending")
    assert access.classify(db, MEMBER) == "pending"


def test_classify_active_member(db):
    db.upsert_user(MEMBER, status="active")
    assert access.classify(db, MEMBER) == "active"


def test_classify_blocked(db):
    db.upsert_user(MEMBER, status="blocked")
    assert access.classify(db, MEMBER) == "blocked"


def _boom(chat_id):
    raise RuntimeError("simulated DB failure")


def test_classify_fails_safe_on_lookup_error(db, monkeypatch):
    """AC-A7: a `users` lookup that raises must classify as not-active
    (deny), never grant."""
    monkeypatch.setattr(db, "get_user", _boom)
    result = access.classify(db, MEMBER)
    assert result not in ("owner", "active")


# ---------------------------------------------------------------------------
# handle_gate() -- R-A1/R-A2/R-A3, AC-A1, AC-A7.
# ---------------------------------------------------------------------------


async def test_handle_gate_owner_proceeds(db, channel, config):
    proceed = await access.handle_gate(db, channel, config, OWNER, OWNER, "Owner", "500ml", lang="en")
    assert proceed is True
    assert channel.sent == []  # nothing sent by the gate itself for an active caller


async def test_handle_gate_active_member_proceeds(db, channel, config):
    db.upsert_user(MEMBER, status="active")
    proceed = await access.handle_gate(db, channel, config, OWNER, MEMBER, "Bob", "500ml", lang="en")
    assert proceed is True
    assert channel.sent == []


async def test_handle_gate_unknown_creates_pending_and_notifies_owner(db, channel, config):
    """AC-A1: an unknown chat's first message creates a pending row,
    the sender gets `access_pending`, and the owner gets `access_request`
    naming the chat id + how to approve. handle_gate itself never logs the
    message or calls the LLM -- it has no access to either -- so a `False`
    return is what the integration wiring uses to skip both (R-A2)."""
    proceed = await access.handle_gate(db, channel, config, OWNER, STRANGER, "Alice", "hi there", lang="en")

    assert proceed is False

    row = db.get_user(STRANGER)
    assert row is not None
    assert row["status"] == "pending"
    assert row["display_name"] == "Alice"

    assert channel.sent_to(STRANGER) == [i18n.t("access_pending", "en")]

    owner_lang = i18n.resolve_unprompted_language(config, user_pref="auto")
    owner_messages = channel.sent_to(OWNER)
    assert len(owner_messages) == 1
    assert "55544433" in owner_messages[0]
    assert "/approve 55544433" in owner_messages[0]
    assert owner_messages[0] == i18n.t("access_request", owner_lang, name="Alice", chat_id=STRANGER)


async def test_handle_gate_unknown_no_display_name_falls_back_to_chat_id(db, channel, config):
    await access.handle_gate(db, channel, config, OWNER, STRANGER, None, "hi", lang="en")
    owner_lang = i18n.resolve_unprompted_language(config, user_pref="auto")
    assert channel.sent_to(OWNER) == [i18n.t("access_request", owner_lang, name=STRANGER, chat_id=STRANGER)]


async def test_handle_gate_pending_repeat_message(db, channel, config):
    """R-A3: a pending chat's Nth message still gets `access_pending`
    (no new owner notification -- they were already notified on first
    contact) and does not proceed."""
    db.upsert_user(STRANGER, status="pending", display_name="Alice")
    proceed = await access.handle_gate(db, channel, config, OWNER, STRANGER, "Alice", "are you there?", lang="en")
    assert proceed is False
    assert channel.sent_to(STRANGER) == [i18n.t("access_pending", "en")]
    assert channel.sent_to(OWNER) == []  # no repeat owner notification


async def test_handle_gate_blocked_chat_denied(db, channel, config):
    """AC-A3 (second half): a blocked chat's next message gets
    `access_denied` and is not processed."""
    db.upsert_user(STRANGER, status="blocked")
    proceed = await access.handle_gate(db, channel, config, OWNER, STRANGER, None, "hello?", lang="en")
    assert proceed is False
    assert channel.sent_to(STRANGER) == [i18n.t("access_denied", "en")]


async def test_handle_gate_thai_reply_language(db, channel, config):
    proceed = await access.handle_gate(db, channel, config, OWNER, STRANGER, "Alice", "สวัสดี", lang="th")
    assert proceed is False
    assert channel.sent_to(STRANGER) == [i18n.t("access_pending", "th")]


async def test_handle_gate_fails_safe_on_lookup_error(db, channel, config, monkeypatch):
    """AC-A7 end-to-end through the gate: a `users` lookup failure must
    never let the caller proceed, and must not attempt to write a pending
    row while the DB just failed to read."""
    monkeypatch.setattr(db, "get_user", _boom)
    proceed = await access.handle_gate(db, channel, config, OWNER, STRANGER, "Alice", "hi", lang="en")
    assert proceed is False
    assert channel.sent_to(STRANGER) == [i18n.t("access_denied", "en")]


# ---------------------------------------------------------------------------
# execute_admin() -- R-A4/R-A5, AC-A2, AC-A3, AC-A4, AC-A5, AC-A6.
# ---------------------------------------------------------------------------


async def test_execute_admin_start_active_user_gets_welcome(db, channel, config):
    """AC-A6 (active branch). The unknown/pending/blocked `/start`
    branches are covered by handle_gate's own tests above -- by the time
    execute_admin is reached for kind="start", the caller is already
    known active (R-A1 gates before any command work)."""
    db.upsert_user(MEMBER, status="active")
    await access.execute_admin(
        commands.Command(kind="start"), db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=MEMBER, lang="en"
    )
    assert channel.sent_to(MEMBER) == [i18n.t("start_welcome", "en")]


async def test_execute_admin_approve_grants_access_and_notifies_target(db, channel, config):
    """AC-A2: /approve grants active status, sends access_granted to the
    approved chat, and (this module's own added UX, see IMPL) acks the
    owner too."""
    await access.execute_admin(
        commands.Command(kind="approve", target_chat=MEMBER),
        db=db,
        channel=channel,
        config=config,
        owner_chat_id=OWNER,
        chat_id=OWNER,
        lang="en",
    )
    row = db.get_user(MEMBER)
    assert row is not None
    assert row["status"] == "active"

    target_lang = i18n.resolve_unprompted_language(config, user_pref="auto")
    assert channel.sent_to(MEMBER) == [i18n.t("access_granted", target_lang)]

    # AC-A2's own "can subsequently log normally" clause.
    assert access.classify(db, MEMBER) == "active"


async def test_execute_admin_invite_is_an_alias_of_approve(db, channel, config):
    """R-A4: /invite pre-authorizes a chat id before they ever message --
    the row doesn't exist yet, and invite still creates it active."""
    assert db.get_user(STRANGER) is None
    await access.execute_admin(
        commands.Command(kind="invite", target_chat=STRANGER),
        db=db,
        channel=channel,
        config=config,
        owner_chat_id=OWNER,
        chat_id=OWNER,
        lang="en",
    )
    row = db.get_user(STRANGER)
    assert row is not None
    assert row["status"] == "active"


async def test_execute_admin_block_revokes_access(db, channel, config):
    """AC-A3 (first half): /block sets a user blocked."""
    db.upsert_user(MEMBER, status="active")
    await access.execute_admin(
        commands.Command(kind="block", target_chat=MEMBER),
        db=db,
        channel=channel,
        config=config,
        owner_chat_id=OWNER,
        chat_id=OWNER,
        lang="en",
    )
    assert db.get_user(MEMBER)["status"] == "blocked"
    assert access.classify(db, MEMBER) == "blocked"


async def test_execute_admin_users_lists_everyone(db, channel, config):
    """AC-A5: /users lists every user with role + status, matching
    SPEC-v1.2.md §3.3's shape (an active row shows its language, a
    pending row doesn't).

    Readable-approval feature (branch line-version): a row WITH a
    captured display_name shows it, parenthesized, right after the chat
    id (STRANGER/"Charlie" below); a row with none (OWNER, MEMBER --
    neither is given a display_name in this test) renders exactly as
    before, no empty parens."""
    db.upsert_user(MEMBER, status="active")
    db.set_user_language(MEMBER, "th")
    db.upsert_user(STRANGER, status="pending", display_name="Charlie")

    await access.execute_admin(
        commands.Command(kind="users"), db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en"
    )
    [reply] = channel.sent_to(OWNER)
    lines = reply.splitlines()
    assert lines[0] == i18n.t("users_list_header", "en")
    assert f"• {OWNER} — owner · active · lang auto" in lines
    assert f"• {MEMBER} — member · active · lang th" in lines
    assert f"• {STRANGER} (Charlie) — member · pending" in lines
    # a pending row must not carry a "· lang" suffix
    assert not any(line.startswith(f"• {STRANGER}") and "lang" in line for line in lines)


async def test_execute_admin_users_list_truncates_long_display_name(db, channel, config):
    """Readable-approval feature: a very long display name is truncated
    (render-budget discipline, `core/render_budget.py:truncate`, reused
    not reimplemented) so a handful of long names can never blow one
    `/users` reply past the sendMessage budget."""
    long_name = "A" * 100
    db.upsert_user(STRANGER, status="pending", display_name=long_name)

    await access.execute_admin(
        commands.Command(kind="users"), db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en"
    )
    [reply] = channel.sent_to(OWNER)
    line = next(l for l in reply.splitlines() if l.startswith(f"• {STRANGER}"))
    assert long_name not in line
    assert "…" in line


async def test_execute_admin_admin_commands_invisible_to_non_owner(db, channel, config):
    """AC-A4: a non-owner's /approve (or /block//users/invite) is not
    executed and reveals nothing -- no reply, no state change."""
    db.upsert_user(MEMBER, status="active")  # an active, non-owner member
    db.upsert_user(STRANGER, status="pending")

    for command in (
        commands.Command(kind="approve", target_chat=STRANGER),
        commands.Command(kind="block", target_chat=OWNER),
        commands.Command(kind="users"),
        commands.Command(kind="invite", target_chat=STRANGER),
    ):
        await access.execute_admin(
            command, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=MEMBER, lang="en"
        )

    assert channel.sent == []
    assert db.get_user(STRANGER)["status"] == "pending"  # not approved
    assert db.get_user(OWNER)["status"] == "active"  # not blocked
    assert db.get_user(OWNER)["role"] == "owner"


async def test_execute_admin_approve_missing_chat_id_gets_usage_reply(db, channel, config):
    """§3.5: a malformed/missing target chat id gets a friendly usage
    message, not a crash or a silent no-op."""
    await access.execute_admin(
        commands.Command(kind="approve", target_chat=None),
        db=db,
        channel=channel,
        config=config,
        owner_chat_id=OWNER,
        chat_id=OWNER,
        lang="en",
    )
    assert channel.sent_to(OWNER) == [i18n.t("admin_usage", "en")]


async def test_execute_admin_approve_malformed_chat_id_gets_usage_reply(db, channel, config):
    await access.execute_admin(
        commands.Command(kind="approve", target_chat="not-a-chat-id"),
        db=db,
        channel=channel,
        config=config,
        owner_chat_id=OWNER,
        chat_id=OWNER,
        lang="en",
    )
    assert channel.sent_to(OWNER) == [i18n.t("admin_usage", "en")]
    assert db.get_user("not-a-chat-id") is None


# ---------------------------------------------------------------------------
# Readable-approval feature (branch line-version): /approve and /block
# resolving a name or id-prefix among PENDING users, not just the full
# opaque chat id -- core/access.py:_resolve_admin_target_chat.
# ---------------------------------------------------------------------------


async def test_execute_admin_approve_by_exact_pending_display_name(db, channel, config):
    """An unambiguous, case-insensitive exact display-name match among
    pending users resolves -- the typed case need not match the stored
    one."""
    db.upsert_user(STRANGER, status="pending", display_name="Alice")
    await access.execute_admin(
        commands.Command(kind="approve", target_chat="alice"),
        db=db,
        channel=channel,
        config=config,
        owner_chat_id=OWNER,
        chat_id=OWNER,
        lang="en",
    )
    assert db.get_user(STRANGER)["status"] == "active"


async def test_execute_admin_approve_by_ambiguous_pending_display_name_takes_no_action(db, channel, config):
    """Two pending users share the same (case-insensitive) display name
    -- no action is taken for either, and the reply lists both
    candidates' name + id so the owner can retype the full id."""
    OTHER = "77766655"
    db.upsert_user(STRANGER, status="pending", display_name="Alice")
    db.upsert_user(OTHER, status="pending", display_name="ALICE")
    await access.execute_admin(
        commands.Command(kind="approve", target_chat="alice"),
        db=db,
        channel=channel,
        config=config,
        owner_chat_id=OWNER,
        chat_id=OWNER,
        lang="en",
    )
    assert db.get_user(STRANGER)["status"] == "pending"
    assert db.get_user(OTHER)["status"] == "pending"
    [reply] = channel.sent_to(OWNER)
    assert STRANGER in reply and OTHER in reply


async def test_execute_admin_approve_by_unique_pending_id_prefix(db, channel, config):
    """A >=6-char id prefix that's unique among pending users resolves,
    even though it fails the full `_CHAT_ID_RE` shape on its own."""
    LINE_USER = "Ubrandnew0000000000000000000000000"
    db.upsert_user(LINE_USER, status="pending")
    await access.execute_admin(
        commands.Command(kind="approve", target_chat="Ubrand"),
        db=db,
        channel=channel,
        config=config,
        owner_chat_id=OWNER,
        chat_id=OWNER,
        lang="en",
    )
    assert db.get_user(LINE_USER)["status"] == "active"


async def test_execute_admin_approve_short_prefix_rejected_even_if_unique(db, channel, config):
    """A prefix under the 6-char floor is rejected even when it would be
    unique among pending users today -- falls through to the usage
    message, never a silent resolve."""
    LINE_USER = "Ubrandnew0000000000000000000000000"
    db.upsert_user(LINE_USER, status="pending")
    await access.execute_admin(
        commands.Command(kind="approve", target_chat="Ubran"),  # 5 chars, below the floor
        db=db,
        channel=channel,
        config=config,
        owner_chat_id=OWNER,
        chat_id=OWNER,
        lang="en",
    )
    assert db.get_user(LINE_USER)["status"] == "pending"
    assert channel.sent_to(OWNER) == [i18n.t("admin_usage", "en")]


async def test_execute_admin_block_by_exact_pending_display_name(db, channel, config):
    """/block resolves an unambiguous PENDING display name too -- the
    same machinery /approve uses -- so a pending request can be rejected
    by name."""
    db.upsert_user(STRANGER, status="pending", display_name="Alice")
    await access.execute_admin(
        commands.Command(kind="block", target_chat="Alice"),
        db=db,
        channel=channel,
        config=config,
        owner_chat_id=OWNER,
        chat_id=OWNER,
        lang="en",
    )
    assert db.get_user(STRANGER)["status"] == "blocked"


async def test_execute_admin_block_by_active_user_name_is_never_auto_resolved(db, channel, config):
    """Safety asymmetry (this feature's own explicit design constraint):
    a token that matches only an ACTIVE user's display name must NEVER
    auto-resolve for /block -- the active user stays untouched, and the
    reply explains that blocking an active user requires the full id
    (so a name typo can never silently block the wrong family member)."""
    db.upsert_user(MEMBER, status="active", display_name="Bob")
    await access.execute_admin(
        commands.Command(kind="block", target_chat="bob"),
        db=db,
        channel=channel,
        config=config,
        owner_chat_id=OWNER,
        chat_id=OWNER,
        lang="en",
    )
    assert db.get_user(MEMBER)["status"] == "active"
    [reply] = channel.sent_to(OWNER)
    assert MEMBER in reply
    assert "Bob" in reply
    assert reply != i18n.t("admin_usage", "en")


async def test_execute_admin_block_by_full_id_still_works_for_active_user(db, channel, config):
    """The full-id path is unaffected by this feature -- /block <full
    id> still blocks an active user directly, no name/prefix involved."""
    db.upsert_user(MEMBER, status="active", display_name="Bob")
    await access.execute_admin(
        commands.Command(kind="block", target_chat=MEMBER),
        db=db,
        channel=channel,
        config=config,
        owner_chat_id=OWNER,
        chat_id=OWNER,
        lang="en",
    )
    assert db.get_user(MEMBER)["status"] == "blocked"


# ---------------------------------------------------------------------------
# End-to-end: dispatch -> handle_gate -> execute_admin, the shape the
# integration wiring will glue together (SPEC-v1.2.md §11 integration
# order step 1). Confirms the pieces actually compose.
# ---------------------------------------------------------------------------


async def test_end_to_end_owner_approves_a_stranger_who_can_then_proceed(db, channel, config, registry):
    # 1. Stranger's first message is gated off.
    gate_result = await access.handle_gate(db, channel, config, OWNER, STRANGER, "Dana", "hello", lang="en")
    assert gate_result is False

    # 2. Owner runs /approve <chat_id>.
    approve_cmd = commands.dispatch(f"/approve {STRANGER}", registry)
    assert approve_cmd is not None
    await access.execute_admin(
        approve_cmd, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en"
    )

    # 3. The formerly-unknown chat now gates through.
    gate_result_after = await access.handle_gate(db, channel, config, OWNER, STRANGER, "Dana", "500ml", lang="en")
    assert gate_result_after is True


async def test_end_to_end_start_from_unknown_runs_pending_flow(db, channel, config, registry):
    """AC-A6: /start from an unknown user runs the same R-A2 pending flow
    as any other message -- handled entirely by handle_gate before
    command dispatch is even reached (R-A1)."""
    command = commands.dispatch("/start", registry)
    assert command == commands.Command(kind="start")

    proceed = await access.handle_gate(db, channel, config, OWNER, STRANGER, "Dana", "/start", lang="en")
    assert proceed is False
    assert channel.sent_to(STRANGER) == [i18n.t("access_pending", "en")]
    assert db.get_user(STRANGER)["status"] == "pending"


# ---------------------------------------------------------------------------
# SPEC-LINE-PORTAL.md §4 R-USERACT-1 (shared surface, admin web portal,
# branch line-version): `execute_admin` now DELEGATES to
# `access.approve_user`/`access.block_user` -- this section is the
# regression guard proving the chat `/approve`/`/block` path is
# byte-identical after the extraction (still records source="admin",
# still sends the same acks/pushes), plus direct coverage of the two
# extracted functions themselves (including source="portal").
# ---------------------------------------------------------------------------


async def test_execute_admin_approve_still_records_source_admin_after_extraction(db, channel, config):
    await access.execute_admin(
        commands.Command(kind="approve", target_chat=MEMBER),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    row = db.recent_audit(1)[0]
    assert row["action"] == "user_approve"
    assert row["source"] == "admin"
    assert row["user_id"] == OWNER  # actor
    assert row["target_user_id"] == MEMBER
    assert row["old_value"] is None
    assert row["new_value"] == "active"
    # The chat-specific ack (NOT part of the extracted function) still fires.
    assert channel.sent_to(OWNER) == [i18n.t("admin_approved_ack", "en", chat_id=MEMBER)]


async def test_execute_admin_approve_ack_is_honest_when_push_not_confirmed(db, config):
    """Integration item 4 (TEST-PORTAL-users.md Finding 1, chat-side
    parity): `admin_approved_ack` ("{chat_id} approved.") never actively
    LIED about delivery, but the fix threads `approve_user`'s new `bool`
    return into the chat ack too, so the owner gets the SAME honest
    signal the portal flash now does -- `admin_approved_ack_nopush` when
    the welcome push wasn't confirmed sent."""

    class _NoConfirmChannel(Channel):
        def __init__(self) -> None:
            self.sent: list[tuple[str, str]] = []

        async def send(self, chat_id: str, text: str) -> str | None:
            self.sent.append((chat_id, text))
            return None  # matches LineChannel.send's own "silently dropped" contract

        async def run(self, on_message, on_callback=None) -> None:
            raise NotImplementedError("not exercised in this test")

        def sent_to(self, chat_id: str) -> list[str]:
            return [text for cid, text in self.sent if cid == chat_id]

    channel = _NoConfirmChannel()
    await access.execute_admin(
        commands.Command(kind="approve", target_chat=MEMBER),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert channel.sent_to(OWNER) == [i18n.t("admin_approved_ack_nopush", "en", chat_id=MEMBER)]
    assert db.get_user(MEMBER)["status"] == "active"  # the approve itself still succeeded


async def test_execute_admin_block_still_records_source_admin_after_extraction(db, channel, config):
    db.upsert_user(MEMBER, status="active")
    await access.execute_admin(
        commands.Command(kind="block", target_chat=MEMBER),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    row = db.recent_audit(1)[0]
    assert row["action"] == "user_block"
    assert row["source"] == "admin"
    assert row["old_value"] == "active"
    assert row["new_value"] == "blocked"
    assert channel.sent_to(OWNER) == [i18n.t("admin_blocked_ack", "en", chat_id=MEMBER)]


async def test_execute_admin_approve_write_failure_still_sends_save_failed(db, channel, config, monkeypatch):
    """The try/except around the whole `approve_user(...)` call in
    `execute_admin` still catches a DB write failure exactly like the
    pre-extraction inline version did."""

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(db, "upsert_user", _boom)
    await access.execute_admin(
        commands.Command(kind="approve", target_chat=MEMBER),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert channel.sent_to(OWNER) == [i18n.t("admin_save_failed", "en")]
    assert channel.sent_to(MEMBER) == []  # no access_granted push -- the write never happened


async def test_approve_user_source_portal_records_portal_source_and_notifies_target(db, channel, config):
    result = await access.approve_user(db, channel, config, actor=OWNER, target_chat=MEMBER, source="portal")
    row = db.recent_audit(1)[0]
    assert row["action"] == "user_approve"
    assert row["source"] == "portal"
    assert row["user_id"] == OWNER  # actor
    assert db.get_user(MEMBER)["status"] == "active"
    target_lang = i18n.resolve_unprompted_language(config, user_pref="auto")
    assert channel.sent_to(MEMBER) == [i18n.t("access_granted", target_lang)]
    # No chat-command-specific ack -- that's execute_admin's own, separate concern.
    assert channel.sent_to(OWNER) == []
    # Integration item 4 (TEST-PORTAL-users.md Finding 1): the FakeChannel
    # fixture always confirms, so this reads True -- the "was the push
    # actually confirmed sent" signal, additive to the pre-existing
    # side-effect assertions above.
    assert result is True


async def test_block_user_source_portal_records_portal_source_no_notification(db, channel, config):
    db.upsert_user(MEMBER, status="active")
    await access.block_user(db, channel, config, actor=OWNER, target_chat=MEMBER, source="portal")
    row = db.recent_audit(1)[0]
    assert row["action"] == "user_block"
    assert row["source"] == "portal"
    assert db.get_user(MEMBER)["status"] == "blocked"
    assert channel.sent_to(MEMBER) == []  # blocking never notifies the target, matches pre-extraction behavior


async def test_approve_user_notification_push_failure_does_not_undo_the_approve(db, config):
    """UX Flow B's own explicit requirement: 'the approve still succeeded'
    even when the access_granted push fails."""

    class _RaisingOnSendChannel(Channel):
        async def send(self, chat_id: str, text: str) -> None:
            raise RuntimeError("simulated LINE API failure")

        async def run(self, on_message, on_callback=None) -> None:
            raise NotImplementedError

    # Should not raise -- the push failure is caught inside approve_user.
    result = await access.approve_user(db, _RaisingOnSendChannel(), config, actor=OWNER, target_chat=MEMBER, source="portal")
    assert db.get_user(MEMBER)["status"] == "active"
    row = db.recent_audit(1)[0]
    assert row["action"] == "user_approve"  # the audit row still landed
    # Integration item 4 (TEST-PORTAL-users.md Finding 1): the caller now
    # gets an honest "not confirmed" signal instead of the exception being
    # swallowed with no trace at all.
    assert result is False


async def test_approve_user_raises_on_db_write_failure_no_audit_row(db, channel, config, monkeypatch):
    """A DB write failure propagates (the caller decides how to present
    it) -- and, critically, no audit row and no side-effects run."""

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(db, "upsert_user", _boom)
    with pytest.raises(RuntimeError):
        await access.approve_user(db, channel, config, actor=OWNER, target_chat=MEMBER, source="portal")
    assert db.audit_total() == 0
    assert channel.sent_to(MEMBER) == []
