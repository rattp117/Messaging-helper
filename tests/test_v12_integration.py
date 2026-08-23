"""SPEC-v1.2.md §11 integration step -- the final pass that wires the three
independently-shipped parallel modules (`access`, `preferences`,
`schedules`) into `main.py`'s real `on_message`/`on_callback` closures:
the access gate (R-A1) running before any logging/LLM/command work, admin
command routing, `lang`/`quiet`/`remind` routing inside
`handle_inbound_message`, per-user language threading into every
unprompted/reply send site (R-P1), and `display_name` capture from a real
Telegram update.

Every module's own test file (`test_access.py`/`test_v12_access_gaps.py`,
`test_preferences.py`, `test_schedules.py`) already proves its owned ACs
in isolation, calling its own `execute_*`/`handle_gate` functions
directly. This file is different in kind: it drives the REAL, wired
`async_main`/`on_message`/`on_callback` closures (mirroring
`tests/test_v11_integration.py`'s own `_run_async_main` pattern) so a
genuine wiring mistake -- a missed branch, wrong argument order, a
call site the coordinator's punch list named but this pass forgot --
would show up here even though every module's own unit tests stay green.

Covers the integration-owned ACs from SPEC-v1.2.md §11's table (S1, S4,
S6, C1, C2, U-ISO, M1-M3, O1, X1) plus the punch list's own explicit
"stranger onboards -> owner approves -> both users log/undo/target/
remind/lang independently with zero cross-visibility" life-cycle.

Live-environment rule (unchanged from every other v1.2 test file): every
DB here is a scratch `tmp_path` SQLite file. Nothing in this file ever
opens `data/habits.db`, and no real Telegram/Ollama call is made."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.channels.telegram import TelegramChannel
from habit_assistant.config import Config
from habit_assistant.core import access, i18n
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.health import HealthMonitor
from habit_assistant.core.reminders import ReminderState, effective_quiet_windows, run_due_reminders
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "1001"
STRANGER = "2002"
STRANGER_DISPLAY_NAME = "Dana"


# ---------------------------------------------------------------------------
# Shared fakes/helpers -- mirrors tests/test_v11_integration.py's own
# `_FakeScheduler`/`_AsyncMainFakeChannel`/`_run_async_main`/
# `_StopAfterSchedulerStart` pattern, generalized to an arbitrary ordered
# SCRIPT of message/callback steps (the two-user life-cycle below needs
# more than the 1-message/1-callback/1-second-message shape that file's
# own fake supports).
# ---------------------------------------------------------------------------


class _StopAfterSchedulerStart(Exception):
    pass


class _FakeScheduler:
    last_instance: "_FakeScheduler | None" = None

    def __init__(self, *args, **kwargs):
        self.jobs: dict[str, object] = {}
        _FakeScheduler.last_instance = self

    def add_job(self, func, trigger=None, args=None, id=None, replace_existing=True, **kwargs):
        self.jobs[id] = SimpleNamespace(func=func, trigger=trigger, args=args, id=id)

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def start(self):
        pass

    def shutdown(self, wait=False):
        pass


class _FakeOllamaClient:
    """Unlike `test_cli.py`'s single-response fakes, this one serves a
    QUEUE of canned `chat_json` responses, consumed in call order -- the
    two-user life-cycle logs several different habits/values across
    several different messages in one `async_main` run, each needing its
    own extraction result. An empty queue falls back to `unknown`
    (never crashes, matches `parse_message`'s own fail-closed contract)."""

    responses: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def chat_text(self, system_prompt, user_prompt):
        return "noted"

    async def chat_json(self, system_prompt, user_prompt, json_schema, valid_categories):
        if _FakeOllamaClient.responses:
            return _FakeOllamaClient.responses.pop(0)
        return json.dumps({"category": "unknown", "value": None, "confidence": 0.1})

    async def probe_schema_support(self, *args, **kwargs) -> dict:
        return {}

    async def aclose(self) -> None:
        pass


def _extraction(category: str, value, confidence: float = 0.9) -> str:
    return json.dumps({"category": category, "value": value, "confidence": confidence})


def _query_intent(habit_id: str, metric: str, timeframe: str) -> str:
    # core/query.py:_validate_intent reads the habit id under the key
    # "category" (matching the extraction schema's own field name), not
    # "habit_id" -- despite QueryIntent's own dataclass field being called
    # habit_id.
    return json.dumps({"category": habit_id, "metric": metric, "timeframe": timeframe})


class _CountingOllamaClient(_FakeOllamaClient):
    """Vera's addition: counts `chat_json` calls so a gated-off message can
    be PROVEN to never reach the LLM boundary at all (not just "the reply
    looks like it wasn't parsed") -- AC-A1's "no LLM call" clause, verified
    through the real wiring rather than `access.handle_gate`'s signature
    alone (which `tests/test_v12_access_gaps.py::
    test_handle_gate_signature_has_no_llm_reference` already covers at the
    module level)."""

    last_instance: "_CountingOllamaClient | None" = None

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.call_count = 0
        _CountingOllamaClient.last_instance = self

    async def chat_json(self, *args, **kwargs):
        self.call_count += 1
        return await super().chat_json(*args, **kwargs)


class _ScriptedChannel(Channel):
    """Drives the REAL `on_message`/`on_callback` closures `async_main`
    wires, in an arbitrary caller-supplied order -- set `_ScriptedChannel.
    script` before invoking `async_main`."""

    last_instance: "_ScriptedChannel | None" = None
    script: list[tuple] = []
    # Vera's addition: job ids (as registered on `_FakeScheduler`, e.g.
    # "daily_summary"/"weekly_review") to invoke directly -- awaited AFTER
    # the scripted message/callback steps but BEFORE raising
    # `_StopAfterSchedulerStart`, i.e. still INSIDE async_main's live `db`
    # connection (which `async_main`'s own `finally` block closes the
    # instant `channel.run()` raises). Calling a job's closure only after
    # `_run()` returns would hit a closed sqlite3 connection -- this is why
    # it has to happen here, not from the test body.
    run_jobs_before_stop: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        self.sent: list[tuple[str, str]] = []
        self.actionable: list[tuple[str, str, list]] = []
        self.set_my_commands_calls: list[dict] = []
        _ScriptedChannel.last_instance = self

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    async def send_actionable(self, chat_id: str, text: str, buttons) -> None:
        self.actionable.append((chat_id, text, buttons))
        self.sent.append((chat_id, text))

    async def set_my_commands(self, commands) -> None:
        self.set_my_commands_calls.append(commands)

    def sent_to(self, chat_id: str) -> list[str]:
        return [text for cid, text in self.sent if cid == chat_id]

    async def run(self, on_message, on_callback=None) -> None:
        for step in _ScriptedChannel.script:
            if step[0] == "message":
                _, chat_id, text, display_name = step
                await on_message(chat_id, text, display_name)
            else:
                _, chat_id, data, source_text, cb_id = step
                assert on_callback is not None
                await on_callback(chat_id, data, source_text, cb_id)
        for job_id in _ScriptedChannel.run_jobs_before_stop:
            job = _FakeScheduler.last_instance.jobs.get(job_id)
            if job is not None:
                await job.func()
        raise _StopAfterSchedulerStart()

    async def aclose(self) -> None:
        pass


def _run_async_main(monkeypatch, config, script, owner_chat_id=OWNER, responses=None, run_jobs=None, channel_cls=None):
    from habit_assistant import main as main_module

    channel_cls = channel_cls or _ScriptedChannel

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module,
        "load_secrets",
        lambda: SimpleNamespace(telegram_bot_token="fake-token", telegram_chat_id=owner_chat_id),
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", channel_cls)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClient)
    _FakeScheduler.last_instance = None
    _ScriptedChannel.last_instance = None
    _ScriptedChannel.script = script
    _ScriptedChannel.run_jobs_before_stop = list(run_jobs or [])
    _FakeOllamaClient.responses = list(responses or [])
    return main_module


async def _run(monkeypatch, config, script, owner_chat_id=OWNER, responses=None, run_jobs=None, channel_cls=None):
    main_module = _run_async_main(monkeypatch, config, script, owner_chat_id, responses, run_jobs, channel_cls)
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)
    return _ScriptedChannel.last_instance


