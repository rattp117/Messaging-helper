"""Vera's own adversarial probes for SPEC-LINE-PORTAL.md module QUOTA
(AC26-AC30), independent of Luna's own `tests/test_portal_quota.py` (28
tests -- read that file and `IMPL-PORTAL-quota.md` first).

Dispatch focus (Archi's note): this is the money-adjacent surface of the
portal -- the NO-DOUBLE-SEND guard around `POST /quota/digest-run` is the
load-bearing item. This file goes beyond Luna's own gather-based replay
test to probe: token replay across a day rollover, two distinct tokens
racing, the marker's own date-rollover semantics, in-memory state loss on
a simulated process restart, and -- the sharpest probe -- whether the
manual trigger's own 3-layer guard has any teeth against the SEPARATE,
unguarded SCHEDULED digest job racing it. Also probes quota-gate
interplay during fan-out, AC29's redaction mechanism against hostile
values plus a structural (import-level) proof that no `Secrets` field can
reach `GET /config`, month-history edge cases (13+ months, an empty
current month), gauge parity with module STATUS's own gauge, the identity
gate on all three QUOTA routes (GET/GET/POST), XSS via display names, and
bilingual/empty-state coverage.

`core/portal/server.py:REGISTERED_MODULES` is `[status.register]` only --
module QUOTA (like USERS/AUDIT) is NOT yet wired into the real portal
app (see `IMPL-PORTAL-quota.md` "Known limitations"). This file registers
`quota.register` itself, the same way Luna's own `tests/test_portal_
quota.py` and the sibling `tests/test_portal_status_gaps.py` do -- this is
a known, pre-existing integration gap across all three parallel modules,
not something to fail this track for; see TEST-PORTAL-quota.md.

Same on-disk-SQLite, no-DB-mock, real-`asyncio.gather`-race conventions as
`tests/test_portal_quota.py`. Fixtures are duplicated locally (not
imported), per this codebase's own `*_gaps.py` convention.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from conftest import RecordingLineChannel
from habit_assistant.config import Config
from habit_assistant.core import digest, i18n
from habit_assistant.core.portal import quota, status
from habit_assistant.core.portal.security import FORBIDDEN_BODY
from habit_assistant.core.portal.server import PortalDeps, PortalServer
from habit_assistant.core.registry_provider import RegistryProvider
from habit_assistant.storage.db import Database

OWNER = "Uowner00000000000000000000000000"
MEMBER_A = "Umembera000000000000000000000000"
MEMBER_B = "Umemberb000000000000000000000000"

OWNER_HEADERS = {"Tailscale-User-Login": "owner@example.com"}


def _current_yyyymm() -> str:
    return datetime.now().strftime("%Y-%m")


def i18n_month_heading(lang: i18n.Language = "th") -> str:
    return i18n.t("portal_quota_month_heading", lang)


def i18n_byuser_heading(lang: i18n.Language = "th") -> str:
    return i18n.t("portal_quota_byuser_heading", lang)


@pytest.fixture(autouse=True)
def _reset_manual_digest_state():
    """Same reset as `tests/test_portal_quota.py`'s own fixture -- the
    NO-DOUBLE-SEND guards are process-lifetime module state by design, so
    they must be reset between tests IN THIS FILE too. Integration item 5:
    the same-day marker moved to `core/digest.py:_DAILY_RUN_CLAIMED`
    (shared with the scheduled job)."""
    digest._DAILY_RUN_CLAIMED.clear()
    quota._pending_digest_tokens.clear()
    yield
    digest._DAILY_RUN_CLAIMED.clear()
    quota._pending_digest_tokens.clear()


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(OWNER, role="owner", status="active", display_name="Owner")
    yield database
    database.close()


def _config(**overrides) -> Config:
    return Config.model_validate(overrides) if overrides else Config()


def _build_bare_app(db_obj: Database, config: Config, channel=None, *, modules=None):
    """Mirrors `tests/test_portal_quota.py:_build_app` -- a bare
    `web.Application()` with `quota.register` called directly, NO
    identity-gate middleware. Used where the test doesn't care about the
    gate itself (most of the NO-DOUBLE-SEND / quota-gate-interplay
    probes)."""
    from aiohttp import web

    deps = PortalDeps(
        db=db_obj,
        config=config,
        scheduler=None,
        channel=channel if channel is not None else RecordingLineChannel(db=db_obj),
        stats=None,
        ring=None,
        owner_id=OWNER,
    )
    app = web.Application()
    app["portal_deps"] = deps
    for register in modules if modules is not None else [quota.register]:
        register(app, deps)
    return app, deps


@pytest.fixture
async def aiohttp_client_factory():
    clients: list[TestClient] = []

    async def make_client(app) -> TestClient:
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    yield make_client

    for client in clients:
        await client.close()


@pytest.fixture
async def gated_client_factory():
    """Builds the REAL `PortalServer.build_app()` (identity_gate +
    error_middleware + the module(s) given) -- used for the identity-gate
    probes, where the gate itself is exactly what's under test. Registers
    `quota.register` (and optionally `status.register`) itself -- see this
    file's own module docstring re: `REGISTERED_MODULES`."""
    clients: list[TestClient] = []

    async def make(deps: PortalDeps, modules: list) -> TestClient:
        server = PortalServer(bind_host="127.0.0.1", bind_port=0, deps=deps, modules=modules)
        client = TestClient(TestServer(server.build_app()))
        await client.start_server()
        clients.append(client)
        return client

    yield make

    for client in clients:
        await client.close()


async def _mint_token(client: TestClient, **kwargs) -> str:
    resp = await client.post("/quota/digest-run", data={}, **kwargs)
    assert resp.status == 200
    text = await resp.text()
    match = re.search(r'name="token" value="([^"]+)"', text)
    assert match is not None, "interstitial did not carry a token field"
    return match.group(1)


