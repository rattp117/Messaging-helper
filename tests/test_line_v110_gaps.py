"""Adversarial probe of the readable-approval flow (branch `line-version`,
target `line/v1.1.0`) -- IMPL-LINE-1.1.0.md. This file does NOT re-derive
straightforward coverage `tests/test_access.py` (Luna's own tests) and
`tests/test_line_readable_approval.py` (Luna's 12 tests) already own; it
exists to attack the ACCESS-CONTROL resolution surface `core/access.py:
_resolve_admin_target_chat` adversarially -- the exact/prefix/ambiguity
rules, their interaction with the id-shape whitelist
(`_CHAT_ID_RE = ^(?:-?\\d+|U[0-9A-Za-z]{16,40})$`), and the (now fixed)
multi-word-name limitation (`core/commands.py:_match_access`).

UPDATE (post-hardening-pass): the original run of this file found 3
CRITICAL + 3 lower findings (F1-F6, TEST-LINE-1.1.0.md) -- one root
mechanism: `_resolve_admin_target_chat`'s original ordering trusted the
`_CHAT_ID_RE` shape check as an unverified, unconditional pass-through
that ran BEFORE any existence check or name/prefix attempt. Per Archi's
ruling, `_resolve_admin_target_chat` was rewritten to collect every
candidate-producing rule (exact-id-exists, pending name match, pending
prefix match) into ONE pool before deciding, and `core/commands.py:
_match_access` now captures the FULL tail (not just the first token) for
`/approve`/`/block` (finding F4). The tests below that used to PIN the
buggy behavior as documentation have been FLIPPED to assert the
corrected outcome instead -- each carries a comment citing its own
finding id (F1-F6) and a short note on what changed. Every test that was
already confirmed SAFE is untouched (see TEST-LINE-1.1.0.md's own
Resolution-Rule Safety Table for the original SAFE/UNSAFE verdicts).

Conventions match `tests/test_access.py` (its own module docstring):
real on-disk SQLite DB via `tmp_path`, no mocks -- imports its `db`/
`channel`/`config`/`registry` fixtures and `OWNER`/`MEMBER`/`STRANGER`/
`FakeChannel` directly rather than re-declaring them."""

from __future__ import annotations

import httpx
import pytest

from habit_assistant.core import access, commands, i18n
from habit_assistant.core.access import _CHAT_ID_RE, _MIN_PREFIX_CHARS
from test_access import (  # noqa: F401  (fixtures re-exported for pytest to discover)
    FakeChannel,
    MEMBER,
    OWNER,
    STRANGER,
    channel,
    config,
    db,
    registry,
)

# ===========================================================================
# Local LINE-shaped ids. Real LINE userIds are "U" + 32 lowercase-hex chars
# (34 total); `_CHAT_ID_RE` itself is NOT hex-restricted (16-40 chars after
# "U"), matching the app's own comment in core/access.py.
# ===========================================================================

REAL_PENDING = "Urealuser0000000000000000000000000"  # 34 chars, realistic length
assert len(REAL_PENDING) == 34


def _prefix(chat_id: str, n: int) -> str:
    return chat_id[:n]


# ===========================================================================
# 1. Ambiguity / active-safety asymmetry -- unicode, emoji, and two NEW
#    inconsistencies the existing test suite doesn't cover.
# ===========================================================================


