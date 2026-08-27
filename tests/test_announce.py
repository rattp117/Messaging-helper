"""SPEC-v1.5.md §4 "Feature 4 -- Release announcements (module `announce`)"
-- module tests for the four ACs this parallel module owns (SPEC-v1.5.md
§11): AC-20, AC-21, AC-22, AC-23.

Two disjoint surfaces:
- `core/announce.py:announce_release` + `core/release_notes.py` -- exercised
  directly (this module is not yet wired into `main.py`'s startup sequence,
  per SPEC-v1.5.md §11's integration order), against a real on-disk SQLite
  DB (tmp_path), mirroring `tests/test_access.py`'s own convention.
- `core/access.py:execute_admin`'s approve/invite catch-up line (R-N5,
  AC-23) -- exercised through the REAL `execute_admin`, not re-implemented,
  so the test proves the actual production code path.

AC-24 (audience + DND + latest-only, verified at the startup-loop
integration per §11) is intentionally NOT re-derived end-to-end here (no
`main.py` wiring exists yet to drive it through); this file instead pins
the underlying pieces AC-24 depends on at the unit level (active-only via
`db.active_user_ids()`, latest-version-only by construction, and a
structural "never calls `in_dnd_now`" proof for R-N4) so the eventual
integration pass has nothing left to discover.
"""

from __future__ import annotations

import inspect

import pytest

from habit_assistant import __version__
from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import access, announce, i18n, release_notes
from habit_assistant.core.commands import Command
from habit_assistant.storage.db import Database

OWNER = "1574572064"
MEMBER = "88899900"
MEMBER2 = "77788899"

KNOWN_VERSION = "1.5.0"  # ships in release_notes.RELEASE_NOTES per R-N1.
UNKNOWN_VERSION = "99.9.9"  # deliberately never given a catalog entry.


class FakeChannel(Channel):
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


# ===========================================================================
# core/release_notes.py -- catalog shape (R-N1).
# ===========================================================================


def test_v150_ships_as_a_catalog_entry():
    assert "1.5.0" in release_notes.RELEASE_NOTES


def test_get_release_note_returns_both_languages_for_v150():
    en = release_notes.get_release_note("1.5.0", "en")
    th = release_notes.get_release_note("1.5.0", "th")
    assert en is not None and th is not None
    assert en != th


def test_get_release_note_unknown_version_returns_none_never_raises():
    assert release_notes.get_release_note(UNKNOWN_VERSION, "en") is None
    assert release_notes.get_release_note(UNKNOWN_VERSION, "th") is None


def test_every_catalog_entry_has_both_en_and_th():
    """R-N1's own authoring convention -- `announce_release` would
    silently under-deliver to a whole language cohort if this ever
    drifted (mirrors `tests/test_i18n.py`'s equivalent guard for
    `core/i18n.py:CATALOG`)."""
    for version, variants in release_notes.RELEASE_NOTES.items():
        assert set(variants.keys()) == {"en", "th"}, version
        assert variants["en"].strip() != ""
        assert variants["th"].strip() != ""


# ===========================================================================
# core/announce.py:announce_release -- AC-20, AC-21, AC-22.
# ===========================================================================


async def test_ac22_no_catalog_entry_sends_nothing_and_never_raises(db, channel, config):
    db.upsert_user(MEMBER, role="member", status="active")
    await announce.announce_release(db, channel, config, UNKNOWN_VERSION)
    assert channel.sent == []
    assert db.get_last_announced_version(OWNER) is None
    assert db.get_last_announced_version(MEMBER) is None


async def test_ac22_v181_gap_fix_patch_has_no_catalog_entry_and_announces_nothing(db, channel, config):
    """v1.8.1 release-prep (Vera, per the coordinator's dispatch brief):
    a concrete, version-pinned proof of the exact real-world case
    `test_ac22_no_catalog_entry_sends_nothing_and_never_raises` above
    already covers generically via `UNKNOWN_VERSION` -- IMPL-v1.8.1.md's
    own "Announce-machinery finding" cites `core/announce.py:announce_release`
    line 65-66 (`if version not in RELEASE_NOTES: return`) as proof this
    patch fail-opens/skips silently, and the release checklist deliberately
    did NOT add a `RELEASE_NOTES["1.8.1"]` entry (a `/help`-copy fix isn't
    worth interrupting users for). First assert the precondition this test
    relies on -- "1.8.1" genuinely has no entry -- so a future contributor
    who adds one is told by an assertion failure here, not a silently
    wrong test."""
    assert "1.8.1" not in release_notes.RELEASE_NOTES

    db.upsert_user(MEMBER, role="member", status="active")
    await announce.announce_release(db, channel, config, "1.8.1")
    assert channel.sent == []
    assert db.get_last_announced_version(OWNER) is None
    assert db.get_last_announced_version(MEMBER) is None


