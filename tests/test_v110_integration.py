"""SPEC-v1.10.md "Never lose a log" -- the sequential integration seam
(`core/routing.py` + `core/app.py`), Archi's own dispatch. M1 (`core/
clarify.py`), M2 (`core/reply_attribution.py`/`core/discoverability.py`),
and M3 (riders) are each already Vera-PASSED in isolation (`TEST-v1.10-
m1.md`/`TEST-v1.10-m2.md`, `tests/test_clarify.py`/`tests/test_unparsed_
closure.py`/`tests/test_reply_to_reminder.py`/`tests/test_outage_
honesty.py`/`tests/test_guide.py`/`tests/test_pause_failopen.py` and their
own adversarial gap suites) -- this file proves those modules ACTUALLY WIRE
TOGETHER through the real `core/routing.py`/`core/app.py` closures, which
none of those module-level suites exercise (M1's own IMPL.md: "no
`routing.py` wiring exists yet, by design"; M2's own TEST.md: "confirmed
NOT wired... deferred slices").

This is the release's own reason to exist (SPEC-v1.10.md §1): the
production zombies id=13 ("500") / id=14 ("Streaching") re-parsed on every
restart forever, with the user never told their log died. Every scenario
below drives the REAL `core/routing.py:handle_inbound_message`/
`reparse_pending_unparsed`/`on_callback` (and, for the menu/`/guide`
end-to-end proof, the REAL `core/app.py:async_main`), not a hand-simulated
restatement of them.

Live-environment rule (unchanged from every other integration test file in
this suite): every DB here is a scratch `tmp_path` SQLite file. Nothing
here ever opens `data/habits.db`, and no real Telegram/Ollama call is made
(all channels/LLMs are fakes; every `parse_message` is either a fake or
never reached at all, proving the zero-LLM claims)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import announce, discoverability, i18n, release_notes, reminders, routing
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.registry_provider import RegistryProvider
from habit_assistant.core.reminders import ReminderState
from habit_assistant.llm.ollama_client import ExtractionResult
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner"
OTHER = "other-user"


# ===========================================================================
# Shared fixtures.
# ===========================================================================


class FakeChannel(Channel):
    """Records `send`/`send_actionable` (both land in `.sent`, mirroring
    every other fake `Channel` in this suite); `send` returns a synthetic
    incrementing message id, mirroring `TelegramChannel.send`'s own
    `str | None` contract (R-SS5) closely enough to drive `ReminderState`'s
    reply-context map the same way a real send would (AC12/13)."""

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
        raise NotImplementedError("not exercised in the direct-call tests below")


class _HealthMonitor:
    """Minimal `.ollama_up`-only stand-in, mirroring `tests/test_
    resilience.py:_FrozenHealthMonitor`."""

    def __init__(self, ollama_up: bool) -> None:
        self.ollama_up = ollama_up


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(OWNER, role="member", status="active")
    database.upsert_user(OTHER, role="member", status="active")
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
    """Shaped exactly like the real production zombies id=13/14 (SPEC-
    v1.10.md §1): `category='unparsed'`, `unparsed_state` NULL (the
    pre-1.10/legacy shape migration 013 leaves untouched, R-SS1's own
    "NULL == awaiting_llm")."""
    return db_.insert_log(LogEntry(None, user_id, "2026-08-24T09:00:00", "unparsed", None, None, raw, "reply"))


async def _still_unknown(text, llm, registry, threshold):
    """A `parse_message` stand-in for "Ollama is back up but still can't
    place this row" -- the recovery sweep's own final-failure case (R1/R7)."""
    return ExtractionResult("unknown", None, 0.0)


# ===========================================================================
# AC5/AC6/AC8 (partial) -- the zombie loop, at the real wired sweep level.
# Seeded with the exact texts SPEC-v1.10.md §1 names ("500"/"Streaching").
# ===========================================================================


