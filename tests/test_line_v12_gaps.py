"""Vera's adversarial gap-probe for SPEC-LINE-1.2.md (branch `line-version`,
v1.2.0) -- Archi's dispatch brief, beyond `IMPL-LINE-1.2.0.md`'s own smoke
coverage and `tests/test_line_v12_integration.py`'s e2e walkthrough:

- Quota gate boundaries: cap-1/cap/cap+1, warn at EXACTLY 80%, once-per-
  month dedup for both alerts, owner exemption, replies never gated, and
  the gate's read-error disposition -- RESOLVED fail-CLOSED by Archi
  ruling 2026-08-31 (see `test_quota_gate_fail_closed_on_monthly_push_
  total_read_error_drops_and_logs`'s own docstring; TEST-LINE-1.2.0.md's
  forensics section records the original spec-vs-dispatch-brief conflict
  this ruling settled).
- Dashboard-in-reply append precedence under overflow, quickReply
  hoisting, the false-byte-identical path, one-board-per-event, and the
  no-active-context/empty-buffer no-op guards.
- Realtime gate reachability at all five `core/jobs.py` sites plus
  `core/digest.py`'s inert-in-realtime guard, `grace_tick`'s permanently-
  suppressed send, and one real DND spot-check.
- Riders: `deploy/setup.sh` step 10's Tailscale auto-fill (all 4 fail-
  soft/success branches, exercised as ONE real bash process -- Luna's own
  "Known limitations" flagged this as only piecewise-tested on her
  Windows dev box), `send_image`'s CHANGE-ME degradation (zero prior test
  coverage), and rich-menu orphan cleanup's actual DELETE path + its
  fail-open-never-blocks-registration guarantee (the existing test only
  covers the empty-list "nothing to delete" case).

No production code is modified by this file."""

from __future__ import annotations

import json
import logging
import shutil
import struct
import subprocess
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from habit_assistant.channels.line import LineChannel
from habit_assistant.config import Config, DigestConfig, LineConfig
from habit_assistant.storage.db import Database

OWNER = "Uowner00000000000000000000000000"
MEMBER = "Umember0000000000000000000000000"
MEMBER2 = "Umembertwo000000000000000000000"


def _current_yyyymm() -> str:
    return datetime.now().strftime("%Y-%m")


def _make_channel(tmp_path, handler, *, mode: str = "digest", push_cap: int = 15000, dashboard_in_reply: bool = True):
    db = Database(tmp_path / "line.db")
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    config = Config(
        line=LineConfig(
            public_base_url="https://vps-host.tailnet.ts.net",
            media_dir=str(tmp_path / "media"),
            dashboard_in_reply=dashboard_in_reply,
        ),
        digest=DigestConfig(mode=mode, push_cap=push_cap),
    )
    channel = LineChannel("access-token", "channel-secret", OWNER, config, db, client=client)
    return channel, db


def _default_handler(captured):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/richmenu"):
            return httpx.Response(200, json={"richMenuId": "richmenu-1"})
        return httpx.Response(200, json={})

    return handler


# ===========================================================================
# AC1 -- config load/validation for the three new knobs. No prior test
# file covers this at all (test_line_integration.py only ever CONSTRUCTS a
# Config with these fields, never validates the failure path).
# ===========================================================================


def test_ac1_new_config_knobs_bind_with_documented_defaults():
    config = Config()
    assert config.line.dashboard_in_reply is True
    assert config.digest.mode == "digest"
    assert config.digest.push_cap == 15000


def test_ac1_unknown_digest_mode_string_raises_config_error(tmp_path):
    from habit_assistant.config import load_config, ConfigError

    path = tmp_path / "config.toml"
    path.write_text('[digest]\nmode = "bogus"\n', encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("bad_cap", [0, -1, -100])
def test_ac1_non_positive_push_cap_raises_config_error(tmp_path, bad_cap):
    from habit_assistant.config import load_config, ConfigError

    path = tmp_path / "config.toml"
    path.write_text(f"[digest]\npush_cap = {bad_cap}\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)


def test_ac1_valid_custom_values_bind_correctly(tmp_path):
    from habit_assistant.config import load_config

    path = tmp_path / "config.toml"
    path.write_text('[line]\ndashboard_in_reply = false\n\n[digest]\nmode = "realtime"\npush_cap = 500\n', encoding="utf-8")

    config = load_config(path)
    assert config.line.dashboard_in_reply is False
    assert config.digest.mode == "realtime"
    assert config.digest.push_cap == 500


# ===========================================================================
# Quota gate boundaries (R-Q1-R-Q8) -- direct `LineChannel._push` probing.
# ===========================================================================


async def test_quota_gate_allows_push_at_cap_minus_one(tmp_path):
    """cap-1: the running total BEFORE this push is one under the cap --
    must be allowed (this push is the one that brings the total TO cap)."""
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured), mode="realtime", push_cap=5)
    for _ in range(4):
        db.increment_push(OWNER, _current_yyyymm())  # seed total=4 (== cap-1)

    await channel._push(MEMBER, [{"type": "text", "text": "hi"}])

    push_calls = [r for r in captured if r.url.path.endswith("/message/push") and json.loads(r.content)["to"] == MEMBER]
    assert len(push_calls) == 1, "a push at exactly cap-1 must be ALLOWED"
    assert db.push_count(MEMBER, _current_yyyymm()) == 1


async def test_quota_gate_drops_push_at_cap_exactly(tmp_path):
    """cap: the running total already equals the cap -- must be dropped,
    no send, no ledger increment."""
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured), mode="realtime", push_cap=5)
    for _ in range(5):
        db.increment_push(OWNER, _current_yyyymm())  # seed total=5 (== cap)

    await channel._push(MEMBER, [{"type": "text", "text": "hi"}])

    push_calls = [r for r in captured if r.url.path.endswith("/message/push") and json.loads(r.content)["to"] == MEMBER]
    assert push_calls == [], "a push at exactly cap must be DROPPED (R-Q3)"
    assert db.push_count(MEMBER, _current_yyyymm()) == 0


