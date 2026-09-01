"""Shared pytest fixtures.

Some tests exercise `main.async_main`, which calls `setup_logging()` ->
`logging.basicConfig(..., stream=sys.stdout, force=True)`. That permanently
rebinds the root logger's handler to that test's captured stdout object.
Once pytest closes/replaces the capture stream for the next test, any log
call from unrelated code (e.g. APScheduler's own logger) raises "I/O
operation on closed file" -- noise, not a real failure, but it pollutes
every later test's output. Restore the root logger's handlers/level after
each test so this doesn't leak across the suite.

SPEC-REFACTOR.md Stage 4 rule 12 (MEDIUM cluster) / AC12: the audit counted
82 hand-rolled channel fakes / 35 LLM fakes / 29 scheduler+db doubles across
test files with no shared conftest. `RecordingChannel`/`FakeOllamaClient`/
`FakeScheduler` below are the shared trio -- each is byte-identical in
observable behavior to the vanilla per-file copies it consolidates (see the
migrated files' own diffs for proof). Per SPEC-REFACTOR.md §10 ("Out of
scope"), the exotic scripted/raising variants (`_ScriptedChannel`,
`RaisingForChannel`, `_CountingOllamaClient`, etc.) are deliberately NOT
consolidated here and stay per-file -- only the plain recording/queueing
shape these three replace. A test file imports what it needs with
`from conftest import RecordingChannel, FakeOllamaClient, FakeScheduler`
(pytest puts `tests/` on `sys.path` for every module under it, the same
mechanism that makes this `conftest.py` itself discoverable with no
`tests/__init__.py` in the tree)."""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.core import digest
from habit_assistant.storage.db import Database


@pytest.fixture(autouse=True)
def _restore_root_logging_state():
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    yield
    root.handlers[:] = original_handlers
    root.level = original_level


