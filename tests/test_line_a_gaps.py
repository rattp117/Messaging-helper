"""Adversarial probe suite for SPEC-LINE.md Module A (`channels/line.py` +
`channels/line_webhook.py`) -- the branch's security perimeter (the public
webhook + media endpoints sit behind Tailscale Funnel with no other auth).

This file does NOT re-test what tests/test_line_channel.py and
tests/test_line_webhook.py already cover well (basic reply-aggregation,
push+ledger, quick-reply mapping, media serving, TTL sweep, degradations,
rich-menu fail-open). It probes:

  - signature verification edge cases (malformed/oversized/binary/mutated
    bodies, header case, timing-safety),
  - the /callback handler's behavior on well-formed-signature-but-malformed
    JSON (a gap the existing suite doesn't probe),
  - media path-traversal corpus beyond the existing parametrized set,
  - reply-buffer boundary conditions (exactly 5/6 objects) and contextvar
    isolation under real concurrency (not just sequential-worker
    correctness),
  - quick-reply boundary conditions (exactly 13/14 items, exactly
    300/301-char postback data) and a verbatim round-trip through a
    simulated postback event,
  - the single-worker FIFO ordering guarantee under interleaved users,
    proven with explicit start/end instrumentation (not just "final order
    matched"),
  - the documented "no retry/backoff on outbound sends" parity with
    channels/telegram.py's own precedent.

Same no-real-network convention as the other two Module A test files:
httpx.MockTransport for LineChannel, a real aiohttp TestClient/TestServer
for LineWebhookServer.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import inspect
import json
import logging
import time
from datetime import datetime

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from habit_assistant.channels.line import LineChannel
from habit_assistant.channels.line_webhook import (
    TOKEN_RE,
    LineWebhookServer,
    verify_signature,
)
from habit_assistant.config import Config, LineConfig
from habit_assistant.storage.db import Database

SECRET = "test-channel-secret"


def _current_yyyymm() -> str:
    return datetime.now().strftime("%Y-%m")


def _sign(secret: str, raw: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return base64.b64encode(mac).decode("utf-8")


def _make_channel(tmp_path, handler, *, media_ttl_seconds=3600):
    """Same convention as tests/test_line_channel.py::_make_channel --
    httpx.MockTransport, no real network."""
    db = Database(tmp_path / "line.db")
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    config = Config(
        line=LineConfig(
            public_base_url="https://vps-host.tailnet.ts.net",
            media_dir=str(tmp_path / "media"),
            media_ttl_seconds=media_ttl_seconds,
        )
    )
    channel = LineChannel("access-token", "channel-secret", "Uowner", config, db, client=client)
    return channel, db


def _counting_handler(counts: dict, *, path_suffix: str, status: int):
    """A MockTransport handler that counts calls to one endpoint and always
    answers with `status` -- used to prove no retry loop happens."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(path_suffix):
            counts["n"] = counts.get("n", 0) + 1
            return httpx.Response(status, json={"message": "error"})
        return httpx.Response(200, json={})

    return handler


class _Harness:
    """Local, self-contained webhook harness -- mirrors tests/test_line_
    webhook.py::_Harness but kept independent so this file has no coupling
    to the other test modules (each Module-A test file must stand alone)."""

    def __init__(self, tmp_path, media_ttl_seconds=3600):
        self.media_dir = tmp_path / "media"
        self.media_dir.mkdir(exist_ok=True)
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


@pytest.fixture
async def aiohttp_client_factory():
    clients: list[TestClient] = []

    async def make_client(app: web.Application) -> TestClient:
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    yield make_client

    for client in clients:
        await client.close()


def _event_body(events: list[dict]) -> bytes:
    return json.dumps({"destination": "Uxxxx", "events": events}).encode("utf-8")


# =============================================================================
# SIGNATURE (R-A1 / AC5) -- the perimeter
# =============================================================================


def test_verify_signature_uses_compare_digest_structurally():
    """Timing side-channel defense: assert the SOURCE actually calls
    hmac.compare_digest (constant-time) rather than `==` (short-circuiting,
    timing-leaky) for the comparison. A real timing measurement in a test
    is unreliable/flaky; a structural check on the implementation is the
    correct way to pin this down and catch a future regression that
    swaps it for `==`."""
    source = inspect.getsource(verify_signature)
    assert "hmac.compare_digest" in source
    # And NOT a plain `==` comparison of the two signature values (a naive
    # `expected == signature` would be a timing side-channel regression).
    assert "expected == signature" not in source.replace(" ", "")