async def test_approve_ambiguous_thai_names_two_pending_takes_no_action(db, channel, config):
    """Two pending users sharing an identical Thai display name are
    ambiguous exactly like an ASCII collision -- .lower() is a no-op on
    Thai script (no case distinction), so the case-fold comparison must
    not accidentally treat them as distinct or crash."""
    a, b = "Uthai0000000000000000000000000000a", "Uthai0000000000000000000000000000b"
    db.upsert_user(a, status="pending", display_name="สมชาย ใจดี")
    db.upsert_user(b, status="pending", display_name="สมชาย ใจดี")
    await access.execute_admin(
        commands.Command(kind="approve", target_chat="สมชาย ใจดี"),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert db.get_user(a)["status"] == "pending"
    assert db.get_user(b)["status"] == "pending"
    [reply] = channel.sent_to(OWNER)
    assert a in reply and b in reply


async def test_approve_ambiguous_emoji_names_two_pending_takes_no_action(db, channel, config):
    """An emoji-only display name must not crash `.lower()`/comparison,
    and two pending users sharing one are still correctly ambiguous."""
    a, b = "Uemoji000000000000000000000000000a", "Uemoji000000000000000000000000000b"
    db.upsert_user(a, status="pending", display_name="🎉Party🎉")
    db.upsert_user(b, status="pending", display_name="🎉Party🎉")
    await access.execute_admin(
        commands.Command(kind="approve", target_chat="🎉Party🎉"),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert db.get_user(a)["status"] == "pending"
    assert db.get_user(b)["status"] == "pending"


async def test_approve_by_exact_thai_name_resolves_when_unambiguous(db, channel, config):
    """Control case: a single pending Thai name resolves cleanly (no
    encoding/case-fold surprise for a real-world non-Latin name)."""
    db.upsert_user(STRANGER, status="pending", display_name="สมชาย ใจดี")
    await access.execute_admin(
        commands.Command(kind="approve", target_chat="สมชาย ใจดี"),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert db.get_user(STRANGER)["status"] == "active"


async def test_approve_name_matching_only_an_active_user_now_gets_the_same_specific_reply_as_block(db, channel, config):
    """FIXED (F6, TEST-LINE-1.1.0.md): this test used to PIN the
    asymmetry -- `/approve` fell through to the generic `admin_usage`
    reply for the identical shape `/block` already got a specific one
    for. Archi's ruling unified the reply: `_resolve_admin_target_chat`'s
    active-name-hit check no longer gates on `command.kind == "block"` --
    both commands now get the same `admin_block_name_is_active` reply
    (same catalog key, per F6's own wording) when the token names only
    an ACTIVE user."""
    db.upsert_user(MEMBER, status="active", display_name="Bob")
    await access.execute_admin(
        commands.Command(kind="approve", target_chat="bob"),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert db.get_user(MEMBER)["status"] == "active"  # no action -- safe, unchanged
    [reply] = channel.sent_to(OWNER)
    assert reply == i18n.t("admin_block_name_is_active", "en", name="Bob", chat_id=MEMBER), (
        f"expected the SAME specific active-name reply /block already gets (F6); got: {reply!r}"
    )
    assert reply != i18n.t("admin_usage", "en")


async def test_name_matching_one_pending_and_one_active_approve_resolves_pending_with_no_warning(db, channel, config):
    """FINDING: `_resolve_admin_target_chat`'s path 2 searches PENDING
    rows only (by design, per its own docstring), so when a pending user
    and an ACTIVE user coincidentally share an exact display name, the
    active candidate is never even considered for ambiguity -- the
    pending one resolves silently, with no signal to the owner that a
    namesake is already active. For /approve this is low-stakes (the
    pending user is exactly who a new approval should target), but it
    means the owner can never learn about the name collision from the
    bot's own reply."""
    pending_id = "Unamecollision00000000000000000000"
    db.upsert_user(MEMBER, status="active", display_name="Somchai")
    db.upsert_user(pending_id, status="pending", display_name="Somchai")
    await access.execute_admin(
        commands.Command(kind="approve", target_chat="somchai"),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert db.get_user(pending_id)["status"] == "active", "the pending namesake should resolve and be approved"
    assert db.get_user(MEMBER)["status"] == "active", "the pre-existing active namesake must be untouched"
    [owner_reply] = [t for c, t in channel.sent if c == OWNER]
    assert "one" not in owner_reply.lower() or "more than one" not in i18n.t("admin_ambiguous_header", "en")
    assert owner_reply == i18n.t("admin_approved_ack", "en", chat_id=pending_id), (
        "no ambiguity is ever surfaced despite the coincidental active namesake"
    )


async def test_name_matching_one_pending_and_one_active_block_now_reports_ambiguous_touches_neither(
    db, channel, config
):
    """FIXED (F5, TEST-LINE-1.1.0.md): this test used to PIN the sharper,
    /block-side inversion of the safety property the feature's own
    docstring claims -- a PENDING stranger sharing an exact display name
    with the ACTIVE person the owner meant to block used to get silently
    blocked instead, with a normal-looking success ack and the real
    active target left fully untouched. Archi's F5 ruling: `/block`
    folds the active namesake in as a SECOND candidate whenever a
    name/prefix match would otherwise settle on exactly one PENDING row
    -- forcing the ambiguous "which one, use the full id" reply instead
    of silently picking the pending stranger. NEITHER row is touched
    either way now."""
    pending_id = "Unamecollision00000000000000000001"
    db.upsert_user(MEMBER, status="active", display_name="Somchai")
    db.upsert_user(pending_id, status="pending", display_name="Somchai")
    await access.execute_admin(
        commands.Command(kind="block", target_chat="somchai"),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert db.get_user(pending_id)["status"] == "pending", "no action on the pending namesake -- ambiguous, not resolved"
    assert db.get_user(MEMBER)["status"] == "active", "no action on the active namesake either"
    [owner_reply] = [t for c, t in channel.sent if c == OWNER]
    assert owner_reply.startswith(i18n.t("admin_ambiguous_header", "en")), (
        f"expected the ambiguous 'which one' reply naming both candidates; got: {owner_reply!r}"
    )
    assert pending_id in owner_reply and MEMBER in owner_reply, "both candidates must be named so the owner can retype the full id"


async def test_block_id_prefix_of_an_active_users_own_id_never_resolves_falls_to_generic_usage(db, channel, config):
    """/block by ID-PREFIX (not name) against an active user: path 3
    (`_pending_users`) excludes active rows entirely, so there is no
    match at all -- confirmed here that this degrades to the generic
    `admin_usage` reply (NOT the specific `admin_block_name_is_active`
    reply, which only fires on a NAME match, never a prefix match). The
    active user is correctly never touched, but the owner gets the same
    unhelpful generic reply as a genuinely malformed input, with no hint
    that a real user's id prefix was recognized."""
    active_id = "Uactiveprefix00000000000000000000a"
    db.upsert_user(active_id, status="active", display_name="Carol")
    prefix = _prefix(active_id, 8)  # well within the 6-16 reachable window
    await access.execute_admin(
        commands.Command(kind="block", target_chat=prefix),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert db.get_user(active_id)["status"] == "active"
    assert channel.sent_to(OWNER) == [i18n.t("admin_usage", "en")]


# ===========================================================================
# 2. THE HEADLINE FINDING: id-shape parsing (_CHAT_ID_RE) runs BEFORE
#    prefix resolution and is a raw, unverified pass-through. Any typed
#    token of 17-41 total chars ("U" + 16-40) is treated as an
#    already-complete id and used as-is -- never checked against pending
#    rows, never checked for existence at all.
# ===========================================================================


def test_boundary_math_documented(db):
    """Pins the exact boundary so the tests below aren't "magic numbers":
    prefix resolution (`_MIN_PREFIX_CHARS`..) is reachable ONLY for a
    typed token of 6-16 total characters. At 17+ total characters the
    token itself matches `_CHAT_ID_RE`'s full-id shape and bypasses
    prefix search entirely."""
    assert _MIN_PREFIX_CHARS == 6
    assert _CHAT_ID_RE.match(_prefix(REAL_PENDING, 16)) is None, "16 total chars must NOT look like a complete id"
    assert _CHAT_ID_RE.match(_prefix(REAL_PENDING, 17)) is not None, "17 total chars DOES look like a complete id"


async def test_prefix_of_sixteen_chars_correctly_resolves_via_prefix_path_control(db, channel, config):
    """Control/boundary case: exactly at the top of the reachable window,
    prefix resolution still works correctly."""
    db.upsert_user(REAL_PENDING, status="pending")
    typed = _prefix(REAL_PENDING, 16)
    await access.execute_admin(
        commands.Command(kind="approve", target_chat=typed),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert db.get_user(REAL_PENDING)["status"] == "active"


async def test_prefix_matching_two_pending_users_is_ambiguous_takes_no_action(db, channel, config):
    """Control case not yet covered by tests/test_access.py's own 7
    resolution tests (which only exercise NAME ambiguity, not PREFIX
    ambiguity): two pending users sharing a common id prefix within the
    reachable 6-16-total-char window must be reported ambiguous, same as
    a name collision -- no action for either."""
    shared = "Ushared00000000"  # 16 chars total -- inside the reachable prefix window
    a, b = shared + "aaaaaaaaaaaaaaaa", shared + "bbbbbbbbbbbbbbbb"
    db.upsert_user(a, status="pending")
    db.upsert_user(b, status="pending")
    typed = shared[:8]  # unique to neither -- 8 chars, well within 6-16
    assert _CHAT_ID_RE.match(typed) is None  # sanity: still in the safe prefix window

    await access.execute_admin(
        commands.Command(kind="approve", target_chat=typed),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert db.get_user(a)["status"] == "pending"
    assert db.get_user(b)["status"] == "pending"
    [reply] = channel.sent_to(OWNER)
    assert a in reply and b in reply


async def test_prefix_of_seventeen_chars_now_correctly_approves_the_real_pending_user_via_prefix_match(
    db, channel, config
):
    """FIXED (F1, TEST-LINE-1.1.0.md): this test used to PIN a single
    character over the old id-shape boundary flipping the outcome
    entirely -- a 20-char prefix of `REAL_PENDING` (34 chars, a real
    LINE userId's own length) used to bypass prefix search altogether
    (treated as an already-complete id) and silently create+activate a
    phantom row instead of approving the real pending user. Archi's
    ruling fix: prefix matching against PENDING rows now runs
    REGARDLESS of whether the token also happens to satisfy
    `_CHAT_ID_RE`'s shape -- step 4 (the legacy full-id creation
    fallback) is reached only when nothing else matched. A 20-char
    prefix of a genuinely pending 34-char id now resolves correctly via
    the prefix path, exactly like the 6-16-char window always did."""
    db.upsert_user(REAL_PENDING, status="pending", display_name="Alice")
    typed = _prefix(REAL_PENDING, 20)  # a very plausible "I copied about 20 chars" mistake
    assert _CHAT_ID_RE.match(typed) is not None  # sanity: this used to be the bug's precondition
    assert typed != REAL_PENDING

    await access.execute_admin(
        commands.Command(kind="approve", target_chat=typed),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )

    assert db.get_user(REAL_PENDING)["status"] == "active", "Alice, the real intended approval, must now resolve correctly"
    assert db.get_user(typed) is None, "no phantom row for the partial-id string"
    [owner_reply] = channel.sent_to(OWNER)
    assert owner_reply == i18n.t("admin_approved_ack", "en", chat_id=REAL_PENDING)


async def test_prefix_of_seventeen_chars_on_block_now_gives_an_honest_no_match_creates_no_phantom(db, channel, config):
    """FIXED (F1, TEST-LINE-1.1.0.md) -- the /block mirror. Unlike the
    approve case above, blocking a merely-ACTIVE user by a partial
    id/prefix guess is intentionally STILL impossible (prefix matching
    only ever searches PENDING rows -- the deliberate safety rule that
    an active user is reachable only by their exact full id). What IS
    fixed: no more silently creating+blocking a phantom row with a
    normal-looking 'blocked' success ack while the real active target
    goes untouched -- the owner now gets an honest `admin_no_match`
    reply (Archi's ruling, step 4's own length-eligibility gate) instead
    of a false positive, and NO garbage row is created at all."""
    db.upsert_user(REAL_PENDING, status="active", display_name="Alice")
    typed = _prefix(REAL_PENDING, 20)

    await access.execute_admin(
        commands.Command(kind="block", target_chat=typed),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )

    assert db.get_user(REAL_PENDING)["status"] == "active", (
        "the real, intended target still has full access -- unchanged, block correctly did not fire"
    )
    assert db.get_user(typed) is None, "no phantom row is created from a too-short partial-id guess (F1 fix)"
    [owner_reply] = channel.sent_to(OWNER)
    assert owner_reply == i18n.t("admin_no_match", "en"), (
        f"expected the honest 'no match, paste the full id' reply, not a false 'blocked' success; got: {owner_reply!r}"
    )


# ===========================================================================
# 3. Sharpest construction: a typed "prefix" that happens to exactly
#    equal a DIFFERENT real user's full id (the whitelist's 16-40
#    variable length after "U" makes one id being a literal prefix of
#    another a legitimate shape, not an out-of-whitelist contrivance).
#    Here the wrong REAL user is mistargeted -- not a garbage phantom.
# ===========================================================================


async def test_prefix_that_equals_a_different_pending_users_full_id_is_now_ambiguous_not_the_wrong_person(
    db, channel, config
):
    """FIXED (F2 edge, TEST-LINE-1.1.0.md). Sharpest case: user A's full
    id is a literal prefix of user B's (both valid, distinct
    `_CHAT_ID_RE` shapes -- the regex allows 16-40 chars after "U", so
    this is well within the legitimate id-shape space, not a contrived
    out-of-band string). This test used to PIN A being silently approved
    verbatim (path 1's old unconditional priority) while B, the
    actually-named intended target, was never touched -- a genuine
    cross-user mistargeting. Archi's F2 ruling: a token that BOTH
    exact-matches one user's full id AND prefixes a DIFFERENT pending
    user's id is AMBIGUOUS -- neither is silently resolved; the reply
    names both so the owner can retype the correct full id."""
    a_full = "Uaaaaaaaaaaaaaaaa"  # 17 chars -- minimum valid full-id shape
    b_full = a_full + "zzzzzzzz"  # 25 chars -- A's id is a literal prefix of B's
    assert b_full.startswith(a_full)
    assert _CHAT_ID_RE.match(a_full) is not None
    assert _CHAT_ID_RE.match(b_full) is not None

    db.upsert_user(a_full, status="pending", display_name="Not The Target")
    db.upsert_user(b_full, status="pending", display_name="The Intended Target")

    await access.execute_admin(
        commands.Command(kind="approve", target_chat=a_full),  # owner intends B, typed A's id verbatim by mistake
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )

    assert db.get_user(a_full)["status"] == "pending", "A must NOT be silently approved -- ambiguous, not resolved"
    assert db.get_user(b_full)["status"] == "pending", "B (the intended target) must also stay untouched -- ambiguous"
    [owner_reply] = channel.sent_to(OWNER)
    assert owner_reply.startswith(i18n.t("admin_ambiguous_header", "en"))
    assert a_full in owner_reply and b_full in owner_reply


async def test_prefix_that_equals_a_different_active_users_full_id_is_now_ambiguous_not_the_wrong_person(
    db, channel, config
):
    """FIXED (F2 edge, TEST-LINE-1.1.0.md) -- the /block mirror, with the
    party whose id is the literal prefix ACTIVE instead of pending. This
    test used to PIN A (innocent, already-approved) being silently
    blocked while B (the pending stranger the owner actually meant)
    stayed fully reachable. Same F2 fix as above applies uniformly
    regardless of A's status: the exact-id-match candidate (A, found by
    step 1 REGARDLESS of status) and the prefix-match candidate (B,
    pending) are merged into one pool -- 2 distinct real users ->
    ambiguous, no action on either."""
    a_full = "Ubbbbbbbbbbbbbbbb"  # 17 chars
    b_full = a_full + "yyyyyyyy"  # A's id is a literal prefix of B's

    db.upsert_user(a_full, status="active", display_name="Innocent Family Member")
    db.upsert_user(b_full, status="pending", display_name="The Actual Nuisance")

    await access.execute_admin(
        commands.Command(kind="block", target_chat=a_full),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )

    assert db.get_user(a_full)["status"] == "active", "the innocent active user (A) must NOT be silently blocked"
    assert db.get_user(b_full)["status"] == "pending", "the actually-intended pending target (B) also stays untouched"
    [owner_reply] = channel.sent_to(OWNER)
    assert owner_reply.startswith(i18n.t("admin_ambiguous_header", "en"))
    assert a_full in owner_reply and b_full in owner_reply


# ===========================================================================
# 3b. NEW probes the merged-candidate-pool design (round 2) invites:
#     a single typed token can satisfy MULTIPLE resolution rules at
#     once. Same row via all rules -> must resolve cleanly (not a false
#     ambiguity). Different rows via different rules -> must be
#     ambiguous (this is exactly F2's general case, tested here with
#     THREE distinct rules/rows instead of two).
# ===========================================================================


async def test_token_that_is_exact_id_and_own_name_and_own_prefix_all_at_once_resolves_cleanly(db, channel, config):
    """A token can trivially satisfy step 1 (exact id), step 2 (name),
    AND step 3 (prefix of its own id) all for the SAME single row (e.g.
    a pending user whose display_name happens to equal their own chat_id
    verbatim, and any chat_id is trivially a prefix of itself). Three
    separate rule-hits on one row must dedupe to ONE candidate and
    resolve directly -- not a spurious 'ambiguous' verdict."""
    self_id = "Uselfmatch01"  # 12 chars: >= _MIN_PREFIX_CHARS, also its own exact id
    db.upsert_user(self_id, status="pending", display_name=self_id)  # name == own chat_id, verbatim

    await access.execute_admin(
        commands.Command(kind="approve", target_chat=self_id),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )

    assert db.get_user(self_id)["status"] == "active", "three rule-hits on the SAME row must still resolve, not stall"
    [owner_reply] = channel.sent_to(OWNER)
    assert owner_reply == i18n.t("admin_approved_ack", "en", chat_id=self_id)
    assert not owner_reply.startswith(i18n.t("admin_ambiguous_header", "en"))


async def test_token_that_is_exact_id_of_p_name_of_q_and_prefix_of_r_is_ambiguous_across_all_three(
    db, channel, config
):
    """The general form of F2's fix: ONE typed token simultaneously (1)
    exactly matches a real row P's id, (2) exactly matches a DIFFERENT
    pending row Q's display_name, and (3) is a prefix of a THIRD,
    distinct pending row R's id. All three rule-hits land on three
    DIFFERENT real users -- the merged pool must report all three
    ambiguous (not silently pick whichever rule happened to run first),
    and none of the three is touched."""
    token = "Umultihit1"  # 10 chars -- itself P's exact id, also a prefix of R below
    p_id = token
    q_id = "Uqrow0000000000000000"
    r_id = token + "extra0000000000"  # token is a literal prefix of r_id
    assert r_id.startswith(token) and q_id != p_id != r_id

    db.upsert_user(p_id, status="pending", display_name="P Display Name")
    db.upsert_user(q_id, status="pending", display_name=token)  # Q's NAME equals the typed token
    db.upsert_user(r_id, status="pending", display_name="R Display Name")

    await access.execute_admin(
        commands.Command(kind="approve", target_chat=token),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )

    assert db.get_user(p_id)["status"] == "pending", "P (matched via exact-id) must not be silently picked"
    assert db.get_user(q_id)["status"] == "pending", "Q (matched via name) must not be silently picked"
    assert db.get_user(r_id)["status"] == "pending", "R (matched via prefix) must not be silently picked"
    [owner_reply] = channel.sent_to(OWNER)
    assert owner_reply.startswith(i18n.t("admin_ambiguous_header", "en"))
    assert p_id in owner_reply and q_id in owner_reply and r_id in owner_reply, (
        f"all three distinct real candidates must be named so the owner can retype the exact id; got: {owner_reply!r}"
    )


# ===========================================================================
# 3c. NEW probe: the 33-char creation floor (`_MIN_FULL_LINE_ID_CHARS`)
#     boundary itself -- one char under vs. exactly at it, matching
#     nothing else at all.
# ===========================================================================


async def test_33_char_u_token_matching_nothing_else_still_fires_legacy_creation(db, channel, config):
    """A 33-char "U"-shaped token (the real, complete LINE userId length
    -- "U" + 32 hex chars) that matches no existing row, pending name, or
    prefix falls through to step 4 and IS eligible for the legacy
    pre-approve/pre-invite row-creation flow (`_is_full_id_eligible_for_
    creation`'s own `>= 33` floor) -- this is the intended, correct
    behavior per Archi's ruling (mirrors `/invite`'s own pre-existing
    "approve a real id that's never contacted the bot" use case), not a
    regression of F1's fix."""
    token = "U" + "x" * 32
    assert len(token) == 33
    await access.execute_admin(
        commands.Command(kind="approve", target_chat=token),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    row = db.get_user(token)
    assert row is not None and row["status"] == "active", "a genuinely full-length (33-char) id must still create+approve"
    [owner_reply] = channel.sent_to(OWNER)
    assert owner_reply == i18n.t("admin_approved_ack", "en", chat_id=token)


async def test_32_char_u_token_matching_nothing_else_gets_honest_no_match_creates_no_row(db, channel, config):
    """One character short of the 33-char floor: F1's fix must still
    hold here -- no phantom row, an honest `admin_no_match` reply
    instead of a false 'approved' success."""
    token = "U" + "x" * 31
    assert len(token) == 32
    await access.execute_admin(
        commands.Command(kind="approve", target_chat=token),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert db.get_user(token) is None, "a 32-char guess must NOT create a phantom row"
    [owner_reply] = channel.sent_to(OWNER)
    assert owner_reply == i18n.t("admin_no_match", "en")


# ===========================================================================
# 4. Order of resolution: a display name that itself looks like a valid
#    U-token shape -- FIXED (F3): id-shape parsing no longer has
#    unconditional priority over name matching.
# ===========================================================================


async def test_display_name_that_looks_like_a_full_id_is_now_correctly_targetable_by_typing_that_name(
    db, channel, config
):
    """FIXED (F3, TEST-LINE-1.1.0.md). A pending user's captured
    display_name (from LINE's own Get Profile API -- an owner never
    controls what a stranger names themselves) happens to itself match
    `_CHAT_ID_RE`'s full-id shape (e.g. a user who set their LINE
    display name to something that reads as "U" + 16 alnum chars). This
    test used to PIN that typing that exact name was indistinguishable
    to the resolver from typing a real id -- the old path 1 won
    unconditionally and created/activated a phantom row for the literal
    name string, NEVER attempting the name lookup that would have found
    the real pending user. Archi's fix: step 1 is now a real DB
    existence check (`db.get_user(token)`), not a shape guess -- since
    no row's chat_id literally equals the weird display-name string, it
    correctly MISSES, and step 2 (name match against pending rows)
    correctly HITS the real pending user instead."""
    real_target = "Uintendedtarget00000000000000000a"
    weird_name = "U1234567890123456"  # 17 chars -- itself a valid, but unrelated, full-id shape
    assert _CHAT_ID_RE.match(weird_name) is not None
    assert weird_name != real_target

    db.upsert_user(real_target, status="pending", display_name=weird_name)

    await access.execute_admin(
        commands.Command(kind="approve", target_chat=weird_name),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )

    assert db.get_user(real_target)["status"] == "active", "name-based resolution must now correctly find and approve them"
    assert db.get_user(weird_name) is None, "no phantom row for the literal name string"


# ===========================================================================
# 5. Multi-word-name handling (`core/commands.py:_match_access`) -- FIXED
#    (F4): /approve and /block now capture the FULL trimmed tail, so a
#    multi-word name resolves (or correctly fails to resolve) as a
#    WHOLE, never truncated to its first word.
# ===========================================================================


async def test_approve_multiword_name_no_collision_now_resolves_correctly_no_longer_a_documented_limitation(
    db, channel, config, registry
):
    """FIXED (F4, TEST-LINE-1.1.0.md): this test used to PIN
    IMPL-LINE-1.1.0.md's own documented limitation -- typing the full
    two-word name of the ONLY pending user used to discard the second
    word (`_first_token`) and fail cleanly via the generic usage reply.
    Archi's ruling fix (`core/commands.py:_match_access` now captures
    the FULL tail for /approve/`/block`) means that documented
    limitation no longer applies for the common, no-collision case: the
    full name now name-matches correctly and the pending user is
    approved."""
    db.upsert_user(STRANGER, status="pending", display_name="Som Chai")
    cmd = commands.dispatch("/approve Som Chai", registry)
    assert cmd == commands.Command(kind="approve", target_chat="Som Chai")
    await access.execute_admin(
        cmd, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    assert db.get_user(STRANGER)["status"] == "active", "the full two-word name must now resolve and approve correctly"
    assert channel.sent_to(OWNER) == [i18n.t("admin_approved_ack", "en", chat_id=STRANGER)]


async def test_approve_multiword_name_first_word_collision_now_approves_the_correctly_named_person(
    db, channel, config, registry
):
    """FIXED (F4, TEST-LINE-1.1.0.md): this test used to PIN the
    CRITICAL contradiction of IMPL-LINE-1.1.0.md's own claim ("Must be a
    safe outcome ... never approves the wrong person"). Two pending
    users: X named "Som" and Y named "Som Chai". The owner wants Y and
    types their full name, "/approve Som Chai" -- the OLD `_first_token`
    capture discarded "Chai", leaving `target_chat="Som"`, an EXACT,
    unambiguous match for the WRONG pending user (X). Archi's F4 fix
    (`core/commands.py:_match_access` captures the full tail for
    /approve/`/block` now) means `target_chat` is the FULL "Som Chai" --
    an exact match for Y, never touching X at all. Not a hypothetical:
    any pending user whose name is a prefix WORD of another's used to
    reproduce this; now it doesn't."""
    x_id, y_id = "Usomonly000000000000000000000000x", "Usomchai0000000000000000000000000y"
    db.upsert_user(x_id, status="pending", display_name="Som")
    db.upsert_user(y_id, status="pending", display_name="Som Chai")

    cmd = commands.dispatch("/approve Som Chai", registry)
    assert cmd == commands.Command(kind="approve", target_chat="Som Chai")

    await access.execute_admin(
        cmd, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )

    assert db.get_user(y_id)["status"] == "active", "Y ('Som Chai'), the actually-named target, must now be approved"
    assert db.get_user(x_id)["status"] == "pending", "X ('Som') must NOT be touched -- no longer a first-word collision"
    [owner_reply] = channel.sent_to(OWNER)
    assert owner_reply == i18n.t("admin_approved_ack", "en", chat_id=y_id)


# ===========================================================================
# 6. get_profile fail-open edge cases not yet covered by
#    tests/test_line_readable_approval.py (garbage JSON body, timeout,
#    and per-user independence of the fetch-once cap).
# ===========================================================================

from habit_assistant.channels.line import LineChannel  # noqa: E402
from habit_assistant.config import Config  # noqa: E402
from habit_assistant.storage.db import Database  # noqa: E402


def _channel_with_transport(handler, tmp_path, db_: Database) -> LineChannel:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cfg = Config.model_validate({"line": {"media_dir": str(tmp_path / "media")}})
    return LineChannel("tok", "secret", OWNER, cfg, db_, client=client)


async def test_get_profile_garbage_json_body_fails_open_to_none(tmp_path):
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all {{{", headers={"content-type": "application/json"})

    db_ = Database(tmp_path / "habits.db")
    ch = _channel_with_transport(handle, tmp_path, db_)
    name = await ch.get_profile("Ugarbage00000000000000000000000000")  # must not raise
    assert name is None
    await ch.aclose()
    db_.close()


async def test_get_profile_timeout_fails_open_to_none(tmp_path):
    async def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    db_ = Database(tmp_path / "habits.db")
    ch = _channel_with_transport(handle, tmp_path, db_)
    name = await ch.get_profile("Utimeout00000000000000000000000000")  # must not raise
    assert name is None
    await ch.aclose()
    db_.close()


async def test_display_name_for_fetch_once_cap_is_per_user_not_global(tmp_path):
    """The fetch-once cap (`_profile_fetch_attempted`) must key on
    user_id individually -- a failed/succeeded fetch for user A must not
    suppress or otherwise affect the (independent) first fetch for user
    B."""
    calls: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        user_id = request.url.path.rsplit("/", 1)[-1]
        if user_id == "Uuserb000000000000000000000000000b":
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"displayName": "Alice"})

    db_ = Database(tmp_path / "habits.db")
    ch = _channel_with_transport(handle, tmp_path, db_)

    a = "Uusera000000000000000000000000000a"
    b = "Uuserb000000000000000000000000000b"
    assert await ch._display_name_for(a) == "Alice"
    assert await ch._display_name_for(b) is None
    assert len(calls) == 2, "each user gets its own independent first-attempt fetch"

    # A repeat for either must not add a third call.
    db_.upsert_user(a, status="pending", display_name="Alice")
    assert await ch._display_name_for(a) == "Alice"
    assert await ch._display_name_for(b) is None
    assert len(calls) == 2
    await ch.aclose()
    db_.close()


# ===========================================================================
# 7. /users render budget with many long Thai names.
# ===========================================================================

from habit_assistant.core import render_budget  # noqa: E402


async def test_users_list_twenty_long_thai_names_stays_within_budget_and_truncates_each_row(db, channel, config):
    long_thai = "รักการออกกำลังกายและดื่มน้ำให้ครบทุกวันอย่างสม่ำเสมอมากๆ"  # > 24 chars
    assert len(long_thai) > access._USERS_NAME_MAX_CHARS
    for i in range(20):
        chat_id = f"Uuser{i:029d}"
        status = "active" if i % 2 == 0 else "pending"
        db.upsert_user(chat_id, status=status, display_name=f"{long_thai}{i}")

    await access.execute_admin(
        commands.Command(kind="users"),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    [rendered] = channel.sent_to(OWNER)

    assert len(rendered) < render_budget.TELEGRAM_MESSAGE_BUDGET, (
        f"20 users' worth of long Thai names must stay under the shared {render_budget.TELEGRAM_MESSAGE_BUDGET}-char "
        f"budget -- got {len(rendered)} chars"
    )
    name_rows = [line for line in rendered.splitlines() if "Uuser" in line]
    assert len(name_rows) == 20
    for line in name_rows:
        # Each row must show the TRUNCATED name (ellipsis marker), never the raw ~58-char original in full.
        assert "…" in line, f"expected a truncation ellipsis in row: {line!r}"
        assert long_thai not in line, f"the raw, untruncated name leaked into a /users row: {line!r}"


async def test_users_list_has_no_structural_total_length_cap_unlike_audit_and_history_views(db, channel, config):
    """OBSERVATION, not a failure against any stated AC (SPEC-LINE.md's
    dispatch for this feature only promises per-row truncation, item 5 in
    IMPL-LINE-1.1.0.md: "shows display names, truncated"). `core/
    render_budget.py`'s own module docstring establishes that per-value
    truncation ALONE does not bound total message length, and that is
    exactly why `core/audit_view.py`/`core/history_view.py` both call
    `render_budget.fit_within_budget`. `_render_users_list` does not --
    it is a flat `"\\n".join(lines)`. This test pins the CURRENT
    (unbounded) behavior at a user count large enough to actually exceed
    the shared 4096-char budget, so the gap is measured, not asserted as
    a bug: at 20 users (the requested probe size) it does NOT yet
    overflow (see the test above); at a larger, still entirely plausible
    user count for a family/small-team bot, it does."""
    long_thai = "รักการออกกำลังกายและดื่มน้ำให้ครบทุกวันอย่างสม่ำเสมอมากๆ"
    for i in range(120):
        chat_id = f"Uuser{i:029d}"
        db.upsert_user(chat_id, status="pending" if i % 2 else "active", display_name=f"{long_thai}{i}")

    await access.execute_admin(
        commands.Command(kind="users"),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    [rendered] = channel.sent_to(OWNER)
    assert len(rendered) > render_budget.TELEGRAM_MESSAGE_BUDGET, (
        "pins the gap: at 120 users /users renders past the shared message budget with no structural cap "
        "(a channel send at this length would 400 on Telegram's own sendMessage limit)"
    )
