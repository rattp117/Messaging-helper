# Implementation — v0.7.0 Multi-Habit, Module M2 (Reminders)

> Scope: **exclusively** `core/reminders.py` + `tests/test_reminders.py`, per
> Archi's module split (SPEC-v0.7.md §11, "M2 Reminders"). `main.py`,
> `core/parser.py`/`core/commands.py` (M1), `core/review.py` (M3), and all
> shared-surface files were read-only inputs, never touched.

## Files changed

| File | Created/Modified | Change |
|---|---|---|
| `src/habit_assistant/core/reminders.py` | modified | `schedule_reminders(scheduler, channel, config, registry: HabitRegistry)` now iterates **every habit in the registry** (not a hardcoded water/stretch/diary tuple) and registers one cron job per `reminder_times` entry, binding `(channel, habit, language)` — `habit` is the real `Habit` object, not a category string. `send_reminder(channel, habit: Habit, language)` resolves copy per SPEC-v0.7.md §4 R15: built-in id (`BUILTIN_IDS`) → its existing `reminder_water`/`_stretch`/`_diary` catalog entry, byte-identical to v0.6.0; else `habit.reminder_text(language)` if the config set one; else the type-generic `reminder_generic` template with `label=habit.label(language)`. Kept a module-level `REMINDER_TEXTS` dict (English text per built-in id) purely so `main.py`'s frozen, unconditional top-level import (`from habit_assistant.core.reminders import REMINDER_TEXTS, schedule_reminders, send_reminder`) doesn't raise `ImportError` — nothing in this module reads it anymore; see Known limitations. |
| `tests/test_reminders.py` | rewritten | All tests rebuilt against the new `(scheduler, channel, config, registry)` / `(channel, habit, language)` contract. AC13: byte-identical built-in reminder text (en + the exact v0.6.0 Thai water string), 9-job default schedule, per-time cron fields, timezone, `replace_existing` dedup, job `args` carrying the actual `Habit` object. AC14: a new habit's `reminder_times` gets scheduled and fires its own `reminder_text` in the resolved language; a new habit *without* `reminder_text` fires `reminder_generic`; a habit with empty `reminder_times` schedules nothing; a mixed registry (built-in + silent + new) schedules only the habits that have times. One pre-existing end-to-end test (`test_async_main_registers_weekly_review_job_from_config`) is `@pytest.mark.skip`ed — see Known limitations. |

## How it works

`schedule_reminders` no longer special-cases three categories: it loops `for habit in registry: for t in habit.reminder_times:` and calls `scheduler.add_job(send_reminder, trigger=CronTrigger(...), args=[channel, habit, language], id=f"reminder_{habit.id}_{t}")`. Since `HabitRegistry.from_config(Config())` yields exactly the same three habits with the same `reminder_times` lists the v0.6.0 hardcoded tuples had, the default schedule is unchanged in job count and times (AC13). `send_reminder` is a pure copy-resolution + `channel.send()` call: built-ins go through the same `i18n.t("reminder_water"/"reminder_stretch"/"reminder_diary", language)` calls as v0.6.0 (verified byte-identical against the live catalog string, not just re-derived), any other habit id falls through to `habit.reminder_text(language)` or the `reminder_generic` catalog template (AC14). Language is resolved once per `schedule_reminders` call (unprompted-send rule, unchanged from v0.6.0) and baked into every job's args, exactly as before.

## Smoke test done

