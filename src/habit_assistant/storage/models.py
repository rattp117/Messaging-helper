"""Dataclasses for a habit-log entry (see SPEC.md §5 for the SQLite schema)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class LogEntry:
    id: int | None
    ts: str  # ISO8601 local time of the event
    category: str  # 'water' | 'stretch' | 'diary'
    value_num: float | None  # water: ml; stretch: minutes; diary: None
    value_text: str | None  # diary text; else None
    raw_message: str  # exactly what was sent
    source: str = "reply"  # 'reply' | 'unprompted'
    created_at: str | None = None
