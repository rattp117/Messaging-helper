"""SPEC-v1.3.md §4 "Read surface (module `audit-view`)" -- module tests for
the two ACs this parallel module owns (SPEC-v1.3.md §11): AC-V1, AC-V2.

This module is self-contained (does not touch `main.py` -- routing
`command.kind == "audit"` to `audit_view.render_recent` behind an
`access.classify(...) == "owner"` gate is integration's job, per
SPEC-v1.3.md §11's own module table, exactly like `access`/`preferences`/
`schedules` before it), so tests exercise `core/commands.py:dispatch`'s new
`"audit"` kind and `core/audit_view.render_recent` directly, against a real
on-disk SQLite DB (tmp_path) -- no mocks for the DB, mirroring
tests/test_access.py's/tests/test_preferences.py's own convention.

The "owner-gating" tests below simulate the exact wiring IMPL-v1.3-view.md
hands to integration (`access.classify(db, chat_id) == "owner"` gates the
call to `render_recent`) -- proving the composition is correct ahead of
`main.py`'s own wiring, the same way test_preferences.py/test_schedules.py
prove their execute_* functions correct before their own integration step
lands.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime

import pytest

from habit_assistant.config import Config
from habit_assistant.core import access, audit_view, commands, i18n
from habit_assistant.core.audit import ACTIONS, record
from habit_assistant.core.commands import Command
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import AuditEntry

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())

OWNER = "1574572064"
MEMBER = "88899900"
STRANGER = "55544433"


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
# commands.dispatch -- shape recognition for the new "audit" kind.
# ===========================================================================


@pytest.mark.parametrize(
    "text,expected_limit",
    [
        ("/audit", None),
        ("/audit 5", 5),
        ("/AUDIT 5", 5),  # case-insensitive slash trigger, mirrors every other slash command
        ("/audit  5", 5),  # extra whitespace tolerated
        ("/audit abc", None),  # AC-V2/§3.3: non-numeric N falls back to the default (None here)
        ("/audit 999", 999),  # capping to 50 is render_recent's job, not dispatch's
        ("/audit 0", 0),
        ("/audit -5", None),  # a leading "-" isn't a bare digit token -> falls back
        ("ประวัติ", None),
        ("ประวัติ 3", 3),
        ("ประวัติ 0", 0),
    ],
)
def test_dispatch_recognizes_audit_shape(text, expected_limit):
    assert commands.dispatch(text, DEFAULT_REGISTRY) == Command(kind="audit", limit=expected_limit)


def test_dispatch_thai_alias_with_non_numeric_tail_does_not_match():
    """Unlike the slash form (fully permissive tail), the Thai alias only
    recognizes a bare word or a PURELY numeric tail -- anything else falls
    through to None entirely (never a "audit" Command with a garbage
    limit), per the conservative "ordinary Thai word" posture documented
    in core/commands.py's own v1.3.0 docstring section."""
    assert commands.dispatch("ประวัติ abc", DEFAULT_REGISTRY) is None


# ===========================================================================
# Adversarial corpus -- ordinary logs/other commands/Thai prose that merely
# contains "audit"/"ประวัติ" must never dispatch as "audit" (AC5.5's
# zero-false-positive contract, applied to this kind).
# ===========================================================================

THAI_ALIAS_FALSE_POSITIVE_CASES = [
    "ประวัติศาสตร์ไทยน่าสนใจ",  # "history" [the school subject] -- glued continuation, no space
    "เขาเขียนประวัติส่วนตัวไว้",  # "he wrote [his] personal history/bio" -- "ประวัติ" mid-sentence
    "ประวัติของบริษัทนี้ยาวมาก",  # "this company's history is very long" -- glued continuation
    "อยากรู้ประวัติของคุณ",  # "[I] want to know your history" -- mid-sentence, not anchored at start
]

# NOTE: bare "ประวัติ" (no tail) is deliberately NOT in this corpus -- it
# IS a recognized "audit" shape (tested above, `test_dispatch_recognizes_
# audit_shape`), not a false positive.
ADVERSARIAL_MESSAGES = [
    "ดื่มน้ำ 2 แก้ว",
    "500ml",
    "did 10 min stretch",
    "please audit the logs",  # contains "audit", not "/audit"
    "I need to audit my finances this year",
    "auditorium",  # contains "audit" as a substring, glued to more letters
    "/target water 2000",
    "/help",
    "/users",
    *THAI_ALIAS_FALSE_POSITIVE_CASES,
]


