# Test Ledger Triage — 2026-08-31 → 2026-09-01 date rollover

Scope: `tests/test_digest.py::test_run_daily_digest_increments_push_ledger_exactly_once_per_user`,
`tests/test_line_c_gaps.py::test_push_ledger_increments_exactly_once_per_successful_push_not_per_composed_user`,
plus a sweep of `core/digest.py` + `channels/line.py` ledger accounting and the wider LINE-branch
test files for the same class of bug. Baseline: clean `line/v1.2.0` (`b9eec9c`).

## Verdict

**Both.** There is a real, latent **production bug** in `channels/line.py` (mixed clock
sources — reported below, **not fixed**, per instructions). Independently, the two named tests
were **also** broken on their own terms: they assumed the push-ledger write would honor
`run_daily_digest`'s injected `clock=`, which it never has — the tests just got lucky as long as
the real wall clock stayed in August. That assumption is now corrected in the two test files
(**fixed**, test-only, no production code touched).

## The critical question, answered

> Is the month-key (yyyymm) for `push_ledger` derived from the REAL clock somewhere while the
> send/compose path uses an injected clock?

**Yes — in production itself, not just in tests.**

### The two clock paths, byte-precise

**Path 1 — composition (`core/digest.py`), properly clock-injected:**
- `run_daily_digest(..., clock=datetime.now)` (`core/digest.py:468-474`, default arg) resolves
  `now = _local_now(config, clock)` (`:518`).
- `_local_now` (`:110-117`) calls the injected `clock()` and normalizes it into
  `config.app.timezone` via `ZoneInfo` — tz-aware, testable, injectable.
- `compose_digest(..., now=now)` (`:257-345`) derives `yyyymm = today.strftime("%Y-%m")` from
  that same resolved `now` (`:293-294`), and uses it for the owner quota-warning read,
  `db.monthly_push_total(yyyymm)` (`:313-316`).
- Every test in `test_digest.py`/`test_line_c_gaps.py` passes a fixed `clock=` lambda and gets
  fully deterministic composition. This half of the system is correct and well-tested.

**Path 2 — the actual send + ledger write (`channels/line.py`), NOT clock-injected:**
- `LineChannel._send_push` (`:262-281`) — the **sole place `push_ledger` is incremented**
  (its own docstring: "the ONLY place `push_ledger` is incremented -- authoritative regardless
  of caller") — computes its own month key independently:
  ```python
  yyyymm = datetime.now().strftime("%Y-%m")   # line.py:280
  self.db.increment_push(chat_id, yyyymm)
  ```
  Bare `datetime.now()`. No `clock` parameter exists on `LineChannel` at all. No
  `ZoneInfo(config.app.timezone)` normalization either — unlike `_local_now`, this is the
  **naive system-local** clock, whatever timezone the host OS happens to be in.
- `LineChannel._push` (`:351-391`), the realtime quota gate (R-Q2/R-Q3) that decides whether a
  non-owner push is even allowed, does the **same** thing independently:
  ```python
  yyyymm = datetime.now().strftime("%Y-%m")   # line.py:367
  ...
  total = self._monthly_push_total_fail_closed(yyyymm)
  ```
  This is a **second, separate** call to `datetime.now()` — not threaded from `_send_push`, not
  threaded from `run_daily_digest`'s resolved `now`.
- `db.increment_push(user_id, yyyymm)` / `db.monthly_push_total(yyyymm)` (`storage/db.py:1253ff`)
  are themselves clock-agnostic — they take an explicit `yyyymm` string and trust the caller. The
  bug is entirely in what the caller (channel layer) hands them.

**`core/digest.py` never calls `channel.send()` with any month/clock context** — `Channel.send()`
(the ABC in `channels/base.py`) has no `now`/`clock` parameter, so there is structurally no way
for the composer's carefully-resolved, tz-aware `now` to reach the ledger write. The channel
layer is on its own clock, always.

### Why this is a real bug, not just an inconvenience

1. **Timezone mismatch, every day.** `config.app.timezone = "Asia/Bangkok"` (UTC+7,
   `config.toml.line:16`), but `_send_push`/`_push` use naive `datetime.now()` with zero tz
   conversion — i.e. whatever timezone the deploy host's OS clock is set to (a VPS running the
   `deploy/habit-assistant-line.service` unit very plausibly defaults to UTC). Bangkok's midnight
   arrives 7 hours before UTC's. On the **last day of any month**, there is a real ~7-hour window
   (17:00–24:00 UTC on day N, which is already 00:00–07:00 Bangkok on day N+1) during which
   `compose_digest` — and anything the user/owner actually reads, since they think in Bangkok
   time — treats "today" as the new month, while `_send_push`'s `datetime.now()` (still on the
   old UTC day) writes the increment into the **old** month's bucket. A digest or realtime push
   sent in that window is silently misattributed to the wrong month.