# ===========================================================================
# The big one: full two-user life-cycle through the REAL wiring.
# AC-A1/AC-A2 (onboarding+approve), AC-C1 (per-chat delivery+ownership),
# AC-U-ISO/AC-U1 (isolation), AC-P1 (per-user language, live through the
# wiring, not just execute_lang directly).
# ===========================================================================


async def test_full_two_user_lifecycle_onboarding_through_isolated_use(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})

    script = [
        # 1. STRANGER's first-ever message: unknown -> gated off, pending
        #    row created, onboarded, owner notified (display_name carried
        #    all the way from the "Telegram update" through on_message's
        #    3rd arg into handle_gate).
        ("message", STRANGER, "500ml", STRANGER_DISPLAY_NAME),
        # 2. OWNER approves.
        ("message", OWNER, f"/approve {STRANGER}", None),
        # 3. STRANGER, now active, logs for real.
        ("message", STRANGER, "500ml", STRANGER_DISPLAY_NAME),
        # 4. OWNER logs something different.
        ("message", OWNER, "10 min stretch", None),
        # 5/6. Each checks their own /habits overview.
        ("message", STRANGER, "/habits", STRANGER_DISPLAY_NAME),
        ("message", OWNER, "/habits", None),
        # 7. STRANGER sets their own water target.
        ("message", STRANGER, "/target water 2000", STRANGER_DISPLAY_NAME),
        # 8. STRANGER opts into Thai.
        ("message", STRANGER, "/lang th", STRANGER_DISPLAY_NAME),
        # 9. STRANGER's next message is plain English text -- but their
        #    stored /lang pref should now win over auto-detection.
        ("message", STRANGER, "500ml", STRANGER_DISPLAY_NAME),
        # 10. OWNER's own English message is unaffected by STRANGER's
        #     /lang choice (AC-P1's own "owner unaffected" clause).
        ("message", OWNER, "500ml", None),
        # 11. STRANGER undoes their own last entry.
        ("message", STRANGER, "/undo", STRANGER_DISPLAY_NAME),
    ]
    responses = [
        # Step 1 never calls chat_json at all -- STRANGER is gated off
        # before handle_inbound_message/parse_message is ever reached.
        # SPEC-v1.5.md R-L1 (module `preparse`): steps 3, 9, and 10 are all
        # bare "500ml" -- a whole-message "NUMBER UNIT" shape that now
        # resolves deterministically without ever reaching `chat_json` --
        # so none of them consume a queue entry any more. Only step 4
        # ("10 min stretch", not a whole-message "NUMBER UNIT" shape) still
        # falls through to the LLM.
        _extraction("stretch", 10),  # step 4
    ]
    channel = await _run(monkeypatch, config, script, responses=responses)
    db = Database(tmp_path / "habits.db")
    try:
        # --- AC-A1: step 1 gated off, onboarding replies ---
        assert channel.sent_to(STRANGER)[0] == i18n.t("access_pending", "en")
        owner_first = channel.sent_to(OWNER)[0]
        assert STRANGER in owner_first and STRANGER_DISPLAY_NAME in owner_first
        # (The whole script runs in one uninterrupted async_main call, so
        # there's no mid-script hook to snapshot STRANGER's row as still
        # "pending" right after step 1 -- the onboarding REPLIES above,
        # plus step 3 producing exactly one water row below, are what
        # prove step 1 didn't log/grant anything by itself.)

        # --- AC-A2: /approve grants access ---
        assert db.get_user(STRANGER)["status"] == "active"
        assert any("granted" in t.lower() or "เข้าใช้งานได้" in t for t in channel.sent_to(STRANGER))

        # --- AC-C1: step 3's confirmation went to STRANGER, row owned by STRANGER ---
        stranger_rows = db.logs_between(STRANGER, "2000-01-01T00:00:00", "2100-01-01T00:00:00")
        assert len(stranger_rows) == 1 and stranger_rows[0]["category"] == "water"
        owner_rows = db.logs_between(OWNER, "2000-01-01T00:00:00", "2100-01-01T00:00:00")
        assert any(r["category"] == "stretch" for r in owner_rows)
        assert not any(r["category"] == "water" and r["value_num"] == 500.0 for r in owner_rows if r is stranger_rows)

        # --- AC-U-ISO: /habits shows only the asking user's own data ---
        stranger_habits = channel.sent_to(STRANGER)
        owner_habits = channel.sent_to(OWNER)
        stranger_overview = next(t for t in stranger_habits if i18n.t("habits_overview_header", "en") in t)
        owner_overview = next(t for t in owner_habits if i18n.t("habits_overview_header", "en") in t)
        assert "today 500 ml" in stranger_overview
        assert "today 0 ml" in owner_overview  # owner never logged water -- their own total stays 0
        assert "today 10 min" in owner_overview  # but their own stretch entry IS reflected

        # --- AC-U1: target isolation ---
        assert db.get_target(STRANGER, "water") == 2000.0
        assert db.get_target(OWNER, "water") is None

        # --- AC-P1: STRANGER's own subsequent messages go Thai, OWNER's stay English ---
        stranger_water_confirmations = [t for t in channel.sent_to(STRANGER) if "บันทึก" in t or "logged" in t]
        assert any("บันทึก" in t for t in stranger_water_confirmations)  # at least one Thai confirmation
        owner_water_confirmations = [t for t in channel.sent_to(OWNER) if "logged" in t]
        assert owner_water_confirmations and all("บันทึก" not in t for t in owner_water_confirmations)

        # --- AC-U-ISO (undo): STRANGER's /undo only touches their own row ---
        assert any("Undone" in t or "ยกเลิก" in t for t in channel.sent_to(STRANGER))
        for row in db.logs_between(OWNER, "2000-01-01T00:00:00", "2100-01-01T00:00:00"):
            assert row["deleted_at"] is None  # owner's rows untouched by stranger's undo
    finally:
        db.close()