async def test_ac5_ac6_zombie_rows_close_or_offer_then_never_reparsed_again(db, config, registry, channel):
    """One recovery sweep, through the REAL `routing.reparse_pending_
    unparsed`: "500" has a tier-1 guess (water is unit-plausible against
    the default 2500ml goal) -> `awaiting_clarify` + the tap-to-fix offer
    (AC8's sweep-side half); "Streaching" has none -> `closed` + the ONE
    bilingual closure notification (AC6). Both PERMANENTLY leave
    `pending_unparsed()` (AC5/R2 -- no zombies). A second DOWN->UP sweep,
    with a `parse_message` that raises if ever called, makes ZERO LLM
    calls for either row -- this is AC5's own literal wording, proved by
    construction: the row is no longer even a candidate to iterate."""
    row_500 = _insert_zombie(db, OWNER, "500")
    row_streaching = _insert_zombie(db, OWNER, "Streaching")

    await routing.reparse_pending_unparsed(
        db, None, channel, config, registry=registry, parse_message=_still_unknown
    )

    assert db.pending_unparsed() == []  # both permanently gone (AC5/R2)

    row_500_after = db.get_log(row_500)
    assert row_500_after["category"] == "unparsed"
    assert row_500_after["unparsed_state"] == "awaiting_clarify"
    offer = next((t, b) for cid, t, b in channel.actionable if cid == OWNER and "500" in t)
    assert offer == (
        i18n.t("clarify_offer", "en", text="500"),
        [("\U0001f4a7 water 500ml", f"clarify:{row_500}:water:500")],
    )

    row_streaching_after = db.get_log(row_streaching)
    assert row_streaching_after["category"] == "unparsed"
    assert row_streaching_after["unparsed_state"] == "closed"
    closure_sends = [t for cid, t in channel.sent if cid == OWNER and "Streaching" in t]
    assert len(closure_sends) == 1  # the ONE closure notification, ever (AC6)
    assert closure_sends[0] == i18n.t("closure_notification", "en", text="Streaching")

    async def _must_not_be_called(text, llm, reg, threshold):  # pragma: no cover - proves it's unreachable
        raise AssertionError(f"parse_message must not be called for a terminal row: {text!r}")

    # Second DOWN->UP sweep: pending_unparsed() no longer returns either
    # row, so the loop body -- and therefore parse_message -- never runs.
    await routing.reparse_pending_unparsed(
        db, None, channel, config, registry=registry, parse_message=_must_not_be_called
    )
    assert db.pending_unparsed() == []


# ===========================================================================
# AC8/AC10/AC11 -- the tap-to-fix offer wired to a REAL on_callback tap:
# ordinary log, Undo works, no audit row, sweep-vs-tap race guard.
# ===========================================================================


async def test_ac8_ac10_tap_wired_through_on_callback_logs_confirms_undo_no_audit(
    db, config, registry, channel, provider
):
    row_id = _insert_zombie(db, OWNER, "500")
    await routing.reparse_pending_unparsed(
        db, None, channel, config, registry=registry, parse_message=_still_unknown
    )
    row = db.get_log(row_id)
    assert row["unparsed_state"] == "awaiting_clarify"  # AC8

    channel.sent.clear()
    channel.actionable.clear()

    await routing.on_callback(
        OWNER, f"clarify:{row_id}:water:500", "500", "cb-1", db=db, channel=channel, config=config, provider=provider
    )

    resolved = db.get_log(row_id)
    assert resolved["category"] == "water" and resolved["value_num"] == 500.0
    assert resolved["unparsed_state"] is None  # AC10: an ordinary log now

    _, text, buttons = channel.actionable[-1]
    assert text == i18n.t("recovered_water", "en", water_ml=500)
    assert buttons and buttons[0][1] == f"undo:{row_id}"  # Undo works (AC10)
    assert db.recent_audit(100) == []  # R12: a clarify-tap log is never audited

    # AC11: the sweep-vs-tap race guard. The row is already resolved
    # (category != 'unparsed'), so a second sweep's own CAS (guarded on
    # the DISJOINT (None, 'awaiting_llm') origin) is a pure no-op -- no
    # double log, no double notification.
    won = db.mark_unparsed_state(row_id, from_states=(None, "awaiting_llm"), to_state="closed")
    assert won is False
    assert db.get_log(row_id)["category"] == "water"  # untouched by the loser


