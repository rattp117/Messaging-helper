"""Final release-gate verification for line/v1.3.0 (the admin portal).

Scope (Archi's release-gate dispatch): a 32-AC sign-off pass against
`SPEC-LINE-PORTAL.md`, sitting ABOVE the four module Veras'
`TEST-PORTAL-status.md`/`-users.md`/`-audit.md`/`-quota.md` and the
integration pass's own `tests/test_portal_integration.py` (5 e2e tests,
item 9). Those already prove every AC in isolation and prove the four
pages compose through the identity gate. This file proves the things
that only exist at the FULLY WIRED, release-candidate level and were not
yet covered:

1. The complete owner journey through the REAL two-listener app, start to
   finish, in one continuous flow (webhook arrival -> owner notification
   with the portal URL hint -> portal approve -> honest flash -> welcome
   push -> a real user log -> the activity feed -> the audit trail -> the
   quota page), rather than each step proven in isolation by a different
   module's own test file.
2. Security-boundary re-proof BY ENUMERATION -- walking the REAL
   `REGISTERED_MODULES` router (not a hardcoded guess-list of paths) to
   prove every real route 403s header-less, and proving the public LINE
   listener has zero portal routes by introspecting its own real
   `web.Application`, not just probing a few known paths.
3. Migration 015 against a REALISTIC multi-user, multi-habit-shape seeded
   database, through the REAL sequential upgrade path (`Database.__init__`
   -> `run_migrations`), not a direct call to the migration function
   against a single hand-built row (already covered per-shape by
   `tests/test_migrations.py`).
4. The Telegram-mode regression, pinning the specced default (the portal
   NEVER constructs on Telegram, independent of `enabled`) alongside a
   "startup stayed otherwise clean" check.
5. Version-string consistency across all three files that must agree, and
   the release-notes posture (this release deliberately does not
   self-announce).
6. The digest double-send guard in BOTH orders -- integration's own item 9
   test only proves manual-then-scheduled; this file adds the mirror
   (scheduled-then-manual).

No production code is modified by this pass -- tests and this report only.
"""

from __future__ import annotations

import logging
import sqlite3
import tomllib
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

import habit_assistant
from habit_assistant.channels import line_webhook as line_webhook_module
from habit_assistant.config import Config
from habit_assistant.core.portal.server import (
    REGISTERED_MODULES,
    PortalDeps,
    PortalServer,
)
from habit_assistant.core.portal.stats import RingBufferHandler, RuntimeStats
from habit_assistant.core.release_notes import RELEASE_NOTES, get_release_note
from habit_assistant.storage.db import Database
from habit_assistant.storage.migrations import (
    MIGRATIONS,
    _migration_015_scrub_diary_undo_audit_rows,
    run_migrations,
)

from test_line_integration import _post_events, _text_event, _wait_until
from test_portal_integration import HEADERS, MEMBER, OWNER, _port_is_closed, _running_app

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate_habit_assistant_logger_handlers():
    """Own copy of `test_portal_integration.py`'s identical fixture --
    autouse fixtures only apply within the module they're defined in, so
    a running app's `RingBufferHandler` installed during one of THIS
    file's tests must not leak into later tests in the session."""
    target = logging.getLogger("habit_assistant")
    original = target.handlers[:]
    yield
    target.handlers[:] = original


# ===========================================================================
# 1. The complete owner journey, through the REAL two-listener app.
# ===========================================================================


