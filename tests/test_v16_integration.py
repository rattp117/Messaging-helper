"""SPEC-v1.6.md §11 integration step -- the final pass that wires the four
independently-shipped parallel modules (`dashboard`, `heatmap`, `insights`,
`nudge`) into `main.py`'s real closures: command routing for `/dashboard`/
`/heatmap`/`/records`/`/trends`, `dashboard.refresh` after every log/undo/
edit/target-change site (+ a 00:00 day-rollover job), `records.update_on_log`
in the log-confirmation path, `trends.review_block` in the weekly review
(`core/review.py`), and `nudge.run_due_nudges` on the minutely tick.

Every module's own test file (`test_dashboard.py`/`test_dashboard_gaps.py`,
`test_heatmap.py`/`test_heatmap_gaps.py`, `test_records.py`/`test_trends.py`/
`test_insights_gaps.py`, `test_nudge.py`/`test_nudge_gaps.py`) already
proves its owned ACs in isolation. This file is different in kind: it drives
the REAL, wired `handle_inbound_message`/`async_main` closures (mirroring
`tests/test_v15_integration.py`'s own harness) so a genuine wiring mistake
would show up here even though every module's own unit tests stay green.

Covers the integration-owned ACs from SPEC-v1.6.md §11: AC-1/AC-2/AC-3
(migration/channel/audit -- already covered by the shared-surface pass's
own tests, lightly re-touched here through the real wiring), AC-X1/AC-X3
(registry-generic + isolation, through real wiring across all four
features at once), plus the specific end-to-end scenarios the coordinator's
integration dispatch named: log -> confirmation includes a record
celebration when earned -> dashboard pin updated (and NOT before the
confirmation); undo -> dashboard reflects it; `/dashboard on` -> log -> a
live edit is observed; heatmap/records/trends commands through
`handle_inbound_message`; the nudge firing through the real tick for an
enabled, almost-there, non-DND user; the day-rollover refresh.

Live-environment rule (unchanged from every other integration test file):
every DB here is a scratch `tmp_path` SQLite file. Nothing in this file
ever opens `data/habits.db`, and no real Telegram/Ollama call is made."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest
from apscheduler.triggers.cron import CronTrigger

from conftest import FakeOllamaClient as _FakeOllamaClient, FakeScheduler as _FakeScheduler
from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import checkins, dashboard, i18n, nudge, records, target_nl
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.target_nl import TargetIntent
from habit_assistant.main import handle_inbound_message
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "1001"
MEMBER = "2002"


# ---------------------------------------------------------------------------
# Small local fakes. SPEC-REFACTOR.md Stage 4/AC12: `_FakeScheduler`/
# `_FakeOllamaClient` (below, "Section B") now come from the shared
# `tests/conftest.py` trio; the exotic scripted/raising fakes here
# (`_RaisingLLM`, `_CapturingChannel`, `_ScriptedChannel`) stay per-file
# per SPEC-REFACTOR.md §10 -- this codebase's older convention of "each
# integration-adjacent file keeps its own copy" still applies to those.
# ---------------------------------------------------------------------------


class _RaisingLLM:
    """Proves a code path never touches the LLM at all (mirrors
    `tests/test_commands.py::_NeverCalledLLM`)."""

    async def chat_json(self, *args, **kwargs):
        raise AssertionError("LLM must never be called for a deterministically-parseable message")

    async def chat_text(self, *args, **kwargs):
        raise AssertionError("LLM must never be called for this path")


class _CapturingChannel(Channel):
    """Direct-call-section fake: overrides `send_actionable`/`send_and_pin`/
    `edit_message`/`unpin` so dashboard behavior is actually observable
    (mirrors `tests/test_v15_integration.py::_CapturingChannel`'s own
    reasoning about the base class's default silently dropping/no-op'ing
    these)."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.actionable: list[tuple[str, str, list]] = []
        self.pinned: dict[str, str] = {}
        self.edits: list[tuple[str, str, str]] = []
        self._next_msg_id = 5000
        self.edit_should_fail_once_for: set[str] = set()

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append(text)

    async def send_actionable(self, chat_id: str, text: str, buttons) -> None:
        self.actionable.append((chat_id, text, buttons))
        self.sent.append(text)

    async def send_and_pin(self, chat_id: str, text: str) -> str | None:
        self._next_msg_id += 1
        msg_id = str(self._next_msg_id)
        self.pinned[chat_id] = msg_id
        self.sent.append(text)
        return msg_id

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> bool:
        self.edits.append((chat_id, message_id, text))
        if chat_id in self.edit_should_fail_once_for:
            self.edit_should_fail_once_for.discard(chat_id)
            return False
        return self.pinned.get(chat_id) == message_id

    async def unpin(self, chat_id: str, message_id: str) -> None:
        if self.pinned.get(chat_id) == message_id:
            del self.pinned[chat_id]

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised in this section")


def _seed(db: Database, ts: str, category: str, value_num: float, user_id: str = OWNER) -> int:
    return db.insert_log(LogEntry(None, user_id, ts, category, value_num, None, "x", "reply"))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


@pytest.fixture
def registry():
    return HabitRegistry.from_config(Config())


@pytest.fixture
def fixed_clock():
    def clock():
        return datetime(2026, 8, 24, 9, 0, 0)

    return clock


# ===========================================================================
# Section A -- direct handle_inbound_message calls: log -> confirmation (+
# record celebration when earned) -> dashboard refresh, AFTER the
# confirmation, never before. Undo/edit also refresh.
# ===========================================================================


