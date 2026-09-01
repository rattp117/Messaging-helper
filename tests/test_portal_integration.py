"""SPEC-LINE-PORTAL.md §4 R-SEC-1/R-SEC-2/R-SEC-5/R-SEC-6/R-INT-2/R-INT-3
(shared surface, admin web portal, branch `line-version`): end-to-end
proof, through the REAL `core/app.py:async_main` wiring, that:

- with `[portal] enabled = false` (the default), nothing portal-shaped
  binds/installs at all -- AC1's own "byte-identical to the pre-portal
  baseline" (`test_portal_disabled_by_default_binds_nothing`).
- `enabled = true` on Telegram never constructs the portal -- AC2
  (`test_portal_never_constructs_on_telegram_even_if_enabled`).
- **Serve/Funnel isolation is the security boundary**: the LINE webhook's
  own `aiohttp.web.Application` (the one Tailscale Funnel would ever
  expose) structurally has ZERO admin/portal routes, and the portal's own
  `Application` rejects every header-less request on every route it has
  -- including one no page module has registered yet -- proving the gate
  runs before route resolution, not after
  (`test_line_webhook_app_has_no_admin_or_portal_routes`,
  `test_portal_app_rejects_headerless_requests_on_every_route`).
- the ring-buffer log handler installs ONLY when the portal is enabled
  (`test_ring_buffer_handler_installed_only_when_portal_enabled`).

Integration pass (line/v1.3.0, item 9) additions, once all four page
modules are registered (item 1): the full portal journey through the REAL
two-listener setup -- all four pages + mutations, header required
(`test_portal_serves_all_four_pages_with_header_and_403s_without`,
`test_portal_mutations_require_the_identity_header_through_the_real_app`);
approve-from-portal end-to-end with BOTH honest-flash outcomes
(`test_approve_from_portal_end_to_end_welcome_push_confirmed`/
`..._not_confirmed`); the digest-run overlap guard through the REAL
scheduled-job + manual-trigger wiring
(`test_digest_run_overlap_guard_manual_then_scheduled_through_real_app`);
and the diary-undo marker rendering identically on chat `/audit` and the
portal's `/audit`+`/activity`
(`test_diary_undo_marker_renders_identically_on_chat_and_portal_audit`).

Mirrors `tests/test_line_integration.py`'s own `_running_line_app`
fixture shape (real `LineChannel` + real `LineWebhookServer`, bound to a
real localhost port, outbound LINE API calls intercepted by an
`httpx.MockTransport`) -- this file builds its own small variant that
additionally turns the portal on, rather than modifying that file's
fixture (out of this shared-surface pass's scope).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import itertools
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from conftest import FakeScheduler
from habit_assistant import main as main_module
from habit_assistant.channels.line import LineChannel as RealLineChannel
from habit_assistant.config import Config
from habit_assistant.core.portal.stats import RingBufferHandler
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry
# Integration item 9: reuse the SAME signed-webhook helpers `tests/
# test_line_integration.py` already built (this codebase's own established
# convention for sharing test-module-level functions -- see e.g. `tests/
# test_line_v110_gaps.py`'s own `from test_access import (...)`) rather
# than re-implementing HMAC signing a second time.
from test_line_integration import _post_events, _postback_event, _text_event, _wait_until

OWNER = "Uowner00000000000000000000000000"
MEMBER = "Umember0000000000000000000000000"
HEADERS = {"Tailscale-User-Login": "owner@example.com"}

_PORTS = itertools.count(19901)


class _PoisonedOllamaClient:
    def __init__(self, *args, **kwargs) -> None:
        raise AssertionError("OllamaClient must never be constructed when config.ollama.enabled is False")


class _PoisonedHealthMonitor:
    def __init__(self, *args, **kwargs) -> None:
        raise AssertionError("HealthMonitor must never be constructed when config.channel.type == 'line'")


class _LineApiRecorder:
    """Integration item 9: `fail_push_for`, additive/keyword-only/defaulted
    empty -- when a `/message/push` call's own `to` matches, the mock
    transport answers `500` instead of `200`, simulating a real LINE API
    outage for exactly that recipient (used by the approve-flash-honesty
    end-to-end test to produce BOTH outcomes -- confirmed and
    not-confirmed -- against the REAL wired app, not a hand-rolled fake
    channel)."""

    def __init__(self, *, fail_push_for: set[str] | None = None) -> None:
        self.calls: list[tuple[str, dict | bytes | None]] = []
        self._fail_push_for = fail_push_for or set()

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        content_type = request.headers.get("content-type", "")
        body: dict | bytes | None
        if content_type.startswith("application/json") and request.content:
            body = json.loads(request.content)
        else:
            body = request.content
        self.calls.append((path, body))
        if path.endswith("/message/push") and isinstance(body, dict) and body.get("to") in self._fail_push_for:
            return httpx.Response(500, json={"message": "simulated LINE API outage"})
        return httpx.Response(200, json={})

    def calls_matching(self, suffix: str) -> list[dict | bytes | None]:
        return [body for path, body in self.calls if path.endswith(suffix)]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)


def _line_channel_factory(recorder: _LineApiRecorder):
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
    raise RuntimeError(f"nothing listening on {host}:{port} within {timeout}s") from last_exc


async def _port_is_closed(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return True
    writer.close()
    await writer.wait_closed()
    return False


@asynccontextmanager
async def _running_app(
    monkeypatch,
    tmp_path,
    *,
    portal_enabled: bool,
    channel_type: str = "line",
    fail_push_for: set[str] | None = None,
    digest_time: str = "20:00",
    extra_users: dict[str, str] | None = None,
):
    """Starts the REAL app (`main_module.async_main`) as a background
    task. When `channel_type == "line"`, both the LINE webhook AND
    (conditionally) the portal bind real ports; `LineChannel` is
    monkeypatched to a real `LineChannel` with a mocked httpx transport,
    same convention as `tests/test_line_integration.py`. When
    `channel_type == "telegram"`, no real network binds at all --
    `channel.run()` is faked to return immediately (this file only uses
    that mode for AC2's negative-construction proof, so it doesn't need a
    live Telegram loop).

    `fail_push_for`/`digest_time`/`extra_users`, additive/keyword-only/
    defaulted (integration item 9): let a test simulate a LINE API outage
    for a specific chat_id (the approve-flash-honesty end-to-end test),
    pin a known digest time, and pre-seed additional active users beyond
    the owner, without every existing call site needing to change."""
    line_port = next(_PORTS)
    portal_port = next(_PORTS)
    db_path = tmp_path / "habits.db"
    media_dir = tmp_path / "media"

    config_dict: dict = {
        "app": {"db_path": str(db_path), "timezone": "Asia/Bangkok"},
        "channel": {"type": channel_type},
        "ollama": {"enabled": False},
        "i18n": {"language": "en"},  # predictable substring assertions below, mirrors test_line_integration.py
        "line": {
            "public_base_url": f"http://127.0.0.1:{line_port}",
            "bind_host": "127.0.0.1",
            "bind_port": line_port,
            "media_dir": str(media_dir),
            "media_ttl_seconds": 3600,
        },
        "portal": {
            "enabled": portal_enabled,
            "bind_host": "127.0.0.1",
            "bind_port": portal_port,
            "require_identity_header": True,
        },
        "digest": {"time": digest_time, "enabled": True},
    }
    config = Config.model_validate(config_dict)

    seed_db = Database(db_path)
    seed_db.upsert_user(OWNER, role="owner", status="active")
    for chat_id, status in (extra_users or {}).items():
        seed_db.upsert_user(chat_id, role="member", status=status)
    seed_db.close()

    recorder = _LineApiRecorder(fail_push_for=fail_push_for)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module,
        "load_secrets",
        lambda **kwargs: SimpleNamespace(
            telegram_bot_token="fake-token" if channel_type == "telegram" else None,
            telegram_chat_id=OWNER if channel_type == "telegram" else None,
            line_channel_access_token="test-access-token",
            line_channel_secret="test-channel-secret",
            line_owner_user_id=OWNER,
        ),
    )
    monkeypatch.setattr(main_module, "LineChannel", _line_channel_factory(recorder))
    monkeypatch.setattr(main_module, "AsyncIOScheduler", FakeScheduler)
    monkeypatch.setattr(main_module, "OllamaClient", _PoisonedOllamaClient)
    FakeScheduler.last_instance = None

    if channel_type == "telegram":
        # AC2's negative-construction proof needs no live Telegram loop --
        # a Telegram channel whose run() raises immediately still exercises
        # every bit of startup wiring up to that point (mirrors test_line_
        # integration.py's own `_stop_after_run` pattern).
        class _StopAfterRun(Exception):
            pass

        class _FakeTelegramChannel:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def set_my_commands(self, *args, **kwargs) -> None:
                pass

            async def run(self, on_message, on_callback=None) -> None:
                raise _StopAfterRun()

            async def aclose(self) -> None:
                pass

        monkeypatch.setattr(main_module, "TelegramChannel", _FakeTelegramChannel)
        monkeypatch.setattr(main_module, "HealthMonitor", _FakeHealthMonitorNoop)
    else:
        monkeypatch.setattr(main_module, "HealthMonitor", _PoisonedHealthMonitor)

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None, migrate=False, backup=False, restore=None, yes=False)
    task = asyncio.create_task(main_module.async_main(args))
    try:
        if channel_type == "line":
            await _wait_for_port(config.line.bind_host, config.line.bind_port)
            if portal_enabled:
                await _wait_for_port(config.portal.bind_host, config.portal.bind_port)
        else:
            # Give the fake Telegram channel's immediate-raise a moment to
            # propagate through async_main's own startup wiring.
            await asyncio.sleep(0.2)
        db = Database(db_path)
        try:
            yield SimpleNamespace(
                db=db, config=config, line_port=line_port, portal_port=portal_port,
                api=recorder, scheduler=FakeScheduler.last_instance,
            )
        finally:
            db.close()
    finally:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            pass  # e.g. _StopAfterRun -- this fixture only cares about wiring, not the loop itself.


class _FakeHealthMonitorNoop:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def run(self) -> None:
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _isolate_habit_assistant_logger_handlers():
    """`RingBufferHandler` installs onto the module-level `"habit_assistant"`
    logger (not the root logger `tests/conftest.py`'s own autouse fixture
    already restores) -- snapshot/restore it here so a test that enables
    the portal doesn't leak a handler into every later test in the
    session."""
    target = logging.getLogger("habit_assistant")
    original = target.handlers[:]
    yield
    target.handlers[:] = original


# ===========================================================================
# AC1 -- disabled by default: nothing portal-shaped binds.
# ===========================================================================


async def test_portal_disabled_by_default_binds_nothing(monkeypatch, tmp_path):
    async with _running_app(monkeypatch, tmp_path, portal_enabled=False) as app:
        assert await _port_is_closed(app.config.portal.bind_host, app.config.portal.bind_port), (
            "portal.enabled=False must not bind the portal port at all (AC1)"
        )
        handler_types = [type(h) for h in logging.getLogger("habit_assistant").handlers]
        assert RingBufferHandler not in handler_types, "no RingBufferHandler installed when the portal is disabled (AC1)"


# ===========================================================================
# AC2 -- enabled=true but channel.type != "line": never constructed.
# ===========================================================================


async def test_portal_never_constructs_on_telegram_even_if_enabled(monkeypatch, tmp_path):
    async with _running_app(monkeypatch, tmp_path, portal_enabled=True, channel_type="telegram") as app:
        assert await _port_is_closed(app.config.portal.bind_host, app.config.portal.bind_port), (
            "portal.enabled=True on Telegram must still not bind the portal port (AC2)"
        )
        handler_types = [type(h) for h in logging.getLogger("habit_assistant").handlers]
        assert RingBufferHandler not in handler_types, "no RingBufferHandler installed on Telegram regardless of portal.enabled (AC2)"


# ===========================================================================
# Structural isolation proof, part 1: the LINE webhook's own Application
# (the one Funnel would ever expose publicly) has ZERO admin/portal routes.
# ===========================================================================


async def test_line_webhook_app_has_no_admin_or_portal_routes(monkeypatch, tmp_path):
    async with _running_app(monkeypatch, tmp_path, portal_enabled=True) as app:
        async with httpx.AsyncClient() as client:
            for path in ("/", "/users", "/audit", "/activity", "/quota", "/config", "/fonts/NotoSansThai-Regular.ttf"):
                resp = await client.get(f"http://127.0.0.1:{app.line_port}{path}")
                assert resp.status_code == 404, (
                    f"LINE webhook app (the publicly-Funneled port) must have NO route at {path!r} -- "
                    f"got {resp.status_code}"
                )
            # Sanity: the LINE app's own real route is still there --
            # proves the 404s above mean "route absent", not "server broken".
            resp = await client.get(f"http://127.0.0.1:{app.line_port}/media/does-not-exist.png")
            assert resp.status_code == 404  # unknown token -> 404, but a REGISTERED route (module A's own contract)


# ===========================================================================
# Structural isolation proof, part 2: the portal rejects header-less
# requests on EVERY route it has -- including a route with no page module
# registered yet, proving the gate runs before route resolution.
# ===========================================================================


async def test_portal_app_rejects_headerless_requests_on_every_route(monkeypatch, tmp_path):
    async with _running_app(monkeypatch, tmp_path, portal_enabled=True) as app:
        async with httpx.AsyncClient() as client:
            base = f"http://127.0.0.1:{app.portal_port}"
            # All four page modules are registered as of the integration
            # pass (item 1) -- "/" DOES have a real handler behind the gate
            # now, so this proves the gate refuses the request BEFORE that
            # handler ever runs (a header-less request gets 403, never the
            # real Status page), not merely that an unmatched route 403s.
            resp = await client.get(f"{base}/")
            assert resp.status_code == 403
            assert "ไม่มีสิทธิ์เข้าถึง" in resp.text and "Not authorized" in resp.text
            assert "<style>" not in resp.text, "the 403 body must never carry the portal stylesheet (fingerprinting risk)"

            # The one route THIS shared-surface pass does register (the
            # vendored font) is gated identically.
            resp = await client.get(f"{base}/fonts/NotoSansThai-Regular.ttf")
            assert resp.status_code == 403

            # POST is gated too (AC20's own "GET and POST alike").
            resp = await client.post(f"{base}/users/approve", data={"chat_id": "Uxxx"})
            assert resp.status_code == 403

            # A correctly-headered request against the font route succeeds
            # (proves the gate isn't just failing everything).
            resp = await client.get(f"{base}/fonts/NotoSansThai-Regular.ttf", headers={"Tailscale-User-Login": "owner@example.com"})
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("font/")


# ===========================================================================
# R-STATS-2: the ring buffer installs iff the portal is actually enabled.
# ===========================================================================


async def test_ring_buffer_handler_installed_only_when_portal_enabled(monkeypatch, tmp_path):
    async with _running_app(monkeypatch, tmp_path, portal_enabled=True) as app:
        del app
        handlers = [h for h in logging.getLogger("habit_assistant").handlers if isinstance(h, RingBufferHandler)]
        assert len(handlers) == 1
        assert handlers[0].capacity == 200  # PortalConfig.log_ring_size default


# ===========================================================================
# Integration item 9: the full portal journey -- all four pages + a
# mutation route, header required, through the REAL two-listener setup.
# ===========================================================================


async def test_portal_serves_all_four_pages_with_header_and_403s_without(monkeypatch, tmp_path):
    async with _running_app(monkeypatch, tmp_path, portal_enabled=True) as app:
        base = f"http://127.0.0.1:{app.portal_port}"
        async with httpx.AsyncClient() as client:
            for path in ("/", "/users", "/audit", "/activity", "/quota", "/config"):
                headered = await client.get(f"{base}{path}", headers=HEADERS)
                assert headered.status_code == 200, f"{path} must render 200 with the identity header"
                assert "<style>" in headered.text, f"{path} must render the real page, not a bare/error body"
                headerless = await client.get(f"{base}{path}")
                assert headerless.status_code == 403, f"{path} must 403 without the identity header"


async def test_portal_mutations_require_the_identity_header_through_the_real_app(monkeypatch, tmp_path):
    """AC20's own "GET and POST alike" through the REAL wired app (`tests/
    test_portal_users_gaps.py` already proves this against a bare
    `Application`; this proves it survives the real `async_main` wiring
    too) -- a mutation POST without the header is refused and performs no
    write."""
    async with _running_app(monkeypatch, tmp_path, portal_enabled=True, extra_users={MEMBER: "pending"}) as app:
        base = f"http://127.0.0.1:{app.portal_port}"
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{base}/users/approve", data={"chat_id": MEMBER})
            assert resp.status_code == 403
        assert app.db.get_user(MEMBER)["status"] == "pending", "no write must happen without the identity header"


# ===========================================================================
# Integration item 9: approve-from-portal end-to-end -- pending user ->
# approve -> welcome push attempted -> honest flash, BOTH outcomes.
# ===========================================================================


async def test_approve_from_portal_end_to_end_welcome_push_confirmed(monkeypatch, tmp_path):
    async with _running_app(monkeypatch, tmp_path, portal_enabled=True, extra_users={MEMBER: "pending"}) as app:
        base = f"http://127.0.0.1:{app.portal_port}"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base}/users/approve", data={"chat_id": MEMBER}, headers=HEADERS, follow_redirects=False
            )
            assert resp.status_code == 303
            assert resp.headers["location"].startswith("/users?ok=approve")
            assert "nopush" not in resp.headers["location"]

            follow = await client.get(f"{base}{resp.headers['location'].split('#')[0]}", headers=HEADERS)
            assert "been messaged" in follow.text

        assert app.db.get_user(MEMBER)["status"] == "active"
        approve_rows = [r for r in app.db.recent_audit(20) if r["action"] == "user_approve"]
        assert len(approve_rows) == 1
        assert approve_rows[0]["source"] == "portal"
        assert app.api.calls_matching("/message/push"), "the welcome push must have actually been attempted"


async def test_approve_from_portal_end_to_end_welcome_push_not_confirmed(monkeypatch, tmp_path):
    """Same flow, but the LINE API itself is simulated as down for MEMBER
    specifically (`fail_push_for`) -- proves the honest `portal_flash_
    approve_nopush` variant renders through the REAL wired app (`tests/
    test_portal_users_gaps.py` already proves this against hand-rolled
    channel doubles; this is the same fix exercised through the actual
    `LineChannel` + real httpx call shape)."""
    async with _running_app(
        monkeypatch, tmp_path, portal_enabled=True, extra_users={MEMBER: "pending"}, fail_push_for={MEMBER}
    ) as app:
        base = f"http://127.0.0.1:{app.portal_port}"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base}/users/approve", data={"chat_id": MEMBER}, headers=HEADERS, follow_redirects=False
            )
            assert resp.status_code == 303
            assert resp.headers["location"].startswith("/users?ok=approve_nopush")

            follow = await client.get(f"{base}{resp.headers['location'].split('#')[0]}", headers=HEADERS)
            assert "notification didn" in follow.text
            assert "been messaged" not in follow.text

        # DB is still the source of truth -- the approve succeeded regardless of the push outcome.
        assert app.db.get_user(MEMBER)["status"] == "active"
        approve_rows = [r for r in app.db.recent_audit(20) if r["action"] == "user_approve"]
        assert len(approve_rows) == 1


# ===========================================================================
# Integration item 9: the digest-run overlap guard, through the REAL
# scheduled-job wiring + the REAL manual portal trigger.
# ===========================================================================


async def test_digest_run_overlap_guard_manual_then_scheduled_through_real_app(monkeypatch, tmp_path):
    """The manual portal trigger runs FIRST; the real scheduled `daily_
    digest` job (`FakeScheduler` records the registration exactly like the
    real `AsyncIOScheduler` would -- this test calls `job.func()` directly,
    the same thing a real `CronTrigger` firing would do) runs SECOND, on
    the SAME calendar day -- must be a clean no-op, not a second fan-out."""
    async with _running_app(monkeypatch, tmp_path, portal_enabled=True, extra_users={MEMBER: "active"}) as app:
        base = f"http://127.0.0.1:{app.portal_port}"
        async with httpx.AsyncClient() as client:
            unconfirmed = await client.post(f"{base}/quota/digest-run", data={}, headers=HEADERS)
            assert unconfirmed.status_code == 200
            match = re.search(r'name="token" value="([^"]+)"', unconfirmed.text)
            assert match is not None, "the confirm interstitial must carry a one-time token"
            token = match.group(1)

            confirmed = await client.post(
                f"{base}/quota/digest-run",
                data={"confirm": "yes", "token": token},
                headers=HEADERS,
                follow_redirects=False,
            )
            assert confirmed.status_code == 303

        pushes_after_manual = len(app.api.calls_matching("/message/push"))
        assert pushes_after_manual > 0, "the manual run must have actually sent something"

        job = app.scheduler.get_job("daily_digest")
        assert job is not None
        await job.func()

        pushes_after_scheduled = len(app.api.calls_matching("/message/push"))
        assert pushes_after_scheduled == pushes_after_manual, (
            "the scheduled job must see the day already claimed by the manual run and skip -- no double-push"
        )


# ===========================================================================
# Integration item 9: the diary-undo marker end-to-end -- undo a diary log
# via the real inline-button postback path, then confirm chat /audit AND
# the portal's /audit + /activity all render the redacted marker, never
# the diary text itself.
# ===========================================================================


async def test_diary_undo_marker_renders_identically_on_chat_and_portal_audit(monkeypatch, tmp_path):
    async with _running_app(monkeypatch, tmp_path, portal_enabled=True) as app:
        diary_text = "feeling anxious about the exam tomorrow, haven't told anyone"
        app.db.insert_log(
            LogEntry(
                id=None, user_id=OWNER, ts="2026-08-31T21:00:00", category="diary",
                value_num=None, value_text=diary_text, raw_message=diary_text, habit_type="text",
            )
        )
        row = app.db.last_log(OWNER)
        assert row is not None and row["category"] == "diary"

        undo_resp = await _post_events(app.line_port, [_postback_event(OWNER, f"undo:{row['id']}", reply_token="rt-undo")])
        assert undo_resp.status_code == 200
        await _wait_until(lambda: app.api.calls_matching("/message/reply") or None)
        assert app.db.get_log(row["id"])["deleted_at"] is not None

        # Chat /audit -- the owner's own command reply.
        audit_resp = await _post_events(app.line_port, [_text_event(OWNER, "/audit", reply_token="rt-audit")])
        assert audit_resp.status_code == 200
        replies = await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if True][-1:] or None
        )
        chat_audit_text = replies[0]["messages"][0]["text"]
        assert diary_text not in chat_audit_text
        assert "text entry removed" in chat_audit_text

        # Portal /audit and /activity -- same underlying data, real routes.
        base = f"http://127.0.0.1:{app.portal_port}"
        async with httpx.AsyncClient() as client:
            portal_audit = await client.get(f"{base}/audit", headers=HEADERS)
            assert portal_audit.status_code == 200
            assert diary_text not in portal_audit.text
            assert "[text entry removed]" in portal_audit.text

            portal_activity = await client.get(f"{base}/activity", headers=HEADERS)
            assert portal_activity.status_code == 200
            assert diary_text not in portal_activity.text
