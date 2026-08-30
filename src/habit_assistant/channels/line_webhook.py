"""SPEC-LINE.md §4 Module A (R-A1/A2/A3/A12/A13): the aiohttp inbound
server for the LINE channel -- `POST /callback` (signature verify -> fast
200 -> enqueue) and `GET /media/{token}.png` (tokened PNG serving), plus
the single-worker FIFO queue and the media TTL sweep.

Kept separate from channels/line.py so the outbound Channel-ABC methods
(send/send_actionable/send_image) stay focused on the reply-vs-push
distinction. This module never imports channels/line.py -- `LineChannel.run()`
constructs a `LineWebhookServer`, handing it plain callables
(`reply_scope`/`flush_reply`) for the two pieces of state
(the contextvars reply buffer, the httpx-backed reply/push calls) that live
on the channel -- so this module is independently testable with fake
callables and never needs a real LineChannel/httpx client.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

logger = logging.getLogger(__name__)

# R-A12: token charset -- letters/digits/underscore/hyphen only. No dot, no
# slash, so a `../`-style traversal attempt can never match this regex,
# regardless of how aiohttp's own router normalizes the request path.
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Release-gate Finding 2 (branch `line-version`): the second argument is
# the inbound event's own owning chat_id (`user_id`), threaded through so
# `LineChannel._emit` can tell a same-user send (buffer) apart from a
# cross-user send (push) made while handling this one event -- see
# `_dispatch`'s own docstring below.
ReplyScope = Callable[[str, "str | None"], "contextlib.AbstractContextManager[dict[str, Any]]"]
FlushReply = Callable[[str, list[dict[str, Any]]], Awaitable[None]]


def verify_signature(channel_secret: str, raw_body: bytes, signature: str | None) -> bool:
    """R-A1: `x-line-signature` == base64(HMAC-SHA256(channel_secret, raw_body)),
    computed over the RAW request bytes (never the re-serialized JSON) and
    compared with `hmac.compare_digest` (constant-time, avoids a timing
    side-channel on the comparison itself)."""
    if not signature:
        return False
    mac = hmac.new(channel_secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def cleanup_expired_media(media_dir: Path, media_ttl_seconds: int) -> None:
    """R-A13: delete every `*.png` under `media_dir` older than
    `media_ttl_seconds`. Exposed as a free function (not bound to a running
    server) so a test, or a scheduler job (Integration's own wiring choice
    per R-A13's "the scheduler wiring is integration's"), can call it
    directly. Never raises into a send path -- any per-file error is logged
    and the sweep continues."""
    now = time.time()
    try:
        candidates = list(media_dir.glob("*.png"))
    except OSError:
        logger.exception("Media TTL sweep failed to list %s; skipping this pass", media_dir)
        return
    for path in candidates:
        try:
            if now - path.stat().st_mtime >= media_ttl_seconds:
                path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Media TTL sweep failed to remove %s; continuing", path)


class LineWebhookServer:
    """Owns the aiohttp `AppRunner`/`TCPSite`, the inbound `asyncio.Queue`,
    the single FIFO worker task (R-A3), and the media route (R-A12/A13).
    `LineChannel.run()` builds a fresh instance per run."""

    def __init__(
        self,
        *,
        channel_secret: str,
        bind_host: str,
        bind_port: int,
        media_dir: Path,
        media_ttl_seconds: int,
        reply_scope: ReplyScope,
        flush_reply: FlushReply,
    ) -> None:
        self._channel_secret = channel_secret
        self._bind_host = bind_host
        self._bind_port = bind_port
        self._media_dir = media_dir
        self._media_ttl_seconds = media_ttl_seconds
        self._reply_scope = reply_scope
        self._flush_reply = flush_reply
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def serve(
        self,
        on_message: Callable[..., Awaitable[None]],
        on_callback: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        """R-A15: start the server + worker + TTL sweep, then await until
        cancelled (mirrors TelegramChannel.run's own long-poll-forever
        shape, just with a passive server instead of an active poll)."""
        app = web.Application()
        app.router.add_post("/callback", self._handle_callback)
        app.router.add_get("/media/{tail:.+}", self._handle_media)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._bind_host, self._bind_port)
        await site.start()
        logger.info("LINE webhook server listening on %s:%d", self._bind_host, self._bind_port)

        worker_task = asyncio.create_task(self._worker(on_message, on_callback))
        ttl_task = asyncio.create_task(self._ttl_loop())
        try:
            await asyncio.Event().wait()  # runs forever until this task is cancelled
        finally:
            worker_task.cancel()
            ttl_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
            with contextlib.suppress(asyncio.CancelledError):
                await ttl_task
            await runner.cleanup()

    # -- POST /callback (R-A1/A2) ------------------------------------------

    async def _handle_callback(self, request: web.Request) -> web.Response:
        raw = await request.read()
        signature = request.headers.get("X-Line-Signature")
        if not verify_signature(self._channel_secret, raw, signature):
            logger.warning("LINE webhook signature verification failed; dropping request")
            return web.Response(status=400)
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # R-A2: "a body that fails JSON parsing -> 400". json.loads can
            # raise either on malformed JSON text (JSONDecodeError) or, via
            # its own internal encoding-detection step, on invalid-UTF8/
            # UTF-16/UTF-32-looking byte sequences (UnicodeDecodeError) --
            # both mean "this body cannot be parsed", same outcome.
            logger.warning("LINE webhook body failed to parse as JSON; dropping request")
            return web.Response(status=400)
        if not isinstance(payload, dict):
            # §3.4 documents exactly two outcomes for POST /callback (200/
            # 400) -- valid JSON whose top level isn't an object (a bare
            # array/null/number/string) is not the documented
            # `{"events": [...]}` shape, so it belongs in the same
            # "unparseable body" 400 bucket as a JSON syntax error, not an
            # unhandled 500 from `payload.get(...)` on a non-dict.
            logger.warning(
                "LINE webhook body parsed but top-level JSON value is not an object (got %s); dropping request",
                type(payload).__name__,
            )
            return web.Response(status=400)
        events = payload.get("events") or []
        for event in events:
            await self.queue.put(event)
        return web.Response(status=200)

    # -- worker: single task, FIFO, sequential (R-A3) -----------------------

    async def _worker(
        self,
        on_message: Callable[..., Awaitable[None]],
        on_callback: Callable[..., Awaitable[None]] | None,
    ) -> None:
        while True:
            event = await self.queue.get()
            try:
                await self.process_event(event, on_message, on_callback)
            except Exception:
                logger.exception("Error processing LINE event; continuing worker loop")
            finally:
                self.queue.task_done()

    async def process_event(
        self,
        event: dict[str, Any],
        on_message: Callable[..., Awaitable[None]],
        on_callback: Callable[..., Awaitable[None]] | None,
    ) -> None:
        """SPEC-LINE.md §2.1: user-only text messages and postbacks; every
        other event type/source (group/room, non-text messages, follow/
        unfollow, ...) is out of scope (§10) and silently skipped -- not an
        error, just nothing this branch handles."""
        source = event.get("source") or {}
        if source.get("type") != "user":
            return
        user_id = source.get("userId") or ""
        if not user_id:
            return
        reply_token = event.get("replyToken")
        event_type = event.get("type")

        if event_type == "message":
            message = event.get("message") or {}
            if message.get("type") != "text":
                return
            text = message.get("text") or ""
            message_id = message.get("id")
            await self._dispatch(reply_token, user_id, lambda: on_message(user_id, text, None, message_id, None))
        elif event_type == "postback" and on_callback is not None:
            data = (event.get("postback") or {}).get("data") or ""
            # R-A9: no LINE construct maps to Telegram's callback_query id
            # (there's no spinner to dismiss -- answer_callback_query stays
            # the base no-op); the event's own replyToken is a fine, unique
            # enough stand-in for the pseudo callback id.
            pseudo_id = reply_token or ""
            await self._dispatch(reply_token, user_id, lambda: on_callback(user_id, data, "", pseudo_id))

    async def _dispatch(
        self, reply_token: str | None, owner_chat_id: str, call: Callable[[], Awaitable[None]]
    ) -> None:
        """R-A4: buffer every send the handler makes into one reply call.
        No replyToken on the event (shouldn't happen for message/postback,
        but defensive) -- run the handler with no active reply context, so
        any `send` it makes falls through to the push path (R-A6).

        Release-gate Finding 2: `owner_chat_id` (this event's own
        `user_id`, always non-empty by the time `process_event` calls
        this -- see its own `if not user_id: return` guard) is threaded
        into `reply_scope` so `LineChannel._emit` can buffer only sends
        actually addressed to THIS user and push anything addressed to
        someone else (e.g. `core/access.py`'s owner-pending-approval
        alert, fired while still processing a stranger's own event)."""
        if not reply_token:
            await call()
            return
        with self._reply_scope(reply_token, owner_chat_id) as ctx:
            await call()
        await self._flush_reply(reply_token, ctx["buffer"])

    # -- GET /media/{token}.png (R-A12) --------------------------------------

    async def _handle_media(self, request: web.Request) -> web.Response:
        tail = request.match_info.get("tail", "")
        if not tail.endswith(".png"):
            return web.Response(status=404)
        token = tail[: -len(".png")]
        if not TOKEN_RE.match(token):
            return web.Response(status=404)
        path = self._media_dir / f"{token}.png"
        if not path.is_file():
            return web.Response(status=404)
        return web.Response(body=path.read_bytes(), content_type="image/png")

    # -- media TTL sweep (R-A13) ---------------------------------------------

    async def _ttl_loop(self) -> None:
        interval = max(1.0, min(float(self._media_ttl_seconds), 300.0))
        while True:
            await asyncio.sleep(interval)
            cleanup_expired_media(self._media_dir, self._media_ttl_seconds)