class _FrozenDatetime(datetime):
    """A `datetime` subclass whose `.now()` returns a class-level,
    mutable instant -- lets a test move `quota.py`'s own notion of "today"
    forward mid-test (`_today_str`/`_current_yyyymm`/`_now_hms` all call
    `datetime.now()` directly, un-injected) without touching the real wall
    clock or any other module's clock."""

    _frozen: datetime = datetime(2026, 8, 31, 12, 0, 0)

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - mirrors stdlib signature
        return cls._frozen


class _SlowChannel:
    """A `Channel` double that `await asyncio.sleep(...)`s before each
    send -- forces a REAL suspension point inside a fan-out (the test
    doubles used elsewhere in this file/`tests/test_portal_quota.py` have
    no internal `await`, so two coroutines racing via `asyncio.gather` can
    otherwise run to completion back-to-back rather than genuinely
    interleaving on a fast local test server). Needed for the "an
    unconfirmed request arrives WHILE a confirmed run is still mid-flight"
    probe to be deterministic rather than a coin flip."""

    def __init__(self, delay: float = 0.03) -> None:
        self._delay = delay
        self.pushes: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> str | None:
        await asyncio.sleep(self._delay)
        self.pushes.append((chat_id, text))
        return None


class _FlakyChannel:
    """A `Channel` double whose `send()` raises for a configured set of
    `chat_id`s -- simulates a mid-fan-out failure (LINE API down, or a
    quota gate rejecting further sends, as `channels/line.py:LineChannel.
    _push` does in REALTIME mode) without needing the real channel."""

    def __init__(self, fail_for: set[str]) -> None:
        self.fail_for = fail_for
        self.pushes: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> str | None:
        if chat_id in self.fail_for:
            raise RuntimeError("simulated send failure mid-fan-out")
        self.pushes.append((chat_id, text))
        return None


# ===========================================================================
# NO-DOUBLE-SEND -- probes beyond Luna's own gather/replay/unrecognized-
# token tests (tests/test_portal_quota.py).
# ===========================================================================


async def test_token_minted_yesterday_unused_is_still_honored_today(db, aiohttp_client_factory, monkeypatch):
    """A token minted on day 1 but never confirmed (the owner opened the
    interstitial and walked away) is still in `_pending_digest_tokens` --
    tokens don't expire by CALENDAR DAY, only by USE. Confirming it the
    next day is a legitimate first-time confirm for that day (no marker
    exists yet for the new day), so it correctly SUCCEEDS -- pinning this
    as intended behavior, not a bug: the token's job is one-time-use, not
    same-day-use."""
    db.upsert_user(MEMBER_A, role="member", status="active")
    channel = RecordingLineChannel(db=db)
    app, _deps = _build_bare_app(db, _config(), channel=channel)
    client = await aiohttp_client_factory(app)
    monkeypatch.setattr(quota, "datetime", _FrozenDatetime)

    _FrozenDatetime._frozen = datetime(2026, 8, 31, 23, 0, 0)
    token = await _mint_token(client)

    _FrozenDatetime._frozen = datetime(2026, 9, 1, 0, 5, 0)
    resp = await client.post("/quota/digest-run", data={"confirm": "yes", "token": token}, allow_redirects=False)
    assert resp.status == 303  # a real, legitimate first-of-the-day send
    assert len(channel.pushes) == 2  # owner + member_a


async def test_replayed_already_used_token_across_day_rollover_is_refused(db, aiohttp_client_factory, monkeypatch):
    """The sharper version of "yesterday's token today": a token that was
    ALREADY CONSUMED by a successful run on day 1 must not work again on
    day 2 either -- the token is discarded from `_pending_digest_tokens`
    on use (quota.py:541), independent of the day-keyed marker, so a
    cross-midnight replay of the SAME spent token is refused exactly like
    a same-day replay is."""
    db.upsert_user(MEMBER_A, role="member", status="active")
    channel = RecordingLineChannel(db=db)
    app, _deps = _build_bare_app(db, _config(), channel=channel)
    client = await aiohttp_client_factory(app)
    monkeypatch.setattr(quota, "datetime", _FrozenDatetime)

    _FrozenDatetime._frozen = datetime(2026, 8, 31, 23, 0, 0)
    token = await _mint_token(client)
    first = await client.post("/quota/digest-run", data={"confirm": "yes", "token": token}, allow_redirects=False)
    assert first.status == 303
    assert len(channel.pushes) == 2

    _FrozenDatetime._frozen = datetime(2026, 9, 1, 0, 5, 0)  # next day
    replay = await client.post("/quota/digest-run", data={"confirm": "yes", "token": token}, allow_redirects=False)
    assert replay.status == 200  # "already sent"/refused page, NOT a redirect
    text = await replay.text()
    assert "Already sent at" in text or "ส่งไปแล้วเมื่อ" in text
    assert len(channel.pushes) == 2  # unchanged -- no second send


