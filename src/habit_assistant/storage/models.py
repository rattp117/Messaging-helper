"""Dataclasses for a habit-log entry (see SPEC.md §5 for the SQLite schema)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class LogEntry:
    id: int | None
    user_id: str  # SPEC-v1.2.md R-D1: the owning chat id (a string, never None for a new write)
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
    # SPEC-v1.10.md §5 R-SS4 (shared surface, migration 013): the unparsed-
    # state machine's own lifecycle marker -- only meaningful for
    # `category='unparsed'` rows. `None` (the default, written as SQL NULL)
    # covers two cases that are deliberately indistinguishable at the model
    # level: a genuinely non-unparsed row (this field is simply unused), and
    # a fresh/legacy deferral row that `db.pending_unparsed()`/the CAS
    # methods (R-SS2/R-SS3) treat as `'awaiting_llm'`. Every existing
    # caller/construction site is byte-identical (still writes NULL, since
    # this is a trailing, defaulted field) -- the deferral insert in
    # particular is UNCHANGED (SPEC-v1.10.md's own "NULL = awaiting_llm,
    # no data-migration UPDATE" design, R-SS1).
    unparsed_state: str | None = None


@dataclass(slots=True)
class AuditEntry:
    """SPEC-v1.3.md "Audit log" (§5): one who/when/what/how row for the
    `audit_log` table. `core/audit.py:record` is the ONLY place that
    constructs one -- capture sites (undo_ui, targets_command, schedules,
    preferences, access, main.py) call `record(...)` with plain
    keyword arguments and never build this dataclass themselves."""

    id: int | None
    ts: str  # ISO8601 local, when the action happened (matches LogEntry.ts's own convention)
    user_id: str  # the ACTOR (who performed the action) -- see target_user_id for who it was done TO
    action: str  # one of core/audit.py:ACTIONS
    entity: str | None  # a habit id for habit-scoped actions; None otherwise
    old_value: str | None  # previous value, already stringified by record(); None when N/A
    new_value: str | None  # new value, already stringified by record(); None when N/A
    source: str  # one of core/audit.py:SOURCES ('command' | 'nl' | 'button' | 'admin' | 'system')
    target_user_id: str | None = None  # admin actions on ANOTHER chat; None otherwise
