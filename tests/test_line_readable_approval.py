"""Readable-approval-flow feature (branch `line-version`, target
line/v1.1.0): the owner used to see only the opaque LINE userId
(`U` + 32 chars) in the approval flow. This file covers:

1. `LineChannel.get_profile`/`_display_name_for` (channels/line.py) --
   the LINE Get Profile fetch itself, fail-open, and the "at most once
   per user per process" cap.
2. The full wired pipeline: a brand-new LINE user's fetched name lands
   in their `users` row via the real `access.handle_gate` (zero core/
   changes for storage, per this feature's own design) and appears in
   the owner's pending-approval notification -- and a SECOND message
   from the same still-pending user never re-fetches.
3. `/users` showing the fetched name end-to-end.
4. A Telegram-mode regression guard: this feature touches only
   channels/line.py and core/access.py (channel-agnostic) --
   channels/telegram.py's own display-name flow is untouched.

`core/access.py`'s own name/id-prefix resolution for `/approve`/`/block`
(exact match, ambiguous match, unique prefix, short-prefix rejection,
the active-user-name safety asymmetry) is covered directly in
tests/test_access.py instead -- that module's own existing fixtures
(a plain on-disk `Database`, no channel/webhook needed) are the natural,
minimal-dependency place for it, and `core/access.py` is shared by both
channels anyway (not LINE-specific).

Reuses tests/test_line_integration.py's own real-webhook harness helpers
(same convention tests/test_line_release_gate.py already uses) rather
than duplicating the signature/queue/scheduler plumbing; only the
recorder gets a small local subclass here, since none of the existing
helpers can stub a `GET /v2/bot/profile/{userId}` response."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx

from conftest import FakeScheduler
from habit_assistant import main as main_module
from habit_assistant.channels.line import LineChannel
from habit_assistant.config import Config
from habit_assistant.storage.db import Database
from test_line_integration import (
    MEMBER,
    OWNER,
    _LineApiRecorder,
    _PoisonedHealthMonitor,
    _PoisonedOllamaClient,
    _PORTS,
    _line_channel_factory,
    _make_config,
    _post_events,
    _text_event,
    _wait_for_port,
    _wait_until,
)

# ===========================================================================
# 1. LineChannel.get_profile / _display_name_for -- unit level, a fully
#    controllable MockTransport, no webhook server needed.
# ===========================================================================


def _channel_with_transport(handler, tmp_path, db: Database) -> LineChannel:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = Config.model_validate({"line": {"media_dir": str(tmp_path / "media")}})
    return LineChannel("tok", "secret", OWNER, config, db, client=client)


async def test_get_profile_success_returns_display_name(tmp_path):
    async def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/bot/profile/Ualice0000000000000000000000000000"
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json={"displayName": "Alice", "userId": "Ualice..."})

    db = Database(tmp_path / "habits.db")
    channel = _channel_with_transport(handle, tmp_path, db)
    name = await channel.get_profile("Ualice0000000000000000000000000000")
    assert name == "Alice"
    await channel.aclose()
    db.close()


async def test_get_profile_non_2xx_fails_open_to_none(tmp_path):
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    db = Database(tmp_path / "habits.db")
    channel = _channel_with_transport(handle, tmp_path, db)
    name = await channel.get_profile("Umissing00000000000000000000000000")
    assert name is None
    await channel.aclose()
    db.close()


async def test_get_profile_network_error_fails_open_to_none_never_raises(tmp_path):
    async def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure", request=request)

    db = Database(tmp_path / "habits.db")
    channel = _channel_with_transport(handle, tmp_path, db)
    name = await channel.get_profile("Uunreachable0000000000000000000000")  # must not raise
    assert name is None
    await channel.aclose()
    db.close()


async def test_get_profile_blank_display_name_is_none_not_empty_string(tmp_path):
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"displayName": ""})

    db = Database(tmp_path / "habits.db")
    channel = _channel_with_transport(handle, tmp_path, db)
    name = await channel.get_profile("Ublank000000000000000000000000000")
    assert name is None
    await channel.aclose()
    db.close()


async def test_display_name_for_fetches_once_then_reuses_the_persisted_db_value(tmp_path):
    """Mirrors the REAL flow: message 1 (unknown) fetches; `access.
    handle_gate` (not this method) is what persists the fetched name to
    the `users` row; message 2 (now pending, name already stored) must
    read it back from the DB rather than re-fetching."""
    calls: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"displayName": "Alice"})

    db = Database(tmp_path / "habits.db")
    user_id = "Urepeat000000000000000000000000000"
    channel = _channel_with_transport(handle, tmp_path, db)

    first = await channel._display_name_for(user_id)
    assert first == "Alice"
    assert len(calls) == 1

    db.upsert_user(user_id, status="pending", display_name=first)  # what handle_gate does on the "unknown" branch

    second = await channel._display_name_for(user_id)
    assert second == "Alice"
    assert len(calls) == 1, "a name already stored in the users row must never trigger a second fetch"
    await channel.aclose()
    db.close()


async def test_display_name_for_does_not_retry_a_failed_fetch_per_message(tmp_path):
    """Even when the fetch fails (so nothing is ever persisted), a
    still-nameless user's every subsequent message within this same
    process must reuse the ONE failed outcome, not retry the network
    call each time."""
    calls: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(500, json={})

    db = Database(tmp_path / "habits.db")
    user_id = "Ufailsforever00000000000000000000"
    channel = _channel_with_transport(handle, tmp_path, db)

    for _ in range(3):
        name = await channel._display_name_for(user_id)
        assert name is None
    assert len(calls) == 1, f"expected exactly one fetch attempt across 3 messages, got {len(calls)}"
    await channel.aclose()
    db.close()


async def test_display_name_for_skips_fetch_entirely_when_already_known(tmp_path):
    """A user whose `users` row already carries a display_name (from a
    previous process run, or set some other way) is never fetched at
    all -- not even once."""
    calls: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"displayName": "Should Never Be Used"})

    db = Database(tmp_path / "habits.db")
    user_id = "Uknownalready0000000000000000000000"
    db.upsert_user(user_id, status="active", display_name="Already Known")
    channel = _channel_with_transport(handle, tmp_path, db)

    name = await channel._display_name_for(user_id)
    assert name == "Already Known"
    assert calls == []
    await channel.aclose()
    db.close()


# ===========================================================================
# 2/3. Full wired pipeline: fetched name -> DB row -> owner notification
#      -> /users listing, plus the fetch-once guarantee at the webhook
#      level (not just the unit level above).
# ===========================================================================


class _ProfileStubRecorder(_LineApiRecorder):
    """Extends the shared recorder (tests/test_line_integration.py) with
    a stubbed `GET /v2/bot/profile/{userId}` response, keyed by a
    caller-supplied `{user_id: displayName}` map -- the base recorder has
    no such route (falls through to its own generic `200 {}` default),
    fine for every test that doesn't care about the fetched name but
    useless for proving a REAL name flows all the way through the wired
    app. A `user_id` with no entry in the map responds 404 (simulates a
    lookup failure), matching `get_profile`'s own documented fail-open
    contract."""

    def __init__(self, profile_names: dict[str, str]) -> None:
        super().__init__()
        self._profile_names = profile_names

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/v2/bot/profile/"):
            self.calls.append((path, None))
            user_id = path.rsplit("/", 1)[-1]
            name = self._profile_names.get(user_id)
            if name is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json={"displayName": name})
        return super()._handle(request)


@asynccontextmanager
async def _running_line_app_with_profiles(monkeypatch, tmp_path, profile_names: dict[str, str]):
    """Mirrors test_line_integration.py's own `_running_line_app` exactly
    (real app, real webhook server, real port -- reuses that module's own
    `_PORTS` counter so ports never collide with its own tests), just
    swapping in `_ProfileStubRecorder` instead of the plain
    `_LineApiRecorder` so a profile fetch can return a real,
    test-controlled name."""
    port = next(_PORTS)
    db_path = tmp_path / "habits.db"
    media_dir = tmp_path / "media"
    config = _make_config(port=port, media_dir=media_dir, db_path=db_path)

    seed_db = Database(db_path)
    seed_db.upsert_user(OWNER, role="member", status="active")
    seed_db.upsert_user(MEMBER, role="member", status="active")
    seed_db.close()

    recorder = _ProfileStubRecorder(profile_names)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module,
        "load_secrets",
        lambda **kwargs: SimpleNamespace(
            telegram_bot_token=None,
            telegram_chat_id=None,
            line_channel_access_token="test-access-token",
            line_channel_secret="test-channel-secret",
            line_owner_user_id=OWNER,
        ),
    )
    monkeypatch.setattr(main_module, "LineChannel", _line_channel_factory(recorder))
    monkeypatch.setattr(main_module, "AsyncIOScheduler", FakeScheduler)
    monkeypatch.setattr(main_module, "OllamaClient", _PoisonedOllamaClient)
    monkeypatch.setattr(main_module, "HealthMonitor", _PoisonedHealthMonitor)
    FakeScheduler.last_instance = None

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None, migrate=False, backup=False, restore=None, yes=False)
    task = asyncio.create_task(main_module.async_main(args))
    try:
        await _wait_for_port(config.line.bind_host, config.line.bind_port)
        db = Database(db_path)
        try:
            yield SimpleNamespace(db=db, api=recorder, scheduler=FakeScheduler.last_instance, config=config, port=port)
        finally:
            db.close()
    finally:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


NEW_USER = "Ubrandnew0000000000000000000000000"


async def test_new_user_name_fetched_lands_in_db_and_owner_notification_fetch_once(monkeypatch, tmp_path):
    async with _running_line_app_with_profiles(monkeypatch, tmp_path, {NEW_USER: "Alice"}) as app:
        await _post_events(app.port, [_text_event(NEW_USER, "/start", reply_token="rt-newuser")])
        await _wait_until(lambda: app.api.calls_matching("/message/reply") or None)

        profile_paths = [p for p, _ in app.api.calls if p.startswith("/v2/bot/profile/")]
        assert profile_paths == [f"/v2/bot/profile/{NEW_USER}"], "exactly one profile fetch for the new user"

        # The name lands in the `users` row via the REAL gate -- zero
        # core/ changes for storage, this feature's own design (the gate
        # stores display_name exactly as Telegram's own flow does).
        assert app.db.get_user(NEW_USER)["display_name"] == "Alice"

        # The owner's pending-approval notification names the user, not
        # just the opaque id -- and still carries the full copy-paste
        # /approve command.
        owner_pushes = [b for b in app.api.calls_matching("/message/push") if b["to"] == OWNER]
        assert owner_pushes, "the owner must be notified of the new pending request"
        owner_text = owner_pushes[0]["messages"][0]["text"]
        assert "Alice" in owner_text
        assert f"/approve {NEW_USER}" in owner_text

        # A SECOND message from the same still-pending user must never
        # re-fetch the profile (fetch-once-per-unknown-user, not per
        # message).
        await _post_events(app.port, [_text_event(NEW_USER, "are you there?", reply_token="rt-again")])
        await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-again"] or None
        )
        profile_paths_after = [p for p, _ in app.api.calls if p.startswith("/v2/bot/profile/")]
        assert profile_paths_after == [f"/v2/bot/profile/{NEW_USER}"], (
            "a repeat message from the same still-pending user must not trigger a second profile fetch"
        )


async def test_new_user_profile_fetch_failure_falls_back_to_chat_id_never_crashes(monkeypatch, tmp_path):
    """Fail-open at the full-pipeline level: no stub entry for this user
    (the recorder answers 404) -- onboarding still completes, the DB row
    is created with no name, and the owner's notification degrades to
    the existing chat-id-only text instead of crashing or hanging."""
    async with _running_line_app_with_profiles(monkeypatch, tmp_path, {}) as app:
        resp = await _post_events(app.port, [_text_event(NEW_USER, "/start", reply_token="rt-newuser")])
        assert resp.status_code == 200
        await _wait_until(lambda: app.api.calls_matching("/message/reply") or None)

        assert app.db.get_user(NEW_USER) is not None
        assert app.db.get_user(NEW_USER)["display_name"] is None

        owner_pushes = [b for b in app.api.calls_matching("/message/push") if b["to"] == OWNER]
        owner_text = owner_pushes[0]["messages"][0]["text"]
        assert NEW_USER in owner_text  # falls back to the chat id


async def test_users_command_shows_fetched_display_name_end_to_end(monkeypatch, tmp_path):
    async with _running_line_app_with_profiles(monkeypatch, tmp_path, {NEW_USER: "Alice"}) as app:
        await _post_events(app.port, [_text_event(NEW_USER, "/start", reply_token="rt-newuser")])
        await _wait_until(lambda: app.api.calls_matching("/message/reply") or None)

        await _post_events(app.port, [_text_event(OWNER, "/users", reply_token="rt-users")])
        users_bodies = await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-users"] or None
        )
        text = users_bodies[0]["messages"][0]["text"]
        assert f"{NEW_USER} (Alice)" in text


async def test_approve_by_display_name_end_to_end_through_wired_app(monkeypatch, tmp_path):
    async with _running_line_app_with_profiles(monkeypatch, tmp_path, {NEW_USER: "Alice"}) as app:
        await _post_events(app.port, [_text_event(NEW_USER, "/start", reply_token="rt-newuser")])
        await _wait_until(lambda: app.api.calls_matching("/message/reply") or None)

        await _post_events(app.port, [_text_event(OWNER, "/approve alice", reply_token="rt-approve")])
        await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-approve"] or None
        )
        assert app.db.get_user(NEW_USER)["status"] == "active", (
            "/approve <display name> (case-insensitive) must resolve the pending user by name"
        )


# ===========================================================================
# 4. Telegram-mode regression guard: this feature touches only
#    channels/line.py and core/access.py (channel-agnostic) --
#    channels/telegram.py's own display-name flow must stay untouched.
# ===========================================================================


def test_telegram_display_name_of_unchanged_and_has_no_profile_lookup():
    from habit_assistant.channels.telegram import TelegramChannel

    assert TelegramChannel._display_name_of({"from": {"first_name": "Somchai"}}) == "Somchai"
    assert TelegramChannel._display_name_of({"from": {"first_name": ""}}) is None
    assert TelegramChannel._display_name_of({"from": {}}) is None
    assert TelegramChannel._display_name_of({}) is None
    assert not hasattr(TelegramChannel, "get_profile"), (
        "the readable-approval feature's profile lookup is LINE-only -- TelegramChannel must never grow an "
        "equivalent (Telegram already provides message.from.first_name inline, no fetch needed)"
    )
