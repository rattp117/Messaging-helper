"""SPEC-v1.10.md "Never lose a log" -- final release-gate verification
(Archi's dispatch: "18-AC sign-off... probe beyond Luna's 16 integration
tests"). `tests/test_v110_integration.py` already proves every AC through
the REAL `core/routing.py`/`core/app.py` closures; this file adds:

1. THE ZOMBIE PROOF, driven through a genuinely LLM-shaped fake (`chat_json`
   returning "unknown", exercising the REAL `core/parser.py:parse_message`
   -- not the `parse_message=` stand-in `test_v110_integration.py` uses)
   for texts shaped exactly like production `id=13`("500")/`id=14`
   ("Streaching") (SPEC-v1.10.md §1), across multiple pre-existing users.
2. Cross-feature probes the 16 integration tests don't cover: a clarify tap
   racing a mid-wait habit archive; the backfill-phrase + clarify quoting
   interplay; quicklog untouched by `clarify.enabled=False`; the R4
   single-flight guard under REAL concurrent sweep triggers; a number+unit
   reply-to-reminder falling through to zero-LLM preparse even while Ollama
   is down; closure/offer notification posture vs. `silent_proactive`.
3. Announce-readiness spot checks (RELEASE_NOTES content, not a version
   bump) and a precise "inert gate" -- which paths are BYTE-IDENTICAL to
   pre-1.10 with every new config off, and which one (R10's `/log`
   keyboard on the generic clarifying question) is a deliberate, spec-
   mandated, UNCONDITIONAL change with no config gate at all.
4. `/guide` + `/help` render-budget checks.

Live-environment rule (same as every other file in this suite): every DB
is a scratch `tmp_path` SQLite file; nothing here ever opens
`data/habits.db`; no real Telegram/Ollama call is made."""

from __future__ import annotations

import asyncio
import json

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import discoverability, i18n, release_notes, reminders, routing
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.registry_provider import RegistryProvider
from habit_assistant.core.reminders import ReminderState
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner"
OTHER = "other-user"
THIRD = "third-user"


# ===========================================================================
# Shared fixtures (own copy -- established per-file convention, see
# IMPL-v1.10-integration.md "Known limitations").
# ===========================================================================


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.actionable: list[tuple[str, str, list]] = []
        self._next_id = 8800

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> str | None:
        self.sent.append((chat_id, text))
        self._next_id += 1
        return str(self._next_id)

    async def send_actionable(self, chat_id: str, text: str, buttons: list) -> None:
        self.actionable.append((chat_id, text, buttons))
        self.sent.append((chat_id, text))

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


class _HealthMonitor:
    def __init__(self, ollama_up: bool) -> None:
        self.ollama_up = ollama_up


