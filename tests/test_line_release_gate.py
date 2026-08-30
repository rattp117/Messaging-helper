"""Final release-gate verification (branch `line-version`), commissioned
by Archi for a PASS/FAIL sign-off before tagging `line/v1.0.0`.

Scope (per Archi's own dispatch, not a module's own AC list): probe BEYOND
`tests/test_line_integration.py`'s existing 16 end-to-end tests --

1. The full user journey through the REAL wired app: onboarding/approval,
   log -> confirm+undo -> tap undo -> undone, unparseable text ->
   tap-to-fix -> tap -> logged, entirely no-LLM (poisoned OllamaClient at
   the app level).
2. The digest at the scheduler level: exact quiet-hours boundary
   (inclusive start / exclusive end, both plain and midnight-crossing),
   a simulated double-fire, opt-out honored end-to-end.
3. Two-user isolation on LINE through the full wired pipeline.
4. A real Telegram-mode message round trip (not just wiring), proving the
   branch didn't leak any LINE-only behavior onto the Telegram path.
5. A closed formal-test gap for AC1's negative/positive `load_secrets`
   paths (previously verified only by a throwaway ad-hoc script, per
   IMPL-LINE-shared.md's own smoke-test section -- never a committed
   regression test).
6. Deploy-consistency (webhook port/path one value everywhere) and
   version/release-note-posture spot checks.
7. A direct regression pin for the `reminders.py:429` self-fix (the
   pause-suppression date check must honor an INJECTED clock, not the
   real wall-clock date).

No production code is modified by this file. Reuses `test_line_integration.
py`'s own fixtures/helpers (same convention `test_line_c_gaps.py` already
uses to import from `test_deploy_line.py`) rather than duplicating the
real-webhook-server harness."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from conftest import FakeScheduler
from habit_assistant import main as main_module
from habit_assistant.config import Config, ConfigError, load_secrets
from habit_assistant.core.digest import _dnd_deferred_datetime
from habit_assistant.storage.db import Database
from test_line_integration import (
    MEMBER,
    OWNER,
    _FakeHealthMonitor,
    _FakeOllamaClient,
    _post_events,
    _postback_event,
    _running_line_app,
    _StopAfterRun,
    _text_event,
    _wait_until,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ===========================================================================
# 1. Full user journey through the REAL wired app.
# ===========================================================================


async def test_full_journey_log_undo_and_tapfix_clarify_no_llm_end_to_end(monkeypatch, tmp_path):
    """The working half of the journey: log "500ml" -> ONE reply with
    confirmation + undo quick-reply -> tap undo -> undone; then an
    unparseable bare number ("500", no unit -- `core/preparse.py`'s own
    documented scope always misses a bare number, R-L1 §9) -> tap-to-fix
    quick-reply (R-B2) -> tap -> logged. Entirely no-LLM: `_running_line_
    app` poisons `OllamaClient`/`HealthMonitor` at the app level, so this
    is also a live proof of R-B9/R-B8 for every step below, not just the
    one test that names it."""
    async with _running_line_app(monkeypatch, tmp_path) as app:
        resp = await _post_events(app.port, [_text_event(MEMBER, "500ml", reply_token="rt-log")])
        assert resp.status_code == 200
        reply_bodies = await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-log"] or None
        )
        (reply_body,) = reply_bodies
        messages = reply_body["messages"]
        assert len(messages) == 1, "one confirmation message, batched into the one free reply (R-A4)"
        msg = messages[0]
        assert "500" in msg["text"]
        actions = [item["action"] for item in msg["quickReply"]["items"]]
        undo_actions = [a for a in actions if a["type"] == "postback" and a["data"].startswith("undo:")]
        assert len(undo_actions) == 1
        undo_data = undo_actions[0]["data"]

        today = datetime.now().date().isoformat()
        assert app.db.sum_value(MEMBER, "water", today) == 500.0

        # -- tap undo -> undone --
        await _post_events(app.port, [_postback_event(MEMBER, undo_data, reply_token="rt-undo")])
        await _wait_until(lambda: True if app.db.sum_value(MEMBER, "water", today) == 0.0 else None)
        assert app.db.sum_value(MEMBER, "water", today) == 0.0

        # -- unparseable bare number "500" -> tap-to-fix clarify offer --
        # (SPEC-LINE.md §5.2 row 1 / R-B2: preparse misses a bare number,
        # falls straight to clarify.tier1_guesses -- against the default
        # registry this is documented to return exactly [("water", 500.0)]).
        resp2 = await _post_events(app.port, [_text_event(MEMBER, "500", reply_token="rt-clarify")])
        assert resp2.status_code == 200
        clarify_bodies = await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-clarify"] or None
        )
        (clarify_body,) = clarify_bodies
        clarify_msg = clarify_body["messages"][0]
        assert "quickReply" in clarify_msg, (
            f"a bare number with a unit-plausible guess must offer tap-to-fix buttons (R-B2), got: {clarify_msg!r}"
        )
        clarify_actions = [item["action"] for item in clarify_msg["quickReply"]["items"]]
        clarify_data = next(a["data"] for a in clarify_actions if a["data"].startswith("clarify:"))

        # -- tap the guess -> logged --
        await _post_events(app.port, [_postback_event(MEMBER, clarify_data, reply_token="rt-clarify-tap")])
        await _wait_until(lambda: True if app.db.sum_value(MEMBER, "water", today) == 500.0 else None)
        assert app.db.sum_value(MEMBER, "water", today) == 500.0, "tapping the tap-to-fix guess must log the value"

        # No unparsed/awaiting_llm row survives anywhere in this journey (AC15).
        assert app.db.pending_unparsed() == []


async def test_new_line_user_owner_notified_via_push_and_asker_reply_stays_clean(monkeypatch, tmp_path):
    """RE-VERIFICATION (Luna's fix, release-gate round 2): `LineChannel`'s
    R-A4 reply-buffer aggregation (`channels/line.py:_REPLY_CONTEXT`) now
    threads the inbound event's own `user_id` through as `_reply_scope`'s
    `owner_chat_id` (`channels/line_webhook.py:_dispatch`), and `_emit`
    pushes (spending quota, R-A6/R-C6) instead of buffering whenever the
    target `chat_id` doesn't match the context's own owner. Proves the
    full chain for `core/access.py:handle_gate`'s cross-user owner
    notification -- previously FAILING (owner got nothing, asker's own
    reply was polluted with owner-facing admin text); now expected to
    PASS: the owner gets a real push (ledger incremented for it), and the
    asker's own reply carries only their own content."""
    async with _running_line_app(monkeypatch, tmp_path) as app:
        NEW_USER = "Ubrandnew0000000000000000000000000"
        yyyymm = datetime.now().strftime("%Y-%m")
        assert app.db.push_count(OWNER, yyyymm) == 0

        resp = await _post_events(app.port, [_text_event(NEW_USER, "/start", reply_token="rt-newuser")])
        assert resp.status_code == 200
        reply_bodies = await _wait_until(lambda: app.api.calls_matching("/message/reply") or None)
        (reply_body,) = reply_bodies
        texts = [m.get("text", "") for m in reply_body["messages"]]

        # The asker's own reply must carry ONLY their own access_pending
        # notice -- never the owner-facing "someone wants access" text.
        leaked = [t for t in texts if "Approve with" in t or "asked for access" in t]
        assert not leaked, (
            f"cross-user leak regressed -- owner-facing access_request text was delivered into the "
            f"ASKER's own reply instead of reaching the owner. Leaked message(s): {leaked!r}"
        )

        # The owner is notified via a real PUSH (no active reply context
        # of their own during the asker's event).
        owner_pushes = [b for b in app.api.calls_matching("/message/push") if b["to"] == OWNER]
        assert owner_pushes, "the owner must receive a push notifying them a new user is requesting access"
        owner_text = owner_pushes[0]["messages"][0]["text"]
        assert NEW_USER in owner_text and "/approve" in owner_text

        # The push must be counted against the owner's own quota ledger
        # (R-A6/R-C6: the channel's push path is the ONE authoritative
        # place `push_ledger` is incremented, regardless of caller).
        assert app.db.push_count(OWNER, yyyymm) == 1, (
            "the owner-notification push must increment push_ledger for the owner exactly once -- R-A6/R-C6"
        )


async def test_approve_command_accepts_real_line_userid_shape_end_to_end(monkeypatch, tmp_path):
    """RE-VERIFICATION (Luna's fix): `core/access.py`'s `_CHAT_ID_RE` now
    whitelists `U[0-9A-Za-z]{16,40}` alongside the pre-existing Telegram
    numeric shape. Previously FAILING (every real LINE userId fell
    through to the usage error, onboarding was structurally impossible);
    now expected to PASS end-to-end: `/approve <line_user_id>` flips the
    target to active AND the newly-approved user is notified (a push,
    same cross-chat-send fix as above -- `execute_admin`'s own
    `access_granted` notice is the SAME mechanism, `access.py:322`).
    The pending row is seeded directly (bypassing `/start`) to isolate
    this from the owner-notification path proven separately above."""
    async with _running_line_app(monkeypatch, tmp_path) as app:
        NEW_USER = "Ubrandnew0000000000000000000000000"
        app.db.upsert_user(NEW_USER, status="pending", display_name="New User")
        yyyymm = datetime.now().strftime("%Y-%m")

        await _post_events(app.port, [_text_event(OWNER, f"/approve {NEW_USER}", reply_token="rt-approve")])
        approve_bodies = await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-approve"] or None
        )
        approve_text = approve_bodies[0]["messages"][0]["text"]
        assert "Usage:" not in approve_text, (
            f"/approve {NEW_USER!r} must not fall through to the usage error -- got: {approve_text!r}"
        )
        assert app.db.get_user(NEW_USER)["status"] == "active", (
            f"/approve {NEW_USER!r} must flip the user to active status -- it never did"
        )

        # The newly-approved user must actually be notified -- a push,
        # since `execute_admin` sends it while still processing the
        # OWNER's own /approve event (same cross-chat-send mechanism as
        # the owner-notification path above).
        approved_pushes = [b for b in app.api.calls_matching("/message/push") if b["to"] == NEW_USER]
        assert approved_pushes, "the newly-approved user must be notified (a push) that they're approved"
        assert app.db.push_count(NEW_USER, yyyymm) == 1

        # The owner's OWN reply (the /approve ack) must not also carry
        # the approved-user's own notification text (the historical leak
        # direction, mirrored for this call site).
        owner_ack_text = approve_bodies[0]["messages"]
        assert len(owner_ack_text) == 1, "the owner's own /approve reply must carry only their own ack"