async def test_quota_gate_drops_push_above_cap(tmp_path):
    """cap+1 (and beyond): once already over cap -- e.g. from the ledger's
    own overshoot while the gate's alerts themselves keep incrementing --
    every further non-owner push must still be dropped."""
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured), mode="realtime", push_cap=5)
    for _ in range(7):
        db.increment_push(OWNER, _current_yyyymm())  # seed total=7 (cap+2)

    await channel._push(MEMBER, [{"type": "text", "text": "hi"}])

    push_calls = [r for r in captured if r.url.path.endswith("/message/push") and json.loads(r.content)["to"] == MEMBER]
    assert push_calls == []
    assert db.push_count(MEMBER, _current_yyyymm()) == 0


async def test_quota_gate_warn_fires_at_exact_80_percent_threshold(tmp_path):
    """R-Q4: `int(cap * 0.8)` exactly (still < cap) -- the FIRST allowed
    non-owner push once the pre-existing total already sits at this exact
    boundary must trigger the one-time owner warn."""
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured), mode="realtime", push_cap=10)
    for _ in range(8):
        db.increment_push(OWNER, _current_yyyymm())  # seed total=8 == int(10*0.8)

    await channel._push(MEMBER, [{"type": "text", "text": "hi"}])

    owner_calls = [r for r in captured if r.url.path.endswith("/message/push") and json.loads(r.content)["to"] == OWNER]
    assert len(owner_calls) == 1, "exactly one owner push (the warn) expected at the exact 80% boundary"
    warn_text = json.loads(owner_calls[0].content)["messages"][0]["text"]
    assert "8" in warn_text and "10" in warn_text


async def test_quota_gate_warn_does_not_fire_one_below_80_percent(tmp_path):
    """One under the 80% boundary (total=7, int(10*0.8)=8) -- the allowed
    push must NOT trigger a warn."""
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured), mode="realtime", push_cap=10)
    for _ in range(7):
        db.increment_push(OWNER, _current_yyyymm())

    await channel._push(MEMBER, [{"type": "text", "text": "hi"}])

    owner_calls = [r for r in captured if r.url.path.endswith("/message/push") and json.loads(r.content)["to"] == OWNER]
    assert owner_calls == [], "no warn expected strictly below the 80% boundary"


async def test_quota_gate_warn_fires_at_most_once_per_month_across_many_crossings(tmp_path):
    """R-Q6: once `_quota_warned_months` has this `yyyymm`, every further
    allowed push that ALSO satisfies `total >= int(cap*0.8)` must not
    re-warn -- even across many separate pushes, same process instance.
    A generously large cap (100) keeps every one of the 5 pushes below
    cap itself, isolating the warn-dedup guard from the (separately
    tested) stop-alert guard -- both fire from the SAME `_push` body, so
    a small cap would trip the stop alert too and conflate the two."""
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured), mode="realtime", push_cap=100)
    for _ in range(80):
        db.increment_push(OWNER, _current_yyyymm())  # seed total=80 == int(100*0.8)

    for _ in range(5):
        await channel._push(MEMBER, [{"type": "text", "text": "hi"}])

    owner_calls = [r for r in captured if r.url.path.endswith("/message/push") and json.loads(r.content)["to"] == OWNER]
    assert len(owner_calls) == 1, f"expected exactly one owner warn across 5 crossings, got {len(owner_calls)}"


async def test_quota_gate_stop_fires_at_most_once_per_month_across_many_drops(tmp_path):
    """R-Q5/R-Q6: the owner stop alert fires on the FIRST drop this month
    and never again, even across many subsequent drops."""
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured), mode="realtime", push_cap=5)
    for _ in range(5):
        db.increment_push(OWNER, _current_yyyymm())

    for _ in range(6):
        await channel._push(MEMBER, [{"type": "text", "text": "hi"}])

    owner_calls = [r for r in captured if r.url.path.endswith("/message/push") and json.loads(r.content)["to"] == OWNER]
    assert len(owner_calls) == 1, f"expected exactly one owner stop alert across 6 drops, got {len(owner_calls)}"
    stop_text = json.loads(owner_calls[0].content)["messages"][0]["text"]
    assert "5" in stop_text  # names the cap


async def test_quota_gate_owner_always_exempt_even_far_over_cap(tmp_path):
    """R-Q3: `chat_id == self.owner_user_id` always allows, unconditionally
    -- including a total wildly over cap (20 pushes against a cap of 5)."""
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured), mode="realtime", push_cap=5)
    for _ in range(20):
        db.increment_push(MEMBER, _current_yyyymm())

    await channel._push(OWNER, [{"type": "text", "text": "owner push"}])

    owner_calls = [r for r in captured if r.url.path.endswith("/message/push") and json.loads(r.content)["to"] == OWNER]
    assert len(owner_calls) == 1
    assert db.push_count(OWNER, _current_yyyymm()) == 1


async def test_quota_gate_never_applies_to_the_reply_path(tmp_path):
    """R-Q8: `_flush_reply` is never touched by the gate -- a reply must
    succeed even with the running total already far over cap."""
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured), mode="realtime", push_cap=1)
    for _ in range(10):
        db.increment_push(OWNER, _current_yyyymm())

    with channel._reply_scope("rt") as ctx:
        await channel.send(MEMBER, "still works")
    await channel._flush_reply("rt", ctx["buffer"])

    reply_calls = [r for r in captured if r.url.path.endswith("/message/reply")]
    assert len(reply_calls) == 1
    assert json.loads(reply_calls[0].content)["messages"][0]["text"] == "still works"