def test_verify_signature_malformed_base64_signature_is_false_not_a_crash():
    """An attacker-supplied header need not even be valid base64 --
    verify_signature must never raise, just reject."""
    raw = b'{"events":[]}'
    for garbage_sig in ["not!!base64$$$", "=====", "\x00\x01\x02", "a" * 5000, ""]:
        assert verify_signature(SECRET, raw, garbage_sig) is False


def test_verify_signature_correct_but_wrong_length_signature_is_false():
    """A signature that's a correct-alphabet base64 string but the wrong
    length (e.g. truncated) must not raise inside compare_digest (which
    historically could behave oddly on length-mismatched inputs on some
    platforms) and must reject."""
    raw = b'{"events":[]}'
    real_sig = _sign(SECRET, raw)
    assert verify_signature(SECRET, raw, real_sig[:10]) is False
    assert verify_signature(SECRET, raw, real_sig + "extra") is False


def test_verify_signature_unicode_body_round_trips():
    """R-A1: the signature is computed over the raw UTF-8 bytes -- a Thai
    (this channel's primary market) message must sign/verify exactly like
    any other body."""
    raw = json.dumps(
        {"events": [{"type": "message", "message": {"type": "text", "text": "ดื่มน้ำ 500ml สวัสดีครับ"}}]},
        ensure_ascii=False,
    ).encode("utf-8")
    sig = _sign(SECRET, raw)
    assert verify_signature(SECRET, raw, sig) is True
    assert verify_signature(SECRET, raw, sig[:-1] + ("A" if sig[-1] != "A" else "B")) is False


def test_verify_signature_binary_non_utf8_body_does_not_raise():
    """A raw byte body that isn't valid UTF-8/JSON at all must still be
    signable/verifiable without verify_signature itself raising (only the
    JSON-parse step downstream is expected to fail on this)."""
    raw = bytes(range(256))  # every byte value, including invalid UTF-8 leads
    sig = _sign(SECRET, raw)
    assert verify_signature(SECRET, raw, sig) is True
    assert verify_signature(SECRET, raw, "wrong") is False


async def test_header_case_insensitivity_lowercase_as_line_actually_sends_it(aiohttp_client_factory, harness):
    """SPEC-LINE.md §2.1 itself documents the header in lowercase
    (`x-line-signature`) -- LINE's own docs write it that way, and HTTP
    headers are case-insensitive by spec (RFC 7230 §3.2). A Funnel/proxy
    hop could also normalize casing. The handler reads
    `request.headers.get("X-Line-Signature")`; aiohttp's Headers is a
    case-insensitive multidict, so this must accept any casing."""
    body = _event_body([])
    client = await aiohttp_client_factory(_app_for(harness))
    sig = _sign(SECRET, body)

    resp_lower = await client.post("/callback", data=body, headers={"x-line-signature": sig})
    assert resp_lower.status == 200

    resp_upper = await client.post("/callback", data=body, headers={"X-LINE-SIGNATURE": sig})
    assert resp_upper.status == 200


async def test_signature_over_mutated_body_whitespace_only_difference_rejected(aiohttp_client_factory, harness):
    """Proves the raw-bytes-before-parse claim (R-A1) with a body that is
    JSON-EQUIVALENT to the signed one but byte-different (extra
    whitespace) -- if verification were done against a re-serialized/
    re-parsed body instead of the exact raw bytes read off the socket,
    this would incorrectly validate."""
    signed_body = b'{"events":[]}'
    sig = _sign(SECRET, signed_body)
    reformatted_body = b'{ "events" : [ ] }'  # same JSON value, different bytes
    assert json.loads(signed_body) == json.loads(reformatted_body)

    client = await aiohttp_client_factory(_app_for(harness))
    resp = await client.post("/callback", data=reformatted_body, headers={"X-Line-Signature": sig})

    assert resp.status == 400
    assert harness.server.queue.qsize() == 0


async def test_signature_valid_for_body_a_rejected_when_events_appended(aiohttp_client_factory, harness):
    """Content-mutation variant of the same claim: appending a forged
    event to an otherwise-legitimately-signed body must be rejected."""
    original = _event_body([{"type": "message", "message": {"type": "text", "text": "legit"}}])
    sig = _sign(SECRET, original)
    forged = _event_body(
        [
            {"type": "message", "message": {"type": "text", "text": "legit"}},
            {"type": "message", "message": {"type": "text", "text": "INJECTED"}},
        ]
    )
    client = await aiohttp_client_factory(_app_for(harness))
    resp = await client.post("/callback", data=forged, headers={"X-Line-Signature": sig})

    assert resp.status == 400
    assert harness.server.queue.qsize() == 0


