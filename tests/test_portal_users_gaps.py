"""Adversarial probe of the portal's mutation surface -- module USERS
(SPEC-LINE-PORTAL.md AC15-AC21, UX.md Flow B/E + §8 Q6/Q7).

Vera's independent pass on top of `tests/test_portal_users.py` (Luna's own
27 tests). This file does NOT re-test what that file already covers well
(the happy paths, the basic 403/no-write shape, the double-submit-approve
pin, the owner-block-forgery refusal) -- it targets composition of the
identity gate with mutations, XSS at every render site a display name
reaches, state-machine coherence for chat_id shapes the happy-path tests
don't exercise, and the UX.md-mandated honesty of the approve flash.

Same conventions as `tests/test_portal_users.py`: real on-disk `Database`,
real `PortalServer.build_app()`, `aiohttp.test_utils.TestClient` -- no
mocks for anything that doesn't involve a paid/external API.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core.portal import users
from habit_assistant.core.portal.server import PortalDeps, PortalServer
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "Uowner00000000000000000000000000"
MEMBER = "Umember0000000000000000000000001"
STRANGER = "Ustranger000000000000000000000002"
STRANGER2 = "Ustranger200000000000000000000003"
HEADERS = {"Tailscale-User-Login": "owner@example.com"}


class FakeChannel(Channel):
    """Same shape as test_portal_users.py's own FakeChannel."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str) -> str | None:
        self.sent.append((chat_id, text))
        # Integration item 4 (TEST-PORTAL-users.md Finding 1 fix): a
        # non-None return signals a confirmed send.
        return "sent"

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")

    def sent_to(self, chat_id: str) -> list[str]:
        return [text for cid, text in self.sent if cid == chat_id]


class RaisingChannel(Channel):
    """Simulates a LINE API outage: every send() raises. Mirrors what
    `_send_push`'s `resp.raise_for_status()` does when the HTTP call
    itself fails -- the case `access.approve_user`'s own try/except is
    written to catch (its docstring cites this exact scenario)."""

    async def send(self, chat_id: str, text: str) -> None:
        raise RuntimeError("simulated LINE API outage")

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


class SilentlyDroppingChannel(Channel):
    """Simulates the REALTIME quota-stopped gate (channels/line.py
    `_push`, `total >= cap` branch): the real gate does not raise -- it
    logs at INFO and returns, having sent nothing and incremented no
    ledger. From `access.approve_user`'s perspective the `await
    channel.send(...)` call returns completely normally. This is the
    scenario the dispatch note asked Vera to check: "does the fail-closed
    gate block the welcome message?" -- verified against the real
    `LineChannel._push` source (cap-reached branch returns without
    raising, without an exception, and without any signal back to the
    caller) and reproduced here with a minimal fake so the test doesn't
    need the full realtime-mode/push_cap/httpx-mock apparatus to make the
    point about the PORTAL's own flash-honesty contract."""

    def __init__(self) -> None:
        self.attempts: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.attempts.append((chat_id, text))
        return None  # silently drops -- exactly like the real gate.

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


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


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(OWNER, role="owner", status="active")
    yield database
    database.close()


def _config(**portal_overrides):
    portal = {"enabled": True, **portal_overrides}
    return Config.model_validate({"portal": portal, "i18n": {"language": "en"}})


async def _make_client(aiohttp_client_factory, deps):
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
# Identity gate x mutation composition -- driven through the REAL server
# and a REAL Database, not the generic middleware-only harness in
# test_portal_security.py, so a wiring bug between the gate and the actual
# handlers (e.g. a handler reading deps before the gate runs) would show up
# here even if the isolated middleware unit tests stayed green.
# ===========================================================================