@pytest.mark.parametrize("message", ADVERSARIAL_MESSAGES)
def test_adversarial_corpus_never_dispatches_as_audit(message):
    command = commands.dispatch(message, DEFAULT_REGISTRY)
    assert command is None or command.kind != "audit"


@pytest.mark.parametrize("message", THAI_ALIAS_FALSE_POSITIVE_CASES)
def test_thai_alias_does_not_misfire_on_common_prose(message):
    """Every case here starts with the literal characters "ประวัติ" --
    proving the whole-message anchor (not a prefix/substring match) is
    what keeps them from misfiring, mirroring `_HABITS_RE`'s/`_HELP_RE`'s
    own "ordinary Thai word" precedent."""
    assert commands.dispatch(message, DEFAULT_REGISTRY) is None


# ===========================================================================
# render_recent -- newest-first, bilingual, humane formatting (AC-V1).
# ===========================================================================


def _seed_four_rows(db):
    """Chronological insert order (oldest first) -- `db.recent_audit`'s own
    `ORDER BY id DESC` then naturally yields newest-first, matching how
    real capture sites insert as actions actually happen."""
    db.upsert_user(MEMBER, role="member", status="active", display_name="Bob")
    record(
        db,
        actor=OWNER,
        action="undo",
        source="button",
        entity="water",
        old_value=500.0,
        new_value=None,
        clock=lambda: datetime(2026, 8, 22, 9, 5, 0),
    )
    record(
        db,
        actor=OWNER,
        action="user_approve",
        source="admin",
        entity=None,
        old_value="pending",
        new_value="active",
        target_user_id=MEMBER,
        clock=lambda: datetime(2026, 8, 22, 11, 20, 0),
    )
    record(
        db,
        actor=MEMBER,
        action="remind_set",
        source="command",
        entity="water",
        old_value=["08:00", "12:00", "18:00"],
        new_value=["08:00", "12:00"],
        clock=lambda: datetime(2026, 8, 22, 13, 58, 0),
    )
    record(
        db,
        actor=OWNER,
        action="target_set",
        source="command",
        entity="water",
        old_value=2500.0,
        new_value=2000.0,
        clock=lambda: datetime(2026, 8, 22, 14, 3, 5),
    )


def test_render_recent_newest_first_with_actor_and_action_and_value_rendering(db, config):
    _seed_four_rows(db)
    reply = audit_view.render_recent(db, config, "en", limit=None, owner_chat_id=OWNER)
    lines = reply.splitlines()

    assert lines[0] == i18n.t("audit_header", "en", limit=20)
    # newest first: target_set (14:03) ... undo (09:05) last
    assert "target set" in lines[1]
    assert "14:03" in lines[1]
    assert "2500" in lines[1] and "2000" in lines[1]
    assert " you " in f" {lines[1]} "  # the owner's own row renders as "you"

    assert "reminder times" in lines[2]
    assert "Bob" in lines[2]  # a member's row renders their display_name, not "you"
    assert "[08:00,12:00,18:00]" in lines[2] and "[08:00,12:00]" in lines[2]

    assert "approved" in lines[3]
    assert MEMBER in lines[3]  # target_user_id shown for an admin action (entity is NULL)

    assert "undo" in lines[4]
    assert "500" in lines[4]
    assert lines[4].rstrip().endswith("(button)")


def test_render_recent_thai_language_localizes_labels_and_actor(db, config):
    _seed_four_rows(db)
    reply = audit_view.render_recent(db, config, "th", limit=None, owner_chat_id=OWNER)
    lines = reply.splitlines()

    assert lines[0] == i18n.t("audit_header", "th", limit=20)
    assert "ตั้งเป้าหมาย" in lines[1]  # target_set label, Thai
    assert "คุณ" in lines[1]  # "you" in Thai
    assert "อนุมัติ" in lines[3]  # user_approve label, Thai
    assert "ยกเลิก" in lines[4]  # undo label, Thai


def test_render_recent_actor_falls_back_to_raw_chat_id_when_no_display_name(db, config):
    record(db, actor=STRANGER, action="lang_set", source="command", old_value="auto", new_value="th")
    reply = audit_view.render_recent(db, config, "en", limit=None, owner_chat_id=OWNER)
    assert STRANGER in reply.splitlines()[1]


# ---------------------------------------------------------------------------
# AC-V2: default 20, capped at 50, non-numeric/missing N -> default.
# ---------------------------------------------------------------------------