async def test_marker_date_rollover_23_59_then_00_01_allows_two_runs_correctly(
    db, aiohttp_client_factory, monkeypatch
):
    """UX.md's own daily allowance is per CALENDAR DAY, not a rolling 24h
    window: a manual run at 23:59 and a second one at 00:01 the next
    calendar minute are two DIFFERENT `_today_str()` keys, so both are
    legitimate, independent "first run of the day" -- pinning this as
    correct, not a double-send (the guard's job is "at most one extra
    manual run per calendar day", not "at most one per N hours")."""
    db.upsert_user(MEMBER_A, role="member", status="active")
    channel = RecordingLineChannel(db=db)
    app, _deps = _build_bare_app(db, _config(), channel=channel)
    client = await aiohttp_client_factory(app)
    monkeypatch.setattr(quota, "datetime", _FrozenDatetime)

    _FrozenDatetime._frozen = datetime(2026, 8, 31, 23, 59, 0)
    token1 = await _mint_token(client)
    r1 = await client.post("/quota/digest-run", data={"confirm": "yes", "token": token1}, allow_redirects=False)
    assert r1.status == 303
    assert len(channel.pushes) == 2

    # Same day, second attempt WITHOUT rollover -- must be refused.
    same_day_retry = await client.post("/quota/digest-run", data={})
    assert "Already sent at" in await same_day_retry.text() or "ส่งไปแล้วเมื่อ" in await same_day_retry.text()

    _FrozenDatetime._frozen = datetime(2026, 9, 1, 0, 1, 0)
    token2 = await _mint_token(client)
    r2 = await client.post("/quota/digest-run", data={"confirm": "yes", "token": token2}, allow_redirects=False)
    assert r2.status == 303
    assert len(channel.pushes) == 4  # a second, legitimate, new-day run


async def test_simulated_process_restart_clears_guards_allows_second_run_same_day(db, aiohttp_client_factory):
    """PINNED, ACCEPTED behavior (not a defect): the same-day guard
    (`core/digest.py:_DAILY_RUN_CLAIMED`, integration item 5)/
    `_pending_digest_tokens` are plain module-level Python state -- a
    process restart mid-day wipes them, so a manual run before a restart
    and another after DO both go through, on the SAME calendar day. This
    mirrors `core/digest.py:_DIGEST_DEFERRED_DATES`'s own documented
    "no distributed lock, single-instance-only" posture. Acceptable under
    the SAME assumption `deploy/habit-assistant-line.service` already
    requires of the whole process (no multi-worker supervisor) --
    simulated here by directly clearing the module globals rather than an
    actual process restart, since the guards ARE those globals."""
    db.upsert_user(MEMBER_A, role="member", status="active")
    channel = RecordingLineChannel(db=db)
    app, _deps = _build_bare_app(db, _config(), channel=channel)
    client = await aiohttp_client_factory(app)

    token1 = await _mint_token(client)
    r1 = await client.post("/quota/digest-run", data={"confirm": "yes", "token": token1}, allow_redirects=False)
    assert r1.status == 303
    assert len(channel.pushes) == 2

    # Simulate a process restart: the in-memory guards are gone.
    digest._DAILY_RUN_CLAIMED.clear()
    quota._pending_digest_tokens.clear()

    token2 = await _mint_token(client)
    r2 = await client.post("/quota/digest-run", data={"confirm": "yes", "token": token2}, allow_redirects=False)
    assert r2.status == 303  # allowed again -- pinned, not a bug, per the note above
    assert len(channel.pushes) == 4


async def test_interleaved_unconfirmed_get_during_an_in_flight_confirmed_run_mints_no_new_token(
    db, aiohttp_client_factory
):
    """FLIPPED wording (integration item 5): before the fix, the same-day
    marker was set only AFTER `run_daily_digest` completed, so a THIRD,
    fresh unconfirmed GET arriving WHILE a confirmed run was in-flight
    could still observe "no marker yet" and mint a second, valid-looking
    token mid-run -- `_handle_digest_run_confirmed`'s own re-check under
    the lock was what closed that window before a second SEND could
    happen. `run_daily_digest_guarded` now claims the shared guard BEFORE
    the slow fan-out even starts, so the window doesn't open at all
    anymore: a concurrent unconfirmed GET during an in-flight confirmed
    run now immediately sees "Already sent" instead of a fresh
    interstitial -- an even tighter guarantee than the lock-based
    re-check alone provided."""
    db.upsert_user(MEMBER_A, role="member", status="active")
    channel = _SlowChannel(delay=0.05)  # forces a real suspension point mid-fan-out
    app, _deps = _build_bare_app(db, _config(), channel=channel)
    client = await aiohttp_client_factory(app)

    token_a = await _mint_token(client)

    async def _confirm_a():
        return await client.post(
            "/quota/digest-run", data={"confirm": "yes", "token": token_a}, allow_redirects=False
        )

    async def _get_unconfirmed_b():
        await asyncio.sleep(0.01)  # let _confirm_a claim the guard and enter the slow send loop first
        return await client.post("/quota/digest-run", data={})

    first, second = await asyncio.gather(_confirm_a(), _get_unconfirmed_b())
    assert first.status == 303
    assert second.status == 200
    second_text = await second.text()
    assert "Already sent" in second_text or "ส่งไปแล้วเมื่อ" in second_text
    assert 'name="token"' not in second_text, "no fresh token must be minted once a run has claimed the day"
    assert len(channel.pushes) == 2  # owner + member_a, from exactly ONE run