def test_current_streak_is_living_not_zero_when_today_partial(db):
    """v1.3.2+line bug fix: `_current_streak` now reads through `streaks.
    display_streak` -- an owner glancing at this admin column shouldn't
    see a false 0 for a user whose streak is intact but who simply hasn't
    logged today yet (config default: water goal 2500ml)."""
    db.upsert_user(MEMBER, status="active", display_name="Nok")
    db.insert_log(LogEntry(None, MEMBER, "2026-08-24T09:00:00", "water", 2500.0, None, "2500ml"))
    db.insert_log(LogEntry(None, MEMBER, "2026-08-25T09:00:00", "water", 2500.0, None, "2500ml"))
    db.insert_log(LogEntry(None, MEMBER, "2026-08-26T09:00:00", "water", 500.0, None, "500ml"))  # today: partial

    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=FakeChannel(),
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    streak = users._current_streak(deps, MEMBER, datetime(2026, 8, 26, 20, 0, 0))
    assert streak == 2


@pytest.mark.parametrize("path,form", [
    ("/users/approve", {"chat_id": STRANGER}),
    ("/users/block", {"chat_id": MEMBER}),
    ("/users/invite", {"chat_id": "Unewuser000000000000000000000009", "confirm": "yes"}),
])
async def test_wrong_owner_login_pinned_refuses_post_with_no_write(aiohttp_client_factory, db, path, form):
    """AC4/AC6 composed with AC16-AC18: `owner_login` pinned to alice, a
    forged/different login on the POST -> 403, and -- unlike
    test_portal_security.py's own AC4/AC6 tests, which use a stub handler
    -- this drives the REAL approve/block/invite handlers and asserts the
    REAL database was not touched."""
    db.upsert_user(STRANGER, status="pending", display_name="Somchai")
    db.upsert_user(MEMBER, status="active", display_name="Nok")
    channel = FakeChannel()
    config = _config(owner_login="alice@example.com")
    deps = PortalDeps(
        db=db, config=config, scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)

    resp = await client.post(path, data=form, headers={"Tailscale-User-Login": "mallory@example.com"})
    assert resp.status == 403

    assert db.get_user(STRANGER)["status"] == "pending"
    assert db.get_user(MEMBER)["status"] == "active"
    assert db.get_user("Unewuser000000000000000000000009") is None
    assert db.recent_audit(1000) == []
    assert channel.sent == []


async def test_header_less_get_users_refused_before_any_pending_count_read(aiohttp_client_factory, db):
    """AC3 composed with R-USER-1: an unauthenticated GET /users must not
    even reach the pending-count/list_users reads -- 403 with no admin
    content, matching AC3's "no admin content in the body"."""
    db.upsert_user(STRANGER, status="pending", display_name="ShouldNotLeak")
    channel = FakeChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)
    resp = await client.get("/users")
    assert resp.status == 403
    body = await resp.text()
    assert "ShouldNotLeak" not in body
    assert STRANGER not in body


# ===========================================================================
# XSS at the mutation surface -- a malicious display_name must render
# escaped EVERYWHERE it appears: the pending card headline, the Approve/
# Block confirm disclosure body (same page, inline), the active-row table
# cell, and the post-mutation flash banner.
# ===========================================================================

XSS_NAME = '<script>alert(1)</script>'


async def test_pending_card_and_confirm_bodies_escape_script_tag_display_name(aiohttp_client_factory, db):
    db.upsert_user(STRANGER, status="pending", display_name=XSS_NAME)
    channel = FakeChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)
    resp = await client.get("/users", headers=HEADERS)
    text = await resp.text()

    assert XSS_NAME not in text, "raw <script> must never appear -- the card headline, the confirm-body text (Approve *and* Block, both reachable from the same page), and any other occurrence must all be escaped"
    assert text.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 2, (
        "expected the escaped name to appear at least twice on this page -- once in the pending card <h3> headline, "
        "once in the Approve confirm body -- both render the same raw display_name"
    )


async def test_active_row_escapes_script_tag_display_name(aiohttp_client_factory, db):
    db.upsert_user(MEMBER, status="active", display_name=XSS_NAME)
    channel = FakeChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)
    resp = await client.get("/users", headers=HEADERS)
    text = await resp.text()
    assert XSS_NAME not in text
    assert "&lt;script&gt;" in text


async def test_approve_flash_escapes_script_tag_display_name(aiohttp_client_factory, db):
    db.upsert_user(STRANGER, status="pending", display_name=XSS_NAME)
    channel = FakeChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)
    resp = await client.post("/users/approve", data={"chat_id": STRANGER}, headers=HEADERS, allow_redirects=False)
    location = resp.headers["Location"]
    follow = await client.get(location.split("#")[0], headers=HEADERS)
    text = await follow.text()
    assert XSS_NAME not in text
    assert "&lt;script&gt;" in text