async def test_two_user_onboarding_and_approval_gate_journey_no_cross_contamination(monkeypatch, tmp_path):
    """Fresh release-gate-round-2 probe: the full onboarding/approval
    gate journey, re-run for TWO independent new users (not just one),
    through the real wired app -- proving the fix holds under multi-user
    conditions, not just the single-user case the two defect tests above
    isolate. Each user's own access_pending reply must stay clean; the
    owner must receive two independent, correctly-attributed push
    notifications and be able to approve each independently; a second
    user's onboarding must never leak into or block the first's."""
    async with _running_line_app(monkeypatch, tmp_path) as app:
        USER_A = "Ualpha00000000000000000000000000"
        USER_B = "Ubeta000000000000000000000000000"
        yyyymm = datetime.now().strftime("%Y-%m")

        await _post_events(app.port, [_text_event(USER_A, "/start", reply_token="rt-a-start")])
        reply_a = await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-a-start"] or None
        )
        assert not any("asked for access" in m.get("text", "") for m in reply_a[0]["messages"]), (
            "USER_A's own reply must not carry owner-facing admin text"
        )

        await _post_events(app.port, [_text_event(USER_B, "/start", reply_token="rt-b-start")])
        reply_b = await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-b-start"] or None
        )
        assert not any("asked for access" in m.get("text", "") for m in reply_b[0]["messages"]), (
            "USER_B's own reply must not carry owner-facing admin text, and must not mention USER_A"
        )
        assert not any(USER_A in m.get("text", "") for m in reply_b[0]["messages"]), (
            "USER_B's own reply must never mention USER_A (cross-user isolation)"
        )

        owner_pushes = [b for b in app.api.calls_matching("/message/push") if b["to"] == OWNER]
        assert len(owner_pushes) == 2, "the owner must get one independent push per new-user request"
        owner_texts = " ".join(p["messages"][0]["text"] for p in owner_pushes)
        assert USER_A in owner_texts and USER_B in owner_texts
        assert app.db.push_count(OWNER, yyyymm) == 2

        await _post_events(app.port, [_text_event(OWNER, f"/approve {USER_A}", reply_token="rt-approve-a")])
        await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-approve-a"] or None
        )
        assert app.db.get_user(USER_A)["status"] == "active"
        assert app.db.get_user(USER_B)["status"] == "pending", "approving USER_A must not also approve USER_B"

        await _post_events(app.port, [_text_event(OWNER, f"/approve {USER_B}", reply_token="rt-approve-b")])
        await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-approve-b"] or None
        )
        assert app.db.get_user(USER_B)["status"] == "active"

        # Both newly-active users can now log, independently, with no leakage.
        await _post_events(app.port, [_text_event(USER_A, "200ml", reply_token="rt-a-log")])
        await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-a-log"] or None
        )
        await _post_events(app.port, [_text_event(USER_B, "400ml", reply_token="rt-b-log")])
        await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-b-log"] or None
        )
        today = datetime.now().date().isoformat()
        assert app.db.sum_value(USER_A, "water", today) == 200.0
        assert app.db.sum_value(USER_B, "water", today) == 400.0