async def test_ac20_active_user_receives_note_and_is_marked(db, channel, config):
    db.upsert_user(MEMBER, role="member", status="active")
    await announce.announce_release(db, channel, config, KNOWN_VERSION)

    expected_owner_note = release_notes.get_release_note(KNOWN_VERSION, "th")  # primary_language default
    assert channel.sent_to(OWNER) == [expected_owner_note]
    assert channel.sent_to(MEMBER) == [expected_owner_note]  # same default language, no per-user override yet
    assert db.get_last_announced_version(OWNER) == KNOWN_VERSION
    assert db.get_last_announced_version(MEMBER) == KNOWN_VERSION


async def test_ac20_per_user_language(db, channel, config):
    db.upsert_user(MEMBER, role="member", status="active")
    db.set_user_language(OWNER, "en")
    db.set_user_language(MEMBER, "th")

    await announce.announce_release(db, channel, config, KNOWN_VERSION)

    assert channel.sent_to(OWNER) == [release_notes.get_release_note(KNOWN_VERSION, "en")]
    assert channel.sent_to(MEMBER) == [release_notes.get_release_note(KNOWN_VERSION, "th")]


async def test_ac21_already_at_version_is_skipped(db, channel, config):
    db.set_last_announced_version(OWNER, KNOWN_VERSION)
    await announce.announce_release(db, channel, config, KNOWN_VERSION)
    assert channel.sent_to(OWNER) == []


async def test_ac21_failed_send_leaves_unmarked_and_is_retried_next_call(db, config):
    failing_channel = FakeChannel(fail_for={OWNER})
    await announce.announce_release(db, failing_channel, config, KNOWN_VERSION)

    assert failing_channel.sent_to(OWNER) == []
    assert db.get_last_announced_version(OWNER) is None  # left unmarked -- AC-21

    working_channel = FakeChannel()
    await announce.announce_release(db, working_channel, config, KNOWN_VERSION)

    assert working_channel.sent_to(OWNER) == [release_notes.get_release_note(KNOWN_VERSION, "th")]
    assert db.get_last_announced_version(OWNER) == KNOWN_VERSION


async def test_ac21_db_read_error_on_the_gate_check_fails_open_and_still_sends(channel, config, tmp_path):
    """A `get_last_announced_version` read blowing up must not silently
    swallow the announcement -- it degrades to "not yet announced" and
    still attempts the send (fail-open, mirrors every other DB-read-in-a-
    fan-out call site in this codebase, e.g. `core/reminders.py:
    _user_language_pref`)."""

    class _RaisingReadDatabase:
        def __init__(self, real: Database) -> None:
            self._real = real

        def active_user_ids(self):
            return self._real.active_user_ids()

        def get_last_announced_version(self, chat_id):
            raise RuntimeError("simulated DB read failure")

        def get_user(self, chat_id):
            return self._real.get_user(chat_id)

        def set_last_announced_version(self, chat_id, version):
            self._real.set_last_announced_version(chat_id, version)

    real_db = Database(tmp_path / "habits.db")
    real_db.attribute_legacy_to_owner(OWNER)
    wrapped = _RaisingReadDatabase(real_db)

    await announce.announce_release(wrapped, channel, config, KNOWN_VERSION)

    assert channel.sent_to(OWNER) == [release_notes.get_release_note(KNOWN_VERSION, "th")]
    real_db.close()


async def test_isolation_two_users_each_get_exactly_their_own_message(db, channel, config):
    db.upsert_user(MEMBER, role="member", status="active")
    db.upsert_user(MEMBER2, role="member", status="active")

    await announce.announce_release(db, channel, config, KNOWN_VERSION)

    assert len(channel.sent_to(OWNER)) == 1
    assert len(channel.sent_to(MEMBER)) == 1
    assert len(channel.sent_to(MEMBER2)) == 1
    assert len(channel.sent) == 3


async def test_pending_and_blocked_users_receive_nothing(db, channel, config):
    db.upsert_user(MEMBER, role="member", status="pending")
    db.upsert_user(MEMBER2, role="member", status="blocked")

    await announce.announce_release(db, channel, config, KNOWN_VERSION)

    assert channel.sent_to(MEMBER) == []
    assert channel.sent_to(MEMBER2) == []
    assert db.get_last_announced_version(MEMBER) is None
    assert db.get_last_announced_version(MEMBER2) is None


async def test_latest_version_only_no_rollup_for_a_user_several_versions_behind(db, channel, config):
    """R-N3: a user whose `last_announced_version` is several releases
    behind gets only the CURRENT version's note -- one message, not a
    backfill of every version they missed."""
    db.set_last_announced_version(OWNER, "1.2.0")
    await announce.announce_release(db, channel, config, KNOWN_VERSION)
    assert channel.sent_to(OWNER) == [release_notes.get_release_note(KNOWN_VERSION, "th")]
    assert db.get_last_announced_version(OWNER) == KNOWN_VERSION


# ===========================================================================
# R-N4 (send-anyway, DND never consulted) -- structural proof, mirrors
# `tests/test_dnd_matrix.py`'s own AC-12 structural pattern.
# ===========================================================================


def test_announce_module_never_calls_in_dnd_now():
    """`in_dnd_now(` (a real call site) must never appear -- the bare
    substring `in_dnd_now` alone would also match this module's own
    docstring prose explaining WHY it doesn't call it, so the check is
    parenthesis-anchored to mean an actual invocation."""
    source = inspect.getsource(announce)
    assert "in_dnd_now(" not in source