async def test_quota_gate_fail_closed_on_monthly_push_total_read_error_drops_and_logs(tmp_path, caplog):
    """R-Q7/§9 OQ3 -- RESOLVED by Archi ruling 2026-08-31: fail-CLOSED,
    overriding SPEC-LINE-1.2.md's own written §4 R-Q7/§9 OQ3 text (which
    specified fail-open as the shipped default). Rationale (the ruling's
    own words): the user's requirement is "the bill can never surprise
    me" -- on a `monthly_push_total` read error, a NON-owner proactive
    push is dropped (no send, no ledger increment), logged loudly; the
    owner's own pushes and every reply are unaffected.

    This test previously pinned the fail-OPEN behavior the spec text (and
    round-1 IMPL) actually shipped -- see IMPL-LINE-1.2.0.md's iteration
    log for the honest account of that miss. Flipped here, alone, per
    Archi's explicit instruction; SPEC-LINE-1.2.md §4 R-Q7/§9 OQ3 have
    been updated in place to record this ruling."""

    class _RaisingDB:
        def __init__(self, real: Database) -> None:
            self._real = real

        def monthly_push_total(self, yyyymm: str) -> int:
            raise OSError("database is locked")

        def __getattr__(self, name):
            return getattr(self._real, name)

    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured), mode="realtime", push_cap=5)
    channel.db = _RaisingDB(db)

    with caplog.at_level(logging.ERROR, logger="habit_assistant.channels.line"):
        await channel._push(MEMBER, [{"type": "text", "text": "hi"}])

    push_calls = [r for r in captured if r.url.path.endswith("/message/push") and json.loads(r.content)["to"] == MEMBER]
    assert push_calls == [], "R-Q7 (Archi ruling 2026-08-31): a ledger-read failure must fail CLOSED (drop), never send"
    assert db.push_count(MEMBER, _current_yyyymm()) == 0, "a dropped push must never increment the ledger"
    assert any("fail" in r.message.lower() and "closed" in r.message.lower() for r in caplog.records), (
        "the fail-closed disposition must be logged loudly (ERROR), not silent"
    )


# ===========================================================================
# Dashboard-in-reply append precedence (R-A1-R-A10).
# ===========================================================================


async def test_board_dropped_first_on_overflow_confirmation_never_dropped(tmp_path):
    """R-A4: 5 ordinary sends + 1 board = 6 objects -- truncation to 5
    must drop the BOARD (the last-appended object), never a confirmation
    that was already in the buffer."""
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured))

    with channel._reply_scope("rt-overflow") as ctx:
        for i in range(5):
            await channel.send(MEMBER, f"confirmation {i}")
        await channel.append_board(MEMBER, "📊 Today board")
    await channel._flush_reply("rt-overflow", ctx["buffer"])

    reply_calls = [r for r in captured if r.url.path.endswith("/message/reply")]
    body = json.loads(reply_calls[0].content)
    texts = [m["text"] for m in body["messages"]]
    assert len(texts) == 5
    assert texts == [f"confirmation {i}" for i in range(5)], "every confirmation object must survive; the board is dropped"
    assert "Today board" not in texts


async def test_quickreply_hoisted_onto_surviving_last_object_after_board_dropped(tmp_path):
    """R-A4/R-A5: when the board is dropped by the 5-object truncation and
    the confirmation with the `undo` button is NOT the last surviving
    object (something else -- e.g. a routine's per-item confirmation --
    was appended after it), the consolidation still hoists `quickReply`
    onto whatever ended up last, exactly as it would if the board had
    survived. Truncation and consolidation are two independent steps in
    `_flush_reply` -- truncation doesn't know or care WHY the final
    object lacks a quickReply."""
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured))

    with channel._reply_scope("rt-overflow-2") as ctx:
        await channel.send(MEMBER, "filler 1")
        await channel.send(MEMBER, "filler 2")
        await channel.send(MEMBER, "filler 3")
        await channel.send_actionable(MEMBER, "confirmation with undo", [("↩︎ Undo", "undo:1")])
        await channel.send(MEMBER, "filler 5")
        await channel.append_board(MEMBER, "board text")
    await channel._flush_reply("rt-overflow-2", ctx["buffer"])

    body = json.loads([r for r in captured if r.url.path.endswith("/message/reply")][0].content)
    messages = body["messages"]
    assert len(messages) == 5
    assert messages[-1]["text"] == "filler 5"
    assert messages[-1]["quickReply"]["items"][0]["action"]["data"] == "undo:1", (
        "the undo button relocates onto whatever survives as the LAST object, even though the board (not "
        "filler 5) was the one dropped -- AC5's own wording"
    )
    undo_obj = next(m for m in messages if m["text"] == "confirmation with undo")
    assert "quickReply" not in undo_obj, "the button leaves its original object once it's no longer last"


async def test_quickreply_hoisted_onto_board_when_board_survives(tmp_path):
    """R-A5: the normal (non-overflow) case -- confirmation with a
    quickReply, then the board appended after it. LINE only renders the
    LAST object's quickReply, so it must relocate onto the board."""
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured))

    with channel._reply_scope("rt-normal") as ctx:
        await channel.send_actionable(MEMBER, "logged!", [("↩︎ Undo", "undo:99")])
        await channel.append_board(MEMBER, "board text")
    await channel._flush_reply("rt-normal", ctx["buffer"])

    body = json.loads([r for r in captured if r.url.path.endswith("/message/reply")][0].content)
    messages = body["messages"]
    assert len(messages) == 2
    assert "quickReply" not in messages[0]
    assert messages[1]["quickReply"]["items"][0]["action"]["data"] == "undo:99"


async def test_dashboard_off_reply_is_byte_identical_no_board_no_consolidation(tmp_path):
    """R-A7: with the board never appended in the first place (simulating
    `dashboard_in_reply=false` at the caller level -- `append_board` is
    simply never called), the reply is exactly the pre-1.2.0 single-object
    shape: quickReply stays on the confirmation itself."""
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured), dashboard_in_reply=False)

    with channel._reply_scope("rt-off") as ctx:
        await channel.send_actionable(MEMBER, "logged!", [("↩︎ Undo", "undo:1")])
        # dashboard_in_reply=false means core/dashboard.py:refresh never
        # calls append_board at all -- modeled here directly, since this
        # file tests channels/line.py in isolation from core/dashboard.py
        # (already covered end-to-end in test_line_v12_integration.py).
    await channel._flush_reply("rt-off", ctx["buffer"])

    body = json.loads([r for r in captured if r.url.path.endswith("/message/reply")][0].content)
    assert body["messages"] == [
        {"type": "text", "text": "logged!", "quickReply": {"items": [{"type": "action", "action": {"type": "postback", "label": "↩︎ Undo", "data": "undo:1"}}]}}
    ]