async def test_ac11_concurrently_triggered_taps_exactly_one_wins(db, config, registry, channel, provider):
    """AC11's own tap-vs-tap flavor, driven through the REAL `on_callback`
    twice for the SAME row/payload -- the second call observes the CAS has
    already moved on and sends only the friendly no-op, never a second
    "Recovered" confirmation."""
    row_id = _insert_zombie(db, OWNER, "500")
    await routing.reparse_pending_unparsed(
        db, None, channel, config, registry=registry, parse_message=_still_unknown
    )
    channel.actionable.clear()
    channel.sent.clear()

    await routing.on_callback(
        OWNER, f"clarify:{row_id}:water:500", "500", "cb-a", db=db, channel=channel, config=config, provider=provider
    )
    await routing.on_callback(
        OWNER, f"clarify:{row_id}:water:500", "500", "cb-b", db=db, channel=channel, config=config, provider=provider
    )

    assert len(channel.actionable) == 1  # only ONE real, button-carrying confirmation
    assert channel.sent[-1] == (OWNER, i18n.t("clarify_already_handled", "en"))


# ===========================================================================
# AC9 -- live LLM-unknown (Ollama UP), both branches, wired through the
# REAL handle_inbound_message.
# ===========================================================================


async def test_ac9_live_unknown_with_guesses_offers_and_writes_a_row(db, config, registry, channel):
    await routing.handle_inbound_message(
        "500",
        db=db,
        llm=None,
        channel=channel,
        config=config,
        user_id=OWNER,
        registry=registry,
        health_monitor=_HealthMonitor(ollama_up=True),
        parse_message=_still_unknown,
    )

    assert db.pending_unparsed() == []  # excluded once awaiting_clarify (R8)
    row = db._conn.execute(
        "SELECT * FROM logs WHERE user_id = ? AND category = 'unparsed' ORDER BY id DESC LIMIT 1", (OWNER,)
    ).fetchone()
    assert row is not None and row["unparsed_state"] == "awaiting_clarify"
    _, text, buttons = channel.actionable[-1]
    assert text == i18n.t("clarify_offer", "en", text="500")
    assert buttons == [("\U0001f4a7 water 500ml", f"clarify:{row['id']}:water:500")]


async def test_ac9_live_unknown_without_guesses_generic_plus_log_keyboard_no_row(db, config, registry, channel):
    before = db._conn.execute("SELECT COUNT(*) AS n FROM logs").fetchone()["n"]

    await routing.handle_inbound_message(
        "kjshdfkjshdf gibberish",
        db=db,
        llm=None,
        channel=channel,
        config=config,
        user_id=OWNER,
        registry=registry,
        health_monitor=_HealthMonitor(ollama_up=True),
        parse_message=_still_unknown,
    )

    after = db._conn.execute("SELECT COUNT(*) AS n FROM logs").fetchone()["n"]
    assert after == before  # R6/R10: no guesses -> no row written at all
    _, text, buttons = channel.actionable[-1]
    assert text == i18n.t("clarifying_question", "en")
    assert buttons and all(cb.startswith("log:") for _, cb in buttons)  # the /log keyboard (R10)


async def test_ac9_clarify_disabled_always_takes_the_generic_path(db, registry, channel):
    config = Config.model_validate({"clarify": {"enabled": False}})
    before = db._conn.execute("SELECT COUNT(*) AS n FROM logs").fetchone()["n"]

    await routing.handle_inbound_message(
        "500",  # would otherwise guess water (see the enabled test above)
        db=db,
        llm=None,
        channel=channel,
        config=config,
        user_id=OWNER,
        registry=registry,
        health_monitor=_HealthMonitor(ollama_up=True),
        parse_message=_still_unknown,
    )

    after = db._conn.execute("SELECT COUNT(*) AS n FROM logs").fetchone()["n"]
    assert after == before  # generic path always -> no row
    _, text, buttons = channel.actionable[-1]
    assert text == i18n.t("clarifying_question", "en")
    assert all(cb.startswith("log:") for _, cb in buttons)  # never a clarify: guess button


# ===========================================================================
# AC12/AC13 -- reply-to-reminder, the full loop: reminder sent -> context
# captured -> reply "500" -> logged zero-LLM, both with Ollama UP and DOWN.
# ===========================================================================


async def test_ac12_reply_to_reminder_full_loop_zero_llm_ollama_up(db, config, registry, channel):
    state = ReminderState()
    water = registry.get("water")
    await reminders.send_reminder(channel, OWNER, water, "en", db, config, state)
    reminder_msg_id = next(iter(state.reminder_context[OWNER]))
    channel.sent.clear()
    channel.actionable.clear()

    await routing.handle_inbound_message(
        "500",
        db=db,
        llm=None,  # never touched -- reply-attribution resolves before preparse/the LLM
        channel=channel,
        config=config,
        user_id=OWNER,
        registry=registry,
        reminder_state=state,
        reply_to_message_id=reminder_msg_id,
        health_monitor=_HealthMonitor(ollama_up=True),
    )

    row = db.last_log(OWNER, category="water")
    assert row["value_num"] == 500.0 and row["source"] == "reply"
    _, text, buttons = channel.actionable[-1]
    assert text == i18n.t("water_confirmation", "en", water_ml=500, total=500, goal=2500, pct=20)
    assert buttons and buttons[0][1].startswith("undo:")


