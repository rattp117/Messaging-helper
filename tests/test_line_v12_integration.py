"""SPEC-LINE-1.2.md §6/§11 "Integration order" (branch `line-version`,
v1.2.0): the dedicated R-I5 byte-identical gate + realtime end-to-end file
Luna's own IMPL-LINE-1.2.0.md "Known limitations" flagged as not yet
written ("a full realtime end-to-end ... has only my own throwaway smoke
coverage, not a committed regression test").

Covers: AC22 (Telegram byte-unchanged regardless of `mode`), AC23/R-I5
(`dashboard_in_reply=false` + `mode="digest"` -> byte-identical to 1.1.0),
AC24 (no migration on a pre-v1.2.0-shaped DB), AC25 (the full realtime
walkthrough: reminder push+ledger, log with board+undo-on-last, near-cap
owner warn, at-cap non-owner block with the owner still served and both
users' replies still working, no cross-user leakage).

Reuses `tests/test_line_integration.py`'s own low-level webhook-driving
primitives (`_LineApiRecorder`, `_line_channel_factory`, `_wait_for_port`,
`_sign`, `_post_events`, `_text_event`, `_wait_until`, `OWNER`/`MEMBER`) --
the same import convention `test_line_release_gate.py`/
`test_line_readable_approval.py` already establish -- rather than
duplicating that machinery. `_running_line_app` here is a LOCAL variant
(not imported) because it needs three additional config knobs
(`mode`/`push_cap`/`dashboard_in_reply`) the shared one doesn't expose.

No production code is modified by this file."""

from __future__ import annotations

import asyncio
import itertools
import json
from datetime import datetime
from types import SimpleNamespace
from contextlib import asynccontextmanager

import httpx
import pytest

from conftest import FakeScheduler
from habit_assistant import main as main_module
from habit_assistant.config import Config
from habit_assistant.storage.db import Database
from test_line_integration import (
    MEMBER,
    OWNER,
    _line_channel_factory,
    _LineApiRecorder,
    _post_events,
    _text_event,
    _wait_for_port,
    _wait_until,
)

# A third, distinct user for AC25's two-non-owner-user cap/isolation scenario
# -- MEMBER (from test_line_integration) plays the "near-cap warn" role,
# MEMBER2 plays the "blocked at cap" role, so the test can assert MEMBER's
# own reply/push traffic is untouched by what happens to MEMBER2.
MEMBER2 = "Umembertwo000000000000000000000"

_PORTS = itertools.count(19901)


def _make_config(
    *,
    port: int,
    media_dir,
    db_path,
    mode: str = "digest",
    push_cap: int = 15000,
    dashboard_in_reply: bool = True,
    warn_cap: int = 280,
    digest_time: str = "20:00",
    channel_type: str = "line",
) -> Config:
    cfg: dict = {
        "app": {"db_path": str(db_path), "timezone": "Asia/Bangkok"},
        "i18n": {"language": "en"},
        "channel": {"type": channel_type},
        "ollama": {"enabled": False},
        "digest": {"time": digest_time, "warn_cap": warn_cap, "enabled": True, "mode": mode, "push_cap": push_cap},
    }
    if channel_type == "line":
        cfg["line"] = {
            "public_base_url": f"http://127.0.0.1:{port}",
            "bind_host": "127.0.0.1",
            "bind_port": port,
            "media_dir": str(media_dir),
            "media_ttl_seconds": 3600,
            "dashboard_in_reply": dashboard_in_reply,
        }
    return Config.model_validate(cfg)


@asynccontextmanager
async def _running_line_app(monkeypatch, tmp_path, *, users: tuple[str, ...] = (OWNER, MEMBER), **config_kwargs):
    """Local variant of `test_line_integration._running_line_app`,
    parameterized for `mode`/`push_cap`/`dashboard_in_reply` and an
    arbitrary user tuple (AC25 needs three: owner + two non-owner
    members)."""
    port = next(_PORTS)
    db_path = tmp_path / "habits.db"
    media_dir = tmp_path / "media"
    config = _make_config(port=port, media_dir=media_dir, db_path=db_path, **config_kwargs)

    seed_db = Database(db_path)
    for uid in users:
        seed_db.upsert_user(uid, role="member", status="active")
    seed_db.close()

    recorder = _LineApiRecorder()
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module,
        "load_secrets",
        lambda **kwargs: SimpleNamespace(
            telegram_bot_token=None,
            telegram_chat_id=None,
            line_channel_access_token="test-access-token",
            line_channel_secret="test-channel-secret",
            line_owner_user_id=OWNER,
        ),
    )
    monkeypatch.setattr(main_module, "LineChannel", _line_channel_factory(recorder))
    monkeypatch.setattr(main_module, "AsyncIOScheduler", FakeScheduler)

    class _PoisonedOllamaClient:
        def __init__(self, *a, **k):
            raise AssertionError("OllamaClient must never be constructed with ollama.enabled=False")

    class _PoisonedHealthMonitor:
        def __init__(self, *a, **k):
            raise AssertionError("HealthMonitor must never be constructed on the LINE path")

    monkeypatch.setattr(main_module, "OllamaClient", _PoisonedOllamaClient)
    monkeypatch.setattr(main_module, "HealthMonitor", _PoisonedHealthMonitor)
    FakeScheduler.last_instance = None

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None, migrate=False, backup=False, restore=None, yes=False)
    task = asyncio.create_task(main_module.async_main(args))
    try:
        await _wait_for_port(config.line.bind_host, config.line.bind_port)
        db = Database(db_path)
        try:
            yield SimpleNamespace(db=db, api=recorder, scheduler=FakeScheduler.last_instance, config=config, port=port)
        finally:
            db.close()
    finally:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