@pytest.mark.parametrize(
    "candidate,should_match",
    [
        ("U" + "a" * 15, False),  # 15 chars after "U" -- one below the 16-char floor
        ("U" + "a" * 16, True),  # exactly at the floor
        ("U" + "0123456789abcdef", True),  # a real-shaped id, 16 hex chars after "U"
        ("U" + "a" * 40, True),  # exactly at the 40-char ceiling
        ("U" + "a" * 41, False),  # one above the ceiling
        ("U" + "AbCdEf0123456789", True),  # mixed case must be accepted
        ("u" + "a" * 20, False),  # lowercase "u" prefix must NOT match (LINE ids are always "U")
    ],
)
def test_chat_id_regex_boundary_and_case_probes(candidate, should_match):
    """Fresh release-gate-round-2 probes on the exact whitelist Luna
    shipped (`core/access.py:_CHAT_ID_RE = re.compile(r"^(?:-?\\d+|U[0-
    9A-Za-z]{16,40})$")`): the length bound is exact at both ends, and
    the alphanumeric class is genuinely case-insensitive (matching the
    module's own stated rationale -- this app's own LINE test fixtures,
    e.g. "Uowner...'/'Umember...", are not hex, so a hex-only whitelist
    would have rejected them)."""
    from habit_assistant.core.access import _CHAT_ID_RE

    matched = _CHAT_ID_RE.match(candidate) is not None
    assert matched == should_match, f"{candidate!r} (len={len(candidate)}): expected match={should_match}, got {matched}"


