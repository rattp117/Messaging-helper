# Test Report — v0.7.0 Multi-Habit, Module M2 (Reminders)

## Summary
- Scope: `core/reminders.py` + `tests/test_reminders.py` only (SPEC-v0.7.md §11 "M2 Reminders").
- Total (module-scoped run): 13 tests in `test_reminders.py` (12 passed, 1 skipped) + 16 in `test_habits.py` (shared-surface sanity, all passed) = **28 passed, 1 skipped, 0 failed**.
- Command run: `.venv\Scripts\python.exe -m pytest -q tests/test_reminders.py tests/test_habits.py`
- Full suite was **not** run/judged per instructions (M1/M3 tracks mid-flight in the shared tree).
- Status: **PASS** (both owned ACs green; the 1 skip is a documented, justified integration boundary, not an M2 defect).

## Test files

| Path | Tests | Covers |
|---|---|---|
| `tests/test_reminders.py` | 12 passed, 1 skipped (13 total) | AC13, AC14 |
| `tests/test_habits.py` | 16 passed | shared surface (`Habit`/`HabitRegistry`) sanity only — not M2-owned, run to confirm no regression in M2's dependency |

No new `tests/test_v07_m2_*.py` file was needed — Luna's rewritten `test_reminders.py` already gives 1:1 AC coverage with clear, behavior-named tests; I verified it independently (re-derived expected values from `core/i18n.py`'s live catalog and `v0.6.0`'s tagged `config.py`, not from Luna's IMPL.md claims) rather than duplicating it.

## AC coverage

**AC13** [→R15, AC7.1-reminders] — default config → same 9 cron jobs (6 water/2 stretch/1 diary) at the same times as v0.6.0; `send_reminder(channel, water_habit, "th")` byte-identical to v0.6.0 Thai water reminder.
- `test_schedule_reminders_registers_one_job_per_configured_time` → PASS (9 jobs: 6/2/1)
- `test_schedule_reminders_cron_times_match_config` → PASS
- `test_schedule_reminders_job_args_bind_correct_habit_and_language` → PASS
- `test_schedule_reminders_uses_configured_timezone` → PASS
- `test_schedule_reminders_replace_existing_does_not_duplicate` → PASS
- `test_send_reminder_sends_byte_identical_v060_text_for_each_builtin_habit` → PASS
- `test_send_reminder_water_habit_thai_is_byte_identical_to_v060` → PASS
- **Verdict: AC13 → PASS**

Independently re-verified (not just trusting the test file):
- `Config()` reminder_times: water `('08:00','10:30','13:00','15:30','18:00','20:30')`, stretch `('11:00','16:00')`, diary `('21:30',)` — confirmed live via interpreter, matches `git show v0.6.0:src/habit_assistant/config.py`'s `WaterConfig.times`/`StretchConfig.times`/`DiaryConfig.times` defaults exactly (6/2/1, same clock values).
- `i18n.t("reminder_water", "th")` in the live catalog is exactly `💧 ถึงเวลาดื่มน้ำแล้วนะ วันนี้ดื่มไปเท่าไหร่แล้ว?` — matches the string hard-coded in both `test_send_reminder_water_habit_thai_is_byte_identical_to_v060` and SPEC-v0.7.md's own examples.

**AC14** [→R15] — new habit's `reminder_times` scheduled; fires its `reminder_text` in resolved language; no `reminder_text` → `reminder_generic` template; empty `reminder_times` → zero jobs.
- `test_schedule_reminders_adds_one_job_for_a_new_habit_with_reminder_times` → PASS
- `test_send_reminder_new_habit_with_reminder_text_sends_it_in_resolved_language` → PASS
- `test_send_reminder_new_habit_without_reminder_text_uses_generic_template` → PASS
- `test_schedule_reminders_habit_with_no_reminder_times_schedules_nothing` → PASS
- `test_schedule_reminders_mixed_registry_only_schedules_habits_with_times` → PASS
- **Verdict: AC14 → PASS**

Independently re-verified: `i18n` catalog's `reminder_generic` entry is `⏰ Time for {label}. How did it go?` (en) / `⏰ ถึงเวลา{label}แล้วนะ วันนี้เป็นยังไงบ้าง?` (th) — matches `send_reminder`'s fallback call and SPEC-v0.7.md §4 R15's description ("type-generic `reminder_generic` template").

Both ACs owned by M2 are **PASS**. No AC outside AC13/AC14 belongs to this track (SPEC-v0.7.md §11 table).

## Failures (if any)

None in M2 scope.

## Regressions detected

None. `test_habits.py` (shared-surface, not M2-owned but a direct dependency of `core/reminders.py`) is fully green — `Habit`/`HabitRegistry` construction and `reminder_text()` resolution that M2 relies on are unaffected by M2's own change.

## Audit — Luna's expectation-change table (per Archi's brief)