# ===========================================================================
# AC24 -- no migration on a pre-v1.2.0-shaped DB.
# ===========================================================================


def test_ac24_opening_a_db_under_v12_applies_no_new_migration(tmp_path):
    """R-I4: `push_ledger`/`users.digest_opt_out` already exist from
    migration 014 -- v1.2.0 ships no new migration at all. A DB created
    once (schema 0 -> 15) and then reopened by this SAME v1.2.0 codebase
    a second time must show NO further migration work: `schema_version_
    before == schema_version == 15` on the reopen, not just "== 15"."""
    db_path = tmp_path / "habits.db"
    first = Database(db_path)
    assert first.schema_version == 15
    first.close()

    second = Database(db_path)
    try:
        assert second.schema_version_before == 15, "reopening a fully-migrated DB must start at 15, not re-run from 0"
        assert second.schema_version == 15, "v1.2.0 must apply zero new migrations opening a 1.1.0-shaped DB (R-I4)"
    finally:
        second.close()


# ===========================================================================
# AC22 -- Telegram is byte-unchanged regardless of `mode` (a LINE-only knob).
# ===========================================================================


async def test_ac22_telegram_reminder_fires_identically_under_digest_and_realtime_mode(tmp_path):
    """Every §4 gate this release adds is spelled `config.channel.type ==
    "line" and ...` -- on Telegram (`type != "line"`) that condition is
    always False, so `mode`'s value can never matter. Proven directly
    against `core/jobs.py:minutely_tick` (not the full webhook app --
    R-I1's gate is the same one line of code either way, and this is the
    faster, equally rigorous way to exercise it) with a habit reminder
    time pinned to the real current minute, once under each `mode`."""
    from habit_assistant.core import jobs
    from habit_assistant.core.habits import HabitRegistry
    from habit_assistant.core.reminders import ReminderState, run_due_reminders
    from habit_assistant.core.registry_provider import RegistryProvider
    from conftest import RecordingChannel

    now_hhmm = datetime.now().strftime("%H:%M")
    results = {}
    for mode in ("digest", "realtime"):
        db_path = tmp_path / f"tg_{mode}.db"
        db = Database(db_path)
        db.upsert_user(OWNER, role="owner", status="active")
        db.set_reminder_times(OWNER, "water", [now_hhmm])
        config = Config.model_validate(
            {"app": {"db_path": str(db_path)}, "channel": {"type": "telegram"}, "digest": {"mode": mode}}
        )
        registry = HabitRegistry.from_config(config)
        provider = RegistryProvider(config, db)
        channel = RecordingChannel()

        await jobs.minutely_tick(
            channel, config, registry, db, ReminderState(), provider, run_due_reminders=run_due_reminders,
        )
        results[mode] = list(channel.sent)
        db.close()

    assert results["digest"] != [], "a Telegram reminder must fire in digest mode (Telegram has no digest concept)"
    assert results["digest"] == results["realtime"], "AC22: Telegram behavior must be byte-unchanged across mode values"


# ===========================================================================
# AC23/R-I5 -- dashboard_in_reply=false + mode="digest" -> byte-identical to
# 1.1.0 LINE output (single-object reply, undo on the confirmation itself,
# digest push unaffected by push_cap since the gate is a pass-through).
# ===========================================================================