# ===========================================================================
# AC-C2 (extended to the gate, R-A1): on_callback must not execute for a
# non-active chat, even a stale/spoofed button tap.
# ===========================================================================


async def test_on_callback_gate_blocks_a_blocked_chats_button_tap(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.upsert_user(STRANGER, role="member", status="active")
    row_id = seed_db.insert_log(
        __import__("habit_assistant.storage.models", fromlist=["LogEntry"]).LogEntry(
            None, STRANGER, "2026-08-21T09:00:00", "water", 500.0, None, "500ml", "reply"
        )
    )
    seed_db.upsert_user(STRANGER, status="blocked")  # blocked AFTER the row existed (stale button scenario)
    seed_db.close()

    script = [("callback", STRANGER, f"undo:{row_id}", "500ml", "cb-1")]
    channel = await _run(monkeypatch, config, script)

    db = Database(tmp_path / "habits.db")
    try:
        row = db.get_log(row_id)
        assert row["deleted_at"] is None  # the tap must not have executed
        assert channel.sent_to(STRANGER) == []  # no reply either -- silent no-op, per this pass's own design
    finally:
        db.close()


async def test_on_callback_gate_lets_an_active_chats_button_tap_through(tmp_path, monkeypatch):
    """Sanity converse of the above -- an ACTIVE (non-owner) chat's own
    undo tap still works exactly as before the gate was added."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.upsert_user(STRANGER, role="member", status="active")
    row_id = seed_db.insert_log(
        __import__("habit_assistant.storage.models", fromlist=["LogEntry"]).LogEntry(
            None, STRANGER, "2026-08-21T09:00:00", "water", 500.0, None, "500ml", "reply"
        )
    )
    seed_db.close()

    script = [("callback", STRANGER, f"undo:{row_id}", "500ml", "cb-1")]
    channel = await _run(monkeypatch, config, script)

    db = Database(tmp_path / "habits.db")
    try:
        row = db.get_log(row_id)
        assert row["deleted_at"] is not None
        assert any("Undone" in t for t in channel.sent_to(STRANGER))
    finally:
        db.close()


# ===========================================================================
# AC-M3 (owner byte-identical): the gate is a true no-op for the owner --
# same exact confirmation string as the pre-v1.2 spec example, through the
# real gated wiring, not just a direct handle_inbound_message call.
# ===========================================================================


async def test_ac_m3_owner_confirmation_is_byte_identical_through_the_gated_wiring(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("message", OWNER, "500ml", None)]
    channel = await _run(monkeypatch, config, script, responses=[_extraction("water", 500)])

    assert channel.sent_to(OWNER) == ["✅ 500 ml logged — today 500 / 2500 ml (20%)"]


# ===========================================================================
# AC-S1/AC-S4/AC-S6: a live /remind write through the real on_message path
# takes effect on the very next tick, with no scheduler rebuild, and still
# honors that same user's quiet hours.
# ===========================================================================


async def test_ac_s4_remind_write_through_real_on_message_is_picked_up_by_the_next_tick(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("message", OWNER, "/remind water 09:15", None)]
    await _run(monkeypatch, config, script)

    # No scheduler/job object involved below -- proving there is nothing
    # to rebuild (mirrors test_schedules.py's own AC-S4 proof, but this
    # time the WRITE went through the real on_message/handle_inbound_
    # message routing this integration step just wired, not a direct
    # execute_remind call).
    from habit_assistant.core.habits import HabitRegistry

    db = Database(tmp_path / "habits.db")
    try:
        db.upsert_user(OWNER, role="owner", status="active")
        registry = HabitRegistry.from_config(config)
        fake_channel = _ScriptedChannel()
        state = ReminderState()
        await run_due_reminders(fake_channel, config, registry, db, state, clock=lambda: datetime(2026, 8, 21, 9, 15, 0))
        assert fake_channel.sent_to(OWNER) != []
        fake_channel.sent.clear()
        # The OLD config time (08:00, water's default) must NOT fire for
        # the owner anymore -- their custom time replaced it (AC-S2).
        await run_due_reminders(fake_channel, config, registry, db, state, clock=lambda: datetime(2026, 8, 21, 8, 0, 0))
        assert fake_channel.sent_to(OWNER) == []
    finally:
        db.close()


async def test_ac_s6_custom_time_reminder_still_honors_that_users_quiet_hours(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [
        ("message", OWNER, "/remind water 23:30", None),
        ("message", OWNER, "/quiet 23:00-07:00", None),
    ]
    await _run(monkeypatch, config, script)

    from habit_assistant.core.habits import HabitRegistry

    db = Database(tmp_path / "habits.db")
    try:
        registry = HabitRegistry.from_config(config)
        fake_channel = _ScriptedChannel()
        state = ReminderState()
        # TEST-v1.2-schedules.md's own documented testability gotcha:
        # send_reminder's quiet-hours check reads the REAL datetime.now(tz)
        # from core/reminders.py -- not the `clock` callable passed to
        # run_due_reminders below (which only drives "which HH:MM is due")
        # -- so it must be frozen separately, mirroring tests/test_streaks.
        # py's own `_freeze_reminders_clock` helper.
        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                fixed = datetime(2026, 8, 21, 23, 30, 0)
                return fixed.replace(tzinfo=tz) if tz is not None else fixed

        monkeypatch.setattr("habit_assistant.core.reminders.datetime", _FixedDatetime)
        await run_due_reminders(fake_channel, config, registry, db, state, clock=lambda: datetime(2026, 8, 21, 23, 30, 0))
        assert fake_channel.sent_to(OWNER) == []  # suppressed -- inside the owner's own quiet window
    finally:
        db.close()


# ===========================================================================
# AC-O1 (health alerts owner-only, re-confirmed with a second active user
# present -- the multi-user angle the shared surface's own test can't
# exercise since it predates a second user existing at all).
# ===========================================================================


async def test_ac_o1_health_alert_reaches_only_the_owner_even_with_other_active_users(tmp_path):
    db = Database(tmp_path / "habits.db")
    try:
        db.upsert_user(OWNER, role="owner", status="active")
        db.upsert_user(STRANGER, role="member", status="active")

        sent: list[tuple[str, str]] = []

        class _RecordingChannel(Channel):
            async def send(self, chat_id, text):
                sent.append((chat_id, text))

            async def run(self, on_message, on_callback=None):
                raise NotImplementedError

        monitor = HealthMonitor(
            "http://mac-mini:11434", "fake-token", OWNER, channel=_RecordingChannel(), language="en"
        )
        await monitor._alert("Ollama is DOWN")

        assert sent == [(OWNER, "Ollama is DOWN")]
        assert not any(chat_id == STRANGER for chat_id, _ in sent)
    finally:
        db.close()


# ===========================================================================
# AC-X1: inbound updates are processed sequentially -- no concurrent LLM
# extraction from the inbound loop, even across two different users' messages.
# ===========================================================================


async def test_ac_x1_inbound_messages_from_two_users_are_processed_sequentially(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    order: list[str] = []

    class _OrderTrackingOllamaClient(_FakeOllamaClient):
        async def chat_json(self, system_prompt, user_prompt, json_schema, valid_categories):
            order.append(f"start:{user_prompt[:12]}")
            result = await super().chat_json(system_prompt, user_prompt, json_schema, valid_categories)
            order.append(f"end:{user_prompt[:12]}")
            return result

    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.upsert_user(STRANGER, role="member", status="active")
    seed_db.close()

    # SPEC-v1.5.md R-L1 (module `preparse`): a bare "500ml" now resolves
    # deterministically, without ever reaching `chat_json` -- this test's
    # own point is proving no interleaving BETWEEN two concurrent LLM
    # calls, so both messages here are phrased to fall through the
    # pre-parser (not a whole-message "NUMBER UNIT" shape) and still
    # genuinely reach the extractor.
    script = [
        ("message", OWNER, "drank some water", None),
        ("message", STRANGER, "did some stretching", None),
    ]
    main_module = _run_async_main(
        monkeypatch, config, script, responses=[_extraction("water", 500), _extraction("stretch", 10)]
    )
    monkeypatch.setattr(main_module, "OllamaClient", _OrderTrackingOllamaClient)
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    # Each call's start/end pair is adjacent -- never interleaved with the
    # other user's own start/end pair (proves no concurrent extraction).
    assert order[0].startswith("start:") and order[1].startswith("end:")
    assert order[2].startswith("start:") and order[3].startswith("end:")


# ===========================================================================
# display_name plumbing: TelegramChannel._display_name_of, unit-level.
# The full life-cycle test above already confirms it end-to-end via
# access_request; this pins the extraction helper itself.
# ===========================================================================


def test_telegram_display_name_of_extracts_first_name():
    message = {"chat": {"id": 123}, "from": {"id": 123, "first_name": "Dana", "is_bot": False}, "text": "hi"}
    assert TelegramChannel._display_name_of(message) == "Dana"


def test_telegram_display_name_of_missing_from_falls_back_to_none():
    message = {"chat": {"id": 123}, "text": "hi"}
    assert TelegramChannel._display_name_of(message) is None


def test_telegram_display_name_of_blank_first_name_falls_back_to_none():
    message = {"chat": {"id": 123}, "from": {"first_name": ""}, "text": "hi"}
    assert TelegramChannel._display_name_of(message) is None


# ===========================================================================
# Command menu: the merged, integration-owned set (see main.py's own
# START_/LANG_/QUIET_/REMIND_COMMAND_DESCRIPTIONS docstring for the
# "admin commands excluded" design call) -- re-confirmed here since it's
# this pass's own addition, alongside tests/test_discoverability.py's own
# already-updated exact-set check.
# ===========================================================================


async def test_command_menu_public_set_excludes_the_four_admin_only_commands(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    channel = await _run(monkeypatch, config, script=[])

    registered = channel.set_my_commands_calls[0]
    for lang, entries in registered.items():
        names = {name for name, _desc in entries}
        # SPEC-v1.4.md's own integration step added "history" to this set
        # (R-A2 -- every active user's own data, unlike owner-only /audit).
        # SPEC-v1.5.md's own integration step added "checkin" too; "dnd" is
        # deliberately absent (shares /quiet's own menu entry).
        assert names == {"start", "undo", "target", "help", "habits", "remind", "lang", "quiet", "history", "checkin"}
        assert not names & {"approve", "block", "users", "invite"}


# ===========================================================================
# AC-A7-adjacent: the gate fails safe even inside the real wiring (a
# `users` lookup error mid-loop must deny, never grant) -- re-confirmed
# through the real on_message closure, not just access.classify directly.
# ===========================================================================


async def test_gate_fails_safe_through_real_on_message_when_users_lookup_raises(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("message", STRANGER, "500ml", None)]
    main_module = _run_async_main(monkeypatch, config, script)

    # Only STRANGER's own lookup fails -- OWNER's lookups (startup's own
    # `attribute_legacy_to_owner`, which calls `get_user` internally
    # before the scheduler/gate is even reached) must keep working, or
    # `async_main` itself would crash before this test ever gets to
    # exercise the gate at all.
    real_get_user = Database.get_user

    def selectively_raising_get_user(self, chat_id):
        if chat_id == STRANGER:
            raise RuntimeError("simulated DB read failure")
        return real_get_user(self, chat_id)

    monkeypatch.setattr(Database, "get_user", selectively_raising_get_user)
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    channel = _ScriptedChannel.last_instance
    # Fails closed (classify -> "blocked" on a lookup error, AC-A7) --
    # denied, not the pending-onboarding flow, and definitely not granted.
    assert channel.sent_to(STRANGER) == [i18n.t("access_denied", "en")]


# ===========================================================================
# Vera's integration-gate adversarial additions (coordinator's punch list,
# 2026-08-21). Everything below drives the REAL `async_main`/`on_message`/
# `on_callback` wiring, same conventions as the tests above -- tmp_path-only
# SQLite, mocked LLM/Telegram, never `data/habits.db`.
# ===========================================================================


# ---------------------------------------------------------------------------
# 1. Gate security: nothing logged, zero LLM calls, exactly one owner
# notification across repeats; a blocked chat's message is denial-only;
# a forged undo callback for the OWNER's own log, from a non-active
# attacker chat, is refused by the LIGHTER on_callback gate (access.classify,
# not the full handle_gate) -- probed for all three non-active states.
# ---------------------------------------------------------------------------


async def test_stranger_gate_never_logs_or_calls_the_llm_and_notifies_owner_exactly_once(tmp_path, monkeypatch):
    """AC-A1, end to end through the real wiring: three different messages
    from the SAME still-pending stranger (a plain log attempt, a plain
    question, and even `/start`) must each get `access_pending`, but the
    owner must be notified only ONCE (on first contact), zero `logs` rows
    for the stranger must ever be written, and the LLM boundary
    (`OllamaClient.chat_json`) must never be called at all -- not "called
    and its result discarded", never CALLED."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [
        ("message", STRANGER, "500ml", STRANGER_DISPLAY_NAME),
        ("message", STRANGER, "are you there?", STRANGER_DISPLAY_NAME),
        ("message", STRANGER, "/start", STRANGER_DISPLAY_NAME),
    ]
    main_module = _run_async_main(monkeypatch, config, script)
    monkeypatch.setattr(main_module, "OllamaClient", _CountingOllamaClient)
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)
    channel = _ScriptedChannel.last_instance

    assert _CountingOllamaClient.last_instance.call_count == 0

    db = Database(tmp_path / "habits.db")
    try:
        assert db._conn.execute("SELECT COUNT(*) AS n FROM logs WHERE user_id = ?", (STRANGER,)).fetchone()["n"] == 0
        assert db.get_user(STRANGER)["status"] == "pending"
    finally:
        db.close()

    assert channel.sent_to(STRANGER) == [i18n.t("access_pending", "en")] * 3
    assert len(channel.sent_to(OWNER)) == 1


