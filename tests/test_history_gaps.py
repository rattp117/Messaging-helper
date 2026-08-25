"""SPEC-v1.4.md "/history [N]" -- Vera's adversarial follow-up pass.

`tests/test_render_budget.py` (13 tests) and `tests/test_history.py` (52
tests) are Luna's own suite and are NOT duplicated here. This file only
adds coverage for gaps found while auditing v1.4.0 against SPEC-v1.4.md's
acceptance criteria: extraction-contract edges, combined U-ISO scenarios,
matcher-discipline cases Luna's adversarial corpus didn't include, hostile
rendering inputs, and integration paths (access gate, Ollama-down) that
Luna's own integration section didn't cover.

Same conventions as `tests/test_history.py`: real on-disk SQLite
(`tmp_path`), no DB mocks; only Ollama/Telegram are faked, and only in
this file's own integration-level section.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import commands, history_view
from habit_assistant.core.commands import dispatch
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.render_budget import fit_within_budget
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())

OWNER = "1001"
MEMBER = "2002"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


def _seed(db: Database, user_id: str, ts: str, category: str, value_num, raw: str, deleted: bool = False) -> int:
    row_id = db.insert_log(LogEntry(None, user_id, ts, category, value_num, None, raw, "reply"))
    if deleted:
        db.soft_delete(row_id)
    return row_id


# ===========================================================================
# Extraction integrity: fit_within_budget's injected-footer contract
# (AC-3 / R-B1) under a hostile footer renderer.
# ===========================================================================


def test_fit_within_budget_footer_containing_format_braces_renders_literally():
    """The footer is caller-supplied text concatenated via `"\\n".join`,
    never passed through `.format()` -- a footer containing literal
    `{}`/`{0}` must render inertly, not raise."""
    header = "H"
    rows = [f"row-{i:03d}-" + "x" * 90 for i in range(60)]
    result = fit_within_budget(header, rows, render_footer=lambda n: f"… {{n}} more, {{0}} dropped ({n})")
    assert len(result) <= 4096
    assert "{n}" in result and "{0}" in result  # literal, not interpolated


def test_fit_within_budget_with_a_footer_renderer_that_itself_exceeds_budget():
    """Edge of the documented contract: `fit_within_budget` drops rows
    until `header + kept + footer` fits OR `kept` is empty (the `not
    kept` floor). If the FOOTER ALONE (with zero rows kept) already
    exceeds the budget, the floor still returns -- it does not hang or
    raise -- but the result CAN legitimately exceed
    `TELEGRAM_MESSAGE_BUDGET` in that pathological case, since there is
    nothing left to drop. Documented here, not treated as a functional
    bug: no real caller's `render_footer` produces output anywhere near
    this size (both current callers render a short fixed-shape "N more"
    catalog string)."""
    header = "H"
    # Must already overflow the budget with ZERO rows dropped, or the
    # function returns before ever invoking render_footer at all.
    rows = [f"row-{i:03d}-" + "x" * 90 for i in range(60)]
    huge_footer = "F" * 5000
    result = fit_within_budget(header, rows, render_footer=lambda n: huge_footer)
    assert isinstance(result, str)  # completed without raising/hanging
    assert huge_footer in result
    # NOTE: len(result) here is ~5002, i.e. OVER TELEGRAM_MESSAGE_BUDGET --
    # this is the one input shape for which the "always fits" guarantee
    # does not hold. Neither `audit_view` nor `history_view` can trigger
    # it (their footers are short i18n strings), so this is a pinned
    # characterization test, not a failing assertion of a violated AC.


# ===========================================================================
# U-ISO combined with limit exhaustion, category filter, and soft-delete
# (AC-9 / AC-10 / R-D1).
# ===========================================================================


def test_recent_logs_does_not_fill_from_other_users_when_requester_is_exhausted(db):
    """A now requesting more rows than they have must NOT be padded with
    B's rows -- the `LIMIT` applies AFTER the `user_id` filter, not
    before it. B alone has more rows than the requested limit."""
    _seed(db, OWNER, "2026-08-20T09:00:00", "water", 500.0, "owner-1")
    _seed(db, OWNER, "2026-08-21T09:00:00", "water", 500.0, "owner-2")
    for i in range(10):
        _seed(db, MEMBER, f"2026-08-{10 + i:02d}T09:00:00", "water", 500.0, f"member-{i}")

    rows = db.recent_logs(OWNER, 5)
    assert len(rows) == 2
    assert {r["raw_message"] for r in rows} == {"owner-1", "owner-2"}


def test_recent_logs_isolation_combined_with_category_filter_and_soft_delete(db):
    """U-ISO must hold even when BOTH a habit filter and a soft-deleted
    row are in play at once -- the combination Luna's own suite tests
    separately but not together."""
    live_id = _seed(db, OWNER, "2026-08-22T09:00:00", "water", 500.0, "owner-live")
    undone_id = _seed(db, OWNER, "2026-08-21T09:00:00", "water", 300.0, "owner-undone")
    db.soft_delete(undone_id)
    for i in range(3):
        _seed(db, MEMBER, f"2026-08-2{i}T09:00:00", "water", 999.0, f"member-{i}")

    rows = db.recent_logs(OWNER, 10, category="water")
    assert {r["raw_message"] for r in rows} == {"owner-live", "owner-undone"}
    assert all(r["user_id"] == OWNER for r in rows)


# ===========================================================================
# Matcher discipline: shapes not in Luna's adversarial corpus.
# ===========================================================================


def test_dispatch_history_with_trailing_letters_does_not_match_history():
    """"/historys" must NOT be recognized as "/history" -- the anchored
    regex requires either end-of-string or whitespace right after
    "/history"; a glued suffix leaves the `$` anchor unsatisfied."""
    command = dispatch("/historys", DEFAULT_REGISTRY)
    assert command is None or command.kind != "history"


def test_dispatch_history_water_abc_ignores_unrecognized_trailing_token():
    """SPEC-v1.4.md §2.1: a third+ token is ignored, per R-D2's own
    comment ("Any token beyond the first two is ignored")."""
    command = dispatch("/history water abc", DEFAULT_REGISTRY)
    assert command.kind == "history"
    assert command.category == "water"
    assert command.limit is None


def test_dispatch_history_negative_number_is_treated_as_an_unknown_habit_token():
    """"-5" fails `str.isdigit()`, so it is NOT parsed as N -- it falls
    into the habit-token branch, unresolved, carried through raw (mirrors
    "/history coffee"). The view layer reports it as an invalid habit
    (AC-6), not silently as "no limit"."""
    command = dispatch("/history -5", DEFAULT_REGISTRY)
    assert command.kind == "history"
    assert command.category == "-5"
    assert command.limit is None


def test_dispatch_history_zero_is_a_well_formed_limit_not_dropped():
    command = dispatch("/history 0", DEFAULT_REGISTRY)
    assert command.kind == "history"
    assert command.category is None
    assert command.limit == 0


def test_dispatch_history_huge_n_parses_without_overflow():
    command = dispatch("/history 999999999999", DEFAULT_REGISTRY)
    assert command.kind == "history"
    assert command.limit == 999999999999


def test_dispatch_thai_alias_with_an_unregistered_habit_word_falls_through():
    """Asymmetry vs. the slash form, by design (R-D2's comment on the
    Thai alias): the Thai regex's habit group is registry-anchored, so a
    non-habit Thai word after "ย้อนหลัง" leaves the `$` anchor
    unsatisfied and the WHOLE match fails -- unlike "/history coffee",
    which the slash form's permissive tail always turns into a Command
    (letting the view report `history_invalid_habit`). This is the
    correct AC-5 anti-false-positive behavior, not a bug: an unrecognized
    trailing word must not partially match."""
    command = dispatch("ย้อนหลัง กาแฟ", DEFAULT_REGISTRY)
    assert command is None or command.kind != "history"


def test_dispatch_thai_alias_glued_prefix_does_not_match():
    command = dispatch("โปรดย้อนหลัง", DEFAULT_REGISTRY)
    assert command is None or command.kind != "history"


def test_dispatch_thai_alias_not_anchored_at_start_does_not_match():
    command = dispatch("คุณย้อนหลัง 5 นาที", DEFAULT_REGISTRY)
    assert command is None or command.kind != "history"


# ===========================================================================
# Rendering behavior for the matcher-discipline edge cases above
# (AC-6 / AC-7 / R-R1).
# ===========================================================================


def test_render_history_limit_zero_returns_the_empty_message_even_with_rows_present(db, config):
    """SPEC-v1.4.md's own contract (history_view.py docstring): "/history
    0" is a well-formed request for zero rows, indistinguishable in its
    RESULT from "no entries yet" -- both render `history_empty`. Pinned
    here since it's easy to accidentally "fix" into an error later."""
    _seed(db, OWNER, "2026-08-23T09:00:00", "water", 500.0, "500ml")
    reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category=None, limit=0)
    assert reply == "🧾 No entries yet."


