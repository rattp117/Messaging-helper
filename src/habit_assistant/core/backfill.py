"""Retroactive logging (SPEC-v1.8.md §4 "Feature -- backfill / retroactive
logging (module `backfill`)", R-B1-R-B6, module `backfill`): a deterministic,
zero-LLM EN+TH date-phrase extractor for a message like "500ml yesterday" or
"เมื่อวาน ดื่มน้ำ 500", plus the small pure helpers `main.py`'s integration
step needs to build the backdated `ts` and the bilingual confirmation prefix
(§3.4) once it has a resolved date.

No channel/DB/LLM import here (mirrors `core/preparse.py`/`core/units.py`'s
own "pure logic in, pure logic out" convention for every zero-LLM module in
this codebase) -- `extract_date` never sees a `HabitRegistry` either: date
extraction is completely independent of which habits exist, so it composes
with the NORMAL extraction path (`preparse.deterministic_parse` then the LLM,
R-B2) on whatever residual text is left over, rather than duplicating any of
that logic here.

**Anchoring rule (R-B1/AC-C5, the load-bearing zero-false-positive
guarantee):** a recognized date phrase must be either the WHOLE LEADING
clause of the (stripped) message -- the phrase starts the message and is
immediately followed by end-of-string or whitespace -- or the WHOLE TRAILING
clause -- the phrase ends the message and is immediately preceded by
whitespace and at least one more character. This is a stricter reading than
"the phrase appears somewhere with word boundaries": a date phrase buried
mid-sentence, or one that is itself trailing but followed by MORE content
past it (e.g. a colon-introduced continuation), never matches -- "diary 2
days ago" (trailing, nothing after it) matches; "diary: yesterday was hard"
and "diary 2 days ago: had a rough day" do not (the date words are not the
message's own leading/trailing edge). This mirrors every other Thai-alias
command matcher already hardened against ordinary-prose false positives in
this codebase (`core/commands.py`'s `_build_target_th_set_pattern`/
`_build_remind_th_pattern`/`_build_history_th_pattern`/`_build_heatmap_th_
pattern`, each anchored to the whole stripped message and gated on a fixed
word-list, never a substring match) -- and matches this module's own stated
bias (SPEC-v1.8.md's dispatch note): "a missed backfill is recoverable; a
misfiled log is not", so an ambiguous placement is deliberately left
unmatched rather than guessed at.

Realistic Thai prose carries no spaces between words at all, so a date
phrase truly buried inside a Thai sentence (no whitespace on either side)
can never satisfy the leading/trailing whitespace-boundary requirement
either -- e.g. "ไดอารี่เมื่อวานเหนื่อยมาก" (all glued) fails to match on
both ends, exactly like the spaced diary-prose case above. This also mirrors
the existing Thai command matchers, which all require an explicit
whitespace run between their trigger word and the resolved token."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, Literal

from habit_assistant.core import i18n

# ---------------------------------------------------------------------------
# The "future vs too old" bounds sentinel (R-B5/AC-C4): `extract_date` and
# `resolve_days_back` (the LLM `date_offset` counterpart) both return this
# instead of `None` when a resolved date falls outside
# `[today - max_days_back, today)`, so the integration caller can pick the
# right bilingual error (`bounds_error_text` below) without re-deriving
# which bound was crossed.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutOfRange:
    reason: Literal["future", "too_old"]


# ---------------------------------------------------------------------------
# Thai/full-width numeral normalization (SPEC-v1.8.md's own dispatch note:
# "Thai numerals and full-width digits in 'N วันที่แล้ว' should work ... mirror
# it" -- no such normalizer exists elsewhere in this codebase yet (checked:
# `core/units.py:VALUE_RE` matches ASCII `\d` only), so this module defines
# its own small, self-contained one rather than inventing a shared-file
# dependency. Scoped to ONLY the digit run captured inside a date phrase --
# it never touches the residual text, so it can't change how any other
# module (preparse/LLM) interprets the rest of the message.
# ---------------------------------------------------------------------------

_DIGIT_TRANSLATION = str.maketrans(
    "๐๑๒๓๔๕๖๗๘๙" "０１２３４５６７８９",
    "01234567890123456789",
)


def _to_int(digits: str) -> int:
    return int(digits.translate(_DIGIT_TRANSLATION))


# ---------------------------------------------------------------------------
# Recognized phrase bodies (§2.4): EN "yesterday" / "N days ago" /
# "(on|last) <weekday>"; TH "เมื่อวาน[นี้]" / "N วันที่แล้ว|N วันก่อน" /
# "วัน<จันทร์..อาทิตย์>". Every alternation lists a longer prefix before a
# shorter one that could otherwise shadow it (e.g. "พฤหัสบดี" before
# "พฤหัส") so the longer form is never truncated by the regex engine's
# left-to-right alternation match.
# ---------------------------------------------------------------------------

_EN_WEEKDAYS: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_TH_WEEKDAYS: dict[str, int] = {
    "พฤหัสบดี": 3,  # must precede its own shorter prefix "พฤหัส" below
    "จันทร์": 0,
    "อังคาร": 1,
    "พุธ": 2,
    "พฤหัส": 3,
    "ศุกร์": 4,
    "เสาร์": 5,
    "อาทิตย์": 6,
}

_EN_YESTERDAY_BODY = r"yesterday"
_EN_DAYS_AGO_BODY = r"(?P<en_days>\d+)\s+days?\s+ago"
_EN_WEEKDAY_BODY = rf"(?:on|last)\s+(?P<en_weekday>{'|'.join(_EN_WEEKDAYS)})"

_TH_YESTERDAY_BODY = r"เมื่อวาน(?:นี้)?"
_TH_DAYS_AGO_BODY = r"(?P<th_days>[0-9๐-๙０-９]+)\s*วัน(?:ที่แล้ว|ก่อน)"
_TH_WEEKDAY_BODY = rf"วัน(?P<th_weekday>{'|'.join(_TH_WEEKDAYS)})"


def _resolve_yesterday(match: re.Match[str], today: date) -> date:
    return today - timedelta(days=1)


def _resolve_en_days_ago(match: re.Match[str], today: date) -> date:
    return today - timedelta(days=int(match.group("en_days")))


def _resolve_th_days_ago(match: re.Match[str], today: date) -> date:
    return today - timedelta(days=_to_int(match.group("th_days")))


def _resolve_past_weekday(today: date, target_weekday: int) -> date:
    """R-B1/SPEC-v1.8.md §9 decision: "weekday names resolve to the most
    recent PAST occurrence" -- today itself is never returned even when
    today IS that weekday (delta 0 -> 7), matching R-B5's separate "==
    today falls through to the normal path" contract, which only applies
    to the literal today's-date case, not "the weekday that happens to be
    today"."""
    delta = (today.weekday() - target_weekday) % 7
    if delta == 0:
        delta = 7
    return today - timedelta(days=delta)