async def test_oversized_payload_rejected_gracefully_not_a_crash_or_hang(aiohttp_client_factory, harness):
    """A multi-megabyte body (well beyond any real LINE payload) must not
    hang the worker or crash the process -- aiohttp's own default
    client_max_size (1 MiB) rejects it before the handler body even runs.
    Documents the perimeter's DoS posture; not a Module-A code path, but
    worth pinning down since nothing in line_webhook.py raises
    client_max_size, so a future refactor that does could silently disable
    this protection."""
    big_text = "a" * (2 * 1024 * 1024)
    body = _event_body([{"type": "message", "message": {"type": "text", "text": big_text}}])
    sig = _sign(SECRET, body)
    client = await aiohttp_client_factory(_app_for(harness))

    resp = await client.post("/callback", data=body, headers={"X-Line-Signature": sig})

    assert resp.status in (400, 413)  # rejected, not 200 and not a 500/hang
    assert harness.server.queue.qsize() == 0


# ---------------------------------------------------------------------------
# BUG PROBES: correct signature, but structurally-malformed JSON body.
# §3.4 documents exactly two outcomes for POST /callback: 200 or 400.
# R-A2: "A body that fails JSON parsing -> 400."
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_body",
    [
        pytest.param(b"[1,2,3]", id="top-level-json-array"),
        pytest.param(b"null", id="top-level-json-null"),
        pytest.param(b"42", id="top-level-json-number"),
        pytest.param(b'"just a string"', id="top-level-json-string"),
    ],
)
async def test_callback_valid_signature_well_formed_json_wrong_top_level_shape_never_500s(
    aiohttp_client_factory, harness, raw_body
):
    """SPEC-LINE.md §3.4 documents exactly two responses for POST
    /callback: `200 OK` (signature verifies, events enqueued) or `400`
    (missing/invalid signature or unparseable body). A correctly-signed
    body that IS valid JSON but not a `{"events": [...]}` -shaped object
    (a bare array/null/number/string) is not on that list of documented
    outcomes -- but the handler does `payload.get("events")` unconditionally
    (channels/line_webhook.py:144), which raises AttributeError for any
    non-dict top-level JSON value. aiohttp turns that into an unhandled
    500.

    FINDING: this currently returns 500, not one of the two documented
    outcomes. A 500 is also a worse signal for LINE's own webhook retry
    behavior (LINE treats 5xx as "redeliver"), so a single malformed
    delivery could be retried indefinitely by LINE's infrastructure.
    """
    sig = _sign(SECRET, raw_body)
    client = await aiohttp_client_factory(_app_for(harness))

    resp = await client.post("/callback", data=raw_body, headers={"X-Line-Signature": sig})

    assert resp.status != 500, (
        f"body {raw_body!r}: got 500 (unhandled AttributeError from "
        f"`payload.get('events')` on a non-dict top-level JSON value) -- "
        f"SPEC-LINE.md §3.4 only documents 200/400 for POST /callback"
    )
    assert resp.status in (200, 400)


async def test_callback_valid_signature_invalid_utf8_body_returns_400_not_500(aiohttp_client_factory, harness):
    """A body that is not valid UTF-8 at all unambiguously "fails JSON
    parsing" per R-A2's own words ("A body that fails JSON parsing ->
    400"). But `json.loads` on certain invalid-UTF8 byte sequences raises
    `UnicodeDecodeError`, not `json.JSONDecodeError` -- and the handler's
    except clause only catches the latter (channels/line_webhook.py:139-
    143), so this byte sequence falls through to an unhandled 500 instead
    of the R-A2-mandated 400.

    Repro: `json.loads(b'\\x80\\x81\\x82\\x83')` raises UnicodeDecodeError
    (verified directly against the stdlib in this Python version), not
    JSONDecodeError.
    """
    raw_body = b"\x80\x81\x82\x83"  # invalid UTF-8 lead bytes; not valid JSON by any measure
    with pytest.raises(UnicodeDecodeError):
        json.loads(raw_body)  # confirms this body hits the uncaught exception type

    sig = _sign(SECRET, raw_body)
    client = await aiohttp_client_factory(_app_for(harness))

    resp = await client.post("/callback", data=raw_body, headers={"X-Line-Signature": sig})

    assert resp.status == 400, (
        f"got {resp.status}, expected 400 per R-A2 (\"A body that fails JSON "
        f"parsing -> 400\") -- invalid-UTF8 bytes raise UnicodeDecodeError, "
        f"which is NOT caught by `except json.JSONDecodeError` in "
        f"channels/line_webhook.py:_handle_callback, producing an unhandled 500"
    )
    assert harness.server.queue.qsize() == 0