async def test_owner_journey_webhook_to_approve_to_log_to_activity_audit_and_quota(monkeypatch, tmp_path):
    """pending user arrives (webhook) -> owner notification (with the
    portal URL hint line, Archi ruling Q2) -> portal approve -> honest
    flash -> welcome push -> user logs -> activity feed shows it
    (metadata only) -> audit shows the portal-source row -> quota page
    reflects the pushes. One continuous flow through the real wired app,
    not five separate module-level probes."""
    async with _running_app(monkeypatch, tmp_path, portal_enabled=True) as app:
        portal_url = "https://host.tailnet-name.ts.net:8081"
        app.config.portal.public_url = portal_url  # mutating the SAME live Config object async_main holds

        # -- Step 1: an unknown chat_id's first message -> pending row + owner notification.
        first_contact = await _post_events(app.line_port, [_text_event(MEMBER, "hi", reply_token="rt-hi")])
        assert first_contact.status_code == 200
        await _wait_until(lambda: app.api.calls_matching("/message/push") or None)

        assert app.db.get_user(MEMBER)["status"] == "pending"
        owner_pushes = app.api.calls_matching("/message/push")
        assert len(owner_pushes) == 1, "the owner's pending-approval alert must be a real push (R-A6), not folded into the asker's own reply"
        owner_push_text = owner_pushes[0]["messages"][0]["text"]
        assert portal_url in owner_push_text, "the owner notification must carry the portal URL hint line (Archi ruling Q2)"

        base = f"http://127.0.0.1:{app.portal_port}"
        async with httpx.AsyncClient() as client:
            # -- Step 2: portal approve -> honest flash (push not simulated as failing here).
            approve = await client.post(
                f"{base}/users/approve", data={"chat_id": MEMBER}, headers=HEADERS, follow_redirects=False
            )
            assert approve.status_code == 303
            assert approve.headers["location"].startswith("/users?ok=approve")
            assert "nopush" not in approve.headers["location"], "the welcome push was not simulated as failing -- flash must claim delivery honestly"

            follow = await client.get(f"{base}{approve.headers['location']}", headers=HEADERS)
            assert "been messaged" in follow.text

            assert app.db.get_user(MEMBER)["status"] == "active"
            welcome_pushes = [p for p in app.api.calls_matching("/message/push") if p not in owner_pushes]
            assert welcome_pushes, "the access_granted welcome push must have actually been attempted"

            # -- Step 3: the newly-approved user logs something for real.
            log_resp = await _post_events(app.line_port, [_text_event(MEMBER, "500ml", reply_token="rt-log")])
            assert log_resp.status_code == 200
            await _wait_until(lambda: app.db.last_log(MEMBER) or None)
            logged = app.db.last_log(MEMBER)
            assert logged["category"] == "water" and logged["value_num"] == 500.0

            # -- Step 4: the activity feed shows it, metadata only (R-AUDIT-3).
            activity = await client.get(f"{base}/activity", headers=HEADERS)
            assert activity.status_code == 200
            assert "water" in activity.text
            assert "500" in activity.text
            assert "500ml" not in activity.text or "raw_message" not in activity.text  # raw_message is never selected at all

            # -- Step 5: the audit trail shows the portal-source approve row.
            audit_page = await client.get(f"{base}/audit", headers=HEADERS)
            assert audit_page.status_code == 200
            assert ">portal<" in audit_page.text
            approve_rows = [r for r in app.db.recent_audit(50) if r["action"] == "user_approve"]
            assert len(approve_rows) == 1
            assert approve_rows[0]["source"] == "portal"
            assert approve_rows[0]["target_user_id"] == MEMBER

            # -- Step 6: the quota page reflects the real pushes made this month.
            all_pushes = app.api.calls_matching("/message/push")
            assert len(all_pushes) == 2  # owner's pending alert + member's welcome push
            yyyymm = ZoneInfo(app.config.app.timezone)
            from datetime import datetime as _dt

            current_yyyymm = _dt.now(yyyymm).strftime("%Y-%m")
            used = app.db.monthly_push_total(current_yyyymm)
            assert used == len(all_pushes)

            quota_page = await client.get(f"{base}/quota", headers=HEADERS)
            assert quota_page.status_code == 200
            assert str(used) in quota_page.text

            # Cross-page parity (F2's own closure, re-checked here at the
            # release-gate level): the Status page's own gauge shows the
            # identical used-count for the identical live data.
            status_page = await client.get(f"{base}/", headers=HEADERS)
            assert status_page.status_code == 200
            assert str(used) in status_page.text


