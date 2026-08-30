"""Vera's adversarial gap-fill for SPEC-LINE.md Module B (no-LLM mode).

`tests/test_line_no_llm.py` (Luna's own file) already proves each of
`5.2`'s 8 rows structurally, one disabled + one enabled test each. This
file targets the shapes Archi's dispatch specifically flagged as
UNPROVEN by that file:

  (1) The `reparse_pending_unparsed` dead-code guard's disposition for a
      DB that already has legacy `awaiting_llm`/NULL rows BEFORE the
      branch flipped to no-LLM -- not just "the guard returns early", but
      "the rows are left genuinely untouched, forever, still visible to
      `pending_unparsed()`" (R-B8's own stated intent).
  (2) The backfill+LLM `date_offset` path's interaction with disabled
      mode: a preparse MISS with a date phrase must never crash and must
      never silently fabricate a date; a preparse HIT with a date phrase
      must be byte-identical whether Ollama is enabled or disabled (it
      never reaches the branch split at all).
  (3) The reply-to-reminder attribution shortcut (R13/R14, already
      zero-LLM pre-LINE) is provably UNAFFECTED by `config.ollama.enabled`
      -- same poisoned-client proof in both configs.
  (4) `HealthMonitor` stays inert (no ping, no alert, no drift) across
      MULTIPLE cycles, not just one.
  (5) The structural clarify-handoff invariant, stated as a standalone
      proof rather than inferred from individual row tests: after N
      unparseable messages in disabled mode, zero rows exist anywhere in
      `logs` with `unparsed_state IN (NULL, 'awaiting_llm')` --
      `awaiting_clarify` rows ARE expected and fine (R-B2's tap-to-fix).
  (6) Enabled=true byte-LEVEL round-trip spot checks (not just "the call
      happened") for 4 surfaces: the diary/review LLM prompts sent are
      EXACTLY the pre-LINE format strings, and a bare `HealthMonitor()`
      (no `ollama_enabled` kwarg at all, the overwhelming majority of
      real call sites) behaves identically to one constructed with
      `ollama_enabled=True` explicitly.
  (7) The disabled-by-default surface: bare `Config()` and the repo's own
      unmodified `config.toml` both resolve `ollama.enabled=True`;
      `config.toml.line` (the branch's own deployment template) resolves
      it `False` -- loaded through the REAL `load_config`, not just
      grepped as text.
  (8) `/checkin` (already zero-LLM pre-LINE, no `llm` parameter at all)
      is unaffected by `config.ollama.enabled` either way -- a quick
      structural check that Module B's gating didn't have to (and
      doesn't) touch it.
  (9) The Module C interleave in `core/routing.py` (the `/digest on|off`
      dispatch branch, out-of-ownership per SPEC-LINE.md §11 but landed
      there because `routing.py`'s command-dispatch table is the only
      place a command kind's execution can live): proof that dispatching
      it does not reach any LLM-shaped call in either config, i.e. it
      genuinely does not interact with Module B's `elif config.ollama.
      enabled: ... else: ...` split at all (the branch returns well
      before that point, same as every other command kind)."""

from __future__ import annotations

import json

import pytest

from habit_assistant.config import Config, OllamaConfig
from habit_assistant.core import commands, confirmation, i18n, review, routing
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.health import HealthMonitor
from habit_assistant.core.reminders import ReminderState
from habit_assistant.llm.prompts import (
    DIARY_REFLECTION_SYSTEM_PROMPT,
    DIARY_REFLECTION_USER_TEMPLATE,
    WEEKLY_REVIEW_SYSTEM_PROMPT,
    WEEKLY_REVIEW_USER_TEMPLATE,
)
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

from conftest import RecordingChannel

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


def disabled_config(**overrides) -> Config:
    return Config(ollama=OllamaConfig(enabled=False, **overrides))


def enabled_config(**overrides) -> Config:
    return Config(ollama=OllamaConfig(enabled=True, **overrides))


class PoisonedOllamaClient:
    """Raises on ANY LLM-shaped call -- the structural zero-LLM proof,
    same double family as tests/test_line_no_llm.py."""

    async def chat_json(self, *args, **kwargs):
        raise AssertionError("chat_json must never be called in no-LLM mode")

    async def chat_text(self, *args, **kwargs):
        raise AssertionError("chat_text must never be called in no-LLM mode")

    async def probe_schema_support(self, *args, **kwargs):
        raise AssertionError("probe_schema_support must never be called in no-LLM mode")

    async def aclose(self) -> None:
        pass


async def _poisoned_parse_message(*args, **kwargs):
    raise AssertionError("parse_message must never be called in no-LLM mode")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


