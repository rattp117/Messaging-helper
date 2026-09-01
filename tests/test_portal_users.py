"""SPEC-LINE-PORTAL.md §4 R-USER-* (module USERS, admin web portal, branch
`line-version`): `core/portal/users.py`'s own tests -- AC15, AC16, AC17,
AC18, AC19, AC20, AC21, plus UX.md §8 Q6 (active rows carry Block too) and
Q7 (owner row unblockable, both in the UI and server-side).

Drives the REAL route path through `PortalServer.build_app()` (identity
gate + error middleware + `users.register`), against a real on-disk
`Database` (tmp_path, mirrors `tests/test_access.py`'s own convention) --
so every assertion here exercises the actual `core/access.py:approve_user`/
`block_user` write path, not a mock.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import audit
from habit_assistant.core.portal import users
from habit_assistant.core.portal.server import PortalDeps, PortalServer
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "Uowner00000000000000000000000000"
MEMBER = "Umember0000000000000000000000001"
STRANGER = "Ustranger000000000000000000000002"
HEADERS = {"Tailscale-User-Login": "owner@example.com"}


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str) -> str | None:
        self.sent.append((chat_id, text))
        # Integration item 4 (TEST-PORTAL-users.md Finding 1): a non-None
        # return signals a confirmed send, mirroring `LineChannel.send`'s
        # own updated contract -- this double always "succeeds", so the
        # portal's flash correctly renders `portal_users_flash_approved`
        # (not the nopush variant) for every test in this file.
        return "sent"

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")

    def sent_to(self, chat_id: str) -> list[str]:
        return [text for cid, text in self.sent if cid == chat_id]


@pytest.fixture
async def aiohttp_client_factory():
    """Mirrors tests/test_portal_security.py's own fixture of the same
    name/shape (conftest.py is shared-surface-owned, out of a page
    module's own scope to add to)."""
    clients: list[TestClient] = []

    async def make_client(app: web.Application) -> TestClient:
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    yield make_client

    for client in clients:
        await client.close()


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(OWNER, role="owner", status="active")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config.model_validate({"portal": {"enabled": True}, "i18n": {"language": "en"}})


@pytest.fixture
def channel():
    return FakeChannel()


@pytest.fixture
def deps(db, config, channel):
    return PortalDeps(
        db=db,
        config=config,
        scheduler=SimpleNamespace(get_jobs=lambda: []),
        channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None),
        ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )


@pytest.fixture
async def client(deps, aiohttp_client_factory):
    server = PortalServer(bind_host="127.0.0.1", bind_port=0, deps=deps, modules=[users.register])
    app = server.build_app()
    return await aiohttp_client_factory(app)


def _audit_rows(db, *, target: str | None = None, action: str | None = None):
    rows = db.recent_audit(1000)
    if target is not None:
        rows = [r for r in rows if r["target_user_id"] == target]
    if action is not None:
        rows = [r for r in rows if r["action"] == action]
    return rows


# ===========================================================================
# register() -- R-INT-1's own contract: exactly the four routes, deps unused
# at registration time (they travel per-request).
# ===========================================================================


def test_register_adds_exactly_the_four_spec_routes(deps):
    app = web.Application()
    users.register(app, deps)
    # aiohttp auto-adds a HEAD alongside every GET -- not a route this
    # module registered itself, so it's excluded from the comparison.
    routes = {(r.method, r.resource.canonical) for r in app.router.routes() if r.method != "HEAD"}
    assert routes == {
        ("GET", "/users"),
        ("POST", "/users/approve"),
        ("POST", "/users/block"),
        ("POST", "/users/invite"),
    }


# ===========================================================================
# AC15 -- pending users listed with display name (or chat_id) + controls.
# ===========================================================================


async def test_pending_user_with_name_lists_name_and_id_and_controls(client, db):
    db.upsert_user(STRANGER, status="pending", display_name="Somchai")
    resp = await client.get("/users", headers=HEADERS)
    assert resp.status == 200
    text = await resp.text()
    assert "Somchai" in text
    assert STRANGER in text
    assert 'action="/users/approve"' in text
    assert 'action="/users/block"' in text
    assert f'value="{STRANGER}"' in text