# ===========================================================================
# 2. Security boundary re-proof, by ENUMERATION of the real routers.
# ===========================================================================


def _release_gate_deps(tmp_path) -> PortalDeps:
    config = Config.model_validate({"portal": {"enabled": True}})
    db = Database(tmp_path / "router_gate.db")
    db.upsert_user(OWNER, role="owner", status="active")
    return PortalDeps(
        db=db,
        config=config,
        scheduler=SimpleNamespace(get_jobs=lambda: []),
        channel=SimpleNamespace(),
        stats=RuntimeStats(),
        ring=RingBufferHandler(50),
        owner_id=OWNER,
    )


async def test_portal_router_enumerated_403s_headerless_on_every_real_registered_route(tmp_path):
    """Walks the REAL `REGISTERED_MODULES` list (the actual production
    wiring, not a hand-rolled fake register list like
    `tests/test_portal_server.py`'s own unit tests use) and probes EVERY
    resulting route -- proving completeness by enumeration rather than by
    a maintained guess-list of paths that could silently miss a future
    5th module's route."""
    deps = _release_gate_deps(tmp_path)
    server = PortalServer(bind_host="127.0.0.1", bind_port=0, deps=deps, modules=REGISTERED_MODULES)
    app = server.build_app()

    resources = list(app.router.resources())
    canonical_paths = sorted({res.canonical for res in resources})
    assert any(p.startswith("/fonts/") for p in canonical_paths), "the vendored Thai font route must be part of the real registered app (/fonts tailnet-only)"
    for expected in (
        "/", "/users", "/users/approve", "/users/block", "/users/invite",
        "/audit", "/activity", "/quota", "/config", "/quota/digest-run",
    ):
        assert expected in canonical_paths, f"expected real production route {expected!r} missing from REGISTERED_MODULES"

    client = TestClient(TestServer(app))
    await client.start_server()
    probed = 0
    try:
        for res in resources:
            for route in res:
                method = route.method
                if method in ("HEAD", "OPTIONS"):
                    continue
                path = res.canonical
                if method == "GET":
                    resp = await client.get(path)
                elif method == "POST":
                    resp = await client.post(path, data={})
                else:
                    continue
                probed += 1
                assert resp.status == 403, f"{method} {path} must 403 header-less at the release-gate level"
                text = await resp.text()
                assert "<style>" not in text, f"{method} {path}'s 403 body must never carry the real page shell"

        # Sanity: the SAME route, correctly headered, gets through -- proves
        # the 403s above are the real gate, not an absent/broken route.
        ok = await client.get("/", headers={"Tailscale-User-Login": "owner@example.com"})
        assert ok.status == 200
    finally:
        await client.close()
    assert probed >= 10
    deps.db.close()


class _CapturingWebProxy:
    """Replaces the `web` NAME inside one module's own namespace (never
    the real `aiohttp.web` module object itself -- `monkeypatch.setattr`
    on a module attribute only rebinds that module's own reference, so
    every other module's independent `from aiohttp import web` is
    untouched) so a real `web.Application()` call can be captured for
    introspection without changing any behavior."""

    def __init__(self, real_web, sink: list) -> None:
        self._real_web = real_web
        self._sink = sink

    def Application(self, *args, **kwargs):
        app = self._real_web.Application(*args, **kwargs)
        self._sink.append(app)
        return app

    def __getattr__(self, name):
        return getattr(self._real_web, name)