def _resolve_en_weekday(match: re.Match[str], today: date) -> date:
    return _resolve_past_weekday(today, _EN_WEEKDAYS[match.group("en_weekday").lower()])


def _resolve_th_weekday(match: re.Match[str], today: date) -> date:
    return _resolve_past_weekday(today, _TH_WEEKDAYS[match.group("th_weekday")])


@dataclass(frozen=True, slots=True)
class _Candidate:
    leading_re: re.Pattern[str]
    trailing_re: re.Pattern[str]
    resolve: Callable[[re.Match[str], date], date]


def _build_candidate(body: str, resolve: Callable[[re.Match[str], date], date], *, ignore_case: bool) -> _Candidate:
    flags = re.IGNORECASE | re.DOTALL if ignore_case else re.DOTALL
    leading_re = re.compile(rf"^{body}(?:\s+(?P<rest>.+))?$", flags)
    trailing_re = re.compile(rf"^(?P<rest>.+)\s+{body}$", flags)
    return _Candidate(leading_re, trailing_re, resolve)


# Order doesn't affect correctness (every body is gated on disjoint literal
# cue text -- no two candidates can both match the same phrase), only which
# one wins on a contrived double-phrase message, an edge case this module
# makes no promise about.
_CANDIDATES: tuple[_Candidate, ...] = (
    _build_candidate(_EN_YESTERDAY_BODY, _resolve_yesterday, ignore_case=True),
    _build_candidate(_EN_DAYS_AGO_BODY, _resolve_en_days_ago, ignore_case=True),
    _build_candidate(_EN_WEEKDAY_BODY, _resolve_en_weekday, ignore_case=True),
    _build_candidate(_TH_YESTERDAY_BODY, _resolve_yesterday, ignore_case=False),
    _build_candidate(_TH_DAYS_AGO_BODY, _resolve_th_days_ago, ignore_case=False),
    _build_candidate(_TH_WEEKDAY_BODY, _resolve_th_weekday, ignore_case=False),
)


def _bound(today: date, target: date, max_days_back: int) -> date | None | OutOfRange:
    """R-B5/AC-C4: future -> `OutOfRange("future")`; older than
    `max_days_back` -> `OutOfRange("too_old")`; exactly today -> `None`
    (falls through to the normal, non-backfill path unchanged); otherwise
    the resolved date itself."""
    if target > today:
        return OutOfRange("future")
    if (today - target).days > max_days_back:
        return OutOfRange("too_old")
    if target == today:
        return None
    return target


def extract_date(
    text: str, clock: Callable[[], datetime], *, max_days_back: int
) -> tuple[str, date] | None | OutOfRange:
    """R-B1: find a leading- or trailing-anchored recognized date phrase
    (§2.4) in `text`. Returns `(residual_text, target_date)` when a phrase
    was found and its resolved date is within
    `[today - max_days_back, today)`; `OutOfRange(...)` when a phrase was
    found but its resolved date is out of bounds (R-B5); `None` when no
    phrase was found AT ALL, or the phrase's resolved date is exactly
    today (R-B5's own "falls through unchanged" case) -- the caller cannot
    (and per R-B5 need not) tell these two `None` causes apart, since both
    mean "proceed with the normal, non-backfill log path"."""
    stripped = text.strip()
    if not stripped:
        return None

    today = clock().date()

    for candidate in _CANDIDATES:
        match = candidate.leading_re.match(stripped)
        if match is not None:
            residual = (match.group("rest") or "").strip()
            return _finish(candidate, match, today, max_days_back, residual)

    for candidate in _CANDIDATES:
        match = candidate.trailing_re.match(stripped)
        if match is not None:
            residual = match.group("rest").strip()
            return _finish(candidate, match, today, max_days_back, residual)

    return None