async def test_second_append_board_call_in_same_event_updates_in_place_not_duplicated(tmp_path):
    """R-A6: two `dashboard.refresh` calls within one event (e.g. an
    undo followed by a log within the same reply context) must produce
    AT MOST ONE board object, with the second call's text winning."""
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured))

    with channel._reply_scope("rt-double") as ctx:
        await channel.send(MEMBER, "confirmation")
        await channel.append_board(MEMBER, "board v1")
        await channel.append_board(MEMBER, "board v2 (updated)")
    await channel._flush_reply("rt-double", ctx["buffer"])

    body = json.loads([r for r in captured if r.url.path.endswith("/message/reply")][0].content)
    board_objs = [m for m in body["messages"] if "board" in m["text"]]
    assert len(board_objs) == 1, "at most one board object per reply (R-A6)"
    assert board_objs[0]["text"] == "board v2 (updated)"


async def test_append_board_with_no_active_reply_context_sends_nothing_never_pushes(tmp_path):
    """R-A3/AC7: a scheduled call (e.g. `dashboard_day_rollover_job`) has
    no active reply context -- `append_board` must send NOTHING, never a
    push, and never touch `push_ledger`."""
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured))

    await channel.append_board(MEMBER, "board text")  # no _reply_scope active

    assert captured == [], "append_board must never make an API call outside a reply context"
    assert db.push_count(MEMBER, _current_yyyymm()) == 0


async def test_append_board_never_appended_to_an_empty_buffer(tmp_path):
    """R-A3: "never emit a board-only reply" -- a reply context with
    nothing sent yet must not gain a board object either; the reply stays
    empty and no API call is made at all."""
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured))

    with channel._reply_scope("rt-empty") as ctx:
        await channel.append_board(MEMBER, "board text")  # nothing sent yet
    await channel._flush_reply("rt-empty", ctx["buffer"])

    assert captured == [], "a board-only reply must never be sent"


# ===========================================================================
# AC10 -- /dashboard on|off|<bare> on LINE always short-circuits to
# dashboard_line_auto, never touches state.
# ===========================================================================


@pytest.mark.parametrize("raw_command", ["/dashboard", "/dashboard on", "/dashboard off", "/dashboard bogus"])
async def test_ac10_dashboard_command_on_line_always_shortcircuits_no_write(tmp_path, raw_command):
    from habit_assistant.core import commands, dashboard, i18n
    from habit_assistant.core.habits import HabitRegistry
    from conftest import RecordingChannel

    db = Database(tmp_path / "h.db")
    db.upsert_user(MEMBER, role="member", status="active")
    config = Config.model_validate({"channel": {"type": "line"}})
    registry = HabitRegistry.from_config(config)
    channel = RecordingChannel()
    command = commands.dispatch(raw_command, registry)

    reply = await dashboard.execute_dashboard(
        command, db=db, channel=channel, config=config, registry=registry, lang="en", user_id=MEMBER,
    )

    assert reply == i18n.t("dashboard_line_auto", "en"), f"{raw_command!r} must always get the LINE auto-note, not on/off/usage text"
    assert reply != i18n.t("dashboard_unsupported", "en")
    assert db.get_dashboard_msg_id(MEMBER) is None, "no per-user state may ever be written on the LINE path (§9 OQ1)"


# ===========================================================================
# Realtime gate reachability at all 5 core/jobs.py sites + digest inert +
# grace send-suppressed-but-write-runs (R-I1/R-R1-R-R10).
# ===========================================================================


@pytest.fixture
def _line_config():
    def _make(mode: str, **extra_digest):
        return Config.model_validate(
            {"channel": {"type": "line"}, "digest": {"mode": mode, **extra_digest}}
        )

    return _make


async def test_minutely_tick_suppressed_in_digest_reachable_in_realtime(tmp_path, monkeypatch, _line_config):
    from habit_assistant.core import jobs
    from habit_assistant.core.habits import HabitRegistry
    from habit_assistant.core.reminders import ReminderState
    from habit_assistant.core.registry_provider import RegistryProvider
    from conftest import RecordingChannel

    called = {"reminders": False, "checkins": False, "nudges": False}

    async def _fake_run_due_reminders(*a, **k):
        called["reminders"] = True

    async def _fake_run_due_checkins(*a, **k):
        called["checkins"] = True

    async def _fake_run_due_nudges(*a, **k):
        called["nudges"] = True

    monkeypatch.setattr(jobs.checkins, "run_due_checkins", _fake_run_due_checkins)
    monkeypatch.setattr(jobs.nudge, "run_due_nudges", _fake_run_due_nudges)

    db = Database(tmp_path / "h.db")
    db.upsert_user(OWNER, role="owner", status="active")
    config = _line_config("digest")
    registry = HabitRegistry.from_config(config)
    provider = RegistryProvider(config, db)
    channel = RecordingChannel()

    await jobs.minutely_tick(channel, config, registry, db, ReminderState(), provider, run_due_reminders=_fake_run_due_reminders)
    assert called == {"reminders": False, "checkins": False, "nudges": False}, "digest mode: the whole tick must no-op on LINE (R-C2)"

    config_rt = _line_config("realtime")
    await jobs.minutely_tick(channel, config_rt, registry, db, ReminderState(), provider, run_due_reminders=_fake_run_due_reminders)
    assert called == {"reminders": True, "checkins": True, "nudges": True}, "realtime mode: all three must be reachable (R-R1/R-R2/R-R3)"