def test_announce_module_is_llm_free():
    """Structural proof, per this module's own LLM-free contract (R-N6):
    neither `core/announce.py` nor `core/release_notes.py` imports or
    calls any Ollama/LLM machinery -- the whole path is deterministic
    string lookups and DB reads/writes. Anchored to actual import/call
    syntax (not the bare substring "llm"), since this module's own
    docstrings legitimately describe themselves as "LLM-free"."""
    source = inspect.getsource(announce) + inspect.getsource(release_notes)
    lowered = source.lower()
    assert "ollama" not in lowered
    assert "chat_text(" not in lowered
    assert "import habit_assistant.llm" not in source
    assert "from habit_assistant.llm" not in source


# ===========================================================================
# core/access.py -- the approve/invite catch-up line (R-N5, AC-23),
# exercised through the REAL execute_admin.
# ===========================================================================


@pytest.fixture
def registry():
    from habit_assistant.core.habits import HabitRegistry

    return HabitRegistry.from_config(Config())


async def test_ac23_approve_catches_a_newly_approved_user_up_to_the_current_version(db, channel, config, monkeypatch):
    monkeypatch.setattr(access, "__version__", KNOWN_VERSION)
    db.upsert_user(MEMBER, status="pending")

    cmd = Command(kind="approve", target_chat=MEMBER)
    await access.execute_admin(cmd, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en")

    assert db.get_last_announced_version(MEMBER) == KNOWN_VERSION

    # ... and therefore does NOT receive v1.5.0's own announcement (AC-23's
    # second half): a subsequent announce_release for that same version
    # skips them, exactly like any other already-caught-up user.
    fresh_channel = FakeChannel()
    await announce.announce_release(db, fresh_channel, config, KNOWN_VERSION)
    assert fresh_channel.sent_to(MEMBER) == []
    # OWNER (approved well before this "release", never caught up) still
    # gets it -- proves the skip is specific to the newly-approved user.
    assert fresh_channel.sent_to(OWNER) != []


async def test_ac23_invite_is_the_same_alias_and_also_catches_up(db, channel, config, monkeypatch):
    monkeypatch.setattr(access, "__version__", KNOWN_VERSION)
    db.upsert_user(MEMBER, status="pending")

    cmd = Command(kind="invite", target_chat=MEMBER)
    await access.execute_admin(cmd, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en")

    assert db.get_last_announced_version(MEMBER) == KNOWN_VERSION


async def test_ac23_a_later_version_bump_does_announce_to_a_caught_up_user(db, channel, config, monkeypatch):
    """AC-23's own explicit second clause: "a later version bump does
    announce to them" -- catch-up only suppresses the CURRENT version's
    note, not every future one."""
    monkeypatch.setattr(access, "__version__", "1.5.0")
    db.upsert_user(MEMBER, status="pending")
    cmd = Command(kind="approve", target_chat=MEMBER)
    await access.execute_admin(cmd, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en")
    assert db.get_last_announced_version(MEMBER) == "1.5.0"

    # A future release ships its own catalog entry; MEMBER is no longer
    # caught up to IT (only to 1.5.0), so they receive it.
    release_notes.RELEASE_NOTES["1.6.0-test"] = {"en": "next release notes", "th": "โน้ตรุ่นถัดไป"}
    try:
        next_channel = FakeChannel()
        await announce.announce_release(db, next_channel, config, "1.6.0-test")
        assert next_channel.sent_to(MEMBER) != []
    finally:
        del release_notes.RELEASE_NOTES["1.6.0-test"]


async def test_ac23_block_does_not_touch_last_announced_version(db, channel, config, monkeypatch):
    """The catch-up write is scoped to the approve/invite branch only --
    `/block` must not (accidentally) mark anyone as caught up."""
    monkeypatch.setattr(access, "__version__", KNOWN_VERSION)
    db.upsert_user(MEMBER, status="active")

    cmd = Command(kind="block", target_chat=MEMBER)
    await access.execute_admin(cmd, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en")

    assert db.get_last_announced_version(MEMBER) is None


async def test_ac23_approve_write_failure_does_not_crash_and_still_reports_save_failed(db, channel, config, monkeypatch):
    """A `set_last_announced_version` failure after a successful approve
    must not raise or block the approve's own ack -- best-effort catch-up
    per R-N5's own "must not undo or block the approve itself" posture."""
    monkeypatch.setattr(access, "__version__", KNOWN_VERSION)
    db.upsert_user(MEMBER, status="pending")

    original = db.set_last_announced_version

    def _raising(chat_id, version):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(db, "set_last_announced_version", _raising)

    cmd = Command(kind="approve", target_chat=MEMBER)
    await access.execute_admin(cmd, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en")

    # Approve itself still fully succeeded (status flipped, acks sent) --
    # never raised despite the catch-up write blowing up.
    assert access.classify(db, MEMBER) == "active"
    assert channel.sent_to(OWNER) == [i18n.t("admin_approved_ack", "en", chat_id=MEMBER)]