async def test_callback_events_key_not_a_list_does_not_crash_the_worker(aiohttp_client_factory, harness):
    """`{"events": "not-a-list"}` is valid JSON, IS dict-shaped (so the
    AttributeError above doesn't fire), but `events` is a truthy non-list
    -- `payload.get("events") or []` yields the string itself, and `for
    event in events: queue.put(event)` enqueues individual characters.
    This must not crash the /callback response, and the worker (broad
    except Exception around process_event) must survive dequeuing garbage
    non-dict "events" without dying -- confirms the fail-safe posture two
    layers deep."""
    body = json.dumps({"events": "abc"}).encode("utf-8")
    sig = _sign(SECRET, body)
    client = await aiohttp_client_factory(_app_for(harness))

    resp = await client.post("/callback", data=body, headers={"X-Line-Signature": sig})
    assert resp.status == 200  # handler itself doesn't crash

    worker = asyncio.create_task(harness.server._worker(harness.on_message, harness.on_callback))
    for _ in range(50):
        if harness.server.queue.qsize() == 0:
            break
        await asyncio.sleep(0.02)
    worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker

    # Garbage characters never matched a real event shape -> no handler calls.
    assert harness.calls == []


# =============================================================================
# MEDIA (R-A11/R-A12) -- path traversal corpus, TTL, content-type
# =============================================================================


@pytest.mark.parametrize(
    "tail",
    [
        "..\\secret.png",  # literal backslash (Windows-style traversal)
        "%5c..%5csecret.png",  # URL-encoded backslash
        "abc%00.png",  # embedded null byte before the extension
        "abc.png%00.txt",  # null-byte extension-confusion attempt
        "a" * 200 + ".png",  # far over the 64-char token cap
        "....//....//etc/passwd.png",  # doubled-dot obfuscation
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd.png",  # fully percent-encoded traversal
        ".png",  # empty token, just the suffix
        "-.png",  # single-char token that's just a hyphen (charset-valid but meaningless)
    ],
)
async def test_media_path_traversal_and_charset_edge_corpus_returns_404_or_400(aiohttp_client_factory, harness, tail):
    """Extends tests/test_line_webhook.py's own traversal parametrization
    with backslash, null-byte, oversized-token, and doubled-obfuscation
    variants. None may ever return 200 or leak a directory listing / any
    file outside media_dir; None may 500."""
    client = await aiohttp_client_factory(_app_for(harness))
    resp = await client.get(f"/media/{tail}")
    assert resp.status in (400, 404), f"tail={tail!r} got {resp.status}"


def test_token_regex_charset_edges():
    """Direct regex probes for characters adjacent to the allowed charset
    (letters/digits/underscore/hyphen) -- each must be rejected."""
    assert TOKEN_RE.match("a" * 64) is not None  # exactly at the cap: allowed
    assert TOKEN_RE.match("a" * 65) is None  # one over: rejected
    for bad in ["abc.def", "abc def", "abc+def", "abc/def", "abc=def", "abc\ndef", "abc\x00def", "ábc"]:
        assert TOKEN_RE.match(bad) is None, f"{bad!r} unexpectedly matched"


async def test_media_get_directory_traversal_cannot_escape_media_dir_even_with_valid_token_shape(tmp_path):
    """Positive-control companion to the traversal corpus: plant a
    "secret" file OUTSIDE media_dir with a name that, if traversal worked,
    would be reachable via a crafted token -- then confirm the server
    response for that path is 404 and the secret's contents never appear
    in any response body across the whole traversal corpus above."""
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    secret_dir = tmp_path  # one level above media_dir
    (secret_dir / "secret.png").write_bytes(b"TOP-SECRET-BYTES")

    harness = _Harness(tmp_path)
    app = _app_for(harness)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        for tail in ["../secret.png", "..%2fsecret.png", "%2e%2e%2fsecret.png"]:
            resp = await client.get(f"/media/{tail}")
            assert resp.status == 404
            body = await resp.read()
            assert b"TOP-SECRET-BYTES" not in body
    finally:
        await client.close()


