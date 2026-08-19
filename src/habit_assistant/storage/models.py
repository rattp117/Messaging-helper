"""Dataclasses for a habit-log entry (see SPEC.md §5 for the SQLite schema)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class LogEntry:
    id: int | None
    ts: str  # ISO8601 local time of the event
    category: str  # a configured habit id (e.g. 'water' | 'stretch' | 'diary'), or 'unparsed'
    value_num: float | None  # numeric/duration value, or boolean as 1.0/0.0; else None
    value_text: str | None  # text value; else None
    raw_message: str  # exactly what was sent
    source: str = "reply"  # 'reply' | 'unprompted'
    created_at: str | None = None
    # ROADMAP.md v0.7.0 migration 004: the matched habit's `type` at log
    # time ('numeric'|'duration'|'text'|'boolean'), so a row stays
    # self-describing even if the habit is later removed from config.
    # NULL for rows whose category has no known type (e.g. 'unparsed').
    habit_type: str | None = None