async def test_ac23_dashboard_off_and_digest_mode_reply_is_byte_identical_to_1_1_0_shape(monkeypatch, tmp_path):
    async with _running_line_app(
        monkeypatch, tmp_path, dashboard_in_reply=False, mode="digest", push_cap=1,  # tiny cap -- must still never gate digest
    ) as app:
        resp = await _post_events(app.port, [_text_event(MEMBER, "500ml", reply_token="rt-off")])
        assert resp.status_code == 200

        reply_bodies = await _wait_until(lambda: app.api.calls_matching("/message/reply") or None)
        assert len(reply_bodies) == 1
        (reply_body,) = reply_bodies
        messages = reply_body["messages"]
        assert len(messages) == 1, "R-A7: no board object appended when dashboard_in_reply=false"
        assert "quickReply" in messages[0], "undo must stay on the confirmation object itself (no consolidation needed)"

        # Digest push must still succeed at a push_cap of 1 -- R-I5/R-Q2:
        # mode="digest" makes the quota gate a pure pass-through, cap value
        # irrelevant.
        job = app.scheduler.get_job("daily_digest")
        assert job is not None
        await job.func()
        push_bodies = app.api.calls_matching("/message/push")
        assert any(body["to"] == MEMBER for body in push_bodies), "digest push must not be gated by push_cap in digest mode"


# ===========================================================================
# AC25 -- the full realtime end-to-end walkthrough.
# ===========================================================================