async def test_ac12_ac13_reply_to_reminder_works_zero_llm_while_ollama_down(db, config, registry, channel):
    """AC12's own "works while Ollama is DOWN" half -- an LLM that would
    raise if ever touched proves the attribution never reaches it, even
    though the health monitor reports DOWN (which would normally trigger
    the deferral branch for a message that ISN'T a mapped reply)."""

    class _RaisingLLM:
        async def chat_json(self, *a, **k):
            raise AssertionError("the LLM must never be called for a reply-to-reminder hit")

    state = ReminderState()
    stretch = registry.get("stretch")
    await reminders.send_reminder(channel, OWNER, stretch, "en", db, config, state)
    reminder_msg_id = next(iter(state.reminder_context[OWNER]))
    channel.sent.clear()
    channel.actionable.clear()

    await routing.handle_inbound_message(
        "15",
        db=db,
        llm=_RaisingLLM(),
        channel=channel,
        config=config,
        user_id=OWNER,
        registry=registry,
        reminder_state=state,
        reply_to_message_id=reminder_msg_id,
        health_monitor=_HealthMonitor(ollama_up=False),
    )

    row = db.last_log(OWNER, category="stretch")
    assert row["value_num"] == 15.0 and row["source"] == "reply"
    assert db.pending_unparsed() == []  # never deferred -- attributed directly


async def test_ac13_unmapped_reply_falls_through_to_the_normal_deferral_path(db, config, registry, channel):
    """R14's own conservatism: a reply to a message id the map doesn't
    know (never a reminder, or evicted, or post-restart) takes the normal
    path -- here, Ollama DOWN, so it defers exactly like a non-reply
    message would."""
    await routing.handle_inbound_message(
        "500",
        db=db,
        llm=None,
        channel=channel,
        config=config,
        user_id=OWNER,
        registry=registry,
        reminder_state=ReminderState(),  # empty map -- nothing was ever sent
        reply_to_message_id="999999",
        health_monitor=_HealthMonitor(ollama_up=False),
    )
    rows = db.logs_between(OWNER, "2000-01-01T00:00:00", "2100-01-01T00:00:00")
    assert len(rows) == 1 and rows[0]["category"] == "unparsed"  # deferred, not wrongly attributed


# ===========================================================================
# AC14 -- outage honesty, wired: default-on message + the row is still
# written; honest_reply=false restores deferred_ack byte-for-byte.
# ===========================================================================


async def test_ac14_outage_honesty_wired_default_on(db, config, registry, channel):
    await routing.handle_inbound_message(
        "went for a run",
        db=db,
        llm=None,
        channel=channel,
        config=config,
        user_id=OWNER,
        registry=registry,
        health_monitor=_HealthMonitor(ollama_up=False),
    )

    assert len(db.pending_unparsed()) == 1  # the deferral row is still written (R15)
    _, text, buttons = channel.actionable[-1]
    assert text == i18n.t("outage_honest_reply", "en", text="went for a run")
    assert buttons  # the /log keyboard (R15)


async def test_ac14_outage_honesty_disabled_restores_deferred_ack_byte_for_byte(db, registry, channel):
    config = Config.model_validate({"outage": {"honest_reply": False}})
    await routing.handle_inbound_message(
        "went for a run",
        db=db,
        llm=None,
        channel=channel,
        config=config,
        user_id=OWNER,
        registry=registry,
        health_monitor=_HealthMonitor(ollama_up=False),
    )

    assert len(db.pending_unparsed()) == 1
    assert channel.sent[-1] == (OWNER, i18n.t("deferred_ack", "en"))
    assert channel.actionable == []  # byte-for-byte the pre-1.10 shape: no keyboard either


# ===========================================================================
# AC15 -- /guide dispatches end-to-end (direct call), and the public/owner
# menus are 23/28 through the REAL wired async_main.
# ===========================================================================


