"""SPEC-v1.2.md §4 "Access control & onboarding (module `access`)" -- Vera's
adversarial pass on top of Luna's own tests/test_access.py (37 tests, all 7
owned ACs AC-A1-AC-A7 already covered on the happy/primary path). This file
adds security-critical, fail-safe, and cross-track edge cases that go beyond
IMPL-v1.2-access.md's own coverage: non-owner/blocked/pending callers hitting
`execute_admin` directly, owner self-block, idempotent approve/re-approve,
garbage/huge/negative chat-id admin args, structural proof that a gated-off
message can never reach `logs` or an LLM, repeat-message spam suppression,
DB-error fail-safes beyond `classify` itself, bilingual coverage of every
access/admin reply, and dispatch-precedence collisions against every other
command kind in this codebase.

Same fixture/style conventions as tests/test_access.py: real on-disk SQLite
(tmp_path), no DB mocks, only external-boundary doubles (channel) faked.
"""

from __future__ import annotations

import inspect

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import access, commands, i18n
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.storage.db import Database

OWNER = "1574572064"
MEMBER = "88899900"
STRANGER = "55544433"
STRANGER_2 = "22233344"


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

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


def _boom(*args, **kwargs):
    raise RuntimeError("simulated DB failure")


def _log_count(db) -> int:
    return db._conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]


# ---------------------------------------------------------------------------
# AC-A4 hardening: execute_admin's owner re-check must refuse EVERY non-owner
# state (active member already covered by Luna) -- pending, blocked, and
# even a chat unknown to `users` entirely, called directly (defense in
# depth, since in production these chats never reach execute_admin at all --
# handle_gate would have already gated them off).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "blocked"])
async def test_execute_admin_noop_for_non_active_non_owner_status(db, channel, config, status):
    db.upsert_user(MEMBER, status=status)
    await access.execute_admin(
        commands.Command(kind="approve", target_chat=STRANGER),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=MEMBER, lang="en",
    )
    assert channel.sent == []
    assert db.get_user(STRANGER) is None  # never approved


async def test_execute_admin_noop_for_unknown_chat(db, channel, config):
    """A chat with no `users` row at all (classify -> "unknown") must be
    refused by execute_admin's own role re-check exactly like any other
    non-owner -- R-A4's "only executed when the acting user's role is
    owner" has no carve-out for "row doesn't exist yet"."""
    assert db.get_user(STRANGER) is None
    await access.execute_admin(
        commands.Command(kind="users"), db=db, channel=channel, config=config,
        owner_chat_id=OWNER, chat_id=STRANGER, lang="en",
    )
    assert channel.sent == []