async def test_ac25_realtime_end_to_end_reminder_push_log_warn_then_cap_block_with_two_user_isolation(
    monkeypatch, tmp_path
):
    """One user's reminder fires as a real push (ledger+1); that SAME user
    then logs and gets a free reply with the board appended and `undo` on
    the trailing (board) object; once the running total is pre-seeded to
    exactly `int(push_cap * 0.8)`, the next allowed non-owner push (a
    second user's reminder) also triggers the owner's one-time 80% warn;
    once the total is at/over `push_cap`, a THIRD non-owner push attempt
    is dropped (no send, no ledger increment) while the owner's own
    proactive push still gets through and both non-owner users' replies
    keep working -- and no user's push/reply ever carries another's data
    or reaches another's chat_id."""
    push_cap = 5  # small and exact: int(5*0.8) == 4, easy to seed precisely
    async with _running_line_app(
        monkeypatch, tmp_path, users=(OWNER, MEMBER, MEMBER2), mode="realtime", push_cap=push_cap, dashboard_in_reply=True,
    ) as app:
        yyyymm = datetime.now().strftime("%Y-%m")
        now_hhmm = datetime.now().strftime("%H:%M")

        # --- Step 1: MEMBER's reminder fires as a real push, ledger+1. ---
        # Cleared back to "off" immediately after -- this test fires
        # several ticks within the SAME real wall-clock minute (something
        # that never happens in production, where ticks are 60s apart), so
        # a still-due override left in place would re-fire on every later
        # tick call below and pollute the cap-boundary arithmetic.
        app.db.set_reminder_times(MEMBER, "water", [now_hhmm])
        tick = app.scheduler.get_job("minutely_tick")
        assert tick is not None
        await tick.func()
        app.db.set_reminder_times(MEMBER, "water", ["off"])

        push_bodies = app.api.calls_matching("/message/push")
        member_pushes = [b for b in push_bodies if b["to"] == MEMBER]
        assert len(member_pushes) == 1, "MEMBER's configured-time reminder must fire exactly once as a real Push"
        assert app.db.push_count(MEMBER, yyyymm) == 1

        # --- Step 2: MEMBER logs -- free reply, board appended, undo on
        # the trailing object, no push spent for it. ---
        resp = await _post_events(app.port, [_text_event(MEMBER, "500ml", reply_token="rt-member-log")])
        assert resp.status_code == 200
        reply_bodies = await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-member-log"] or None
        )
        (log_reply,) = reply_bodies
        assert len(log_reply["messages"]) == 2, "AC2: confirmation + trailing board object"
        assert "quickReply" not in log_reply["messages"][0]
        assert "quickReply" in log_reply["messages"][1], "AC3: undo relocated onto the trailing board object"
        assert app.db.push_count(MEMBER, yyyymm) == 1, "the log's own reply must not spend push quota"

        # --- Step 3: pre-seed the running total to EXACTLY int(cap*0.8),
        # then fire MEMBER2's reminder -- this push must go through AND
        # trigger the owner's one-time 80% warn. ---
        target_pre_warn_total = int(push_cap * 0.8)  # == 4
        while app.db.monthly_push_total(yyyymm) < target_pre_warn_total:
            app.db.increment_push(OWNER, yyyymm)  # a neutral, non-attributed seed of prior activity
        assert app.db.monthly_push_total(yyyymm) == target_pre_warn_total

        app.db.set_reminder_times(MEMBER2, "water", [now_hhmm])
        await tick.func()

        push_bodies = app.api.calls_matching("/message/push")
        member2_pushes = [b for b in push_bodies if b["to"] == MEMBER2]
        assert len(member2_pushes) == 1, "MEMBER2's push must be ALLOWED at exactly int(cap*0.8) (still < cap)"
        owner_pushes = [b for b in push_bodies if b["to"] == OWNER]
        warn_pushes = [b for b in owner_pushes if "push_quota_warn" in json.dumps(b) or "%" in b["messages"][0]["text"]]
        assert len(warn_pushes) == 1, f"exactly one owner 80% warn expected, got {len(warn_pushes)}: {owner_pushes}"

        # Firing the tick again this same month must NOT re-warn (R-Q6
        # once-per-month dedup) even though the ratio is still >= 80%.
        # MEMBER2's own reminder is cleared first so this second tick has
        # a due candidate again without re-adding to the running total via
        # a habit that already fired.
        app.db.set_reminder_times(MEMBER2, "water", ["off"])
        app.db.set_reminder_times(MEMBER2, "stretch", [now_hhmm])
        push_count_before = len(app.api.calls_matching("/message/push"))
        await tick.func()
        app.db.set_reminder_times(MEMBER2, "stretch", ["off"])
        new_warns = [
            b for b in app.api.calls_matching("/message/push")[push_count_before:]
            if b["to"] == OWNER and "%" in b["messages"][0]["text"]
        ]
        assert new_warns == [], "R-Q6: the 80% owner warn must fire at most once per calendar month"

        # --- Step 4: push the running total to/over cap, then attempt a
        # non-owner push -- must be DROPPED (no send, no ledger increment)
        # while the OWNER's own proactive push still succeeds. ---
        while app.db.monthly_push_total(yyyymm) < push_cap:
            app.db.increment_push(OWNER, yyyymm)
        total_at_cap = app.db.monthly_push_total(yyyymm)
        assert total_at_cap >= push_cap

        member_count_before = app.db.push_count(MEMBER, yyyymm)
        owner_pushes_before = app.db.push_count(OWNER, yyyymm)
        app.db.set_reminder_times(MEMBER, "water", [now_hhmm])  # due again
        # Same tick also proves R-Q3's owner exemption: the owner gets a
        # DIFFERENT due habit in this exact tick so both dispositions
        # (non-owner dropped, owner still served) are observed together,
        # from the same real push-quota gate, in the same call.
        app.db.set_reminder_times(OWNER, "stretch", [now_hhmm])
        await tick.func()
        app.db.set_reminder_times(MEMBER, "water", ["off"])
        app.db.set_reminder_times(OWNER, "stretch", ["off"])

        assert app.db.push_count(MEMBER, yyyymm) == member_count_before, "AC17: a non-owner push at/over cap must be DROPPED, no ledger increment"
        assert app.db.push_count(OWNER, yyyymm) > owner_pushes_before, "AC17: the owner keeps receiving pushes at/over cap"

        # Exactly one stop alert to the owner this month (R-Q5/R-Q6).
        stop_alerts = [
            b for b in app.api.calls_matching("/message/push")
            if b["to"] == OWNER and ("cap" in b["messages"][0]["text"].lower() and "%" not in b["messages"][0]["text"])
        ]
        assert len(stop_alerts) >= 1, "expected at least one owner stop alert once the cap was reached"

        # --- Step 5: BOTH non-owner users' replies still work at/over cap
        # (AC17/R-Q8: the gate never touches the reply path). ---
        resp_member = await _post_events(app.port, [_text_event(MEMBER, "diary note", reply_token="rt-member-2")])
        resp_member2 = await _post_events(app.port, [_text_event(MEMBER2, "diary note two", reply_token="rt-member2-2")])
        assert resp_member.status_code == 200
        assert resp_member2.status_code == 200
        await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-member-2"] or None
        )
        await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-member2-2"] or None
        )

        # --- Step 6: no cross-user leakage anywhere in the recorded
        # traffic -- MEMBER's own push/reply bodies never mention MEMBER2
        # (or vice versa), and every push/reply is addressed to the right
        # chat only. ---
        for call_path, body in app.api.calls:
            if not isinstance(body, dict):
                continue
            if call_path.endswith("/message/push"):
                to = body.get("to")
                text_blob = json.dumps(body["messages"])
                other = MEMBER2 if to == MEMBER else (MEMBER if to == MEMBER2 else None)
                if other is not None:
                    assert other not in text_blob, f"push to {to} leaked {other}'s id: {body}"
            if call_path.endswith("/message/reply"):
                token = body.get("replyToken")
                text_blob = json.dumps(body["messages"])
                if token == "rt-member-log" or token == "rt-member-2":
                    assert MEMBER2 not in text_blob, f"MEMBER's reply ({token}) leaked MEMBER2's id: {body}"
                if token == "rt-member2-2":
                    assert MEMBER not in text_blob, f"MEMBER2's reply ({token}) leaked MEMBER's id: {body}"