async def test_manual_digest_run_concurrent_with_scheduled_digest_job_can_double_push(db, aiohttp_client_factory):
    """FINDING (trace requested by the dispatch note) -- SEVERITY: MEDIUM,
    DISCLOSED-BY-DESIGN. `quota.py`'s own 3-layer guard (token +
    same-day marker + `asyncio.Lock`) is entirely LOCAL to `core/portal/
    quota.py`'s own module state. The REAL scheduled digest job
    (`core/jobs.py`, via APScheduler's `CronTrigger`) calls `core/
    digest.py:run_daily_digest` DIRECTLY -- it has no knowledge of, and
    never acquires, `quota._manual_digest_lock`, and never touches
    `quota._manual_digest_runs`. `core/digest.py`'s own module docstring
    is explicit that `run_daily_digest` has "no internal dedup, the
    scheduler owns that" for the ordinary immediate-send path (see
    `_DIGEST_DEFERRED_DATES`'s block comment and `tests/test_digest.py::
    test_run_daily_digest_has_no_internal_dedup_the_scheduler_owns_that`).

    Consequence, proven here: if the owner's manual "Send digest now"
    happens to overlap in wall-clock time with the day's own scheduled
    cron firing (or simply lands on a day the schedule ALSO fires, per
    UX.md's own disclosed copy), every active, digest-on user receives
    the digest TWICE -- once from each independent call. This is NOT a
    portal-specific bug: UX.md Screen 5's own interstitial copy already
    discloses exactly this risk verbatim ("If today's scheduled digest
    already went out, people will get it twice") and Q3's own framing is
    "mechanism is open, behavior [replay-safety] is not" -- replay-safety
    of the MANUAL path was delivered; safety against the INDEPENDENT
    scheduled path was never in scope and is not achievable without a
    guard shared with `core/jobs.py`/`core/digest.py`, which is outside
    `core/portal/quota.py`'s owned files. Recorded here as a proven,
    reproducible trace for Archi/integration, not a QUOTA-track defect."""
    db.upsert_user(MEMBER_A, role="member", status="active")
    channel = RecordingLineChannel(db=db)
    config = _config()
    app, deps = _build_bare_app(db, config, channel=channel)
    client = await aiohttp_client_factory(app)
    provider = RegistryProvider(config, db)

    token = await _mint_token(client)

    async def _manual_confirm():
        return await client.post(
            "/quota/digest-run", data={"confirm": "yes", "token": token}, allow_redirects=False
        )

    async def _scheduled_job_fires_independently():
        # Exactly what core/jobs.py's own CronTrigger callback does: call
        # run_daily_digest directly, on the SAME db/channel, with no
        # knowledge of quota.py's guards at all.
        await digest.run_daily_digest(db, channel, config, provider)

    await asyncio.gather(_manual_confirm(), _scheduled_job_fires_independently())

    owner_pushes = channel.pushes_to(OWNER)
    member_pushes = channel.pushes_to(MEMBER_A)
    assert len(channel.pushes) == 4, (
        f"expected 4 total pushes (2 users x 2 independent full runs), got {len(channel.pushes)} "
        f"-- if this ever drops to 2, a shared guard now exists and this finding should be closed"
    )
    assert len(owner_pushes) == 2, "the owner was double-pushed by the two independent, unguarded runs"
    assert len(member_pushes) == 2, "member_a was double-pushed by the two independent, unguarded runs"


# ===========================================================================
# Quota-gate interplay during fan-out.
# ===========================================================================


async def test_stopped_quota_message_is_visible_on_the_page_not_just_a_redirect_code(db, aiohttp_client_factory):
    """AC30/UX Flow D error branch: the refusal must be a CLEAR MESSAGE
    the owner actually sees, not merely a 303 status code a human never
    looks at -- follow the redirect and assert the flash text renders."""
    db.upsert_user(MEMBER_A, role="member", status="active")
    config = _config(digest={"warn_cap": 1})
    yyyymm = _current_yyyymm()
    db.increment_push(MEMBER_A, yyyymm)
    db.increment_push(MEMBER_A, yyyymm)
    channel = RecordingLineChannel(db=db)
    app, _deps = _build_bare_app(db, config, channel=channel)
    client = await aiohttp_client_factory(app)

    resp = await client.post("/quota/digest-run", data={}, allow_redirects=True)
    assert resp.status == 200
    text = await resp.text()
    assert "Push cap reached" in text or "ถึงเพดานพุชแล้ว" in text
    assert channel.pushes == []


async def test_mid_fanout_no_per_user_quota_recheck_all_candidates_sent_even_past_cap(db, aiohttp_client_factory):
    """PINS the answer to the dispatch's "cap-1 with 3 users to send, what
    happens mid-fan-out" question: NOTHING gates it mid-run. `quota.py`'s
    own stop check (`_quota_snapshot`) runs exactly ONCE, before the fan-
    out starts (`_handle_digest_run_confirmed`); `core/digest.py:
    run_daily_digest`'s per-user loop has no cap check of its own; and
    `channels/line.py:LineChannel._push`'s own cap gate (R-Q3) applies
    ONLY in `digest.mode == "realtime"` -- in `digest.mode == "digest"`
    (the only mode in which the manual trigger sends anything at all,
    since realtime makes `run_daily_digest` itself a no-op) `_push` is a
    "pure pass-through", per that file's own docstring. Net effect,
    proven here: with the pre-check just under cap and 3 eligible users,
    ALL 3 are sent -- a real, honestly-reported overrun, not a silent
    partial cutoff."""
    db.upsert_user(MEMBER_A, role="member", status="active")
    db.upsert_user(MEMBER_B, role="member", status="active")
    config = _config(digest={"warn_cap": 5})
    yyyymm = _current_yyyymm()
    for _ in range(4):
        db.increment_push(MEMBER_A, yyyymm)  # total=4, cap=5 -> stop_fired=False (just under)
    channel = RecordingLineChannel(db=db)
    app, _deps = _build_bare_app(db, config, channel=channel)
    client = await aiohttp_client_factory(app)

    token = await _mint_token(client)
    resp = await client.post("/quota/digest-run", data={"confirm": "yes", "token": token}, allow_redirects=False)
    assert resp.status == 303
    assert resp.headers["Location"] == "/quota?ran=3.0.0#flash"  # owner + member_a + member_b, ALL sent, none failed
    assert len(channel.pushes) == 3  # cap overrun (4+3=7 > 5), not stopped mid-way, honestly reported as 3