async def test_weekly_review_job_suppressed_in_digest_reachable_in_realtime(tmp_path, monkeypatch, _line_config):
    from habit_assistant.core import jobs

    calls = []

    def _fake_render(*a, **k):  # render_weekly_review_charts is called SYNCHRONOUSLY (jobs.py:150), not awaited
        return []

    class _FakeProvider:
        def for_user(self, user_id):
            from habit_assistant.core.habits import HabitRegistry
            return HabitRegistry.from_config(Config())

    async def _fake_run_weekly_review(*a, **k):
        calls.append(1)
        return "review text"

    monkeypatch.setattr(jobs, "run_weekly_review", _fake_run_weekly_review)

    db = Database(tmp_path / "h.db")
    db.upsert_user(MEMBER, role="member", status="active")
    from habit_assistant.storage.models import LogEntry
    db.insert_log(LogEntry(None, MEMBER, datetime.now().isoformat(timespec="seconds"), "water", 500.0, None, "500ml", "reply"))

    from conftest import RecordingChannel
    channel = RecordingChannel()

    config = _line_config("digest")
    await jobs.weekly_review_job(db, channel, config, _FakeProvider(), llm=None, render_weekly_review_charts=_fake_render)
    assert calls == [], "digest mode: weekly review must be suppressed on LINE (R-C2)"

    config_rt = _line_config("realtime")
    await jobs.weekly_review_job(db, channel, config_rt, _FakeProvider(), llm=None, render_weekly_review_charts=_fake_render)
    assert calls == [1], "realtime mode: weekly review must be reachable (R-R5)"
    assert channel.sent, "the review text must actually have been sent"


async def test_daily_summary_job_suppressed_in_digest_reachable_in_realtime(tmp_path, _line_config):
    from habit_assistant.core import jobs
    from habit_assistant.core.registry_provider import RegistryProvider
    from habit_assistant.storage.models import LogEntry
    from conftest import RecordingChannel

    db = Database(tmp_path / "h.db")
    db.upsert_user(MEMBER, role="member", status="active")
    db.insert_log(LogEntry(None, MEMBER, datetime.now().isoformat(timespec="seconds"), "water", 500.0, None, "500ml", "reply"))
    channel = RecordingChannel()

    config = _line_config("digest")
    provider = RegistryProvider(config, db)
    await jobs.daily_summary_job(db, channel, config, provider)
    assert channel.sent == [], "digest mode: daily summary must be suppressed on LINE (R-C2)"

    config_rt = _line_config("realtime")
    provider_rt = RegistryProvider(config_rt, db)
    await jobs.daily_summary_job(db, channel, config_rt, provider_rt)
    assert channel.sent != [], "realtime mode: daily summary must be reachable (R-R4)"


async def test_wrapped_auto_job_suppressed_in_digest_reachable_in_realtime(tmp_path, _line_config):
    from habit_assistant.core import jobs
    from habit_assistant.core.registry_provider import RegistryProvider
    from habit_assistant.storage.models import LogEntry
    from conftest import RecordingChannel

    db = Database(tmp_path / "h.db")
    db.upsert_user(MEMBER, role="member", status="active")
    # Seed enough of the current month's data that execute_wrapped has
    # something non-empty to report (an empty reply short-circuits before
    # channel.send, which would produce a false negative either way).
    now = datetime.now()
    db.insert_log(LogEntry(None, MEMBER, now.isoformat(timespec="seconds"), "water", 500.0, None, "500ml", "reply"))
    channel = RecordingChannel()

    config = Config.model_validate({"channel": {"type": "line"}, "digest": {"mode": "digest"}, "wrapped": {"auto_send": True}})
    provider = RegistryProvider(config, db)
    await jobs.wrapped_auto_job(db, channel, config, provider)
    assert channel.sent == [], "digest mode: wrapped auto-send must be suppressed on LINE regardless of auto_send (R-C2)"

    config_rt = Config.model_validate(
        {"channel": {"type": "line"}, "digest": {"mode": "realtime"}, "wrapped": {"auto_send": True}}
    )
    provider_rt = RegistryProvider(config_rt, db)
    await jobs.wrapped_auto_job(db, channel, config_rt, provider_rt)
    assert channel.sent != [], "realtime mode + auto_send=true: wrapped auto-send must be reachable (R-R9)"


async def test_wrapped_auto_job_stays_suppressed_in_realtime_when_auto_send_is_false(tmp_path):
    """R-R9: realtime alone doesn't turn auto-send on -- the default
    `auto_send=false` must still suppress this job even in realtime."""
    from habit_assistant.core import jobs
    from habit_assistant.core.registry_provider import RegistryProvider
    from habit_assistant.storage.models import LogEntry
    from conftest import RecordingChannel

    db = Database(tmp_path / "h.db")
    db.upsert_user(MEMBER, role="member", status="active")
    db.insert_log(LogEntry(None, MEMBER, datetime.now().isoformat(timespec="seconds"), "water", 500.0, None, "500ml", "reply"))
    channel = RecordingChannel()

    config_rt = Config.model_validate({"channel": {"type": "line"}, "digest": {"mode": "realtime"}})  # auto_send default False
    provider = RegistryProvider(config_rt, db)
    await jobs.wrapped_auto_job(db, channel, config_rt, provider)
    assert channel.sent == [], "realtime mode alone must NOT enable wrapped auto-send (still gated by config.wrapped.auto_send)"