def test_line_channel_bare_reply_scope_call_preserves_documented_default(tmp_path):
    """`_reply_scope`'s `owner_chat_id` param is additive/keyword-
    defaulted `None` specifically so a caller with no owning chat_id to
    compare against (a hand-rolled test, or any future bare call site)
    still gets the pre-fix "buffer everything sent during this scope"
    behavior -- this is a documented fallback, not a live bug, now that
    every REAL production call site (`channels/line_webhook.py:_dispatch`)
    always supplies the event's own user_id. See `test_line_channel_
    reply_buffer_pushes_cross_chat_sends_when_owner_chat_id_given` below
    for the fixed, chat_id-aware production behavior."""
    from habit_assistant.channels.line import LineChannel

    calls: list[tuple[str, dict]] = []

    async def _handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content) if request.content else {}))
        return httpx.Response(200, json={})

    async def _run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(_handle))
        config = Config.model_validate({"line": {"media_dir": str(tmp_path / "media")}})

        class _StubDb:
            def increment_push(self, *a, **k):
                pass

        channel = LineChannel("tok", "secret", "OWNER_ID", config, _StubDb(), client=client)
        with channel._reply_scope("rt-bare-call") as ctx:  # no owner_chat_id supplied -- defaults to None
            await channel.send("ASKER_ID", "hello asker")
            await channel.send("OWNER_ID", "hello owner (a DIFFERENT chat_id)")
        await channel._flush_reply("rt-bare-call", ctx["buffer"])
        await channel.aclose()

    import asyncio

    asyncio.run(_run())

    reply_calls = [(p, b) for p, b in calls if p.endswith("/message/reply")]
    push_calls = [(p, b) for p, b in calls if p.endswith("/message/push")]
    assert len(reply_calls) == 1 and push_calls == [], (
        "a bare _reply_scope() call (no owner_chat_id) must keep buffering everything, by design"
    )
    assert len(reply_calls[0][1]["messages"]) == 2


async def test_line_channel_reply_buffer_pushes_cross_chat_sends_when_owner_chat_id_given(tmp_path):
    """THE FIX, isolated at the channel level (mirrors production:
    `channels/line_webhook.py:_dispatch` always supplies the event's own
    user_id as `owner_chat_id`). A send to a DIFFERENT chat_id than the
    context's own owner must PUSH (and increment that chat_id's own
    ledger), not buffer into the wrong user's reply."""
    from habit_assistant.channels.line import LineChannel

    calls: list[tuple[str, dict]] = []
    incremented: list[tuple[str, str]] = []

    async def _handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content) if request.content else {}))
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handle))
    config = Config.model_validate({"line": {"media_dir": str(tmp_path / "media")}})

    class _StubDb:
        def increment_push(self, chat_id, yyyymm):
            incremented.append((chat_id, yyyymm))

    channel = LineChannel("tok", "secret", "OWNER_ID", config, _StubDb(), client=client)

    with channel._reply_scope("rt-asker-event", "ASKER_ID") as ctx:
        await channel.send("ASKER_ID", "hello asker")  # same chat_id as the event -> buffered
        await channel.send("OWNER_ID", "hello owner (a DIFFERENT chat_id)")  # -> pushed
    await channel._flush_reply("rt-asker-event", ctx["buffer"])
    await channel.aclose()

    reply_calls = [(p, b) for p, b in calls if p.endswith("/message/reply")]
    push_calls = [(p, b) for p, b in calls if p.endswith("/message/push")]
    assert len(reply_calls) == 1
    (_, reply_payload) = reply_calls[0]
    assert len(reply_payload["messages"]) == 1, "only the SAME-chat_id send stays in the asker's own reply"

    assert len(push_calls) == 1, "the DIFFERENT-chat_id send must go out as an immediate push"
    (_, push_payload) = push_calls[0]
    assert push_payload["to"] == "OWNER_ID"
    assert incremented == [("OWNER_ID", datetime.now().strftime("%Y-%m"))], (
        "the cross-chat push must increment push_ledger for the ACTUAL recipient (OWNER_ID), not the "
        "asker whose event triggered it"
    )