async def test_ac15_guide_dispatches_end_to_end_bilingual(db, config, registry, channel):
    await routing.handle_inbound_message(
        "/guide", db=db, llm=None, channel=channel, config=config, user_id=OWNER, registry=registry
    )
    assert channel.sent[-1] == (OWNER, discoverability.build_guide_text(config, "en"))

    channel.sent.clear()
    await routing.handle_inbound_message(
        "คู่มือ", db=db, llm=None, channel=channel, config=config, user_id=OWNER, registry=registry
    )
    assert channel.sent[-1] == (OWNER, discoverability.build_guide_text(config, "th"))


class _StopAfterSchedulerStart(Exception):
    pass


class _MenuFakeScheduler:
    def __init__(self, *a, **k) -> None:
        pass

    def add_job(self, *a, **k) -> None:
        pass

    def start(self) -> None:
        pass

    def shutdown(self, wait: bool = False) -> None:
        pass


class _MenuFakeOllamaClient:
    def __init__(self, *a, **k) -> None:
        pass

    async def chat_text(self, *a, **k) -> str:
        return "noted"

    async def chat_json(self, *a, **k) -> str:
        return json.dumps({"category": "unknown", "value": None, "confidence": 0.1})

    async def probe_schema_support(self, *a, **k) -> dict:
        return {}

    async def aclose(self) -> None:
        pass


class _MenuFakeHealthMonitor:
    def __init__(self, *a, **k) -> None:
        pass

    async def run(self) -> None:
        return

    async def aclose(self) -> None:
        pass


class _MenuChannel(Channel):
    """Drives `core/app.py:async_main`'s real command-menu registration and
    a scripted `/guide` message through the REAL `on_message` closure."""

    last_instance: "_MenuChannel | None" = None
    script: list[tuple[str, str]] = []

    def __init__(self, *a, **k) -> None:
        self.sent: list[tuple[str, str]] = []
        self.set_my_commands_calls: list[tuple[dict, str | None]] = []
        self._next_id = 9000
        _MenuChannel.last_instance = self

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> str | None:
        self.sent.append((chat_id, text))
        self._next_id += 1
        return str(self._next_id)

    async def set_my_commands(self, commands, *, scope_chat_id: str | None = None) -> None:
        self.set_my_commands_calls.append((commands, scope_chat_id))

    async def run(self, on_message, on_callback=None) -> None:
        for chat_id, text in _MenuChannel.script:
            await on_message(chat_id, text, None, "m1")
        raise _StopAfterSchedulerStart()

    async def aclose(self) -> None:
        pass


async def test_ac15_public_and_owner_menus_are_23_and_28_and_guide_reaches_the_real_on_message(
    tmp_path, monkeypatch
):
    from habit_assistant import main as main_module

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module, "load_secrets", lambda: SimpleNamespace(telegram_bot_token="fake-token", telegram_chat_id=OWNER)
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _MenuFakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", _MenuChannel)
    monkeypatch.setattr(main_module, "OllamaClient", _MenuFakeOllamaClient)
    monkeypatch.setattr(main_module, "HealthMonitor", _MenuFakeHealthMonitor)
    monkeypatch.setattr(main_module, "__version__", "1.10.0")
    _MenuChannel.script = [(OWNER, "/guide")]

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    channel = _MenuChannel.last_instance
    assert len(channel.set_my_commands_calls) == 2
    public_commands, public_scope = channel.set_my_commands_calls[0]
    owner_commands, owner_scope = channel.set_my_commands_calls[1]
    assert public_scope is None
    assert owner_scope == OWNER
    for lang, entries in public_commands.items():
        names = [n for n, _d in entries]
        assert len(names) == 23, f"{lang}: public menu is {len(names)}, not 23: {names}"
        assert "guide" in names
    for lang, entries in owner_commands.items():
        assert len(entries) == 28, f"{lang}: owner menu is {len(entries)}, not 28"

    guide_config = Config()  # the async_main-loaded config above, structurally identical
    assert channel.sent[-1] == (OWNER, discoverability.build_guide_text(guide_config, "en"))


# ===========================================================================
# AC18 -- release notes announced once per active user per version.
# ===========================================================================