async def test_blocked_users_message_gets_denial_only_nothing_logged_no_llm_call(tmp_path, monkeypatch):
    """AC-A3/R-A3 through the real wiring: a blocked chat's message gets
    `access_denied` and nothing else -- no owner side-channel, no log row,
    no LLM call."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.upsert_user(STRANGER, status="blocked")
    seed_db.close()

    script = [("message", STRANGER, "500ml", None)]
    main_module = _run_async_main(monkeypatch, config, script)
    monkeypatch.setattr(main_module, "OllamaClient", _CountingOllamaClient)
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)
    channel = _ScriptedChannel.last_instance

    assert channel.sent_to(STRANGER) == [i18n.t("access_denied", "en")]
    assert channel.sent_to(OWNER) == []
    assert _CountingOllamaClient.last_instance.call_count == 0

    db = Database(tmp_path / "habits.db")
    try:
        assert db._conn.execute("SELECT COUNT(*) AS n FROM logs WHERE user_id = ?", (STRANGER,)).fetchone()["n"] == 0
    finally:
        db.close()


@pytest.mark.parametrize("attacker_status", [None, "pending", "blocked"], ids=["unknown", "pending", "blocked"])
async def test_forged_undo_callback_for_owners_log_from_a_non_active_attacker_is_refused(
    tmp_path, monkeypatch, attacker_status
):
    """Attacker probe of the LIGHTER `on_callback` gate (`access.classify`,
    not `handle_gate`): an attacker chat that is unknown/pending/blocked
    forges `undo:<id>` for a log id that belongs to the OWNER, not them.
    Must be refused before `undo_ui.handle_undo_callback`'s own row-
    ownership check is ever reached -- the owner's row survives, and
    neither the attacker nor the owner receives anything."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    attacker = "6666"
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    if attacker_status is not None:
        seed_db.upsert_user(attacker, status=attacker_status)
    owner_row_id = seed_db.insert_log(
        LogEntry(None, OWNER, "2026-08-21T09:00:00", "water", 500.0, None, "500ml", "reply")
    )
    seed_db.close()

    script = [("callback", attacker, f"undo:{owner_row_id}", "500ml", "cb-attack")]
    channel = await _run(monkeypatch, config, script)

    db = Database(tmp_path / "habits.db")
    try:
        row = db.get_log(owner_row_id)
        assert row["deleted_at"] is None  # the owner's row survives the forged tap
    finally:
        db.close()
    assert channel.sent_to(attacker) == []
    assert channel.sent_to(OWNER) == []  # no side-channel notification to the owner either