async def test_execute_admin_start_branch_now_has_defense_in_depth_role_check(db, channel, config):
    """UPDATED at the integration step (IMPL-v1.2-integration.md): this
    test originally documented a low-severity, non-blocking FINDING --
    unlike approve/block/users/invite, which all re-check `classify(db,
    chat_id)` even though the caller is supposed to already be gated
    (belt-and-suspenders, per `execute_admin`'s own docstring), the
    `"start"` branch had NO status check at all, relying entirely on the
    precondition "`handle_gate` already returned `True` for this chat".
    TEST-v1.2-access.md's own recommendation ("Suggest Luna add the same
    one-line re-check for consistency") was accepted and implemented in
    the integration pass -- `execute_admin`'s `"start"` branch now
    re-checks `classify(db, chat_id) in ("owner", "active")` (not
    `== "owner"` alone, since `/start` is available to any active user,
    R-A5 -- unlike the four true admin commands, which ARE owner-only).
    This test is updated to assert the FIXED (safe) behavior instead of
    the gap it used to document; the original assertion (STRANGER, never
    gated/approved, DOES get `start_welcome`) now fails by design."""
    assert db.get_user(STRANGER) is None  # never gated/approved
    await access.execute_admin(
        commands.Command(kind="start"), db=db, channel=channel, config=config,
        owner_chat_id=OWNER, chat_id=STRANGER, lang="en",
    )
    # Fixed behavior: an ungated STRANGER gets no reply at all.
    assert channel.sent_to(STRANGER) == []

    # Sanity: the fix doesn't regress /start for a genuinely active member
    # (not owner-only -- R-A5) or the owner themselves.
    db.upsert_user(MEMBER, role="member", status="active")
    await access.execute_admin(
        commands.Command(kind="start"), db=db, channel=channel, config=config,
        owner_chat_id=OWNER, chat_id=MEMBER, lang="en",
    )
    assert channel.sent_to(MEMBER) == [i18n.t("start_welcome", "en")]

    db.upsert_user(OWNER, role="owner", status="active")
    await access.execute_admin(
        commands.Command(kind="start"), db=db, channel=channel, config=config,
        owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert channel.sent_to(OWNER) == [i18n.t("start_welcome", "en")]


async def test_execute_admin_fails_safe_when_role_lookup_errors_even_for_the_real_owner(db, channel, config, monkeypatch):
    """Security-critical: extends AC-A7's fail-safe direction past
    `classify`/`handle_gate` into `execute_admin` itself. If the `users`
    lookup used for the owner re-check raises, the ACTUAL owner's admin
    command must still be refused (deny), never silently allowed through
    on a stale/cached notion of "owner" -- `execute_admin` re-derives
    ownership via a fresh `classify(db, chat_id)` call every time, so a
    DB hiccup mid-command fails the same direction as a DB hiccup on the
    inbound gate."""
    monkeypatch.setattr(db, "get_user", _boom)
    await access.execute_admin(
        commands.Command(kind="approve", target_chat=MEMBER),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert channel.sent == []


# ---------------------------------------------------------------------------
# Owner self-block: R-A1 states "owner ⊂ active" and `classify`'s own
# docstring says role="owner" classifies as "owner" OUTRIGHT regardless of
# status -- so /block on the owner's own chat id changes the stored status
# column but cannot actually revoke access (role, not status, is
# authoritative for the owner). This is a deliberate consequence of the
# documented classify() contract, not a spec violation -- verified
# explicitly since it's the kind of edge case that's easy to get backwards.
# ---------------------------------------------------------------------------


async def test_owner_blocking_self_does_not_revoke_owner_classify(db, channel, config):
    await access.execute_admin(
        commands.Command(kind="block", target_chat=OWNER),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert db.get_user(OWNER)["status"] == "blocked"  # the write did happen
    assert db.get_user(OWNER)["role"] == "owner"
    # ... but classify() still reports "owner" (role is authoritative),
    # so the gate still lets them through -- self-block cannot lock the
    # owner out via this path.
    assert access.classify(db, OWNER) == "owner"
    proceed = await access.handle_gate(db, channel, config, OWNER, OWNER, "Owner", "500ml", lang="en")
    assert proceed is True


# ---------------------------------------------------------------------------
# Idempotency: approving an already-active user; block -> re-approve round
# trip.
# ---------------------------------------------------------------------------


async def test_approving_already_active_user_is_idempotent(db, channel, config):
    db.upsert_user(MEMBER, status="active")
    await access.execute_admin(
        commands.Command(kind="approve", target_chat=MEMBER),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert db.get_user(MEMBER)["status"] == "active"
    assert access.classify(db, MEMBER) == "active"
    # Re-approving still (harmlessly) re-sends access_granted -- no crash,
    # no duplicate row, no state corruption.
    target_lang = i18n.resolve_unprompted_language(config, user_pref="auto")
    assert channel.sent_to(MEMBER) == [i18n.t("access_granted", target_lang)]


async def test_block_then_reapprove_restores_active_access(db, channel, config):
    db.upsert_user(MEMBER, status="active")
    await access.execute_admin(
        commands.Command(kind="block", target_chat=MEMBER),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert access.classify(db, MEMBER) == "blocked"

    await access.execute_admin(
        commands.Command(kind="approve", target_chat=MEMBER),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert access.classify(db, MEMBER) == "active"

    proceed = await access.handle_gate(db, channel, config, OWNER, MEMBER, "Bob", "500ml", lang="en")
    assert proceed is True


async def test_invite_reactivates_an_existing_pending_row(db, channel, config):
    """R-A4: "/invite <chat_id> -- alias of /approve". Luna's own test only
    covers a brand-new chat id; /invite must equally re-activate a chat
    that already has a pending (or blocked) row."""
    db.upsert_user(STRANGER, status="pending", display_name="Charlie")
    await access.execute_admin(
        commands.Command(kind="invite", target_chat=STRANGER),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert db.get_user(STRANGER)["status"] == "active"
    assert db.get_user(STRANGER)["display_name"] == "Charlie"  # not clobbered


# ---------------------------------------------------------------------------
# /approve|/block|/invite chat-id argument validation: garbage, absent,
# huge, negative (group-chat-shaped), zero, whitespace-only tails.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target_chat",
    [
        "abc",
        "12ab",
        "",
        "   ",
        "+123",
        "12.5",
        "--123",
        "123-456",
        None,
    ],
)
async def test_approve_rejects_malformed_or_missing_chat_id(db, channel, config, target_chat):
    await access.execute_admin(
        commands.Command(kind="approve", target_chat=target_chat),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert channel.sent_to(OWNER) == [i18n.t("admin_usage", "en")]


@pytest.mark.parametrize("target_chat", ["99999999999999999999999999", "-987654321", "0", "007"])
async def test_approve_accepts_huge_negative_zero_and_leading_zero_chat_ids(db, channel, config, target_chat):
    """Group/channel chat ids are negative in the real Telegram API (§2.1),
    and a chat id is just an opaque digit string otherwise -- huge,
    zero, and leading-zero values are all structurally valid and must be
    accepted, not rejected as "malformed"."""
    await access.execute_admin(
        commands.Command(kind="approve", target_chat=target_chat),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    row = db.get_user(target_chat)
    assert row is not None
    assert row["status"] == "active"


async def test_block_rejects_malformed_chat_id_without_writing(db, channel, config):
    await access.execute_admin(
        commands.Command(kind="block", target_chat="not-a-chat-id"),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert channel.sent_to(OWNER) == [i18n.t("admin_usage", "en")]
    assert db.get_user("not-a-chat-id") is None


def test_dispatch_approve_trailing_whitespace_only_still_matches_bare_form(registry):
    """`dispatch()` strips the whole message before any pattern runs
    (`stripped = text.strip()`), so "/approve" with only trailing
    whitespace is indistinguishable from bare "/approve" by the time
    `_APPROVE_RE` sees it -- both correctly produce `target_chat=None`
    (-> `admin_usage`), not a silent `None` fall-through. (`_APPROVE_RE`
    matched in isolation against the UNSTRIPPED string would actually
    fail here since its optional group requires `\\s+` followed by a
    non-whitespace char -- `dispatch`'s own `.strip()` is what prevents
    that from ever being observable.)"""
    assert commands.dispatch("/approve   ", registry) == commands.Command(kind="approve", target_chat=None)
    assert commands.dispatch("/approve", registry) == commands.Command(kind="approve", target_chat=None)


# ---------------------------------------------------------------------------
# AC-A1 (structural): a gated-off message can NEVER reach `logs` or an LLM.
# `handle_gate`'s own signature has no LLM parameter at all, so "zero LLM
# calls" is provable from the signature alone; "zero log writes" is proven
# by making `insert_log` raise if it's ever called, for every non-proceeding
# classification.
# ---------------------------------------------------------------------------


def test_handle_gate_signature_has_no_llm_reference():
    """Structural proof that handle_gate cannot call an LLM: it has no
    parameter through which one could be reached (no `llm`, no `client`,
    nothing callable-shaped besides `db`/`channel`)."""
    params = set(inspect.signature(access.handle_gate).parameters)
    assert params == {"db", "channel", "config", "owner_chat_id", "chat_id", "display_name", "text", "lang"}


@pytest.mark.parametrize(
    "setup_status",
    [None, "pending", "blocked"],
    ids=["unknown", "pending", "blocked"],
)
async def test_handle_gate_never_writes_a_log_row_for_a_non_proceeding_caller(db, channel, config, monkeypatch, setup_status):
    if setup_status is not None:
        db.upsert_user(STRANGER, status=setup_status)
    monkeypatch.setattr(db, "insert_log", lambda entry: (_ for _ in ()).throw(AssertionError("insert_log must never be called")))

    before = _log_count(db)
    proceed = await access.handle_gate(db, channel, config, OWNER, STRANGER, "Alice", "500ml", lang="en")
    assert proceed is False
    assert _log_count(db) == before == 0


async def test_handle_gate_unknown_path_survives_a_write_failure_on_the_pending_row(db, channel, config, monkeypatch):
    """R-A2's own "best-effort on the write" contract: if creating the
    pending row itself fails (DB hiccup), the asker must still get a
    reply and the caller must still be denied -- not crash the inbound
    loop and not accidentally proceed."""
    monkeypatch.setattr(db, "upsert_user", _boom)
    proceed = await access.handle_gate(db, channel, config, OWNER, STRANGER, "Alice", "hi", lang="en")
    assert proceed is False
    assert channel.sent_to(STRANGER) == [i18n.t("access_pending", "en")]


# ---------------------------------------------------------------------------
# AC-A1 spam check: an unknown chat's first message notifies the owner
# EXACTLY once, never once per subsequent message while still pending.
# Luna's own test covers one repeat; this extends to several repeats and a
# mix of message content (including "/start").
# ---------------------------------------------------------------------------


async def test_owner_notified_exactly_once_across_many_repeat_messages_while_pending(db, channel, config):
    await access.handle_gate(db, channel, config, OWNER, STRANGER, "Alice", "hi", lang="en")
    for text in ("hi again", "hello?", "/start", "are you there", "500ml"):
        proceed = await access.handle_gate(db, channel, config, OWNER, STRANGER, "Alice", text, lang="en")
        assert proceed is False

    owner_messages = channel.sent_to(OWNER)
    assert len(owner_messages) == 1
    # the asker got a reply every single time, though (not silently dropped)
    assert len(channel.sent_to(STRANGER)) == 6


# ---------------------------------------------------------------------------
# AC-A6 completeness: /start from a PENDING or BLOCKED chat (not just
# unknown) still runs the ordinary R-A3 gate reply -- handle_gate ignores
# `text` entirely, so `/start` is not special-cased for these two states.
# ---------------------------------------------------------------------------


async def test_start_from_pending_chat_gets_access_pending(db, channel, config):
    db.upsert_user(STRANGER, status="pending", display_name="Alice")
    proceed = await access.handle_gate(db, channel, config, OWNER, STRANGER, "Alice", "/start", lang="en")
    assert proceed is False
    assert channel.sent_to(STRANGER) == [i18n.t("access_pending", "en")]


async def test_start_from_blocked_chat_gets_access_denied(db, channel, config):
    db.upsert_user(STRANGER, status="blocked")
    proceed = await access.handle_gate(db, channel, config, OWNER, STRANGER, None, "/start", lang="en")
    assert proceed is False
    assert channel.sent_to(STRANGER) == [i18n.t("access_denied", "en")]


# ---------------------------------------------------------------------------
# classify(): defensive fallback for an unexpected `status` value neither
# "active" nor "blocked" (the docstring's own "or a defensive fallback for
# an unexpected value -> pending" clause).
# ---------------------------------------------------------------------------


async def test_classify_unexpected_status_value_falls_back_to_pending(db):
    db._conn.execute("UPDATE users SET status = 'weird_future_status' WHERE chat_id = ?", (OWNER,))
    db._conn.execute(
        "INSERT INTO users (chat_id, role, status) VALUES (?, 'member', 'weird_future_status')", (MEMBER,)
    )
    db._conn.commit()
    # the owner row is unaffected by the status column at all (role wins)
    assert access.classify(db, OWNER) == "owner"
    assert access.classify(db, MEMBER) == "pending"


# ---------------------------------------------------------------------------
# Bilingual coverage: every access/admin reply id, in both languages, with
# no KeyError / mojibake. Luna's own tests cover access_pending/
# access_denied in Thai; this rounds out access_granted, admin_usage,
# admin_save_failed, users list, and start_welcome.
# ---------------------------------------------------------------------------


async def test_approve_grants_access_in_thai_when_target_prefers_thai(db, channel, config):
    db.upsert_user(MEMBER, status="pending")
    db.set_user_language(MEMBER, "th")
    await access.execute_admin(
        commands.Command(kind="approve", target_chat=MEMBER),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert channel.sent_to(MEMBER) == [i18n.t("access_granted", "th")]
    assert "เข้าใช้งานได้แล้ว" in channel.sent_to(MEMBER)[0]


async def test_admin_usage_reply_in_thai(db, channel, config):
    await access.execute_admin(
        commands.Command(kind="approve", target_chat=None),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="th",
    )
    assert channel.sent_to(OWNER) == [i18n.t("admin_usage", "th")]


async def test_admin_save_failed_reply_both_languages_on_db_error(db, channel, config, monkeypatch):
    monkeypatch.setattr(db, "upsert_user", _boom)
    for lang in ("en", "th"):
        chan = FakeChannel()
        await access.execute_admin(
            commands.Command(kind="approve", target_chat=MEMBER),
            db=db, channel=chan, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang=lang,
        )
        assert chan.sent_to(OWNER) == [i18n.t("admin_save_failed", lang)]


async def test_users_list_in_thai_has_no_keyerror_or_mojibake(db, channel, config):
    db.upsert_user(MEMBER, status="active")
    db.set_user_language(MEMBER, "th")
    await access.execute_admin(
        commands.Command(kind="users"), db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="th",
    )
    [reply] = channel.sent_to(OWNER)
    assert reply.splitlines()[0] == i18n.t("users_list_header", "th")
    assert f"• {MEMBER} — member · active · lang th" in reply


async def test_start_welcome_in_thai(db, channel, config):
    db.upsert_user(MEMBER, status="active")
    await access.execute_admin(
        commands.Command(kind="start"), db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=MEMBER, lang="th",
    )
    assert channel.sent_to(MEMBER) == [i18n.t("start_welcome", "th")]


@pytest.mark.parametrize("msg_id", [
    "access_pending", "access_denied", "access_request", "access_granted",
    "start_welcome", "admin_usage", "admin_save_failed", "admin_approved_ack",
    "admin_blocked_ack", "users_list_header", "users_list_line",
    # Readable-approval feature (branch line-version): the /approve|/block
    # name/id-prefix resolver's own two new catalog ids.
    "admin_ambiguous_header", "admin_ambiguous_line", "admin_block_name_is_active",
])
def test_every_access_catalog_key_formats_cleanly_both_languages(msg_id):
    """No KeyError formatting either language variant with a representative
    kwarg set -- covers every id this module owns, independent of any
    particular code path exercising it."""
    kwargs = {
        "name": "Alice",
        "chat_id": STRANGER,
        "role": "member",
        "status": "active",
        "lang_suffix": " · lang th",
        # Readable-approval feature: users_list_line's own new placeholder
        # (empty string is the "no display_name" shape, the common case);
        # str.format ignores every kwarg a given template doesn't reference,
        # so this is a no-op for every other id in the list above.
        "name_suffix": " (Alice)",
    }
    for lang in ("en", "th"):
        text = i18n.t(msg_id, lang, **kwargs)
        assert isinstance(text, str) and text  # non-empty, no raw "{...}" left unformatted
        assert "{" not in text and "}" not in text


# ---------------------------------------------------------------------------
# Cross-track dispatch precedence: the five `access` kinds must never
# collide with undo/edit/snooze/target/remind/lang/quiet/help/habits/query,
# in either direction (a real other-track command must not be swallowed by
# an access pattern, and vice versa).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_kind",
    [
        ("/undo", "undo"),
        ("undo", "undo"),
        ("/target", "target"),
        ("/target water 2000", "target"),
        ("/help", "help"),
        ("/habits", "habits"),
        ("/remind water 08:00", "remind"),
        ("เตือน water 08:00", "remind"),
        ("/lang th", "lang"),
        ("ภาษา th", "lang"),
        ("/quiet 22:00-07:00", "quiet"),
        ("เงียบ off", "quiet"),
        ("snooze 30", "snooze"),
        ("how much water did I drink?", "query"),
    ],
)
def test_other_track_commands_are_not_shadowed_by_access_patterns(registry, text, expected_kind):
    cmd = commands.dispatch(text, registry)
    assert cmd is not None
    assert cmd.kind == expected_kind


@pytest.mark.parametrize(
    "text,expected_kind",
    [
        ("/start", "start"),
        ("/users", "users"),
        ("/approve 123", "approve"),
        ("/block 123", "block"),
        ("/invite 123", "invite"),
    ],
)
def test_access_commands_are_not_shadowed_by_any_other_track(registry, text, expected_kind):
    """The inverse direction: none of the other tracks' patterns (undo,
    edit, snooze, target, remind, lang/quiet, help/habits, query) steal
    an access command before `_match_access` gets a chance -- `dispatch`
    checks access strictly after target/remind and before lang/quiet/
    help/habits/query (core/commands.py's own documented order)."""
    cmd = commands.dispatch(text, registry)
    assert cmd is not None
    assert cmd.kind == expected_kind


@pytest.mark.parametrize(
    "text",
    ["/startup", "/usersome", "/approve123", "/blocking 5", "/invited", "restart", "please /users"],
)
def test_access_near_misses_do_not_false_positive(registry, text):
    cmd = commands.dispatch(text, registry)
    assert cmd is None or cmd.kind not in ("start", "users", "approve", "block", "invite")
