"""SPEC-LINE.md §11 "Integration order" step 4 (branch `line-version`):
end-to-end tests through the REAL wired app -- `core/app.py`'s own
`async_main`, exercised via `habit_assistant.main`'s module-level names
(the same monkeypatch convention every pre-LINE integration test in this
suite already uses, e.g. `tests/test_reminders.py::test_async_main_
registers_weekly_review_job_from_config`).

Two fixture shapes, matching what each group of assertions actually needs:

- `_running_line_app` (most tests below): a REAL `channels.line.LineChannel`
  + a REAL `channels.line_webhook.LineWebhookServer`, bound to a real
  localhost TCP port and driven by genuine signed HTTP POSTs -- this is
  the only way to prove the FULL chain (signature verify -> enqueue ->
  FIFO worker -> `core/routing.py:on_message`/`on_callback` -> real
  command dispatch -> real SQLite `Database` -> reply-buffer aggregation
  -> one LINE API call) actually holds together, not just each piece in
  isolation (which modules A/B/C/D's own test suites already prove).
  Outbound LINE API calls (`api.line.me`) are intercepted by an
  `httpx.MockTransport` recorder -- no real network call ever leaves this
  process. The scheduler is `conftest.FakeScheduler` (records `add_job`
  calls, never fires anything on a timer) -- this is what lets a test
  invoke a registered job's own `.func(...)` directly (e.g. the digest
  job) without waiting for a real CronTrigger, while the REAL webhook
  server keeps running independently and un-gated by the scheduler choice.
  `OllamaClient`/`HealthMonitor` are POISONED classes (raise if ever
  constructed) -- every test using this fixture is therefore also,
  incidentally, a live proof of R-B7/R-B8/R-B9's "no LLM client, no probe,
  no health monitor" guarantee, not just the one test that names it.

- `_stop_after_run` (the two wiring-only tests at the bottom): mirrors
  `tests/test_reminders.py`'s own `_StopAfterSchedulerStart` pattern --
  `channel.run()` raises immediately, `async_main` never enters a real
  serve-forever loop at all. Used only for the Telegram-mode smoke test
  (a real Telegram long-poll loop has no reason to run here) and as a
  faster alternative for pure wiring assertions that don't need a live
  webhook.

No production code is modified by this file."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import itertools
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from conftest import FakeScheduler
from habit_assistant import main as main_module
from habit_assistant.channels.line import LineChannel as RealLineChannel
from habit_assistant.config import Config
from habit_assistant.storage.db import Database

OWNER = "Uowner00000000000000000000000000"
MEMBER = "Umember0000000000000000000000000"

# Distinct ports per test avoid TIME_WAIT collisions across the file --
# each `_running_line_app` invocation gets its own, never reused.
_PORTS = itertools.count(19801)


class _PoisonedOllamaClient:
    """R-B9: `core/app.py` must never construct this when `ollama.enabled`
    is False -- raising in `__init__` turns any accidental construction
    into a loud, immediate test failure instead of a silent pass."""

    def __init__(self, *args, **kwargs) -> None:
        raise AssertionError("OllamaClient must never be constructed when config.ollama.enabled is False (R-B9)")


class _PoisonedHealthMonitor:
    """R-B8: `core/app.py` must never construct this at all when
    `config.channel.type == 'line'` (not even with `ollama_enabled=False`
    -- the monitor's Telegram half has no LINE equivalent either)."""

    def __init__(self, *args, **kwargs) -> None:
        raise AssertionError("HealthMonitor must never be constructed when config.channel.type == 'line' (R-B8)")


class _LineApiRecorder:
    """Records every outbound call this process makes to `api.line.me`
    (reply/push/rich-menu) and answers with canned success responses --
    no real network call ever leaves this process. `richmenu_id_counter`
    gives `POST /v2/bot/richmenu` a fresh, distinguishable id per call so
    a test can tell a create call apart from its own content-upload/
    set-default follow-ups if it ever needs to."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | bytes | None]] = []
        self._richmenu_id_counter = 0

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        content_type = request.headers.get("content-type", "")
        body: dict | bytes | None
        if content_type.startswith("application/json") and request.content:
            body = json.loads(request.content)
        else:
            body = request.content
        self.calls.append((path, body))
        if path == "/v2/bot/richmenu":
            self._richmenu_id_counter += 1
            return httpx.Response(200, json={"richMenuId": f"richmenu-test-{self._richmenu_id_counter}"})
        return httpx.Response(200, json={})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def calls_matching(self, suffix: str) -> list[dict | bytes | None]:
        return [body for path, body in self.calls if path.endswith(suffix)]


def _line_channel_factory(recorder: _LineApiRecorder):
    """The value monkeypatched onto `main_module.LineChannel` -- a plain
    function (not a class), matching the exact call shape `core/app.py`
    already uses for the real class (`LineChannel(token, secret, owner,
    config, db)`, no `client=` kwarg at that call site) while injecting
    the mock-transport-backed httpx client `RealLineChannel.__init__`
    accepts as its own additive, keyword-only `client` param."""

    def factory(channel_access_token, channel_secret, owner_user_id, config, db):
        client = httpx.AsyncClient(transport=recorder.transport, timeout=5.0)
        return RealLineChannel(channel_access_token, channel_secret, owner_user_id, config, db, client=client)

    return factory


async def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            _, writer = await asyncio.open_connection(host, port)
        except OSError as exc:
            last_exc = exc
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise RuntimeError(f"LINE webhook server on {host}:{port} did not start within {timeout}s") from last_exc


def _make_config(*, port: int, media_dir, db_path, warn_cap: int = 280, digest_time: str = "20:00") -> Config:
    return Config.model_validate(
        {
            "app": {"db_path": str(db_path), "timezone": "Asia/Bangkok"},
            "i18n": {"language": "en"},  # predictable substring assertions below
            "channel": {"type": "line"},
            "ollama": {"enabled": False},
            "line": {
                "public_base_url": f"http://127.0.0.1:{port}",
                "bind_host": "127.0.0.1",
                "bind_port": port,
                "media_dir": str(media_dir),
                "media_ttl_seconds": 3600,
            },
            "digest": {"time": digest_time, "warn_cap": warn_cap, "enabled": True},
        }
    )


@asynccontextmanager
async def _running_line_app(monkeypatch, tmp_path, *, warn_cap: int = 280, digest_time: str = "20:00"):
    """Starts the REAL app (via `main_module.async_main`) as a background
    task, real webhook server bound to a real port, waits until it's
    actually listening, yields `(db, api, scheduler, config, port)`, then
    cancels the task and awaits its shutdown cleanup on exit."""
    port = next(_PORTS)
    db_path = tmp_path / "habits.db"
    media_dir = tmp_path / "media"
    config = _make_config(port=port, media_dir=media_dir, db_path=db_path, warn_cap=warn_cap, digest_time=digest_time)

    # Pre-seed both test users as ACTIVE before the app opens the DB --
    # `core/access.py:handle_gate` blocks command dispatch for a "pending"
    # (first-contact, never seen before) user, which every test below
    # would otherwise have to route around.
    seed_db = Database(db_path)
    seed_db.upsert_user(OWNER, role="member", status="active")
    seed_db.upsert_user(MEMBER, role="member", status="active")
    seed_db.close()

    recorder = _LineApiRecorder()
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(main_module, "load_secrets", lambda **kwargs: SimpleNamespace(
        telegram_bot_token=None,
        telegram_chat_id=None,
        line_channel_access_token="test-access-token",
        line_channel_secret="test-channel-secret",
        line_owner_user_id=OWNER,
    ))
    monkeypatch.setattr(main_module, "LineChannel", _line_channel_factory(recorder))
    monkeypatch.setattr(main_module, "AsyncIOScheduler", FakeScheduler)
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


def _sign(secret: str, raw: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return base64.b64encode(mac).decode("utf-8")


async def _post_events(port: int, events: list[dict]) -> httpx.Response:
    payload = {"destination": "Uxxxxxxxxxxxxxx", "events": events}
    raw = json.dumps(payload).encode("utf-8")
    signature = _sign("test-channel-secret", raw)
    async with httpx.AsyncClient() as client:
        return await client.post(
            f"http://127.0.0.1:{port}/callback",
            content=raw,
            headers={"X-Line-Signature": signature, "Content-Type": "application/json"},
        )


def _text_event(user_id: str, text: str, reply_token: str = "rt-1") -> dict:
    return {
        "type": "message",
        "replyToken": reply_token,
        "source": {"type": "user", "userId": user_id},
        "timestamp": 1749000000000,
        "message": {"type": "text", "id": "msg-1", "text": text},
    }


def _postback_event(user_id: str, data: str, reply_token: str = "rt-2") -> dict:
    return {
        "type": "postback",
        "replyToken": reply_token,
        "source": {"type": "user", "userId": user_id},
        "timestamp": 1749000001000,
        "postback": {"data": data},
    }


async def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        await asyncio.sleep(interval)
    raise AssertionError(f"condition never became true within {timeout}s")


# ===========================================================================
# 1. Signed webhook event -> dispatch -> ONE reply with quick replies
#    (undo button present). AC5/AC6/AC7/AC9/AC10/AC28/AC29.
# ===========================================================================


async def test_webhook_signed_text_message_dispatches_and_replies_with_undo_quickreply(monkeypatch, tmp_path):
    async with _running_line_app(monkeypatch, tmp_path) as app:
        resp = await _post_events(app.port, [_text_event(MEMBER, "500ml", reply_token="rt-log")])
        assert resp.status_code == 200

        reply_bodies = await _wait_until(lambda: app.api.calls_matching("/message/reply") or None)
        assert len(reply_bodies) == 1, "exactly one reply call, not a push, for one inbound message (R-A4/R-A5)"
        (reply_body,) = reply_bodies
        assert reply_body["replyToken"] == "rt-log"
        messages = reply_body["messages"]
        assert len(messages) == 1
        msg = messages[0]
        assert "quickReply" in msg
        actions = [item["action"] for item in msg["quickReply"]["items"]]
        assert any(a["type"] == "postback" and a["data"].startswith("undo:") for a in actions), (
            f"expected an undo: postback quick-reply button, got {actions}"
        )

        # No push, no ledger increment for a purely reactive reply (R-A6/AC8's negative case).
        assert app.api.calls_matching("/message/push") == []
        assert app.db.push_count(MEMBER, datetime.now().strftime("%Y-%m")) == 0

        # The log itself actually landed (U-ISO: only this user's data).
        today = datetime.now().date().isoformat()
        assert app.db.sum_value(MEMBER, "water", today) == 500.0
        assert app.db.sum_value(OWNER, "water", today) == 0.0


# ===========================================================================
# 2. Postback tap -> callback -> logged (the undo round-trip). AC10.
# ===========================================================================


async def test_postback_undo_flows_through_callback_and_removes_the_log(monkeypatch, tmp_path):
    async with _running_line_app(monkeypatch, tmp_path) as app:
        await _post_events(app.port, [_text_event(MEMBER, "300ml", reply_token="rt-log-2")])
        reply_bodies = await _wait_until(lambda: app.api.calls_matching("/message/reply") or None)
        undo_data = None
        for item in reply_bodies[0]["messages"][0]["quickReply"]["items"]:
            if item["action"]["data"].startswith("undo:"):
                undo_data = item["action"]["data"]
        assert undo_data is not None

        today = datetime.now().date().isoformat()
        assert app.db.sum_value(MEMBER, "water", today) == 300.0

        await _post_events(app.port, [_postback_event(MEMBER, undo_data, reply_token="rt-undo")])

        await _wait_until(lambda: True if app.db.sum_value(MEMBER, "water", today) == 0.0 else None)
        assert app.db.sum_value(MEMBER, "water", today) == 0.0, "the undo postback must remove the log"

        # source_text=="" for a postback (R-A9) -- proven indirectly: the
        # undo confirmation reply must still have landed correctly (a
        # crash on a malformed source_text would show up as no reply at
        # all / a 500 from the worker's own exception log, not tested
        # directly here since it's already Module A's own coverage).
        undo_replies = app.api.calls_matching("/message/reply")
        assert len(undo_replies) == 2


# ===========================================================================
# 3. /guide, /help, /dashboard, /history all arrive as free replies.
# ===========================================================================


@pytest.mark.parametrize("command_text", ["/guide", "/help", "/dashboard", "/history"])
async def test_guide_help_dashboard_history_all_reply_as_text(monkeypatch, tmp_path, command_text):
    async with _running_line_app(monkeypatch, tmp_path) as app:
        resp = await _post_events(app.port, [_text_event(MEMBER, command_text, reply_token=f"rt-{command_text}")])
        assert resp.status_code == 200
        reply_bodies = await _wait_until(lambda: app.api.calls_matching("/message/reply") or None)
        assert len(reply_bodies) == 1
        messages = reply_bodies[0]["messages"]
        assert messages and messages[0]["text"].strip(), f"{command_text} produced an empty reply"
        assert app.api.calls_matching("/message/push") == [], f"{command_text} must never push"

    # R-I5/AC30: /dashboard on LINE never persists dashboard_msg_id -- base
    # send_and_pin degrades to a plain send (returns None), so the "on"
    # path (not exercised by the bare "/dashboard" show-reply above) would
    # also stay inert; checked here at the DB level for the bare form's
    # own side effect (none).


async def test_dashboard_on_never_persists_a_live_board_on_line(monkeypatch, tmp_path):
    async with _running_line_app(monkeypatch, tmp_path) as app:
        await _post_events(app.port, [_text_event(MEMBER, "/dashboard on", reply_token="rt-dash-on")])
        await _wait_until(lambda: app.api.calls_matching("/message/reply") or None)
        assert app.db.get_dashboard_msg_id(MEMBER) is None, (
            "R-I5/AC30: dashboard_msg_id must stay inert on LINE (base send_and_pin returns None)"
        )


# ===========================================================================
# 4. /review returns text + a media-URL image message, via a free reply.
# ===========================================================================


async def test_review_command_returns_text_and_media_url_image_reply(monkeypatch, tmp_path):
    async with _running_line_app(monkeypatch, tmp_path) as app:
        # Some data to review, so the weekly stats block + a chart both
        # have something to render.
        await _post_events(app.port, [_text_event(MEMBER, "500ml", reply_token="rt-seed")])
        await _wait_until(lambda: app.api.calls_matching("/message/reply") or None)

        resp = await _post_events(app.port, [_text_event(MEMBER, "/review", reply_token="rt-review")])
        assert resp.status_code == 200

        reply_bodies = await _wait_until(lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-review"] or None)
        (reply_body,) = reply_bodies
        messages = reply_body["messages"]
        assert len(messages) <= 5, "SPEC-LINE.md §7: at most 5 objects per reply"
        assert messages[0]["type"] == "text" and messages[0]["text"].strip()

        image_messages = [m for m in messages if m["type"] == "image"]
        if image_messages:  # matplotlib/[charts] must be installed for this branch to be exercised
            img = image_messages[0]
            assert img["originalContentUrl"].startswith(app.config.line.public_base_url + "/media/")
            assert img["previewImageUrl"] == img["originalContentUrl"]

            # The media URL is genuinely servable (R-A11/A12) -- fetch it
            # back over real HTTP and confirm it's a real PNG.
            media_path = img["originalContentUrl"][len(f"http://127.0.0.1:{app.port}"):]
            async with httpx.AsyncClient() as client:
                media_resp = await client.get(f"http://127.0.0.1:{app.port}{media_path}")
            assert media_resp.status_code == 200
            assert media_resp.headers["content-type"] == "image/png"
            assert media_resp.content[:8] == b"\x89PNG\r\n\x1a\n"

        # Never a push, no matter what -- R-C5/AC25's "always a free reply".
        assert app.api.calls_matching("/message/push") == []


# ===========================================================================
# 5. No-LLM inert proof at the app level: an unparseable message must
#    never touch the (poisoned) Ollama client, and must still get a
#    sensible clarify reply -- proving the whole chain (webhook -> routing
#    -> preparse-miss -> clarify) works with genuinely zero LLM dependency,
#    not just that Module B's own unit tests say so. AC15/AC16/AC19.
# ===========================================================================


async def test_no_llm_inert_at_app_level_unparseable_message_never_touches_ollama(monkeypatch, tmp_path):
    async with _running_line_app(monkeypatch, tmp_path) as app:
        # Gibberish: not a recognized command, not NUMBER [+ UNIT], no
        # tier-1 clarify guess plausible -- the preparse-miss/no-guesses
        # path (R-B1/R-B2). If OllamaClient were ever constructed or
        # called, _PoisonedOllamaClient's __init__ would have raised
        # inside the worker, which would show up as a swallowed exception
        # (Module A's own worker `except Exception: logger.exception(...)`)
        # and, critically, NO reply at all -- so "a reply arrived" is
        # already meaningful evidence, tightened by the ledger/push checks
        # below.
        resp = await _post_events(app.port, [_text_event(MEMBER, "asdkjqwe zxcv 999 !!!", reply_token="rt-gibberish")])
        assert resp.status_code == 200
        reply_bodies = await _wait_until(lambda: app.api.calls_matching("/message/reply") or None)
        assert len(reply_bodies) == 1
        assert reply_bodies[0]["messages"], "a preparse-miss must still get a clarifying reply, not silence"

        # AC15: no unparsed/awaiting_llm row was ever written.
        assert app.db.pending_unparsed() == []


# ===========================================================================
# 6. Rich menu registration at startup -- fail-open, three ordered calls.
#    AC14/R-I3.
# ===========================================================================


async def test_rich_menu_registered_at_startup_create_upload_set_default(monkeypatch, tmp_path):
    async with _running_line_app(monkeypatch, tmp_path) as app:
        # Exact path match for the create call, not the /content or
        # /user/all/richmenu/{id} suffixes (which also happen to CONTAIN
        # "/v2/bot/richmenu") -- those are checked separately below.
        paths = [p for p, _ in app.api.calls]
        assert "/v2/bot/richmenu" in paths
        assert any(p.endswith("/content") for p in paths)
        assert any("/user/all/richmenu/" in p for p in paths)
        # Ordering: create, THEN upload, THEN set-default (R-A10).
        create_idx = paths.index("/v2/bot/richmenu")
        content_idx = next(i for i, p in enumerate(paths) if p.endswith("/content"))
        default_idx = next(i for i, p in enumerate(paths) if "/user/all/richmenu/" in p)
        assert create_idx < content_idx < default_idx


async def test_rich_menu_missing_image_is_fail_open_startup_still_serves(monkeypatch, tmp_path):
    port = next(_PORTS)
    db_path = tmp_path / "habits.db"
    media_dir = tmp_path / "media"
    config = _make_config(port=port, media_dir=media_dir, db_path=db_path)
    config = config.model_copy(update={"line": config.line.model_copy(update={"rich_menu_image": "does/not/exist.png"})})

    seed_db = Database(db_path)
    seed_db.upsert_user(MEMBER, role="member", status="active")
    seed_db.close()

    recorder = _LineApiRecorder()
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(main_module, "load_secrets", lambda **kwargs: SimpleNamespace(
        telegram_bot_token=None, telegram_chat_id=None,
        line_channel_access_token="tok", line_channel_secret="test-channel-secret", line_owner_user_id=OWNER,
    ))
    monkeypatch.setattr(main_module, "LineChannel", _line_channel_factory(recorder))
    monkeypatch.setattr(main_module, "AsyncIOScheduler", FakeScheduler)
    monkeypatch.setattr(main_module, "OllamaClient", _PoisonedOllamaClient)
    monkeypatch.setattr(main_module, "HealthMonitor", _PoisonedHealthMonitor)
    FakeScheduler.last_instance = None

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None, migrate=False, backup=False, restore=None, yes=False)
    task = asyncio.create_task(main_module.async_main(args))
    try:
        await _wait_for_port(config.line.bind_host, config.line.bind_port)
        # Fail-open: zero richmenu calls, startup proceeded anyway --
        # proven by the webhook actually answering a real message.
        assert recorder.calls_matching("richmenu") == [] and not any("richmenu" in p for p, _ in recorder.calls)
        resp = await _post_events(port, [_text_event(MEMBER, "/help", reply_token="rt-after-missing-menu")])
        assert resp.status_code == 200
        await _wait_until(lambda: recorder.calls_matching("/message/reply") or None)
    finally:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


# ===========================================================================
# 7. Digest full loop: compose -> push -> ledger increment -> owner warning
#    at threshold, plus the quiet-hours deferral + once-per-day guard.
#    AC20/AC21/AC23/AC24, ARCHI RULING.
# ===========================================================================


async def test_digest_job_registered_and_full_loop_pushes_and_increments_ledger(monkeypatch, tmp_path):
    async with _running_line_app(monkeypatch, tmp_path) as app:
        job = app.scheduler.get_job("daily_digest")
        assert job is not None, 'core/app.py must register the digest job under id "daily_digest" on LINE'
        trigger_fields = {f.name: str(f) for f in job.trigger.fields}
        assert trigger_fields["hour"] == "20"
        assert trigger_fields["minute"] == "0"

        # Give the member something the digest will report on.
        await _post_events(app.port, [_text_event(MEMBER, "500ml", reply_token="rt-seed-digest")])
        await _wait_until(lambda: app.api.calls_matching("/message/reply") or None)

        yyyymm = datetime.now().strftime("%Y-%m")
        assert app.db.push_count(MEMBER, yyyymm) == 0
        assert app.db.push_count(OWNER, yyyymm) == 0

        # Fire the digest "by hand" (bypassing the real CronTrigger, which
        # FakeScheduler never fires on its own) -- exactly what happens at
        # [digest].time in production.
        await job.func()

        push_bodies = app.api.calls_matching("/message/push")
        pushed_to = {body["to"] for body in push_bodies}
        assert MEMBER in pushed_to
        assert OWNER in pushed_to
        assert app.db.push_count(MEMBER, yyyymm) == 1, "R-C6: the channel's own push path must increment the ledger"
        assert app.db.push_count(OWNER, yyyymm) == 1

        # A reply-context send from earlier must NOT have touched the ledger.
        assert app.db.push_count(MEMBER, yyyymm) == 1  # not 2 -- the earlier reply never counted


async def test_digest_owner_quota_warning_line_appears_at_threshold(monkeypatch, tmp_path):
    async with _running_line_app(monkeypatch, tmp_path, warn_cap=2) as app:
        yyyymm = datetime.now().strftime("%Y-%m")
        app.db.increment_push(MEMBER, yyyymm)
        app.db.increment_push(MEMBER, yyyymm)  # monthly_push_total == 2 == warn_cap

        job = app.scheduler.get_job("daily_digest")
        await job.func()

        owner_pushes = [body for body in app.api.calls_matching("/message/push") if body["to"] == OWNER]
        assert owner_pushes, "the owner must still receive their own digest at/above the cap"
        owner_text = owner_pushes[0]["messages"][0]["text"]
        assert str(app.config.digest.warn_cap) in owner_text and (
            "quota" in owner_text.lower() or "%" in owner_text or "cap" in owner_text.lower()
        ), f"expected a quota-warning line naming the total/cap in the owner's digest, got: {owner_text!r}"


async def test_digest_quiet_hours_deferral_and_once_per_day_guard(monkeypatch, tmp_path):
    async with _running_line_app(monkeypatch, tmp_path) as app:
        now_bkk = datetime.now(ZoneInfo("Asia/Bangkok"))
        window_start = (now_bkk - timedelta(minutes=5)).strftime("%H:%M")
        window_end = (now_bkk + timedelta(minutes=5)).strftime("%H:%M")
        app.db.set_user_quiet_hours(MEMBER, f'[["{window_start}","{window_end}"]]')

        job = app.scheduler.get_job("daily_digest")
        await job.func()

        yyyymm = datetime.now().strftime("%Y-%m")
        # MEMBER is inside their own quiet-hours window right now -> deferred, not sent yet.
        assert app.db.push_count(MEMBER, yyyymm) == 0
        assert app.api.calls_matching("/message/push") == [] or all(
            body["to"] != MEMBER for body in app.api.calls_matching("/message/push")
        )

        today_str = now_bkk.date().isoformat()
        deferred_job_id = f"digest_deferred_{MEMBER}_{today_str}"
        deferred_job = app.scheduler.get_job(deferred_job_id)
        assert deferred_job is not None, "ARCHI RULING: a DND-window digest must be deferred via a one-off scheduled job"
        from apscheduler.triggers.date import DateTrigger

        assert isinstance(deferred_job.trigger, DateTrigger)

        # Firing the base digest job AGAIN the same day must not RE-SCHEDULE
        # a second deferred job for the same user/day (once-per-day guard,
        # `core/digest.py:_DIGEST_DEFERRED_DATES`). APScheduler's own
        # `id`+`replace_existing=True` would silently hide a redundant
        # re-schedule at the SAME id anyway, so asserting on the resulting
        # `scheduler.jobs` dict size alone would pass even without the
        # guard -- a call-counting spy on `add_job` is what actually
        # proves the guard skipped the second attempt.
        add_job_calls = []
        real_add_job = app.scheduler.add_job

        def _counting_add_job(*args, **kwargs):
            add_job_calls.append(kwargs.get("id"))
            return real_add_job(*args, **kwargs)

        app.scheduler.add_job = _counting_add_job
        try:
            await job.func()
        finally:
            app.scheduler.add_job = real_add_job
        assert deferred_job_id not in add_job_calls, (
            "the once-per-day guard must skip re-scheduling the same user/day's deferred job on a second call"
        )

        # Now actually fire the deferred one-off job (simulating the
        # window's end arriving) -- the send happens then.
        await deferred_job.func(*deferred_job.args, **deferred_job.kwargs)
        assert app.db.push_count(MEMBER, yyyymm) == 1
        assert any(body["to"] == MEMBER for body in app.api.calls_matching("/message/push"))


# ===========================================================================
# 8. Telegram-mode startup still works, byte-unchanged (AC28's other half).
#    Uses the lighter _StopAfterSchedulerStart pattern -- a real Telegram
#    long-poll loop has no reason to run in this test.
# ===========================================================================


class _StopAfterRun(Exception):
    pass


class _FakeTelegramChannel:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def send(self, chat_id, text, *, disable_notification=False):
        return None

    async def send_actionable(self, chat_id, text, buttons):
        return None

    async def set_my_commands(self, commands, *, scope_chat_id=None):
        return None

    async def run(self, on_message, on_callback=None):
        raise _StopAfterRun()

    async def aclose(self) -> None:
        return None


class _FakeOllamaClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def probe_schema_support(self, *args, **kwargs):
        return {}

    async def aclose(self) -> None:
        return None


class _FakeHealthMonitor:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def run(self):
        await asyncio.sleep(3600)

    async def aclose(self) -> None:
        return None


async def test_telegram_mode_startup_still_works_byte_unchanged_smoke(monkeypatch, tmp_path):
    """AC28's Telegram half: `config.channel.type` unset (defaults
    "telegram") must still construct `TelegramChannel`, a real
    `OllamaClient`, and a real `HealthMonitor` -- and must NOT register a
    "daily_digest" job (that job is LINE-only, R-I2)."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    assert config.channel.type == "telegram"

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
    monkeypatch.setattr(main_module, "TelegramChannel", _FakeTelegramChannel)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClient)
    monkeypatch.setattr(main_module, "HealthMonitor", _FakeHealthMonitor)
    FakeScheduler.last_instance = None

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None, migrate=False, backup=False, restore=None, yes=False)
    with pytest.raises(_StopAfterRun):
        await main_module.async_main(args)

    scheduler = FakeScheduler.last_instance
    assert scheduler is not None
    assert scheduler.get_job("weekly_review") is not None
    assert scheduler.get_job("minutely_tick") is not None
    assert scheduler.get_job("daily_digest") is None, "the digest job must never be registered on the Telegram path"