async def test_line_channel_reply_buffer_still_batches_multiple_same_chat_sends(tmp_path):
    """Fresh release-gate-round-2 probe: when the SAME user (matching the
    reply context's own owner) is sent to TWICE within one event, both
    sends must still be buffered into the ONE free reply -- not treated
    as a second, spurious cross-chat push. Confirms the fix is scoped to
    a genuine chat_id MISMATCH, not "a second send of any kind"."""
    from habit_assistant.channels.line import LineChannel

    calls: list[tuple[str, dict]] = []

    async def _handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content) if request.content else {}))
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handle))
    config = Config.model_validate({"line": {"media_dir": str(tmp_path / "media")}})

    class _StubDb:
        def increment_push(self, *a, **k):
            raise AssertionError("no push should ever occur for two same-chat_id sends in one context")

    channel = LineChannel("tok", "secret", "OWNER_ID", config, _StubDb(), client=client)

    with channel._reply_scope("rt-same-user-twice", "MEMBER_ID") as ctx:
        await channel.send("MEMBER_ID", "first message")
        await channel.send("MEMBER_ID", "second message, same user")
    await channel._flush_reply("rt-same-user-twice", ctx["buffer"])
    await channel.aclose()

    reply_calls = [(p, b) for p, b in calls if p.endswith("/message/reply")]
    push_calls = [(p, b) for p, b in calls if p.endswith("/message/push")]
    assert push_calls == [], "two same-chat_id sends must never trigger a push"
    assert len(reply_calls) == 1
    assert len(reply_calls[0][1]["messages"]) == 2, "both same-chat_id sends must land in the one batched reply"


# ===========================================================================
# 2. Digest at the scheduler level: exact boundaries, double-fire, opt-out.
# ===========================================================================


def test_digest_deferral_boundary_start_inclusive_end_exclusive(tmp_path):
    """R-C1/ARCHI RULING boundary check: a plain (non-midnight-crossing)
    quiet-hours window is `[start, end)` -- start inclusive, end
    exclusive, mirroring `core/reminders.py:_in_quiet_hours` exactly."""
    db = Database(tmp_path / "habits.db")
    db.upsert_user(MEMBER, role="member", status="active")
    db.set_user_quiet_hours(MEMBER, '[["22:00","23:00"]]')
    config = Config()
    tz = ZoneInfo("Asia/Bangkok")

    at_start = datetime(2026, 8, 30, 22, 0, 0, tzinfo=tz)
    assert _dnd_deferred_datetime(db, config, MEMBER, at_start) == datetime(2026, 8, 30, 23, 0, 0, tzinfo=tz), (
        "window start is INCLUSIVE -- exactly 22:00:00 must already be deferred"
    )

    just_before = datetime(2026, 8, 30, 21, 59, 59, tzinfo=tz)
    assert _dnd_deferred_datetime(db, config, MEMBER, just_before) is None

    at_end = datetime(2026, 8, 30, 23, 0, 0, tzinfo=tz)
    assert _dnd_deferred_datetime(db, config, MEMBER, at_end) is None, (
        "window end is EXCLUSIVE -- exactly 23:00:00 must NOT be deferred (the window has already ended)"
    )

    just_before_end = datetime(2026, 8, 30, 22, 59, 59, tzinfo=tz)
    assert _dnd_deferred_datetime(db, config, MEMBER, just_before_end) == datetime(2026, 8, 30, 23, 0, 0, tzinfo=tz)
    db.close()


def test_digest_deferral_boundary_midnight_crossing_window(tmp_path):
    """Same boundary semantics, for a window that crosses midnight --
    mirrors `_in_quiet_hours`'s own two-branch handling; the deferred
    'end' datetime must land on the correct calendar day on both sides
    of midnight."""
    db = Database(tmp_path / "habits.db")
    db.upsert_user(MEMBER, role="member", status="active")
    db.set_user_quiet_hours(MEMBER, '[["23:00","07:00"]]')
    config = Config()
    tz = ZoneInfo("Asia/Bangkok")

    at_start = datetime(2026, 8, 30, 23, 0, 0, tzinfo=tz)
    assert _dnd_deferred_datetime(db, config, MEMBER, at_start) == datetime(2026, 8, 31, 7, 0, 0, tzinfo=tz), (
        "pre-midnight side: deferred end must land on the NEXT calendar day"
    )

    at_end = datetime(2026, 8, 31, 7, 0, 0, tzinfo=tz)
    assert _dnd_deferred_datetime(db, config, MEMBER, at_end) is None, "end exclusive, post-midnight side too"

    just_before_end = datetime(2026, 8, 31, 6, 59, 59, tzinfo=tz)
    assert _dnd_deferred_datetime(db, config, MEMBER, just_before_end) == datetime(2026, 8, 31, 7, 0, 0, tzinfo=tz), (
        "post-midnight side: deferred end must land on the SAME calendar day as `now`"
    )
    db.close()