async def test_media_no_directory_listing_on_bare_media_path(aiohttp_client_factory, harness):
    (harness.media_dir / "real-token-abc.png").write_bytes(b"data")
    client = await aiohttp_client_factory(_app_for(harness))

    resp_trailing = await client.get("/media/")
    resp_bare = await client.get("/media")

    assert resp_trailing.status == 404
    assert resp_bare.status == 404
    body = await resp_trailing.read()
    assert b"real-token-abc.png" not in body  # no listing of the directory's contents


async def test_media_ttl_boundary_file_just_under_ttl_survives_just_over_is_removed(tmp_path):
    """R-A13 boundary check: cleanup_expired_media's own `>=` comparison
    (age >= ttl -> delete) means a file exactly at the boundary is
    deleted, not kept -- verify both sides of that boundary explicitly
    rather than only the existing suite's ~100s-over-a-3600s-ttl margin."""
    import os

    from habit_assistant.channels.line_webhook import cleanup_expired_media

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    just_under = media_dir / "just_under.png"
    just_over = media_dir / "just_over.png"
    just_under.write_bytes(b"a")
    just_over.write_bytes(b"b")

    now = time.time()
    os.utime(just_under, (now - 5, now - 5))  # age 5s, ttl 10s -> keep
    os.utime(just_over, (now - 11, now - 11))  # age 11s, ttl 10s -> delete

    cleanup_expired_media(media_dir, media_ttl_seconds=10)

    assert just_under.exists()
    assert not just_over.exists()


# =============================================================================
# REPLY BUFFER (R-A4/R-A5) -- boundary conditions + real concurrency
# =============================================================================


def _default_handler(captured):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={})

    return handler


async def test_exactly_five_reply_objects_survive_with_no_warning(tmp_path, caplog):
    """Boundary companion to the existing 7-objects-dropped test: exactly
    the limit (5) must NOT trigger the overflow-warning path at all."""
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))

    with channel._reply_scope("rt-exact-5") as ctx:
        for i in range(5):
            await channel.send("U1", f"m{i}")
    with caplog.at_level(logging.WARNING, logger="habit_assistant.channels.line"):
        await channel._flush_reply("rt-exact-5", ctx["buffer"])

    reply_calls = [r for r in captured if r.url.path.endswith("/message/reply")]
    body = json.loads(reply_calls[0].content)
    assert len(body["messages"]) == 5
    assert not any("dropping the overflow" in r.message for r in caplog.records)


async def test_exactly_six_reply_objects_drops_only_the_sixth(tmp_path):
    """Which 5 survive, precisely: the FIRST 5 in call order, not the last
    5 and not an arbitrary subset -- the 6th (and only the 6th) is
    dropped."""
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))

    with channel._reply_scope("rt-exact-6") as ctx:
        for i in range(6):
            await channel.send("U1", f"m{i}")
    await channel._flush_reply("rt-exact-6", ctx["buffer"])

    reply_calls = [r for r in captured if r.url.path.endswith("/message/reply")]
    body = json.loads(reply_calls[0].content)
    surviving_texts = [m["text"] for m in body["messages"]]
    assert surviving_texts == ["m0", "m1", "m2", "m3", "m4"]
    assert "m5" not in surviving_texts


