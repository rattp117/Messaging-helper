"""SPEC-LINE-PORTAL.md §4/§5 R-STATS-1/R-STATS-2 (shared surface, admin
web portal, branch `line-version`): `core/portal/stats.py`'s own unit
tests -- AC9/AC10 (`RuntimeStats`) and AC14 (`RingBufferHandler`).
"""

from __future__ import annotations

import logging
from datetime import datetime

from habit_assistant.core.portal.stats import RingBufferHandler, RuntimeStats


# ===========================================================================
# RuntimeStats
# ===========================================================================


def test_runtime_stats_started_at_defaults_to_construction_time():
    before = datetime.now()
    stats = RuntimeStats()
    after = datetime.now()
    assert before <= stats.started_at <= after


def test_runtime_stats_last_event_at_starts_unset():
    stats = RuntimeStats()
    assert stats.last_event_at is None


def test_runtime_stats_mark_event_sets_and_updates_last_event_at():
    stats = RuntimeStats()
    stats.mark_event()
    first = stats.last_event_at
    assert first is not None
    stats.mark_event()
    assert stats.last_event_at >= first


# ===========================================================================
# RingBufferHandler
# ===========================================================================


def _record(level: int, msg: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="habit_assistant.core.digest", level=level, pathname=__file__, lineno=1, msg=msg, args=(), exc_info=None
    )


def test_ring_buffer_starts_empty():
    handler = RingBufferHandler(5)
    assert handler.records() == []
    assert len(handler) == 0
    assert not handler.at_capacity()


def test_ring_buffer_only_captures_warning_and_above_via_its_own_level():
    handler = RingBufferHandler(10)
    logger = logging.getLogger("test_portal_stats_warning_gate")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        logger.info("info -- below the handler's own WARNING level")
        logger.warning("warning -- at the floor")
        logger.error("error -- above the floor")
    finally:
        logger.removeHandler(handler)
    messages = [r.getMessage() for r in handler.records()]
    assert "warning -- at the floor" in messages
    assert "error -- above the floor" in messages
    assert not any("info --" in m for m in messages)


def test_ring_buffer_newest_first_snapshot():
    handler = RingBufferHandler(5)
    handler.emit(_record(logging.WARNING, "first"))
    handler.emit(_record(logging.WARNING, "second"))
    handler.emit(_record(logging.WARNING, "third"))
    messages = [r.getMessage() for r in handler.records()]
    assert messages == ["third", "second", "first"]


def test_ring_buffer_drops_oldest_past_capacity():
    handler = RingBufferHandler(3)
    for i in range(5):
        handler.emit(_record(logging.WARNING, f"msg-{i}"))
    messages = [r.getMessage() for r in handler.records()]
    assert messages == ["msg-4", "msg-3", "msg-2"]
    assert len(handler) == 3
    assert handler.at_capacity()


def test_ring_buffer_capacity_property():
    handler = RingBufferHandler(200)
    assert handler.capacity == 200


def test_ring_buffer_capacity_floor_is_one():
    handler = RingBufferHandler(0)
    assert handler.capacity == 1