async def test_digest_immediate_path_double_fire_sends_twice_documented_risk(monkeypatch, tmp_path):
    """Confirms the documented, Archi-accepted behavior (IMPL-LINE-
    integration.md's own "Known limitations"): `_DIGEST_DEFERRED_DATES`
    covers ONLY the quiet-hours deferral path. Calling the digest job
    twice with no quiet-hours window configured (the immediate-send
    path) sends TWICE and double-increments the ledger -- protection
    against a real double-fire rests entirely on the single daily
    CronTrigger + systemd's single-instance guarantee (deploy/habit-
    assistant-line.service's own documented ruling), not on any
    code-level guard. This test exists so a future change that silently
    weakens either of those two structural protections (or silently
    adds/removes an immediate-path guard) is caught here first."""
    async with _running_line_app(monkeypatch, tmp_path) as app:
        await _post_events(app.port, [_text_event(MEMBER, "500ml", reply_token="rt-seed")])
        await _wait_until(lambda: app.api.calls_matching("/message/reply") or None)

        job = app.scheduler.get_job("daily_digest")
        await job.func()
        await job.func()

        yyyymm = datetime.now().strftime("%Y-%m")
        pushes_to_member = [b for b in app.api.calls_matching("/message/push") if b["to"] == MEMBER]
        assert len(pushes_to_member) == 2, (
            "documented/accepted behavior changed: the immediate-send path used to have no internal "
            "once-per-day dedup (TEST-LINE-C.md Finding 2). If this now sends only once, core/digest.py "
            "gained an immediate-path guard -- update IMPL-LINE-integration.md's Known Limitations and "
            "this test to match; if it still sends twice, this just re-confirms the documented posture"
        )
        assert app.db.push_count(MEMBER, yyyymm) == 2


async def test_digest_opt_out_honored_end_to_end_through_wired_app(monkeypatch, tmp_path):
    """R-C4/AC22 at the full wired-app level (module C's own tests cover
    this at the function-call level; this proves the `/digest off`
    command -> DB write -> next digest fan-out chain holds together
    through the real webhook + real scheduler-job wiring)."""
    async with _running_line_app(monkeypatch, tmp_path) as app:
        await _post_events(app.port, [_text_event(MEMBER, "/digest off", reply_token="rt-off")])
        await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-off"] or None
        )
        assert app.db.digest_opt_out(MEMBER) is True

        await _post_events(app.port, [_text_event(MEMBER, "500ml", reply_token="rt-log")])
        await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-log"] or None
        )

        job = app.scheduler.get_job("daily_digest")
        await job.func()

        yyyymm = datetime.now().strftime("%Y-%m")
        assert app.db.push_count(MEMBER, yyyymm) == 0
        assert all(b["to"] != MEMBER for b in app.api.calls_matching("/message/push"))
        assert any(b["to"] == OWNER for b in app.api.calls_matching("/message/push")), (
            "the owner (not opted out) must still receive their own digest"
        )

        await _post_events(app.port, [_text_event(MEMBER, "/digest on", reply_token="rt-on")])
        await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-on"] or None
        )
        assert app.db.digest_opt_out(MEMBER) is False


# ===========================================================================
# 3. Two-user isolation on LINE through the full wired pipeline.
# ===========================================================================


async def test_two_user_isolation_through_full_wired_pipeline(monkeypatch, tmp_path):
    async with _running_line_app(monkeypatch, tmp_path) as app:
        THIRD = "Uthirduser000000000000000000000000"
        app.db.upsert_user(THIRD, role="member", status="active")

        await _post_events(app.port, [_text_event(MEMBER, "500ml", reply_token="rt-member")])
        await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-member"] or None
        )
        await _post_events(app.port, [_text_event(THIRD, "300ml", reply_token="rt-third")])
        await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-third"] or None
        )

        today = datetime.now().date().isoformat()
        assert app.db.sum_value(MEMBER, "water", today) == 500.0
        assert app.db.sum_value(THIRD, "water", today) == 300.0
        assert app.db.sum_value(OWNER, "water", today) == 0.0

        # Word-boundary-safe number check -- both users share the SAME
        # "2500" goal literal in their own confirmation text, so a bare
        # substring check would false-positive ("500" is a substring of
        # "2500"). `(?<!\d)N(?!\d)` matches N only when NOT flanked by
        # another digit on either side.
        def _has_bare_number(text: str, n: int) -> bool:
            return re.search(rf"(?<!\d){n}(?!\d)", text) is not None

        member_reply = next(b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-member")
        third_reply = next(b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-third")
        assert not _has_bare_number(member_reply["messages"][0]["text"], 300)
        assert not _has_bare_number(third_reply["messages"][0]["text"], 500)

        await _post_events(app.port, [_text_event(MEMBER, "/history", reply_token="rt-hist-member")])
        hist_member = await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-hist-member"]
            or None
        )
        assert not _has_bare_number(hist_member[0]["messages"][0]["text"], 300), (
            "MEMBER's own /history must never show THIRD's log"
        )

        job = app.scheduler.get_job("daily_digest")
        await job.func()
        yyyymm = datetime.now().strftime("%Y-%m")
        assert app.db.push_count(MEMBER, yyyymm) == 1
        assert app.db.push_count(THIRD, yyyymm) == 1
        member_push = next(b for b in app.api.calls_matching("/message/push") if b["to"] == MEMBER)
        third_push = next(b for b in app.api.calls_matching("/message/push") if b["to"] == THIRD)
        assert not _has_bare_number(member_push["messages"][0]["text"], 300)
        assert not _has_bare_number(third_push["messages"][0]["text"], 500)


# ===========================================================================
# 4. Telegram-mode regression: a real message round trip, not just wiring.
# ===========================================================================


class _RecordingTelegramChannel:
    def __init__(self, *a, **k) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id, text, *, disable_notification: bool = False):
        self.sent.append((chat_id, text))
        return None

    async def send_actionable(self, chat_id, text, buttons):
        self.sent.append((chat_id, text))
        return None

    async def set_my_commands(self, commands, *, scope_chat_id=None):
        return None

    async def set_message_reaction(self, chat_id, message_id, emoji):
        return None

    async def run(self, on_message, on_callback=None):
        await on_message(OWNER, "500ml", "Tester", "msg-1", None)
        raise _StopAfterRun()

    async def aclose(self) -> None:
        return None