async def test_block_confirm_disclosure_escapes_html_entities_in_display_name(aiohttp_client_factory, db):
    """A name that isn't a <script> tag but is still HTML-meaningful --
    ampersands and quotes -- must round-trip through the confirm body and
    the hidden-field value attribute correctly escaped, not just the
    obvious script-tag case."""
    tricky = 'Bob & "Bad" <Guy>\''
    db.upsert_user(MEMBER, status="active", display_name=tricky)
    channel = FakeChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)
    resp = await client.get("/users", headers=HEADERS)
    text = await resp.text()
    assert tricky not in text
    assert "<Guy>" not in text
    assert "&lt;Guy&gt;" in text
    assert "&amp;" in text


async def test_display_name_4000_chars_renders_without_crashing(aiohttp_client_factory, db):
    """Render-budget probe: an implausibly long display_name (LINE's own
    API caps profile display names well under this, but nothing in
    `db.upsert_user`/`users.py` enforces a length bound server-side) must
    not 500 the page. Documents a real gap: `core/access.py:
    _USERS_NAME_MAX_CHARS = 24` truncates this exact field for the chat
    `/users` command, but `core/portal/users.py` applies NO truncation to
    the pending/active listing -- see Vera's report for the recommendation
    (not a hard AC15/AC19 requirement, but a real layout/DoS-adjacent
    inconsistency worth Luna or Archi's attention)."""
    long_name = "A" * 4000
    db.upsert_user(STRANGER, status="pending", display_name=long_name)
    channel = FakeChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)
    resp = await client.get("/users", headers=HEADERS)
    assert resp.status == 200
    text = await resp.text()
    assert long_name in text, "not truncated -- the full 4000 chars render verbatim (escaped, but untruncated)"


async def test_display_name_with_rtl_override_and_zero_width_chars_renders_escaped(aiohttp_client_factory, db):
    """RTL override (U+202E) + zero-width joiner/space characters: must
    not crash the renderer and must still be HTML-escaped like any other
    string (no special-casing that could itself be a bypass vector)."""
    tricky = "‮evil​<script>‍x‬"
    db.upsert_user(STRANGER, status="pending", display_name=tricky)
    channel = FakeChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)
    resp = await client.get("/users", headers=HEADERS)
    assert resp.status == 200
    text = await resp.text()
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


# ===========================================================================
# Forged/malformed POSTs -- state-machine coherence for shapes the
# happy-path suite doesn't exercise.
# ===========================================================================


async def test_approve_explicit_empty_string_chat_id_is_rejected_with_no_write(aiohttp_client_factory, db):
    """Distinct from the existing "missing key entirely" AC21 test --
    an explicitly empty chat_id="" must hit the same chat_unknown branch,
    not some other code path (e.g. `db.get_user("")` behaving oddly)."""
    channel = FakeChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)
    resp = await client.post("/users/approve", data={"chat_id": ""}, headers=HEADERS, allow_redirects=False)
    assert resp.status == 303
    assert "err=chat_unknown" in resp.headers["Location"]
    assert db.recent_audit(1000) == []


async def test_block_explicit_empty_string_chat_id_is_rejected_with_no_write(aiohttp_client_factory, db):
    channel = FakeChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)
    resp = await client.post("/users/block", data={"chat_id": ""}, headers=HEADERS, allow_redirects=False)
    assert resp.status == 303
    assert "err=chat_unknown" in resp.headers["Location"]
    assert db.recent_audit(1000) == []


async def test_approve_targets_only_the_submitted_chat_id_not_a_different_pending_row(aiohttp_client_factory, db):
    """Two pending users exist; a POST naming the SECOND one's chat_id
    must activate only that one -- the first stays untouched. Guards
    against an id-confusion bug (e.g. iterating list_users() and acting
    on the wrong index) that a single-pending-user test can't catch."""
    db.upsert_user(STRANGER, status="pending", display_name="First")
    db.upsert_user(STRANGER2, status="pending", display_name="Second")
    channel = FakeChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)
    resp = await client.post("/users/approve", data={"chat_id": STRANGER2}, headers=HEADERS, allow_redirects=False)
    assert resp.status == 303

    assert db.get_user(STRANGER2)["status"] == "active"
    assert db.get_user(STRANGER)["status"] == "pending", "the OTHER pending row must be untouched"
    assert _audit_rows(db, target=STRANGER) == []
    assert channel.sent_to(STRANGER) == []