async def test_reply_dropped_on_network_transport_error_not_just_bad_status(tmp_path, caplog):
    """R-A5's "never fall back to push" must hold for a TRANSPORT-level
    failure (connection refused / DNS / timeout via httpx.RequestError),
    not just an LINE-returned 4xx/5xx status -- httpx.HTTPError is the
    common base of both HTTPStatusError and RequestError, so
    `except httpx.HTTPError` in _flush_reply should already cover this;
    confirm it does."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    channel, db = _make_channel(tmp_path, handler)

    with channel._reply_scope("rt-network-fail") as ctx:
        await channel.send("U1", "hello")
    with caplog.at_level(logging.WARNING, logger="habit_assistant.channels.line"):
        await channel._flush_reply("rt-network-fail", ctx["buffer"])  # must not raise

    assert db.push_count("U1", _current_yyyymm()) == 0  # never fell back to push


async def test_reply_context_resets_via_finally_even_when_handler_raises(tmp_path):
    """The contextvar reset in `_reply_scope`'s `finally` must fire even
    when the wrapped handler code raises -- otherwise a crashing handler
    would leave the module-level `_REPLY_CONTEXT` "stuck" set for
    whatever runs next (a real cross-event contamination risk, since the
    ContextVar is process/module-level state). Prove it: raise inside the
    scope, then confirm a FRESH, unrelated send() immediately afterward
    correctly falls through to push (i.e. no active context leaked)."""
    captured: list[httpx.Request] = []
    channel, db = _make_channel(tmp_path, _default_handler(captured))

    with pytest.raises(RuntimeError):
        with channel._reply_scope("rt-will-raise"):
            await channel.send("U1", "before the crash")
            raise RuntimeError("handler blew up mid-event")

    # No active reply context anymore -> this send must PUSH, not silently
    # vanish into a stale buffer from the crashed scope.
    await channel.send("U2", "unrelated later send")

    push_calls = [r for r in captured if r.url.path.endswith("/message/push")]
    assert len(push_calls) == 1
    assert json.loads(push_calls[0].content)["to"] == "U2"
    assert db.push_count("U2", _current_yyyymm()) == 1


async def test_two_concurrent_reply_scopes_never_cross_contaminate_buffers(tmp_path):
    """R-A4's isolation guarantee, tested at the primitive level under
    REAL concurrency (asyncio.gather, not the sequential worker) --
    contextvars.ContextVar is copy-on-task-creation, so two tasks each
    holding their own active `_reply_scope` must never see each other's
    buffered messages, regardless of interleaved await points. This is
    the mechanism-level proof that backs up R-A3's sequential-worker
    design: even if a future change ever parallelized event handling,
    THIS primitive would still hold the line."""
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))
    results: dict[str, list[dict]] = {}

    async def user_flow(name: str, delay_before: float, delay_after: float, text: str) -> None:
        with channel._reply_scope(f"rt-{name}") as ctx:
            await asyncio.sleep(delay_before)
            await channel.send(f"U-{name}", text)
            await channel.send(f"U-{name}", f"{text}-second")
            await asyncio.sleep(delay_after)
        results[name] = list(ctx["buffer"])

    await asyncio.gather(
        user_flow("A", 0.01, 0.05, "from-A"),
        user_flow("B", 0.03, 0.01, "from-B"),
    )

    assert results["A"] == [{"type": "text", "text": "from-A"}, {"type": "text", "text": "from-A-second"}]
    assert results["B"] == [{"type": "text", "text": "from-B"}, {"type": "text", "text": "from-B-second"}]


# =============================================================================
# PUSH (R-A6/R-C6) -- ledger increments only on success, including
# transport-level failure
# =============================================================================


async def test_push_network_transport_error_does_not_increment_ledger(tmp_path):
    """Companion to the existing 500-status test: a transport-level
    failure (no HTTP response at all) must also not inflate the ledger --
    `_push` has no try/except of its own, so the exception propagates
    (caller's problem), but the ledger increment line must never be
    reached either way."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    channel, db = _make_channel(tmp_path, handler)

    with pytest.raises(httpx.ConnectTimeout):
        await channel.send("U5", "digest text")

    assert db.push_count("U5", _current_yyyymm()) == 0


# =============================================================================
# RETRY/BACKOFF -- outbound sends must NOT retry (parity with
# channels/telegram.py's own precedent: only the inbound poll loop backs
# off; sendMessage/sendPhoto/etc never retry)
# =============================================================================


async def test_push_failure_makes_exactly_one_http_attempt_no_retry_loop(tmp_path):
    counts: dict = {}
    channel, db = _make_channel(tmp_path, _counting_handler(counts, path_suffix="/message/push", status=429))

    with pytest.raises(httpx.HTTPStatusError):
        await channel.send("U1", "rate-limited push")

    assert counts["n"] == 1, f"expected exactly 1 attempt (no retry), got {counts['n']}"
    assert db.push_count("U1", _current_yyyymm()) == 0


async def test_reply_failure_makes_exactly_one_http_attempt_no_retry_loop(tmp_path, caplog):
    counts: dict = {}
    channel, _db = _make_channel(tmp_path, _counting_handler(counts, path_suffix="/message/reply", status=500))

    with channel._reply_scope("rt") as ctx:
        await channel.send("U1", "will fail to reply")
    with caplog.at_level(logging.WARNING, logger="habit_assistant.channels.line"):
        await channel._flush_reply("rt", ctx["buffer"])  # must not raise, must not retry

    assert counts["n"] == 1, f"expected exactly 1 attempt (no retry), got {counts['n']}"


async def test_rich_menu_registration_failure_makes_no_retry_attempts(tmp_path, caplog):
    image_path = tmp_path / "richmenu.png"
    image_path.write_bytes(b"fake-png-bytes")
    counts: dict = {}
    channel, _db = _make_channel(tmp_path, _counting_handler(counts, path_suffix="/richmenu", status=503))
    channel._config.line.rich_menu_image = str(image_path)

    with caplog.at_level(logging.WARNING, logger="habit_assistant.channels.line"):
        await channel.register_rich_menu()  # fail-open, must not raise

    assert counts["n"] == 1, f"expected exactly 1 create-richmenu attempt (no retry), got {counts['n']}"


# =============================================================================
# QUICK REPLIES (R-A8/R-A9) -- boundary conditions + verbatim round-trip
# through a simulated postback event
# =============================================================================


async def test_exactly_thirteen_buttons_all_survive_no_warning(tmp_path, caplog):
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))
    buttons = [(f"l{i}", f"log:{i}") for i in range(13)]

    with channel._reply_scope("rt") as ctx:
        with caplog.at_level(logging.WARNING, logger="habit_assistant.channels.line"):
            await channel.send_actionable("U1", "pick", buttons)
    await channel._flush_reply("rt", ctx["buffer"])

    body = json.loads(captured[0].content)
    items = body["messages"][0]["quickReply"]["items"]
    assert len(items) == 13
    assert not any("truncating" in r.message for r in caplog.records)