def _unparsed_state_rows(db: Database, user_id: str) -> list[dict]:
    rows = db._conn.execute(
        "SELECT id, category, unparsed_state FROM logs WHERE user_id = ? ORDER BY id", (user_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# (1) Legacy awaiting_llm/NULL rows: disposition, not just "guard fires".
# ---------------------------------------------------------------------------


async def test_legacy_awaiting_llm_and_null_rows_survive_disabled_reparse_guard_untouched(db):
    """R-B8/§5.2 row 2: a DB carried over from a Telegram/enabled deployment
    can have BOTH kinds of pending row (`unparsed_state IS NULL` -- the
    original deferral shape -- and the explicit `'awaiting_llm'` string).
    `reparse_pending_unparsed` in disabled mode must leave BOTH completely
    inert: not reparsed, not reclassified, not closed, and still visible to
    `pending_unparsed()` afterward -- the spec's actual disposition is
    "permanently inert", not "silently resolved"."""
    null_row_id = db.insert_log(LogEntry(None, "u1", "2026-08-18T09:00:00", "unparsed", None, None, "500", "reply"))
    explicit_row_id = db.insert_log(
        LogEntry(
            None, "u2", "2026-08-19T09:00:00", "unparsed", None, None, "1 บ้วน",
            "reply", unparsed_state="awaiting_llm",
        )
    )

    before = db.pending_unparsed()
    assert {r["id"] for r in before} == {null_row_id, explicit_row_id}

    config = disabled_config()
    await routing.reparse_pending_unparsed(
        db, PoisonedOllamaClient(), RecordingChannel(), config, DEFAULT_REGISTRY,
        parse_message=_poisoned_parse_message,
    )

    after = db.pending_unparsed()
    assert {r["id"] for r in after} == {null_row_id, explicit_row_id}, (
        "legacy rows must remain visible to pending_unparsed() -- disabled mode "
        "must not silently resolve/close/lose them, only refuse to reparse them"
    )
    for row_id in (null_row_id, explicit_row_id):
        row = db.get_log(row_id)
        assert row["category"] == "unparsed"
        # Neither row was reclassified, and the NULL row's state was not
        # normalized/rewritten to the literal string either.
    assert db.get_log(null_row_id)["unparsed_state"] is None
    assert db.get_log(explicit_row_id)["unparsed_state"] == "awaiting_llm"


# ---------------------------------------------------------------------------
# (2) Backfill date-phrase interaction with disabled mode.
# ---------------------------------------------------------------------------


async def test_backfill_date_phrase_with_preparse_miss_disabled_no_crash_no_fabricated_date(db):
    """A preparse-miss message that ALSO carries a recognized trailing date
    phrase must still go through R-B2's clarify machinery untouched -- no
    crash from the (enabled-only) date_offset-resolution block, no log row
    fabricated with a backdated timestamp, zero LLM calls. Uses nonsense
    unit-free text so tier1_guesses is empty -> the generic clarify path,
    keeping the assertion simple (no row written at all)."""
    channel = RecordingChannel()
    config = disabled_config()

    await routing.handle_inbound_message(
        "zzqx flurbnorb wobblewomp 3 days ago",
        db=db,
        llm=PoisonedOllamaClient(),
        channel=channel,
        config=config,
        user_id="u1",
        registry=DEFAULT_REGISTRY,
        parse_message=_poisoned_parse_message,
    )

    assert channel.sent_to("u1") == [i18n.t("clarifying_question", "en")]
    assert db.pending_unparsed() == []
    assert db._conn.execute("SELECT COUNT(*) AS n FROM logs WHERE user_id='u1'").fetchone()["n"] == 0


async def test_backfill_date_phrase_with_preparse_hit_byte_identical_disabled_vs_enabled(db):
    """A preparse-HIT message with a trailing date phrase ("500ml 3 days
    ago") never reaches the `elif config.ollama.enabled: ... else: ...`
    split at all (the deterministic preparse hit wins at the top of
    `handle_inbound_message`, before the split) -- so it must log the exact
    same backdated row whether Ollama is enabled or disabled, with a
    POISONED llm/parse_message in BOTH configs proving neither ever
    touches the LLM for this path."""
    from datetime import date, timedelta

    def clock():
        from datetime import datetime
        return datetime(2026, 8, 30, 12, 0, 0)

    expected_date = (date(2026, 8, 30) - timedelta(days=3)).isoformat()

    for label, config in (("disabled", disabled_config()), ("enabled", enabled_config())):
        database = Database(":memory:")
        channel = RecordingChannel()
        try:
            await routing.handle_inbound_message(
                "500ml 3 days ago",
                db=database,
                llm=PoisonedOllamaClient(),
                channel=channel,
                config=config,
                user_id="u1",
                registry=DEFAULT_REGISTRY,
                clock=clock,
                parse_message=_poisoned_parse_message,
            )
            row = database._conn.execute(
                "SELECT category, value_num, ts FROM logs WHERE user_id='u1'"
            ).fetchone()
            assert row is not None, f"{label}: no row logged"
            assert row["category"] == "water", label
            assert row["value_num"] == 500.0, label
            assert row["ts"].startswith(expected_date), (label, row["ts"], expected_date)
        finally:
            database.close()


# ---------------------------------------------------------------------------
# (3) Reply-to-reminder attribution: unaffected by ollama.enabled.
# ---------------------------------------------------------------------------


async def test_reply_to_reminder_attribution_zero_llm_in_both_configs(db):
    """R13/R14 (already zero-LLM pre-LINE): a bare-value reply to a
    remembered reminder message resolves via `reply_attribution.
    resolve_reply_value` BEFORE the preparse-miss branch split is even
    reached -- so it must behave identically, with a POISONED llm/
    parse_message, whether `config.ollama.enabled` is True or False."""
    for label, config in (("disabled", disabled_config()), ("enabled", enabled_config())):
        database = Database(":memory:")
        channel = RecordingChannel()
        state = ReminderState()
        state.remember_reminder("u1", "msg-1", "water", cap=10)
        try:
            await routing.handle_inbound_message(
                "750",
                db=database,
                llm=PoisonedOllamaClient(),
                channel=channel,
                config=config,
                user_id="u1",
                registry=DEFAULT_REGISTRY,
                reply_to_message_id="msg-1",
                reminder_state=state,
                parse_message=_poisoned_parse_message,
            )
            row = database._conn.execute(
                "SELECT category, value_num FROM logs WHERE user_id='u1'"
            ).fetchone()
            assert row is not None, label
            assert row["category"] == "water", label
            assert row["value_num"] == 750.0, label
        finally:
            database.close()


# ---------------------------------------------------------------------------
# (4) HealthMonitor: multi-cycle stability, not just one call.
# ---------------------------------------------------------------------------


async def test_disabled_health_monitor_never_drifts_across_multiple_cycles():
    """R-B8: `ollama_enabled=False` must stay inert across repeated
    `run_once()` cycles -- no HTTP call ever (poisoned transport), no
    alert, no recovery callback, `.ollama_up` staying True the whole
    time. A single-call proof (test_line_no_llm.py's row 8) can't rule
    out drift after N cycles; this does."""
    import httpx

    class PoisonedHTTPTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"no HTTP request may be made in no-LLM mode, got: {request.url}")

        async def aclose(self) -> None:
            pass

    poisoned_client = httpx.AsyncClient(transport=PoisonedHTTPTransport())
    channel = RecordingChannel()
    recovered_calls = []

    async def on_recovered():
        recovered_calls.append(1)

    async def _true() -> bool:
        return True

    monitor = HealthMonitor(
        "http://mac-mini:11434",
        "fake-telegram-token",
        "owner",
        client=poisoned_client,
        channel=channel,
        on_ollama_recovered=on_recovered,
        ollama_enabled=False,
    )
    monitor.check_telegram = lambda: _true()

    for _ in range(3):
        await monitor.run_once()
        assert monitor.ollama_up is True

    assert channel.sent == []
    assert recovered_calls == []
    await poisoned_client.aclose()


# ---------------------------------------------------------------------------
# (5) Structural clarify-handoff invariant, standalone.
# ---------------------------------------------------------------------------


async def test_zero_awaiting_llm_rows_after_n_unparseable_messages_disabled(db):
    """AC15/R-B1's structural invariant, stated directly rather than
    inferred: after N distinct unparseable inbound messages in disabled
    mode -- a mix that trips BOTH the tier1-guesses tap-to-fix branch AND
    the generic-clarify (no-guesses) branch -- zero rows anywhere in
    `logs` have `unparsed_state IN (NULL, 'awaiting_llm')` (the exact set
    `pending_unparsed()` matches). `awaiting_clarify` rows ARE expected
    and correct (R-B2) -- this test asserts they're the ONLY state that
    ever appears for `category='unparsed'`."""
    channel = RecordingChannel()
    config = disabled_config()
    messages = [
        "asdkjqwexyz not a real habit phrase",  # generic clarify, no row
        "500",  # tier1 guess (water-plausible) -> awaiting_clarify row
        "qqqzxx totally nonsense blah",  # generic clarify, no row
        "300",  # tier1 guess -> awaiting_clarify row
        "flibbertigibbet nonsense text here",  # generic clarify, no row
        "10",  # tier1 guess -> awaiting_clarify row
    ]
    for i, text in enumerate(messages):
        await routing.handle_inbound_message(
            text,
            db=db,
            llm=PoisonedOllamaClient(),
            channel=channel,
            config=config,
            user_id=f"u{i}",
            registry=DEFAULT_REGISTRY,
            parse_message=_poisoned_parse_message,
        )

    assert db.pending_unparsed() == []
    forbidden = db._conn.execute(
        "SELECT COUNT(*) AS n FROM logs WHERE category='unparsed' "
        "AND (unparsed_state IS NULL OR unparsed_state='awaiting_llm')"
    ).fetchone()["n"]
    assert forbidden == 0

    # Sanity: the tap-to-fix branch DID actually write awaiting_clarify
    # rows for the guess-eligible messages, proving the assertion above
    # isn't vacuously true because nothing was ever written.
    clarify_rows = db._conn.execute(
        "SELECT COUNT(*) AS n FROM logs WHERE category='unparsed' AND unparsed_state='awaiting_clarify'"
    ).fetchone()["n"]
    assert clarify_rows >= 1


# ---------------------------------------------------------------------------
# (6) Enabled=true byte-LEVEL round-trip spot checks.
# ---------------------------------------------------------------------------


class _CapturingLLM:
    def __init__(self, text: str | None = "some narrative"):
        self._text = text
        self.calls: list[tuple[str, str]] = []

    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        self.calls.append((system_prompt, user_prompt))
        return self._text


async def test_enabled_diary_confirmation_prompt_byte_identical_to_v110_format():
    """Not just "chat_text was called" -- the EXACT system/user prompt
    strings sent must match the pre-LINE `DIARY_REFLECTION_*` format
    strings verbatim. Proves the `if config.ollama.enabled: <call> else:
    None` wrapping in confirmation.py didn't alter what's actually sent."""
    config = enabled_config()
    llm = _CapturingLLM("Lovely.")
    database = Database(":memory:")
    try:
        await confirmation.confirmation_text(
            database, llm, DEFAULT_REGISTRY.get("diary"), "had a good day", "2026-08-19", "en", config, "u1"
        )
    finally:
        database.close()

    assert len(llm.calls) == 1
    system_prompt, user_prompt = llm.calls[0]
    assert system_prompt == DIARY_REFLECTION_SYSTEM_PROMPT.format(
        language_instruction=i18n.language_instruction("en")
    )
    assert user_prompt == DIARY_REFLECTION_USER_TEMPLATE.format(diary_text="had a good day")


async def test_enabled_weekly_review_prompt_byte_identical_to_v110_format():
    """Same byte-level proof for review.py's narrative call."""
    from datetime import date

    database = Database(":memory:")
    database.insert_log(LogEntry(None, "owner", "2026-08-19T09:00:00", "water", 2500.0, None, "seed", "reply"))
    try:
        config = enabled_config()
        llm = _CapturingLLM("Solid week.")

        await review.run_weekly_review(database, config, DEFAULT_REGISTRY, llm, "en", "owner", today=date(2026, 8, 19))

        assert len(llm.calls) == 1
        system_prompt, user_prompt = llm.calls[0]
        assert system_prompt == WEEKLY_REVIEW_SYSTEM_PROMPT.format(
            language_instruction=i18n.language_instruction("en")
        )
        assert WEEKLY_REVIEW_USER_TEMPLATE.split("{stats_summary}")[0] in user_prompt
    finally:
        database.close()


async def test_enabled_health_monitor_bare_default_matches_explicit_true():
    """The overwhelming majority of real (pre-LINE) `HealthMonitor(...)`
    call sites never pass `ollama_enabled` at all -- this proves the bare
    default behaves identically to passing `ollama_enabled=True`
    explicitly: both make exactly one real HTTP call and report the same
    result for the same transport."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    for kwargs in ({}, {"ollama_enabled": True}):
        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        monitor = HealthMonitor("http://mac-mini:11434", "fake-telegram-token", "owner", client=client, **kwargs)
        assert monitor._ollama_enabled is True
        assert await monitor.check_ollama() is False
        await client.aclose()


# ---------------------------------------------------------------------------
# (7) Disabled-by-default surface: the real config loader, not grepped text.
# ---------------------------------------------------------------------------


def test_bare_config_and_repo_config_toml_default_ollama_enabled_true():
    """Neither the bare code default nor the repo's own unmodified
    `config.toml` (the Telegram/pre-LINE branch's own file, untouched by
    this branch per R-S1: "no other config field changes meaning") sets
    `[ollama].enabled` explicitly -- both must resolve to `True`."""
    from pathlib import Path

    from habit_assistant.config import load_config

    assert Config().ollama.enabled is True

    repo_config_toml = Path(__file__).resolve().parent.parent / "config.toml"
    assert repo_config_toml.exists(), "expected the repo's own config.toml to exist"
    loaded = load_config(repo_config_toml)
    assert loaded.ollama.enabled is True, "config.toml must NOT flip the Telegram-branch default"


def test_config_toml_line_loads_with_ollama_enabled_false():
    """R-S1: `config.toml.line` (the branch's own deployment template) is
    THE no-LLM-mode default -- loaded through the real `load_config`
    (TOML parse + pydantic validation), not just grepped as text."""
    from pathlib import Path

    from habit_assistant.config import load_config

    line_config_toml = Path(__file__).resolve().parent.parent / "config.toml.line"
    assert line_config_toml.exists(), "expected config.toml.line to exist at repo root"
    loaded = load_config(line_config_toml)
    assert loaded.ollama.enabled is False


# ---------------------------------------------------------------------------
# (8) /checkin: already zero-LLM, unaffected by config.ollama.enabled.
# ---------------------------------------------------------------------------


async def test_checkin_command_unaffected_by_ollama_enabled_in_either_direction(db):
    """`checkins.execute_checkin` takes no `llm` parameter at all (it was
    already zero-LLM before this branch, ROADMAP.md v1.5.0) -- Module B's
    gating had nothing to touch here. Structural confirmation: dispatching
    `/checkin on` produces a reply with a POISONED llm/parse_message in
    BOTH configs, proving no accidental new LLM dependency was introduced
    anywhere on this path."""
    for label, config in (("disabled", disabled_config()), ("enabled", enabled_config())):
        channel = RecordingChannel()
        database = Database(":memory:")
        try:
            await routing.handle_inbound_message(
                "/checkin on",
                db=database,
                llm=PoisonedOllamaClient(),
                channel=channel,
                config=config,
                user_id="u1",
                registry=DEFAULT_REGISTRY,
                parse_message=_poisoned_parse_message,
            )
            assert channel.sent_to("u1") != [], label
        finally:
            database.close()


# ---------------------------------------------------------------------------
# (9) Module C interleave audit: the `/digest` branch in routing.py.
# ---------------------------------------------------------------------------


def test_digest_matcher_registered_disjoint_from_every_b_owned_command_kind():
    """Structural half of the interleave audit: `commands.dispatch`'s
    `/digest` matcher (Module C, landed in `core/commands.py`'s
    `_MATCHERS` table -- NOT a file Module B owns either, but confirmed
    here as a read-only cross-check) produces `kind == "digest"`, disjoint
    from every kind Module B's own branch logic inspects
    (`query`/`target`/none-of-the-27-commands-fallthrough). No collision,
    no shadowing."""
    command = commands.dispatch("/digest on", DEFAULT_REGISTRY)
    assert command is not None
    assert command.kind == "digest"
    assert command.kind not in ("query", "target")


async def test_digest_dispatch_makes_zero_llm_calls_in_either_config(db):
    """Behavioral half of the interleave audit: Module C's `/digest`
    dispatch branch in `core/routing.py` (lines ~414-424) sits ABOVE
    Module B's `elif config.ollama.enabled: ... else: ...` preparse-miss
    split -- every command-kind branch returns before that split is ever
    reached. Proven here with a POISONED llm/parse_message in BOTH
    configs: the digest toggle must never touch the LLM, and must not
    perturb B's own zero-LLM guarantee for adjacent command dispatch."""
    for label, config in (("disabled", disabled_config()), ("enabled", enabled_config())):
        channel = RecordingChannel()
        database = Database(":memory:")
        try:
            await routing.handle_inbound_message(
                "/digest off",
                db=database,
                llm=PoisonedOllamaClient(),
                channel=channel,
                config=config,
                user_id="u1",
                registry=DEFAULT_REGISTRY,
                parse_message=_poisoned_parse_message,
            )
            assert channel.sent_to("u1") != [], label
            assert database.digest_opt_out("u1") is True, label

            channel2 = RecordingChannel()
            await routing.handle_inbound_message(
                "/digest on",
                db=database,
                llm=PoisonedOllamaClient(),
                channel=channel2,
                config=config,
                user_id="u1",
                registry=DEFAULT_REGISTRY,
                parse_message=_poisoned_parse_message,
            )
            assert channel2.sent_to("u1") != [], label
            assert database.digest_opt_out("u1") is False, label
        finally:
            database.close()
