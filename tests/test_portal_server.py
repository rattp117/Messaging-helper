"""SPEC-LINE-PORTAL.md §4/§5 R-INT-1/R-SEC-5/R-SEC-6 (shared surface,
admin web portal, branch `line-version`): `core/portal/server.py`'s own
unit tests -- `PortalDeps`/`PortalServer.build_app` wiring, module
registration (R-INT-1), middleware ordering (identity gate must be
OUTERMOST), the vendored-font route, and `run_portal_server`'s own
fail-open startup contract (R-SEC-6).
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from habit_assistant.config import Config
from habit_assistant.core.portal.server import PortalDeps, PortalServer, build_portal, cancel_task, run_portal_server
from habit_assistant.core.portal.stats import RingBufferHandler
from habit_assistant.storage.db import Database

OWNER = "Uowner00000000000000000000000000"


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


def _deps(tmp_path, *, owner_login: str = "") -> PortalDeps:
    config = Config.model_validate({"portal": {"enabled": True, "owner_login": owner_login}})
    db = Database(tmp_path / "habits.db")
    return PortalDeps(
        db=db,
        config=config,
        scheduler=SimpleNamespace(get_jobs=lambda: []),
        channel=SimpleNamespace(),
        stats=SimpleNamespace(started_at=None, last_event_at=None),
        ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )


# ===========================================================================
# R-INT-1: every module registers into ONE Application via register(app, deps).
# ===========================================================================


def test_build_app_calls_every_registered_module_with_the_app_and_deps(tmp_path):
    deps = _deps(tmp_path)
    calls = []

    async def _status_handler(request):
        return web.Response(text="status-ok")

    async def _users_handler(request):
        return web.Response(text="users-ok")

    def fake_status_register(app, module_deps):
        calls.append(("status", app, module_deps))
        app.router.add_get("/", _status_handler)

    def fake_users_register(app, module_deps):
        calls.append(("users", app, module_deps))
        app.router.add_get("/users", _users_handler)

    server = PortalServer(bind_host="127.0.0.1", bind_port=0, deps=deps, modules=[fake_status_register, fake_users_register])
    app = server.build_app()

    assert [c[0] for c in calls] == ["status", "users"]
    assert all(c[1] is app for c in calls)
    assert all(c[2] is deps for c in calls)
    deps.db.close()


async def test_build_app_registered_routes_are_reachable_with_correct_header(tmp_path, aiohttp_client_factory):
    deps = _deps(tmp_path)

    async def _hello_handler(request):
        return web.Response(text="hello")

    def fake_register(app, module_deps):
        del module_deps
        app.router.add_get("/", _hello_handler)

    server = PortalServer(bind_host="127.0.0.1", bind_port=0, deps=deps, modules=[fake_register])
    app = server.build_app()
    client = await aiohttp_client_factory(app)
    resp = await client.get("/", headers={"Tailscale-User-Login": "owner@example.com"})
    assert resp.status == 200
    assert await resp.text() == "hello"
    deps.db.close()


# ===========================================================================
# Middleware ordering: identity_gate must be OUTERMOST -- a headerless
# request to a module's own route (once registered) is refused BEFORE that
# handler ever runs, and a handler's own crash still gets caught by
# _error_middleware (which only runs for an AUTHENTICATED request).
# ===========================================================================


async def test_identity_gate_runs_before_a_registered_handler(tmp_path, aiohttp_client_factory):
    deps = _deps(tmp_path)
    handler_called = False

    def fake_register(app, module_deps):
        del module_deps

        async def _handler(request):
            nonlocal handler_called
            handler_called = True
            return web.Response(text="reached")

        app.router.add_get("/users", _handler)

    server = PortalServer(bind_host="127.0.0.1", bind_port=0, deps=deps, modules=[fake_register])
    client = await aiohttp_client_factory(server.build_app())
    resp = await client.get("/users")  # no header
    assert resp.status == 403
    assert handler_called is False
    deps.db.close()


async def test_error_middleware_catches_a_handler_crash_and_renders_500(tmp_path, aiohttp_client_factory):
    deps = _deps(tmp_path)

    def fake_register(app, module_deps):
        del module_deps

        async def _boom(request):
            raise RuntimeError("simulated handler crash")

        app.router.add_get("/quota", _boom)

    server = PortalServer(bind_host="127.0.0.1", bind_port=0, deps=deps, modules=[fake_register])
    client = await aiohttp_client_factory(server.build_app())
    resp = await client.get("/quota", headers={"Tailscale-User-Login": "owner@example.com"})
    assert resp.status == 500
    body = await resp.text()
    assert "Traceback" not in body
    assert "RuntimeError" not in body
    deps.db.close()


async def test_error_middleware_never_reached_for_an_unauthenticated_crash_prone_route(tmp_path, aiohttp_client_factory):
    """A handler that would crash never even gets a chance to -- proves
    identity_gate really does wrap OUTSIDE _error_middleware, not the
    other way around."""
    deps = _deps(tmp_path)

    def fake_register(app, module_deps):
        del module_deps

        async def _boom(request):
            raise RuntimeError("must never run")

        app.router.add_get("/quota", _boom)

    server = PortalServer(bind_host="127.0.0.1", bind_port=0, deps=deps, modules=[fake_register])
    client = await aiohttp_client_factory(server.build_app())
    resp = await client.get("/quota")  # no header
    assert resp.status == 403
    deps.db.close()


# ===========================================================================
# The vendored-font route (E2, option b).
# ===========================================================================


async def test_font_route_is_registered_and_gated(tmp_path, aiohttp_client_factory):
    deps = _deps(tmp_path)
    server = PortalServer(bind_host="127.0.0.1", bind_port=0, deps=deps, modules=[])
    client = await aiohttp_client_factory(server.build_app())

    resp = await client.get("/fonts/NotoSansThai-Regular.ttf")
    assert resp.status == 403

    resp = await client.get("/fonts/NotoSansThai-Regular.ttf", headers={"Tailscale-User-Login": "owner@example.com"})
    assert resp.status == 200
    assert resp.headers["content-type"].startswith("font/")
    body = await resp.read()
    assert len(body) > 1000  # a real TTF, not an empty/placeholder response
    deps.db.close()


# ===========================================================================
# R-SEC-6: run_portal_server never lets a startup failure propagate.
# ===========================================================================


async def test_run_portal_server_swallows_a_bind_failure(tmp_path):
    deps = _deps(tmp_path)

    class _BoomServer:
        async def serve(self):
            raise OSError("port already in use")

    # Must not raise -- R-SEC-6's own "must not crash the main channel loop".
    await run_portal_server(_BoomServer())
    deps.db.close()


async def test_run_portal_server_reraises_cancellation():
    class _CancelServer:
        async def serve(self):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await run_portal_server(_CancelServer())


# ===========================================================================
# build_portal: the whole conditional construction-and-launch sequence
# core/app.py delegates to, so it gains only a thin call (AC1/AC2/R-STATS-1/
# R-STATS-2/R-SEC-6).
# ===========================================================================


@pytest.fixture(autouse=True)
def _isolate_habit_assistant_logger_handlers():
    target = logging.getLogger("habit_assistant")
    original = target.handlers[:]
    yield
    target.handlers[:] = original


def test_build_portal_disabled_returns_noop_and_no_task(tmp_path):
    config = Config()  # portal.enabled=False by default
    db = Database(tmp_path / "habits.db")
    mark_event, task = build_portal(config, db, SimpleNamespace(), SimpleNamespace(), OWNER)
    assert task is None
    mark_event()  # must not raise -- a real, callable no-op
    handler_types = [type(h) for h in logging.getLogger("habit_assistant").handlers]
    assert RingBufferHandler not in handler_types
    db.close()


def test_build_portal_enabled_on_telegram_returns_noop_and_no_task(tmp_path):
    config = Config.model_validate({"portal": {"enabled": True, "bind_port": 9401}, "channel": {"type": "telegram"}})
    db = Database(tmp_path / "habits.db")
    mark_event, task = build_portal(config, db, SimpleNamespace(), SimpleNamespace(), OWNER)
    assert task is None
    mark_event()
    db.close()


async def test_build_portal_enabled_on_line_returns_a_running_task_and_installs_ring_handler(tmp_path):
    config = Config.model_validate({"portal": {"enabled": True, "bind_port": 9402}, "channel": {"type": "line"}})
    db = Database(tmp_path / "habits.db")
    mark_event, task = build_portal(config, db, SimpleNamespace(), SimpleNamespace(), OWNER)
    try:
        assert isinstance(task, asyncio.Task)
        handler_types = [type(h) for h in logging.getLogger("habit_assistant").handlers]
        assert RingBufferHandler in handler_types
        mark_event()  # a real RuntimeStats.mark_event, must not raise
    finally:
        await cancel_task(task)
        db.close()


async def test_build_portal_modules_default_to_registered_modules(tmp_path, monkeypatch):
    from habit_assistant.core.portal import server as server_module

    calls = []

    def fake_register(app, deps):
        calls.append(app)

    monkeypatch.setattr(server_module, "REGISTERED_MODULES", [fake_register])
    config = Config.model_validate({"portal": {"enabled": True, "bind_port": 9403}, "channel": {"type": "line"}})
    db = Database(tmp_path / "habits.db")
    mark_event, task = build_portal(config, db, SimpleNamespace(), SimpleNamespace(), OWNER)
    try:
        # build_app() (where register() is called) runs inside the task,
        # which only progresses once the event loop schedules it.
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.02)
        assert len(calls) == 1
    finally:
        await cancel_task(task)
        db.close()


# ===========================================================================
# cancel_task: generic cancel-and-await-swallowing-CancelledError helper.
# ===========================================================================


async def test_cancel_task_none_is_a_noop():
    await cancel_task(None)  # must not raise


async def test_cancel_task_cancels_a_running_task_and_swallows_cancelled_error():
    async def _forever():
        await asyncio.Event().wait()

    task = asyncio.create_task(_forever())
    await asyncio.sleep(0)  # let it actually start
    await cancel_task(task)  # must not raise CancelledError out to the caller
    assert task.cancelled()


async def test_cancel_task_on_an_already_finished_task_is_safe():
    async def _quick():
        return "done"

    task = asyncio.create_task(_quick())
    await task
    await cancel_task(task)  # cancel() on a finished task is a no-op; must not raise
