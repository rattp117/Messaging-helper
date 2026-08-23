"""SPEC-v1.5.md §4 "Feature 4 -- Release announcements (module `announce`)"
-- adversarial/gap-filling tests for AC-20..AC-23, on top of Luna's own
`tests/test_announce.py` (20 tests). Written by Vera (independent tester
pass); covers angles not already exercised by the implementation's own
suite:

- Partial-failure fan-out across a 5-user active set (one user fails; all
  fail).
- Version-comparison semantics: literal string equality only (no semver
  ordering) -- downgrade/rollback, garbage stored values, explicit
  NULL-means-never-announced for a pre-v1.5 user.
- Catalog integrity: length margin vs. Telegram's message limit, no
  format-brace hazards, `get_release_note` against malformed/wrong-type
  version arguments.
- Concurrency/idempotency: two sequential calls send at most once per
  user; a true concurrent race (two overlapping calls sharing one DB/
  channel) is also probed.
- The `access.py` approve/invite catch-up line: invite-branch write
  failure (mirrors the existing approve-branch test), and the specific
  "blocked, then re-approved" scenario -- catch-up at re-approval must not
  cause a back-announcement, but must not suppress a *later* release
  either.

Same conventions as `tests/test_announce.py`: a real on-disk SQLite DB via
`tmp_path`, a `Channel` fake (no mocks for cheap/reliable in-process
behavior), `asyncio_mode = "auto"` (no explicit `@pytest.mark.asyncio`).
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import access, announce, release_notes
from habit_assistant.core.commands import Command
from habit_assistant.storage.db import Database

OWNER = "1574572064"
MEMBER = "88899900"

KNOWN_VERSION = "1.5.0"  # ships in release_notes.RELEASE_NOTES per R-N1.
UNKNOWN_VERSION = "99.9.9"


class FakeChannel(Channel):
    """Mirrors `tests/test_announce.py::FakeChannel` -- records every send,
    optionally raising for a configured set of recipients."""

    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self._fail_for = fail_for or set()

    async def send(self, chat_id: str, text: str) -> None:
        if chat_id in self._fail_for:
            raise RuntimeError(f"simulated send failure for {chat_id}")
        self.sent.append((chat_id, text))

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")

    def sent_to(self, chat_id: str) -> list[str]:
        return [text for cid, text in self.sent if cid == chat_id]

    def count_to(self, chat_id: str) -> int:
        return len(self.sent_to(chat_id))


class YieldingChannel(Channel):
    """Like `FakeChannel`, but `send` actually yields control back to the
    event loop (`asyncio.sleep(0)`) BEFORE recording the send -- a
    `FakeChannel`-style coroutine with no real suspension point runs a
    whole `announce_release()` call to completion without ever yielding,
    so two `asyncio.gather`-launched calls never truly interleave and a
    genuine race can hide. This fake creates the interleaving a real
    network call would."""

    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self._fail_for = fail_for or set()

    async def send(self, chat_id: str, text: str) -> None:
        await asyncio.sleep(0)
        if chat_id in self._fail_for:
            raise RuntimeError(f"simulated send failure for {chat_id}")
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


# ===========================================================================
# Partial-failure fan-out (5 active users).
# ===========================================================================


async def test_fan_out_one_user_fails_others_sent_and_marked(db, config):
    """5 active users; the channel raises for user 3 only. Users 1, 2, 4, 5
    must be sent-to and marked; user 3 must be left unmarked."""
    users = [f"user{i}" for i in range(1, 6)]
    for u in users:
        db.upsert_user(u, role="member", status="active")
    failing_user = users[2]  # "user3"

    channel = FakeChannel(fail_for={failing_user})
    await announce.announce_release(db, channel, config, KNOWN_VERSION)

    for u in users:
        if u == failing_user:
            assert channel.sent_to(u) == [], f"{u} should not have been sent to"
            assert db.get_last_announced_version(u) is None, f"{u} should be unmarked"
        else:
            assert len(channel.sent_to(u)) == 1, f"{u} should have received exactly one send"
            assert db.get_last_announced_version(u) == KNOWN_VERSION
    # OWNER is also active (attribute_legacy_to_owner) and unaffected.
    assert db.get_last_announced_version(OWNER) == KNOWN_VERSION


async def test_fan_out_one_user_fails_next_run_resends_only_that_user(db, config):
    """After the partial failure above, a second `announce_release` call
    (the "next startup" retry) must send ONLY to the still-unmarked user,
    and must NOT re-send to anyone already marked."""
    users = [f"user{i}" for i in range(1, 6)]
    for u in users:
        db.upsert_user(u, role="member", status="active")
    failing_user = users[2]

    first_channel = FakeChannel(fail_for={failing_user})
    await announce.announce_release(db, first_channel, config, KNOWN_VERSION)

    second_channel = FakeChannel()
    await announce.announce_release(db, second_channel, config, KNOWN_VERSION)

    assert second_channel.sent_to(failing_user) != []
    for u in users:
        if u != failing_user:
            assert second_channel.sent_to(u) == [], f"{u} must not be resent to on retry"
    assert second_channel.sent_to(OWNER) == []
    assert db.get_last_announced_version(failing_user) == KNOWN_VERSION


async def test_fan_out_all_users_fail_nobody_marked_no_crash(db, config):
    """The channel raises for every active user -> nobody gets marked, and
    the call completes without raising (fail-open per user, R-N2)."""
    users = [f"user{i}" for i in range(1, 6)]
    for u in users:
        db.upsert_user(u, role="member", status="active")
    all_ids = users + [OWNER]

    channel = FakeChannel(fail_for=set(all_ids))
    await announce.announce_release(db, channel, config, KNOWN_VERSION)  # must not raise

    assert channel.sent == []
    for u in all_ids:
        assert db.get_last_announced_version(u) is None


async def test_fan_out_all_users_fail_then_all_succeed_no_duplicates(db, config):
    """First run: total outage, nobody sent/marked. Second run: channel now
    works -- everybody gets exactly ONE send total across the two runs (no
    duplicate sends to anyone, no crash on either run)."""
    users = [f"user{i}" for i in range(1, 6)]
    for u in users:
        db.upsert_user(u, role="member", status="active")
    all_ids = users + [OWNER]

    first_channel = FakeChannel(fail_for=set(all_ids))
    await announce.announce_release(db, first_channel, config, KNOWN_VERSION)
    assert first_channel.sent == []

    second_channel = FakeChannel()
    await announce.announce_release(db, second_channel, config, KNOWN_VERSION)

    for u in all_ids:
        assert len(second_channel.sent_to(u)) == 1, f"{u} should get exactly one send total"
        assert db.get_last_announced_version(u) == KNOWN_VERSION
    assert len(second_channel.sent) == len(all_ids)


# ===========================================================================
# Version-comparison semantics: literal string equality, no ordering.
# ===========================================================================


async def test_downgrade_stored_version_newer_than_running_does_not_crash_and_sends_once(db, config):
    """A rollback scenario: the stored `last_announced_version` is NEWER
    than the currently-running version (e.g. the process was rolled back
    after having already announced a later release). `announce_release`
    does a literal string comparison (R-N2: `== version`), so this does
    NOT count as "already announced" for the older running version --  it
    sends (once, not repeatedly) and does not crash or spam."""
    db.set_last_announced_version(OWNER, "9.9.9")  # "newer" than KNOWN_VERSION
    channel = FakeChannel()

    await announce.announce_release(db, channel, config, KNOWN_VERSION)
    assert channel.sent_to(OWNER) == [release_notes.get_release_note(KNOWN_VERSION, "th")]
    assert db.get_last_announced_version(OWNER) == KNOWN_VERSION  # overwritten, not preserved

    # A second call at the SAME (older) running version is now idempotent
    # -- no spam, no crash.
    channel2 = FakeChannel()
    await announce.announce_release(db, channel2, config, KNOWN_VERSION)
    assert channel2.sent_to(OWNER) == []


@pytest.mark.parametrize(
    "garbage",
    ["", "not-a-version", "1.5", "v1.5.0", "🎉", "1.5.0 ", " 1.5.0", "1.5.0.0.0"],
)
async def test_garbage_stored_version_string_no_crash_treated_as_not_announced(db, config, garbage):
    """Any non-matching stored string (empty, malformed, whitespace-
    padded, emoji, etc.) must never crash the comparison -- it simply
    isn't equal to the running version, so the user is (correctly) treated
    as not-yet-announced and sent to."""
    db.set_last_announced_version(OWNER, garbage)
    channel = FakeChannel()

    await announce.announce_release(db, channel, config, KNOWN_VERSION)  # must not raise

    assert channel.sent_to(OWNER) == [release_notes.get_release_note(KNOWN_VERSION, "th")]
    assert db.get_last_announced_version(OWNER) == KNOWN_VERSION


async def test_null_last_announced_version_pre_v15_user_receives_current_note(db, config):
    """R-N5's own explicit statement: 'Existing users at the v1.5 upgrade
    have last_announced_version = NULL ... so they DO receive the v1.5.0
    note on first startup (self-announce).' A fresh `db` fixture's OWNER
    row (from `attribute_legacy_to_owner`) has never had
    `set_last_announced_version` called -- `NULL` by construction, exactly
    the pre-v1.5-migration shape."""
    assert db.get_last_announced_version(OWNER) is None  # sanity: genuinely NULL

    channel = FakeChannel()
    await announce.announce_release(db, channel, config, KNOWN_VERSION)

    assert channel.sent_to(OWNER) == [release_notes.get_release_note(KNOWN_VERSION, "th")]
    assert db.get_last_announced_version(OWNER) == KNOWN_VERSION


def test_get_release_note_malformed_version_arguments_never_raise():
    """`get_release_note` against version strings/values that will never
    have a catalog entry -- empty string, whitespace, a version-shaped-but-
    unknown string, and a wrong-type argument (defensive; the annotation
    says `str` but nothing enforces it at the dict-lookup boundary) -- all
    return `None`, never raise."""
    assert release_notes.get_release_note("", "en") is None
    assert release_notes.get_release_note("   ", "en") is None
    assert release_notes.get_release_note("1.5.0.1", "en") is None
    assert release_notes.get_release_note("1.5", "en") is None
    assert release_notes.get_release_note(None, "en") is None  # type: ignore[arg-type]
    assert release_notes.get_release_note(1.5, "en") is None  # type: ignore[arg-type]


def test_get_release_note_unrecognized_language_returns_none():
    """A `lang` value that isn't a real catalog language key (defensive --
    the `i18n.Language` Literal only permits "en"/"th", but nothing at
    runtime stops a bad value reaching here) must not raise -- it's just
    another missing dict key."""
    assert release_notes.get_release_note(KNOWN_VERSION, "fr") is None  # type: ignore[arg-type]


# ===========================================================================
# Catalog integrity.
# ===========================================================================

_TELEGRAM_MESSAGE_LIMIT = 4096
_SAFETY_MARGIN = 500  # a release note should have generous headroom, not skim the ceiling.


def test_catalog_entries_fit_telegram_limit_with_margin():
    for version, variants in release_notes.RELEASE_NOTES.items():
        for lang, text in variants.items():
            assert len(text) <= _TELEGRAM_MESSAGE_LIMIT - _SAFETY_MARGIN, (
                f"{version}/{lang} release note is {len(text)} chars -- too close to Telegram's "
                f"{_TELEGRAM_MESSAGE_LIMIT}-char limit"
            )


def test_catalog_entries_have_no_stray_format_braces():
    """The catalog is read verbatim (`announce_release` never calls
    `.format()`/`.format_map()` on it -- confirmed structurally below), but
    a stray unescaped `{`/`}` would still be a landmine for any future code
    path that DOES format it (e.g. logging with `%`-style vs `.format`
    mixed in, or a future templating pass). Every brace in today's catalog
    should be balanced and paired."""
    for version, variants in release_notes.RELEASE_NOTES.items():
        for lang, text in variants.items():
            assert text.count("{") == text.count("}"), f"{version}/{lang} has unbalanced braces"


def test_announce_release_never_formats_the_catalog_string():
    """Structural guard for the brace-hazard concern above: `announce.py`
    must pass the catalog string straight through to `channel.send`, never
    through `.format(`/`.format_map(`/an f-string built from it, or a
    single malformed placeholder in a future entry could raise mid-fan-out
    instead of just rendering oddly."""
    source = inspect.getsource(announce)
    assert ".format(" not in source
    assert ".format_map(" not in source


# ===========================================================================
# Concurrency / idempotency.
# ===========================================================================


async def test_double_call_in_a_row_sends_at_most_once_per_user(db, config):
    """Sequential double-invocation (e.g. two startups back-to-back, or a
    supervisor restarting the process immediately) -- across BOTH calls,
    every active user receives exactly one send total, aggregated over a
    single shared channel."""
    db.upsert_user(MEMBER, role="member", status="active")
    channel = FakeChannel()

    await announce.announce_release(db, channel, config, KNOWN_VERSION)
    await announce.announce_release(db, channel, config, KNOWN_VERSION)

    assert len(channel.sent_to(OWNER)) == 1
    assert len(channel.sent_to(MEMBER)) == 1
    assert len(channel.sent) == 2


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Known TOCTOU race in announce_release under truly concurrent "
        "invocation (no lock between the 'already announced?' read and "
        "the mark-on-success write) -- Archi ruling, PROGRESS.md "
        '2026-08-23: "Accepted out of scope: single-instance architecture '
        "makes it pathological, sequential-restart idempotency is proven, "
        'harm is cosmetic. Revisit only if the bot ever runs multi-instance."'
    ),
)
async def test_concurrent_overlapping_calls_send_at_most_once_per_user(db, config):
    """A true race: two `announce_release` calls launched concurrently
    (`asyncio.gather`) against the SAME db/channel, simulating a double-
    startup race rather than a clean sequential retry. `YieldingChannel`
    forces a real interleaving point inside `channel.send` so the two
    calls can genuinely overlap instead of one running to completion
    before the other starts. Each active user must still receive at most
    one send -- if this fails, `announce_release` has a TOCTOU race between
    its "already announced?" read and the mark-on-success write (no lock),
    and Luna should be told before this ships behind a process model that
    doesn't already guarantee single-flight startup.

    xfail(strict=False), not skip: the race is real and reproducible (5/5
    per TEST-v1.5-announce.md) -- this documents it as a known, accepted
    limitation rather than hiding it, while keeping the suite green.
    strict=False means an unexpected PASS (e.g. if a future asyncio
    scheduling change or a fix removes the race) is not itself a failure --
    it would show as XPASS, worth a follow-up glance, not a red suite."""
    db.upsert_user(MEMBER, role="member", status="active")
    channel = YieldingChannel()

    await asyncio.gather(
        announce.announce_release(db, channel, config, KNOWN_VERSION),
        announce.announce_release(db, channel, config, KNOWN_VERSION),
    )

    owner_sends = len(channel.sent_to(OWNER))
    member_sends = len(channel.sent_to(MEMBER))
    assert owner_sends <= 1, f"OWNER received {owner_sends} sends from two concurrent announce_release calls"
    assert member_sends <= 1, f"MEMBER received {member_sends} sends from two concurrent announce_release calls"


# ===========================================================================
# access.py approve/invite catch-up line -- invite-branch failure, and the
# blocked-then-approved scenario.
# ===========================================================================


async def test_ac23_invite_write_failure_does_not_crash_and_still_acks(db, config, monkeypatch):
    """Mirrors `test_ac23_approve_write_failure_does_not_crash_and_still_reports_save_failed`
    in `tests/test_announce.py`, but through the `invite` branch specifically
    -- the parent dispatch calls out "invite branch same" as its own check,
    not an assumption to take on faith from the approve-branch test alone."""
    monkeypatch.setattr(access, "__version__", KNOWN_VERSION)
    db.upsert_user(MEMBER, status="pending")
    channel = FakeChannel()

    def _raising(chat_id, version):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(db, "set_last_announced_version", _raising)

    cmd = Command(kind="invite", target_chat=MEMBER)
    await access.execute_admin(cmd, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en")

    # Approve/invite itself still fully succeeded despite the catch-up
    # write blowing up.
    assert access.classify(db, MEMBER) == "active"
    assert channel.sent_to(OWNER) != []  # admin_approved_ack still sent
    assert channel.sent_to(MEMBER) != []  # access_granted still sent to the invitee


async def test_blocked_then_reapproved_user_gets_no_back_announcement_but_future_release_reaches_them(
    db, config, monkeypatch
):
    """The specific adversarial scenario: a user is blocked BEFORE a
    version ships (so they never receive that version's announcement --
    §10 "announcements to pending/blocked users" is out of scope), then
    re-approved AFTER that version has already shipped. R-N5's catch-up
    write fires on every approve/invite (not just a user's first-ever
    approval), so re-approval marks them caught up to the CURRENT running
    version -- no back-announcement for the release they missed while
    blocked. A subsequent, later release must still reach them normally
    (catch-up is not a permanent announcement kill-switch)."""
    monkeypatch.setattr(access, "__version__", KNOWN_VERSION)
    db.upsert_user(MEMBER, status="active")

    # Blocked while KNOWN_VERSION is the running version -- excluded from
    # active_user_ids(), so a startup announce right now would skip them
    # even without ever having been marked.
    block_cmd = Command(kind="block", target_chat=MEMBER)
    channel = FakeChannel()
    await access.execute_admin(
        block_cmd, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en"
    )
    assert access.classify(db, MEMBER) == "blocked"
    assert db.get_last_announced_version(MEMBER) is None  # block never touches this (existing AC-23 coverage)

    # The version's announcement round happens while MEMBER is blocked --
    # they get nothing (audience exclusion, not this test's own concern,
    # but establishes the "missed it" precondition).
    startup_channel = FakeChannel()
    await announce.announce_release(db, startup_channel, config, KNOWN_VERSION)
    assert startup_channel.sent_to(MEMBER) == []

    # Re-approved AFTER the release shipped -- R-N5 catches them up to the
    # CURRENT version, so no back-announcement for what they missed.
    approve_cmd = Command(kind="approve", target_chat=MEMBER)
    reapprove_channel = FakeChannel()
    await access.execute_admin(
        approve_cmd, db=db, channel=reapprove_channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en"
    )
    assert db.get_last_announced_version(MEMBER) == KNOWN_VERSION

    retry_channel = FakeChannel()
    await announce.announce_release(db, retry_channel, config, KNOWN_VERSION)
    assert retry_channel.sent_to(MEMBER) == []  # no back-announcement

    # A LATER release must still reach them -- catch-up only suppresses
    # the version they were caught up to, not every future one.
    release_notes.RELEASE_NOTES["1.6.0-test-gap"] = {"en": "next release notes", "th": "โน้ตรุ่นถัดไป"}
    try:
        future_channel = FakeChannel()
        await announce.announce_release(db, future_channel, config, "1.6.0-test-gap")
        assert future_channel.sent_to(MEMBER) != []
    finally:
        del release_notes.RELEASE_NOTES["1.6.0-test-gap"]