async def test_approve_of_an_already_blocked_user_reactivates_them(aiohttp_client_factory, db):
    """Pinning actual behavior (dispatch note: "verify what it does and
    pin it"): `handle_approve`'s only guard is `db.get_user(chat_id) is
    not None` -- it does not special-case status="blocked". A forged (or
    deliberate) approve of a blocked chat_id therefore reactivates them,
    same as approving a pending one. Not reachable from the normal UI (no
    row in the Active or Pending sections offers an Approve control for a
    blocked user), so this is only exercisable by the owner directly
    POSTing -- and since the whole portal is owner-gated, "the owner can
    manually undo their own block via a raw POST" is a reasonable
    capability, not a vulnerability. Flagging as PASS-with-note: symmetric
    with Q6's block-an-active-user capability, not contradicted by any AC,
    but worth Luna/Archi confirming it's intentional rather than
    accidental."""
    db.upsert_user(MEMBER, status="blocked", display_name="Nok")
    channel = FakeChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)
    resp = await client.post("/users/approve", data={"chat_id": MEMBER}, headers=HEADERS, allow_redirects=False)
    assert resp.status == 303
    assert "ok=approve" in resp.headers["Location"]
    assert db.get_user(MEMBER)["status"] == "active"
    rows = _audit_rows(db, target=MEMBER, action="user_approve")
    assert len(rows) == 1
    assert rows[0]["old_value"] == "blocked" and rows[0]["new_value"] == "active"


async def test_block_of_an_already_blocked_user_is_idempotent_friendly(aiohttp_client_factory, db):
    """Symmetric to test_portal_users.py's own double-submit-approve pin:
    blocking an already-blocked user is a friendly re-affirmation, not an
    error -- old_value=new_value="blocked", a second audit row written."""
    db.upsert_user(MEMBER, status="blocked", display_name="Nok")
    channel = FakeChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)
    resp = await client.post("/users/block", data={"chat_id": MEMBER}, headers=HEADERS, allow_redirects=False)
    assert resp.status == 303
    assert "ok=block" in resp.headers["Location"]
    assert db.get_user(MEMBER)["status"] == "blocked"
    rows = _audit_rows(db, target=MEMBER, action="user_block")
    assert len(rows) == 1
    assert rows[0]["old_value"] == "blocked" and rows[0]["new_value"] == "blocked"


async def test_double_submit_block_is_idempotent_friendly(aiohttp_client_factory, db):
    db.upsert_user(MEMBER, status="active", display_name="Nok")
    channel = FakeChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)
    r1 = await client.post("/users/block", data={"chat_id": MEMBER}, headers=HEADERS, allow_redirects=False)
    r2 = await client.post("/users/block", data={"chat_id": MEMBER}, headers=HEADERS, allow_redirects=False)
    assert r1.status == 303 and r2.status == 303
    assert db.get_user(MEMBER)["status"] == "blocked"
    rows = _audit_rows(db, target=MEMBER, action="user_block")
    assert len(rows) == 2


async def test_double_confirm_invite_is_idempotent_friendly(aiohttp_client_factory, db):
    """A second `confirm=yes` re-POST for the same never-seen chat_id
    (double-tap, stale back-button resubmit on the interstitial) must not
    error -- same idempotent-friendly semantics as double-submit approve,
    since `handle_invite`'s confirmed branch delegates to the identical
    `approve_user`."""
    new_id = "Unewuser000000000000000000000009"
    channel = FakeChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)
    r1 = await client.post("/users/invite", data={"chat_id": new_id, "confirm": "yes"}, headers=HEADERS, allow_redirects=False)
    r2 = await client.post("/users/invite", data={"chat_id": new_id, "confirm": "yes"}, headers=HEADERS, allow_redirects=False)
    assert r1.status == 303 and r2.status == 303
    assert db.get_user(new_id)["status"] == "active"
    rows = _audit_rows(db, target=new_id, action="user_approve")
    assert len(rows) == 2
    assert len(channel.sent_to(new_id)) == 2