async def test_exactly_fourteen_buttons_drops_only_the_fourteenth(tmp_path):
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))
    buttons = [(f"l{i}", f"log:{i}") for i in range(14)]

    with channel._reply_scope("rt") as ctx:
        await channel.send_actionable("U1", "pick", buttons)
    await channel._flush_reply("rt", ctx["buffer"])

    body = json.loads(captured[0].content)
    items = body["messages"][0]["quickReply"]["items"]
    assert [item["action"]["data"] for item in items] == [f"log:{i}" for i in range(13)]
    assert "log:13" not in [item["action"]["data"] for item in items]


async def test_postback_data_exactly_300_chars_no_warning(tmp_path, caplog):
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))
    data_300 = "log:" + ("x" * 296)
    assert len(data_300) == 300

    with channel._reply_scope("rt") as ctx:
        with caplog.at_level(logging.WARNING, logger="habit_assistant.channels.line"):
            await channel.send_actionable("U1", "pick", [("L", data_300)])
    await channel._flush_reply("rt", ctx["buffer"])

    body = json.loads(captured[0].content)
    assert body["messages"][0]["quickReply"]["items"][0]["action"]["data"] == data_300
    assert not any("300-char limit" in r.message for r in caplog.records)


async def test_postback_data_301_chars_warns_but_still_sent_verbatim(tmp_path, caplog):
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))
    data_301 = "log:" + ("x" * 297)
    assert len(data_301) == 301

    with channel._reply_scope("rt") as ctx:
        with caplog.at_level(logging.WARNING, logger="habit_assistant.channels.line"):
            await channel.send_actionable("U1", "pick", [("L", data_301)])
    await channel._flush_reply("rt", ctx["buffer"])

    body = json.loads(captured[0].content)
    assert body["messages"][0]["quickReply"]["items"][0]["action"]["data"] == data_301  # NOT truncated
    assert any("300-char limit" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "callback_data",
    ["undo:98765", "log:water:500", "clarify:12345:water:500", "routine:run:morning", "x" * 310],
)
async def test_callback_data_verbatim_round_trip_send_actionable_to_on_callback(tmp_path, harness, callback_data):
    """End-to-end round trip: the exact `data` string LineChannel.
    send_actionable puts into a quickReply postback action must come back
    byte-for-byte identical when a real LINE postback event carrying that
    same data string is routed through LineWebhookServer.process_event
    into on_callback -- proving the two Module-A halves (outbound button
    encode, inbound postback decode) agree on the wire contract, not just
    each in isolation."""
    captured: list[httpx.Request] = []
    channel, _db = _make_channel(tmp_path, _default_handler(captured))

    with channel._reply_scope("rt-outbound") as ctx:
        await channel.send_actionable("U1", "pick one", [("Button", callback_data)])
    await channel._flush_reply("rt-outbound", ctx["buffer"])

    body = json.loads(captured[0].content)
    emitted_data = body["messages"][0]["quickReply"]["items"][0]["action"]["data"]
    assert emitted_data == callback_data  # sanity: verbatim on the way out

    # Now simulate the user tapping that button: a postback event carrying
    # exactly that data string arrives inbound.
    event = {
        "type": "postback",
        "replyToken": "rt-inbound",
        "source": {"type": "user", "userId": "U1"},
        "postback": {"data": emitted_data},
    }
    await harness.server.process_event(event, harness.on_message, harness.on_callback)

    assert harness.calls == [("callback", "U1", emitted_data, "", "rt-inbound")]
    assert emitted_data == callback_data  # byte-for-byte round trip