def test_render_history_huge_limit_is_capped_and_does_not_crash(db, config):
    """`_effective_limit` caps the requested N at 50 (`min(limit,
    MAX_LIMIT)`) before it ever reaches SQLite -- confirmed here it does
    not overflow/crash on a huge Python int. The header text names the
    EFFECTIVE (capped) LIMIT REQUESTED, not the actual row count returned
    -- e.g. only 1 row exists here, yet the header still says "last 50"
    -- this mirrors `core/audit_view.py:render_recent`'s own identical,
    pre-existing behavior byte-for-byte (its own tests pin
    `audit_header(limit=50)` the same way with fewer actual rows), so
    it's a deliberate parity choice (SPEC-v1.4.md's "mirrors /audit"),
    not a defect introduced by this feature."""
    _seed(db, OWNER, "2026-08-23T09:00:00", "water", 500.0, "500ml")
    reply = history_view.render_history(
        db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category=None, limit=999999999999
    )
    assert reply.splitlines()[0] == "🧾 Your last 50 entries:"
    assert len(reply.splitlines()) == 2  # header + exactly the 1 row that actually exists


# ===========================================================================
# Rendering hostility beyond Luna's control-char/brace/4000-char cases
# (AC-12 / R-R2 risk note).
# ===========================================================================


