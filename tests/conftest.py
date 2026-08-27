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

import json
import logging
from types import SimpleNamespace
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel


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
