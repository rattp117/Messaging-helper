"""SPEC-LINE.md §4 Module A -- `channels/line_webhook.py`'s own tests
(AC5, AC6, AC10, AC11, AC12): signature verification, the `/callback`
fast-200 + single-worker FIFO ordering, postback -> on_callback routing
(verbatim data, empty source_text), the `/media/{token}.png` route, and
the TTL cleanup sweep.

`LineWebhookServer` never imports channels/line.py (see that module's own
docstring), so these tests drive it directly with plain fake
`reply_scope`/`flush_reply` callables -- no httpx, no real LineChannel --
exercising exactly the server surface this file owns."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from habit_assistant.channels.line_webhook import LineWebhookServer, TOKEN_RE, cleanup_expired_media, verify_signature

SECRET = "test-channel-secret"


@pytest.fixture
async def aiohttp_client_factory():
    """Local to this file (conftest.py is shared-surface-owned, out of
    Module A's scope) -- builds a real aiohttp `TestClient` bound to a
    `TestServer` wrapping the given `web.Application`, closed at teardown."""
    clients: list[TestClient] = []

    async def make_client(app: web.Application) -> TestClient:
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    yield make_client

    for client in clients:
        await client.close()


def _sign(secret: str, raw: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return base64.b64encode(mac).decode("utf-8")


def _event_body(events: list[dict]) -> bytes:
    return json.dumps({"destination": "Uxxxx", "events": events}).encode("utf-8")


# ---------------------------------------------------------------------------
# AC5 -- signature verification: valid / invalid / missing / replay(tampered)
# ---------------------------------------------------------------------------


def test_verify_signature_valid():
    raw = b'{"events":[]}'
    sig = _sign(SECRET, raw)
    assert verify_signature(SECRET, raw, sig) is True


def test_verify_signature_invalid():
    raw = b'{"events":[]}'
    assert verify_signature(SECRET, raw, "not-a-real-signature") is False


def test_verify_signature_missing():
    raw = b'{"events":[]}'
    assert verify_signature(SECRET, raw, None) is False
    assert verify_signature(SECRET, raw, "") is False


def test_verify_signature_wrong_secret():
    raw = b'{"events":[]}'
    sig = _sign("a-different-secret", raw)
    assert verify_signature(SECRET, raw, sig) is False


def test_verify_signature_replay_against_different_body_fails():
    """A signature computed for one body must not validate a different
    (e.g. replayed-then-modified) body -- proves the signature is bound to
    the exact raw bytes, not just "some valid signature was supplied"."""
    original = b'{"events":[{"type":"message"}]}'
    sig = _sign(SECRET, original)
    tampered = b'{"events":[{"type":"message"},{"type":"postback"}]}'
    assert verify_signature(SECRET, tampered, sig) is False


# ---------------------------------------------------------------------------
# Test harness: a LineWebhookServer wired to spy reply_scope/flush_reply
# and recording on_message/on_callback handlers.
# ---------------------------------------------------------------------------


class _Harness:
    def __init__(self, tmp_path, media_ttl_seconds=3600):
        self.media_dir = tmp_path / "media"
        self.media_dir.mkdir()
        self.flushed: list[tuple[str, list[dict]]] = []
        self.calls: list[tuple] = []

        self.server = LineWebhookServer(
            channel_secret=SECRET,
            bind_host="127.0.0.1",
            bind_port=0,
            media_dir=self.media_dir,
            media_ttl_seconds=media_ttl_seconds,
            reply_scope=self._reply_scope,
            flush_reply=self._flush_reply,
        )

    @contextlib.contextmanager
    def _reply_scope(self, reply_token: str, owner_chat_id: str | None = None):
        # Release-gate Finding 2: `_dispatch` now passes the event's own
        # user_id as `owner_chat_id` -- this fake doesn't need to interpret
        # it (that comparison lives in `LineChannel._emit`, not the
        # webhook server), just accept it so the real call shape matches.
        ctx = {"replyToken": reply_token, "buffer": [], "ownerChatId": owner_chat_id}
        yield ctx

    async def _flush_reply(self, reply_token: str, buffer: list[dict]) -> None:
        self.flushed.append((reply_token, list(buffer)))

    async def on_message(self, user_id, text, display_name=None, message_id=None, reply_to_message_id=None):
        self.calls.append(("message", user_id, text, message_id))

    async def on_callback(self, user_id, data, source_text, callback_id):
        self.calls.append(("callback", user_id, data, source_text, callback_id))


@pytest.fixture
def harness(tmp_path):
    return _Harness(tmp_path)


def _app_for(harness: "_Harness") -> web.Application:
    app = web.Application()
    app.router.add_post("/callback", harness.server._handle_callback)
    app.router.add_get("/media/{tail:.+}", harness.server._handle_media)
    return app


# ---------------------------------------------------------------------------
# AC5/AC6 -- POST /callback: verify -> fast 200 -> enqueue; 400 on bad sig
# ---------------------------------------------------------------------------


async def test_callback_valid_signature_returns_200_and_enqueues(aiohttp_client_factory, harness):
    body = _event_body(
        [{"type": "message", "replyToken": "rt1", "source": {"type": "user", "userId": "U1"},
          "message": {"type": "text", "id": "m1", "text": "500ml"}}]
    )
    client = await aiohttp_client_factory(_app_for(harness))

    resp = await client.post("/callback", data=body, headers={"X-Line-Signature": _sign(SECRET, body)})

    assert resp.status == 200
    assert harness.server.queue.qsize() == 1


async def test_callback_wrong_signature_returns_400_and_nothing_enqueued(aiohttp_client_factory, harness):
    body = _event_body([{"type": "message", "replyToken": "rt1", "source": {"type": "user", "userId": "U1"},
                          "message": {"type": "text", "id": "m1", "text": "hi"}}])
    client = await aiohttp_client_factory(_app_for(harness))

    resp = await client.post("/callback", data=body, headers={"X-Line-Signature": "wrong"})

    assert resp.status == 400
    assert harness.server.queue.qsize() == 0


async def test_callback_missing_signature_header_returns_400(aiohttp_client_factory, harness):
    body = _event_body([{"type": "message", "replyToken": "rt1", "source": {"type": "user", "userId": "U1"},
                          "message": {"type": "text", "id": "m1", "text": "hi"}}])
    client = await aiohttp_client_factory(_app_for(harness))

    resp = await client.post("/callback", data=body)

    assert resp.status == 400
    assert harness.server.queue.qsize() == 0


async def test_callback_unparseable_json_body_returns_400(aiohttp_client_factory, harness):
    body = b"{not valid json"
    client = await aiohttp_client_factory(_app_for(harness))

    resp = await client.post("/callback", data=body, headers={"X-Line-Signature": _sign(SECRET, body)})

    assert resp.status == 400
    assert harness.server.queue.qsize() == 0


async def test_callback_returns_200_before_any_handler_runs(aiohttp_client_factory, harness):
    """AC6: the POST response must not wait on event processing -- no
    worker task is even running here, so a 200 proves enqueueing alone
    (not handling) is what the response depends on."""
    body = _event_body(
        [{"type": "message", "replyToken": f"rt{i}", "source": {"type": "user", "userId": "U1"},
          "message": {"type": "text", "id": str(i), "text": f"msg{i}"}} for i in range(3)]
    )
    client = await aiohttp_client_factory(_app_for(harness))

    started = time.monotonic()
    resp = await client.post("/callback", data=body, headers={"X-Line-Signature": _sign(SECRET, body)})
    elapsed = time.monotonic() - started

    assert resp.status == 200
    assert elapsed < 1.0
    assert harness.calls == []  # nothing processed -- worker never ran
    assert harness.server.queue.qsize() == 3


# ---------------------------------------------------------------------------
# AC6 -- single-worker FIFO ordering
# ---------------------------------------------------------------------------


async def test_worker_processes_events_fifo_in_order(harness):
    events = [
        {"type": "message", "replyToken": "rt-a", "source": {"type": "user", "userId": "U1"},
         "message": {"type": "text", "id": "1", "text": "first"}},
        {"type": "postback", "replyToken": "rt-b", "source": {"type": "user", "userId": "U1"},
         "postback": {"data": "undo:98765"}},
        {"type": "message", "replyToken": "rt-c", "source": {"type": "user", "userId": "U1"},
         "message": {"type": "text", "id": "3", "text": "third"}},
    ]
    for event in events:
        await harness.server.queue.put(event)

    worker = asyncio.create_task(harness.server._worker(harness.on_message, harness.on_callback))
    for _ in range(50):
        if harness.server.queue.qsize() == 0 and len(harness.calls) == 3:
            break
        await asyncio.sleep(0.02)
    worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker

    assert harness.calls == [
        ("message", "U1", "first", "1"),
        ("callback", "U1", "undo:98765", "", "rt-b"),
        ("message", "U1", "third", "3"),
    ]
    assert [t for t, _ in harness.flushed] == ["rt-a", "rt-b", "rt-c"]


async def test_worker_survives_handler_exception_and_keeps_processing(harness):
    """Mirrors channels/telegram.py's own "on_message exception does not
    crash the loop" test -- a handler raising must not stop the worker
    from draining the rest of the queue."""

    async def flaky_on_message(user_id, text, display_name=None, message_id=None, reply_to_message_id=None):
        harness.calls.append(("message", user_id, text))
        if text == "boom":
            raise RuntimeError("handler blew up")

    await harness.server.queue.put(
        {"type": "message", "replyToken": "rt1", "source": {"type": "user", "userId": "U1"},
         "message": {"type": "text", "id": "1", "text": "boom"}}
    )
    await harness.server.queue.put(
        {"type": "message", "replyToken": "rt2", "source": {"type": "user", "userId": "U1"},
         "message": {"type": "text", "id": "2", "text": "next"}}
    )

    worker = asyncio.create_task(harness.server._worker(flaky_on_message, harness.on_callback))
    for _ in range(50):
        if len(harness.calls) == 2:
            break
        await asyncio.sleep(0.02)
    worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker

    assert harness.calls == [("message", "U1", "boom"), ("message", "U1", "next")]


# ---------------------------------------------------------------------------
# AC10 -- postback -> on_callback(userId, data, "", pseudo_id) verbatim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("data", ["undo:98765", "log:water:500", "clarify:12345:water:500", "routine:run:morning"])
async def test_postback_data_routed_verbatim_with_empty_source_text(harness, data):
    event = {"type": "postback", "replyToken": "rt", "source": {"type": "user", "userId": "U1"}, "postback": {"data": data}}
    await harness.server.process_event(event, harness.on_message, harness.on_callback)

    assert harness.calls == [("callback", "U1", data, "", "rt")]
    assert harness.flushed == [("rt", [])]


async def test_postback_with_no_on_callback_handler_is_skipped(harness):
    event = {"type": "postback", "replyToken": "rt", "source": {"type": "user", "userId": "U1"}, "postback": {"data": "undo:1"}}
    await harness.server.process_event(event, harness.on_message, None)

    assert harness.calls == []
    assert harness.flushed == []


# ---------------------------------------------------------------------------
# §10 out of scope: non-text messages, non-user sources silently skipped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message_type", ["image", "sticker", "location", "audio", "video"])
async def test_non_text_message_types_are_skipped(harness, message_type):
    event = {"type": "message", "replyToken": "rt", "source": {"type": "user", "userId": "U1"},
             "message": {"type": message_type, "id": "1"}}
    await harness.server.process_event(event, harness.on_message, harness.on_callback)

    assert harness.calls == []
    assert harness.flushed == []


@pytest.mark.parametrize("source_type", ["group", "room"])
async def test_non_user_sources_are_skipped(harness, source_type):
    event = {"type": "message", "replyToken": "rt", "source": {"type": source_type, "groupId": "G1"},
             "message": {"type": "text", "id": "1", "text": "hi all"}}
    await harness.server.process_event(event, harness.on_message, harness.on_callback)

    assert harness.calls == []
    assert harness.flushed == []


async def test_event_with_no_reply_token_runs_without_a_reply_context(harness):
    """Defensive path: a message/postback event that somehow carries no
    replyToken must still reach the handler -- just with no active reply
    context, so any send() the handler makes falls through to push (R-A6)
    rather than being silently dropped."""
    event = {"type": "message", "source": {"type": "user", "userId": "U1"}, "message": {"type": "text", "id": "1", "text": "hi"}}
    await harness.server.process_event(event, harness.on_message, harness.on_callback)

    assert harness.calls == [("message", "U1", "hi", "1")]
    assert harness.flushed == []  # no reply_scope entered -> nothing to flush


# ---------------------------------------------------------------------------
# AC11 -- GET /media/{token}.png
# ---------------------------------------------------------------------------


async def test_media_get_known_token_returns_bytes_and_content_type(aiohttp_client_factory, harness):
    (harness.media_dir / "abc123.png").write_bytes(b"\x89PNGDATA")
    client = await aiohttp_client_factory(_app_for(harness))

    resp = await client.get("/media/abc123.png")

    assert resp.status == 200
    assert resp.headers["Content-Type"] == "image/png"
    assert await resp.read() == b"\x89PNGDATA"


async def test_media_get_unknown_token_returns_404(aiohttp_client_factory, harness):
    client = await aiohttp_client_factory(_app_for(harness))
    resp = await client.get("/media/does-not-exist.png")
    assert resp.status == 404


async def test_media_get_non_png_suffix_returns_404(aiohttp_client_factory, harness):
    (harness.media_dir / "abc123.png").write_bytes(b"data")
    client = await aiohttp_client_factory(_app_for(harness))
    resp = await client.get("/media/abc123.txt")
    assert resp.status == 404


@pytest.mark.parametrize("tail", ["../secret.png", "..%2Fsecret.png", "a/b.png", "..%2f..%2fetc%2fpasswd.png"])
async def test_media_get_path_traversal_attempts_return_404(aiohttp_client_factory, harness, tail):
    client = await aiohttp_client_factory(_app_for(harness))
    resp = await client.get(f"/media/{tail}")
    assert resp.status in (400, 404)  # aiohttp itself may reject a malformed path before routing


def test_token_regex_rejects_traversal_and_separators():
    assert TOKEN_RE.match("Ab3xY9_k-1") is not None
    assert TOKEN_RE.match("../secret") is None
    assert TOKEN_RE.match("a/b") is None
    assert TOKEN_RE.match("a" * 65) is None  # over the 64-char cap
    assert TOKEN_RE.match("") is None


# ---------------------------------------------------------------------------
# AC12 -- media TTL cleanup
# ---------------------------------------------------------------------------


async def test_cleanup_expired_media_deletes_aged_file_and_keeps_fresh_one(tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    old_file = media_dir / "old.png"
    fresh_file = media_dir / "fresh.png"
    old_file.write_bytes(b"old")
    fresh_file.write_bytes(b"fresh")

    old_time = time.time() - 3700
    import os

    os.utime(old_file, (old_time, old_time))

    cleanup_expired_media(media_dir, media_ttl_seconds=3600)

    assert not old_file.exists()
    assert fresh_file.exists()


async def test_get_after_cleanup_returns_404_for_expired_token(aiohttp_client_factory, harness):
    expired = harness.media_dir / "expired.png"
    expired.write_bytes(b"data")
    old_time = time.time() - 10
    import os

    os.utime(expired, (old_time, old_time))

    cleanup_expired_media(harness.media_dir, media_ttl_seconds=1)

    client = await aiohttp_client_factory(_app_for(harness))
    resp = await client.get("/media/expired.png")
    assert resp.status == 404


def test_cleanup_expired_media_never_raises_when_dir_missing(tmp_path):
    missing_dir = tmp_path / "does-not-exist"
    cleanup_expired_media(missing_dir, media_ttl_seconds=1)  # must not raise