async def test_telegram_mode_real_message_round_trip_byte_identical_to_v1_10(monkeypatch, tmp_path):
    """AC28's other half, exercised at the MESSAGE level, not just
    wiring: a Telegram-mode inbound "500ml" must still produce the exact
    same water-confirmation shape v1.10.0 always did. `config.channel.
    type` defaults to "telegram" and every LINE-only gate (R-B1-B9,
    R-C1-C8) is gated on it, so none of this branch's own additions
    should be reachable here at all -- a real `OllamaClient`/
    `HealthMonitor` ARE constructed (unlike every LINE-mode test in this
    file)."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    assert config.channel.type == "telegram"
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.close()

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module,
        "load_secrets",
        lambda **kwargs: SimpleNamespace(
            telegram_bot_token="fake-token", telegram_chat_id=OWNER,
            line_channel_access_token=None, line_channel_secret=None, line_owner_user_id=None,
        ),
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", FakeScheduler)
    channel = _RecordingTelegramChannel()
    monkeypatch.setattr(main_module, "TelegramChannel", lambda *a, **k: channel)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClient)
    monkeypatch.setattr(main_module, "HealthMonitor", _FakeHealthMonitor)
    FakeScheduler.last_instance = None

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None, migrate=False, backup=False, restore=None, yes=False)
    with pytest.raises(_StopAfterRun):
        await main_module.async_main(args)

    assert len(channel.sent) == 1, "exactly one send for one inbound water log, same as v1.10.0"
    chat_id, text = channel.sent[0]
    assert chat_id == OWNER
    assert "500 ml logged" in text, f"expected the standard v1.10.0 water-confirmation phrasing, got: {text!r}"

    scheduler = FakeScheduler.last_instance
    assert scheduler.get_job("daily_digest") is None, "the digest job must never register on the Telegram path"
    assert scheduler.get_job("weekly_review") is not None
    assert scheduler.get_job("daily_summary") is not None


# ===========================================================================
# 5. Closed formal-test gap: AC1's load_secrets(channel_type="line") paths
#    (previously verified only by a throwaway ad-hoc script per IMPL-LINE-
#    shared.md's own smoke-test section -- never a committed regression
#    test protecting it going forward).
# ===========================================================================


def test_ac1_load_secrets_line_missing_var_raises_configerror_naming_it(tmp_path, monkeypatch):
    for var in ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET", "LINE_OWNER_USER_ID"):
        monkeypatch.delenv(var, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("LINE_CHANNEL_ACCESS_TOKEN=tok\nLINE_CHANNEL_SECRET=sec\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_secrets(env_path, channel_type="line")

    detail = str(excinfo.value).lower().split("copy")[0]
    assert "line_owner_user_id" in detail
    assert "line_channel_access_token" not in detail and "line_channel_secret" not in detail


def test_ac1_load_secrets_line_success_with_all_three_vars(tmp_path, monkeypatch):
    for var in ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET", "LINE_OWNER_USER_ID"):
        monkeypatch.delenv(var, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "LINE_CHANNEL_ACCESS_TOKEN=tok\nLINE_CHANNEL_SECRET=sec\nLINE_OWNER_USER_ID=Uowner123\n", encoding="utf-8"
    )

    secrets = load_secrets(env_path, channel_type="line")

    assert secrets.line_channel_access_token == "tok"
    assert secrets.line_channel_secret == "sec"
    assert secrets.line_owner_user_id == "Uowner123"


def test_ac3_push_ledger_and_opt_out_accessors_round_trip(tmp_path):
    """AC3's literal wording, as a committed regression test (previously
    verified only by IMPL-LINE-shared.md's own throwaway ad-hoc smoke
    script): `increment_push` 3x -> `push_count`==3;
    `monthly_push_total` sums across users; `set_digest_opt_out`/
    `digest_opt_out` round-trip."""
    db = Database(tmp_path / "habits.db")
    db.upsert_user(MEMBER, role="member", status="active")
    db.upsert_user(OWNER, role="owner", status="active")

    for _ in range(3):
        db.increment_push(MEMBER, "2026-09")
    assert db.push_count(MEMBER, "2026-09") == 3
    assert db.push_count(MEMBER, "2026-08") == 0, "a different month must not share the counter"

    db.increment_push(OWNER, "2026-09")
    assert db.monthly_push_total("2026-09") == 4, "monthly_push_total sums across every user"

    assert db.digest_opt_out(MEMBER) is False, "default is opt-OUT-able but opted IN (digest_opt_out=0)"
    db.set_digest_opt_out(MEMBER, True)
    assert db.digest_opt_out(MEMBER) is True
    db.set_digest_opt_out(MEMBER, False)
    assert db.digest_opt_out(MEMBER) is False
    db.close()


def test_ac1_load_secrets_telegram_default_unaffected_by_line_fields(tmp_path, monkeypatch):
    """The bare `load_secrets()` call (`channel_type` defaults
    "telegram") must stay byte-identical for every pre-LINE caller --
    the ~24 tests IMPL-LINE-integration.md's own iteration log describes
    depend on exactly this."""
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n", encoding="utf-8")

    secrets = load_secrets(env_path)

    assert secrets.telegram_bot_token == "123:abc"
    assert secrets.telegram_chat_id == "999"


# ===========================================================================
# 6. Deploy-consistency and version/release-note-posture spot checks.
# ===========================================================================


def test_deploy_consistency_webhook_port_and_callback_path_one_value_everywhere():
    default_port = Config().line.bind_port
    assert default_port == 8080

    template_text = (REPO_ROOT / "config.toml.line").read_text(encoding="utf-8")
    assert "bind_port = 8080" in template_text
    assert 'bind_host = "127.0.0.1"' in template_text

    service_text = (REPO_ROOT / "deploy" / "habit-assistant-line.service").read_text(encoding="utf-8")
    assert "8080" in service_text, "the systemd unit's own docstring/comments must reference the real default port"

    deploy_doc = (REPO_ROOT / "docs" / "DEPLOY-LINE.md").read_text(encoding="utf-8")
    assert "8080" in deploy_doc
    assert "/callback" in deploy_doc
    assert "tailscale funnel" in deploy_doc.lower()

    webhook_src = (REPO_ROOT / "src" / "habit_assistant" / "channels" / "line_webhook.py").read_text(encoding="utf-8")
    assert 'add_post("/callback"' in webhook_src
    assert 'add_get("/media/{tail:.+}"' in webhook_src


def test_version_consistent_across_files_and_release_note_posture():
    from habit_assistant import __version__
    from habit_assistant.core.release_notes import RELEASE_NOTES

    version_file = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert version_file == "1.0.0-line"
    assert __version__ == version_file, "src/habit_assistant/__init__.py:__version__ must match VERSION"
    assert re.match(r"^\d+\.\d+\.\d+-line$", __version__), (
        "SPEC-LINE.md §7's own recommended, SemVer-tolerant shape: X.Y.Z-line"
    )

    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"1.0.0-line"' in pyproject_text or "1.0.0-line" in pyproject_text

    # By design (not a gap): a brand-new product edition's first version
    # has nothing to announce an upgrade FROM, so RELEASE_NOTES carries
    # no "1.0.0-line" entry -- core/digest.py's own
    # `_pending_announcement_version` therefore never fires a phantom
    # "what's new" line on a fresh v1.0.0-line install; onboarding is
    # instead the /start welcome (core/access.py, unaffected by this
    # branch) once a user is approved.
    assert __version__ not in RELEASE_NOTES


# ===========================================================================
# 7. Direct regression pin: the reminders.py:429 self-fix.
# ===========================================================================


async def test_reminders_pause_suppression_honors_injected_clock_not_real_date(tmp_path):
    """Integration's own self-found regression fix (IMPL-LINE-
    integration.md "sanctioned extra, item 8"): `send_reminder`'s
    pause-suppression date check must read the INJECTED `clock`, not the
    real wall-clock date. Proven directly here with a pause window
    pinned to a date that is deliberately NOT today's real date, using
    an injected clock that IS that date -- this would have failed before
    the fix (the pause check used to always read `datetime.now()`)."""
    from habit_assistant.core import reminders
    from habit_assistant.core.habits import Habit

    db = Database(tmp_path / "habits.db")
    db.upsert_user(MEMBER, role="member", status="active")
    config = Config()
    habit = Habit(
        id="water", type="numeric", label_en="water", label_th="น้ำ", unit_en="ml", unit_th="มล.",
        goal=2500, reminder_times=("09:00",), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )

    fixed_date = date(2099, 1, 1)  # deliberately NOT today's real date
    db.insert_pause(MEMBER, "water", fixed_date.isoformat(), fixed_date.isoformat())

    class _FakeChannel:
        def __init__(self) -> None:
            self.sent: list[tuple[str, str]] = []

        async def send(self, chat_id, text, *, disable_notification: bool = False):
            self.sent.append((chat_id, text))
            return None

    channel = _FakeChannel()
    injected_clock = lambda: datetime(2099, 1, 1, 9, 0, 0)
    await reminders.send_reminder(
        channel, MEMBER, habit, "en", db, config, reminders.ReminderState(), clock=injected_clock
    )
    assert channel.sent == [], (
        "the pause window (pinned to the INJECTED clock's own date, 2099-01-01) must suppress this "
        "reminder even though the real wall-clock date is different -- if this fails, reminders.py:429's "
        "own self-fix regressed"
    )
    db.close()