def _finish(
    candidate: _Candidate, match: re.Match[str], today: date, max_days_back: int, residual: str
) -> tuple[str, date] | None | OutOfRange:
    target = candidate.resolve(match, today)
    bounded = _bound(today, target, max_days_back)
    if bounded is None or isinstance(bounded, OutOfRange):
        return bounded
    return residual, bounded


def resolve_days_back(
    clock: Callable[[], datetime], days_back: int, *, max_days_back: int
) -> date | None | OutOfRange:
    """R-B5: "The optional LLM `date_offset` is subject to the same
    bounds" -- integration's own helper for that field (out of this
    module's scope beyond providing the shared bounds check).
    `days_back` is the LLM's raw offset: negative (a future date) ->
    `OutOfRange("future")`; 0 -> `None` (today, normal path); a value
    beyond `max_days_back` -> `OutOfRange("too_old")`; otherwise the
    resolved date -- exactly `extract_date`'s own bounds semantics,
    applied directly to an already-known day count instead of a parsed
    phrase."""
    today = clock().date()
    if days_back < 0:
        return OutOfRange("future")
    return _bound(today, today - timedelta(days=days_back), max_days_back)


# ---------------------------------------------------------------------------
# Small pure helpers for integration (§3.4): the backdated `ts` a backfilled
# `LogEntry` is inserted with (R-B2, local noon so it unambiguously
# attributes to the resolved day regardless of timezone-adjacent rounding),
# and the bilingual confirmation-prefix formatter integration prepends to
# the normal per-habit confirmation text (R-B4's "no retro-celebration"
# line is integration's own concern -- this only builds the date prefix).
# ---------------------------------------------------------------------------


def backdated_ts(target_date: date) -> str:
    """R-B2: `ts` for a backfilled row -- `target_date` at local noon,
    formatted exactly like `main.py`'s own live-log `ts`
    (`now.isoformat(timespec="seconds")`, e.g. "2026-08-18T12:00:00") so
    every existing `ts`-prefix/`ts BETWEEN` aggregation (R-B3) treats it
    identically to a same-shaped live row."""
    return datetime.combine(target_date, time(12, 0, 0)).isoformat(timespec="seconds")


_EN_WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_EN_MONTH_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
_TH_WEEKDAY_ABBR = ("จ.", "อ.", "พ.", "พฤ.", "ศ.", "ส.", "อา.")
_TH_MONTH_ABBR = (
    "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
    "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.",
)


def _format_day(target_date: date, lang: i18n.Language) -> str:
    """"Mon 18 Aug" (EN) / "จ. 18 ส.ค." (TH) -- no existing weekday/month-
    name localization helper exists elsewhere in this codebase to reuse
    (`core/heatmap.py`'s own chart deliberately stays English-only/numeric
    for this exact reason, per its module docstring); this module needs
    the bilingual form for real (§3.4), so it defines its own small
    abbreviation tables rather than reaching for `date.strftime`, whose
    locale-dependent output this codebase never relies on (see
    `core/heatmap.py`'s "default 'C' locale" note)."""
    weekday = target_date.weekday()
    if lang == "th":
        return f"{_TH_WEEKDAY_ABBR[weekday]} {target_date.day} {_TH_MONTH_ABBR[target_date.month - 1]}"
    return f"{_EN_WEEKDAY_ABBR[weekday]} {target_date.day} {_EN_MONTH_ABBR[target_date.month - 1]}"


def confirmation_prefix(target_date: date, lang: i18n.Language) -> str:
    """§3.4: "📅 Logged for Mon 18 Aug — " -- integration prepends this
    directly to whatever the normal per-habit confirmation text is
    (`prefix + normal_confirmation`), reusing the existing confirmation
    formatter verbatim rather than this module inventing a second one
    (R-B2's own "no second confirmation formatter" discipline, mirrored
    from R-Q2's identical note for quick-log)."""
    return i18n.t("backfill_confirmation_prefix", lang, day=_format_day(target_date, lang))


def bounds_error_text(out_of_range: OutOfRange, lang: i18n.Language, max_days_back: int) -> str:
    """R-B5/AC-C4: the friendly bilingual error for a future or too-old
    resolved date -- `out_of_range.reason` picks which of the two
    `backfill_error_*` catalog entries integration sends, no write either
    way."""
    msg_id = "backfill_error_future" if out_of_range.reason == "future" else "backfill_error_too_old"
    return i18n.t(msg_id, lang, max_days=max_days_back)