class _UnknownOllamaClient:
    """A genuinely LLM-shaped fake: `chat_json` returns a real JSON payload
    that `core/parser.py:parse_message` (the REAL, un-stubbed function)
    parses into `ExtractionResult.unknown()`-equivalent -- this is what
    "a mocked-up Ollama that returns unknown" means literally, as opposed
    to `test_v110_integration.py`'s own `parse_message=_still_unknown`
    stand-in, which bypasses `parser.py` entirely. Counts calls so a
    "zero LLM calls on the second sweep" claim is proved by an actual
    counter, not just by `pending_unparsed()` being empty."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat_json(self, system_prompt, user_prompt, schema, valid_categories) -> str:
        self.calls += 1
        return json.dumps({"category": "unknown", "value": None, "confidence": 0.1})


class _RaisingIfCalledOllamaClient:
    async def chat_json(self, *a, **k) -> str:
        raise AssertionError("the LLM must never be called for a terminal (closed/awaiting_clarify) row")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(OWNER, role="member", status="active")
    database.upsert_user(OTHER, role="member", status="active")
    database.upsert_user(THIRD, role="member", status="active")
    yield database
    database.close()


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def registry(config: Config) -> HabitRegistry:
    return HabitRegistry.from_config(config)


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def provider(config: Config, db: Database) -> RegistryProvider:
    return RegistryProvider(config, db)


def _insert_zombie(db_: Database, user_id: str, raw: str) -> int:
    """Shaped exactly like the real production zombies id=13/14: `category=
    'unparsed'`, `unparsed_state` NULL (migration 013's own untouched
    legacy shape)."""
    return db_.insert_log(LogEntry(None, user_id, "2026-08-24T09:00:00", "unparsed", None, None, raw, "reply"))


# ===========================================================================
# 1. THE ZOMBIE PROOF -- a real LLM-shaped fake, production-shaped rows,
# multiple pre-existing users, two sweeps.
# ===========================================================================


async def test_zombie_proof_real_ollama_shaped_fake_closes_and_offers_then_zero_llm_calls_on_resweep(
    db, config, registry, channel
):
    """id=13-shaped ("500") and id=14-shaped ("Streaching") rows, for TWO
    different pre-existing users (isolation + the literal SPEC-v1.10.md §1
    scenario together). First sweep drives them through the REAL
    `core/parser.py:parse_message` via a fake Ollama client that actually
    returns "unknown" JSON (2 calls per row-with-a-user = 4 total this
    sweep). Second sweep: the fake raises if `chat_json` is EVER called --
    zero LLM calls, not just an empty `pending_unparsed()`. Each user gets
    EXACTLY one message for their own row."""
    row_500_owner = _insert_zombie(db, OWNER, "500")
    row_streaching_owner = _insert_zombie(db, OWNER, "Streaching")
    row_500_other = _insert_zombie(db, OTHER, "500")
    row_streaching_other = _insert_zombie(db, OTHER, "Streaching")

    llm = _UnknownOllamaClient()
    await routing.reparse_pending_unparsed(db, llm, channel, config, registry=registry)

    assert llm.calls == 4  # one real LLM call per row this first sweep
    assert db.pending_unparsed() == []  # AC5/R2: no zombies left, for either user

    for row_id, expect_state in (
        (row_500_owner, "awaiting_clarify"),
        (row_500_other, "awaiting_clarify"),
        (row_streaching_owner, "closed"),
        (row_streaching_other, "closed"),
    ):
        row = db.get_log(row_id)
        assert row["category"] == "unparsed"
        assert row["unparsed_state"] == expect_state

    # Each user got exactly ONE message about their "500" row (the guess
    # offer) and exactly ONE about their "Streaching" row (the closure) --
    # never zero, never doubled, never cross-delivered to the other user.
    for user_id, row500, row_streaching in ((OWNER, row_500_owner, row_streaching_owner), (OTHER, row_500_other, row_streaching_other)):
        user_actionable = [(t, b) for cid, t, b in channel.actionable if cid == user_id]
        offers = [(t, b) for t, b in user_actionable if f"clarify:{row500}:" in b[0][1]]
        assert len(offers) == 1
        user_sent = [t for cid, t in channel.sent if cid == user_id]
        closures = [t for t in user_sent if "Streaching" in t]
        assert len(closures) == 1

    # Second sweep: a fake that raises on ANY call -- proves zero LLM
    # calls, by construction, not merely "no new rows appeared".
    raising_llm = _RaisingIfCalledOllamaClient()
    await routing.reparse_pending_unparsed(db, raising_llm, channel, config, registry=registry)
    assert db.pending_unparsed() == []


# ===========================================================================
# 2a. Cross-feature: a clarify tap racing a mid-wait custom-habit archive.
# ===========================================================================


async def test_clarify_tap_after_the_guessed_habit_is_archived_mid_wait_is_a_friendly_noop(
    db, config, channel, provider
):
    """A custom habit is created, a clarify offer is built naming it, the
    user archives that SAME habit while the offer is still sitting
    un-tapped (a real, if narrow, window -- archive is a matter of one
    /delhabit message), and only THEN is the button tapped. The tapping
    user's REFRESHED registry (`provider.for_user`, cache invalidated by
    the archive, mirroring `execute_delhabit`'s own real invalidation
    call) no longer contains the habit -- `handle_clarify_callback` must
    treat this exactly like any other unknown/foreign habit id: a
    friendly no-op, no write, row stays `awaiting_clarify` (not silently
    dropped, not force-resolved)."""
    db.add_user_habit(
        OWNER,
        {
            "id": "pushups", "type": "numeric", "label_en": "pushups", "label_th": "วิดพื้น",
            "unit_en": "reps", "unit_th": "ครั้ง", "goal": 50.0, "unit_aliases": None,
        },
    )
    row_id = _insert_zombie(db, OWNER, "pushups")
    registry_with_custom = provider.for_user(OWNER)
    assert registry_with_custom.get("pushups") is not None

    async def _still_unknown(text, llm, reg, threshold):
        from habit_assistant.llm.ollama_client import ExtractionResult

        return ExtractionResult("unknown", None, 0.0)

    await routing.reparse_pending_unparsed(
        db, None, channel, config, registry=registry_with_custom, parse_message=_still_unknown
    )
    row_before_tap = db.get_log(row_id)
    assert row_before_tap["unparsed_state"] == "awaiting_clarify"
    offer_callback = next(b for cid, t, buttons in channel.actionable if cid == OWNER for _lbl, b in buttons if b.startswith(f"clarify:{row_id}:"))
    assert offer_callback == f"clarify:{row_id}:pushups:50"

    # Archive it mid-wait -- exactly like a real /delhabit would -- and
    # invalidate the cache (the real production call site's own contract).
    db.archive_user_habit(OWNER, "pushups")
    provider.invalidate(OWNER)

    channel.sent.clear()
    channel.actionable.clear()
    await routing.on_callback(
        OWNER, offer_callback, "pushups", "cb-archived", db=db, channel=channel, config=config, provider=provider
    )

    row_after_tap = db.get_log(row_id)
    assert row_after_tap["category"] == "unparsed"  # never force-resolved
    assert row_after_tap["unparsed_state"] == "awaiting_clarify"  # untouched, still tappable in theory
    assert channel.actionable == []  # no "Recovered" confirmation, no Undo button
    assert channel.sent == [(OWNER, i18n.t("quicklog_unknown_habit", "en"))]


# ===========================================================================
# 2b. Cross-feature: backfill phrase + clarify quoting/guess interplay.
# ===========================================================================


async def test_backfill_phrase_plus_unit_still_logs_zero_llm_even_while_ollama_down(db, config, registry, channel):
    """Positive control: "500ml yesterday" backdates correctly and logs
    immediately (zero LLM, no deferral at all) even while Ollama is down
    -- the backfill+deterministic-preparse path is unaffected by v1.10."""
    await routing.handle_inbound_message(
        "500ml yesterday",
        db=db, llm=None, channel=channel, config=config, user_id=OWNER, registry=registry,
        health_monitor=_HealthMonitor(ollama_up=False),
    )
    assert db.pending_unparsed() == []
    rows = db.logs_between(OWNER, "2000-01-01T00:00:00", "2100-01-01T00:00:00")
    assert len(rows) == 1 and rows[0]["category"] == "water" and rows[0]["value_num"] == 500.0


async def test_backfill_phrase_without_a_unit_still_closes_safely_but_the_date_prefix_defeats_the_bare_number_guess(
    db, config, registry, channel
):
    """FINDING (not a data-loss bug -- documented here, not a blocking AC
    failure): "yesterday 500" has no unit, so the deterministic pre-parser
    can't place it and it defers (Ollama down) with `raw_message` = the
    FULL original text, "yesterday 500" (backfill's date-stripping only
    ever touches `parse_text`, a routing.py-local variable -- the DB row
    and everything `clarify.py` reads keeps the untouched original,
    SPEC-v1.10.md's own "quoting `row['raw_message']`" contract, R1).

    On recovery, `clarify.tier1_guesses` also receives that SAME full
    text. Its bare-number-plausibility check (`_is_bare_number`) is a
    whole-message-ANCHORED match (`VALUE_RE.match(text.strip())`, not
    `.search`) -- "yesterday 500" does not start with a digit, so the
    check that would otherwise have offered a `(water, 500)` guess (a
    bare "500" alone gets exactly that guess, proven elsewhere in this
    suite) silently fails to fire here. The row still safely reaches a
    TERMINAL state (`closed`, not a zombie) and the user still gets the
    ONE closure notification quoting their own exact words -- no data
    loss, no silence -- just a less helpful guess than a bare "500"
    would have gotten. No SPEC-v1.10.md AC governs the backfill/clarify
    interaction (backfill is v1.8, unrelated to this release's own ACs),
    so this is a pre-existing interplay gap, not a v1.10 regression, and
    not a release blocker -- flagged for a future backlog item (re-run
    `backfill.extract_date` inside `tier1_guesses`/the closure/offer
    quoting, or quote the residual instead of the raw text)."""
    await routing.handle_inbound_message(
        "yesterday 500",
        db=db, llm=None, channel=channel, config=config, user_id=OWNER, registry=registry,
        health_monitor=_HealthMonitor(ollama_up=False),
    )
    pending = db.pending_unparsed()
    assert len(pending) == 1
    row_id = pending[0]["id"]
    assert pending[0]["raw_message"] == "yesterday 500"  # untouched original, not the "500" residual

    async def _still_unknown(text, llm, reg, threshold):
        from habit_assistant.llm.ollama_client import ExtractionResult

        return ExtractionResult("unknown", None, 0.0)

    channel.sent.clear()
    channel.actionable.clear()
    await routing.reparse_pending_unparsed(db, None, channel, config, registry=registry, parse_message=_still_unknown)

    row_after = db.get_log(row_id)
    assert row_after["category"] == "unparsed"
    assert row_after["unparsed_state"] == "closed"  # terminal -- no zombie, despite the missed guess
    assert db.pending_unparsed() == []
    closure_sends = [t for cid, t in channel.sent if cid == OWNER]
    assert len(closure_sends) == 1
    assert closure_sends[0] == i18n.t("closure_notification", "en", text="yesterday 500")  # quotes the FULL original


# ===========================================================================
# 2c. Cross-feature: quicklog tap is completely unaffected by
# `clarify.enabled=False` (disjoint callback-data prefixes).
# ===========================================================================


async def test_quicklog_log_tap_unaffected_by_clarify_disabled(db, registry, channel, provider):
    config = Config.model_validate({"clarify": {"enabled": False}})
    await routing.on_callback(
        OWNER, "log:water:250", "250", "cb-quicklog", db=db, channel=channel, config=config, provider=provider
    )
    row = db.last_log(OWNER, category="water")
    assert row is not None and row["value_num"] == 250.0
    assert channel.actionable  # the ordinary quicklog confirmation, unaffected by an unrelated config flag


# ===========================================================================
# 2d. Cross-feature: the R4 single-flight guard under a REAL concurrent
# trigger (not just a directly-called re-entrancy probe).
# ===========================================================================


async def test_single_flight_guard_holds_under_real_concurrent_sweep_triggers(db, config, registry, channel):
    """Two `reparse_pending_unparsed` calls launched genuinely concurrently
    (`asyncio.gather`) against the SAME pending row. A slow `parse_message`
    (yields control mid-call via `asyncio.sleep(0)`) creates a real
    interleaving window between "sweep A checks/sets the in-progress flag"
    and "sweep A finishes" -- sweep B must observe the flag and return
    immediately, doing nothing, rather than also processing the row (which
    the per-row CAS would also catch, but R4 is meant to prevent the
    redundant second pass entirely, not just rely on the CAS as a
    backstop). Proven by: exactly ONE closure notification is ever sent
    for the row, and `parse_message` is called exactly once total, not
    twice."""
    row_id = _insert_zombie(db, OWNER, "Streaching")
    calls = {"n": 0}

    async def _slow_still_unknown(text, llm, reg, threshold):
        from habit_assistant.llm.ollama_client import ExtractionResult

        calls["n"] += 1
        await asyncio.sleep(0)  # yield control -- gives the second concurrent call a chance to interleave
        return ExtractionResult("unknown", None, 0.0)

    await asyncio.gather(
        routing.reparse_pending_unparsed(db, None, channel, config, registry=registry, parse_message=_slow_still_unknown),
        routing.reparse_pending_unparsed(db, None, channel, config, registry=registry, parse_message=_slow_still_unknown),
    )

    assert calls["n"] == 1  # the second call's own single-flight guard skipped the loop body entirely
    row = db.get_log(row_id)
    assert row["unparsed_state"] == "closed"
    closure_sends = [t for cid, t in channel.sent if cid == OWNER]
    assert len(closure_sends) == 1  # never doubled


# ===========================================================================
# 2e. Cross-feature: a number+unit reply-to-reminder falls through to
# zero-LLM preparse, even while Ollama is down -- conservatism never means
# data loss or a forced deferral.
# ===========================================================================


async def test_reply_to_reminder_number_plus_unit_falls_through_to_deterministic_preparse_zero_llm_while_down(
    db, config, registry, channel
):
    class _RaisingLLM:
        async def chat_json(self, *a, **k):
            raise AssertionError("must never be called -- deterministic preparse should handle this")

    state = ReminderState()
    water = registry.get("water")
    await reminders.send_reminder(channel, OWNER, water, "en", db, config, state)
    reminder_msg_id = next(iter(state.reminder_context[OWNER]))
    channel.sent.clear()
    channel.actionable.clear()

    await routing.handle_inbound_message(
        "500ml",  # a number+UNIT reply -- resolve_reply_value returns None (R14), NOT attributed here
        db=db, llm=_RaisingLLM(), channel=channel, config=config, user_id=OWNER, registry=registry,
        reminder_state=state, reply_to_message_id=reminder_msg_id,
        health_monitor=_HealthMonitor(ollama_up=False),
    )

    assert db.pending_unparsed() == []  # never deferred -- preparse caught it directly
    row = db.last_log(OWNER, category="water")
    assert row is not None and row["value_num"] == 500.0 and row["source"] == "reply"


# ===========================================================================
# 2f. Closure/offer notification posture vs. silent_proactive -- consistent
# with the pre-existing recovered-* confirmation precedent (send_actionable
# has never carried a silent flag in this codebase; this is not new to
# v1.10 and no AC calls for it to gate recovery-sweep sends).
# ===========================================================================


async def test_closure_and_offer_notifications_use_send_actionable_like_every_other_confirmation(
    db, config, registry, channel
):
    """`silent_proactive` (SPEC-v1.8.md R-D1) only ever gates `channel.send`
    calls at the three ORIGINAL proactive tick sites (reminders/check-ins/
    nudge) -- `send_actionable` (used by every ordinary log confirmation,
    the recovered-* sweep confirmation since v0.4, and now `clarify.
    offer_clarify`/`send_closure`) has never accepted a `disable_notification`
    parameter at all, in this release or any prior one. A sweep-triggered
    closure/offer is therefore consistent with the established "recovery
    confirmations are never silent" precedent, not a v1.10-introduced gap
    -- verified directly against `Channel.send_actionable`'s own signature
    (no such parameter exists to omit) rather than assumed."""
    import inspect

    from habit_assistant.channels.base import Channel as ChannelABC

    sig = inspect.signature(ChannelABC.send_actionable)
    assert "disable_notification" not in sig.parameters

    row_id = _insert_zombie(db, OWNER, "Streaching")

    async def _still_unknown(text, llm, reg, threshold):
        from habit_assistant.llm.ollama_client import ExtractionResult

        return ExtractionResult("unknown", None, 0.0)

    await routing.reparse_pending_unparsed(db, None, channel, config, registry=registry, parse_message=_still_unknown)
    assert db.get_log(row_id)["unparsed_state"] == "closed"
    assert channel.sent  # the closure notification was sent (content already proven in the zombie-proof test above)


# ===========================================================================
# 3. Announce readiness -- content shape, NOT a version bump.
# ===========================================================================


def test_release_notes_1_10_0_content_matches_spec_bullets_both_languages():
    en = release_notes.get_release_note("1.10.0", "en")
    th = release_notes.get_release_note("1.10.0", "th")
    assert en.startswith("\U0001f389 What's new in v1.10.0")
    assert th.startswith("\U0001f389 มีอะไรใหม่ใน v1.10.0")
    # SPEC-v1.10.md §3.7: bullets closure+tap-to-fix, reply-to-reminder,
    # outage honesty, /guide -- spot-check EN keywords (content, not exact
    # wording, which is Patty/copy's own latitude).
    for keyword in ("never lose a log", "reply to a reminder", "outage honesty", "/guide"):
        assert keyword.lower() in en.lower(), f"EN release note missing a mention of {keyword!r}"
    assert th.strip() != "" and th != en


# ===========================================================================
# 4. The inert gate -- precisely which paths are byte-identical to pre-1.10
# with every new config off, and which one (R10) is deliberately NOT.
# ===========================================================================


async def _all_v110_off_config() -> Config:
    return Config.model_validate(
        {
            "outage": {"honest_reply": False},
            "clarify": {"enabled": False},
            "reply_to_reminder": {"enabled": False},
        }
    )


async def test_inert_gate_ordinary_preparse_log_byte_identical_with_every_v110_config_off(db, registry, channel):
    config = await _all_v110_off_config()
    await routing.handle_inbound_message(
        "500ml", db=db, llm=None, channel=channel, config=config, user_id=OWNER, registry=registry,
        health_monitor=_HealthMonitor(ollama_up=True),
    )
    _, text, buttons = channel.actionable[-1]
    assert text == i18n.t("water_confirmation", "en", water_ml=500, total=500, goal=2500, pct=20)
    assert buttons and buttons[0][1].startswith("undo:")
    assert db.pending_unparsed() == []


async def test_inert_gate_outage_deferral_byte_identical_deferred_ack_with_every_v110_config_off(
    db, registry, channel
):
    config = await _all_v110_off_config()
    await routing.handle_inbound_message(
        "went for a run", db=db, llm=None, channel=channel, config=config, user_id=OWNER, registry=registry,
        health_monitor=_HealthMonitor(ollama_up=False),
    )
    assert len(db.pending_unparsed()) == 1
    assert channel.sent[-1] == (OWNER, i18n.t("deferred_ack", "en"))
    assert channel.actionable == []  # pre-1.10 shape: no /log keyboard either


async def test_inert_gate_mapped_reply_falls_through_when_reply_to_reminder_disabled(db, registry, channel):
    config = await _all_v110_off_config()
    state = ReminderState()
    water = registry.get("water")
    await reminders.send_reminder(channel, OWNER, water, "en", db, config, state)
    reminder_msg_id = next(iter(state.reminder_context[OWNER]))
    channel.sent.clear()
    channel.actionable.clear()

    await routing.handle_inbound_message(
        "500", db=db, llm=None, channel=channel, config=config, user_id=OWNER, registry=registry,
        reminder_state=state, reply_to_message_id=reminder_msg_id,
        health_monitor=_HealthMonitor(ollama_up=False),
    )
    # reply_to_reminder.enabled=False -> the map is still built but never
    # consulted -- "500" (no unit) can't preparse either, so it defers
    # exactly like an unmapped reply would.
    rows = db.logs_between(OWNER, "2000-01-01T00:00:00", "2100-01-01T00:00:00")
    assert len(rows) == 1 and rows[0]["category"] == "unparsed"


async def test_inert_gate_does_NOT_extend_to_the_live_clarify_generic_question_by_spec_design(
    db, registry, channel
):
    """R10's own wording has no config gate: "If neither the live nor
    recovery path can produce a tier-1 guess, the user gets a generic
    bilingual question / closure with the /log keyboard" -- unconditional.
    `clarify.enabled=False` only suppresses GUESS BUTTONS (R6); it does
    NOT restore the pre-1.10 zero-button generic clarifying question.
    Documented here as the deliberately-NOT-inert exception, so this
    doesn't get mistaken for a gap in a future audit."""
    config = await _all_v110_off_config()

    async def _still_unknown(text, llm, reg, threshold):
        from habit_assistant.llm.ollama_client import ExtractionResult

        return ExtractionResult("unknown", None, 0.0)

    await routing.handle_inbound_message(
        "kjshdfkjshdf gibberish", db=db, llm=None, channel=channel, config=config, user_id=OWNER, registry=registry,
        health_monitor=_HealthMonitor(ollama_up=True),
        parse_message=_still_unknown,
    )
    _, text, buttons = channel.actionable[-1]
    assert text == i18n.t("clarifying_question", "en")
    assert buttons and all(cb.startswith("log:") for _, cb in buttons)  # R10's keyboard, even with clarify OFF


# ===========================================================================
# 5. /guide + /help render within Telegram's budget, both languages.
# ===========================================================================


@pytest.mark.parametrize("lang", ["en", "th"])
def test_guide_and_help_render_within_telegram_budget(config, lang):
    guide = discoverability.build_guide_text(config, lang)
    help_text = discoverability.build_help_text(config, lang)
    assert 0 < len(guide) < 4096
    assert 0 < len(help_text) < 4096