- `.venv\Scripts\python.exe -m pytest -q tests/test_reminders.py tests/test_habits.py -v` → **28 passed, 1 skipped** (the 1 skip is the documented main.py-boundary test below; `test_habits.py` is shared-surface-owned, run alongside only to confirm I didn't regress `HabitRegistry`/`Habit` usage).
- Manual interpreter smoke test (`python -c "..."`): built a real `HabitRegistry.from_config(Config())`, called `schedule_reminders` against a real `AsyncIOScheduler` → confirmed `len(scheduler.get_jobs()) == 9`; called `send_reminder(channel, registry.get("water"), "th")` against a fake channel and printed the captured text with `PYTHONIOENCODING=utf-8` → got exactly `💧 ถึงเวลาดื่มน้ำแล้วนะ วันนี้ดื่มไปเท่าไหร่แล้ว?`, matching `core/i18n.py`'s `reminder_water`/`th` entry character-for-character.
- Full suite (`pytest -q`, no filter) run for visibility — see "Remaining non-M2 failures" below; my own file (`test_reminders.py`) is stable and green across repeated runs, the full-suite total fluctuated between two consecutive runs (37→53 failed) purely from concurrent edits by the other Lunas (M1/M3) mid-flight in this shared tree, not from anything in this changeset.
- No real Telegram/Ollama calls; no touch to `data\habits.db`, `.env`, or the running production process (PID 13956, not queried this session — nothing here starts a process or scheduler against real infra). No git commit made.

## Maps to acceptance criteria

- **AC13** [→R15, AC7.1-reminders] → `core/reminders.py:schedule_reminders` + `send_reminder`. Tests: `test_schedule_reminders_registers_one_job_per_configured_time` (9 jobs = 6 water + 2 stretch + 1 diary), `test_schedule_reminders_cron_times_match_config`, `test_send_reminder_sends_byte_identical_v060_text_for_each_builtin_habit`, `test_send_reminder_water_habit_thai_is_byte_identical_to_v060` (asserts the literal Thai string from SPEC-v0.7.md's own v0.6.0 corpus).
- **AC14** [→R15] → `core/reminders.py:send_reminder`'s built-in/else branch + `schedule_reminders`'s per-habit loop. Tests: `test_schedule_reminders_adds_one_job_for_a_new_habit_with_reminder_times`, `test_send_reminder_new_habit_with_reminder_text_sends_it_in_resolved_language`, `test_send_reminder_new_habit_without_reminder_text_uses_generic_template`, `test_schedule_reminders_habit_with_no_reminder_times_schedules_nothing`, `test_schedule_reminders_mixed_registry_only_schedules_habits_with_times`.

Both ACs pass on a clean, isolated run of `tests/test_reminders.py`.

## Existing test expectation changes (old → new, for Vera's audit)

All in `tests/test_reminders.py` (the only test file I own):

| Test | Old | New | Why |
|---|---|---|---|
| `test_send_reminder_sends_correct_text_per_category` → renamed `test_send_reminder_sends_byte_identical_v060_text_for_each_builtin_habit` | `send_reminder(channel, "water")` (bare string) | `send_reminder(channel, registry.get("water"), "en")` (real `Habit`) | SPEC-v0.7.md §5 fixes `send_reminder(channel: Channel, habit: Habit, language)` — habit objects, not category strings, are the contract. |
| `test_send_reminder_unknown_category_raises_value_error` | asserted `ValueError` for an unrecognized category string | **removed** | There is no "unknown category" case anymore — the caller always resolves a `Habit` (or doesn't call at all) via `registry.get()`; passing a nonexistent id is a caller bug outside this function's contract, not a runtime input to validate. |
| `test_schedule_reminders_registers_one_job_per_configured_time`, `test_schedule_reminders_cron_times_match_config`, `test_schedule_reminders_uses_configured_timezone`, `test_schedule_reminders_replace_existing_does_not_duplicate` | called `schedule_reminders(scheduler, channel, config)` with `config.reminders.water/stretch/diary.times` | call `schedule_reminders(scheduler, channel, config, registry)` where `registry = HabitRegistry.from_config(config)`, and `config` is built via `Config.model_validate({"habits": [...]})` instead of the legacy `reminders` block | SPEC-v0.7.md §5 fixes the 4-arg signature; `reminder_times` now lives on each `[[habits]]` entry, not the legacy `[reminders.*]` config section (§2.1). Same resulting job count/times/timezone assertions, unchanged. |
| `test_schedule_reminders_job_args_bind_correct_category_and_channel` → renamed `test_schedule_reminders_job_args_bind_correct_habit_and_language` | asserted `job.args == (channel, "water", "th")` | asserts `job.args == (channel, registry.get("water"), "th")` | Job args now bind the resolved `Habit` object (needed by `send_reminder`'s new signature), not a bare id string. |
| `test_async_main_registers_weekly_review_job_from_config` | ran end-to-end, asserting `schedule_reminders`'s jobs land via `async_main` | `@pytest.mark.skip`ped, same body | `main.py`'s `schedule_reminders(scheduler, channel, config)` call site is a 3-arg call (frozen, v0.6.0 shape — see Known limitations); calling the new 4-arg `schedule_reminders` from it raises `TypeError` until Archi's integration step flips it. AC13/AC14 are already covered directly, at the unit level, by the tests above — this test only re-verified the same wiring end-to-end through `main.py`, which is integration's job, not M2's. |

No test outside `tests/test_reminders.py` was touched.

## Known limitations

**1. `main.py`'s reminder call sites are frozen on the v0.6.0 contract and will raise until integration.** Per my brief, I must not touch `main.py`. It currently has:
- `schedule_reminders(scheduler, channel, config)` (3-arg, no registry) at its one unconditional startup call site.
- `send_reminder(channel, args.test_reminder, i18n.resolve_unprompted_language(config))` in the `--test-reminder` CLI branch, where `args.test_reminder` is a bare category string.

Both are already flagged in `main.py` with `NOTE` comments (added by the shared-surface Luna, referencing `IMPL.md`'s "Known limitations") stating explicitly that calling the new SPEC-v0.7.md §5 signatures from these frozen sites "would raise" and is "deferred to integration" (`SPEC-v0.7.md` §11 "Integration order" step 1). Implementing `core/reminders.py` exactly per the frozen §5 contract (no optional/back-compat params — the spec's signature has none) therefore surfaces that documented boundary as two concrete test failures, **both pre-existing-file, not-mine-to-fix**:
- `tests/test_cli.py::test_test_reminder_flag_sends_correct_reminder_text_offline`
- `tests/test_cli.py::test_test_reminder_flag_fails_cleanly_on_401_not_crash`

Both fail with `AttributeError: 'str' object has no attribute 'id'` inside `send_reminder`, i.e. exactly the expected, single-cause boundary break — not a new/different bug. These will go green once Archi's integration step flips `main.py`'s `--test-reminder` branch to build/pass a real `Habit` (e.g. `registry.get(args.test_reminder)`).

I considered accepting `habit: Habit | str` in `send_reminder` to avoid this, but chose not to: the spec's §5 signature has no such union, `main.py`'s own NOTE already treats this exact breakage as expected and deferred, and a permissive union type would be a silent, undocumented contract softening exactly where the parallel-module design wants a hard, auditable interface.

**2. `REMINDER_TEXTS` kept as a dead but importable name.** `main.py`'s top-level `from habit_assistant.core.reminders import REMINDER_TEXTS, schedule_reminders, send_reminder` is unconditional (executes at process/test-collection import time, not at call time). My first draft dropped `REMINDER_TEXTS` (spec §5 doesn't mention it) and that broke **collection** of 7 unrelated test files (`test_bilingual_confirmations.py`, `test_cli.py`, `test_commands.py`, `test_confirmations.py`, `test_fallback.py`, `test_resilience.py`, `test_v060_bilingual_gaps.py`) with `ImportError` — a far worse blast radius than a runtime `TypeError` in the two files that actually exercise reminders. I restored `REMINDER_TEXTS` (English text per built-in id, same computation as v0.6.0) purely to keep the import graph intact; nothing in `core/reminders.py` reads it. Remove it once integration flips `main.py`'s import.

**3. Full-suite numbers are not stable right now — not from this changeset.** Two consecutive `pytest -q` runs (no changes in between) went from 37 failed/337 passed to 53 failed/321 passed, with the delta entirely in `test_db.py` (`compute_weekly_stats`/`format_stats_summary`), `test_review.py`, `test_commands.py`, and `test_v060_bilingual_gaps.py` — all `TypeError`s pointing at `run_weekly_review()`/`compute_weekly_stats` mid-signature-change, i.e. module M3 (`core/review.py`) being actively edited by another Luna in this same tree while I was testing. `tests/test_reminders.py` itself was reconfirmed stable (12 passed, 1 skipped) across every run in this session.

## Remaining non-M2 failures (for Archi/Vera — not my scope)

From the last full-suite run, grouped by owner, excluding the 2 test_cli.py failures documented above as this module's own boundary fallout:

- **Module M1 (`core/parser.py`, `core/commands.py`, `llm/ollama_client.py` old-contract callers) — pre-existing per shared-surface `IMPL.md`:** `tests/test_fallback.py` (19), `tests/test_parser.py` (12), `tests/test_commands.py::test_soft_deleted_row_excluded_from_stretch_count_and_ordinal`, `tests/test_commands.py::test_command_layer_does_not_intercept_a_normal_log_even_after_commands_ran`, `tests/test_resilience.py` (3), `tests/test_cli.py::test_dry_run_flag_via_async_main_prints_structured_result_offline`.
- **Module M3 (`core/review.py`) — appears to be mid-edit by the concurrent M3 Luna, not stable enough to characterize precisely:** `tests/test_db.py` (`compute_weekly_stats`/`format_stats_summary`, 6 tests), `tests/test_review.py` (7 tests), `tests/test_commands.py::test_soft_deleted_rows_excluded_from_weekly_review_stats`, `tests/test_v060_bilingual_gaps.py` (2 weekly-review-narrative tests) — all `TypeError`s at `run_weekly_review()`/`compute_weekly_stats()` call sites, consistent with M3's file being under active construction, not a defect in my scope.

None of the above touch `core/reminders.py` or `tests/test_reminders.py`.