def test_render_recent_default_limit_is_20(db, config):
    _seed_four_rows(db)
    reply = audit_view.render_recent(db, config, "en", limit=None, owner_chat_id=OWNER)
    assert reply.splitlines()[0] == i18n.t("audit_header", "en", limit=20)


def test_render_recent_honors_an_explicit_limit_within_cap(db, config):
    _seed_four_rows(db)
    reply = audit_view.render_recent(db, config, "en", limit=2, owner_chat_id=OWNER)
    lines = reply.splitlines()
    assert lines[0] == i18n.t("audit_header", "en", limit=2)
    assert len(lines) == 1 + 2  # header + exactly 2 rows


def test_render_recent_caps_a_request_above_50(db, config):
    _seed_four_rows(db)
    reply = audit_view.render_recent(db, config, "en", limit=999, owner_chat_id=OWNER)
    assert reply.splitlines()[0] == i18n.t("audit_header", "en", limit=50)


def test_render_recent_via_full_pipeline_audit_abc_falls_back_to_default(db, config):
    """§3.3: "`/audit abc` (non-numeric N) falls back to the default
    limit" -- proven end-to-end, dispatch -> render_recent, exactly as
    main.py's future integration step will call it."""
    _seed_four_rows(db)
    command = commands.dispatch("/audit abc", DEFAULT_REGISTRY)
    reply = audit_view.render_recent(db, config, "en", limit=command.limit, owner_chat_id=OWNER)
    assert reply.splitlines()[0] == i18n.t("audit_header", "en", limit=20)


def test_render_recent_limit_zero_shows_the_empty_state(db, config):
    """`/audit 0` is a well-formed request for zero rows -- indistinguishable
    from "nothing recorded yet" in its rendered result (both show
    audit_empty); dispatch itself still recognizes the shape (limit=0),
    proven separately above."""
    _seed_four_rows(db)
    reply = audit_view.render_recent(db, config, "en", limit=0, owner_chat_id=OWNER)
    assert reply == i18n.t("audit_empty", "en")


# ---------------------------------------------------------------------------
# Empty state (§3.2).
# ---------------------------------------------------------------------------


def test_render_recent_empty_audit_log_en(db, config):
    assert audit_view.render_recent(db, config, "en", limit=None, owner_chat_id=OWNER) == i18n.t("audit_empty", "en")


def test_render_recent_empty_audit_log_th(db, config):
    assert audit_view.render_recent(db, config, "th", limit=None, owner_chat_id=OWNER) == i18n.t("audit_empty", "th")


# ---------------------------------------------------------------------------
# Every action in the closed vocabulary renders a distinct, non-raw label
# in both languages (R-V2: "Action/labels localize via core/i18n.py").
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ACTIONS)
def test_every_audit_action_has_a_localized_label_in_both_languages(db, config, action):
    record(db, actor=OWNER, action=action, source="command", entity="water", old_value="a", new_value="b")
    en_reply = audit_view.render_recent(db, config, "en", limit=None, owner_chat_id=OWNER)
    th_reply = audit_view.render_recent(db, config, "th", limit=None, owner_chat_id=OWNER)
    en_line = en_reply.splitlines()[1]
    th_line = th_reply.splitlines()[1]
    # the raw underscored action string (e.g. "target_set") must never leak
    # verbatim -- it always resolves through the i18n catalog to a
    # human-phrased label ("target set"), even where an action's EN label
    # happens to equal the bare action word itself ("undo" -> "undo").
    assert "_" not in en_line.split(" · ")[2]
    # the Thai variant is written entirely in Thai script -- the raw
    # (Latin, underscored-or-not) action string can never legitimately
    # appear in it, so this proves the Thai catalog entry is actually
    # being used rather than a silent English fallback.
    assert action not in th_line


# ---------------------------------------------------------------------------
# LLM-free / works with Ollama down (R-V2/R-V3): render_recent has no
# llm/health_monitor parameter at all and is a plain synchronous function,
# so it structurally cannot await or call an LLM -- the strongest proof
# available at this module's own boundary (main.py's integration step is
# what proves the full end-to-end pipeline, mirroring test_discoverability.
# py's own AC35/AC37 coverage for /help and /habits).
# ---------------------------------------------------------------------------


def test_render_recent_is_synchronous_and_has_no_llm_dependency():
    assert not inspect.iscoroutinefunction(audit_view.render_recent)
    params = inspect.signature(audit_view.render_recent).parameters
    assert "llm" not in params
    assert "health_monitor" not in params