async def test_grace_tick_send_stays_suppressed_in_realtime_but_write_still_runs(tmp_path):
    """R-R8: the ONE exception to "every gate flips on realtime" --
    `evaluate_grace`'s write must happen unconditionally, but the send
    must stay suppressed even in realtime mode. Uses a REAL grace-eligible
    seed (mirrors the mechanism verified in the Monday-flake forensics)."""
    from datetime import timedelta
    from habit_assistant.core import jobs, grace
    from habit_assistant.core.habits import HabitRegistry
    from habit_assistant.core.registry_provider import RegistryProvider
    from habit_assistant.storage.models import LogEntry
    from conftest import RecordingChannel

    # A Tuesday-safe "today" avoids the documented ISO-week-boundary
    # Monday artifact entirely (see TEST-LINE-1.2.0.md's mojibake/flake
    # forensics section) -- pick a date that is provably NOT a Monday.
    today = datetime.now().date()
    while today.isoweekday() == 1:
        today += timedelta(days=1)

    db = Database(tmp_path / "h.db")
    db.upsert_user(MEMBER, role="member", status="active")
    for offset in range(2, 7):
        day = today - timedelta(days=offset)
        db.insert_log(LogEntry(None, MEMBER, f"{day.isoformat()}T09:00:00", "diary", None, None, "entry", "reply"))

    config_rt = Config.model_validate({"channel": {"type": "line"}, "digest": {"mode": "realtime"}, "app": {"timezone": "UTC"}})
    provider = RegistryProvider(config_rt, db)
    channel = RecordingChannel()

    # Freeze grace_tick's own `date.today()` read to our chosen date.
    import habit_assistant.core.jobs as jobs_module

    class _FixedDate(type(today)):
        @classmethod
        def today(cls):
            return today

    real_date = jobs_module.date
    jobs_module.date = _FixedDate
    try:
        await jobs.grace_tick(db, channel, config_rt, provider)
    finally:
        jobs_module.date = real_date

    assert channel.sent == [], "R-R8: the grace send must stay suppressed even in realtime mode"
    yesterday = (today - timedelta(days=1)).isoformat()
    protected = db.grace_protected_dates(MEMBER, "diary", yesterday, yesterday)
    assert yesterday in protected, "the evaluate_grace WRITE must still run unconditionally, even though the send is suppressed"


async def test_run_daily_digest_is_inert_in_realtime_no_read_no_send(tmp_path):
    """R-I2/R-R10/AC16: `mode=="realtime"` must early-return BEFORE any
    DB read at all -- proven with a DB stub that raises on
    `active_user_ids()`, not just by asserting zero sends."""
    from habit_assistant.core.digest import run_daily_digest
    from habit_assistant.core.registry_provider import RegistryProvider
    from conftest import RecordingChannel

    class _RaisingOnAnyReadDB:
        def active_user_ids(self):
            raise AssertionError("run_daily_digest must not read the DB at all in realtime mode (R-R10)")

    config_rt = Config.model_validate({"channel": {"type": "line"}, "digest": {"mode": "realtime", "enabled": True}})
    channel = RecordingChannel()
    provider = RegistryProvider(config_rt, _RaisingOnAnyReadDB())

    await run_daily_digest(_RaisingOnAnyReadDB(), channel, config_rt, provider)  # must not raise

    assert channel.sent == []


async def test_realtime_reminder_respects_dnd_no_push_inside_quiet_hours(tmp_path):
    """R-R7: a spot-check that realtime reachability doesn't bypass the
    existing per-user DND gate each sender already applies -- DND
    mechanics themselves are `core/reminders.py`'s own, unchanged, and
    fully covered by its own test suite; this proves the NEW realtime
    reachability doesn't accidentally skip that existing check."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from habit_assistant.core import jobs
    from habit_assistant.core.habits import HabitRegistry
    from habit_assistant.core.reminders import ReminderState, run_due_reminders
    from habit_assistant.core.registry_provider import RegistryProvider
    from conftest import RecordingChannel

    # Default app timezone is Asia/Bangkok (config.py) -- both the
    # reminder-due check and the DND window must be computed in THAT
    # zone, mirroring test_line_integration.py's own DND-deferral test.
    now = datetime.now(ZoneInfo("Asia/Bangkok"))
    now_hhmm = now.strftime("%H:%M")
    window_start = (now - timedelta(minutes=5)).strftime("%H:%M")
    window_end = (now + timedelta(minutes=5)).strftime("%H:%M")

    db = Database(tmp_path / "h.db")
    db.upsert_user(MEMBER, role="member", status="active")
    db.set_reminder_times(MEMBER, "water", [now_hhmm])
    db.set_user_quiet_hours(MEMBER, f'[["{window_start}","{window_end}"]]')

    config_rt = Config.model_validate({"channel": {"type": "line"}, "digest": {"mode": "realtime"}})
    registry = HabitRegistry.from_config(config_rt)
    provider = RegistryProvider(config_rt, db)
    channel = RecordingChannel()

    await jobs.minutely_tick(
        channel, config_rt, registry, db, ReminderState(), provider, run_due_reminders=run_due_reminders
    )

    assert channel.sent == [], "R-R7: a user inside their own quiet-hours window must get no realtime push"


# ===========================================================================
# Riders: deploy/setup.sh step 10 (Tailscale public_base_url auto-fill).
# ===========================================================================

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = REPO_ROOT / "deploy"


def _find_real_bash() -> str | None:
    candidates = [r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe", shutil.which("bash")]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            result = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=10)
        except OSError:
            continue
        if result.returncode == 0 and "bash" in result.stdout.lower():
            return candidate
    return None


_REAL_BASH = _find_real_bash()


def _extract_step_10() -> str:
    text = (DEPLOY_DIR / "setup.sh").read_text(encoding="utf-8")
    start = text.index("# --- 10.")
    return text[start:]


def _run_step_10(repo_root: Path, *, extra_path_dir: Path | None = None) -> str:
    script_path = repo_root / "_step10.sh"
    script = "#!/usr/bin/env bash\nset -euo pipefail\nREPO_ROOT=\"$1\"\nlog() { echo \"[setup.sh] $*\"; }\n" + _extract_step_10()
    script_path.write_text(script, encoding="utf-8", newline="\n")
    env = None
    if extra_path_dir is not None:
        import os

        env = dict(os.environ)
        env["PATH"] = f"{extra_path_dir}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run([_REAL_BASH, str(script_path), str(repo_root)], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _write_fake_bin(tmp_path: Path, *, dns_name: str | None, python_available: bool = True) -> Path:
    """A throwaway `$PATH` directory carrying a fake `tailscale` (and,
    optionally, a `python3` wrapper around the REAL interpreter this suite
    itself runs under) -- lets step 10's full pipe (`command -v` +
    `tailscale status --json` + a real JSON parse) be exercised as ONE
    process, closing the gap Luna's own IMPL-LINE-1.2.0.md flagged
    ("the full three-part pipe was not exercised as one process on this
    box")."""
    import sys

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)

    if dns_name is not None:
        payload = json.dumps({"Self": {"DNSName": dns_name}})
    else:
        payload = json.dumps({"Self": {}})  # present but no usable DNSName
    tailscale_script = bin_dir / "tailscale"
    tailscale_script.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "status" ] && [ "$2" = "--json" ]; then\n'
        f"  echo '{payload}'\n"
        "fi\n",
        encoding="utf-8",
        newline="\n",
    )
    tailscale_script.chmod(0o755)

    if python_available:
        real_py = sys.executable.replace("\\", "/")
        if real_py[1:3] == ":/":
            real_py = "/" + real_py[0].lower() + real_py[2:]
        py_script = bin_dir / "python3"
        py_script.write_text(f'#!/usr/bin/env bash\nexec "{real_py}" "$@"\n', encoding="utf-8", newline="\n")
        py_script.chmod(0o755)

    return bin_dir