def test_history_line_with_emoji_zero_width_and_rtl_override_does_not_crash(db, config):
    """Emoji, a zero-width joiner (U+200D), and a Unicode RTL override
    (U+202E) are NOT ASCII control characters (`\\x00-\\x1f`/`\\x7f`), so
    `_sanitize_raw_message`'s collapse regex intentionally leaves them
    untouched -- this test's job is only to confirm they never crash the
    renderer, not that they get stripped."""
    hostile = "went for a run \U0001f3c3‍♂️ ‮GNITROPS‬ today"
    _seed(db, OWNER, "2026-08-23T09:00:00", "water", 500.0, hostile)
    reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category=None, limit=None)
    assert isinstance(reply, str)
    assert len(reply.splitlines()) == 2


def test_history_shows_generic_fallback_for_a_habit_removed_from_the_registry(db, config):
    """SPEC-v1.4.md §9's own documented risk: a historical row whose
    `category` is no longer a configured habit must fall back to
    `describe_log`'s generic branch, not crash. Only reachable when no
    `category` filter is requested -- see the next test for the filtered
    case."""
    _seed(db, OWNER, "2026-08-23T09:00:00", "retired_habit", 3.0, "did the old thing")
    reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category=None, limit=None)
    assert "retired_habit" in reply
    assert "did the old thing" in reply


def test_history_filter_by_a_habit_removed_from_the_registry_is_reported_invalid_not_crashed(db, config):
    """Filtering BY a category that used to exist but is no longer
    configured is NOT treated as "show its historical rows" -- R-D2's
    `registry.get(category) is None` check fires exactly the same as an
    always-unknown habit like "coffee" (AC-6), even though rows with that
    category genuinely exist in the DB. Documented, not a defect: the
    filter vocabulary is the LIVE registry, per R-D2's own contract."""
    _seed(db, OWNER, "2026-08-23T09:00:00", "retired_habit", 3.0, "did the old thing")
    reply = history_view.render_history(
        db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category="retired_habit", limit=None
    )
    assert "retired_habit" in reply
    assert "isn't a habit" in reply  # history_invalid_habit, not the row itself


def test_format_ts_across_the_midnight_boundary(db, config):
    _seed(db, OWNER, "2026-08-22T23:59:30", "water", 500.0, "before midnight")
    _seed(db, OWNER, "2026-08-23T00:00:15", "water", 500.0, "after midnight")
    reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category=None, limit=None)
    lines = reply.splitlines()
    assert "08-23 00:00" in lines[1]  # newest-first: after-midnight row first
    assert "08-22 23:59" in lines[2]


def test_recent_logs_newest_first_stable_for_identical_timestamps(db):
    """Two rows sharing the exact same `ts` must still resolve
    deterministically -- `ORDER BY ts DESC, id DESC` ties on `ts` and
    breaks the tie by insertion order (higher `id` = inserted later =
    shown first)."""
    _seed(db, OWNER, "2026-08-23T09:00:00", "water", 500.0, "first-inserted")
    _seed(db, OWNER, "2026-08-23T09:00:00", "water", 500.0, "second-inserted")
    rows = db.recent_logs(OWNER, 10)
    assert [r["raw_message"] for r in rows] == ["second-inserted", "first-inserted"]


def test_history_unparsed_rows_sandwiched_between_real_entries_leave_them_intact(db, config):
    _seed(db, OWNER, "2026-08-23T09:00:00", "water", 500.0, "real-1")
    _seed(db, OWNER, "2026-08-23T09:30:00", "unparsed", None, "garbled-1")
    _seed(db, OWNER, "2026-08-23T10:00:00", "stretch", 10.0, "real-2")
    _seed(db, OWNER, "2026-08-23T10:30:00", "unparsed", None, "garbled-2")
    _seed(db, OWNER, "2026-08-23T11:00:00", "water", 250.0, "real-3")

    reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category=None, limit=None)
    assert "garbled-1" not in reply and "garbled-2" not in reply
    assert "real-1" in reply and "real-2" in reply and "real-3" in reply
    lines = reply.splitlines()[1:]
    assert len(lines) == 3  # exactly the three real rows, nothing else