async def test_pending_user_with_no_name_shows_id_as_headline(client, db):
    db.upsert_user(STRANGER, status="pending", display_name=None)
    resp = await client.get("/users", headers=HEADERS)
    text = await resp.text()
    assert f"<h3>{STRANGER}</h3>" in text


async def test_pending_empty_state_is_affirmative_and_points_at_invite(client, db):
    resp = await client.get("/users", headers=HEADERS)
    text = await resp.text()
    assert "Nobody" in text
    assert "invite" in text.lower()


# ===========================================================================
# AC16 -- POST /users/approve: pending -> active, audit row (user_approve,
# source=portal), access_granted push, page reflects it.
# ===========================================================================


async def test_approve_activates_user_writes_audit_and_sends_push(client, db, channel):
    db.upsert_user(STRANGER, status="pending", display_name="Somchai")

    resp = await client.post("/users/approve", data={"chat_id": STRANGER}, headers=HEADERS, allow_redirects=False)
    assert resp.status == 303
    location = resp.headers["Location"]
    assert location.startswith("/users?ok=approve")
    assert location.endswith("#flash")

    assert db.get_user(STRANGER)["status"] == "active"

    rows = _audit_rows(db, target=STRANGER, action="user_approve")
    assert len(rows) == 1
    assert rows[0]["source"] == "portal"
    assert rows[0]["user_id"] == OWNER  # actor = owner

    assert channel.sent_to(STRANGER), "the newly-approved user must receive the access_granted push"

    follow = await client.get(location.split("#")[0], headers=HEADERS)
    follow_text = await follow.text()
    assert "Somchai" in follow_text
    assert "Approved" in follow_text  # config forces i18n.language="en" in this fixture


# ===========================================================================
# AC17 -- POST /users/block: any (non-owner) chat_id -> blocked, audit row.
# ===========================================================================


async def test_block_active_user_writes_audit_row(client, db):
    db.upsert_user(MEMBER, status="active", display_name="Nok")

    resp = await client.post("/users/block", data={"chat_id": MEMBER}, headers=HEADERS, allow_redirects=False)
    assert resp.status == 303
    assert resp.headers["Location"].startswith("/users?ok=block")

    assert db.get_user(MEMBER)["status"] == "blocked"
    rows = _audit_rows(db, target=MEMBER, action="user_block")
    assert len(rows) == 1
    assert rows[0]["source"] == "portal"
    assert rows[0]["user_id"] == OWNER


async def test_block_pending_user_writes_audit_row(client, db):
    db.upsert_user(STRANGER, status="pending", display_name="Ploy")
    resp = await client.post("/users/block", data={"chat_id": STRANGER}, headers=HEADERS, allow_redirects=False)
    assert resp.status == 303
    assert db.get_user(STRANGER)["status"] == "blocked"


# ===========================================================================
# AC18 -- POST /users/invite: two-step (unconfirmed -> interstitial;
# confirm=yes -> the write), never-seen chat_id -> active, source=portal.
# ===========================================================================


async def test_invite_unconfirmed_valid_shape_renders_interstitial_no_write(client, db):
    new_id = "Unewuser000000000000000000000009"
    resp = await client.post("/users/invite", data={"chat_id": new_id}, headers=HEADERS)
    assert resp.status == 200
    text = await resp.text()
    assert "wrap decide" in text
    assert "<nav>" not in text  # UI.md §3.22: no nav on the interstitial
    assert new_id in text
    assert 'name="confirm" value="yes"' in text

    assert db.get_user(new_id) is None, "the unconfirmed step must not write anything yet"


async def test_invite_confirmed_creates_active_user_with_portal_source(client, db):
    new_id = "Unewuser000000000000000000000009"
    resp = await client.post(
        "/users/invite", data={"chat_id": new_id, "confirm": "yes"}, headers=HEADERS, allow_redirects=False
    )
    assert resp.status == 303
    assert resp.headers["Location"].startswith("/users?ok=invite")

    row = db.get_user(new_id)
    assert row is not None
    assert row["status"] == "active"

    rows = _audit_rows(db, target=new_id, action="user_approve")
    assert len(rows) == 1
    assert rows[0]["source"] == "portal"