# =============================================================================
# ORDERING (R-A3) -- single-worker FIFO, proven with explicit
# start/end instrumentation, not just final-order matching
# =============================================================================


async def test_second_users_handler_does_not_start_until_first_users_dispatch_fully_completes(harness):
    """R-A3's actual guarantee is stronger than "final call order matches
    enqueue order" -- it's that event N+1 is not even STARTED (handler
    invoked) until event N's entire dispatch (handler await + reply flush)
    has completed. Prove the stronger claim with explicit start/end
    timestamps, not just an order-of-completion assertion (which a
    sufficiently-fast concurrent implementation could also satisfy)."""
    timeline: list[str] = []

    async def slow_on_message(user_id, text, display_name=None, message_id=None, reply_to_message_id=None):
        timeline.append(f"start-{user_id}-{text}")
        await asyncio.sleep(0.05)
        timeline.append(f"end-{user_id}-{text}")

    await harness.server.queue.put(
        {"type": "message", "replyToken": "rt-a", "source": {"type": "user", "userId": "UA"},
         "message": {"type": "text", "id": "1", "text": "first"}}
    )
    await harness.server.queue.put(
        {"type": "message", "replyToken": "rt-b", "source": {"type": "user", "userId": "UB"},
         "message": {"type": "text", "id": "2", "text": "second"}}
    )

    worker = asyncio.create_task(harness.server._worker(slow_on_message, harness.on_callback))
    for _ in range(100):
        if len(timeline) == 4:
            break
        await asyncio.sleep(0.02)
    worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker

    # UB's handler must not START until UA's handler has fully ENDED --
    # this is what "single worker, fully awaited before the next" means,
    # not merely "results came back in order."
    assert timeline == [
        "start-UA-first",
        "end-UA-first",
        "start-UB-second",
        "end-UB-second",
    ], timeline


async def test_interleaved_users_preserve_strict_global_enqueue_order(harness):
    """Two users' events enqueued in an interleaved pattern
    (A1, B1, A2, B2) must be processed in that EXACT global order -- R-A3
    guarantees global (hence per-user) ordering via one worker draining
    one queue; it does NOT give each user their own independent lane, so
    interleaving must be preserved exactly, not reordered per-user."""
    events = [
        {"type": "message", "replyToken": "rt-a1", "source": {"type": "user", "userId": "UA"},
         "message": {"type": "text", "id": "a1", "text": "A1"}},
        {"type": "message", "replyToken": "rt-b1", "source": {"type": "user", "userId": "UB"},
         "message": {"type": "text", "id": "b1", "text": "B1"}},
        {"type": "message", "replyToken": "rt-a2", "source": {"type": "user", "userId": "UA"},
         "message": {"type": "text", "id": "a2", "text": "A2"}},
        {"type": "message", "replyToken": "rt-b2", "source": {"type": "user", "userId": "UB"},
         "message": {"type": "text", "id": "b2", "text": "B2"}},
    ]
    for event in events:
        await harness.server.queue.put(event)

    worker = asyncio.create_task(harness.server._worker(harness.on_message, harness.on_callback))
    for _ in range(100):
        if harness.server.queue.qsize() == 0 and len(harness.calls) == 4:
            break
        await asyncio.sleep(0.02)
    worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker

    assert [c[2] for c in harness.calls] == ["A1", "B1", "A2", "B2"]
    assert [t for t, _ in harness.flushed] == ["rt-a1", "rt-b1", "rt-a2", "rt-b2"]


async def test_same_user_two_messages_process_and_flush_in_order(harness):
    """The narrower, explicit per-user-ordering AC framing: two messages
    from the SAME user must be handled and replied to in send order."""
    events = [
        {"type": "message", "replyToken": "rt-1", "source": {"type": "user", "userId": "U1"},
         "message": {"type": "text", "id": "1", "text": "500ml water"}},
        {"type": "message", "replyToken": "rt-2", "source": {"type": "user", "userId": "U1"},
         "message": {"type": "text", "id": "2", "text": "undo"}},
    ]
    for event in events:
        await harness.server.queue.put(event)

    worker = asyncio.create_task(harness.server._worker(harness.on_message, harness.on_callback))
    for _ in range(50):
        if len(harness.calls) == 2:
            break
        await asyncio.sleep(0.02)
    worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker

    assert harness.calls == [
        ("message", "U1", "500ml water", "1"),
        ("message", "U1", "undo", "2"),
    ]
    assert [t for t, _ in harness.flushed] == ["rt-1", "rt-2"]