# ---------------------------------------------------------------------------
# 2. Two-user isolation, filling the gaps the life-cycle test above doesn't
# cover: undo via BUTTON (not just text), /remind and /quiet between two
# ACTIVE MEMBERS (not owner-vs-member), queries, and daily-summary content.
# ---------------------------------------------------------------------------


async def test_two_active_members_undo_via_button_only_affects_the_tapping_members_own_row(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    a, b = "3001", "3002"
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.upsert_user(a, role="member", status="active")
    seed_db.upsert_user(b, role="member", status="active")
    seed_db.close()

    script = [("message", a, "500ml", None), ("message", b, "300ml", None)]
    await _run(monkeypatch, config, script, responses=[_extraction("water", 500), _extraction("water", 300)])

    db = Database(tmp_path / "habits.db")
    a_row_id = db.last_log(a)["id"]
    b_row_id = db.last_log(b)["id"]
    db.close()

    script2 = [("callback", a, f"undo:{a_row_id}", "500ml", "cb-a")]
    channel2 = await _run(monkeypatch, config, script2)

    db = Database(tmp_path / "habits.db")
    try:
        assert db.get_log(a_row_id)["deleted_at"] is not None  # a's own row undone
        assert db.get_log(b_row_id)["deleted_at"] is None  # b's row untouched
    finally:
        db.close()
    assert channel2.sent_to(b) == []  # b receives nothing from a's undo
    assert any("Undone" in t for t in channel2.sent_to(a))


async def test_remind_isolation_between_two_active_members(tmp_path, monkeypatch):
    """AC-S2-shaped, but between two ordinary MEMBERS (not owner-vs-member,
    which test_ac_s4/test_ac_s6 above already cover) -- through the real
    `/remind` write path."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    a, b = "3001", "3002"
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.upsert_user(a, role="member", status="active")
    seed_db.upsert_user(b, role="member", status="active")
    seed_db.close()

    script = [("message", a, "/remind water 07:00", None)]
    await _run(monkeypatch, config, script)

    db = Database(tmp_path / "habits.db")
    try:
        registry = HabitRegistry.from_config(config)
        fake_channel = _ScriptedChannel()
        state = ReminderState()
        await run_due_reminders(fake_channel, config, registry, db, state, clock=lambda: datetime(2026, 8, 21, 7, 0, 0))
        assert fake_channel.sent_to(a) != []
        assert fake_channel.sent_to(b) == []  # b has no override, and 07:00 isn't one of water's config defaults

        fake_channel.sent.clear()
        await run_due_reminders(fake_channel, config, registry, db, state, clock=lambda: datetime(2026, 8, 21, 8, 0, 0))
        assert fake_channel.sent_to(b) != []  # b still fires at water's config default (08:00)
        assert fake_channel.sent_to(a) == []  # a's override replaced 08:00 entirely, for a only
    finally:
        db.close()


async def test_quiet_isolation_between_two_active_members(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    a, b = "3001", "3002"
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.upsert_user(a, role="member", status="active")
    seed_db.upsert_user(b, role="member", status="active")
    seed_db.close()

    script = [("message", a, "/quiet 22:00-07:00", None)]
    await _run(monkeypatch, config, script)

    db = Database(tmp_path / "habits.db")
    try:
        assert effective_quiet_windows(db, config, a) == [("22:00", "07:00")]
        assert effective_quiet_windows(db, config, b) == []  # b untouched, inherits the (empty) global default
    finally:
        db.close()


async def test_query_answers_are_scoped_to_the_asking_users_own_data(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    a, b = "3001", "3002"
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.upsert_user(a, role="member", status="active")
    seed_db.upsert_user(b, role="member", status="active")
    seed_db.close()

    # SPEC-v1.5.md R-L1 (module `preparse`): "500ml"/"300ml" now resolve
    # deterministically without ever reaching `chat_json` -- only the two
    # query-classification calls below still consume the mocked response
    # queue.
    script = [
        ("message", a, "500ml", None),
        ("message", b, "300ml", None),
        ("message", a, "how much water today?", None),
        ("message", b, "how much water today?", None),
    ]
    responses = [
        _query_intent("water", "sum", "today"),
        _query_intent("water", "sum", "today"),
    ]
    channel = await _run(monkeypatch, config, script, responses=responses)

    a_answer = channel.sent_to(a)[-1]
    b_answer = channel.sent_to(b)[-1]
    assert "500" in a_answer and "300" not in a_answer
    assert "300" in b_answer and "500" not in b_answer


async def test_daily_summary_fan_out_shows_each_users_own_totals_and_skips_a_user_with_no_logs_today(
    tmp_path, monkeypatch
):
    """AC-U3, through the real scheduled-job closure (not a direct
    `streaks.run_daily_summary` call): each active user who logged
    something today gets their OWN summary, and a user with zero logs
    today is skipped entirely -- no empty recap."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    a, b, c = "3001", "3002", "3003"
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.upsert_user(a, role="member", status="active")
    seed_db.upsert_user(b, role="member", status="active")
    seed_db.upsert_user(c, role="member", status="active")  # logs nothing today -- must be skipped
    seed_db.close()

    # SPEC-v1.5.md R-L1 (module `preparse`): "500ml" now resolves
    # deterministically without ever reaching `chat_json` -- only "10 min
    # stretch" (not a whole-message "NUMBER UNIT" shape) still consumes
    # the mocked response queue.
    script = [("message", a, "500ml", None), ("message", b, "10 min stretch", None)]
    channel = await _run(
        monkeypatch,
        config,
        script,
        responses=[_extraction("stretch", 10)],
        run_jobs=["daily_summary"],
    )

    # NOTE: nobody here ever ran /lang, so each user's stored preference is
    # "auto" -- for an UNPROMPTED send (no inbound message to auto-detect
    # from), "auto" resolves to `config.i18n.primary_language`, which
    # defaults to Thai (core/i18n.py's own documented resolution). The
    # summary is therefore in Thai by default, not English.
    habit_stretch_label_th = HabitRegistry.from_config(config).get("stretch").label("th")

    a_summary = next((t for t in channel.sent_to(a) if i18n.t("daily_summary_header", "th") in t), None)
    b_summary = next((t for t in channel.sent_to(b) if i18n.t("daily_summary_header", "th") in t), None)
    assert a_summary is not None and "500" in a_summary  # a's own water total (numeric habits show a running total)
    # A duration habit's daily-summary line shows a SESSION COUNT, not the
    # logged minutes (streaks.compute_daily_summary's own per-type
    # formatting) -- b's own stretch label + a "1 session" count is the
    # correct signal that b's own entry (not a's) was counted.
    assert b_summary is not None and habit_stretch_label_th in b_summary
    # b never logged water -- a's 500ml total must not leak into b's own
    # summary (checked as "water: 0 /", not a bare "not in" substring
    # check, since the config default GOAL is 2500 -- which itself
    # contains "500" as a substring and would false-positive).
    habit_water_label_th = HabitRegistry.from_config(config).get("water").label("th")
    assert f"{habit_water_label_th}: 0 /" in b_summary
    assert not any(i18n.t("daily_summary_header", "th") in t for t in channel.sent_to(c))


# ---------------------------------------------------------------------------
# 3. AC-M3, extended past a single confirmation string: /habits and /undo
# are also byte-identical for the owner when they are the ONLY user in the
# system (the gate is a true structural no-op, not just "happens to
# produce the same string this one time").
# ---------------------------------------------------------------------------


async def test_ac_m3_owner_habits_and_undo_stay_byte_identical_with_zero_other_users(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [
        ("message", OWNER, "500ml", None),
        ("message", OWNER, "/habits", None),
        ("message", OWNER, "/undo", None),
    ]
    channel = await _run(monkeypatch, config, script, responses=[_extraction("water", 500)])
    owner_msgs = channel.sent_to(OWNER)

    assert owner_msgs[0] == "✅ 500 ml logged — today 500 / 2500 ml (20%)"
    habits_reply = next(t for t in owner_msgs if i18n.t("habits_overview_header", "en") in t)
    assert "today 500 ml" in habits_reply
    undo_reply = owner_msgs[-1]
    assert undo_reply.startswith("↩️ Undone") and "0 / 2500 ml (0%)" in undo_reply

    db = Database(tmp_path / "habits.db")
    try:
        # Exactly one `users` row ever existed -- the gate never created,
        # touched, or needed anyone else to let the owner through.
        assert db._conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 4. The safety-net deviation (IMPL-v1.2-integration.md "Deviations" #1):
# an access-owned command kind reaching `handle_inbound_message` DIRECTLY
# (bypassing `on_message`'s routing) must be a safe no-op -- specifically,
# it must NOT null out the acting user's most recent log row (the
# `_execute_edit(category=None, value_num=None)` data-corruption edge
# Luna found while wiring this).
# ---------------------------------------------------------------------------


async def test_admin_kind_reaching_handle_inbound_message_directly_does_not_corrupt_the_last_log(tmp_path):
    from habit_assistant.main import handle_inbound_message

    config = Config()
    db = Database(tmp_path / "safety_net.db")
    row_id = db.insert_log(LogEntry(None, OWNER, "2026-08-21T09:00:00", "water", 500.0, None, "500ml", "reply"))

    class _RaisingChannel(Channel):
        async def send(self, chat_id, text) -> None:
            raise AssertionError(f"channel.send must never be called for a safety-net no-op (got {text!r})")

        async def send_actionable(self, chat_id, text, buttons) -> None:
            raise AssertionError("channel.send_actionable must never be called for a safety-net no-op")

        async def run(self, on_message, on_callback=None) -> None:
            raise NotImplementedError

    llm = _FakeOllamaClient()
    try:
        for text in ("/approve 123", "/block 123", "/users", "/invite 123", "/start"):
            # Not through the channel at all (proves it structurally, not
            # just "nothing observed") for the normal path...
            await handle_inbound_message(
                text, db=db, llm=llm, channel=_RaisingChannel(), config=config, user_id=OWNER, dry_run=False
            )
            # ...and no channel reference needed at all for --dry-run,
            # matching `main.py`'s own `assert channel is not None` guard
            # for every OTHER kind but deliberately not this safety-net one.
            await handle_inbound_message(
                text, db=db, llm=llm, channel=None, config=config, user_id=OWNER, dry_run=True
            )

        row = db.get_log(row_id)
        assert row["value_num"] == 500.0  # NOT nulled -- the exact corruption this deviation prevents
        assert row["category"] == "water"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 5. user_pref threading: /lang th propagates into UNPROMPTED sends
# (reminders, daily summary, weekly review), not just the reply to /lang
# itself; the owner's default "auto" is unaffected -- auto-detection from
# the inbound message still works normally when no /lang was ever run.
# ---------------------------------------------------------------------------


async def test_lang_th_propagates_to_reminder_text(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("message", OWNER, "/lang th", None)]
    await _run(monkeypatch, config, script)

    db = Database(tmp_path / "habits.db")
    try:
        registry = HabitRegistry.from_config(config)
        fake_channel = _ScriptedChannel()
        state = ReminderState()
        await run_due_reminders(fake_channel, config, registry, db, state, clock=lambda: datetime(2026, 8, 21, 8, 0, 0))
        owner_msgs = fake_channel.sent_to(OWNER)
        assert owner_msgs and owner_msgs[0] == i18n.t("reminder_water", "th")
    finally:
        db.close()


async def test_lang_th_propagates_to_daily_summary_and_weekly_review(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("message", OWNER, "/lang th", None), ("message", OWNER, "500ml", None)]
    channel = await _run(
        monkeypatch,
        config,
        script,
        responses=[_extraction("water", 500)],
        run_jobs=["daily_summary", "weekly_review"],
    )

    owner_msgs = channel.sent_to(OWNER)
    assert any(i18n.t("daily_summary_header", "th") in t for t in owner_msgs)
    assert any(i18n.t("weekly_review_header", "th") in t for t in owner_msgs)
    # And NOT the English headers -- this is "in Thai", not "in both".
    assert not any(i18n.t("daily_summary_header", "en") in t for t in owner_msgs)
    assert not any(i18n.t("weekly_review_header", "en") in t for t in owner_msgs)


async def test_owner_autodetect_unaffected_when_no_lang_pref_ever_set(tmp_path, monkeypatch):
    """AC-P1's own "owner unaffected" clause, re-confirmed for the case the
    life-cycle test above doesn't isolate cleanly: with NO /lang ever run
    by anyone, the owner's replies still auto-detect from each message's
    own language -- Thai in, Thai out; English in, English out -- proving
    the new `_stored_language_pref` threading is a true no-op for an
    unset preference, not an accidental hardcoded default."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [
        ("message", OWNER, "ดื่มน้ำ 500 มล", None),
        ("message", OWNER, "500ml", None),
    ]
    channel = await _run(monkeypatch, config, script, responses=[_extraction("water", 500), _extraction("water", 500)])

    owner_msgs = channel.sent_to(OWNER)
    assert "บันทึก" in owner_msgs[0]
    assert "logged" in owner_msgs[1]


# ---------------------------------------------------------------------------
# 6. display_name: /users deliberately does NOT show it (a finding worth
# recording -- only `access_request` does; SPEC-v1.2.md §3.3's own example
# is chat-id-only, so this matches spec, not a gap); a genuinely 2-arg
# `on_message` call (pre-integration-shaped caller) still works and falls
# back to the bare chat id.
# ---------------------------------------------------------------------------


async def test_users_listing_never_includes_display_name(tmp_path, monkeypatch):
    """Documents actual behavior against the coordinator's point 6: only
    the owner's `access_request` NOTIFICATION carries `display_name`
    (R-A2) -- `/users` (`core/access.py:_render_users_list`) has never
    rendered it, in this pass or any prior one, and SPEC-v1.2.md §3.3's own
    illustrative example is chat-id-only too. Not a gap; recorded here so
    the assumption doesn't silently drift into the release notes."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [
        ("message", "7777", "hi", "Charlie"),
        ("message", OWNER, "/approve 7777", None),
        ("message", OWNER, "/users", None),
    ]
    channel = await _run(monkeypatch, config, script)

    users_reply = next(t for t in channel.sent_to(OWNER) if i18n.t("users_list_header", "en") in t)
    assert "Charlie" not in users_reply
    assert "7777" in users_reply
    # The access_request notification (a DIFFERENT message) did carry it.
    assert any("Charlie" in t for t in channel.sent_to(OWNER))


async def test_two_arg_on_message_call_still_works_and_falls_back_to_chat_id_when_no_display_name(
    tmp_path, monkeypatch
):
    """Back-compat: `on_message`'s `display_name` param defaults to `None`
    (SPEC-v1.2.md §11/IMPL-v1.2-integration.md), so a caller that still
    only passes 2 positional args (any pre-integration test/fake) must not
    crash, and `access_request` must fall back to the bare chat id."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})

    class _TwoArgChannel(_ScriptedChannel):
        async def run(self, on_message, on_callback=None) -> None:
            await on_message(STRANGER, "hi")  # exactly 2 positional args
            raise _StopAfterSchedulerStart()

    channel = await _run(monkeypatch, config, script=[], channel_cls=_TwoArgChannel)

    assert channel.sent_to(STRANGER) == [i18n.t("access_pending", "en")]
    owner_msg = channel.sent_to(OWNER)[0]
    assert STRANGER in owner_msg


# ---------------------------------------------------------------------------
# 8. Migration + attribution rehearsal: a v1.1-shaped scratch DB (raw
# sqlite3, schema through migration 005, user_version=5) with real
# pre-existing owner data, opened through the REAL `async_main` startup
# path (migration 006 + `attribute_legacy_to_owner`, exactly as production
# would run it on upgrade day) -- then the app is actually used on it.
# ---------------------------------------------------------------------------


async def test_migration_and_attribution_rehearsal_on_a_v1_1_shaped_scratch_db(tmp_path, monkeypatch):
    db_path = tmp_path / "upgrade_rehearsal.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE logs (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          ts          TEXT NOT NULL,
          category    TEXT NOT NULL,
          value_num   REAL,
          value_text  TEXT,
          raw_message TEXT NOT NULL,
          source      TEXT NOT NULL DEFAULT 'reply',
          created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          deleted_at  TEXT NULL,
          habit_type  TEXT NULL
        );
        CREATE INDEX idx_logs_ts_cat ON logs(ts, category);
        CREATE INDEX idx_logs_category ON logs(category);
        CREATE INDEX idx_logs_deleted_at ON logs(deleted_at);
        CREATE TABLE habit_targets (
          habit_id   TEXT PRIMARY KEY,
          goal       REAL NOT NULL,
          updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        PRAGMA user_version = 5;
        """
    )
    today_ts = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO logs (ts, category, value_num, value_text, raw_message, source, habit_type) "
        "VALUES (?, 'water', 500.0, NULL, '500ml', 'reply', 'numeric')",
        (today_ts,),
    )
    conn.execute(
        "INSERT INTO logs (ts, category, value_num, value_text, raw_message, source, habit_type) "
        "VALUES (?, 'water', 300.0, NULL, '300ml', 'reply', 'numeric')",
        (today_ts,),
    )
    conn.execute("INSERT INTO habit_targets (habit_id, goal) VALUES ('water', 3000.0)")
    conn.commit()
    conn.close()

    config = Config.model_validate({"app": {"db_path": str(db_path)}})
    script = [
        ("message", OWNER, "/habits", None),
        # A brand-new, never-before-seen chat: gated off as unknown, and
        # must see none of the owner's (migrated) legacy data.
        ("message", "9999", "500ml", "NewMember"),
    ]
    channel = await _run(monkeypatch, config, script, owner_chat_id=OWNER)

    habits_reply = next(t for t in channel.sent_to(OWNER) if i18n.t("habits_overview_header", "en") in t)
    assert "today 800 ml" in habits_reply  # 500 + 300, both legacy rows attributed to the owner

    assert channel.sent_to("9999") == [i18n.t("access_pending", "en")]

    db = Database(db_path)
    try:
        assert db.schema_version == 8  # SPEC-v1.3.md's migration 007 (audit_log) + SPEC-v1.5.md's migration 008 also land now
        assert db._conn.execute("SELECT COUNT(*) AS n FROM logs WHERE user_id IS NULL").fetchone()["n"] == 0
        assert db._conn.execute("SELECT COUNT(*) AS n FROM habit_targets WHERE user_id IS NULL").fetchone()["n"] == 0
        owner_row = db.get_user(OWNER)
        assert owner_row["role"] == "owner" and owner_row["status"] == "active"
        assert db.get_target(OWNER, "water") == 3000.0  # the legacy target override carried over too
        # The new chat never touched the owner's migrated data.
        assert db.last_log("9999") is None
    finally:
        db.close()
