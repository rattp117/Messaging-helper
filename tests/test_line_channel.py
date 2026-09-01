"""SPEC-LINE.md §4 Module A -- `channels/line.py`'s own tests (AC7-AC9,
AC11-AC14): reply aggregation vs push (the free/quota-counted split), the
quick-reply mapping and its LINE-imposed caps, the send_image media-token
path, and the base-default degradations. `channels/line_webhook.py`'s own
server surface (signature verify, /callback fast-200+FIFO, /media serving,
TTL cleanup) is covered separately in tests/test_line_webhook.py.

None of these tests make a real network call -- every LINE API call goes
through an httpx.MockTransport, same convention as
tests/test_channels.py's own TelegramChannel tests."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.channels.line import LineChannel, _default_rich_menu_payload
from habit_assistant.config import Config, DigestConfig, LineConfig
from habit_assistant.storage.db import Database


def _current_yyyymm() -> str:
    return datetime.now().strftime("%Y-%m")


def _make_channel(
    tmp_path,
    handler,
    *,
    rich_menu_image=None,
    media_ttl_seconds=3600,
    mode: str = "digest",
    push_cap: int = 15000,
    clock=None,
):
    db = Database(tmp_path / "line.db")
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    kwargs = {"public_base_url": "https://vps-host.tailnet.ts.net", "media_dir": str(tmp_path / "media")}
    kwargs["media_ttl_seconds"] = media_ttl_seconds
    if rich_menu_image is not None:
        kwargs["rich_menu_image"] = str(rich_menu_image)
    config = Config(line=LineConfig(**kwargs), digest=DigestConfig(mode=mode, push_cap=push_cap))
    channel_kwargs = {"client": client}
    if clock is not None:
        channel_kwargs["clock"] = clock
    channel = LineChannel("access-token", "channel-secret", "Uowner", config, db, **channel_kwargs)
    return channel, db


def _default_handler(captured):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/richmenu"):
            return httpx.Response(200, json={"richMenuId": "richmenu-1"})
        return httpx.Response(200, json={})

    return handler


# ---------------------------------------------------------------------------
# ABC conformance + construction
# ---------------------------------------------------------------------------


def test_line_channel_is_a_channel(tmp_path):
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))
    assert isinstance(channel, Channel)


def test_construction_creates_media_dir(tmp_path):
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))
    assert (tmp_path / "media").is_dir()


# ---------------------------------------------------------------------------
# AC7 -- reply aggregation: one event, two sends -> one reply call, no push
# ---------------------------------------------------------------------------


async def test_two_sends_in_one_reply_context_batch_into_one_reply_call(tmp_path):
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured))

    with channel._reply_scope("reply-token-1") as ctx:
        await channel.send("U1", "part one")
        await channel.send("U1", "part two")
    await channel._flush_reply("reply-token-1", ctx["buffer"])

    reply_calls = [r for r in captured if r.url.path.endswith("/message/reply")]
    push_calls = [r for r in captured if r.url.path.endswith("/message/push")]
    assert len(reply_calls) == 1
    assert push_calls == []

    body = json.loads(reply_calls[0].content)
    assert body["replyToken"] == "reply-token-1"
    assert [m["text"] for m in body["messages"]] == ["part one", "part two"]
    assert db.push_count("U1", _current_yyyymm()) == 0


async def test_reply_call_carries_the_bearer_token(tmp_path):
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))

    with channel._reply_scope("rt") as ctx:
        await channel.send("U1", "hi")
    await channel._flush_reply("rt", ctx["buffer"])

    assert captured[0].headers["Authorization"] == "Bearer access-token"


async def test_empty_reply_buffer_makes_no_api_call(tmp_path):
    """A handler that calls neither send/send_actionable/send_image leaves
    the buffer empty -- nothing to reply with, so no call at all (an empty
    `messages` array is invalid on LINE's own API)."""
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))

    with channel._reply_scope("rt-empty") as ctx:
        pass
    await channel._flush_reply("rt-empty", ctx["buffer"])

    assert captured == []