def test_render_recent_works_with_no_ollama_reachable_at_all(db, config):
    """No LLM/health_monitor object is ever constructed in this test at
    all -- if render_recent secretly needed one, this would raise
    NameError/TypeError, not silently pass."""
    _seed_four_rows(db)
    reply = audit_view.render_recent(db, config, "en", limit=None, owner_chat_id=OWNER)
    assert reply.startswith("🧾")


# ===========================================================================
# Owner-gating (R-V3, integration's own AC-V3 -- verified here at the
# composition level ahead of main.py's wiring; see IMPL-v1.3-view.md for
# the exact call main.py's integration step makes).
# ===========================================================================


def _route_audit_like_integration_will(command, *, db, config, lang, chat_id, owner_chat_id):
    """Mirrors the exact wiring documented in IMPL-v1.3-view.md: a
    non-owner gets no reply at all (silent no-op, `None`); the owner gets
    the rendered string."""
    if access.classify(db, chat_id) != "owner":
        return None
    return audit_view.render_recent(db, config, lang, limit=command.limit, owner_chat_id=owner_chat_id)


def test_owner_gating_owner_gets_a_reply(db, config):
    _seed_four_rows(db)
    command = commands.dispatch("/audit", DEFAULT_REGISTRY)
    reply = _route_audit_like_integration_will(command, db=db, config=config, lang="en", chat_id=OWNER, owner_chat_id=OWNER)
    assert reply is not None
    assert reply.startswith("🧾")


@pytest.mark.parametrize("chat_id", [MEMBER, STRANGER])
def test_owner_gating_non_owner_gets_silent_no_op(db, config, chat_id):
    db.upsert_user(MEMBER, role="member", status="active")
    _seed_four_rows(db)
    command = commands.dispatch("/audit", DEFAULT_REGISTRY)
    reply = _route_audit_like_integration_will(command, db=db, config=config, lang="en", chat_id=chat_id, owner_chat_id=OWNER)
    assert reply is None


def test_owner_gating_reveals_nothing_even_when_activity_exists(db, config):
    """A non-owner's `/audit` must not leak even a hint that activity
    exists (§3.5's "reveals nothing" posture, shared with /approve/
    /block//users) -- the gate short-circuits before render_recent (and
    therefore before any DB read of audit_log) is ever reached for a
    non-owner."""
    _seed_four_rows(db)
    command = commands.dispatch("/audit", DEFAULT_REGISTRY)
    reply = _route_audit_like_integration_will(
        command, db=db, config=config, lang="en", chat_id=STRANGER, owner_chat_id=OWNER
    )
    assert reply is None


# ===========================================================================
# Vera (TEST-v1.3-view.md) -- additional adversarial/robustness coverage
# beyond Luna's own 58 tests above.
# ===========================================================================


# ---------------------------------------------------------------------------
# Extended Thai-alias discipline: "ประวัติ" is an ordinary word ("history"/
# "record"/"profile") that opens real prose. Beyond the false-positive
# corpus Luna already covers, these probe numeric-LOOKING tails that are
# NOT purely-digits-to-end-of-message, and non-word-forming continuations.
# ---------------------------------------------------------------------------

EXTENDED_THAI_ALIAS_FALSE_POSITIVE_CASES = [
    "ประวัติ 20 คน",  # "[a] history/record of 20 people" -- digit token followed by more prose
    "ประวัติ การนอนของฉันเป็นยังไง",  # "what's my sleep history like?" -- a real question about ME, not a command
    "ประวัติ 5 ปีที่แล้ว",  # "history, 5 years ago" -- digit token followed by more prose
    "ประวัติๆ",  # mai yamok (repetition mark) glued on -- not a bare-word match
    "ประวัติฯ",  # paiyannoi (abbreviation mark) glued on -- not a bare-word match
]


@pytest.mark.parametrize("message", EXTENDED_THAI_ALIAS_FALSE_POSITIVE_CASES)
def test_extended_thai_alias_prose_never_dispatches_as_audit(message):
    """The Thai-alias regex is anchored to the WHOLE stripped message with
    only a purely-numeric optional tail (`^ประวัติ(?:\\s+\\d+)?$`) -- a
    digit token followed by MORE text ("20 คน", "5 ปีที่แล้ว") must still
    fail the anchor and fall through to None, exactly like ordinary prose.
    A false positive here would mean asking "what's my [sleep] history"
    silently gets intercepted as the owner-only /audit admin command."""
    assert commands.dispatch(message, DEFAULT_REGISTRY) is None