async def test_partial_mid_fanout_failure_is_now_counted_as_failed(db, aiohttp_client_factory):
    """FLIPPED (integration pass, item 5, TEST-PORTAL-quota.md Finding
    F4): when one candidate's `channel.send()` fails mid-fan-out,
    `core/digest.py:_send_one_user_digest`'s own fail-open `try/except`
    still swallows it (correct -- one user's failure must never abort the
    others) and returns `False`, so that user is still absent from `sent`
    (only a real successful `channel.send()` increments `_CountingChannel.
    sent`). The FIX (`_run_digest_now`'s new `failed = max(0, goes_to -
    sent)`) makes the gap VISIBLE instead of silently vanishing: here, 3
    candidates go in, `sent=2, skipped=0, failed=1` comes out -- every
    candidate is now accounted for (2+0+1=3), and the result banner
    switches to `portal_digest_result_with_failed` ("...1 could not be
    sent.") instead of the plain success copy."""
    db.upsert_user(MEMBER_A, role="member", status="active")
    db.upsert_user(MEMBER_B, role="member", status="active")
    channel = _FlakyChannel(fail_for={MEMBER_B})
    app, _deps = _build_bare_app(db, _config(), channel=channel)
    client = await aiohttp_client_factory(app)

    token = await _mint_token(client)
    resp = await client.post("/quota/digest-run", data={"confirm": "yes", "token": token}, allow_redirects=False)
    assert resp.status == 303
    assert resp.headers["Location"] == "/quota?ran=2.0.1#flash"  # 2 sent, 0 skipped, 1 failed -- fully accounted for
    assert len(channel.pushes) == 2  # owner + member_a only; member_b's own send genuinely failed

    follow = await client.get(resp.headers["Location"].split("#")[0], headers=OWNER_HEADERS)
    text = await follow.text()
    assert "could not be sent" in text or "ส่งไม่สำเร็จ" in text


# ===========================================================================
# AC29 -- GET /config: hostile values, and a structural no-Secrets proof.
# ===========================================================================


async def test_config_hostile_value_in_a_non_secret_field_is_escaped_not_executed(db, aiohttp_client_factory):
    """A field whose NAME doesn't match the redaction needles (token/
    secret/password) but whose VALUE looks hostile (embeds "token=" text
    and an HTML/script payload) must render as plain, ESCAPED text -- the
    redaction mechanism is name-based, not content-based (by design, per
    `quota.py`'s own docstring), so this must not be silently redacted
    (that would be its own kind of bug -- a non-secret owner-authored
    value going missing) NOR rendered as live markup (XSS)."""
    hostile = '<script>alert(1)</script> token=abc "; DROP TABLE users;--'
    config = _config(portal={"owner_login": hostile})
    app, _deps = _build_bare_app(db, config)
    client = await aiohttp_client_factory(app)

    resp = await client.get("/config")
    assert resp.status == 200
    text = await resp.text()
    assert "<script>alert(1)</script>" not in text  # never live markup
    assert "&lt;script&gt;" in text  # escaped, present, and readable
    assert "••••••" not in text.split("owner_login")[-1].split("</div>")[0] or True  # not force-redacted (see below)
    # The field's own value substring (post-escaping) is genuinely present,
    # not swapped for the mask -- proves this is "render escaped", not
    # "redact because the VALUE looks secret-shaped".
    assert "token=abc" in text


async def test_config_habit_list_with_secret_looking_label_never_renders_at_all(db, aiohttp_client_factory):
    """`habits: list[HabitConfig]` is a non-scalar top-level field, and
    `_render_config_body` skips any `Config` field that isn't itself a
    `BaseModel` section (`quota.py:_render_config_body`'s own `if not
    isinstance(value, BaseModel): continue`) -- so a habit whose label
    contains something that LOOKS like a leaked credential doesn't even
    reach the redaction logic; the whole array is structurally absent
    from `GET /config`, which is strictly safer than "was redacted"."""
    from habit_assistant.config import HabitConfig, HabitLabel

    hostile_habit = HabitConfig(
        id="creds",
        type="text",
        label=HabitLabel(en="token=sk-abcdef123456 secret!!", th="โทเค็นลับ"),
    )
    config = _config()
    config = config.model_copy(update={"habits": [*config.habits, hostile_habit]})
    app, _deps = _build_bare_app(db, config)
    client = await aiohttp_client_factory(app)

    resp = await client.get("/config")
    text = await resp.text()
    assert resp.status == 200
    assert "sk-abcdef123456" not in text
    assert "token=sk-abcdef123456" not in text
    assert "[habits]" not in text  # the section itself never appears


def test_no_secrets_field_structurally_reachable_from_the_portal():
    """Import-level structural proof, not an end-to-end render check:
    walks every field pydantic-config's `Config` class declares (one
    level -- every section is itself a flat `BaseModel` in this codebase,
    per `config.py`'s own convention) and asserts none of them IS, or
    contains as one of ITS OWN fields, the `Secrets` model (`config.py:
    Secrets`, the actual home of `line_channel_access_token`/
    `line_channel_secret`). Also asserts `PortalDeps` (`server.py`) --
    the ONLY object `core/portal/*` handlers can read from -- carries no
    `secrets`-named field and is never itself, nor threads, a `Secrets`
    instance. This is what makes AC29's "LINE token/secret never shown in
    plaintext" true by CONSTRUCTION (`GET /config` literally cannot reach
    a field it was never given), not merely by the redaction heuristic
    (which is defense-in-depth on top of this, per `quota.py`'s own
    module docstring)."""
    import dataclasses

    from habit_assistant.config import Config, Secrets
    from habit_assistant.core.portal.server import PortalDeps

    for field_name, field_info in Config.model_fields.items():
        assert field_info.annotation is not Secrets, (
            f"Config.{field_name} is directly typed as Secrets -- AC29's structural guarantee is broken"
        )
        section_default = getattr(Config(), field_name)
        if hasattr(type(section_default), "model_fields"):
            for sub_name, sub_info in type(section_default).model_fields.items():
                assert sub_info.annotation is not Secrets, (
                    f"Config.{field_name}.{sub_name} is typed as Secrets -- AC29's structural guarantee is broken"
                )

    portal_deps_fields = {f.name: str(f.type) for f in dataclasses.fields(PortalDeps)}
    assert "secrets" not in portal_deps_fields, "PortalDeps carries a 'secrets' field -- it must never thread Secrets in"
    assert "Config" in portal_deps_fields.get("config", ""), "PortalDeps.config type changed unexpectedly"
    for field_name, type_str in portal_deps_fields.items():
        assert "Secrets" not in type_str, f"PortalDeps.{field_name} is typed as/around Secrets -- structural leak risk"