async def test_more_than_five_reply_objects_dropped_with_warning(tmp_path, caplog):
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))

    with channel._reply_scope("rt-overflow") as ctx:
        for i in range(7):
            await channel.send("U1", f"line {i}")
    with caplog.at_level(logging.WARNING, logger="habit_assistant.channels.line"):
        await channel._flush_reply("rt-overflow", ctx["buffer"])

    reply_calls = [r for r in captured if r.url.path.endswith("/message/reply")]
    assert len(reply_calls) == 1
    body = json.loads(reply_calls[0].content)
    assert len(body["messages"]) == 5
    assert [m["text"] for m in body["messages"]] == [f"line {i}" for i in range(5)]
    assert any("dropping the overflow" in r.message for r in caplog.records)


async def test_reply_rejected_by_line_is_dropped_never_falls_back_to_push(tmp_path, caplog):
    """R-A5: an expired/already-used replyToken must be logged and dropped
    -- never silently converted to a push (that would spend quota for
    output the user never got, and double-charge if they re-send)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/message/reply"):
            return httpx.Response(400, json={"message": "Invalid reply token"})
        return httpx.Response(200, json={})

    channel, db = _make_channel(tmp_path, handler)

    with channel._reply_scope("expired-token") as ctx:
        await channel.send("U1", "too late")
    with caplog.at_level(logging.WARNING, logger="habit_assistant.channels.line"):
        await channel._flush_reply("expired-token", ctx["buffer"])  # must not raise

    assert db.push_count("U1", _current_yyyymm()) == 0
    assert any("expired" in r.message.lower() or "dropping" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# AC8 -- push when no reply context, ledger increment
# ---------------------------------------------------------------------------


async def test_send_with_no_active_context_pushes_and_increments_ledger(tmp_path):
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured))

    result = await channel.send("U2", "your daily digest")

    push_calls = [r for r in captured if r.url.path.endswith("/message/push")]
    reply_calls = [r for r in captured if r.url.path.endswith("/message/reply")]
    assert len(push_calls) == 1
    assert reply_calls == []
    body = json.loads(push_calls[0].content)
    assert body == {"to": "U2", "messages": [{"type": "text", "text": "your daily digest"}]}
    assert db.push_count("U2", _current_yyyymm()) == 1
    # TEST-PORTAL-users.md Finding 1 fix (integration item 4): LINE still
    # has no REAL per-message id contract (R-A6's own historical note),
    # but `send()` now returns a non-None confirmation sentinel on an
    # actual successful push, so a caller (core/access.py:approve_user)
    # can distinguish "confirmed sent" from "silently dropped by the
    # realtime quota gate" (both used to return None indistinguishably).
    assert result is not None


async def test_three_pushes_increment_ledger_three_times(tmp_path):
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured))

    await channel.send("U3", "a")
    await channel.send("U3", "b")
    await channel.send("U3", "c")

    assert db.push_count("U3", _current_yyyymm()) == 3
    assert db.monthly_push_total(_current_yyyymm()) == 3


async def test_failed_push_does_not_increment_ledger(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "server error"})

    channel, db = _make_channel(tmp_path, handler)

    with pytest.raises(httpx.HTTPStatusError):
        await channel.send("U4", "boom")

    assert db.push_count("U4", _current_yyyymm()) == 0


async def test_disable_notification_is_accepted_but_ignored(tmp_path):
    """§ degradation table: no LINE equivalent -- accepted for ABC shape
    conformance, silently ignored (not forwarded into the payload)."""
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))

    await channel.send("U1", "hush", disable_notification=True)

    body = json.loads(captured[0].content)
    assert "disable_notification" not in body
    assert body == {"to": "U1", "messages": [{"type": "text", "text": "hush"}]}


# ---------------------------------------------------------------------------
# AC9 -- quick replies: mapping, 13-item cap, verbatim data, 300-char guard
# ---------------------------------------------------------------------------


async def test_send_actionable_maps_buttons_to_quick_reply_postback_items(tmp_path):
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))

    with channel._reply_scope("rt") as ctx:
        await channel.send_actionable("U1", "pick one", [("↩︎ Undo", "undo:98765"), ("Log 500ml", "log:water:500")])
    await channel._flush_reply("rt", ctx["buffer"])

    body = json.loads(captured[0].content)
    message = body["messages"][0]
    assert message["type"] == "text"
    assert message["text"] == "pick one"
    assert message["quickReply"]["items"] == [
        {"type": "action", "action": {"type": "postback", "label": "↩︎ Undo", "data": "undo:98765"}},
        {"type": "action", "action": {"type": "postback", "label": "Log 500ml", "data": "log:water:500"}},
    ]


async def test_send_actionable_more_than_13_buttons_truncated_with_warning(tmp_path, caplog):
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))
    buttons = [(f"label{i}", f"log:{i}") for i in range(20)]

    with channel._reply_scope("rt") as ctx:
        with caplog.at_level(logging.WARNING, logger="habit_assistant.channels.line"):
            await channel.send_actionable("U1", "many", buttons)
    await channel._flush_reply("rt", ctx["buffer"])

    body = json.loads(captured[0].content)
    items = body["messages"][0]["quickReply"]["items"]
    assert len(items) == 13
    assert [item["action"]["data"] for item in items] == [f"log:{i}" for i in range(13)]
    assert any("truncating" in r.message for r in caplog.records)


async def test_send_actionable_no_buttons_omits_quick_reply_key(tmp_path):
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))

    with channel._reply_scope("rt") as ctx:
        await channel.send_actionable("U1", "no buttons here", [])
    await channel._flush_reply("rt", ctx["buffer"])

    body = json.loads(captured[0].content)
    assert "quickReply" not in body["messages"][0]


async def test_send_actionable_callback_data_passed_verbatim_even_over_300_chars(tmp_path, caplog):
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))
    long_data = "log:" + ("x" * 310)
    assert len(long_data) > 300

    with channel._reply_scope("rt") as ctx:
        with caplog.at_level(logging.WARNING, logger="habit_assistant.channels.line"):
            await channel.send_actionable("U1", "pick", [("Long", long_data)])
    await channel._flush_reply("rt", ctx["buffer"])

    body = json.loads(captured[0].content)
    assert body["messages"][0]["quickReply"]["items"][0]["action"]["data"] == long_data  # verbatim, not truncated
    assert any("300-char limit" in r.message for r in caplog.records)


async def test_send_actionable_pushes_when_no_reply_context(tmp_path):
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured))

    await channel.send_actionable("U1", "text", [("a", "log:a")])

    push_calls = [r for r in captured if r.url.path.endswith("/message/push")]
    assert len(push_calls) == 1
    assert db.push_count("U1", _current_yyyymm()) == 1


# ---------------------------------------------------------------------------
# AC11 -- send_image: token file + originalContentUrl/previewImageUrl
# ---------------------------------------------------------------------------


async def test_send_image_writes_token_file_and_emits_text_plus_image(tmp_path):
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))
    png_bytes = b"\x89PNG\r\n\x1a\nFAKEDATA"

    with channel._reply_scope("rt") as ctx:
        await channel.send_image("U1", png_bytes, "your heatmap")
    await channel._flush_reply("rt", ctx["buffer"])

    body = json.loads(captured[0].content)
    messages = body["messages"]
    assert messages[0] == {"type": "text", "text": "your heatmap"}
    assert messages[1]["type"] == "image"
    assert messages[1]["originalContentUrl"] == messages[1]["previewImageUrl"]
    url = messages[1]["originalContentUrl"]
    assert url.startswith("https://vps-host.tailnet.ts.net/media/")
    assert url.endswith(".png")

    token = url.rsplit("/", 1)[-1][: -len(".png")]
    on_disk = tmp_path / "media" / f"{token}.png"
    assert on_disk.is_file()
    assert on_disk.read_bytes() == png_bytes


async def test_send_image_token_is_unguessable_and_unique_per_call(tmp_path):
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))

    with channel._reply_scope("rt") as ctx:
        await channel.send_image("U1", b"one", "a")
        await channel.send_image("U1", b"two", "b")
    await channel._flush_reply("rt", ctx["buffer"])

    body = json.loads(captured[0].content)
    urls = [m["originalContentUrl"] for m in body["messages"] if m["type"] == "image"]
    assert len(urls) == 2
    assert urls[0] != urls[1]


async def test_send_image_write_failure_propagates_not_swallowed(tmp_path):
    """R-3.5: media-serve/token errors propagate so the EXISTING caller's
    own try/except (execute_heatmap/execute_wrapped/weekly-review) can
    degrade to a text summary -- send_image itself must not catch this."""
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))
    import shutil

    shutil.rmtree(tmp_path / "media")  # the directory send_image tries to write into is now gone

    with pytest.raises(OSError):
        await channel.send_image("U1", b"data", "caption")


# ---------------------------------------------------------------------------
# AC13 -- degradations use the base Channel ABC's no-op/degrade defaults
# ---------------------------------------------------------------------------


async def test_degradations_use_base_defaults(tmp_path):
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))

    assert await channel.send_and_pin("U1", "text") is None
    assert await channel.edit_message("U1", "msg-id", "text") is False
    assert await channel.unpin("U1", "msg-id") is None
    assert await channel.set_message_reaction("U1", "msg-id", "👍") is None
    assert await channel.answer_callback_query("cb-id") is None
    assert await channel.set_my_commands({"en": [("help", "Show help")]}) is None

    # send_and_pin/set_message_reaction fall through to send() -> one push
    # (base default just calls self.send(...)); no crash either way.
    assert captured != []


# ---------------------------------------------------------------------------
# AC14 -- rich menu registration at startup, fail-open
# ---------------------------------------------------------------------------


async def test_register_rich_menu_missing_image_is_fail_open_no_api_calls(tmp_path, caplog):
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured), rich_menu_image=tmp_path / "missing.png")

    with caplog.at_level(logging.WARNING, logger="habit_assistant.channels.line"):
        await channel.register_rich_menu()  # must not raise

    assert captured == []
    assert any("not found" in r.message for r in caplog.records)


async def test_register_rich_menu_creates_uploads_and_sets_default(tmp_path):
    """Pins the exact host each registration call goes to -- not just the
    path. LINE splits JSON management calls (list, create, set-default)
    onto api.line.me from binary-content calls (the image upload) onto
    api-data.line.me; hitting api.line.me for the upload 404s in
    production (hotfix v1.0.2). A bare MockTransport doesn't reject a
    wrong host on its own, so these host assertions are the only thing
    that would have caught the regression.

    Archi rider (2026-08-31, orphan cleanup): the very first call is now
    the `/v2/bot/richmenu/list` orphan-cleanup listing (this handler's own
    default -- `_default_handler` -- returns an empty `{}` body for it, so
    `existing == []` and no DELETE call follows), shifting the create/
    upload/set-default sequence down by one index."""
    image_path = tmp_path / "richmenu.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfakepng")
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured), rich_menu_image=image_path)

    await channel.register_rich_menu()

    hosts = [r.url.host for r in captured]
    paths = [r.url.path for r in captured]
    assert hosts[0] == "api.line.me"
    assert paths[0] == "/v2/bot/richmenu/list"
    assert hosts[1] == "api.line.me"
    assert paths[1] == "/v2/bot/richmenu"
    assert hosts[2] == "api-data.line.me"  # binary content upload -- the wrong-host bug
    assert paths[2] == "/v2/bot/richmenu/richmenu-1/content"
    assert hosts[3] == "api.line.me"
    assert paths[3] == "/v2/bot/user/all/richmenu/richmenu-1"
    assert captured[2].headers["Content-Type"] == "image/png"

    create_body = json.loads(captured[1].content)
    assert create_body == _default_rich_menu_payload()
    assert len(create_body["areas"]) == 6


async def test_register_rich_menu_api_failure_is_fail_open(tmp_path, caplog):
    image_path = tmp_path / "richmenu.png"
    image_path.write_bytes(b"fake")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "internal error"})

    channel, _db = _make_channel(tmp_path, handler, rich_menu_image=image_path)

    with caplog.at_level(logging.WARNING, logger="habit_assistant.channels.line"):
        await channel.register_rich_menu()  # must not raise -- startup continues

    assert any("registration failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


async def test_aclose_closes_the_http_client(tmp_path):
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))
    await channel.aclose()
    assert channel._client.is_closed


# ---------------------------------------------------------------------------
# Line-clock fix (branch line-version, TEST-LEDGER-TRIAGE.md): push-ledger
# month attribution honors config.app.timezone via an injectable clock
# (LineChannel._clock/_now_yyyymm), and the realtime gate's own monthly-total
# read shares exactly ONE resolved yyyymm with _send_push's ledger increment
# per push attempt -- no longer two independent datetime.now() calls that
# could straddle a literal month turn against each other.
# ---------------------------------------------------------------------------


class _TickingClock:
    """Returns each of `values` in order on successive calls, then keeps
    returning the last one. Used to prove a given code path resolves "what
    time is it" exactly once per push attempt: if it were read a second
    time (the pre-fix bug -- `_push`'s gate read and `_send_push`'s own
    increment were two independent `datetime.now()` calls), the second,
    DIFFERENT value below would leak into the assertions."""

    def __init__(self, *values: datetime) -> None:
        self._values = list(values)
        self.calls = 0

    def __call__(self) -> datetime:
        value = self._values[min(self.calls, len(self._values) - 1)]
        self.calls += 1
        return value


def test_now_yyyymm_normalizes_through_config_timezone_not_a_bare_clock_read(tmp_path):
    """Config defaults to Asia/Bangkok, UTC+7 (config.py AppConfig.timezone).
    2026-08-31 18:00 UTC is already 2026-09-01 01:00 in Bangkok -- exactly
    the ~7-hour divergence window TEST-LEDGER-TRIAGE.md flagged (17:00-24:00
    UTC on a month's last day is already the 1st in Bangkok). Pre-fix, a
    bare `datetime.now()` on a UTC-clocked host would have read this same
    instant as still August -- no tz conversion at all."""
    captured: list[httpx.Request] = []
    clock = lambda: datetime(2026, 8, 31, 18, 0, tzinfo=ZoneInfo("UTC"))
    channel, _db = _make_channel(tmp_path, _default_handler(captured), clock=clock)

    assert channel._now_yyyymm() == "2026-09"


def test_now_yyyymm_naive_clock_treated_as_already_local_per_local_now_convention(tmp_path):
    """Mirrors `core/digest.py:_local_now` and `core/timeutil.py`'s own
    convention exactly: a NAIVE clock result is treated as already being in
    `config.app.timezone`, not converted from some other assumed zone."""
    captured: list[httpx.Request] = []
    clock = lambda: datetime(2026, 9, 1, 1, 0)  # naive -- read as Bangkok-local directly, no conversion
    channel, _db = _make_channel(tmp_path, _default_handler(captured), clock=clock)

    assert channel._now_yyyymm() == "2026-09"


async def test_digest_mode_push_resolves_yyyymm_exactly_once(tmp_path):
    """Digest mode is `_push`'s pure pass-through branch. A plain `send()`
    outside any reply context must read the injected clock exactly ONCE --
    proof `_send_push`'s own increment reuses `_push`'s single resolved
    `yyyymm` (threaded via its `yyyymm=` kwarg) instead of independently
    re-reading the clock, which is what let a straddle happen pre-fix."""
    captured: list[httpx.Request] = []
    clock = _TickingClock(
        datetime(2026, 8, 31, 23, 0, tzinfo=ZoneInfo("UTC")),  # -> Bangkok 2026-09-01 -> "2026-09"
        datetime(2026, 9, 30, 23, 0, tzinfo=ZoneInfo("UTC")),  # -> Bangkok 2026-10-01 -> "2026-10" (would leak if re-read)
    )
    channel, db = _make_channel(tmp_path, _default_handler(captured), clock=clock)

    await channel.send("U1", "no reply context, straight to push")

    assert clock.calls == 1
    assert db.push_count("U1", "2026-09") == 1
    assert db.push_count("U1", "2026-10") == 0


async def test_realtime_gate_read_and_ledger_increment_share_one_yyyymm_across_a_month_tick(tmp_path):
    """R-Q2/R-Q3 realtime gate: the gate's own `monthly_push_total` read
    and `_send_push`'s eventual ledger increment must key off the SAME
    month, even under a clock that would tick over to a new month between
    them. Pre-fix, `_push`'s gate read (`datetime.now()`) and
    `_send_push`'s increment (a SEPARATE `datetime.now()` call) straddled
    the real network POST to LINE's Push API -- a push crossing a literal
    month turn during that window could pass the cap check against one
    month and then write its increment into the next."""
    captured: list[httpx.Request] = []
    clock = _TickingClock(
        datetime(2026, 8, 31, 23, 0, tzinfo=ZoneInfo("UTC")),  # -> Bangkok 2026-09-01 -> "2026-09"
        datetime(2026, 9, 30, 23, 0, tzinfo=ZoneInfo("UTC")),  # -> Bangkok 2026-10-01 -> "2026-10" (the straddle bug's target)
    )
    channel, db = _make_channel(tmp_path, _default_handler(captured), mode="realtime", push_cap=100, clock=clock)

    await channel._push("Umember", [{"type": "text", "text": "realtime nudge"}])

    assert clock.calls == 1, "gate read + ledger increment must share ONE resolved yyyymm, not two independent clock reads"
    assert db.push_count("Umember", "2026-09") == 1
    assert db.push_count("Umember", "2026-10") == 0


async def test_realtime_gate_fail_closed_behavior_unchanged_with_injected_clock(tmp_path, caplog):
    """R-Q7 (Archi ruling 2026-08-31, fail-closed) is untouched by the
    line-clock fix: a `monthly_push_total` read error still drops the
    non-owner push (no send, no increment) and logs loudly at ERROR, exactly
    as `test_line_v12_gaps.py::test_quota_gate_fail_closed_on_monthly_push_
    total_read_error_drops_and_logs` already pins against the real wall
    clock -- this is the same behavior confirmed under an injected clock,
    so the `yyyymm`-threading change above didn't quietly alter it."""

    class _RaisingDB:
        def __init__(self, real: Database) -> None:
            self._real = real

        def monthly_push_total(self, yyyymm: str) -> int:
            raise OSError("database is locked")

        def __getattr__(self, name):
            return getattr(self._real, name)

    captured: list[httpx.Request] = []
    clock = lambda: datetime(2026, 8, 31, 23, 0, tzinfo=ZoneInfo("UTC"))  # -> Bangkok "2026-09"
    channel, db = _make_channel(tmp_path, _default_handler(captured), mode="realtime", push_cap=5, clock=clock)
    channel.db = _RaisingDB(db)

    with caplog.at_level(logging.ERROR, logger="habit_assistant.channels.line"):
        await channel._push("Umember", [{"type": "text", "text": "hi"}])

    push_calls = [r for r in captured if r.url.path.endswith("/message/push")]
    assert push_calls == [], "a monthly_push_total read failure must fail CLOSED (drop), never send"
    assert db.push_count("Umember", "2026-09") == 0, "a dropped push must never increment the ledger"
    assert any("fail" in r.message.lower() and "closed" in r.message.lower() for r in caplog.records)


async def test_owner_pushes_bypass_gate_and_still_use_the_injected_clocks_month(tmp_path):
    """R-Q3's owner exemption is untouched: the owner's own push always goes
    through `_send_push` regardless of the running total, and still lands
    in the month the injected clock resolves to."""
    captured: list[httpx.Request] = []
    clock = lambda: datetime(2026, 8, 31, 23, 0, tzinfo=ZoneInfo("UTC"))  # -> Bangkok "2026-09"
    channel, db = _make_channel(tmp_path, _default_handler(captured), mode="realtime", push_cap=1, clock=clock)
    for _ in range(10):
        db.increment_push("Umember", "2026-09")

    await channel._push("Uowner", [{"type": "text", "text": "owner push"}])

    owner_calls = [r for r in captured if r.url.path.endswith("/message/push")]
    assert len(owner_calls) == 1
    assert db.push_count("Uowner", "2026-09") == 1