2. **Gate vs. increment can diverge from each other, independent of #1.** `_push`'s gate read
   (`:367`) and `_send_push`'s increment (`:280`) are two unsynchronized `datetime.now()` calls
   with a live network POST to LINE's Push API in between them (`_push` → `await
   self._send_push(...)` → the actual `httpx` POST → *then* `_send_push`'s own `datetime.now()`
   read). A push that straddles the literal turn of a month between the gate check and the
   increment could pass the cap check against one month's total and then write into the next —
   a genuine (if narrow) violation of R-Q7's own stated rationale, "the bill can never surprise
   me."

### Fix shape (report only — production code NOT modified, per instructions)

Both call sites should derive `yyyymm` the same way `core/digest.py:_local_now` already does,
from a single source of truth:

```python
# channels/line.py — add near __init__, mirroring core/digest.py's own convention
from zoneinfo import ZoneInfo

def _now_yyyymm(self) -> str:
    """Same normalization as core/digest.py:_local_now — resolves through
    config.app.timezone rather than trusting the host OS's own local zone."""
    return datetime.now(ZoneInfo(self._config.app.timezone)).strftime("%Y-%m")
```
- Replace `yyyymm = datetime.now().strftime("%Y-%m")` at `_send_push` (`:280`) and `_push`
  (`:367`) with `yyyymm = self._now_yyyymm()`. This alone closes the timezone-mismatch bug
  (#1 above) and makes gate-vs-increment consistent in spirit (both derive from the same
  formula, though still two separate calls — closing #2 fully would mean threading one resolved
  `yyyymm` from `_push` into `_send_push` as a parameter instead of each recomputing it).
- Optional, lower priority: give `LineChannel.__init__` an injectable `clock:
  Callable[[], datetime] = datetime.now` (stored as `self._clock`, used inside `_now_yyyymm`
  instead of the bare call) — mirrors `core/digest.py`'s own injectable-clock convention and
  would let a future test exercise `LineChannel` itself deterministically without monkeypatching
  `datetime`. Not required to fix the timezone bug; only improves testability of module A
  directly.
- This is Luna's/module A's call to make — flagging for Archi to route back to her rather than
  patching `channels/line.py` here.

## Test-side fix (applied)

Both failing tests assumed the ledger write would land in the fixed `clock=`'s own month
(`"2026-08"`). It never has — `RecordingLineChannel.send()` (`tests/conftest.py:197-204`) is a
**deliberate, faithful mirror** of the real bug above (its own docstring: "exactly matching the
real channel's contract"), and itself keys `db.increment_push(chat_id,
datetime.now().strftime("%Y-%m"))` off the real wall clock, never off any injected clock. That
double is intentionally accurate to production and was **not** touched.

This is the exact shape the task described: this codebase already has an established,
deliberate workaround for this — three other LINE test files
(`tests/test_line_a_gaps.py:62-63`, `tests/test_line_channel.py:27-28`,
`tests/test_line_v12_gaps.py:50-51`) each define their own identical `_current_yyyymm()` helper
(`datetime.now().strftime("%Y-%m")`) specifically because they exercise the real `LineChannel`/
`RecordingLineChannel` push path and know it doesn't honor an injected clock. `test_digest.py`
and `test_line_c_gaps.py` were the two files that hadn't caught up to that convention yet — this
is the 4th member of the date-drift class (after the ones `test_pause.py`'s own fixed-anchor-date
standard addressed).

Fix applied — added the same `_current_yyyymm()` helper to both files and pointed the two
assertions at it instead of the literal `"2026-08"`:

- `tests/test_digest.py`: added `_current_yyyymm()` (next to `_fixed_now`); line 365's
  `yyyymm = "2026-08"` → `yyyymm = _current_yyyymm()`.
- `tests/test_line_c_gaps.py`: added `_current_yyyymm()` (next to the `OWNER`/`ALICE`/`BOB`
  constants); the two `db.push_count(..., "2026-08")` assertions in
  `test_push_ledger_increments_exactly_once_per_successful_push_not_per_composed_user` now use
  `_current_yyyymm()`.

This is date-independent forever in the same sense the existing three helpers already are: it
tracks whatever the real send path actually does (today, and after any future production fix),
rather than a literal tied to the digest's own fixed composition clock. It does **not** change
what the tests exercise (still real `run_daily_digest` → real `RecordingLineChannel` → real
`db.increment_push`), only what the correct expected month is.

No other assertion in either file needed the same fix — checked every `db.push_count(...)` call
in both files; the rest either assert `== 0` (true regardless of which month bucket a hypothetical
push would have landed in, since no send occurs on that path) or use a local, fully self-hardcoded
double (`test_push_ledger_not_incremented_when_the_send_itself_fails`'s `_FlakyChannel` writes and
reads the same literal `"2026-08"` by construction — self-consistent, not wall-clock-dependent,
not at risk).

## Exit bar — results

```
tests/test_digest.py::test_run_daily_digest_increments_push_ledger_exactly_once_per_user       PASSED
tests/test_line_c_gaps.py::test_push_ledger_increments_exactly_once_per_successful_push_not_per_composed_user  PASSED

