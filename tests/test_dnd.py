"""SPEC-v1.5.md §4 "Feature 2 -- Do-Not-Disturb" R-D5 (module `checkins`
owns the alias): `/dnd` (Thai `งดรบกวน`) as a PURE alias of `/quiet` --
`core/commands.py`'s `_match_dnd` produces the exact same `Command(kind=
"quiet", pref_value=...)` shape `_match_quiet` already does, so it routes
to the SAME `core/preferences.execute_quiet` / `quiet_hours_json` storage
with zero new plumbing (no parallel mechanism, per SPEC-v1.5.md §1).

Owns AC-13 (SPEC-v1.5.md §11): "/dnd behaves identically to /quiet (same
storage/effect); /help mentions DND + check-ins." `in_dnd_now`/the DND
suppression MATRIX itself (R-D1-R-D4) is the shared surface's own scope,
already tested in `tests/test_dnd_matrix.py` -- this file is scoped purely
to the ALIAS's own shape + effect, mirroring `tests/test_preferences.py`'s
own convention for `/quiet` itself.

**Known scope note** (see IMPL-v1.5-checkins.md's "Known limitations"):
SPEC-v1.5.md §6/§11 lists `core/checkins.py`/`core/commands.py`/
`core/i18n.py` as this module's owned files -- `core/discoverability.py`
(where `/help`'s actual text is assembled) is NOT among them. This mirrors
the EXACT same precedent already on file in `core/discoverability.py`'s own
`build_help_text` (see its "Integration step (IMPL-v1.2-preferences.md's
own documented 'Known limitations' #3)" comment): `/lang`/`/quiet`/`/remind`
copy landed with their OWNING modules, but wiring those lines into
`build_help_text` was a separate, later append. This file therefore proves
the `help_checkin_cmd`/`help_dnd_cmd` COPY exists and is well-formed
(bilingual, catalog-integrity-checked by `tests/test_i18n.py` too); wiring
them into the actual `/help` OUTPUT is integration's job, same as last time.
"""

from __future__ import annotations

from habit_assistant.config import Config
from habit_assistant.core import commands, i18n
from habit_assistant.core.commands import Command
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.preferences import execute_quiet
from habit_assistant.core.reminders import effective_quiet_windows
from habit_assistant.storage.db import Database

import pytest

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())

OWNER = "owner-chat"
MEMBER = "member-chat-b"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "dnd.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(MEMBER, role="member", status="active")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


# ===========================================================================
# dispatch() shape -- /dnd is a PURE alias of /quiet: same Command kind,
# same pref_value shape, for the equivalent /quiet input.
# ===========================================================================


@pytest.mark.parametrize(
    "dnd_text,quiet_text",
    [
        ("/dnd 22:00-07:00", "/quiet 22:00-07:00"),
        ("/dnd 22:00-07:00,10:00-11:00", "/quiet 22:00-07:00,10:00-11:00"),
        ("/dnd off", "/quiet off"),
        ("/DND OFF", "/QUIET OFF"),
        ("งดรบกวน 22:00-07:00", "เงียบ 22:00-07:00"),
        ("งดรบกวน off", "เงียบ off"),
    ],
)
def test_dispatch_dnd_produces_the_identical_command_a_quiet_input_would(dnd_text, quiet_text):
    dnd_command = commands.dispatch(dnd_text, DEFAULT_REGISTRY)
    quiet_command = commands.dispatch(quiet_text, DEFAULT_REGISTRY)
    assert dnd_command == quiet_command
    assert dnd_command.kind == "quiet"  # no separate "dnd" CommandKind exists


def test_dispatch_bare_dnd_is_a_usage_shape_same_as_bare_quiet():
    assert commands.dispatch("/dnd", DEFAULT_REGISTRY) == Command(kind="quiet", pref_value=None)


def test_dispatch_bare_thai_dnd_alias_does_not_match():
    assert commands.dispatch("งดรบกวน", DEFAULT_REGISTRY) is None


def test_dispatch_dnd_carries_through_a_malformed_window_unvalidated():
    assert commands.dispatch("/dnd 25:99", DEFAULT_REGISTRY) == Command(kind="quiet", pref_value="25:99")


# ===========================================================================
# adversarial corpus -- งดรบกวน mirrors เงียบ's own hardening (mandatory
# value, shape-whitelisted) -- an ordinary sentence must fall through.
# ===========================================================================