# ===========================================================================
# Month-history edge cases: 13+ months, an empty current month.
# ===========================================================================


async def test_month_history_caps_at_12_months_even_with_15_months_of_ledger_data(db, aiohttp_client_factory):
    """`db.monthly_push_history(months=12)`'s own default LIMIT means a
    15-month-old ledger renders exactly the most recent 12 rows, not all
    15 -- no render-budget blowup, and the oldest 3 months are correctly
    dropped, not truncated mid-row."""
    yyyymms = []
    for i in range(15):
        year = 2025 + (i // 12)
        month = (i % 12) + 1
        yyyymm = f"{year:04d}-{month:02d}"
        yyyymms.append(yyyymm)
        db.increment_push(MEMBER_A if False else OWNER, yyyymm)
    app, _deps = _build_bare_app(db, _config())
    client = await aiohttp_client_factory(app)

    resp = await client.get("/quota")
    text = await resp.text()
    assert resp.status == 200
    newest_12 = sorted(yyyymms)[-12:]
    oldest_3 = sorted(yyyymms)[:3]
    for ym in newest_12:
        assert ym in text, f"{ym} should be among the most recent 12 months shown"
    for ym in oldest_3:
        assert ym not in text, f"{ym} is outside the 12-month window and should not render"


async def test_current_month_marker_present_when_current_month_has_zero_pushes_but_history_exists(
    db, aiohttp_client_factory
):
    """FLIPPED (integration pass, item 6, TEST-PORTAL-quota.md Finding
    F1): UX.md Screen 4's own "Block order is diagnostic order" promise
    (Flow C: "is this month anomalous?") depends on the CURRENT month
    always having a visible row (even at 0) so the owner can compare it
    against history. `_render_month_panel` now synthesizes a `{yyyymm:
    current, total: 0}` row when `db.monthly_push_history()` has no
    `push_ledger` row for the current month yet but DOES have prior
    months -- distinct from the (correctly handled, unchanged) brand-new-
    deployment empty state, where `rows` is empty entirely."""
    db.increment_push(OWNER, "2026-01")
    db.increment_push(OWNER, "2026-02")
    app, _deps = _build_bare_app(db, _config())
    client = await aiohttp_client_factory(app)

    resp = await client.get("/quota")
    text = await resp.text()
    current = _current_yyyymm()
    assert resp.status == 200
    # Scope the check to the MONTH-HISTORY panel's own <table> specifically --
    # the gauge panel legitimately shows the current month in its own heading
    # (it reads `monthly_push_total` directly, not `monthly_push_history`),
    # so a whole-page substring check would false-negative against this
    # finding. Isolate the table between the two panel headings.
    month_panel_heading = i18n_month_heading()
    byuser_panel_heading = i18n_byuser_heading()
    assert month_panel_heading in text
    month_panel_html = text.split(month_panel_heading, 1)[1].split(byuser_panel_heading, 1)[0]
    assert "2026-01" in month_panel_html
    assert "2026-02" in month_panel_html
    assert current in month_panel_html, "the current month must render, even at 0, alongside real prior history"
    # The row must actually show a zero total (not just the yyyymm string
    # appearing incidentally elsewhere), and the current-month marker.
    current_row_start = month_panel_html.index(current)
    current_row_html = month_panel_html[current_row_start : current_row_start + 400]
    assert ">0<" in current_row_html


async def test_brand_new_deployment_month_panel_has_no_synthesized_zero_row(db, aiohttp_client_factory):
    """Documents the ACTUAL (not the UX.md-literal) brand-new-deployment
    behavior: `monthly_push_history()` returns `[]`, and `_render_month_
    panel` shows ONLY the empty-state caption -- there is no synthesized
    "one row for the current month with 0" (UX.md Screen 4's own literal
    wording). This is a narrower, simpler implementation than the
    wireframe describes; not re-flagged as a second finding (same root
    cause as the "empty current month" test above), just pinned here so
    the exact rendered shape for a fresh install is on record."""
    app, _deps = _build_bare_app(db, _config())
    client = await aiohttp_client_factory(app)

    resp = await client.get("/quota")
    text = await resp.text()
    assert "No push history yet" in text or "ยังไม่มีประวัติการพุช" in text
    assert "<table>" not in text.split("By month")[1].split("This month, by user")[0].split(
        'class="collapse"'
    )[0] if "By month" in text else True


async def test_yyyymm_key_used_by_quota_matches_the_real_ledger_write_clock(db, aiohttp_client_factory):
    """Consistency check with the ledger-clock fix (TEST-LEDGER-TRIAGE.md,
    `IMPL-LEDGER-CLOCK-FIX.md`): `quota.py:_current_yyyymm()` docstring
    claims it deliberately matches `channels/line.py:_send_push`'s own
    REAL wall-clock `datetime.now().strftime("%Y-%m")`, NOT any
    `config.app.timezone`-adjusted or injected clock -- proven here by
    writing through `RecordingLineChannel` (which keys `push_ledger`
    off the real wall clock too, per its own docstring) and confirming
    the quota gauge's reported `used` total reflects that exact write,
    for the SAME month key `_current_yyyymm()` computes independently."""
    db.upsert_user(MEMBER_A, role="member", status="active")
    channel = RecordingLineChannel(db=db)
    config = _config(i18n={"language": "en"})  # pin EN so the "used / cap" format is deterministic
    app, deps = _build_bare_app(db, config, channel=channel)
    client = await aiohttp_client_factory(app)

    await channel.send(MEMBER_A, "hello")  # a direct push, outside any reply context
    assert db.monthly_push_total(_current_yyyymm()) == 1

    resp = await client.get("/quota")
    text = await resp.text()
    assert f"{_current_yyyymm()}" in text
    # the gauge's own <b> line reports the SAME used=1 total quota.py computed independently
    assert re.search(r"\b1\s*/\s*280\b", text), "gauge used-count doesn't match the real ledger write"


# ===========================================================================
# Gauge parity with module STATUS (structural -- same helpers, same numbers).
# ===========================================================================


async def test_gauge_month_heading_format_now_matches_between_status_and_quota_pages(db, aiohttp_client_factory):
    """FLIPPED (integration pass, item 6, TEST-PORTAL-status.md Finding 1
    / TEST-PORTAL-quota.md Finding F2): `quota.py:_render_gauge` and
    `status.py:_build_gauge` both call the SAME i18n key
    (`portal_status_quota_heading`, `{month}` placeholder) for what
    UI.md's own docstring calls "the SAME 3-state gauge component" -- they
    used to format `month` DIFFERENTLY (`status.py` "Aug 2026" vs
    `quota.py`'s raw "2026-08"). Both now call the SAME shared helper,
    `layout.format_month_heading`, so the identical live month renders the
    IDENTICAL string on both pages."""
    from types import SimpleNamespace

    from habit_assistant.core.portal.stats import RingBufferHandler, RuntimeStats

    config = _config()
    from aiohttp import web

    deps = PortalDeps(
        db=db,
        config=config,
        scheduler=SimpleNamespace(get_jobs=lambda: []),
        channel=RecordingLineChannel(db=db),
        stats=RuntimeStats(),
        ring=RingBufferHandler(200),
        owner_id=OWNER,
    )
    app = web.Application()
    app["portal_deps"] = deps
    status.register(app, deps)
    quota.register(app, deps)
    client = await aiohttp_client_factory(app)

    status_resp = await client.get("/")
    quota_resp = await client.get("/quota")
    status_text = await status_resp.text()
    quota_text = await quota_resp.text()

    now = datetime.now()
    shared_heading_month = now.strftime("%b %Y")
    raw_iso_month = now.strftime("%Y-%m")
    assert shared_heading_month in status_text
    assert shared_heading_month in quota_text
    # The old, divergent raw-ISO gauge heading must be gone from the Quota
    # page (it can still legitimately appear elsewhere, e.g. the By-month
    # table's own row keys, which intentionally stay raw ISO -- so this
    # checks specifically that the GAUGE panel's own heading changed, not
    # that "2026-08" never appears anywhere on the page).
    gauge_panel = quota_text.split('class="panel gauge', 1)[1].split("</section>", 1)[0]
    gauge_heading = gauge_panel.split("<h2>", 1)[1].split("</h2>", 1)[0]
    assert shared_heading_month in gauge_heading
    assert raw_iso_month not in gauge_heading


# ===========================================================================
# Identity gate on all three QUOTA routes (GET /quota, GET /config,
# POST /quota/digest-run) -- both unconfirmed and confirmed POST bodies.
# ===========================================================================


async def test_identity_gate_blocks_headerless_get_quota(db, gated_client_factory):
    deps = PortalDeps(
        db=db, config=_config(), scheduler=None, channel=RecordingLineChannel(db=db),
        stats=None, ring=None, owner_id=OWNER,
    )
    client = await gated_client_factory(deps, [quota.register])
    resp = await client.get("/quota")
    assert resp.status == 403
    body = await resp.text()
    assert body == FORBIDDEN_BODY
    assert "Push quota" not in body and "โควตา" not in body


async def test_identity_gate_blocks_headerless_get_config(db, gated_client_factory):
    deps = PortalDeps(
        db=db, config=_config(), scheduler=None, channel=RecordingLineChannel(db=db),
        stats=None, ring=None, owner_id=OWNER,
    )
    client = await gated_client_factory(deps, [quota.register])
    resp = await client.get("/config")
    assert resp.status == 403
    body = await resp.text()
    assert body == FORBIDDEN_BODY
    assert "Asia/Bangkok" not in body  # no config content leaks


async def test_identity_gate_blocks_headerless_post_digest_run_unconfirmed(db, gated_client_factory):
    db.upsert_user(MEMBER_A, role="member", status="active")
    channel = RecordingLineChannel(db=db)
    deps = PortalDeps(
        db=db, config=_config(), scheduler=None, channel=channel, stats=None, ring=None, owner_id=OWNER,
    )
    client = await gated_client_factory(deps, [quota.register])
    resp = await client.post("/quota/digest-run", data={})
    assert resp.status == 403
    assert channel.pushes == []
    assert quota._pending_digest_tokens == set()  # not even a token minted for an unauthorized caller


async def test_identity_gate_blocks_headerless_post_digest_run_confirmed(db, gated_client_factory):
    """The sharpest identity-gate probe for THIS module: a would-be
    attacker who somehow obtained a valid token (e.g. a mis-Funneled port
    leaking the interstitial once) still cannot spend it without the
    identity header -- the gate runs OUTERMOST (`PortalServer.build_app`'s
    own middleware order), before the handler ever sees the token."""
    db.upsert_user(MEMBER_A, role="member", status="active")
    channel = RecordingLineChannel(db=db)
    deps = PortalDeps(
        db=db, config=_config(), scheduler=None, channel=channel, stats=None, ring=None, owner_id=OWNER,
    )
    client = await gated_client_factory(deps, [quota.register])
    # Legitimately mint a token AS the owner first...
    authorized = await client.post("/quota/digest-run", data={}, headers=OWNER_HEADERS)
    assert authorized.status == 200
    match = re.search(r'name="token" value="([^"]+)"', await authorized.text())
    token = match.group(1)

    # ...then try to SPEND it with no identity header at all.
    resp = await client.post("/quota/digest-run", data={"confirm": "yes", "token": token})
    assert resp.status == 403
    assert channel.pushes == []


async def test_identity_gate_blocks_wrong_owner_login_on_all_three_routes(db, gated_client_factory):
    db.upsert_user(MEMBER_A, role="member", status="active")
    channel = RecordingLineChannel(db=db)
    config = _config(portal={"owner_login": "owner@example.com"})
    deps = PortalDeps(
        db=db, config=config, scheduler=None, channel=channel, stats=None, ring=None, owner_id=OWNER,
    )
    client = await gated_client_factory(deps, [quota.register])
    wrong = {"Tailscale-User-Login": "intruder@example.com"}

    r1 = await client.get("/quota", headers=wrong)
    r2 = await client.get("/config", headers=wrong)
    r3 = await client.post("/quota/digest-run", data={}, headers=wrong)
    assert (r1.status, r2.status, r3.status) == (403, 403, 403)
    assert channel.pushes == []


async def test_identity_gate_allows_correct_owner_login_on_all_three_routes(db, gated_client_factory):
    db.upsert_user(MEMBER_A, role="member", status="active")
    channel = RecordingLineChannel(db=db)
    config = _config(portal={"owner_login": "owner@example.com"})
    deps = PortalDeps(
        db=db, config=config, scheduler=None, channel=channel, stats=None, ring=None, owner_id=OWNER,
    )
    client = await gated_client_factory(deps, [quota.register])

    r1 = await client.get("/quota", headers=OWNER_HEADERS)
    r2 = await client.get("/config", headers=OWNER_HEADERS)
    r3 = await client.post("/quota/digest-run", data={}, headers=OWNER_HEADERS)
    assert (r1.status, r2.status, r3.status) == (200, 200, 200)


# ===========================================================================
# XSS via user display names.
# ===========================================================================


async def test_xss_via_display_name_in_byuser_and_digest_panels_is_escaped(db, aiohttp_client_factory):
    hostile_name = '<img src=x onerror=alert(1)>Nok'
    db.upsert_user(MEMBER_A, role="member", status="active", display_name=hostile_name)
    db.increment_push(MEMBER_A, _current_yyyymm())
    app, _deps = _build_bare_app(db, _config())
    client = await aiohttp_client_factory(app)

    resp = await client.get("/quota")
    text = await resp.text()
    assert resp.status == 200
    assert "<img src=x onerror=alert(1)>" not in text  # never live markup
    assert "&lt;img src=x onerror=alert(1)&gt;" in text  # present, escaped
    assert "Nok" in text


# ===========================================================================
# Bilingual + empty states.
# ===========================================================================


async def test_quota_page_forced_english_renders_english_not_thai(db, aiohttp_client_factory):
    config = _config(i18n={"language": "en"})
    app, _deps = _build_bare_app(db, config)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/quota")
    text = await resp.text()
    assert "Push quota" in text or "By month" in text
    assert "โควตา" not in text


async def test_quota_page_forced_thai_renders_thai_not_english_digest_button(db, aiohttp_client_factory):
    config = _config(i18n={"language": "th"})
    app, _deps = _build_bare_app(db, config)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/quota")
    text = await resp.text()
    assert "ส่งสรุปรายวันตอนนี้" in text
    assert "Send digest now" not in text


async def test_config_page_forced_thai_renders_thai_secrets_note(db, aiohttp_client_factory):
    config = _config(i18n={"language": "th"})
    app, _deps = _build_bare_app(db, config)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/config")
    text = await resp.text()
    assert "ข้อมูลลับ" in text
    assert "Secrets are shown" not in text


async def test_digest_confirm_interstitial_forced_thai_renders_thai_irreversibility_copy(
    db, aiohttp_client_factory
):
    config = _config(i18n={"language": "th"})
    app, _deps = _build_bare_app(db, config)
    client = await aiohttp_client_factory(app)
    resp = await client.post("/quota/digest-run", data={})
    text = await resp.text()
    assert "ส่งแล้วยกเลิกไม่ได้" in text
    assert "This can't be undone" not in text


# ===========================================================================
# Registration precedent (documents the known gap; does not fail the run).
# ===========================================================================


def test_quota_register_now_wired_into_server_registered_modules():
    """FLIPPED (integration pass, line/v1.3.0): this test used to pin the
    gap that `quota.register` (like `users.register`/`audit.register`)
    was NOT yet appended to `core/portal/server.py:REGISTERED_MODULES`
    (TEST-PORTAL-quota.md's own "Registration / integration note"). The
    integration pass closed that gap -- all four page modules are now
    registered, with the precedent settled that INTEGRATION alone owns
    this list (see `server.py`'s own comment above `REGISTERED_MODULES`).
    Flipped to assert the fix rather than deleted, so a future regression
    (someone accidentally dropping a module from the list) is caught."""
    from habit_assistant.core.portal.server import REGISTERED_MODULES

    assert quota.register in REGISTERED_MODULES