async def test_get_request_to_invite_path_is_not_allowed(aiohttp_client_factory, db):
    """UX Flow E has no separate "confirm URL" to GET-navigate to directly
    -- the interstitial is rendered as the RESPONSE to an unconfirmed POST,
    not served at its own GET route. `register()` adds only `POST
    /users/invite` (pinned by test_register_adds_exactly_the_four_spec_
    routes in the happy-path file), so a bare GET to that path is refused
    by routing itself -- structurally safe by construction, verified here
    end-to-end through the real gated server rather than just asserted
    from the route table."""
    channel = FakeChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)
    resp = await client.get("/users/invite", headers=HEADERS)
    assert resp.status in (404, 405)
    assert db.recent_audit(1000) == []


async def test_invite_accepts_shape_valid_id_shorter_than_chat_commands_creation_floor(aiohttp_client_factory, db):
    """R-USER-4 (SPEC-LINE-PORTAL.md) explicitly names `access._CHAT_ID_RE`
    -- not the stricter `_is_full_id_eligible_for_creation` 33-char floor
    the chat `/approve` legacy-creation path added in line/v1.1.0
    (F1/F2 hardening, TEST-LINE-1.1.0.md) -- as Invite's own validation.
    Confirms the spec's own (narrower) contract: a 20-char "U..." token
    (shape-valid, but shorter than a real 33-char LINE id, and shorter
    than the chat command's own creation floor) IS accepted and created by
    Invite. Not a defect: Invite is a single-purpose creation form with
    its own typo-guard interstitial (UX Flow E, echoing the id back in
    large letter-spaced mono specifically for this class of mistake), so
    the chat command's prefix-collision-avoidance rationale for the
    33-char floor doesn't transfer here. Documented so a future spec
    change to R-USER-4 (tightening Invite to match the chat command) is a
    deliberate choice, not a silent behavior change this test would catch
    either way."""
    short_id = "U" + "a" * 19  # 20 chars total -- shape-valid, below the 33-char floor.
    assert len(short_id) < 33
    channel = FakeChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)
    resp = await client.post(
        "/users/invite", data={"chat_id": short_id, "confirm": "yes"}, headers=HEADERS, allow_redirects=False
    )
    assert resp.status == 303
    assert "ok=invite" in resp.headers["Location"]
    assert db.get_user(short_id)["status"] == "active"


# ===========================================================================
# CSRF posture -- structural: no cookie-based session anywhere on this
# mutation surface. The spec's boundary is tailnet + header (R-SEC-3/4),
# not a browser session; a Set-Cookie anywhere would be a second, weaker
# auth factor the spec never asked for and never documented.
# ===========================================================================


async def test_no_set_cookie_header_on_any_users_response(aiohttp_client_factory, db):
    db.upsert_user(STRANGER, status="pending", display_name="Somchai")
    channel = FakeChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)

    get_resp = await client.get("/users", headers=HEADERS)
    assert "Set-Cookie" not in get_resp.headers

    post_resp = await client.post("/users/approve", data={"chat_id": STRANGER}, headers=HEADERS, allow_redirects=False)
    assert "Set-Cookie" not in post_resp.headers

    forbidden_resp = await client.get("/users")  # no header -> 403
    assert "Set-Cookie" not in forbidden_resp.headers


# ===========================================================================
# The approval push, quota-drop interaction, and the flash-honesty
# contract UX.md §3 Flow B states as a MUST ("The flash must say so
# honestly rather than claiming a message was delivered", citing
# `portal_flash_approve_nopush`).
# ===========================================================================


async def test_approve_push_fires_exactly_once_per_approve(aiohttp_client_factory, db):
    db.upsert_user(STRANGER, status="pending", display_name="Somchai")
    channel = FakeChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)
    resp = await client.post("/users/approve", data={"chat_id": STRANGER}, headers=HEADERS, allow_redirects=False)
    assert resp.status == 303
    assert len(channel.sent_to(STRANGER)) == 1