async def test_invite_invalid_shape_redirects_with_error_and_echoes_typed_value(client, db):
    resp = await client.post("/users/invite", data={"chat_id": "not-a-valid-id"}, headers=HEADERS, allow_redirects=False)
    assert resp.status == 303
    location = resp.headers["Location"]
    assert "err=chat_invalid" in location
    assert "val=not-a-valid-id" in location

    follow = await client.get(location.split("#")[0], headers=HEADERS)
    follow_text = await follow.text()
    assert 'aria-invalid="true"' in follow_text
    assert 'value="not-a-valid-id"' in follow_text


async def test_invite_invalid_shape_truncates_echoed_value_to_64_chars(client, db):
    junk = "x" * 200
    resp = await client.post("/users/invite", data={"chat_id": junk}, headers=HEADERS, allow_redirects=False)
    location = resp.headers["Location"]
    from urllib.parse import parse_qs, urlparse

    qs = parse_qs(urlparse(location).query)
    assert len(qs["val"][0]) <= 64


async def test_invite_empty_chat_id_is_rejected_with_no_write(client, db):
    resp = await client.post("/users/invite", data={"chat_id": ""}, headers=HEADERS, allow_redirects=False)
    assert resp.status == 303
    assert "err=chat_invalid" in resp.headers["Location"]
    assert db.recent_audit(1000) == []


# ===========================================================================
# AC19 -- active rows show last-log time, current streak, digest opt-out,
# language pref.
# ===========================================================================


async def test_active_row_shows_last_log_streak_digest_and_language(client, db):
    db.upsert_user(MEMBER, status="active", display_name="Nok")
    db.set_user_language(MEMBER, "th")
    db.insert_log(
        LogEntry(None, MEMBER, datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "water", 500.0, None, "500ml")
    )

    resp = await client.get("/users", headers=HEADERS)
    text = await resp.text()
    assert "Nok" in text
    assert "Streak" in text  # column header (en, per this fixture's config)
    assert "Digest" in text
    assert "Language" in text
    row_html = text.split("Nok", 1)[1].split("</tr>", 1)[0]
    # a fresh log today (the last_log read succeeded) must render as "just
    # now", never "Never logged".
    assert "just now" in row_html
    assert "Never logged" not in row_html
    assert "TH" in row_html  # language_pref, uppercased


async def test_active_row_with_no_logs_shows_never_logged(client, db):
    db.upsert_user(MEMBER, status="active", display_name="Nok")
    resp = await client.get("/users", headers=HEADERS)
    text = await resp.text()
    assert "Never logged" in text


async def test_active_row_digest_opt_out_renders_off(client, db):
    db.upsert_user(MEMBER, status="active", display_name="Nok")
    db.set_digest_opt_out(MEMBER, True)
    resp = await client.get("/users", headers=HEADERS)
    text = await resp.text()
    row_html = text.split("Nok", 1)[1].split("</tr>", 1)[0]
    assert ">off<" in row_html


# ===========================================================================
# Q6 -- active (non-owner) rows carry a Block control.
# ===========================================================================


async def test_non_owner_active_row_has_block_control(client, db):
    db.upsert_user(MEMBER, status="active", display_name="Nok")
    resp = await client.get("/users", headers=HEADERS)
    text = await resp.text()
    row_html = text.split("Nok", 1)[1].split("</tr>", 1)[0]
    assert 'action="/users/block"' in row_html
    assert f'value="{MEMBER}"' in row_html


# ===========================================================================
# Q7 -- the owner's own row: "You (owner)", no Block control in the HTML,
# AND POST /users/block on deps.owner_id is refused server-side (the
# omission alone is not the guard).
# ===========================================================================


async def test_owner_row_renders_as_you_owner_with_no_block_control(client, db):
    resp = await client.get("/users", headers=HEADERS)
    text = await resp.text()
    assert "You (owner)" in text
    owner_row_html = text.split("You (owner)", 1)[1].split("</tr>", 1)[0]
    assert 'action="/users/block"' not in owner_row_html


async def test_forged_post_block_on_owner_row_is_refused_no_write_no_audit(client, db):
    """Adversarial: a forged POST /users/block with chat_id=owner, bypassing
    the fact that the UI never renders this control at all."""
    resp = await client.post("/users/block", data={"chat_id": OWNER}, headers=HEADERS, allow_redirects=False)
    assert resp.status == 303
    assert "err=block_owner" in resp.headers["Location"]

    assert db.get_user(OWNER)["status"] == "active"  # unchanged
    assert _audit_rows(db, target=OWNER, action="user_block") == []