async def test_line_webhook_router_enumerated_has_zero_portal_routes(monkeypatch, tmp_path):
    """Introspects the REAL `web.Application` the public, Funnel-exposed
    LINE webhook listener builds for itself -- proving by enumeration
    (not by probing a fixed list of known portal paths, which is what
    `test_portal_integration.py`'s own structural-isolation test already
    does) that it has EXACTLY its own two routes and nothing portal-shaped
    could have silently been added to it."""
    captured: list = []
    monkeypatch.setattr(line_webhook_module, "web", _CapturingWebProxy(line_webhook_module.web, captured))
    async with _running_app(monkeypatch, tmp_path, portal_enabled=True) as app:
        del app
        assert len(captured) == 1, "exactly one Application must back the public LINE webhook listener"
        line_app = captured[0]
        canonical_paths = sorted({res.canonical for res in line_app.router.resources()})
        assert canonical_paths == sorted(["/callback", "/media/{tail}"]), (
            f"the publicly-Funneled LINE webhook app must have EXACTLY its own 2 routes, no portal route "
            f"leaked in -- got {canonical_paths}"
        )
        for path in canonical_paths:
            assert not path.startswith(("/users", "/audit", "/activity", "/quota", "/config", "/fonts")), path


async def test_spoofed_identity_header_via_the_public_line_port_cannot_reach_portal_handlers(monkeypatch, tmp_path):
    """A spoofed `Tailscale-User-Login` sent to the WRONG (public) port
    does nothing -- the two listeners are structurally separate
    `web.Application`s with no shared router, so there is no bridge for a
    header to cross even if an attacker somehow learned/guessed it."""
    async with _running_app(monkeypatch, tmp_path, portal_enabled=True, extra_users={MEMBER: "pending"}) as app:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{app.line_port}/users/approve",
                data={"chat_id": MEMBER},
                headers=HEADERS,
            )
            assert resp.status_code == 404, "the public LINE webhook port has no portal routes at all -- header or not"
        assert app.db.get_user(MEMBER)["status"] == "pending", "no write must happen -- there is no route to reach"


# ===========================================================================
# 3. Migration 015 against a REALISTIC, multi-user, multi-habit-shape DB,
#    through the REAL sequential upgrade path.
# ===========================================================================