@pytest.mark.skipif(_REAL_BASH is None, reason="no functional bash found (Windows box without Git Bash/WSL)")
def test_setup_step10_already_configured_leaves_config_untouched(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[line]\npublic_base_url = "https://real-host.tailnet.ts.net"\n', encoding="utf-8", newline="\n")

    stdout = _run_step_10(tmp_path)

    assert config_path.read_text(encoding="utf-8") == '[line]\npublic_base_url = "https://real-host.tailnet.ts.net"\n'
    assert "already configured" in stdout
    assert "leaving config.toml untouched" in stdout


@pytest.mark.skipif(_REAL_BASH is None, reason="no functional bash found (Windows box without Git Bash/WSL)")
def test_setup_step10_success_autofills_from_real_tailscale_json_pipe(tmp_path):
    """The full 3-part pipe (`command -v tailscale` -> `tailscale status
    --json` -> python JSON parse -> `sed -i`) as ONE real bash process --
    the exact gap Luna's own IMPL flagged as unexercised on her box."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[line]\npublic_base_url = "https://CHANGE-ME.example.ts.net"\n', encoding="utf-8", newline="\n")
    bin_dir = _write_fake_bin(tmp_path, dns_name="my-real-vps.tailxxxx.ts.net.")

    stdout = _run_step_10(tmp_path, extra_path_dir=bin_dir)

    new_content = config_path.read_text(encoding="utf-8")
    assert 'public_base_url = "https://my-real-vps.tailxxxx.ts.net"' in new_content, new_content
    assert "CHANGE-ME" not in new_content
    assert "Auto-filled" in stdout


@pytest.mark.skipif(_REAL_BASH is None, reason="no functional bash found (Windows box without Git Bash/WSL)")
def test_setup_step10_no_tailscale_on_path_leaves_placeholder_and_warns_loudly(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[line]\npublic_base_url = "https://CHANGE-ME.example.ts.net"\n', encoding="utf-8", newline="\n")

    # Deliberately no fake `tailscale` on PATH, and this dev box has none
    # installed either (Luna's own IMPL note confirms this for the target
    # environment) -- exercises the real "command -v tailscale" miss.
    stdout = _run_step_10(tmp_path)

    assert "CHANGE-ME" in config_path.read_text(encoding="utf-8"), "the placeholder must be left exactly as-is"
    assert "WARNING" in stdout
    assert "STILL the CHANGE-ME placeholder" in stdout


@pytest.mark.skipif(_REAL_BASH is None, reason="no functional bash found (Windows box without Git Bash/WSL)")
def test_setup_step10_tailscale_present_but_no_usable_dns_name_leaves_placeholder(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[line]\npublic_base_url = "https://CHANGE-ME.example.ts.net"\n', encoding="utf-8", newline="\n")
    bin_dir = _write_fake_bin(tmp_path, dns_name=None)

    stdout = _run_step_10(tmp_path, extra_path_dir=bin_dir)

    assert "CHANGE-ME" in config_path.read_text(encoding="utf-8")
    assert "WARNING" in stdout
    assert "didn't return a usable DNS name" in stdout


@pytest.mark.skipif(_REAL_BASH is None, reason="no functional bash found (Windows box without Git Bash/WSL)")
def test_setup_step10_no_config_toml_at_all_is_a_silent_no_op(tmp_path):
    """No `config.toml` present at all (e.g. step 6 itself failed
    upstream) -- step 10's own `[ -f "$REPO_ROOT/config.toml" ]` guard
    must make this a clean no-op, not a crash (the script runs under
    `set -euo pipefail`, so any unguarded read here would abort setup.sh
    entirely)."""
    stdout = _run_step_10(tmp_path)  # no config.toml written at all
    assert "already configured" in stdout  # the `else` branch's own wording


# ===========================================================================
# Riders: send_image CHANGE-ME degradation (zero prior test coverage).
# ===========================================================================


async def test_send_image_degrades_to_text_when_public_base_url_still_change_me(tmp_path, caplog):
    """Archi rider (2026-08-31 live incident): a public_base_url still
    carrying the CHANGE-ME placeholder must degrade `send_image` to a
    text-only reply (caption + an honest note) -- never emit an `image`
    message object pointing at an unfetchable URL."""
    captured: list[httpx.Request] = []
    db = Database(tmp_path / "line.db")
    transport = httpx.MockTransport(_default_handler(captured))
    client = httpx.AsyncClient(transport=transport)
    config = Config(line=LineConfig(public_base_url="https://CHANGE-ME.example.ts.net", media_dir=str(tmp_path / "media")))
    channel = LineChannel("tok", "secret", OWNER, config, db, client=client)

    with channel._reply_scope("rt") as ctx:
        with caplog.at_level(logging.ERROR, logger="habit_assistant.channels.line"):
            await channel.send_image(MEMBER, b"\x89PNG\r\n\x1a\nfake", "your heatmap")
    await channel._flush_reply("rt", ctx["buffer"])

    body = json.loads([r for r in captured if r.url.path.endswith("/message/reply")][0].content)
    messages = body["messages"]
    assert all(m["type"] == "text" for m in messages), "no image object may ever be emitted with an unconfigured public_base_url"
    assert messages[0]["text"] == "your heatmap"
    assert any("public" in m["text"].lower() or "url" in m["text"].lower() or "🖼️" in m["text"] for m in messages[1:])
    media_dir = tmp_path / "media"
    written_pngs = list(media_dir.glob("*.png")) if media_dir.exists() else []
    assert written_pngs == [], "no PNG should even be written to disk on the degraded path"
    assert any("CHANGE-ME" in r.message or "unconfigured" in r.message for r in caplog.records)


async def test_send_image_still_sends_real_image_when_public_base_url_is_configured(tmp_path):
    """Regression control for the rider above -- a REAL, non-placeholder
    public_base_url must still produce the normal caption+image pair,
    proving the CHANGE-ME check doesn't over-fire."""
    captured: list[httpx.Request] = []
    channel, db = None, None
    db = Database(tmp_path / "line.db")
    transport = httpx.MockTransport(_default_handler(captured))
    client = httpx.AsyncClient(transport=transport)
    config = Config(line=LineConfig(public_base_url="https://real-vps.tailnet.ts.net", media_dir=str(tmp_path / "media")))
    channel = LineChannel("tok", "secret", OWNER, config, db, client=client)

    with channel._reply_scope("rt") as ctx:
        await channel.send_image(MEMBER, b"\x89PNG\r\n\x1a\nfake", "your heatmap")
    await channel._flush_reply("rt", ctx["buffer"])

    body = json.loads([r for r in captured if r.url.path.endswith("/message/reply")][0].content)
    messages = body["messages"]
    assert messages[0] == {"type": "text", "text": "your heatmap"}
    assert messages[1]["type"] == "image"
    assert messages[1]["originalContentUrl"].startswith("https://real-vps.tailnet.ts.net/media/")


# ===========================================================================
# Riders: rich-menu orphan cleanup -- actual DELETE path + fail-open.
# ===========================================================================


async def test_register_rich_menu_deletes_every_existing_orphan_before_creating_a_new_one(tmp_path):
    """Archi rider (2026-08-31): 3 pre-existing rich menus on LINE's own
    side must ALL be deleted (one DELETE call per orphan) before the
    fresh create/upload/set-default sequence -- the existing test suite
    only covers the "empty list, nothing to delete" case."""
    captured: list[httpx.Request] = []
    existing = {"richmenus": [{"richMenuId": "orphan-1"}, {"richMenuId": "orphan-2"}, {"richMenuId": "orphan-3"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/v2/bot/richmenu/list":
            return httpx.Response(200, json=existing)
        if request.url.path.endswith("/richmenu"):
            return httpx.Response(200, json={"richMenuId": "new-menu"})
        return httpx.Response(200, json={})

    image_path = tmp_path / "richmenu.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfakepng")
    db = Database(tmp_path / "line.db")
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    config = Config(line=LineConfig(public_base_url="https://x.ts.net", media_dir=str(tmp_path / "media"), rich_menu_image=str(image_path)))
    channel = LineChannel("tok", "secret", OWNER, config, db, client=client)

    await channel.register_rich_menu()

    delete_calls = [r for r in captured if r.method == "DELETE"]
    deleted_ids = {r.url.path.rsplit("/", 1)[-1] for r in delete_calls}
    assert deleted_ids == {"orphan-1", "orphan-2", "orphan-3"}, f"every orphan must be deleted, got {deleted_ids}"
    # The create/upload/set-default sequence must still run afterward.
    create_calls = [r for r in captured if r.url.path == "/v2/bot/richmenu" and r.method == "POST"]
    assert len(create_calls) == 1


async def test_register_rich_menu_cleanup_failure_never_blocks_registration(tmp_path, caplog):
    """Fail-open: a DELETE failure for one stale menu must not prevent
    the fresh menu from still being created+uploaded+set-default."""
    captured: list[httpx.Request] = []
    existing = {"richmenus": [{"richMenuId": "orphan-fails"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/v2/bot/richmenu/list":
            return httpx.Response(200, json=existing)
        if request.method == "DELETE":
            return httpx.Response(500, json={"message": "internal error"})
        if request.url.path.endswith("/richmenu") and request.method == "POST":
            return httpx.Response(200, json={"richMenuId": "new-menu"})
        return httpx.Response(200, json={})

    image_path = tmp_path / "richmenu.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfakepng")
    db = Database(tmp_path / "line.db")
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    config = Config(line=LineConfig(public_base_url="https://x.ts.net", media_dir=str(tmp_path / "media"), rich_menu_image=str(image_path)))
    channel = LineChannel("tok", "secret", OWNER, config, db, client=client)

    with caplog.at_level(logging.ERROR, logger="habit_assistant.channels.line"):
        await channel.register_rich_menu()  # must not raise

    create_calls = [r for r in captured if r.url.path == "/v2/bot/richmenu" and r.method == "POST"]
    assert len(create_calls) == 1, "a cleanup DELETE failure must never block the fresh registration that follows it"
    set_default_calls = [r for r in captured if "/user/all/richmenu/" in r.url.path]
    assert len(set_default_calls) == 1


async def test_register_rich_menu_cleanup_list_failure_never_blocks_registration(tmp_path, caplog):
    """Fail-open at the LISTING step itself (not just an individual
    delete) -- a 500 on `GET /v2/bot/richmenu/list` must still let the
    fresh create/upload/set-default sequence proceed."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/v2/bot/richmenu/list":
            return httpx.Response(500, json={"message": "internal error"})
        if request.url.path.endswith("/richmenu") and request.method == "POST":
            return httpx.Response(200, json={"richMenuId": "new-menu"})
        return httpx.Response(200, json={})

    image_path = tmp_path / "richmenu.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfakepng")
    db = Database(tmp_path / "line.db")
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    config = Config(line=LineConfig(public_base_url="https://x.ts.net", media_dir=str(tmp_path / "media"), rich_menu_image=str(image_path)))
    channel = LineChannel("tok", "secret", OWNER, config, db, client=client)

    with caplog.at_level(logging.ERROR, logger="habit_assistant.channels.line"):
        await channel.register_rich_menu()  # must not raise

    create_calls = [r for r in captured if r.url.path == "/v2/bot/richmenu" and r.method == "POST"]
    assert len(create_calls) == 1