# ===========================================================================
# AC20 -- no/invalid identity header on POST /users/* -> 403, no DB write.
# ===========================================================================


@pytest.mark.parametrize("path", ["/users/approve", "/users/block", "/users/invite"])
async def test_post_without_identity_header_is_refused_with_no_write(client, db, path):
    db.upsert_user(STRANGER, status="pending", display_name="Somchai")
    resp = await client.post(path, data={"chat_id": STRANGER})  # no headers
    assert resp.status == 403
    assert db.get_user(STRANGER)["status"] == "pending"
    assert db.recent_audit(1000) == []


async def test_get_users_without_identity_header_is_refused(client):
    resp = await client.get("/users")
    assert resp.status == 403


# ===========================================================================
# AC21 -- missing/unresolvable chat_id -> localized inline error, 303 back,
# no write, no audit row.
# ===========================================================================


async def test_approve_missing_chat_id_is_rejected_with_no_write(client, db):
    resp = await client.post("/users/approve", data={}, headers=HEADERS, allow_redirects=False)
    assert resp.status == 303
    assert "err=chat_unknown" in resp.headers["Location"]
    assert db.recent_audit(1000) == []


async def test_approve_nonexistent_chat_id_is_rejected_with_no_write(client, db):
    """Adversarial: approve of an id that has never contacted the bot and
    was never invited -- must be refused as unknown, not silently created
    (that's Invite's job, via its own typo-safe interstitial)."""
    ghost = "Ughost0000000000000000000000003"
    resp = await client.post("/users/approve", data={"chat_id": ghost}, headers=HEADERS, allow_redirects=False)
    assert resp.status == 303
    assert "err=chat_unknown" in resp.headers["Location"]
    assert db.get_user(ghost) is None
    assert db.recent_audit(1000) == []


async def test_block_nonexistent_chat_id_is_rejected_with_no_write(client, db):
    ghost = "Ughost0000000000000000000000003"
    resp = await client.post("/users/block", data={"chat_id": ghost}, headers=HEADERS, allow_redirects=False)
    assert resp.status == 303
    assert "err=chat_unknown" in resp.headers["Location"]
    assert db.recent_audit(1000) == []


# ===========================================================================
# Double-submit idempotency (access layer's existing semantics, pinned).
# ===========================================================================


async def test_double_submit_approve_is_idempotent_friendly_not_an_error(client, db, channel):
    """Two identical POST /users/approve for the same chat_id: the SECOND
    submission (e.g. a slow network double-tap, or a stale back-button
    resubmit) must not error or corrupt state -- `access.approve_user`'s
    own semantics (a plain upsert) make this a harmless re-affirmation:
    the row stays active, a SECOND audit row is written (old_value=
    new_value="active"), and the access_granted push fires again. Pinning
    this exact behavior so a future access.py change can't silently
    change it without a test noticing."""
    db.upsert_user(STRANGER, status="pending", display_name="Somchai")

    resp1 = await client.post("/users/approve", data={"chat_id": STRANGER}, headers=HEADERS, allow_redirects=False)
    assert resp1.status == 303
    resp2 = await client.post("/users/approve", data={"chat_id": STRANGER}, headers=HEADERS, allow_redirects=False)
    assert resp2.status == 303  # NOT a 4xx/5xx -- a friendly redirect both times

    assert db.get_user(STRANGER)["status"] == "active"
    rows = _audit_rows(db, target=STRANGER, action="user_approve")  # newest-first (db.recent_audit)
    assert len(rows) == 2
    assert all(r["source"] == "portal" for r in rows)
    assert rows[0]["old_value"] == "active" and rows[0]["new_value"] == "active"  # 2nd call: re-affirms active
    assert rows[1]["old_value"] == "pending" and rows[1]["new_value"] == "active"  # 1st call: the real transition
    assert len(channel.sent_to(STRANGER)) == 2, (
        "the push fires again on the second approve, matching approve_user's own per-call contract"
    )


# ===========================================================================
# audit.Source vocabulary -- the gap this pass closed.
# ===========================================================================


def test_portal_is_a_recognized_audit_source():
    assert "portal" in audit.SOURCES