# ===========================================================================
# Menu + routing: pending/blocked users get the v1.2 access gate, not
# `/history`; `/history` still works with Ollama reported down, proven at
# the `handle_inbound_message` seam directly (AC-14).
# ===========================================================================


class _RecordingChannel(Channel):
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id, text):
        self.sent.append((chat_id, text))

    async def send_actionable(self, chat_id, text, buttons):
        self.sent.append((chat_id, text))

    async def set_my_commands(self, commands, *, scope_chat_id=None):
        pass

    async def run(self, on_message, on_callback=None):
        raise NotImplementedError

    async def aclose(self):
        pass


class _RaisingLLM:
    async def chat_text(self, system_prompt, user_prompt):
        raise AssertionError("LLM must not be called for a gated or LLM-free /history request")

    async def chat_json(self, *args, **kwargs):
        raise AssertionError("LLM must not be called for a gated or LLM-free /history request")

    async def probe_schema_support(self, *args, **kwargs):
        return {}

    async def aclose(self):
        pass


async def test_pending_user_sending_history_gets_the_access_gate_not_a_history_reply(tmp_path):
    """The access gate (`access.handle_gate`) runs in `on_message`,
    strictly BEFORE `handle_inbound_message` is ever invoked (main.py's
    `on_message` calls `access.handle_gate` first and returns early on
    `False` -- see main.py's own `proceed = await access.handle_gate(...)`
    gate). A pending user's "/history" must never reach
    `history_view.render_history` at all."""
    from habit_assistant.core import access, i18n

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    database = Database(tmp_path / "habits.db")
    database.upsert_user(MEMBER, status="pending")
    database.insert_log(LogEntry(None, MEMBER, "2026-08-23T09:00:00", "water", 500.0, None, "500ml", "reply"))

    channel = _RecordingChannel()
    proceed = await access.handle_gate(
        database, channel, config, owner_chat_id=OWNER, chat_id=MEMBER, display_name=None, text="/history", lang="en"
    )
    database.close()

    assert proceed is False
    assert channel.sent != []
    reply_text = channel.sent[0][1]
    assert reply_text == i18n.t("access_pending", "en")
    assert "🧾" not in reply_text  # not a history reply
    assert "500 ml" not in reply_text  # the member's own logged row never leaked into the gate reply


async def test_blocked_user_sending_history_gets_denied_not_a_history_reply(tmp_path):
    from habit_assistant.core import access, i18n

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    database = Database(tmp_path / "habits.db")
    database.upsert_user(MEMBER, status="blocked")

    channel = _RecordingChannel()
    proceed = await access.handle_gate(
        database, channel, config, owner_chat_id=OWNER, chat_id=MEMBER, display_name=None, text="/history", lang="en"
    )
    database.close()

    assert proceed is False
    reply_text = channel.sent[0][1]
    assert reply_text == i18n.t("access_denied", "en")
    assert "🧾" not in reply_text


async def test_history_works_directly_through_handle_inbound_message_with_ollama_reported_down(tmp_path):
    """R-A1: `/history` is dispatched in the command branch BEFORE the
    health-monitor deferral check, so it must still produce a real reply
    -- not a deferred/"noted" acknowledgment -- while Ollama is down.
    Exercised at the real `handle_inbound_message` seam (not the fuller
    `async_main` harness `tests/test_history.py` already uses), with a
    health_monitor double reporting DOWN and an LLM double that raises if
    ever called."""
    from habit_assistant.main import handle_inbound_message

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    database = Database(tmp_path / "habits.db")
    database.upsert_user(MEMBER, role="member", status="active")
    database.insert_log(LogEntry(None, MEMBER, "2026-08-23T09:00:00", "water", 500.0, None, "500ml", "reply"))

    channel = _RecordingChannel()
    health_monitor_down = SimpleNamespace(ollama_up=False)

    await handle_inbound_message(
        "/history",
        db=database,
        llm=_RaisingLLM(),
        channel=channel,
        config=config,
        user_id=MEMBER,
        health_monitor=health_monitor_down,
        registry=DEFAULT_REGISTRY,
    )
    database.close()

    assert channel.sent != []
    reply_text = channel.sent[0][1]
    assert "500 ml water" in reply_text  # a REAL history reply, not a deferred/"noted" ack