async def test_approve_flash_honestly_reports_nopush_when_push_raises(aiohttp_client_factory, db):
    """FLIPPED (integration pass, item 4): this test used to pin a FAIL
    against UX.md §3 Flow B's explicit MUST ("If the access_granted push
    fails ... The flash must say so honestly rather than claiming a
    message was delivered. See §7 `portal_flash_approve_nopush`.").

    Fixed via `channels/line.py:LineChannel.send`'s new confirmation-
    sentinel return, threaded through `access.approve_user`'s new `bool`
    return (push confirmed sent) into `core/portal/users.py:handle_
    approve`'s flash selection (`ok=approve` vs `ok=approve_nopush`)."""
    db.upsert_user(STRANGER, status="pending", display_name="Somchai")
    channel = RaisingChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)

    resp = await client.post("/users/approve", data={"chat_id": STRANGER}, headers=HEADERS, allow_redirects=False)
    assert resp.status == 303
    assert "ok=approve_nopush" in resp.headers["Location"], (
        "the approve itself must still succeed even though the push raised, "
        "but the flash must now honestly say the push wasn't confirmed"
    )
    assert db.get_user(STRANGER)["status"] == "active", "DB is the source of truth -- must be active regardless of the push"

    follow = await client.get(resp.headers["Location"].split("#")[0], headers=HEADERS)
    text = await follow.text()
    # NOTE: `layout.escape()` renders the apostrophe as `&#x27;`, so match
    # on "notification didn" (unaffected by the entity) rather than the
    # raw English contraction.
    assert "notification didn" in text, "the honest nopush flash must render, not the unconditional 'been messaged' claim"
    assert "been messaged" not in text


async def test_approve_flash_honestly_reports_nopush_when_push_is_silently_dropped_by_quota_gate(
    aiohttp_client_factory, db
):
    """FLIPPED (integration pass, item 4): approve when the realtime quota
    is hard-stopped -- `channels/line.py:LineChannel._push`'s `total >=
    cap` branch does NOT raise (logs at INFO, maybe fires the once-per-
    month owner quota-stop alert, returns having sent nothing). This USED
    TO be indistinguishable from success from `approve_user`'s point of
    view (both returned `None`); `LineChannel.send`'s own fix (a non-None
    confirmation sentinel on an ACTUAL send, `None` on a silent drop) now
    makes it distinguishable -- `SilentlyDroppingChannel` mirrors that
    `None`-on-drop return exactly, matching the real gate's own contract.

    Reproduced with `SilentlyDroppingChannel` (no raise, matching the real
    gate exactly) rather than pulling in the full realtime-mode +
    push_cap + httpx-mock apparatus -- that machinery belongs to the QUOTA
    track's own tests; this test's job is the PORTAL-side consequence: a
    user is approved and never actually welcomed (no push reached them),
    and the owner's flash must now say so honestly."""
    db.upsert_user(STRANGER, status="pending", display_name="Somchai")
    channel = SilentlyDroppingChannel()
    deps = PortalDeps(
        db=db, config=_config(), scheduler=SimpleNamespace(get_jobs=lambda: []), channel=channel,
        stats=SimpleNamespace(started_at=None, last_event_at=None), ring=SimpleNamespace(records=lambda: []),
        owner_id=OWNER,
    )
    client = await _make_client(aiohttp_client_factory, deps)

    resp = await client.post("/users/approve", data={"chat_id": STRANGER}, headers=HEADERS, allow_redirects=False)
    assert resp.status == 303
    assert "ok=approve_nopush" in resp.headers["Location"]
    assert db.get_user(STRANGER)["status"] == "active"

    # The channel DID get an attempted call (proving the code path was
    # reached), but a real quota-stopped gate would have swallowed it
    # before any bytes left the process.
    assert len(channel.attempts) == 1

    follow = await client.get(resp.headers["Location"].split("#")[0], headers=HEADERS)
    text = await follow.text()
    assert "notification didn" in text, "the honest nopush flash must render for a silently-dropped push too"
    assert "been messaged" not in text