tests/test_digest.py tests/test_line_c_gaps.py tests/test_line_v12_gaps.py:  142 passed, 0 failed
tests/ -k "line" (full LINE-branch subset, incl. portal's one line-webhook test):  427 passed, 3 skipped, 0 failed
```

Real wall clock at triage time: `2026-09-01` (confirmed via `python -c "import datetime; print(datetime.datetime.now())"`).

## Sweep — other real-clock dependencies in LINE-branch test files

Grepped every `test_line*.py` + `test_digest.py`/`test_line_c_gaps.py` for `datetime.now()` /
`date.today()` / `isocalendar` / week-53 patterns.

**Not at risk — same self-consistent pattern already vetted above (write and read both derived
from one real-clock call within the same test, so they move together):**
- `test_line_a_gaps.py`, `test_line_channel.py`, `test_line_v12_gaps.py` — own `_current_yyyymm()`
  helpers (pre-existing, correct).
- `test_line_integration.py:305,308,332,533,555,580`, `test_line_release_gate.py:101,149,195,236,
  285,400,521,552,585,614`, `test_line_v12_integration.py:196,273-274` — all `yyyymm =
  datetime.now().strftime("%Y-%m")` / `today = datetime.now().date().isoformat()` computed once
  and reused for both the seeded data and the assertion within the same test.
- `test_line_v12_gaps.py:665-692` — picks `today = datetime.now().date()`, nudges off Monday
  (documented ISO-week artifact avoidance, not a bug), then monkeypatches `jobs_module.date` so
  `grace_tick`'s own `date.today()` read is frozen to that *same* real-clock-derived value —
  self-consistent by construction.
- ISO-week-53 tests (`test_v19_grace_gaps.py`, `test_v19_cadence_gaps.py`,
  `test_v19_release_gate.py`, `test_pause.py::TestEngineIsoWeekBoundaryNeutral`) already anchor on
  **explicit fixed dates** (2026-12-28 Mon .. 2027-01-03 Sun, verified via `date.isocalendar()`
  in their own docstrings), not the real clock — not at risk at the next year rollover.

**No new drift bugs found beyond the one already fixed above.** The only genuine hazard is the
production one reported above (`channels/line.py`'s untimezoned `datetime.now()`), which is
structural, not test-only, and will keep reproducing (invisibly, since no other test currently
probes cross-midnight/cross-month timezone skew directly) until Luna applies the fix shape above.
Worth a follow-up ask to Archi: no test in this suite currently exercises the
`config.app.timezone != host OS timezone` scenario directly (every test here runs on whatever the
CI/dev machine's local zone is) — a dedicated test for that would need `LineChannel` to accept an
injectable clock first (the optional part of the fix shape above), so it's blocked on Luna's fix,
not addable today without touching production code.

## Recommendation

**Escalate to Archi** — production bug found in `channels/line.py` (`_send_push`/`_push`, module
A / Luna's ownership), fix shape above, not applied here per Vera's mandate. Test-side fix (the
2 named tests + no others) is applied and green; full LINE-branch subset and the wider `-k line`
suite are green (427 passed, 3 skipped, 0 failed). Recommend Archi route the fix-shape report to
Luna for `channels/line.py`, then re-run this same subset after her patch to confirm the
production fix doesn't change any of the now-passing assertions above (it shouldn't — they assert
via `_current_yyyymm()`, which will remain correct whether `_send_push` uses naive or
tz-normalized `datetime.now()`, since both compute against the real clock at test time).