DND_ADVERSARIAL_CORPUS = [
    "งดรบกวนหน่อยนะ",  # glued continuation, no space
    "please do not disturb me",  # English prose, not the Thai trigger
    "dndnzone",  # a random glued word containing "dnd" as a substring
]


@pytest.mark.parametrize("message", DND_ADVERSARIAL_CORPUS)
def test_dispatch_returns_none_for_dnd_adversarial_corpus(message):
    assert commands.dispatch(message, DEFAULT_REGISTRY) is None


# ===========================================================================
# AC-13 -- behavioral: /dnd's effect through execute_quiet is byte-identical
# to /quiet's own (same storage column, same reply catalog ids).
# ===========================================================================


async def test_ac13_dnd_window_writes_the_same_quiet_hours_json_as_quiet_would(db, config):
    dnd_command = commands.dispatch("/dnd 22:00-07:00", DEFAULT_REGISTRY)
    reply = await execute_quiet(dnd_command, db=db, lang="en", user_id=MEMBER)

    assert effective_quiet_windows(db, config, MEMBER) == [("22:00", "07:00")]
    assert reply == i18n.t("quiet_set", "en", windows="22:00-07:00")  # /quiet's own reply id, reused verbatim


async def test_ac13_dnd_off_clears_exactly_like_quiet_off(db, config):
    await execute_quiet(commands.dispatch("/dnd 22:00-07:00", DEFAULT_REGISTRY), db=db, lang="en", user_id=MEMBER)
    reply = await execute_quiet(commands.dispatch("/dnd off", DEFAULT_REGISTRY), db=db, lang="en", user_id=MEMBER)

    assert db.get_user(MEMBER)["quiet_hours_json"] == "[]"
    assert effective_quiet_windows(db, config, MEMBER) == []
    assert reply == i18n.t("quiet_cleared", "en")


async def test_ac13_dnd_thai_alias_sets_the_same_windows_as_the_thai_quiet_alias_would(db, config):
    dnd_reply = await execute_quiet(commands.dispatch("งดรบกวน 22:00-07:00", DEFAULT_REGISTRY), db=db, lang="en", user_id=MEMBER)
    assert effective_quiet_windows(db, config, MEMBER) == [("22:00", "07:00")]
    assert dnd_reply == i18n.t("quiet_set", "en", windows="22:00-07:00")


async def test_ac13_dnd_scoped_to_the_setting_user_only(db, config):
    await execute_quiet(commands.dispatch("/dnd 22:00-07:00", DEFAULT_REGISTRY), db=db, lang="en", user_id=MEMBER)

    assert effective_quiet_windows(db, config, MEMBER) == [("22:00", "07:00")]
    assert effective_quiet_windows(db, config, OWNER) == config.quiet_hours.windows  # unaffected


async def test_ac13_dnd_invalid_window_reports_quiets_own_usage_reply_and_writes_nothing(db):
    reply = await execute_quiet(commands.dispatch("/dnd 25:99", DEFAULT_REGISTRY), db=db, lang="en", user_id=MEMBER)

    assert db.get_user(MEMBER)["quiet_hours_json"] is None
    assert reply == i18n.t("quiet_invalid_window", "en")


async def test_ac13_bare_dnd_reports_quiets_own_usage_reply(db):
    reply = await execute_quiet(commands.dispatch("/dnd", DEFAULT_REGISTRY), db=db, lang="en", user_id=MEMBER)
    assert reply == i18n.t("quiet_usage", "en")


# ===========================================================================
# AC-13 -- /help mentions DND + check-ins: the catalog copy exists and is
# well-formed (bilingual). See this file's own module docstring for why
# wiring it into build_help_text's actual OUTPUT is integration's job, not
# tested here.
# ===========================================================================


def test_help_checkin_and_dnd_copy_exists_and_is_bilingual():
    assert set(i18n.CATALOG["help_checkin_cmd"]) == {"en", "th"}
    assert set(i18n.CATALOG["help_dnd_cmd"]) == {"en", "th"}
    assert i18n.t("help_checkin_cmd", "en") != i18n.t("help_checkin_cmd", "th")
    assert i18n.t("help_dnd_cmd", "en") != i18n.t("help_dnd_cmd", "th")


def test_help_dnd_copy_mentions_quiet_as_the_underlying_command():
    assert "/quiet" in i18n.t("help_dnd_cmd", "en")


def test_help_checkin_copy_mentions_the_opt_in_default():
    """R-K2/AC-8's own framing: the /help line should communicate that
    check-ins are off by default (opt-in), not on."""
    assert "off by default" in i18n.t("help_checkin_cmd", "en").lower()
