# Implementation — push-ledger month-key clock fix

Fixes the production bug TEST-LEDGER-TRIAGE.md found and left unfixed (report-only, per its own
instructions): `channels/line.py`'s `_send_push`/`_push` each computed `yyyymm` from a bare,
untimezoned `datetime.now()` — the host OS's own local clock, independent of
`config.app.timezone` (`Asia/Bangkok`, UTC+7) — and did so via two *separate* `datetime.now()`
calls with a live network POST to LINE's Push API in between them.

## Files changed

| Path | Created/modified | Description |
|---|---|---|
| `src/habit_assistant/channels/line.py` | modified | Added `LineChannel._now_yyyymm()` (tz-normalizes an injectable `self._clock()` through `config.app.timezone`, mirroring `core/digest.py:_local_now`'s own naive-vs-aware convention). Added a `clock: Callable[[], datetime] = datetime.now` constructor param, stored as `self._clock`. `_push` now resolves `yyyymm` exactly ONCE, up front, and threads it into `_monthly_push_total_fail_closed`, `_send_push` (new optional `yyyymm=` kwarg), and `_maybe_alert_quota_warn`/`_maybe_alert_quota_stop` (which now also thread it into their own `_send_push` calls). Replaced both bare `datetime.now().strftime("%Y-%m")` call sites. |
| `tests/conftest.py` | modified | `RecordingLineChannel` gained a matching `clock: Callable[[], datetime] = datetime.now` constructor param (`self._clock`), and `send()` now keys `db.increment_push` off `self._clock()` instead of a bare `datetime.now()`. Default behavior (no `clock=` passed) is unchanged — still the real wall clock, no tz normalization (this double takes no `config`, so it doesn't attempt `LineChannel`'s tz-normalization; it exists to give module-C/integration tests an injectable seam, not tz-correctness of its own). Docstring updated to stop describing itself as a "faithful mirror of the real bug" — the bug is now fixed in production; the double's *un-injected* default simply still matches `LineChannel`'s own un-injected default (real wall clock). |
| `tests/test_line_channel.py` | modified | Extended `_make_channel` with optional `mode`, `push_cap`, `clock` kwargs (all default to prior behavior — additive only). Added a `_TickingClock` test double (returns each of N values in order, then repeats the last) and 7 new tests covering AC-equivalent behavior below. |

## How it works

`LineChannel._now_yyyymm()` reads `self._clock()` (defaults to `datetime.now`, injectable via the
constructor) and normalizes it through `ZoneInfo(self._config.app.timezone)` — a naive result is
treated as already being in that timezone (byte-identical convention to `core/digest.py:_local_now`
and `core/timeutil.py`), an aware one is converted. `_push` (the gated realtime entry point every
no-reply-context send funnels through via `_emit`) now calls `_now_yyyymm()` exactly once at its
top and passes that single string down through every branch — the gate's `monthly_push_total`
read, the eventual `_send_push` call (which increments `push_ledger`), and the once-per-month
owner alerts. `_send_push` keeps an optional `yyyymm=None` fallback (resolving its own
`_now_yyyymm()` if not given) so it stays independently callable, but every real production call
site in this file now supplies the one value `_push` already resolved — the gate-read month and
the increment month are the same string by construction, closing the straddle window.

## Smoke test done

Ran the required exit-bar subset and a wider LINE-branch sweep from the worktree with
`PYTHONPATH=src`:

```
python -m pytest tests/test_line_channel.py tests/test_digest.py tests/test_line_c_gaps.py tests/test_line_v12_gaps.py -q
  -> 172 passed in 13.68s   (0 failed)

python -m pytest tests/ -k "line" -q
  -> 434 passed, 3 skipped, 0 failed
```

Also spot-checked `test_line_channel.py` alone (30 passed, includes all 9 new/changed tests) and
confirmed the four exit-bar files individually show 0 failures before/after (166 passed pre-fix
baseline for those four files, same suite green post-fix at 172 with the new tests added).

## Maps to task requirements

- **Item 1** (`_now_yyyymm()` mirroring `_local_now`, used at both sites) → `channels/line.py:_now_yyyymm`, used by `_send_push` (fallback) and `_push` (primary resolution).
- **Item 2** (kill the straddle — compute once in `_push`, thread into `_send_push`) → `channels/line.py:_push` resolves `yyyymm` once at the top; every downstream call (`_monthly_push_total_fail_closed`, `_send_push(..., yyyymm=yyyymm)`, `_maybe_alert_quota_warn`/`_stop`) reuses it. `_maybe_alert_quota_warn`/`_maybe_alert_quota_stop` also now thread `yyyymm` into their own `_send_push` calls (not required by the letter of item 2, but closes the same class of gap for the owner-alert sends, which previously would have called `_now_yyyymm()` a further, separate time).
- **Item 3** (injectable clock seam + `RecordingLineChannel` update) → `channels/line.py:LineChannel.__init__`'s new `clock=` param / `self._clock`; `tests/conftest.py:RecordingLineChannel` mirrors it with its own `clock=` param, comment updated to no longer claim to be a "deliberate mirror of the real bug" (the bug is fixed in production now).
- **Item 4** (tests) → `tests/test_line_channel.py`, new section "Line-clock fix (branch line-version, TEST-LEDGER-TRIAGE.md)":
  - `test_now_yyyymm_normalizes_through_config_timezone_not_a_bare_clock_read` — probes the 7-hour Bangkok-vs-UTC divergence window with an injected aware-UTC clock (2026-08-31 18:00 UTC → Bangkok 2026-09-01 01:00 → `"2026-09"`).
  - `test_now_yyyymm_naive_clock_treated_as_already_local_per_local_now_convention` — naive-clock convention parity with `_local_now`.
  - `test_digest_mode_push_resolves_yyyymm_exactly_once` and `test_realtime_gate_read_and_ledger_increment_share_one_yyyymm_across_a_month_tick` — use `_TickingClock` (two DIFFERENT-month values) to prove the clock is read exactly once per push attempt, and that the gate-read month and the ledger-increment month are identical even when the injected clock would otherwise tick over between them.
  - `test_realtime_gate_fail_closed_behavior_unchanged_with_injected_clock` — R-Q7 fail-closed gate behavior confirmed unchanged under an injected clock (mirrors `test_line_v12_gaps.py`'s existing real-clock version of this test).
  - `test_owner_pushes_bypass_gate_and_still_use_the_injected_clocks_month` — owner exemption still works and still resolves the injected clock's month.

## Known limitations / notes for the record

- `tests/test_digest.py` and `tests/test_line_c_gaps.py`'s own `_current_yyyymm()` helpers (added
  by Vera's triage fix) still compute against the **real, un-normalized** host wall clock
  (`datetime.now().strftime("%Y-%m")`), matching `RecordingLineChannel`'s own default (still real
  wall clock, still no tz normalization — see `tests/conftest.py` docstring). This is intentional
  and unaffected by this fix: those two files exercise `RecordingLineChannel`, not the real
  `LineChannel`, so they never see `_now_yyyymm`'s tz normalization. Confirmed green on this run's
  host TZ; genuine host-TZ-dependence remains in those two files' `_current_yyyymm()` helpers (and
  the three pre-existing sibling helpers in `test_line_a_gaps.py`/`test_line_channel.py`'s own
  `_current_yyyymm` used for non-clock-injected assertions/`test_line_v12_gaps.py`) exactly as
  TEST-LEDGER-TRIAGE.md already noted — not introduced or worsened by this change, and out of this
  fix's scope (would require giving `RecordingLineChannel` a `config` + the same `ZoneInfo`
  normalization, which no current consumer test needs).
- No production behavior change for any caller that doesn't inject a `clock` — the default
  `clock=datetime.now` combined with the new `ZoneInfo(config.app.timezone)` normalization is a
  strict correctness fix (previously-naive host-local reads become correctly Bangkok-normalized
  reads), not a new knob any deployment has to configure.
- Nothing committed, per instructions — folds into `line/v1.3.0` whenever Archi cuts that release.
