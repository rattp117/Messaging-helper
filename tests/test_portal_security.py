"""SPEC-LINE-PORTAL.md §4 R-SEC-3/R-SEC-4 (shared surface, admin web
portal, branch `line-version`): `core/portal/security.py:identity_gate`'s
own unit tests -- AC3, AC4, AC5, AC6, AC20's "GET and POST alike" -- plus
the 403 body's own byte-shape assertions (UI.md Screen 9's "no shell, no
stylesheet, no i18n catalog, ~150 bytes, byte-identical" requirements).

Drives the middleware directly with a minimal `web.Application` (mirrors
`tests/test_line_webhook.py`'s own `aiohttp_client_factory` convention) --
no real `LineChannel`/`Database`/scheduler needed, since the gate only
ever reads `deps.config.portal`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from habit_assistant.config import Config
from habit_assistant.core.portal.security import FORBIDDEN_BODY, identity_gate


@pytest.fixture
async def aiohttp_client_factory():
    """Local to this file, mirrors `tests/test_line_webhook.py`'s own
    fixture of the same name/shape (that file's own docstring notes
    `conftest.py` is shared-surface-owned, out of a per-module test's
    scope to add to -- the same reasoning applies here, one level up:
    this fixture is generically useful to every future portal test file,
    but adding it to `conftest.py` is this shared-surface pass's own call,
    not a page module's)."""
    clients: list[TestClient] = []

    async def make_client(app: web.Application) -> TestClient:
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    yield make_client

    for client in clients:
        await client.close()


def _build_app(*, owner_login: str = "", require_identity_header: bool = True) -> web.Application:
    config = Config.model_validate(
        {"portal": {"enabled": True, "owner_login": owner_login, "require_identity_header": require_identity_header}}
    )
    deps = SimpleNamespace(config=config)
    app = web.Application(middlewares=[identity_gate])
    app["portal_deps"] = deps

    async def _ok_get(request: web.Request) -> web.Response:
        del request
        return web.Response(text="ok-get")

    async def _ok_post(request: web.Request) -> web.Response:
        del request
        return web.Response(text="ok-post")

    app.router.add_get("/", _ok_get)
    app.router.add_post("/users/approve", _ok_post)
    return app


# ===========================================================================
# AC3 -- no Tailscale-User-Login header, require_identity_header=True -> 403.
# ===========================================================================


async def test_missing_header_refused_with_no_admin_content(aiohttp_client_factory):
    app = _build_app()
    client = await aiohttp_client_factory(app)
    resp = await client.get("/")
    assert resp.status == 403
    body = await resp.text()
    assert "ok-get" not in body
    assert body == FORBIDDEN_BODY


# ===========================================================================
# AC4/AC6 -- owner_login set, a DIFFERENT (or forged) login -> 403. Never
# treated as authorized, whatever value is sent.
# ===========================================================================


@pytest.mark.parametrize("forged_login", ["bob@example.com", "alice@example.com ", "ALICE@EXAMPLE.COM", ""])
async def test_owner_login_pin_refuses_any_non_matching_login(aiohttp_client_factory, forged_login):
    app = _build_app(owner_login="alice@example.com")
    client = await aiohttp_client_factory(app)
    headers = {"Tailscale-User-Login": forged_login} if forged_login else {}
    resp = await client.get("/", headers=headers)
    assert resp.status == 403


# ===========================================================================
# AC5 -- correct header (and matching owner_login, when set) -> proceeds.
# ===========================================================================


async def test_correct_header_with_no_owner_login_pin_proceeds(aiohttp_client_factory):
    app = _build_app(owner_login="")
    client = await aiohttp_client_factory(app)
    resp = await client.get("/", headers={"Tailscale-User-Login": "anyone@example.com"})
    assert resp.status == 200
    assert await resp.text() == "ok-get"


async def test_correct_header_matching_owner_login_pin_proceeds(aiohttp_client_factory):
    app = _build_app(owner_login="alice@example.com")
    client = await aiohttp_client_factory(app)
    resp = await client.get("/", headers={"Tailscale-User-Login": "alice@example.com"})
    assert resp.status == 200


# ===========================================================================
# AC20 -- the gate applies to POST exactly like GET.
# ===========================================================================


async def test_post_without_header_refused_no_write_reached(aiohttp_client_factory):
    app = _build_app()
    client = await aiohttp_client_factory(app)
    resp = await client.post("/users/approve", data={"chat_id": "Uxxx"})
    assert resp.status == 403
    assert await resp.text() != "ok-post"


async def test_post_with_correct_header_reaches_the_handler(aiohttp_client_factory):
    app = _build_app()
    client = await aiohttp_client_factory(app)
    resp = await client.post("/users/approve", data={"chat_id": "Uxxx"}, headers={"Tailscale-User-Login": "owner@example.com"})
    assert resp.status == 200
    assert await resp.text() == "ok-post"


# ===========================================================================
# The escape hatch: require_identity_header=False and no owner_login pin
# means the gate is fully open (documented behavior, R-SEC-3's own
# "otherwise the request proceeds").
# ===========================================================================


async def test_require_identity_header_false_and_no_pin_is_fully_open(aiohttp_client_factory):
    app = _build_app(owner_login="", require_identity_header=False)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/")
    assert resp.status == 200


async def test_require_identity_header_false_still_honors_an_owner_login_pin(aiohttp_client_factory):
    """Even with the header requirement relaxed, a configured owner_login
    pin still refuses a request that doesn't carry it -- R-SEC-3's two
    checks are independent, not "either one satisfied is enough"."""
    app = _build_app(owner_login="alice@example.com", require_identity_header=False)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/")
    assert resp.status == 403


# ===========================================================================
# UI.md Screen 9: the 403 body is a fixed, minimal, unstyled, non-i18n
# bilingual constant.
# ===========================================================================


def test_forbidden_body_carries_no_shell_no_stylesheet_no_version():
    assert "<style>" not in FORBIDDEN_BODY
    assert "nav" not in FORBIDDEN_BODY.lower()
    assert "habit assistant" not in FORBIDDEN_BODY.lower()
    assert len(FORBIDDEN_BODY.encode("utf-8")) < 250
    assert "ไม่มีสิทธิ์เข้าถึง" in FORBIDDEN_BODY
    assert "Not authorized" in FORBIDDEN_BODY


async def test_forbidden_body_is_byte_identical_regardless_of_config():
    """Same 403 body whether the header is simply missing or the
    owner_login pin failed to match -- R-SEC-3's own "never reveals WHY"."""
    app_missing = _build_app(owner_login="alice@example.com")
    app_mismatch = _build_app(owner_login="alice@example.com")
    client_missing = TestClient(TestServer(app_missing))
    client_mismatch = TestClient(TestServer(app_mismatch))
    await client_missing.start_server()
    await client_mismatch.start_server()
    try:
        r1 = await client_missing.get("/")
        r2 = await client_mismatch.get("/", headers={"Tailscale-User-Login": "bob@example.com"})
        assert await r1.text() == await r2.text() == FORBIDDEN_BODY
    finally:
        await client_missing.close()
        await client_mismatch.close()