**(a) Removed test: `test_send_reminder_unknown_category_raises_value_error`.**
Justified, not a coverage hole. The v0.6.0 contract took a bare `category: str` and had to validate it against a fixed `{water,stretch,diary}` map at runtime, so an unrecognized string was a real caller input needing a `ValueError`. SPEC-v0.7.md §5 changes the signature to `send_reminder(channel: Channel, habit: Habit, language)` — the parameter is typed as an already-resolved `Habit` object, not an id to look up. Section 8's AC13/AC14 text describes no error path for `send_reminder`; the only place a bad id could arise is `registry.get(some_id)` returning `None`, and that resolution happens at the *caller* (either `schedule_reminders`'s `for habit in registry:` loop, which only ever yields real `Habit`s, or `main.py`'s `--test-reminder` branch, which is exactly the frozen boundary discussed below). There is no code path inside `core/reminders.py` itself that receives an unresolved id post-integration, so there is nothing left in this module's contract for a `ValueError` test to protect. I checked whether `schedule_reminders` or `send_reminder` should defensively guard against `habit=None` — spec's signature is `habit: Habit` (not `Habit | None`), and IMPL.md explicitly weighed and rejected a permissive `Habit | str` union for the same reason. I agree with that call: a hard, typed contract is what the parallel-module design wants here. **Not a coverage hole for M2.**

**(a) Skipped test: `test_async_main_registers_weekly_review_job_from_config`.**
Justified as a *skip*, but it is real coverage that must be picked up at integration, not silently dropped. Confirmed independently: `main.py:731` still calls `schedule_reminders(scheduler, channel, config)` (3-arg, no `registry`), which raises `TypeError: schedule_reminders() missing 1 required positional argument: 'registry'` immediately — before the weekly-review job at `main.py:733+` is ever registered — so the test cannot currently pass or even meaningfully partially-run. The two behaviors AC13/AC14 actually require of `schedule_reminders`/`send_reminder` are already fully covered at the unit level by the other 12 tests (verified above). What this skipped test additionally covered — that `main.py`'s `async_main` wires `schedule_reminders` and the weekly-review job onto the **same** scheduler instance — is main.py wiring, which SPEC-v0.7.md §11 "Integration order" step 1 explicitly assigns to the integration pass, not to M2. **Recommendation: the integration Vera must re-enable (unskip) this exact test, or an equivalent, once `main.py`'s call site is flipped to pass `registry` — do not let it stay permanently skipped.**

**(b) Two `test_cli.py` boundary failures — confirmed genuinely integration-deferred, not M2 defects.**
Reproduced directly:
- `tests/test_cli.py::test_test_reminder_flag_sends_correct_reminder_text_offline`
- `tests/test_cli.py::test_test_reminder_flag_fails_cleanly_on_401_not_crash`

Both fail identically: `AttributeError: 'str' object has no attribute 'id'` at `src/habit_assistant/core/reminders.py:52` (`if habit.id in BUILTIN_IDS:`), called from `main.py:663` — `await send_reminder(channel, args.test_reminder, i18n.resolve_unprompted_language(config))`, where `args.test_reminder` is still a bare category string (the frozen v0.6.0 `--test-reminder` CLI contract). Verified `main.py` itself carries an explicit `NOTE` comment at lines 656–661 documenting this exact deferral ("`send_reminder` still takes a category string ... same deferred-to-integration reasoning applies here"), written by the shared-surface Luna before M2 started — i.e. this boundary was anticipated and documented in the shared surface, not introduced by M2's implementation. `core/reminders.py`'s own contract (`habit: Habit`) is exactly SPEC-v0.7.md §5's signature; there is no way to satisfy both the frozen `main.py` call site and the spec'd `core/reminders.py` signature simultaneously without touching `main.py`, which is out of M2's file ownership. **Confirmed: genuine integration-deferred boundary breaks, not M2 defects.**

## Items to hand to the integration Vera

1. `tests/test_cli.py::test_test_reminder_flag_sends_correct_reminder_text_offline` — will go green once `main.py`'s `--test-reminder` branch (around line 656-663) is flipped to resolve a real `Habit` via `registry.get(args.test_reminder)` before calling `send_reminder`.
2. `tests/test_cli.py::test_test_reminder_flag_fails_cleanly_on_401_not_crash` — same fix as #1 unblocks it; note its own docstring flags a *separate*, pre-existing issue worth keeping on the integration radar: the `--test-reminder` branch has no try/except around the network `channel.send()` call, so a 401 currently surfaces as a raw unhandled exception rather than a deliberate clean `SystemExit`. That second issue is orthogonal to the M2 signature boundary and was already present before M2 (per Luna's docstring) — confirm during integration whether it's in scope to fix or tracked separately.
3. `tests/test_reminders.py::test_async_main_registers_weekly_review_job_from_config` (currently `@pytest.mark.skip`) — unskip after `main.py:731`'s `schedule_reminders(scheduler, channel, config)` call is flipped to the 4-arg `(scheduler, channel, config, registry)` form. This is the one place that verifies reminder jobs and the weekly-review job coexist correctly on the same live scheduler through the real `async_main` path; AC13/AC14 don't need it (already unit-covered) but AC17's "jointly byte-identical, wiring-only edits" composite claim does.
4. Confirm `main.py`'s dead `REMINDER_TEXTS` re-export (kept only to avoid `ImportError` at collection time for ~7 unrelated test files) is removed from `core/reminders.py` once `main.py`'s import line (`from habit_assistant.core.reminders import REMINDER_TEXTS, schedule_reminders, send_reminder`) drops the unused name — currently dead code, correctly flagged by Luna as temporary.

## Recommendation

**Ready to ship** (M2 track). Both owned ACs (AC13, AC14) are PASS on a clean, isolated run; the single skip and the two `test_cli.py` failures are confirmed genuine, pre-documented shared-surface/main.py boundary issues external to `core/reminders.py`'s contract, not defects introduced by this module. Handing the 4 items above to the integration Vera for the post-wiring-flip pass (SPEC-v0.7.md §11 "Integration order" step 1, AC17).
