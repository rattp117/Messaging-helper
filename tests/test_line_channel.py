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

import httpx
import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.channels.line import LineChannel, _default_rich_menu_payload
from habit_assistant.config import Config, LineConfig
from habit_assistant.storage.db import Database


def _current_yyyymm() -> str:
    return datetime.now().strftime("%Y-%m")


def _make_channel(tmp_path, handler, *, rich_menu_image=None, media_ttl_seconds=3600):
    db = Database(tmp_path / "line.db")
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    kwargs = {"public_base_url": "https://vps-host.tailnet.ts.net", "media_dir": str(tmp_path / "media")}
    kwargs["media_ttl_seconds"] = media_ttl_seconds
    if rich_menu_image is not None:
        kwargs["rich_menu_image"] = str(rich_menu_image)
    config = Config(line=LineConfig(**kwargs))
    channel = LineChannel("access-token", "channel-secret", "Uowner", config, db, client=client)
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
    assert result is None  # LINE has no per-message id contract (R-A6)


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
    image_path = tmp_path / "richmenu.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfakepng")
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured), rich_menu_image=image_path)

    await channel.register_rich_menu()

    paths = [r.url.path for r in captured]
    assert paths[0] == "/v2/bot/richmenu"
    assert paths[1] == "/v2/bot/richmenu/richmenu-1/content"
    assert paths[2] == "/v2/bot/user/all/richmenu/richmenu-1"
    assert captured[1].headers["Content-Type"] == "image/png"

    create_body = json.loads(captured[0].content)
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
