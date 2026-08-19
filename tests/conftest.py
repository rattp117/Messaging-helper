"""Shared pytest fixtures.

Some tests exercise `main.async_main`, which calls `setup_logging()` ->
`logging.basicConfig(..., stream=sys.stdout, force=True)`. That permanently
rebinds the root logger's handler to that test's captured stdout object.
Once pytest closes/replaces the capture stream for the next test, any log
call from unrelated code (e.g. APScheduler's own logger) raises "I/O
operation on closed file" -- noise, not a real failure, but it pollutes
every later test's output. Restore the root logger's handlers/level after
each test so this doesn't leak across the suite.
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _restore_root_logging_state():
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    yield
    root.handlers[:] = original_handlers
    root.level = original_level