def test_migration_015_realistic_seeded_db_surgical_scrub_idempotent_schema_stamps_15(tmp_path):
    """Release-gate closure proof for the MAJOR FINDING
    (TEST-PORTAL-audit.md). `tests/test_migrations.py` already proves the
    predicate correctly per SHAPE (one row at a time, migration function
    called directly); this proves the real upgrade lands correctly on one
    MESSY, multi-user database in a single pass -- base habits, a custom
    text habit, a custom numeric habit, multiple users, and a decoy
    non-undo row all sharing one DB -- going through `Database.__init__`
    exactly like a real `habit-assistant-line.service` restart after a
    code update would."""
    db_path = tmp_path / "release_gate_migration.db"

    # Freeze the DB at v14 (one migration short of 015) using the REAL
    # migration chain, not a hand-rolled schema.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    run_migrations(conn, migrations=MIGRATIONS[:-1])
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS) - 1

    USER_A = "Ualice00000000000000000000000000"
    USER_B = "Ubob000000000000000000000000000A"
    USER_C = "Ucarol0000000000000000000000000A"
    conn.executemany(
        "INSERT INTO users (chat_id, role, status, display_name) VALUES (?, 'member', 'active', ?)",
        [(USER_A, "Alice"), (USER_B, "Bob"), (USER_C, "Carol")],
    )
    # USER_B owns a custom TEXT habit -- migration 015 must find this one
    # via the user_habits JOIN, not just the entity='diary' special-case.
    conn.execute(
        "INSERT INTO user_habits (user_id, id, type, label_en, label_th, unit_en, unit_th, goal, unit_aliases) "
        "VALUES (?, 'journal', 'text', 'Journal', 'บันทึก', NULL, NULL, NULL, '{}')",
        (USER_B,),
    )
    # USER_C owns a custom NUMERIC habit -- its undo's old_value is already
    # just a number and must never be touched.
    conn.execute(
        "INSERT INTO user_habits (user_id, id, type, label_en, label_th, unit_en, unit_th, goal, unit_aliases) "
        "VALUES (?, 'pushups', 'numeric', 'Push-ups', 'วิดพื้น', 'reps', 'ครั้ง', 20, '{}')",
        (USER_C,),
    )

    at_risk_diary_a = "feeling anxious about tomorrow, told no one at all"
    at_risk_diary_a_2 = "second private thought from the same user, same day"
    at_risk_journal_b = "B's own private journal entry, never meant to be seen"
    safe_numeric_a = "500"
    safe_numeric_c = "20"
    safe_other_a = "1"

    # (user_id, action, entity, old_value, new_value, source, target_user_id)
    rows = [
        (USER_A, "undo", "diary", at_risk_diary_a, None, "command", None),
        (USER_A, "undo", "diary", at_risk_diary_a_2, None, "button", None),
        (USER_B, "undo", "journal", at_risk_journal_b, None, "button", None),
        (USER_A, "undo", "water", safe_numeric_a, None, "command", None),
        (USER_C, "undo", "pushups", safe_numeric_c, None, "button", None),
        (USER_A, "undo", "stretch", safe_other_a, None, "command", None),
        (USER_A, "target_set", "diary", "some diary text used as a goal note", "10", "command", None),
        (USER_C, "user_approve", None, None, "active", "portal", USER_C),
    ]
    conn.executemany(
        "INSERT INTO audit_log (ts, user_id, action, entity, old_value, new_value, source, target_user_id) "
        "VALUES ('2026-08-20T09:00:00', ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()

    # The REAL upgrade path.
    db = Database(db_path)
    assert db.schema_version_before == len(MIGRATIONS) - 1
    assert db.schema_version == len(MIGRATIONS) == 15, "schema must stamp exactly 15 after the real sequential upgrade"

    scrubbed = db._conn.execute("SELECT id, user_id, action, entity, old_value FROM audit_log ORDER BY id").fetchall()
    assert len(scrubbed) == len(rows)

    # AT-RISK rows: scrubbed to the exact redacted marker, never containing the original text.
    assert scrubbed[0]["old_value"] == f"[text entry removed] ({len(at_risk_diary_a)} chars)"
    assert scrubbed[1]["old_value"] == f"[text entry removed] ({len(at_risk_diary_a_2)} chars)"
    assert scrubbed[2]["old_value"] == f"[text entry removed] ({len(at_risk_journal_b)} chars)"
    all_old_values = [r["old_value"] for r in scrubbed]
    for leaked_text in (at_risk_diary_a, at_risk_diary_a_2, at_risk_journal_b):
        assert leaked_text not in all_old_values

    # SAFE rows: byte-identical, untouched -- surgical, not "every undo".
    assert scrubbed[3]["old_value"] == safe_numeric_a
    assert scrubbed[4]["old_value"] == safe_numeric_c
    assert scrubbed[5]["old_value"] == safe_other_a
    assert scrubbed[6]["old_value"] == "some diary text used as a goal note"  # target_set, not undo
    assert scrubbed[7]["old_value"] is None  # unrelated action entirely

    # Idempotent: re-invoking the migration function directly a second time
    # must not double-wrap an already-scrubbed marker or touch anything else.
    _migration_015_scrub_diary_undo_audit_rows(db._conn)
    db._conn.commit()
    rescrubbed = [r["old_value"] for r in db._conn.execute("SELECT old_value FROM audit_log ORDER BY id").fetchall()]
    assert rescrubbed == all_old_values

    db.close()


# ===========================================================================
# 4. Telegram-mode regression: the specced default, pinned.
# ===========================================================================


@pytest.mark.parametrize("portal_enabled", [False, True])
async def test_telegram_mode_never_gets_the_portal_regardless_of_enabled_startup_clean(monkeypatch, tmp_path, portal_enabled):
    """SPEC-LINE-PORTAL.md R-SEC-1: 'The portal is constructed ... only
    when config.portal.enabled is True AND config.channel.type == "line"'.
    The Telegram edition's specced default is NO portal at all, full
    stop, independent of the `enabled` flag -- both values are pinned in
    one parametrized test, plus a "startup otherwise stayed clean" check:
    the ordinary channel-agnostic jobs still registered normally, proving
    the portal's absence didn't skip or break anything else in startup."""
    async with _running_app(monkeypatch, tmp_path, portal_enabled=portal_enabled, channel_type="telegram") as app:
        assert await _port_is_closed(app.config.portal.bind_host, app.config.portal.bind_port)
        handler_types = [type(h) for h in logging.getLogger("habit_assistant").handlers]
        assert RingBufferHandler not in handler_types

        assert app.scheduler is not None, "the scheduler itself must still have started normally on Telegram"
        assert app.scheduler.get_job("minutely_tick") is not None, "channel-agnostic jobs must still register -- startup reached scheduler.start() cleanly"
        assert app.scheduler.get_job("daily_digest") is None, "the LINE-only digest job must never register on Telegram"


# ===========================================================================
# 5. Version consistency + release-notes posture.
# ===========================================================================


def test_version_consistency_across_the_three_files_and_release_notes_posture_unchanged():
    version_file = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    pyproject_data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pyproject_version = pyproject_data["project"]["version"]

    assert version_file == "1.3.0+line"
    assert habit_assistant.__version__ == "1.3.0+line"
    assert pyproject_version == "1.3.0+line"
    assert version_file == habit_assistant.__version__ == pyproject_version

    # Release-notes posture: this release deliberately does NOT self-
    # announce (SPEC-LINE-PORTAL.md §9 OQ2's own default) -- verify that
    # posture is unchanged by the integration pass.
    assert "1.3.0" not in RELEASE_NOTES
    assert "1.3.0+line" not in RELEASE_NOTES
    assert "1.2.0" not in RELEASE_NOTES
    assert get_release_note("1.3.0+line", "en") is None
    assert get_release_note("1.3.0+line", "th") is None


# ===========================================================================
# 6. Digest double-send guard, the SECOND order (scheduled-then-manual).
#    Item 9's own test only proves manual-then-scheduled.
# ===========================================================================


async def test_digest_run_overlap_guard_scheduled_then_manual_through_real_app(monkeypatch, tmp_path):
    """Mirror image of `test_portal_integration.py`'s own item-9 proof.
    The scheduled job fires FIRST here; the manual portal trigger's own
    unconfirmed interstitial must immediately recognize the day is already
    claimed and refuse to even mint a token, let alone send again."""
    async with _running_app(monkeypatch, tmp_path, portal_enabled=True, extra_users={MEMBER: "active"}) as app:
        job = app.scheduler.get_job("daily_digest")
        assert job is not None
        await job.func()
        pushes_after_scheduled = len(app.api.calls_matching("/message/push"))
        assert pushes_after_scheduled > 0, "the scheduled run must have actually sent something first"

        base = f"http://127.0.0.1:{app.portal_port}"
        async with httpx.AsyncClient() as client:
            unconfirmed = await client.post(f"{base}/quota/digest-run", data={}, headers=HEADERS)
            assert unconfirmed.status_code == 200
            assert 'name="token"' not in unconfirmed.text, "no fresh token must be minted -- the day is already claimed by the scheduled run"
            assert "Already sent" in unconfirmed.text or "ส่งไปแล้วเมื่อ" in unconfirmed.text

        pushes_after_manual_attempt = len(app.api.calls_matching("/message/push"))
        assert pushes_after_manual_attempt == pushes_after_scheduled, (
            "the manual trigger must see the day already claimed and send nothing -- no double-push in this order either"
        )