def test_dispatch_audit_tab_whitespace_tolerated():
    """`\\s+` in both the slash and Thai-alias regexes matches a tab, not
    just a literal space."""
    assert commands.dispatch("/audit\t7", DEFAULT_REGISTRY) == Command(kind="audit", limit=7)


def test_dispatch_thai_alias_double_space_tolerated():
    assert commands.dispatch("ประวัติ  5", DEFAULT_REGISTRY) == Command(kind="audit", limit=5)


def test_dispatch_audit_no_false_positive_on_glued_suffix():
    """"/audits"/"/audit5" -- extra characters glued directly onto "audit"
    with no separating whitespace before them -- must NOT be recognized
    as the audit command (mirrors the "auditorium" false-positive
    discipline already proven in the adversarial corpus above, at the
    slash-form's own word boundary)."""
    assert commands.dispatch("/audits", DEFAULT_REGISTRY) is None
    assert commands.dispatch("/audit5", DEFAULT_REGISTRY) is None


# ---------------------------------------------------------------------------
# Rendering robustness: NULL columns, vocabulary drift, lookup failures --
# a read-only view must degrade gracefully, never crash, on any row shape.
# ---------------------------------------------------------------------------


def test_render_recent_entity_and_target_user_both_null_renders_bare_change(db, config):
    """A lang_set/quiet_set-shaped row (SPEC-v1.3.md §2.1: `entity=NULL`,
    no `target_user_id`) must render just the "old -> new" change, with no
    dangling "None ·" prefix and no crash -- `_detail`'s `entity or
    target_user_id` both being falsy is a real, spec-documented row shape,
    not a hypothetical."""
    record(db, actor=OWNER, action="lang_set", source="command", entity=None, old_value="auto", new_value="th")
    reply = audit_view.render_recent(db, config, "en", limit=None, owner_chat_id=OWNER)
    line = reply.splitlines()[1]
    assert "auto → th" in line
    assert "None" not in line


def test_render_recent_unknown_action_renders_raw_string_without_crashing(db, config):
    """Vocabulary drift (audit_view.py's own docstring: "an action
    recorded under a value not in this map still renders ... rather than
    raising"): a row whose `action` is outside `core/audit.py:ACTIONS`
    (e.g. written by a future recorder version, or a hand-edited row)
    must still render -- falling back to the raw action string -- instead
    of KeyError/crashing the whole /audit reply over one bad row."""
    entry = AuditEntry(
        id=None,
        ts="2026-08-22T10:00:00",
        user_id=OWNER,
        action="some_future_action_v2",
        entity="water",
        old_value="1",
        new_value="2",
        source="command",
        target_user_id=None,
    )
    db.insert_audit(entry)
    reply = audit_view.render_recent(db, config, "en", limit=1, owner_chat_id=OWNER)
    assert "some_future_action_v2" in reply.splitlines()[1]


def test_render_recent_unknown_source_renders_verbatim_without_crashing(db, config):
    """`source` is shown verbatim, never looked up in a closed vocabulary
    (`core/i18n.py`'s own comment: "source ... is likewise shown
    verbatim") -- an unrecognized source string must still render rather
    than crash."""
    entry = AuditEntry(
        id=None,
        ts="2026-08-22T10:01:00",
        user_id=OWNER,
        action="undo",
        entity="water",
        old_value="1",
        new_value=None,
        source="future_source",
        target_user_id=None,
    )
    db.insert_audit(entry)
    reply = audit_view.render_recent(db, config, "en", limit=1, owner_chat_id=OWNER)
    assert reply.splitlines()[1].rstrip().endswith("(future_source)")