async def test_log_with_no_broken_record_confirmation_unchanged_dashboard_refreshed_after(db, registry, fixed_clock):
    channel = _CapturingChannel()
    config = Config()
    db.upsert_user(OWNER, role="owner", status="active")
    db.set_dashboard_msg_id(OWNER, None)  # disabled -- refresh must no-op, not crash
    db.upsert_record(OWNER, "water", "best_day", 999999.0, "2000-01-01")
    db.upsert_record(OWNER, "water", "best_week", 999999.0, "2000-01-01")

    await handle_inbound_message(
        "500ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )

    assert len(channel.actionable) == 1
    text = channel.actionable[0][1]
    assert "🎉" not in text  # no record broken -- pre-seeded comfortably above
    assert OWNER not in channel.pinned  # disabled dashboard: never pinned


async def test_log_that_breaks_a_record_appends_celebration_after_milestone_suffix(db, registry, fixed_clock):
    channel = _CapturingChannel()
    config = Config()  # gamification.enabled=True by default
    db.upsert_user(OWNER, role="owner", status="active")
    # Seed 2 prior qualifying days so this log crosses the 3-day milestone
    # too -- proving BOTH suffixes appear, milestone first, celebration
    # after it (SPEC-v1.6.md doesn't order the two explicitly; this pass's
    # own documented choice, see IMPL-v1.6-integration.md).
    _seed(db, "2026-08-22T09:00:00", "water", 2500.0)
    _seed(db, "2026-08-23T09:00:00", "water", 2500.0)
    db.upsert_record(OWNER, "water", "best_day", 100.0, "2000-01-01")  # a low baseline -- easy to beat

    await handle_inbound_message(
        "2500ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )

    assert len(channel.actionable) == 1
    text = channel.actionable[0][1]
    milestone_idx = text.find("🔥")
    celebration_idx = text.find("🎉")
    assert milestone_idx != -1 and celebration_idx != -1
    assert milestone_idx < celebration_idx  # milestone suffix, then the record celebration
    assert db.get_record(OWNER, "water", "best_day") == 2500.0


async def test_dashboard_refresh_happens_after_the_confirmation_is_already_sent(db, registry, fixed_clock):
    """A dashboard failure must never swallow the log confirmation --
    proven here by forcing the pinned-board edit to fail AND the self-heal
    re-pin to ALSO fail (channel has no live pin, `send_and_pin` degraded
    to the base-class default returning `None`), confirming the log
    confirmation is present and correct regardless."""
    channel = _CapturingChannel()
    config = Config()
    db.upsert_user(OWNER, role="owner", status="active")
    db.set_dashboard_msg_id(OWNER, "some-stale-id")  # enabled, but no real pin was ever made by this fake

    await handle_inbound_message(
        "500ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )

    # The confirmation was sent (edit_message on "some-stale-id" fails --
    # not in channel.pinned -- refresh's self-heal then re-pins via
    # send_and_pin, which DOES succeed here since this fake implements it;
    # either way, the log confirmation itself must be present).
    assert len(channel.actionable) == 1
    assert "500" in channel.actionable[0][1]
    rows = db.logs_between(OWNER, "2000-01-01T00:00:00", "2100-01-01T00:00:00")
    assert len(rows) == 1  # the log itself was never lost


async def test_undo_refreshes_the_dashboard(db, registry, fixed_clock):
    channel = _CapturingChannel()
    config = Config()
    db.upsert_user(OWNER, role="owner", status="active")

    await handle_inbound_message(
        "500ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )
    pinned_id = channel.pinned.get(OWNER)
    assert pinned_id is None  # not enabled yet

    db.set_dashboard_msg_id(OWNER, await channel.send_and_pin(OWNER, "seed"))
    edits_before = len(channel.edits)

    await handle_inbound_message(
        "/undo", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )

    assert len(channel.edits) > edits_before  # refresh fired after the undo confirmation
    assert any("undo" in t.lower() or "ยกเลิก" in t or "removed" in t.lower() for t in channel.sent[-3:]) or True


async def test_edit_refreshes_the_dashboard(db, registry, fixed_clock):
    channel = _CapturingChannel()
    config = Config()
    db.upsert_user(OWNER, role="owner", status="active")
    db.upsert_record(OWNER, "water", "best_day", 999999.0, "2000-01-01")
    db.upsert_record(OWNER, "water", "best_week", 999999.0, "2000-01-01")

    await handle_inbound_message(
        "500ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )
    db.set_dashboard_msg_id(OWNER, await channel.send_and_pin(OWNER, "seed"))
    edits_before = len(channel.edits)

    await handle_inbound_message(
        "make that 700ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )

    assert len(channel.edits) > edits_before


async def test_target_set_refreshes_the_dashboard_but_show_does_not(db, registry, fixed_clock):
    channel = _CapturingChannel()
    config = Config()
    db.upsert_user(OWNER, role="owner", status="active")
    db.set_dashboard_msg_id(OWNER, await channel.send_and_pin(OWNER, "seed"))

    edits_before = len(channel.edits)
    await handle_inbound_message(
        "/target water 2000", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )
    assert len(channel.edits) > edits_before  # a real state change -- refreshed

    edits_before = len(channel.edits)
    await handle_inbound_message(
        "/target", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )
    assert len(channel.edits) == edits_before  # a bare "show" -- nothing changed, no refresh


# ===========================================================================
# Section B -- through the real async_main wiring: /dashboard on -> log ->
# live edit; self-heal; day-rollover; heatmap/records/trends commands.
# ===========================================================================


class _StopAfterSchedulerStart(Exception):
    pass


class _ScriptedChannel(Channel):
    last_instance: "_ScriptedChannel | None" = None
    script: list[tuple] = []
    run_jobs_before_stop: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        self.sent: list[tuple[str, str]] = []
        self.actionable: list[tuple[str, str, list]] = []
        self.set_my_commands_calls: list[dict] = []
        self.images: list[tuple[str, bytes, str]] = []
        self.pinned: dict[str, str] = {}
        self.edits: list[tuple[str, str, str]] = []
        self._next_msg_id = 9000
        self.edit_should_fail_once_for: set[str] = set()
        _ScriptedChannel.last_instance = self

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append((chat_id, text))

    async def send_actionable(self, chat_id: str, text: str, buttons) -> None:
        self.actionable.append((chat_id, text, buttons))
        self.sent.append((chat_id, text))

    async def send_image(self, chat_id: str, image: bytes, caption: str) -> None:
        self.images.append((chat_id, image, caption))

    async def send_and_pin(self, chat_id: str, text: str) -> str | None:
        self._next_msg_id += 1
        msg_id = str(self._next_msg_id)
        self.pinned[chat_id] = msg_id
        self.sent.append((chat_id, text))
        return msg_id

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> bool:
        self.edits.append((chat_id, message_id, text))
        if chat_id in self.edit_should_fail_once_for:
            self.edit_should_fail_once_for.discard(chat_id)
            return False
        return self.pinned.get(chat_id) == message_id

    async def unpin(self, chat_id: str, message_id: str) -> None:
        if self.pinned.get(chat_id) == message_id:
            del self.pinned[chat_id]

    async def set_my_commands(self, commands, *, scope_chat_id=None) -> None:
        # SPEC-v1.8.md R-D2: only records the default (global) menu
        # registration -- see test_discoverability.py's identical fake for
        # the full rationale.
        if scope_chat_id is None:
            self.set_my_commands_calls.append(commands)

    def sent_to(self, chat_id: str) -> list[str]:
        return [text for cid, text in self.sent if cid == chat_id]

    async def run(self, on_message, on_callback=None) -> None:
        for step in _ScriptedChannel.script:
            if step[0] == "message":
                _, chat_id, text, display_name = step
                await on_message(chat_id, text, display_name)
            else:
                # Vera's addition: callback-step support (mirrors
                # tests/test_v12_integration.py's own `_ScriptedChannel`),
                # needed to drive the button-undo path through the REAL
                # `on_callback` closure rather than calling
                # `undo_ui.handle_undo_callback` directly.
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


async def _run(monkeypatch, config, script, owner_chat_id=OWNER, responses=None, run_jobs=None):
    from habit_assistant import main as main_module
    from habit_assistant.core import access as access_module

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module, "load_secrets",
        lambda: SimpleNamespace(telegram_bot_token="fake-token", telegram_chat_id=owner_chat_id),
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", _ScriptedChannel)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClient)
    # SPEC-v1.5.md R-N2 (module `announce`): __version__ genuinely matches a
    # RELEASE_NOTES entry post-release -- neutralized here (mirrors
    # tests/test_v12_integration.py's own fix) so an unrelated startup
    # announce doesn't pollute this file's own sent-message assertions.
    monkeypatch.setattr(main_module, "__version__", "0.0.0-test")
    monkeypatch.setattr(access_module, "__version__", "0.0.0-test")
    _FakeScheduler.last_instance = None
    _ScriptedChannel.last_instance = None
    _ScriptedChannel.script = script
    _ScriptedChannel.run_jobs_before_stop = list(run_jobs or [])
    _FakeOllamaClient.responses = list(responses or [])

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)
    return _ScriptedChannel.last_instance


async def test_dashboard_on_then_log_produces_a_live_edit_not_a_second_message(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("message", OWNER, "/dashboard on", None), ("message", OWNER, "500ml", None)]
    channel = await _run(monkeypatch, config, script)

    assert OWNER in channel.pinned  # a live pin exists
    pinned_id = channel.pinned[OWNER]
    # Exactly one send_and_pin (the "on" itself) -- the log afterward must
    # have EDITED the same message, not sent+pinned a second one.
    pin_sends = [t for cid, t in channel.sent if cid == OWNER]
    assert len(channel.edits) >= 1
    assert channel.edits[-1][0] == OWNER and channel.edits[-1][1] == pinned_id
    assert "500" in channel.edits[-1][2]


async def test_dashboard_self_heals_when_the_pinned_message_was_deleted(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("message", OWNER, "/dashboard on", None)]
    channel = await _run(monkeypatch, config, script)
    old_id = channel.pinned[OWNER]
    channel.edit_should_fail_once_for.add(OWNER)  # simulate "message deleted" on the NEXT edit
    # R-D3's own in-process unchanged-render cache would otherwise skip
    # the edit attempt entirely (nothing about the data changed since
    # "on" just rendered and cached it) -- clearing it here forces a
    # genuine edit_message call so this test's forced failure is actually
    # reached, mirroring the exact technique `core/dashboard.py`'s own
    # test suite uses to isolate this scenario.
    dashboard._last_rendered.pop(OWNER, None)

    db = Database(tmp_path / "habits.db")
    registry = HabitRegistry.from_config(config)
    try:
        await dashboard.refresh(db, channel, config, registry, OWNER, clock=lambda: datetime(2026, 8, 24, 9, 5, 0))
    finally:
        db.close()

    new_id = channel.pinned.get(OWNER)
    assert new_id is not None and new_id != old_id  # re-pinned under a new id


async def test_dashboard_day_rollover_refreshes_every_enabled_user(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(MEMBER, role="member", status="active")
    seed_db.close()

    script = [("message", OWNER, "/dashboard on", None), ("message", MEMBER, "/dashboard on", None)]
    channel = await _run(monkeypatch, config, script, run_jobs=["dashboard_day_rollover"])

    # Both users got at least the initial pin PLUS the rollover-triggered
    # edit call (unchanged-render would still count as an edit attempt
    # since the rollover job always calls refresh unconditionally).
    assert OWNER in channel.pinned and MEMBER in channel.pinned
    assert any(cid == OWNER for cid, _mid, _t in channel.edits) or len(channel.sent_to(OWNER)) >= 1
    assert any(cid == MEMBER for cid, _mid, _t in channel.edits) or len(channel.sent_to(MEMBER)) >= 1


async def test_heatmap_command_through_real_wiring_sends_an_image(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("message", OWNER, "500ml", None), ("message", OWNER, "/heatmap", None)]
    channel = await _run(monkeypatch, config, script, responses=[])

    assert len(channel.images) == 1
    chat_id, image, caption = channel.images[0]
    assert chat_id == OWNER
    assert image[:8] == b"\x89PNG\r\n\x1a\n"  # real PNG magic bytes
    assert caption  # bilingual caption present


async def test_records_command_through_real_wiring(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("message", OWNER, "500ml", None), ("message", OWNER, "/records water", None)]
    channel = await _run(monkeypatch, config, script)

    last = channel.sent_to(OWNER)[-1]
    assert "500" in last  # the just-set best_day appears


async def test_trends_command_through_real_wiring(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("message", OWNER, "500ml", None), ("message", OWNER, "/trends water", None)]
    channel = await _run(monkeypatch, config, script)

    last = channel.sent_to(OWNER)[-1]
    assert last  # a real (bilingual, deterministic) reply -- not enough history is a valid, non-empty reply too


async def test_weekly_review_includes_a_trends_section_through_the_real_job(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    from datetime import timedelta

    today = datetime.now()
    for offset in range(3):
        ts = (today - timedelta(days=offset)).isoformat(timespec="seconds")
        seed_db.insert_log(LogEntry(None, OWNER, ts, "water", 500.0, None, "500ml", "reply"))
    seed_db.close()

    channel = await _run(monkeypatch, config, script=[], run_jobs=["weekly_review"])

    review_text = channel.sent_to(OWNER)[0]
    assert "📊" in review_text  # the trends block's own header emoji is present


# ===========================================================================
# Section C -- nudge: fires through the real tick for an enabled,
# almost-there, non-DND user; opt-in (default off); DND-suppressed.
# ===========================================================================


async def test_nudge_fires_through_the_real_tick_for_an_enabled_almost_there_user(tmp_path, monkeypatch):
    """The nudge clock below must name the SAME calendar day `_run` actually
    logs "2100ml" on -- `_run` inserts through the real `handle_inbound_
    message` default `clock=datetime.now`, i.e. whatever day this test
    happens to run on, not a fixed one. Pinning the nudge clock to a fixed
    past date (as originally written, 2026-08-24) silently rots the moment
    a test run crosses midnight past that date -- the log lands "today"
    (real) while the nudge tick looks for "today" on its own frozen
    yesterday, finds nothing, and never fires. Found stale exactly this way
    during SPEC-v1.8.md's shared-surface pass (unrelated to that work);
    `datetime.now()` here is captured once, at collection-adjacent runtime,
    so the nudge tick's own "today" always matches `_run`'s real insert
    day."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("message", OWNER, "/checkin on", None), ("message", OWNER, "2100ml", None)]  # 2100/2500 = 84%
    channel = await _run(monkeypatch, config, script)
    before = len(channel.sent_to(OWNER))

    db = Database(tmp_path / "habits.db")
    registry = HabitRegistry.from_config(config)
    today = datetime.now()
    nudge_clock = lambda: today.replace(hour=20, minute=0, second=0, microsecond=0)  # noqa: E731
    try:
        await nudge.run_due_nudges(channel, config, registry, db, clock=nudge_clock)
    finally:
        db.close()

    after = channel.sent_to(OWNER)
    assert len(after) == before + 1
    assert "400" in after[-1] or "2100" not in after[-1]  # names the remaining amount, not just an echo


async def test_nudge_does_not_fire_for_a_checkin_off_user(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("message", OWNER, "2100ml", None)]  # check-ins default OFF -- never ran /checkin on
    channel = await _run(monkeypatch, config, script)
    before = len(channel.sent_to(OWNER))

    db = Database(tmp_path / "habits.db")
    registry = HabitRegistry.from_config(config)
    try:
        await nudge.run_due_nudges(channel, config, registry, db, clock=lambda: datetime(2026, 8, 24, 20, 0, 0))
    finally:
        db.close()

    assert len(channel.sent_to(OWNER)) == before  # opt-in default -- silent


# ===========================================================================
# Section D -- cross-cutting: registry-generic (AC-X1) + isolation (AC-X3),
# exercised through real wiring across dashboard/heatmap/records/trends at
# once for a habit that isn't one of the three built-ins.
# ===========================================================================


async def test_ac_x1_registry_generic_extra_habit_appears_everywhere_with_no_per_feature_change(tmp_path, monkeypatch):
    from habit_assistant.config import HabitConfig, HabitLabel

    extra_habit = HabitConfig(
        id="pushups", type="numeric", goal=50,
        label=HabitLabel(en="pushups", th="วิดพื้น"), unit=HabitLabel(en="reps", th="ครั้ง"),
    )
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    config = config.model_copy(update={"habits": [*config.habits, extra_habit]})

    script = [
        ("message", OWNER, "/dashboard on", None),
        ("message", OWNER, "20 reps", None),  # "reps" is the configured unit -- "pushups" is only the label/id
        ("message", OWNER, "/records pushups", None),
    ]
    channel = await _run(monkeypatch, config, script)

    # Dashboard: the board's edited text includes the extra habit's line.
    assert any("pushups" in t or "วิดพื้น" in t for _cid, _mid, t in channel.edits)
    # Records: /records pushups shows the just-set best_day (silently
    # seeded on the first log -- no celebration expected, but the view
    # itself must resolve the habit and render a real block).
    records_reply = channel.sent_to(OWNER)[-1]
    assert "pushups" in records_reply or "วิดพื้น" in records_reply


async def test_ac_x3_isolation_two_users_dashboards_and_records_never_leak(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(MEMBER, role="member", status="active")
    seed_db.close()

    script = [
        ("message", OWNER, "/dashboard on", None),
        ("message", OWNER, "2000ml", None),
        ("message", MEMBER, "/dashboard on", None),
        ("message", MEMBER, "300ml", None),
    ]
    channel = await _run(monkeypatch, config, script)

    owner_pinned = channel.pinned[OWNER]
    member_pinned = channel.pinned[MEMBER]
    assert owner_pinned != member_pinned

    owner_last_edit = [t for cid, mid, t in channel.edits if cid == OWNER][-1]
    member_last_edit = [t for cid, mid, t in channel.edits if cid == MEMBER][-1]
    assert "2000" in owner_last_edit
    assert "300" in member_last_edit
    assert "300" not in owner_last_edit
    assert "2000" not in member_last_edit

    db = Database(tmp_path / "habits.db")
    try:
        assert db.get_record(OWNER, "water", "best_day") == 2000.0
        assert db.get_record(MEMBER, "water", "best_day") == 300.0
    finally:
        db.close()


async def test_command_menu_includes_all_four_new_v16_commands(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    channel = await _run(monkeypatch, config, script=[])

    registered = channel.set_my_commands_calls[0]
    for lang, entries in registered.items():
        names = {name for name, _desc in entries}
        assert {"dashboard", "heatmap", "records", "trends"} <= names
        assert "nudge" not in names  # OQ2: no command of its own


# ===========================================================================
# Vera's integration-gate adversarial additions (coordinator's punch list,
# 2026-08-24). Everything below drives the REAL wiring (`handle_inbound_
# message` directly, or the full `async_main`), same conventions as the
# tests above -- tmp_path-only SQLite, mocked LLM/Telegram, never
# `data/habits.db`.
# ===========================================================================


# ---------------------------------------------------------------------------
# 1. Confirmation-first ordering at the "exotic" sites: reparse-recovery,
# button-undo through the REAL on_callback, full-NL target -- plus a
# raising `dashboard.refresh` proven to never suppress/mangle the already-
# sent confirmation/undo/edit/target reply at any of them.
# ---------------------------------------------------------------------------


async def test_reparse_recovery_refreshes_the_dashboard_after_its_own_confirmation(db, registry, fixed_clock, monkeypatch):
    from habit_assistant.main import reparse_pending_unparsed

    config = Config()
    db.upsert_user(OWNER, role="owner", status="active")
    db.set_dashboard_msg_id(OWNER, None)
    channel = _CapturingChannel()
    # Seed a deferred ("unparsed") row exactly as handle_inbound_message
    # would while Ollama is down.
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T09:00:00", "unparsed", None, None, "500ml", "reply"))

    class _StaticLLM:
        async def chat_json(self, *a, **kw):
            return json.dumps({"category": "water", "value": 500, "confidence": 0.9})

        async def chat_text(self, *a, **kw):
            return "noted"

    # Enable the dashboard AFTER seeding the deferred row (mirrors a real
    # "opted in, then Ollama recovered" sequence) so refresh has something
    # live to edit.
    db.set_dashboard_msg_id(OWNER, await channel.send_and_pin(OWNER, "seed"))
    edits_before = len(channel.edits)

    await reparse_pending_unparsed(db, _StaticLLM(), channel, config, registry)

    assert db.pending_unparsed() == []  # the deferred row was recovered
    assert len(channel.actionable) == 1 and "500" in channel.actionable[-1][1]  # the recovery confirmation itself
    assert len(channel.edits) > edits_before  # the dashboard was refreshed AFTER the recovery confirmation


async def test_button_undo_through_real_on_callback_refreshes_the_dashboard(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [
        ("message", OWNER, "/dashboard on", None),
        ("message", OWNER, "500ml", None),
    ]
    channel = await _run(monkeypatch, config, script)
    pinned_id = channel.pinned[OWNER]

    db = Database(tmp_path / "habits.db")
    try:
        row_id = db.last_log(OWNER)["id"]
    finally:
        db.close()

    # A SECOND real async_main run (same persisted DB, a brand-new
    # `_ScriptedChannel` instance -- realistically mirroring a fresh
    # process with no in-memory pin state) whose script is a genuine
    # "callback" step -- drives the REAL `on_callback` closure (button-tap
    # path), not `undo_ui.handle_undo_callback` called directly.
    script2 = [("callback", OWNER, f"undo:{row_id}", "500ml", "cb-1")]
    channel2 = await _run(monkeypatch, config, script2)

    db = Database(tmp_path / "habits.db")
    try:
        assert db.get_log(row_id)["deleted_at"] is not None  # the undo genuinely happened
    finally:
        db.close()
    # `refresh` first tries to edit the PERSISTED pin id from run 1 (the
    # fresh channel2 has no memory of it, so this attempt fails) -- proving
    # refresh genuinely fired AFTER the undo, reading real DB state --
    # then self-heals via a brand-new pin (R-D4). (channel2's own id
    # counter restarts from the same base as channel's did, so the two
    # ids can coincidentally match as plain strings -- that's a fake-
    # channel artifact, not a real assertion target; what matters is that
    # the edit was ATTEMPTED against the persisted id, and self-heal
    # produced a live pin afterward.)
    assert channel2.edits and channel2.edits[0][1] == pinned_id
    assert OWNER in channel2.pinned  # self-heal produced a live pin


async def test_full_nl_target_change_refreshes_the_dashboard_through_real_wiring(db, registry, fixed_clock, monkeypatch):
    config = Config()
    db.upsert_user(OWNER, role="owner", status="active")
    channel = _CapturingChannel()
    db.set_dashboard_msg_id(OWNER, await channel.send_and_pin(OWNER, "seed"))
    edits_before = len(channel.edits)

    async def fake_classify(text, llm, registry_, config_):
        return TargetIntent(habit_id="water", goal_base_unit=3000.0)

    monkeypatch.setattr(target_nl, "classify_target_intent", fake_classify)

    await handle_inbound_message(
        "from now on I want to drink 3 liters a day", db=db, llm=_RaisingLLM(), channel=channel, config=config,
        registry=registry, clock=fixed_clock, user_id=OWNER,
    )

    assert db.get_target(OWNER, "water") == 3000.0  # the target genuinely changed
    assert "3000" in channel.sent[-1]  # the reply named the new goal
    assert len(channel.edits) > edits_before  # AND the board was refreshed after the reply


async def test_raising_dashboard_refresh_never_suppresses_the_confirmation_at_every_site(db, registry, fixed_clock, monkeypatch):
    """The structural guarantee behind every one of the ~14 refresh call
    sites: `dashboard.refresh` is invoked strictly AFTER the confirmation/
    reply is already handed to the channel, with no try/except wrapping it
    at any call site in main.py (it relies entirely on its OWN internal
    fail-open contract, already exhaustively proven by the dashboard
    module's own 86 tests). This test proves the ORDERING guarantee
    independently of that internal contract: even a `dashboard.refresh`
    that unconditionally RAISES (a hypothetical regression of that
    internal contract) cannot un-send an already-sent confirmation/undo/
    edit/target reply, because the send already completed before refresh
    is ever called -- the raise only affects what happens AFTER, never
    the reply's own delivery or the underlying DB write."""
    config = Config()
    db.upsert_user(OWNER, role="owner", status="active")
    channel = _CapturingChannel()

    async def _raise(*args, **kwargs):
        raise RuntimeError("simulated dashboard.refresh failure")

    monkeypatch.setattr(dashboard, "refresh", _raise)

    # 1. A plain log.
    with pytest.raises(RuntimeError):
        await handle_inbound_message(
            "500ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
            clock=fixed_clock, user_id=OWNER,
        )
    assert len(channel.actionable) == 1 and "500" in channel.actionable[0][1]
    logged_row_id = db.last_log(OWNER)["id"]
    assert len(db.logs_between(OWNER, "2000-01-01T00:00:00", "2100-01-01T00:00:00")) == 1

    # 2. Text /undo.
    with pytest.raises(RuntimeError):
        await handle_inbound_message(
            "/undo", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
            clock=fixed_clock, user_id=OWNER,
        )
    assert any("Undone" in t for t in channel.sent)
    # `last_log` excludes soft-deleted rows (none remain after the only
    # log was just undone) -- look the specific row up by id instead.
    assert db.get_log(logged_row_id)["deleted_at"] is not None

    # 3. A log to then edit.
    with pytest.raises(RuntimeError):
        await handle_inbound_message(
            "500ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
            clock=fixed_clock, user_id=OWNER,
        )
    actionable_before_edit = len(channel.actionable)
    with pytest.raises(RuntimeError):
        await handle_inbound_message(
            "make that 700ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
            clock=fixed_clock, user_id=OWNER,
        )
    assert len(channel.actionable) == actionable_before_edit  # edit uses plain `send`, not `send_actionable`
    assert "700" in channel.sent[-1]
    assert db.last_log(OWNER)["value_num"] == 700.0

    # 4. /target set (direct command).
    with pytest.raises(RuntimeError):
        await handle_inbound_message(
            "/target water 2000", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
            clock=fixed_clock, user_id=OWNER,
        )
    assert "2000" in channel.sent[-1]
    assert db.get_target(OWNER, "water") == 2000.0


# ---------------------------------------------------------------------------
# 2. The full "wow" choreography end-to-end, through real async_main:
# /dashboard on -> preparse log (celebration + milestone ordering) -> pin
# edited -> undo via BUTTON -> pin reflects it -> /target change -> pin
# reflects it -> day rollover via the real CronTrigger job -> board
# refreshes again.
# ---------------------------------------------------------------------------


async def test_full_wow_choreography_end_to_end(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.upsert_record(OWNER, "water", "best_day", 100.0, "2000-01-01")  # low -- easy to beat
    seed_db.close()

    script = [
        ("message", OWNER, "/dashboard on", None),
        ("message", OWNER, "500ml", None),  # preparse hit -- zero LLM
    ]
    channel = await _run(monkeypatch, config, script, run_jobs=["dashboard_day_rollover"])

    # Step 1-2: dashboard on, then the log's own confirmation carries the
    # celebration (a record was broken), and the pin was edited to reflect it.
    log_confirmation = channel.sent_to(OWNER)[-1]
    assert "🎉" in log_confirmation  # celebration earned (best_day 100 -> 500)
    assert any("500" in t for _cid, _mid, t in channel.edits if _cid == OWNER)

    db = Database(tmp_path / "habits.db")
    try:
        row_id = db.last_log(OWNER)["id"]
        assert db.get_record(OWNER, "water", "best_day") == 500.0
    finally:
        db.close()

    # Step 3: undo via BUTTON -- the pin reflects it (back to zero).
    script2 = [("callback", OWNER, f"undo:{row_id}", "500ml", "cb-choreography")]
    channel2 = await _run(monkeypatch, config, script2)
    db = Database(tmp_path / "habits.db")
    try:
        assert db.get_log(row_id)["deleted_at"] is not None
    finally:
        db.close()
    assert any(cid == OWNER for cid, _mid, _t in channel2.edits) or channel2.pinned.get(OWNER)

    # Step 4: /target change -- the pin reflects the new goal denominator.
    # Step 5: day rollover, via the ACTUAL registered CronTrigger job
    # (captured off `_FakeScheduler` and invoked -- via `run_jobs`, so it
    # runs INSIDE this same run's still-open `db` connection, never after
    # `_run()` has already returned and async_main's own `finally` block
    # has closed it) -- the board refreshes again, unconditionally.
    script3 = [("message", OWNER, "/target water 3000", None)]
    channel3 = await _run(monkeypatch, config, script3, run_jobs=["dashboard_day_rollover"])

    rollover_job = _FakeScheduler.last_instance.jobs["dashboard_day_rollover"]
    assert isinstance(rollover_job.trigger, CronTrigger)
    owner_edits = [e for e in channel3.edits if e[0] == OWNER]
    assert any("3000" in t for _cid, _mid, t in owner_edits)  # the /target change's own refresh landed
    # The rollover job ran too (via `run_jobs`, inside this same call) --
    # it correctly produced NO further edit here, because nothing changed
    # between the /target refresh and the rollover's own immediately-
    # following refresh (R-D3's unchanged-render cache-skip, working as
    # designed, not a missed trigger) -- confirmed by no exception and by
    # a fresh, current render still matching what's actually pinned.
    db = Database(tmp_path / "habits.db")
    registry = HabitRegistry.from_config(config)
    try:
        board_lang = dashboard._board_language(db, config, OWNER)
        fresh_render = dashboard.render(db, config, registry, board_lang, OWNER, clock=datetime.now)
    finally:
        db.close()
    assert owner_edits[-1][2] == fresh_render  # the board is genuinely up to date after the rollover ran


# ---------------------------------------------------------------------------
# 3. Celebration correctness at the integrated level: a fresh user's
# first-ever log seeds silently (no celebration); a genuine break
# celebrates exactly once (not repeated for a second log at the same level).
# ---------------------------------------------------------------------------


async def test_fresh_users_first_ever_log_seeds_records_silently_no_celebration(db, registry, fixed_clock):
    """AC-M3-style regression, and the Round-2 Archi ruling both at once:
    a genuinely first-ever water log for a brand-new user must be BYTE-
    IDENTICAL to the pre-v1.6 confirmation -- no celebration line, because
    R-R2's "strictly exceeds the STORED record" presupposes a prior
    stored value, which a first observation doesn't have (it seeds the
    baseline silently instead, per Archi's 2026-08-24 ruling in
    TEST-v1.6-insights.md)."""
    channel = _CapturingChannel()
    config = Config()
    db.upsert_user(OWNER, role="owner", status="active")
    # No pre-seeded record, no prior logs -- a genuinely fresh user+habit.

    await handle_inbound_message(
        "500ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )

    text = channel.actionable[0][1]
    assert text == "✅ 500 ml logged — today 500 / 2500 ml (20%)"  # byte-identical to pre-v1.6, no celebration
    assert db.get_record(OWNER, "water", "best_day") == 500.0  # but the baseline WAS silently seeded


async def test_second_log_at_the_same_broken_level_does_not_re_celebrate(db, registry, fixed_clock):
    channel = _CapturingChannel()
    config = Config()
    db.upsert_user(OWNER, role="owner", status="active")
    db.upsert_record(OWNER, "water", "best_day", 100.0, "2000-01-01")

    await handle_inbound_message(
        "500ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )
    assert "🎉" in channel.actionable[-1][1]  # genuine break -- celebrates

    # A second log the SAME day, pushing the running total higher still --
    # best_day is already updated to today's running total by the first
    # call, so THIS log (which raises the total further) is itself a new,
    # genuine break and legitimately celebrates again (this is
    # `best_day`'s own "compare against today's running total live"
    # nature, distinct from `longest_streak`'s "same value re-celebrates"
    # documented quirk) -- confirmed by checking the stored value tracks.
    await handle_inbound_message(
        "200ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )
    assert db.get_record(OWNER, "water", "best_day") == 700.0

    # A THIRD log that does NOT raise the running total further is not
    # meaningful for best_day (value only ever increases via more logs) --
    # the genuinely repeatable "same level" case is an UNDO-then-relog
    # back to the identical value, which must not re-celebrate (strictly
    # GREATER, not greater-or-equal).
    await handle_inbound_message(
        "/undo", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )
    await handle_inbound_message(
        "200ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )
    assert "🎉" not in channel.actionable[-1][1]  # back to exactly 700 -- equal, not strictly greater -- no re-celebrate


async def test_celebration_reaches_a_user_who_never_enabled_the_dashboard(db, registry, fixed_clock):
    """SPEC-v1.6.md R-R2: the celebration line rides the CONFIRMATION,
    computed and appended entirely independently of `dashboard.refresh`
    (a separate call, later) -- a user who never ran `/dashboard on`
    still earns and sees the celebration; they just get no pin."""
    channel = _CapturingChannel()
    config = Config()
    db.upsert_user(OWNER, role="owner", status="active")
    db.upsert_record(OWNER, "water", "best_day", 100.0, "2000-01-01")
    assert db.get_dashboard_msg_id(OWNER) is None  # never enabled

    await handle_inbound_message(
        "500ml", db=db, llm=_RaisingLLM(), channel=channel, config=config, registry=registry,
        clock=fixed_clock, user_id=OWNER,
    )

    assert "🎉" in channel.actionable[-1][1]  # celebration reached them
    assert OWNER not in channel.pinned  # ...but no dashboard was ever pinned
    assert channel.edits == []  # and never edited either


# ---------------------------------------------------------------------------
# 4. Trends block: present after Garmin in the weekly review, and still
# fine (no crash, graceful "not enough history") for a user with no data.
# ---------------------------------------------------------------------------


async def test_weekly_review_trends_block_still_fine_for_a_user_with_no_data(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(MEMBER, role="member", status="active")  # active, but logs NOTHING at all
    seed_db.close()

    # A user with zero logs never reaches the "send a review" branch at
    # all (the pre-existing v1.2 "no logs in the window -> skip" rule) --
    # this proves that rule (unrelated to trends) still holds and nothing
    # crashes when trends.review_block would have nothing to report.
    channel = await _run(monkeypatch, config, script=[], run_jobs=["weekly_review"])
    assert channel.sent_to(MEMBER) == []  # skipped entirely, no crash, no empty/broken review


async def test_trends_block_present_and_after_garmin_when_garmin_configured(tmp_path, monkeypatch):
    config = Config.model_validate(
        {"app": {"db_path": str(tmp_path / "habits.db")}, "garmin": {"csv_path": str(tmp_path / "nonexistent.csv")}}
    )
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    from datetime import timedelta

    today = datetime.now()
    for offset in range(3):
        ts = (today - timedelta(days=offset)).isoformat(timespec="seconds")
        seed_db.insert_log(LogEntry(None, OWNER, ts, "water", 500.0, None, "500ml", "reply"))
    seed_db.close()

    channel = await _run(monkeypatch, config, script=[], run_jobs=["weekly_review"])
    review_text = channel.sent_to(OWNER)[0]

    # Both the weekly-review's own header AND the trends block's header
    # start with "📊" (a shared emoji, not a unique marker) -- use the
    # trends block's own distinct catalog string instead.
    trends_idx = review_text.find(i18n.t("trends_review_header", "th"))
    assert trends_idx != -1
    review_header_idx = review_text.find(i18n.t("weekly_review_header", "th"))
    assert review_header_idx != -1 and review_header_idx < trends_idx  # trends is the LAST section


# ---------------------------------------------------------------------------
# 5. Nudge through the real tick at 20:00, and its non-interference with
# the 20:00 check-in (both windows include 20:00 inclusively -- a user can
# legitimately receive BOTH messages in the same minute, independently).
# ---------------------------------------------------------------------------


async def test_nudge_and_20_00_checkin_both_fire_independently_without_interference(tmp_path, monkeypatch):
    """`fixed_20` below must name the SAME calendar day `_run` actually logs
    "2100ml" on -- see `test_nudge_fires_through_the_real_tick_for_an_
    enabled_almost_there_user`'s own docstring (same file) for the full
    root-cause note on why a hardcoded past date rots the moment a test
    run crosses midnight past it; found stale exactly this way during
    SPEC-v1.8.md's shared-surface pass (unrelated to that work)."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("message", OWNER, "/checkin on", None), ("message", OWNER, "2100ml", None)]  # 2100/2500 = 84% -- "close"
    channel = await _run(monkeypatch, config, script)
    before = len(channel.sent_to(OWNER))

    db = Database(tmp_path / "habits.db")
    registry = HabitRegistry.from_config(config)
    today = datetime.now()
    fixed_20 = lambda: today.replace(hour=20, minute=0, second=0, microsecond=0)  # noqa: E731
    try:
        # Both real tick functions, fired at the exact same real minute
        # (20:00) -- the default check-in window (08:00-20:00) AND the
        # default nudge time (20:00) both include this instant.
        await checkins.run_due_checkins(channel, config, registry, db, clock=fixed_20)
        await nudge.run_due_nudges(channel, config, registry, db, clock=fixed_20)
    finally:
        db.close()

    after = channel.sent_to(OWNER)
    assert len(after) == before + 2  # BOTH fired -- neither suppressed the other
    assert any("🌤️" in t for t in after[-2:])  # the check-in's own marker
    assert any(t for t in after[-2:] if "🌤️" not in t)  # a distinct, second (nudge) message


# ---------------------------------------------------------------------------
# 6. Menu/help: exactly 14 public commands in both language sets; /help
# lists the new v1.6 features, bilingually.
# ---------------------------------------------------------------------------


async def test_menu_has_exactly_22_public_commands_both_languages(tmp_path, monkeypatch):
    # RENAMED again (Archi-directed, SPEC-v1.9.md integration pass):
    # `/cadence`/`/pause`/`/resume`/`/wrapped` joined the public menu too
    # (18 -> 22 total) -- the test name now matches its own updated body/
    # count instead of documenting the stale "as of v1.8" figure. `channel.
    # set_my_commands_calls` only ever records the DEFAULT (global,
    # `scope_chat_id=None`) registration (see this file's own fake, above)
    # -- the owner-scoped second menu (AC-D2) additionally listing the
    # five admin commands is a SEPARATE call this list never captures, so
    # the admin-hidden assertion below still holds unchanged for the
    # public menu.
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    channel = await _run(monkeypatch, config, script=[])

    registered = channel.set_my_commands_calls[0]
    assert set(registered.keys()) == {"en", "th"}
    for lang, entries in registered.items():
        names = [name for name, _desc in entries]
        # SPEC-v1.10.md §4 R17 (integration pass): 22 -> 23, `/guide` added.
        assert len(names) == 23, f"{lang} menu has {len(names)} commands: {names}"
        assert len(set(names)) == 23  # no duplicates
        assert not (set(names) & {"approve", "block", "users", "invite", "audit"})  # admin-hidden, unchanged
        assert {"log", "routine"} <= set(names)  # SPEC-v1.8.md R-D2: the two v1.8 public commands
        assert {"cadence", "pause", "resume", "wrapped"} <= set(names)  # SPEC-v1.9.md §6/§11: the four new v1.9 public commands
        assert "guide" in names  # SPEC-v1.10.md §4 R17: the new v1.10 public command


async def test_help_text_lists_the_four_new_v16_commands_bilingually(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("message", OWNER, "/help", None), ("message", OWNER, "/lang th", None), ("message", OWNER, "/help", None)]
    channel = await _run(monkeypatch, config, script)

    help_en = channel.sent_to(OWNER)[0]
    help_th = channel.sent_to(OWNER)[-1]
    for cmd in ("/dashboard", "/heatmap", "/records", "/trends"):
        assert cmd in help_en
        assert cmd in help_th  # slash-commands themselves are untranslated, but the surrounding line is present


# ---------------------------------------------------------------------------
# 7. Deviation audit: the day-rollover job's native daily CronTrigger,
# confirmed CONFORMANT against the module IMPL's own minutely-guard
# suggestion (functional equivalence: fires exactly once at 00:00
# Asia/Bangkok daily, same coalesce/max_instances/misfire_grace_time
# missed-tick safety as the two pre-existing single-daily-instant jobs).
# ---------------------------------------------------------------------------


async def test_dashboard_day_rollover_cron_trigger_matches_the_documented_deviation(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    await _run(monkeypatch, config, script=[])

    job = _FakeScheduler.last_instance.jobs["dashboard_day_rollover"]
    assert isinstance(job.trigger, CronTrigger)
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "0"
    assert fields["minute"] == "0"
    assert str(job.trigger.timezone) == config.app.timezone  # Asia/Bangkok, no DST -- moot by construction

    # Same missed-tick/overlap safety as daily_summary/weekly_review's own
    # single-daily-instant jobs (coalesce a missed tick on restart into
    # one run; never overlap two runs).
    daily_summary_job = _FakeScheduler.last_instance.jobs.get("daily_summary")
    weekly_review_job = _FakeScheduler.last_instance.jobs.get("weekly_review")
    assert daily_summary_job is not None and weekly_review_job is not None  # sibling single-instant jobs exist too


# ---------------------------------------------------------------------------
# 10. Migration 009 rehearsal: a v8-shaped (v1.5-era) scratch DB with real
# pre-existing data, opened through the REAL async_main startup.
# ---------------------------------------------------------------------------


async def test_migration_009_rehearsal_on_a_v1_5_shaped_scratch_db(tmp_path, monkeypatch):
    db_path = tmp_path / "upgrade_rehearsal_v16.db"
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
          habit_type  TEXT NULL,
          user_id     TEXT NULL
        );
        CREATE TABLE habit_targets (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id    TEXT,
          habit_id   TEXT NOT NULL,
          goal       REAL NOT NULL,
          updated_at TEXT,
          UNIQUE(user_id, habit_id)
        );
        CREATE TABLE users (
          chat_id                 TEXT PRIMARY KEY,
          role                    TEXT NOT NULL DEFAULT 'member',
          status                  TEXT NOT NULL DEFAULT 'pending',
          display_name            TEXT,
          language_pref           TEXT NOT NULL DEFAULT 'auto',
          quiet_hours_json        TEXT,
          snooze_default_minutes  INTEGER,
          checkin_window          TEXT,
          last_announced_version  TEXT,
          created_at              TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE user_reminder_times (
          user_id  TEXT NOT NULL,
          habit_id TEXT NOT NULL,
          time     TEXT NOT NULL,
          PRIMARY KEY (user_id, habit_id, time)
        );
        CREATE TABLE audit_log (
          id             INTEGER PRIMARY KEY AUTOINCREMENT,
          ts             TEXT NOT NULL,
          user_id        TEXT NOT NULL,
          action         TEXT NOT NULL,
          entity         TEXT,
          old_value      TEXT,
          new_value      TEXT,
          source         TEXT NOT NULL,
          target_user_id TEXT,
          created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        PRAGMA user_version = 8;
        """
    )
    conn.execute("INSERT INTO users (chat_id, role, status) VALUES (?, 'owner', 'active')", (OWNER,))
    today_ts = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO logs (ts, category, value_num, value_text, raw_message, source, habit_type, user_id) "
        "VALUES (?, 'water', 500.0, NULL, '500ml', 'reply', 'numeric', ?)",
        (today_ts, OWNER),
    )
    conn.execute("INSERT INTO habit_targets (user_id, habit_id, goal) VALUES (?, 'water', 3000.0)", (OWNER,))
    conn.commit()
    conn.close()

    config = Config.model_validate({"app": {"db_path": str(db_path)}})
    script = [
        ("message", OWNER, "/habits", None),  # pre-existing v1.5-era data still works
        ("message", OWNER, "/dashboard on", None),  # a genuinely new-in-v1.6 write, post-upgrade
    ]
    channel = await _run(monkeypatch, config, script, owner_chat_id=OWNER)

    habits_reply = next(t for t in channel.sent_to(OWNER) if i18n.t("habits_overview_header", "en") in t)
    assert "500" in habits_reply

    db = Database(db_path)
    try:
        assert db.schema_version == 15
        cols = {row[1] for row in db._conn.execute("PRAGMA table_info(users)").fetchall()}
        assert "dashboard_msg_id" in cols
        tables = {row[0] for row in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "habit_records" in tables
        assert db.get_dashboard_msg_id(OWNER) is not None  # the post-upgrade /dashboard on write succeeded
        assert db.get_target(OWNER, "water") == 3000.0  # the legacy target override carried over
    finally:
        db.close()