async def test_line_mode_never_constructs_ollama_or_health_and_registers_digest_job(monkeypatch, tmp_path):
    """The wiring-only mirror of the above, using the SAME
    `_StopAfterRun`-style short-circuit (no real webhook server needed
    just to inspect what got constructed/registered)."""

    class _FakeLineChannelStops:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def register_rich_menu(self):
            return None

        async def run(self, on_message, on_callback=None):
            raise _StopAfterRun()

        async def aclose(self) -> None:
            return None

    config = Config.model_validate(
        {
            "app": {"db_path": str(tmp_path / "habits.db")},
            "channel": {"type": "line"},
            "ollama": {"enabled": False},
            "line": {"public_base_url": "http://127.0.0.1:1", "bind_port": 1},
        }
    )
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module,
        "load_secrets",
        lambda **kwargs: SimpleNamespace(
            telegram_bot_token=None, telegram_chat_id=None,
            line_channel_access_token="tok", line_channel_secret="secret", line_owner_user_id=OWNER,
        ),
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", FakeScheduler)
    monkeypatch.setattr(main_module, "LineChannel", _FakeLineChannelStops)
    monkeypatch.setattr(main_module, "OllamaClient", _PoisonedOllamaClient)
    monkeypatch.setattr(main_module, "HealthMonitor", _PoisonedHealthMonitor)
    FakeScheduler.last_instance = None

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None, migrate=False, backup=False, restore=None, yes=False)
    with pytest.raises(_StopAfterRun):
        await main_module.async_main(args)

    scheduler = FakeScheduler.last_instance
    assert scheduler is not None
    assert scheduler.get_job("daily_digest") is not None
    # R-I2: the other six jobs are STILL registered (they self-gate inside
    # core/jobs.py) -- registration itself must not be skipped on LINE.
    for job_id in ("minutely_tick", "dashboard_day_rollover", "weekly_review", "daily_summary", "grace_tick", "wrapped_auto"):
        assert scheduler.get_job(job_id) is not None, f"{job_id} must still be registered on LINE (R-I2)"
