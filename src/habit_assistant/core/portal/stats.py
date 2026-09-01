"""SPEC-LINE-PORTAL.md §4 R-STATS-1/R-STATS-2 (shared surface, admin web
portal, branch `line-version`): the two process-lifetime, in-memory-only
holders every portal page reads through `PortalDeps`.

`RuntimeStats` backs the Status page's uptime + "last webhook event"
tiles (AC9/AC10). `RingBufferHandler` backs its "recent errors" panel
(AC14). Neither is ever persisted -- both reset on restart, which is
DELIBERATE (UX.md Screen 1: "This list clears on every restart" is not
decoration, it's the difference between a dashboard the owner can trust
and one that quietly lies once a crash-loop restart has happened).

`journalctl` was considered and rejected for "recent errors" (R-STATS-2):
it needs the `habitbot` service user to hold journal-read privilege plus
a subprocess call, both fragile under the systemd unit's own
`NoNewPrivileges=true`/`ProtectSystem=strict` hardening (see
`deploy/habit-assistant-line.service`). A `collections.deque` needs
neither.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RuntimeStats:
    """R-STATS-1: `started_at` is set once, at construction (process
    start -- `core/app.py` constructs exactly one of these, right where
    it builds the portal's other dependencies). `last_event_at` is
    updated by `core/app.py`'s `_on_message`/`_on_callback` wrappers via
    `mark_event()` on every inbound LINE event. Never persisted -- the
    Status page's localized "no events since restart" state (AC10) is
    exactly what an unset `last_event_at` means."""

    started_at: datetime = field(default_factory=datetime.now)
    last_event_at: datetime | None = None

    def mark_event(self) -> None:
        self.last_event_at = datetime.now()


class RingBufferHandler(logging.Handler):
    """R-STATS-2: keeps the last `capacity` `WARNING`+ log records in a
    `collections.deque(maxlen=capacity)` -- installed on the
    `habit_assistant` logger at startup (`core/app.py`), ONLY when the
    portal is enabled. Records propagate normally to the root logger's
    own handler (installing this adds a handler, it doesn't replace or
    intercept anything) -- existing log output is unaffected."""

    def __init__(self, capacity: int) -> None:
        super().__init__(level=logging.WARNING)
        self._buffer: deque[logging.LogRecord] = deque(maxlen=max(1, capacity))
        self._capacity = max(1, capacity)

    def emit(self, record: logging.LogRecord) -> None:
        self._buffer.append(record)

    def records(self) -> list[logging.LogRecord]:
        """Newest-first snapshot (R-STATUS-5's own display order)."""
        return list(reversed(self._buffer))

    @property
    def capacity(self) -> int:
        return self._capacity

    def at_capacity(self) -> bool:
        """UX.md Screen 1's "at capacity" panel state (AC14): whether
        older records have already been dropped -- the Status page shows
        an extra "Showing the latest N; older records have been dropped"
        note only in this state, per the empty/populated/at-capacity
        three-way split."""
        return len(self._buffer) >= self._capacity

    def __len__(self) -> int:
        return len(self._buffer)
