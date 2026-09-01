"""SPEC-LINE-PORTAL.md §4 R-STATUS-* (module STATUS, admin web portal,
branch `line-version`): `core/portal/status.py`'s own tests -- AC8-AC14,
plus the verdict banner / needs-you block UX.md Flow A and UI.md §3.3/§3.4
describe (both compose only AC8-AC14 data, no new datum), the per-panel
"unavailable" degradation (SPEC-LINE-PORTAL.md §3.3), and R-I18N-1/AC31
(bilingual, no hardcoded literals).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from habit_assistant import __version__
from habit_assistant.config import Config
from habit_assistant.core.portal import status
from habit_assistant.core.portal.server import PortalDeps, PortalServer
from habit_assistant.core.portal.stats import RingBufferHandler, RuntimeStats
from habit_assistant.storage.db import Database

OWNER = "Uowner00000000000000000000000000"


# ===========================================================================
# Fixtures / helpers.
# ===========================================================================


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


def _config(tmp_path, db_path, **overrides):
    base = {
        "portal": {"enabled": True, "bind_port": 9500},
        "channel": {"type": "line"},
        "app": {"db_path": str(db_path)},
        "line": {"media_dir": str(tmp_path / "media")},
        "backup": {"dir": str(tmp_path / "backups")},
    }
    for key, value in overrides.items():
        base[key] = {**base.get(key, {}), **value}
    return Config.model_validate(base)


def _deps(db_obj, config, *, scheduler=None, stats=None, ring=None, owner=OWNER) -> PortalDeps:
    return PortalDeps(
        db=db_obj,
        config=config,
        scheduler=scheduler if scheduler is not None else SimpleNamespace(get_jobs=lambda: []),
        channel=SimpleNamespace(),
        stats=stats if stats is not None else RuntimeStats(),
        ring=ring if ring is not None else RingBufferHandler(200),
        owner_id=owner,
    )


@pytest.fixture
async def client_factory():
    clients: list[TestClient] = []

    async def make(deps: PortalDeps) -> TestClient:
        server = PortalServer(bind_host="127.0.0.1", bind_port=0, deps=deps, modules=[status.register])
        client = TestClient(TestServer(server.build_app()))
        await client.start_server()
        clients.append(client)
        return client

    yield make

    for client in clients:
        await client.close()


async def _get_status(client: TestClient, **kwargs) -> tuple[int, str]:
    resp = await client.get("/", headers={"Tailscale-User-Login": "owner@example.com"}, **kwargs)
    return resp.status, await resp.text()


# ===========================================================================
# AC8: version, channel, Ollama mode.
# ===========================================================================


async def test_ac8_shows_version_channel_and_ollama_off(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", ollama={"enabled": False})
    deps = _deps(db, config)
    client = await client_factory(deps)
    status_code, body = await _get_status(client)
    assert status_code == 200
    assert __version__ in body
    assert "<b>line</b>" in body
    assert "Ollama" in body and "<b>off</b>" in body


async def test_ac8_ollama_on_when_enabled(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", ollama={"enabled": True})
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "<b>on</b>" in body


# ===========================================================================
# AC9: uptime, derived from RuntimeStats.started_at.
# ===========================================================================


async def test_ac9_uptime_derived_from_started_at(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    stats = RuntimeStats(started_at=datetime.now() - timedelta(days=1, hours=2, minutes=5))
    deps = _deps(db, config, stats=stats)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "1d 2h 5m" in body


async def test_ac9_uptime_under_an_hour_omits_days_and_hours(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    stats = RuntimeStats(started_at=datetime.now() - timedelta(minutes=7))
    deps = _deps(db, config, stats=stats)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "7m" in body
    assert "0d" not in body


# ===========================================================================
# AC10: last webhook event -- relative+absolute when set, localized
# "no events since restart" when unset.
# ===========================================================================


async def test_ac10_no_events_shows_localized_empty_state(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    deps = _deps(db, config)  # RuntimeStats() default -- last_event_at is None
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "No events since the service restarted" in body


async def test_ac10_last_event_shows_relative_and_absolute(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    stats = RuntimeStats()
    stats.last_event_at = datetime(2026, 8, 31, 14, 3)
    deps = _deps(db, config, stats=stats)
    client = await client_factory(deps)
    _, body = await _get_status(client, params=None)
    assert "2026-08-31 14:03" in body


async def test_ac10_last_event_in_thai_uses_relative_ago_copy(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    stats = RuntimeStats()
    stats.last_event_at = datetime.now() - timedelta(minutes=4)
    deps = _deps(db, config, stats=stats)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "นาทีที่แล้ว" in body  # Thai is the default resolved language (no owner pref stored)


# ===========================================================================
# AC11: scheduler jobs by id + next-run time; dead job marker.
# ===========================================================================


async def test_ac11_lists_every_job_id_with_next_run_time(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    next_run = datetime(2026, 8, 31, 14, 5, 0)
    scheduler = SimpleNamespace(
        get_jobs=lambda: [
            SimpleNamespace(id="minutely_tick", next_run_time=next_run),
            SimpleNamespace(id="daily_digest", next_run_time=next_run + timedelta(hours=6)),
        ]
    )
    deps = _deps(db, config, scheduler=scheduler)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "minutely_tick" in body
    assert "daily_digest" in body
    assert "2026-08-31 14:05" in body


async def test_ac11_dead_job_renders_not_scheduled_tag_and_drives_stop_verdict(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    scheduler = SimpleNamespace(get_jobs=lambda: [SimpleNamespace(id="daily_digest", next_run_time=None)])
    deps = _deps(db, config, scheduler=scheduler)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert 'class="tag word stop"' in body
    assert "ยังไม่ได้ตั้งเวลา" in body
    assert "verdict stop" in body
    assert "daily_digest" in body


async def test_ac11_empty_scheduler_shows_empty_state_not_a_crash(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    deps = _deps(db, config, scheduler=SimpleNamespace(get_jobs=lambda: []))
    client = await client_factory(deps)
    status_code, body = await _get_status(client)
    assert status_code == 200
    assert "class=\"empty mute\"" in body


# ===========================================================================
# AC12: quota gauge -- used/cap/pct/mode, three tiers.
# ===========================================================================


async def test_ac12_gauge_shows_used_cap_pct_and_mode_realtime(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", digest={"mode": "realtime", "push_cap": 15000}, i18n={"language": "en"})
    for _ in range(182):
        db.increment_push("Ua", datetime.now().strftime("%Y-%m"))
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "182 / 15000 (1.2%)" in body
    assert "realtime" in body
    assert 'panel gauge ok' in body


async def test_ac12_gauge_uses_warn_cap_in_digest_mode(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", digest={"mode": "digest", "warn_cap": 280}, i18n={"language": "en"})
    for _ in range(10):
        db.increment_push("Ua", datetime.now().strftime("%Y-%m"))
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "10 / 280" in body
    assert "digest" in body


async def test_ac12_gauge_warn_tier_at_80_percent(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", digest={"mode": "digest", "warn_cap": 100})
    for _ in range(85):
        db.increment_push("Ua", datetime.now().strftime("%Y-%m"))
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "panel gauge warn" in body
    assert "verdict warn" in body


async def test_ac12_gauge_stop_tier_at_100_percent_drives_stop_verdict(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", digest={"mode": "digest", "warn_cap": 50})
    for _ in range(50):
        db.increment_push("Ua", datetime.now().strftime("%Y-%m"))
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "panel gauge stop" in body
    assert "verdict stop" in body


async def test_ac12_gauge_read_failure_renders_unavailable_and_does_not_500(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")

    class _BoomDB:
        def __getattr__(self, name):
            if name == "monthly_push_total":
                def _boom(*a, **k):
                    raise RuntimeError("simulated DB hiccup")
                return _boom
            return getattr(db, name)

    deps = _deps(_BoomDB(), config)
    client = await client_factory(deps)
    status_code, body = await _get_status(client)
    assert status_code == 200
    # TEST-PORTAL-status.md Finding 2: `layout.escape()` renders the
    # apostrophe as `&#x27;`, so "Can't" never appears literally -- this
    # branch of the `or` was dead (the default-Thai fixture always took
    # the Thai side). Matched on an escape-safe substring instead, so the
    # English branch is actually exercised.
    assert "read this right now." in body or "อ่านข้อมูลส่วนนี้ไม่ได้ตอนนี้" in body
    assert "verdict warn" in body  # a failed panel is a "Needs a look" trigger


# ===========================================================================
# AC13: DB size (+wal/shm), media size, backups list, last backup.
# ===========================================================================


async def test_ac13_shows_db_size_media_size_and_backup_list(tmp_path, db, client_factory):
    db_path = tmp_path / "habits.db"
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "tok1.png").write_bytes(b"x" * 2048)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "habits-20260830T030000-000000.db").write_bytes(b"y" * 4096)
    (backup_dir / "habits-20260829T030000-000000.db").write_bytes(b"z" * 1024)

    config = _config(tmp_path, db_path)
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)

    assert "2.0 KB" in body  # media dir total
    assert "habits-20260830T030000-000000.db" in body  # newest backup listed
    assert "habits-20260829T030000-000000.db" in body
    assert "2 " in body and "backups" in body or "สำรองข้อมูล 2 ชุด" in body


async def test_ac13_no_backups_shows_localized_fallback_not_a_timestamp(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "ยังไม่มีการสำรองข้อมูล" in body
    assert "<details class=\"more\">" not in body


async def test_ac13_storage_read_failure_renders_unavailable_not_a_500(tmp_path, db, client_factory):
    missing_db_path = tmp_path / "does-not-exist.db"
    config = _config(tmp_path, missing_db_path)
    deps = _deps(db, config)
    client = await client_factory(deps)
    status_code, body = await _get_status(client)
    assert status_code == 200
    assert "id=\"storage\"" in body
    # TEST-PORTAL-status.md Finding 2: same escape-safe substring fix as
    # the AC12 test above -- "Can't" never appears literally once escaped.
    assert "read this right now." in body or "อ่านข้อมูลส่วนนี้ไม่ได้ตอนนี้" in body


# ===========================================================================
# AC14: recent-errors ring buffer -- populated, empty, at-capacity.
# ===========================================================================


def _log(ring: RingBufferHandler, level: int, name: str, msg: str) -> None:
    logger = logging.getLogger(name)
    logger.addHandler(ring)
    logger.setLevel(logging.DEBUG)
    try:
        logger.log(level, msg)
    finally:
        logger.removeHandler(ring)


async def test_ac14_empty_ring_buffer_shows_localized_empty_state(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "ยังไม่มีข้อผิดพลาดตั้งแต่ระบบเริ่มทำงาน" in body
    assert "รายการนี้จะล้างทุกครั้งที่ระบบรีสตาร์ต" in body
    assert "verdict ok" in body


async def test_ac14_populated_ring_buffer_renders_rows_and_drives_warn_verdict(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    ring = RingBufferHandler(200)
    _log(ring, logging.WARNING, "habit_assistant.core.digest", "push failed for Uxxxx: 429")
    deps = _deps(db, config, ring=ring)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "push failed for Uxxxx: 429" in body
    assert "WARNING" in body
    assert "habit_assistant.core.digest" in body
    assert "verdict warn" in body


async def test_ac14_at_capacity_shows_the_dropped_note(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    ring = RingBufferHandler(2)
    for i in range(4):
        _log(ring, logging.ERROR, "habit_assistant.core.digest", f"boom-{i}")
    deps = _deps(db, config, ring=ring)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "Older records have been dropped" in body or "ถูกทิ้งไปแล้ว" in body
    assert "boom-3" in body  # newest kept
    assert "boom-0" not in body  # oldest dropped


async def test_ac14_error_level_gets_stop_tag_warning_gets_warn_tag(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    ring = RingBufferHandler(200)
    _log(ring, logging.ERROR, "habit_assistant.x", "err-msg")
    _log(ring, logging.WARNING, "habit_assistant.x", "warn-msg")
    deps = _deps(db, config, ring=ring)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert 'class="tag stop">ERROR<' in body
    assert 'class="tag warn">WARNING<' in body


# ===========================================================================
# Verdict banner: severity precedence, single vs multi-cause rendering.
# ===========================================================================


async def test_verdict_ok_when_nothing_is_wrong(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    scheduler = SimpleNamespace(get_jobs=lambda: [SimpleNamespace(id="minutely_tick", next_run_time=datetime.now() + timedelta(seconds=30))])
    deps = _deps(db, config, scheduler=scheduler)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert '<div class="verdict ok">' in body
    assert "ทุกอย่างปกติ" in body


async def test_verdict_stop_wins_over_warn_when_both_present(tmp_path, db, client_factory):
    """A dead job (stop) AND a nonempty ring buffer (warn) at once -- the
    verdict must show STOP, naming only the stop-tier cause(s)."""
    config = _config(tmp_path, tmp_path / "habits.db")
    ring = RingBufferHandler(200)
    _log(ring, logging.WARNING, "habit_assistant.x", "a warning")
    scheduler = SimpleNamespace(get_jobs=lambda: [SimpleNamespace(id="daily_digest", next_run_time=None)])
    deps = _deps(db, config, scheduler=scheduler, ring=ring)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    match = re.search(r'<div class="verdict.*?</div>', body, re.DOTALL)
    assert match is not None
    verdict_html = match.group(0)
    assert "verdict stop" in verdict_html
    assert "daily_digest" in verdict_html
    assert "#errors" not in verdict_html  # the warn-tier cause is NOT named once stop wins


async def test_verdict_multi_cause_renders_a_ul_with_each_link(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", digest={"mode": "digest", "warn_cap": 100})
    for _ in range(85):
        db.increment_push("Ua", datetime.now().strftime("%Y-%m"))
    ring = RingBufferHandler(200)
    _log(ring, logging.WARNING, "habit_assistant.x", "a warning")
    deps = _deps(db, config, ring=ring)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "things to check" in body or "เรื่องต้องดู" in body
    assert '<li><a href="#errors">' in body
    assert '<li><a href="/quota">' in body


# ===========================================================================
# Needs-you banner: rendered only when pending >= 1.
# ===========================================================================


async def test_needs_you_absent_when_no_pending_users(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert 'class="needs"' not in body


async def test_needs_you_present_when_pending_users_exist(tmp_path, db, client_factory):
    db.upsert_user("Upending000000000000000000000000", status="pending")
    config = _config(tmp_path, tmp_path / "habits.db")
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert 'class="needs" href="/users"' in body
    assert 'class="pending"' in body  # nav badge also reflects it


# ===========================================================================
# i18n / R-I18N-1 / AC31: bilingual, resolved language drives every string.
# ===========================================================================


async def test_page_renders_english_when_forced(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert '<html lang="en">' in body
    assert "All good" in body
    assert "Version" in body and "Channel" in body and "Uptime" in body


async def test_page_renders_thai_by_default(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert '<html lang="th">' in body
    assert "ทุกอย่างปกติ" in body


# ===========================================================================
# Escaping / XSS discipline: every dynamic value passes through escape().
# ===========================================================================


async def test_hostile_job_id_is_escaped_everywhere_it_appears(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    hostile = "<script>alert(1)</script>"
    scheduler = SimpleNamespace(get_jobs=lambda: [SimpleNamespace(id=hostile, next_run_time=None)])
    deps = _deps(db, config, scheduler=scheduler)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


async def test_hostile_log_message_is_escaped(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    ring = RingBufferHandler(200)
    _log(ring, logging.WARNING, "habit_assistant.x", "<img src=x onerror=alert(1)>")
    deps = _deps(db, config, ring=ring)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "<img src=x" not in body
    assert "&lt;img" in body


# ===========================================================================
# Route wiring / integration: registered on the real portal server, gated
# by identity_gate like every other route.
# ===========================================================================


async def test_status_route_requires_identity_header(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    deps = _deps(db, config)
    server = PortalServer(bind_host="127.0.0.1", bind_port=0, deps=deps, modules=[status.register])
    client = TestClient(TestServer(server.build_app()))
    await client.start_server()
    try:
        resp = await client.get("/")  # no header
        assert resp.status == 403
    finally:
        await client.close()


async def test_status_registered_via_the_real_registered_modules_list(tmp_path, db):
    """Proves this module actually wired itself into `core/portal/server.
    py:REGISTERED_MODULES` (the sanctioned integration point), not just
    that its own `register` function works in isolation."""
    from habit_assistant.core.portal.server import REGISTERED_MODULES

    assert status.register in REGISTERED_MODULES


async def test_content_type_is_html(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    deps = _deps(db, config)
    client = await client_factory(deps)
    resp = await client.get("/", headers={"Tailscale-User-Login": "owner@example.com"})
    assert resp.status == 200
    assert resp.headers["content-type"].startswith("text/html")


async def test_page_shell_carries_as_of_and_nav_current(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert 'aria-current="page"' in body
    assert "ข้อมูล ณ " in body  # "As of {time}" footer, mandatory on every page (UX.md §4)