class RecordingChannel(Channel):
    """Mirrors the `FakeChannel` used by (pre-migration) tests/test_checkins.py,
    tests/test_nudge.py, tests/test_reminders.py, and several other files:
    records every `send()` call as a `(chat_id, text)` pair. `run()` is
    never exercised by these tests (no fake here ever drives the inbound
    loop), so it raises `NotImplementedError` if ever called -- matching
    every one of those files' own copy exactly.

    SPEC-v1.10.md §4 R-SS5/R-SS6 (shared surface, "never lose a log"
    reply-to-reminder attribution): `send()` now returns a synthetic,
    per-instance INCREMENTING string id ("1", "2", ...) -- mirroring
    `TelegramChannel.send`'s own real `str | None` contract closely enough
    that a reply-attribution test can map a recorded send back to a habit
    (`core/reminders.py:ReminderState.remember_reminder`) the same way a
    real Telegram send id would be used. This is additive/observable-only:
    every existing test that ignores the return value (the overwhelming
    majority) is unaffected -- only `.sent`'s own shape/append behavior
    matters to them, unchanged."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self._next_message_id = 1

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> str | None:
        self.sent.append((chat_id, text))
        message_id = str(self._next_message_id)
        self._next_message_id += 1
        return message_id

    async def run(
        self,
        on_message: Callable[[str, str], Awaitable[None]],
        on_callback=None,
    ) -> None:
        raise NotImplementedError

    def sent_to(self, chat_id: str) -> list[str]:
        return [text for cid, text in self.sent if cid == chat_id]


class FakeOllamaClient:
    """Mirrors the `_FakeOllamaClient` used by the `test_vNN_integration.py`
    family: serves a class-level QUEUE of canned `chat_json` responses,
    consumed in call order (an empty queue falls back to `unknown`, never
    crashes -- matches `parse_message`'s own fail-closed contract). Set
    `FakeOllamaClient.responses = [...]` before use, same convention as
    every per-file copy it replaces; `_reset_shared_doubles` below clears
    it again after every test so no leftover queue can leak across tests
    or files that both import this same class object."""

    responses: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def chat_text(self, system_prompt, user_prompt) -> str:
        return "noted"

    async def chat_json(self, system_prompt, user_prompt, json_schema, valid_categories) -> str:
        if FakeOllamaClient.responses:
            return FakeOllamaClient.responses.pop(0)
        return json.dumps({"category": "unknown", "value": None, "confidence": 0.1})

    async def probe_schema_support(self, *args, **kwargs) -> dict:
        return {}

    async def aclose(self) -> None:
        pass


class FakeScheduler:
    """Mirrors the `_FakeScheduler` used by the `test_vNN_integration.py`
    family: records every `add_job` call so a test can later invoke
    `job.func(*job.args, **job.kwargs)` directly (via `get_job`/`.jobs`).
    Superset of every per-file copy's own `add_job` signature -- some
    callers only ever pass `args`, some also pass `kwargs` (SPEC-REFACTOR.md
    v1.9's minutely-tick jobs); this stores both unconditionally, which is
    additive and doesn't change any existing caller's own read of
    `job.func`/`job.trigger`/`job.args`/`job.id`."""

    last_instance: "FakeScheduler | None" = None

    def __init__(self, *args, **kwargs) -> None:
        self.jobs: dict[str, object] = {}
        FakeScheduler.last_instance = self

    def add_job(self, func, trigger=None, args=None, kwargs=None, id=None, replace_existing=True, **extra):
        self.jobs[id] = SimpleNamespace(
            func=func, trigger=trigger, args=list(args or []), kwargs=dict(kwargs or {}), id=id
        )

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def start(self) -> None:
        pass

    def shutdown(self, wait: bool = False) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_shared_doubles():
    yield
    FakeOllamaClient.responses = []
    FakeScheduler.last_instance = None


@pytest.fixture(autouse=True)
def _reset_daily_digest_claim():
    """Integration item 5 (TEST-PORTAL-quota.md Finding F3): `core/
    digest.py:_DAILY_RUN_CLAIMED` is process-lifetime, module-level state
    consulted by BOTH `core/app.py`'s real scheduled `daily_digest` job
    AND `core/portal/quota.py`'s manual trigger (`run_daily_digest_
    guarded`) -- any test that exercises either real call site (not just
    the portal-quota test files) can leave today's date claimed behind
    it, silently no-op-ing a LATER, unrelated test's own scheduled-job
    assertion in the same worker process. Global, not file-local (unlike
    `tests/test_portal_quota.py`'s own narrower reset), since production
    code -- not just this test suite -- shares this state across both
    call sites by design."""
    digest._DAILY_RUN_CLAIMED.clear()
    yield
    digest._DAILY_RUN_CLAIMED.clear()


class RecordingLineChannel(Channel):
    """SPEC-LINE.md §4 R-A4/R-A6/R-C6 (shared surface, branch
    `line-version`): a LINE-flavored double for modules OTHER than A
    (module C's `core/digest.py`, Integration's end-to-end tests) that
    need a `Channel` whose Reply-vs-Push distinction -- the one thing
    that makes LINE's free/uncounted-reply vs quota-counted-push economics
    observable -- is real enough to assert against, without pulling in
    the real aiohttp webhook/HTTP machinery module A owns.

    `reply_context(token)` mirrors `LineChannel`'s own per-event
    `contextvars.ContextVar` (R-A4): every `send()` while a context is
    active is BUFFERED into `.replies[token]` instead of sent immediately
    (a real reply call batches up to 5 objects into one free API call --
    this double doesn't cap at 5, since no test here asserts that limit;
    module A's own tests do). Outside any active context -- a
    scheduled/proactive send, e.g. the digest -- `send()` goes to
    `.pushes` AND calls `db.increment_push(chat_id, yyyymm)` for the
    current month (per `clock`) if a `db` was given (R-A6: "the channel's
    own responsibility on the push path ... so the count is authoritative
    regardless of caller", R-C6).

    `clock`, additive/keyword-only/defaulted `datetime.now` (line-clock
    fix, TEST-LEDGER-TRIAGE.md): mirrors the injectable-clock seam
    `channels/line.py:LineChannel` itself now carries
    (`LineChannel._clock`/`_now_yyyymm`), so a test can point this double
    at a fixed instant too. Deliberately does NOT reproduce
    `LineChannel._now_yyyymm`'s `ZoneInfo(config.app.timezone)`
    normalization -- this double takes no `config`, and every consumer of
    it (module C's digest tests, integration) only ever needs an
    injectable "what month is it", not tz-conversion behavior of its own;
    that behavior is covered directly against the real `LineChannel` in
    `tests/test_line_channel.py`. Defaults to the real wall clock, same as
    `LineChannel`, so this is NOT a "deliberately mirrors the real bug"
    double any more -- the bug it used to (accidentally) mirror is fixed
    in production; this double's un-injected default simply matches
    `LineChannel`'s own un-injected default (the real wall clock,
    `Asia/Bangkok`-normalized in production, un-normalized here)."""

    def __init__(self, db: Database | None = None, *, clock: Callable[[], datetime] = datetime.now) -> None:
        self.db = db
        self.replies: dict[str, list[tuple[str, str]]] = {}
        self.pushes: list[tuple[str, str]] = []
        self._active_token: str | None = None
        self._clock = clock

    @contextlib.contextmanager
    def reply_context(self, reply_token: str):
        previous, self._active_token = self._active_token, reply_token
        self.replies.setdefault(reply_token, [])
        try:
            yield
        finally:
            self._active_token = previous

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> str | None:
        # Integration item 4 (TEST-PORTAL-users.md Finding 1): this double
        # doesn't model the realtime quota gate at all (it always
        # succeeds), so it always returns a non-None confirmation sentinel
        # -- matching `LineChannel.send`'s own updated contract for its
        # "confirmed sent" case (see that method's docstring).
        if self._active_token is not None:
            self.replies[self._active_token].append((chat_id, text))
            return "buffered"
        self.pushes.append((chat_id, text))
        if self.db is not None:
            self.db.increment_push(chat_id, self._clock().strftime("%Y-%m"))
        return "pushed"

    async def run(
        self,
        on_message: Callable[[str, str], Awaitable[None]],
        on_callback=None,
    ) -> None:
        raise NotImplementedError

    def pushes_to(self, chat_id: str) -> list[str]:
        return [text for cid, text in self.pushes if cid == chat_id]

    def replies_for(self, reply_token: str) -> list[str]:
        return [text for _cid, text in self.replies.get(reply_token, [])]