async def test_ac18_release_1_10_0_announced_once_per_user(db, channel):
    # `i18n.language: "en"` -- announce_release resolves via `i18n.resolve_
    # unprompted_language`, which defaults to Thai (ROADMAP.md v0.6.0
    # AC6.3), unlike a reply's own `resolve_reply_language`; forcing EN
    # here keeps this test deterministic and readable.
    config = Config.model_validate({"i18n": {"language": "en"}})
    await announce.announce_release(db, channel, config, "1.10.0")

    sent_to_owner = [text for cid, text in channel.sent if cid == OWNER]
    sent_to_other = [text for cid, text in channel.sent if cid == OTHER]
    assert sent_to_owner == [release_notes.get_release_note("1.10.0", "en")]
    assert sent_to_other == [release_notes.get_release_note("1.10.0", "en")]
    assert db.get_last_announced_version(OWNER) == "1.10.0"
    assert db.get_last_announced_version(OTHER) == "1.10.0"

    channel.sent.clear()
    await announce.announce_release(db, channel, config, "1.10.0")
    assert channel.sent == []  # idempotent -- already announced, no resend


# ===========================================================================
# Two-user isolation across every new v1.10 surface: the reminder-context
# map, and the recovery sweep.
# ===========================================================================


async def test_two_user_isolation_reminder_context_and_recovery_sweep(db, config, registry, channel):
    # Reminder-context isolation: OTHER replying with OWNER's own reminder
    # message id must find nothing (the map is per-chat, R-SS6) and fall
    # through to the normal path -- never OWNER's water habit.
    state = ReminderState()
    await reminders.send_reminder(channel, OWNER, registry.get("water"), "en", db, config, state)
    owner_reminder_msg_id = next(iter(state.reminder_context[OWNER]))
    channel.sent.clear()
    channel.actionable.clear()

    await routing.handle_inbound_message(
        "500",
        db=db,
        llm=None,
        channel=channel,
        config=config,
        user_id=OTHER,
        registry=registry,
        reminder_state=state,
        reply_to_message_id=owner_reminder_msg_id,
        health_monitor=_HealthMonitor(ollama_up=False),
    )
    other_rows = db.logs_between(OTHER, "2000-01-01T00:00:00", "2100-01-01T00:00:00")
    assert len(other_rows) == 1 and other_rows[0]["category"] == "unparsed"  # not cross-attributed to water

    # Recovery-sweep isolation: both users have their own "500" zombie row
    # -- each gets exactly their OWN offer, addressed to their own chat.
    row_owner = _insert_zombie(db, OWNER, "500")
    row_other = _insert_zombie(db, OTHER, "500")
    channel.sent.clear()
    channel.actionable.clear()

    await routing.reparse_pending_unparsed(
        db, None, channel, config, registry=registry, parse_message=_still_unknown
    )

    # Note: OTHER may ALSO have received an outage-honesty offer for the
    # earlier deferred "500" reply-fallthrough above (a DIFFERENT row,
    # same coincidental text) -- the isolation claim under test is that
    # EACH row's own offer reaches ONLY its own owner, not an exact count,
    # so assert on the specific callback_data rather than list length.
    owner_callback_datas = {cb for cid, _t, b in channel.actionable if cid == OWNER for _lbl, cb in b}
    other_callback_datas = {cb for cid, _t, b in channel.actionable if cid == OTHER for _lbl, cb in b}
    assert f"clarify:{row_owner}:water:500" in owner_callback_datas
    assert f"clarify:{row_other}:water:500" in other_callback_datas
    assert f"clarify:{row_owner}:water:500" not in other_callback_datas  # never cross-delivered
    assert f"clarify:{row_other}:water:500" not in owner_callback_datas


# ===========================================================================
# The inert gate: no v1.10 feature invoked (no reply, Ollama UP, a plain
# deterministic preparse hit) -> byte-identical to v1.9.4's own output.
# ===========================================================================


async def test_inert_gate_ordinary_log_byte_identical_to_pre_1_10(db, config, registry, channel):
    await routing.handle_inbound_message(
        "500ml",
        db=db,
        llm=None,
        channel=channel,
        config=config,
        user_id=OWNER,
        registry=registry,
        health_monitor=_HealthMonitor(ollama_up=True),
    )

    _, text, buttons = channel.actionable[-1]
    assert text == i18n.t("water_confirmation", "en", water_ml=500, total=500, goal=2500, pct=20)
    assert buttons and buttons[0][1].startswith("undo:")
    assert db.pending_unparsed() == []  # no v1.10 row of any kind was written