def test_render_recent_actor_lookup_failure_falls_back_to_chat_id(db, config, monkeypatch):
    """`_actor_display`'s own fail-open contract: if `db.get_user` raises
    for any reason (corrupt row, DB locked), the view must fall back to
    the raw chat id rather than propagating the exception and breaking
    the entire /audit reply over one actor lookup."""
    record(db, actor=STRANGER, action="lang_set", source="command", old_value="en", new_value="th")

    def _raise(user_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(db, "get_user", _raise)
    reply = audit_view.render_recent(db, config, "en", limit=None, owner_chat_id=OWNER)
    assert STRANGER in reply.splitlines()[1]


def test_render_recent_timestamp_uses_arabic_numerals_in_thai_too(db, config):
    """Thai localization touches only the label/actor words, never the
    timestamp digits or timezone -- `ts` is already local wall-clock text
    (R-M1) and `_format_ts` only reformats it, so the TH rendering must
    show the identical "MM-DD HH:MM" as EN, not Thai numerals or a
    shifted time."""
    record(
        db,
        actor=OWNER,
        action="undo",
        source="command",
        entity="water",
        old_value=1.0,
        new_value=None,
        clock=lambda: datetime(2026, 8, 22, 14, 3, 0),
    )
    reply_th = audit_view.render_recent(db, config, "th", limit=None, owner_chat_id=OWNER)
    assert "08-22 14:03" in reply_th.splitlines()[1]


def test_render_recent_negative_limit_bypassing_dispatch_is_treated_as_unlimited(db, config):
    """ADVISORY (defense-in-depth), not an AC-V2 violation: `commands.
    dispatch` never produces a negative `limit` -- a leading "-" fails the
    `\\d+` shape (see "/audit -5" -> None in the parametrized shape test
    above), so this path is unreachable from a real Telegram message
    today. But `render_recent`'s own public signature accepts any `int`,
    and `_effective_limit`'s `min(limit, MAX_LIMIT)` never clamps a LOWER
    bound -- a negative int reaches `db.recent_audit` unchanged, where
    SQLite's `LIMIT -N` means "no limit" (documented SQLite behavior), so
    ALL rows are returned instead of zero or an error. Pinning the current
    behavior here so a future caller (e.g. a config-driven limit) doesn't
    get silently surprised; recommend `_effective_limit` also clamp a
    floor of 0 for defense-in-depth, though nothing in today's product
    surface can trigger it."""
    for i in range(5):
        record(
            db,
            actor=OWNER,
            action="undo",
            source="command",
            entity="water",
            old_value=float(i),
            new_value=None,
            clock=lambda i=i: datetime(2026, 8, 22, 9, 0, i),
        )
    reply = audit_view.render_recent(db, config, "en", limit=-5, owner_chat_id=OWNER)
    assert len(reply.splitlines()) - 1 == 5  # all 5 rows, not zero -- pins the current SQLite-driven behavior


# ---------------------------------------------------------------------------
# Message-length robustness: Telegram's sendMessage hard-caps at 4096
# characters (src/habit_assistant/channels/telegram.py:send posts `text`
# as-is with no length check -- a message over the cap gets a 400 from
# Telegram's API). Neither render_recent nor any call site truncates a
# long old/new value or chunks the reply into multiple sends.
# ---------------------------------------------------------------------------


def test_render_recent_typical_50_rows_stays_within_telegram_limit(db, config):
    """A realistic MIX of short-valued actions (the common case: target
    tweaks, undos, a language change, a couple of short remind edits) at
    the 50-row cap stays comfortably under Telegram's 4096-char
    sendMessage limit -- confirms ordinary usage is fine; the next test
    shows where it stops being fine."""
    actions = [
        dict(action="target_set", entity="water", old_value=2500.0, new_value=2000.0),
        dict(action="undo", entity="water", old_value=500.0, new_value=None),
        dict(action="remind_set", entity="water", old_value=["08:00", "12:00"], new_value=["08:00", "12:00", "18:00"]),
        dict(action="lang_set", old_value="auto", new_value="th"),
        dict(action="quiet_set", old_value=None, new_value=["22:00-07:00"]),
    ]
    for i in range(50):
        a = actions[i % len(actions)]
        record(
            db,
            actor=MEMBER,
            action=a["action"],
            source="command",
            entity=a.get("entity"),
            old_value=a.get("old_value"),
            new_value=a.get("new_value"),
            clock=lambda i=i: datetime(2026, 8, 22, 9, 0, i % 60),
        )
    reply = audit_view.render_recent(db, config, "en", limit=50, owner_chat_id=OWNER)
    assert len(reply) <= 4096


def test_render_recent_50_rows_of_realistic_remind_edits_exceeds_telegram_limit(db, config):
    """FINDING (TEST-v1.3-view.md): a plausible, everyday scenario -- 50
    remind_set edits, each with an ordinary 4-time schedule (well under
    `schedules.py`'s own `MAX_REMINDER_TIMES=24` cap, nowhere near a
    pathological input) -- already produces a reply LONGER than
    Telegram's 4096-char sendMessage limit. Neither `render_recent` (no
    per-value truncation) nor any call site (`core/commands.py`, `main.
    py`'s future integration) chunks or splits the message.
    SPEC-v1.3.md §9 states "the viewer truncates the value display" as an
    existing behavior; it is not implemented. A household with a couple
    of members who tweak their reminder schedule over time would make
    /audit's real `sendMessage` call fail with Telegram's "message is too
    long" 400 error the first time 50 such rows are in the window.

    Expected to FAIL until Luna adds either per-value truncation (cap a
    rendered old/new string's length) or reply chunking (split into
    multiple sendMessage calls) to close this gap."""
    times_a = ["07:00", "12:00", "18:00", "21:00"]
    times_b = ["07:00", "12:30", "18:00", "21:30"]
    for i in range(50):
        record(
            db,
            actor=MEMBER,
            action="remind_set",
            source="command",
            entity="water",
            old_value=times_a,
            new_value=times_b,
            clock=lambda i=i: datetime(2026, 8, 22, 9, 0, i % 60),
        )
    reply = audit_view.render_recent(db, config, "en", limit=50, owner_chat_id=OWNER)
    assert len(reply) <= 4096, (
        f"/audit 50 produced a {len(reply)}-char message, over Telegram's 4096-char "
        "sendMessage limit -- see TEST-v1.3-view.md for the finding"
    )


# ===========================================================================
# Luna (IMPL-v1.3-view.md iteration log) -- fix for TEST-v1.3-view.md's
# finding above: per-value truncation (`_humanize_stored_value`) plus a
# STRUCTURAL total-length guard (`_fit_within_budget`) that drops the
# oldest shown rows and appends a bilingual "N more" footer whenever the
# fully-rendered message would exceed Telegram's limit -- regardless of
# WHY (many short rows, one long value, or a combination), so this is a
# genuine worst-case bound, not a fix tuned to Vera's one reproduction.
# ===========================================================================


def test_render_recent_worst_case_pathological_rows_still_stays_within_telegram_limit(db, config):
    """The actual structural guarantee: 50 rows, EACH at the worst
    realistic value shape simultaneously -- a full `MAX_REMINDER_TIMES=24`
    schedule for both old AND new, a long member chat id as actor, and
    Thai localization (the language whose action labels run longest) --
    still fits under Telegram's 4096-char `sendMessage` limit. This is
    considerably more extreme than Vera's own reproduction (a realistic
    4-time schedule), proving the fix is a genuine bound rather than a
    value tuned to just barely pass the one finding."""
    long_actor = "5000000001"  # a long-but-plausible Telegram chat id
    db.upsert_user(long_actor, role="member", status="active")
    times_old = [f"{h:02d}:00" for h in range(24)]
    times_new = [f"{h:02d}:30" for h in range(24)]
    for i in range(50):
        record(
            db,
            actor=long_actor,
            action="remind_set",
            source="command",
            entity="water",
            old_value=times_old,
            new_value=times_new,
            clock=lambda i=i: datetime(2026, 8, 22, 9, 0, i % 60),
        )
    for lang in ("en", "th"):
        reply = audit_view.render_recent(db, config, lang, limit=50, owner_chat_id=OWNER)
        assert len(reply) <= 4096, f"[{lang}] pathological 50-row /audit produced {len(reply)} chars"
        # the footer must actually explain the shortfall, not silently
        # drop rows with no indication anything was omitted.
        assert "…" in reply.splitlines()[-1]


def test_fit_within_budget_footer_reports_the_correct_dropped_count(db, config):
    """When rows must be dropped, the "N more" footer's count matches
    exactly how many of the 50 requested rows were actually omitted --
    not an approximation, and the kept rows are still the NEWEST ones
    (the guard drops from the tail of a newest-first list)."""
    times_old = [f"{h:02d}:00" for h in range(24)]
    times_new = [f"{h:02d}:30" for h in range(24)]
    for i in range(50):
        record(
            db,
            actor=MEMBER,
            action="remind_set",
            source="command",
            entity="water",
            old_value=times_old,
            new_value=times_new,
            clock=lambda i=i: datetime(2026, 8, 22, 9, 0, i % 60),
        )
    reply = audit_view.render_recent(db, config, "en", limit=50, owner_chat_id=OWNER)
    lines = reply.splitlines()
    shown_rows = [line for line in lines if line.startswith("•")]
    footer = lines[-1]
    assert footer == i18n.t("audit_more_rows", "en", count=50 - len(shown_rows))
    assert len(shown_rows) < 50  # confirms this scenario actually exercises the drop path


def test_humanize_stored_value_truncates_a_long_scalar_string():
    long_value = "x" * 200
    result = audit_view._humanize_stored_value(long_value)
    assert len(result) == audit_view._MAX_VALUE_CHARS
    assert result.endswith("…")


def test_humanize_stored_value_truncates_a_long_json_list():
    long_list_json = json.dumps([f"{h:02d}:00" for h in range(24)])
    result = audit_view._humanize_stored_value(long_list_json)
    assert len(result) == audit_view._MAX_VALUE_CHARS
    assert result.endswith("…")


def test_humanize_stored_value_leaves_a_short_value_untouched():
    """No regression for the common case -- Luna's/Vera's existing
    rendering tests above already pin exact short-value output; this just
    confirms the truncation threshold doesn't kick in below it."""
    assert audit_view._humanize_stored_value("2000") == "2000"
    assert audit_view._humanize_stored_value(json.dumps(["08:00", "12:00"])) == "[08:00,12:00]"


# ===========================================================================
# Vera (TEST-v1.3-view.md follow-up) -- three specific re-verification
# probes the coordinator asked for: (1) a single row whose one UNTRUNCATED
# field (actor display_name -- _humanize_stored_value's 60-char cap only
# covers old/new, never actor) alone would blow the budget even after
# per-value truncation; (2) footer accuracy when the drop loop empties
# `kept` all the way to zero; (3) Thai combining-mark safety at the
# truncation boundary.
# ===========================================================================


def test_render_recent_single_row_with_pathological_actor_name_still_fits_budget(db, config):
    """`_actor_display` returns a stored `display_name` VERBATIM --
    unlike old_value/new_value, it never goes through `_humanize_stored_
    value`'s 60-char `_truncate`. A single row whose actor alone is huge
    (unreachable via real Telegram data -- Telegram caps first/last name
    at 64 chars each -- but nothing in `db.upsert_user`/this module
    enforces that at the DB layer) must still be handled: `_fit_within_
    budget`'s "drop the oldest kept row" loop has to be able to drop the
    row down to a bare header+footer even when a SINGLE row is the entire
    dataset, not just when there are many rows to shed."""
    huge_actor = "999999999"
    db.upsert_user(huge_actor, role="member", status="active", display_name="A" * 6000)
    record(db, actor=huge_actor, action="lang_set", source="command", old_value="en", new_value="th")
    reply = audit_view.render_recent(db, config, "en", limit=50, owner_chat_id=OWNER)
    assert len(reply) <= 4096
    # the pathological row itself must be gone, not silently half-rendered
    assert "A" * 100 not in reply
    assert reply.splitlines()[-1] == i18n.t("audit_more_rows", "en", count=1)


def test_render_recent_all_rows_dropped_footer_count_matches_total(db, config):
    """The "exactly 0 rows fit" case: every row is individually oversized
    (same huge-actor construction as above, repeated), so the drop loop
    empties `kept` all the way to zero. The footer's count must still be
    exactly the number of rows that were actually fetched/dropped (5, not
    an off-by-one or a stale count from a partial drop), and the reply
    must still be a single well-formed, budget-fitting message -- not a
    crash, not an infinite loop."""
    huge_actor = "999999998"
    db.upsert_user(huge_actor, role="member", status="active", display_name="B" * 6000)
    for i in range(5):
        record(
            db, actor=huge_actor, action="lang_set", source="command", old_value="en", new_value="th",
            clock=lambda i=i: datetime(2026, 8, 22, 9, 0, i),
        )
    reply = audit_view.render_recent(db, config, "en", limit=50, owner_chat_id=OWNER)
    lines = reply.splitlines()
    assert len(lines) == 2  # header + footer only, zero row lines survived
    assert lines[1] == i18n.t("audit_more_rows", "en", count=5)
    assert len(reply) <= 4096


def test_truncate_thai_combining_mark_at_boundary_produces_valid_unicode():
    """Worst-case grapheme-cluster boundary: construct a value where the
    cut lands exactly between a Thai base consonant (kept) and its
    combining tone mark (dropped) -- Python's `str` slices by codepoint,
    never by UTF-8 byte, so this can NEVER produce an invalid UTF-8
    sequence, a lone surrogate, or a U+FFFD replacement character
    (mojibake) regardless of where the cut lands; the only possible
    artifact is a dropped diacritic (cosmetic), never corruption. Proven
    directly against `_truncate`, not simulated."""
    base = "ก" * 58
    word = base + "น้ำ"  # index 58 = 'น' (base, kept), index 59 = '้' (Mai Tho, combining, dropped by the cut)
    result = audit_view._truncate(word, max_chars=60)
    assert len(result) == 60
    assert result.endswith("…")
    assert "�" not in result  # no Unicode replacement character anywhere
    # round-trips cleanly through UTF-8 -- proves no broken/lone surrogate
    assert result.encode("utf-8").decode("utf-8") == result
