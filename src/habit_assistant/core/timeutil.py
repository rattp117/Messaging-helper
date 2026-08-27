"""Shared time helpers (SPEC-REFACTOR.md Stage 3, rule 12(b)/(e)): the
"resolve today, or the current HH:MM, from an injectable clock + IANA
timezone name" shim, and the "7 ISO day strings ending at end_date" window
helper -- each previously independently duplicated across several call
sites (this codebase's established convention up to v1.9, e.g.
`core/user_prefs.py`'s own identical precedent for the `_user_language_pref`
lookup, and `core/records.py`'s docstring explicitly citing this exact
`_today`/`_now_hhmm` duplication class before this module existed).

Behavior is byte-identical to every original these functions replace: a
naive `clock()` result (this app's usual injectable-clock shape, e.g.
`datetime.now` or a test's fixed callable) is treated as already being in
`tz_name`; an aware one is converted to it."""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo


def today_in_timezone(clock, tz_name: str) -> date:
    """Today's calendar date in `tz_name`, per an injectable `clock`."""
    now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(tz_name))
    else:
        now = now.astimezone(ZoneInfo(tz_name))
    return now.date()


def now_hhmm(clock, tz_name: str) -> str:
    """The current wall-clock `HH:MM` in `tz_name`, per an injectable
    `clock` -- same clock-normalization as `today_in_timezone`."""
    now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(tz_name))
    else:
        now = now.astimezone(ZoneInfo(tz_name))
    return now.strftime("%H:%M")


def week_days(end_date: date) -> list[str]:
    """The 7 ISO day strings ending at (and including) `end_date`."""
    return [(end_date - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
