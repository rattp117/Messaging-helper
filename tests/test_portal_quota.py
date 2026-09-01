"""SPEC-LINE-PORTAL.md §4 R-QUOTA-1..5 (module QUOTA, admin web portal,
branch `line-version`) -- Luna's own test suite for `core/portal/quota.py`.

Owned ACs (SPEC-LINE-PORTAL.md §11): AC26 (monthly push totals + current-
month per-user breakdown), AC27 (active cap, 80%/100% thresholds, whether
warn/stop have fired), AC28 (per-user digest opt-out state + the global
digest time/mode), AC29 (`GET /config`, secrets redacted), AC30 (manual
digest trigger, confirm-gated, no send without confirm) -- plus the
dispatch note's own load-bearing NO-DOUBLE-SEND requirement (a replayed
POST or a concurrent double-POST must never send twice).

Conventions mirror `tests/test_portal_server.py`/`tests/test_portal_
security.py` (a real on-disk SQLite `Database` via `tmp_path`, a real
`aiohttp` `TestClient`/`TestServer`, `PortalDeps` built directly -- no
mocks for the portal's own plumbing) and `tests/test_digest.py`
(`RecordingLineChannel` from `conftest.py`, and its own documented
`_current_yyyymm()` helper for asserting against the REAL wall-clock
month `channels/line.py`/`RecordingLineChannel` key `push_ledger` off,
independent of any injected `clock=`).
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel

from conftest import RecordingLineChannel
from habit_assistant.config import Config
from habit_assistant.core import digest
from habit_assistant.core.portal import quota
from habit_assistant.core.portal.server import PortalDeps
from habit_assistant.storage.db import Database

OWNER = "Uowner00000000000000000000000000"
MEMBER_A = "Umembera000000000000000000000000"
MEMBER_B = "Umemberb000000000000000000000000"


def _current_yyyymm() -> str:
    """Mirrors `tests/test_digest.py`'s own identically-named/documented
    helper: `RecordingLineChannel.send()` (conftest.py) increments
    `push_ledger` off the REAL wall clock, never off any injected
    `clock=`, so assertions about "this month's" total must use this,
    not a literal tied to test data."""
    return datetime.now().strftime("%Y-%m")


@pytest.fixture(autouse=True)
def _reset_manual_digest_state():
    """NO-DOUBLE-SEND guards (`quota.py`'s own module docstring) are
    process-lifetime, module-level state by design (mirrors `core/
    digest.py:_DIGEST_DEFERRED_DATES`) -- exactly what makes them work
    correctly against a REAL restart, but it also means they must be
    reset between tests IN THIS FILE, or one test's successful run would
    leak into the next as a false "already sent today". Integration item
    5: the same-day marker moved to `core/digest.py:_DAILY_RUN_CLAIMED`
    (shared with the scheduled job) -- reset here too, alongside the
    still-local token set."""
    digest._DAILY_RUN_CLAIMED.clear()
    quota._pending_digest_tokens.clear()
    yield
    digest._DAILY_RUN_CLAIMED.clear()
    quota._pending_digest_tokens.clear()


@pytest.fixture
async def aiohttp_client_factory():
    """Mirrors `tests/test_portal_security.py`/`tests/test_portal_
    server.py`'s own fixture of the same name/shape."""
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
    database.upsert_user(OWNER, role="owner", status="active", display_name="Owner")
    yield database
    database.close()


def _config(**overrides) -> Config:
    return Config.model_validate(overrides) if overrides else Config()


def _build_app(db: Database, config: Config, channel=None) -> web.Application:
    deps = PortalDeps(
        db=db,
        config=config,
        scheduler=None,
        channel=channel if channel is not None else RecordingLineChannel(db=db),
        stats=None,
        ring=None,
        owner_id=OWNER,
    )
    app = web.Application()
    app["portal_deps"] = deps
    quota.register(app, deps)
    return app


async def _mint_token(client: TestClient) -> str:
    resp = await client.post("/quota/digest-run", data={})
    assert resp.status == 200
    text = await resp.text()
    match = re.search(r'name="token" value="([^"]+)"', text)
    assert match is not None, "interstitial did not carry a token field"
    return match.group(1)


# ===========================================================================
# AC26 -- GET /quota: monthly push totals + current-month per-user breakdown.
# ===========================================================================


async def test_quota_page_shows_monthly_history_and_current_month_marker(db, aiohttp_client_factory):
    yyyymm = _current_yyyymm()
    db.increment_push(MEMBER_A, yyyymm)
    db.increment_push(MEMBER_A, "2026-01")
    client = await aiohttp_client_factory(_build_app(db, _config()))

    resp = await client.get("/quota")
    assert resp.status == 200
    text = await resp.text()
    assert yyyymm in text
    assert "2026-01" in text
    assert "เดือนนี้" in text or "this month" in text  # the "<- current month" marker


async def test_quota_page_shows_current_month_per_user_breakdown_sorted_desc(db, aiohttp_client_factory):
    db.upsert_user(MEMBER_A, role="member", status="active", display_name="Nok")
    db.upsert_user(MEMBER_B, role="member", status="active", display_name="Somchai")
    yyyymm = _current_yyyymm()
    for _ in range(5):
        db.increment_push(MEMBER_A, yyyymm)
    db.increment_push(MEMBER_B, yyyymm)
    client = await aiohttp_client_factory(_build_app(db, _config()))

    resp = await client.get("/quota")
    text = await resp.text()
    assert resp.status == 200
    assert "Nok" in text
    assert "Somchai" in text
    assert text.index("Nok") < text.index("Somchai")  # higher pusher first (db.push_by_user is already sorted desc)


async def test_quota_page_month_history_empty_state(db, aiohttp_client_factory):
    client = await aiohttp_client_factory(_build_app(db, _config()))
    resp = await client.get("/quota")
    text = await resp.text()
    assert "No push history yet" in text or "ยังไม่มีประวัติการพุช" in text


async def test_quota_page_byuser_empty_state_this_month(db, aiohttp_client_factory):
    db.increment_push(MEMBER_A, "2026-01")  # history exists, but NOT this month
    client = await aiohttp_client_factory(_build_app(db, _config()))
    resp = await client.get("/quota")
    text = await resp.text()
    assert "No pushes recorded this month yet" in text or "เดือนนี้ยังไม่มีการพุช" in text


# ===========================================================================
# AC27 -- active cap, 80%/100% thresholds, whether warn/stop have fired.
# ===========================================================================


async def test_quota_page_shows_active_cap_and_not_fired_thresholds(db, aiohttp_client_factory):
    config = _config(digest={"warn_cap": 280})  # digest mode (default) -> cap = warn_cap
    client = await aiohttp_client_factory(_build_app(db, config))
    resp = await client.get("/quota")
    text = await resp.text()
    assert "280" in text
    assert "not fired" in text or "ยังไม่แจ้งเตือน" in text


async def test_quota_page_shows_warn_fired_when_at_80_percent(db, aiohttp_client_factory):
    config = _config(digest={"warn_cap": 100})
    yyyymm = _current_yyyymm()
    for _ in range(80):
        db.increment_push(MEMBER_A, yyyymm)
    client = await aiohttp_client_factory(_build_app(db, config))
    resp = await client.get("/quota")
    text = await resp.text()
    assert "fired this month" in text or "แจ้งเตือนแล้วเดือนนี้" in text


async def test_quota_page_shows_stop_fired_when_cap_reached(db, aiohttp_client_factory):
    config = _config(digest={"warn_cap": 10})
    yyyymm = _current_yyyymm()
    for _ in range(10):
        db.increment_push(MEMBER_A, yyyymm)
    client = await aiohttp_client_factory(_build_app(db, config))
    resp = await client.get("/quota")
    text = await resp.text()
    assert "🛑" in text  # the stop-fired tag's own icon, distinct from the warn tag's ⚠️


async def test_quota_page_active_cap_uses_push_cap_in_realtime_mode(db, aiohttp_client_factory):
    config = _config(digest={"mode": "realtime", "push_cap": 15000, "warn_cap": 280})
    client = await aiohttp_client_factory(_build_app(db, config))
    resp = await client.get("/quota")
    text = await resp.text()
    assert "15000" in text
    assert "280" not in text  # warn_cap is NOT the active cap in realtime mode


# ===========================================================================
# AC28 -- per-user digest opt-out state + the global digest time/mode.
# ===========================================================================


async def test_quota_page_shows_digest_roster_and_schedule(db, aiohttp_client_factory):
    db.upsert_user(MEMBER_A, role="member", status="active", display_name="Nok")
    db.set_digest_opt_out(MEMBER_A, True)
    config = _config(digest={"time": "20:00", "mode": "digest"})
    client = await aiohttp_client_factory(_build_app(db, config))
    resp = await client.get("/quota")
    text = await resp.text()
    assert "20:00" in text
    assert "digest" in text
    assert "Nok" in text
    assert "off" in text or "ปิด" in text  # Nok's own opted-out row


async def test_quota_page_owner_row_labeled_you_owner(db, aiohttp_client_factory):
    client = await aiohttp_client_factory(_build_app(db, _config()))
    resp = await client.get("/quota")
    text = await resp.text()
    assert "You (owner)" in text or "คุณ (เจ้าของบอท)" in text


async def test_quota_page_send_button_shows_go_count_of_digest_on_users(db, aiohttp_client_factory):
    db.upsert_user(MEMBER_A, role="member", status="active")
    db.upsert_user(MEMBER_B, role="member", status="active")
    db.set_digest_opt_out(MEMBER_B, True)  # opted out -- only OWNER + MEMBER_A are "on"
    client = await aiohttp_client_factory(_build_app(db, _config()))
    resp = await client.get("/quota")
    text = await resp.text()
    assert "Send digest now" in text or "ส่งสรุปรายวันตอนนี้" in text
    assert "2" in text  # 2 users with digest on


# ===========================================================================
# AC29 -- GET /config: effective config renders, secrets redacted.
# ===========================================================================


async def test_config_page_renders_sections_in_config_order(db, aiohttp_client_factory):
    client = await aiohttp_client_factory(_build_app(db, _config()))
    resp = await client.get("/config")
    assert resp.status == 200
    text = await resp.text()
    assert "[app]" in text
    assert "[digest]" in text
    assert "[portal]" in text
    assert "Asia/Bangkok" in text  # AppConfig.timezone default, unredacted


async def test_config_page_secrets_note_is_present():
    from habit_assistant.core import i18n

    note_en = i18n.t("portal_config_secrets_note", "en")
    assert "••••••" in note_en


def test_redact_or_render_masks_a_secret_shaped_field():
    """Proves the generic name-matching redaction mechanism directly --
    `Config` itself carries no LINE token/secret field today (those live
    in the separate `Secrets` model, never threaded into the portal, see
    `quota.py`'s own module docstring), so this is exercised against a
    synthetic value rather than end-to-end against the real Config."""
    masked = quota._redact_or_render("channel_access_token", "AbCdEf12345", "en")
    assert "AbCdEf12345" not in masked
    assert "••••••" in masked
    assert "(hidden)" in masked


def test_redact_or_render_unset_secret_shows_not_set_not_hidden():
    masked = quota._redact_or_render("channel_secret", "", "en")
    assert "(not set)" in masked
    assert "••••••" not in masked


def test_redact_or_render_non_secret_field_shows_plain_value():
    rendered = quota._redact_or_render("timezone", "Asia/Bangkok", "en")
    assert "Asia/Bangkok" in rendered
    assert "••••••" not in rendered


def test_render_section_dl_skips_non_scalar_fields():
    class _FakeSection(BaseModel):
        timezone: str = "Asia/Bangkok"
        nested: dict = {}  # must be skipped, not rendered raw

    html = quota._render_section_dl(_FakeSection(), "en")
    assert "Asia/Bangkok" in html
    assert "nested" not in html


# ===========================================================================
# AC30 -- manual digest trigger: confirm-gated, real fan-out, result summary.
# ===========================================================================


async def test_digest_run_without_confirm_sends_nothing(db, aiohttp_client_factory):
    db.upsert_user(MEMBER_A, role="member", status="active")
    channel = RecordingLineChannel(db=db)
    client = await aiohttp_client_factory(_build_app(db, _config(), channel=channel))

    resp = await client.post("/quota/digest-run", data={})
    assert resp.status == 200
    text = await resp.text()
    assert "Send today's digest now?" in text or "ส่งสรุปของวันนี้เลยไหม?" in text
    assert channel.pushes == []


async def test_digest_run_confirmed_invokes_real_run_daily_digest_and_reports_result(db, aiohttp_client_factory):
    db.upsert_user(MEMBER_A, role="member", status="active")
    channel = RecordingLineChannel(db=db)
    client = await aiohttp_client_factory(_build_app(db, _config(), channel=channel))

    token = await _mint_token(client)
    resp = await client.post("/quota/digest-run", data={"confirm": "yes", "token": token}, allow_redirects=False)
    assert resp.status == 303
    location = resp.headers["Location"]
    assert location.startswith("/quota?ran=")
    # OWNER + MEMBER_A both digest-on by default -> 2 real sends, 0 skipped,
    # 0 failed. Integration item 5: `ran=` now carries a third `failed`
    # field (`sent.skipped.failed`, TEST-PORTAL-quota.md Finding F4).
    assert location == "/quota?ran=2.0.0#flash"
    assert len(channel.pushes) == 2


async def test_digest_run_respects_opt_outs_reports_skipped(db, aiohttp_client_factory):
    db.upsert_user(MEMBER_A, role="member", status="active")
    db.set_digest_opt_out(MEMBER_A, True)
    channel = RecordingLineChannel(db=db)
    client = await aiohttp_client_factory(_build_app(db, _config(), channel=channel))

    token = await _mint_token(client)
    resp = await client.post("/quota/digest-run", data={"confirm": "yes", "token": token}, allow_redirects=False)
    location = resp.headers["Location"]
    assert location == "/quota?ran=1.1.0#flash"  # 1 sent (owner), 1 skipped (opted-out member), 0 failed


async def test_digest_run_stopped_quota_refuses_with_clear_message_no_send(db, aiohttp_client_factory):
    db.upsert_user(MEMBER_A, role="member", status="active")
    config = _config(digest={"warn_cap": 1})
    yyyymm = _current_yyyymm()
    db.increment_push(MEMBER_A, yyyymm)
    db.increment_push(MEMBER_A, yyyymm)  # total=2 >= warn_cap(1) -> stopped
    channel = RecordingLineChannel(db=db)
    client = await aiohttp_client_factory(_build_app(db, config, channel=channel))

    resp = await client.post("/quota/digest-run", data={}, allow_redirects=False)
    assert resp.status == 303
    assert "err=quota_stopped" in resp.headers["Location"]
    assert channel.pushes == []

    resp2 = await client.post(
        "/quota/digest-run", data={"confirm": "yes", "token": "forged-or-stale"}, allow_redirects=False
    )
    assert resp2.status == 303
    assert "err=quota_stopped" in resp2.headers["Location"]
    assert channel.pushes == []


async def test_quota_page_button_replaced_not_disabled_when_stopped(db, aiohttp_client_factory):
    config = _config(digest={"warn_cap": 1})
    yyyymm = _current_yyyymm()
    db.increment_push(MEMBER_A, yyyymm)
    db.increment_push(MEMBER_A, yyyymm)
    client = await aiohttp_client_factory(_build_app(db, config))

    resp = await client.get("/quota")
    text = await resp.text()
    assert "Send digest now" not in text and "ส่งสรุปรายวันตอนนี้" not in text
    assert "Push cap reached" in text or "ถึงเพดานพุชแล้ว" in text


async def test_digest_run_realtime_mode_is_a_real_no_op_reports_zero_sent(db, aiohttp_client_factory):
    """R-QUOTA-5's own literal instruction is to invoke the REAL
    `digest.run_daily_digest` -- in realtime mode that function no-ops by
    design (SPEC-LINE-1.2.md: digest and realtime are mode-exclusive), so
    the honest, correct report here is 0 sent, not a fabricated count."""
    db.upsert_user(MEMBER_A, role="member", status="active")
    config = _config(digest={"mode": "realtime", "push_cap": 100})
    channel = RecordingLineChannel(db=db)
    client = await aiohttp_client_factory(_build_app(db, config, channel=channel))

    token = await _mint_token(client)
    resp = await client.post("/quota/digest-run", data={"confirm": "yes", "token": token}, allow_redirects=False)
    assert resp.headers["Location"] == "/quota?ran=0.0.0#flash"
    assert channel.pushes == []


# ===========================================================================
# NO-DOUBLE-SEND -- the dispatch note's own load-bearing requirement.
# ===========================================================================


async def test_digest_run_replayed_confirm_same_token_does_not_send_twice(db, aiohttp_client_factory):
    db.upsert_user(MEMBER_A, role="member", status="active")
    channel = RecordingLineChannel(db=db)
    client = await aiohttp_client_factory(_build_app(db, _config(), channel=channel))

    token = await _mint_token(client)
    first = await client.post("/quota/digest-run", data={"confirm": "yes", "token": token}, allow_redirects=False)
    assert first.status == 303
    assert len(channel.pushes) == 2

    replay = await client.post("/quota/digest-run", data={"confirm": "yes", "token": token}, allow_redirects=False)
    assert replay.status == 200  # NOT a redirect -- the "already sent" page, no second send
    replay_text = await replay.text()
    assert "Already sent at" in replay_text or "ส่งไปแล้วเมื่อ" in replay_text
    assert len(channel.pushes) == 2  # unchanged


async def test_digest_run_second_visit_same_day_shows_already_sent_before_confirming(db, aiohttp_client_factory):
    """A SECOND, otherwise-legitimate visit to the button (a fresh
    unconfirmed POST, not a replay of the same submission) on a day that
    already sent must not offer a working confirm flow again."""
    db.upsert_user(MEMBER_A, role="member", status="active")
    channel = RecordingLineChannel(db=db)
    client = await aiohttp_client_factory(_build_app(db, _config(), channel=channel))

    token = await _mint_token(client)
    await client.post("/quota/digest-run", data={"confirm": "yes", "token": token}, allow_redirects=False)
    assert len(channel.pushes) == 2

    second_visit = await client.post("/quota/digest-run", data={})
    assert second_visit.status == 200
    text = await second_visit.text()
    assert "Already sent at" in text or "ส่งไปแล้วเมื่อ" in text
    assert "Send today's digest now?" not in text and "ส่งสรุปของวันนี้เลยไหม?" not in text


async def test_digest_run_unrecognized_token_refuses_no_send(db, aiohttp_client_factory):
    db.upsert_user(MEMBER_A, role="member", status="active")
    channel = RecordingLineChannel(db=db)
    client = await aiohttp_client_factory(_build_app(db, _config(), channel=channel))

    resp = await client.post(
        "/quota/digest-run", data={"confirm": "yes", "token": "never-minted"}, allow_redirects=False
    )
    assert resp.status == 200
    text = await resp.text()
    assert "Already sent at" in text or "ส่งไปแล้วเมื่อ" in text
    assert channel.pushes == []


async def test_digest_run_concurrent_double_post_sends_exactly_once(db, aiohttp_client_factory):
    """The dispatch note's own explicit test requirement: a concurrent
    double-POST (simulating a double-click, or a race between the real
    submission and a browser-retried/replayed copy of it) must result in
    exactly ONE real send, not zero and not two."""
    db.upsert_user(MEMBER_A, role="member", status="active")
    db.upsert_user(MEMBER_B, role="member", status="active")
    channel = RecordingLineChannel(db=db)
    client = await aiohttp_client_factory(_build_app(db, _config(), channel=channel))

    token = await _mint_token(client)

    async def _confirm():
        return await client.post(
            "/quota/digest-run", data={"confirm": "yes", "token": token}, allow_redirects=False
        )

    first, second = await asyncio.gather(_confirm(), _confirm())
    statuses = sorted([first.status, second.status])
    assert statuses == [200, 303], f"expected one redirect (real send) + one already-sent page, got {statuses}"
    # OWNER + MEMBER_A + MEMBER_B, all digest-on by default -> exactly 3 pushes, from exactly ONE run.
    assert len(channel.pushes) == 3


async def test_digest_run_lock_serializes_two_distinct_first_time_tokens(db, aiohttp_client_factory):
    """Two SEPARATE interstitial page loads (two distinct tokens, e.g.
    two browser tabs) racing their own confirm submissions must still
    only result in one real send -- the same-day marker (not just token
    identity) is what makes this safe."""
    db.upsert_user(MEMBER_A, role="member", status="active")
    channel = RecordingLineChannel(db=db)
    client = await aiohttp_client_factory(_build_app(db, _config(), channel=channel))

    token_a = await _mint_token(client)
    token_b = await _mint_token(client)
    assert token_a != token_b

    async def _confirm(tok):
        return await client.post("/quota/digest-run", data={"confirm": "yes", "token": tok}, allow_redirects=False)

    first, second = await asyncio.gather(_confirm(token_a), _confirm(token_b))
    statuses = sorted([first.status, second.status])
    assert statuses == [200, 303]
    assert len(channel.pushes) == 2  # exactly one run (owner + member_a)
