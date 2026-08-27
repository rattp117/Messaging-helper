# Test Report — Refactor Stage 2 (main.py decomposition, v1.9.2)

## Summary

- Total: 4335 tests (4308 baseline + Luna's 4 `test_refactor_s2_gaps.py` tests already counted in that 4308, + 27 new adversarial tests in `tests/test_refactor_s2_verify.py`)
- Passed: 4335 (both runs)
- Failed: 0 (both runs)
- Skipped: 1 · xfailed: 1 (unchanged from baseline)
- Two consecutive full-suite runs: **202.79s** and **209.38s**, both `4335 passed, 1 skipped, 1 xfailed`, 0 failed
- `data/habits.db` `LastWriteTime`: `2026-08-21 08:58:03 AM` before AND after the entire verification session (checked via PowerShell `Get-Item`, and `git status --short data/habits.db` reports no change) — the live DB was never touched
- **Status: PASS**

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_refactor_s2_verify.py` (new) | 27 | AC6, AC7, AC8, AC-G1, AC-G2 (adversarial, wired-level probes — see below) |
| `tests/test_refactor_s2_gaps.py` (Luna's, unmodified, re-verified) | 4 | AC6 (cycle, thin-entry), AC7 (dispatch-once) |
| `tests/test_riders.py` (1 sanctioned edit, reviewed) | 0 new | AC-G1 (verified the edit is genuinely mechanical) |
| Pre-existing 4306-test suite (unmodified) | 0 new | AC-G1, AC-G2 (byte-identical regression net) |

## AC coverage

| AC | Test(s) | Result |
|---|---|---|
| **AC6** — `main.py` < 150 lines; `handle_inbound_message`/`async_main`/jobs/confirmation formatter in own modules; no import cycle | `test_refactor_s2_gaps.py::test_no_import_cycle_anywhere_in_src`, `::test_no_module_in_src_imports_main_except_main_itself`, `::test_main_py_is_a_thin_entry_under_150_lines`; my own `test_module_line_counts_match_impl_refactor_s2_table`, `test_confirmation_leaf_imports_nothing_from_routing_or_quicklog_or_main`, `test_quicklog_imports_confirmation_not_the_reverse` | **PASS** |
| **AC7** — `commands.dispatch` invoked exactly once per routed message | `test_refactor_s2_gaps.py::test_dispatch_called_exactly_once_per_message_via_on_message`; my own `test_dispatch_called_exactly_once_per_on_message_call[ordinary_log_message]`, `[a_recognized_command]`, `test_on_callback_never_calls_commands_dispatch_at_all`, `test_monkeypatching_commands_dispatch_on_core_routing_module_takes_effect` | **PASS** |
| **AC8** — `core/quicklog.py` imports the confirmation formatter from `core/confirmation.py`; mirror deleted; quicklog's byte-identical tests still pass | `tests/test_quicklog.py`'s 6 `test_byte_identical_*` tests (unmodified, still green); my own `test_quicklog_tap_vs_typed_parity_via_on_message_and_on_callback_incl_stored_lang_pref` (re-proves it at the wired `on_message`/`on_callback` level, including a stored `/lang th` preference — the pre-existing suite only proved it through direct `handle_log_callback`/`handle_inbound_message` calls) | **PASS** |
| **AC-G1** — full suite green, unmodified except the one sanctioned `test_riders.py` edit | Full suite: 4335 passed / 0 failed / 1 skipped / 1 xfailed, two consecutive clean runs; `test_riders.py` diff reviewed line-by-line (see "Sanctioned edit review" below) | **PASS** |
| **AC-G2** — byte-identical output probe | New fixed-corpus wired-level tests (typed EN/TH, quicklog tap-vs-typed, `/help`, `/audit`, backfill prefix, routine run summary — see "Behavior preservation" below) + full manual source diff of `handle_inbound_message`/`confirmation_text`/`suffix`/all 6 job bodies/`async_main` against the pre-Stage-2 `main.py` (git `HEAD`) | **PASS** |

Every AC in Luna's Stage 2 scope (§8 of `SPEC-REFACTOR.md`: AC6, AC7, AC8, plus cross-cutting AC-G1/AC-G2) is covered above. Stage 1's ACs (AC1–AC5) and Stage 3/4's ACs are out of this stage's scope and untouched.

## Behavior preservation (the gate) — findings

I did a full line-by-line manual diff of the pre-Stage-2 `main.py` (`git show HEAD:src/habit_assistant/main.py`, 2523 lines) against the new `core/routing.py`, `core/jobs.py`, `core/app.py`, and `core/confirmation.py`, in addition to the wired-level tests below.

- **`handle_inbound_message`**: every one of the ~18 `command.kind` arms is untouched (diffed byte-for-byte, only docstrings/comments were trimmed). The water/stretch/diary/generic confirmation branches and the milestone/record/celebration-burst suffix logic — previously ~130 inline lines — were consolidated into `core/confirmation.py:confirmation_text`/`suffix`; I traced every conditional (`config.gamification.enabled and backfill_date is None` → `apply and config.gamification.enabled` where `apply = backfill_date is None`; the backfill-vs-live `record_clock` selection; the `_react_to_typed_log`/dashboard-refresh placement) and confirmed the boolean algebra and call order are unchanged.
- **`core/quicklog.py`**: the `_ordinal`/`_generic_confirmation` byte-mirror is gone (net −76 lines, matches IMPL); `_log_and_confirm` now calls the same `confirmation.confirmation_text`/`suffix` `core/routing.py` calls.
- **All 6 scheduler job bodies** (`minutely_tick`, `dashboard_day_rollover_job`, `weekly_review_job`, `daily_summary_job`, `grace_tick`, `wrapped_auto_job`) diffed identical to the pre-Stage-2 closures, apart from parameter-passing (closures → explicit params) and comment trimming.
- **`async_main`**: the `--seed`/`--dry-run`/`--migrate`/`--backup`/`--restore`/`--test-reminder` CLI branches, startup announce, command-menu construction (all 22 public + 5 owner-only commands), and scheduler registration are line-for-line identical to the original, modulo the documented monkeypatch-forwarding indirection.
- New wired-level byte-identity tests (through the real `core/routing.py:on_message`/`on_callback`, not a hand-rolled reimplementation): typed water confirmation EN and TH, stretch + a custom generic-numeric habit, quicklog tap-vs-typed parity **including a stored `/lang th` preference** (closes a gap the pre-existing suite's own `test_quicklog.py` doesn't cover, since it drives `handle_log_callback`/`handle_inbound_message` directly rather than `on_message`/`on_callback`), `/help` (compared against `discoverability.build_help_text` directly), `/audit` (compared against `audit_view.render_recent` directly, plus the non-owner silent-no-op gate), the backfill confirmation prefix (`"500ml yesterday"` against `backfill.confirmation_prefix`), and a routine run summary (`"2 of 2"`, DB rows verified). All pass.

**One minor, non-blocking finding**: `reparse_pending_unparsed`'s two internal `logger.info("Re-parsing %d deferred message(s)", ...)` and `logger.warning("...still unparseable after Ollama recovery...", ...)` calls were dropped during the `main.py` → `core/routing.py` move (confirmed by an exhaustive `logger.*` call-site count: 18 in the old file, 16 in the new files, and these are the only two missing — every other `logger.exception`/`logger.info` fail-open/diagnostic call site, including every job's own, survived intact). This is **not** a violation of SPEC-REFACTOR.md's own invariant list (§3: "no change to any i18n catalog string emitted, any `logs`/`audit_log`/settings row written, any migration, any config key's meaning, any command grammar, any PNG output") — it's an internal operator log line, not a string the bot emits to a chat, and no test in the 4335-test suite asserts on either of these two specific log messages (confirmed by grep). Flagging for Luna/Archi as a cheap one-line fix, not as a FAIL.

## Monkeypatch back-compat findings

I wrote live probes (not import-exists checks) for the load-bearing symbols on `habit_assistant.main`, driven through the REAL `async_main` → `core/app.py` → `core/jobs.py`/`core/routing.py` chain (a `_FakeScheduler` capturing registered jobs + a scripted `Channel` that runs them), confirming each patched value is actually used at the real call site, not just imported once at module load:

| Symbol patched on `habit_assistant.main` | Reaches (real call site) | Result |
|---|---|---|
| `load_config`, `load_secrets`, `AsyncIOScheduler`, `TelegramChannel`, `OllamaClient` | `core/app.py:async_main`'s bootstrap (all 6 jobs register successfully) | PASS (also exercised extensively by the pre-existing suite) |
| `__version__` | `core/app.py:async_main` → `announce.announce_release(..., version)` — spied, exact patched string (`"9.9.9-verify"`) arrives | PASS |
| `run_due_reminders` | `core/jobs.py:minutely_tick`'s own call site, fired through a REAL scheduler-registered job (not a direct function call) | PASS |
| `render_weekly_review_charts` | `core/jobs.py:weekly_review_job`'s own call site, same real-scheduler-fired path | PASS |
| `parse_message` | `core/routing.py:handle_inbound_message`, via `main.py`'s wrapper, for an ordinary LLM-extraction-path message | PASS |
| `setup_logging` | `core/app.py:async_main`'s own call, with `config.app.log_level` | PASS |
| `handle_inbound_message` (whole function) | **Not applicable** — `core/routing.py:on_message` calls its own local `handle_inbound_message`, not `main.py`'s wrapper. I checked whether any pre-existing test relies on patching `main.handle_inbound_message` and expecting it to affect the live `on_message` pipeline: **none do** (grep across `tests/` for `setattr(main_module, "handle_inbound_message"`/`setattr("habit_assistant.main", "handle_inbound_message"` returns zero matches). Not a regression — it was never a supported pattern (`main.py`'s wrapper exists for `--dry-run`/direct test callers, not for `on_message`'s own internal call). |
| `*_COMMAND_DESCRIPTIONS` dicts (moved `main.py` → `core/app.py`) | N/A | Not a monkeypatch-compat risk — grepped the whole suite for any reference to these dicts via `main`/`main_module`; none exist. The dicts' *content and construction logic* were diffed byte-identical against the pre-Stage-2 file. |

**New-module-path patching** (tomorrow's tests patching `core.routing`/`core.jobs` directly, per the task brief): verified `monkeypatch.setattr(commands, "dispatch", spy)` (i.e. patching `core.commands.dispatch`, the attribute `core/routing.py` actually reads through its `from habit_assistant.core import ... commands` import) is observed by `on_message`; verified `unittest.mock.patch.object(core.jobs.checkins, "run_due_checkins", ...)` / `core.jobs.nudge.run_due_nudges` are the correct patch points for `core/jobs.py:minutely_tick`'s own module-level references, and that call **order** (reminders → checkins → nudge) is preserved.

## Luna's two self-caught bugs — confirmed dead

1. **Dispatch-once `None`-default no-op** (sentinel bug): re-tested via `commands.dispatch` call-count spies through three shapes — an ordinary log message (`"500ml"`), a recognized command (`"/help"`), both via `on_message` (exactly 1 call each) — and a button callback (`on_callback` with `"log:water:500"`, exactly **0** calls, confirming a tap never re-parses the payload as a command at all, which would be the inverse failure mode). All pass.
2. **Dropped `__main__` guard**: re-tested via a real `subprocess.run([sys.executable, "-m", "habit_assistant.main", "--dry-run", "500ml"], cwd=<scratch tmp_path>)` (not an in-process import, which would never exercise `if __name__ == "__main__"`) — exit code 0, correct structured output (`'category': 'water'`, `'value': 500.0`), a scratch `data/habits.db` created under the subprocess's own cwd with zero `logs` rows (dry-run writes nothing), and the real repo's `data/habits.db` untouched. PASS.

Additionally re-ran Stage 1's own per-tick-function isolation shape (from `TEST-refactor-s1.md`) through the relocated `core/jobs.py:minutely_tick`: a raising `run_due_reminders` does not prevent `checkins.run_due_checkins`/`nudge.run_due_nudges` from still running the same tick (each wrapped in its own try/except). PASS.

## Import discipline

- `test_refactor_s2_gaps.py`'s AST-based whole-`src/`-tree cycle detector (including imports inside function bodies, i.e. lazy/deferred imports still count) is a real, general-purpose cycle check — I mentally tried to break it with `core/confirmation.py` importing `core/routing.py` or `core/quicklog.py` (it doesn't; verified independently by a second, narrower AST check: `core/confirmation.py` imports nothing from `core.routing`, `core.quicklog`, or `habit_assistant.main`), and confirmed `core/quicklog.py` imports `confirmation`, never the reverse.
- `test_no_module_in_src_imports_main_except_main_itself` — re-verified: zero modules under `src/` import `habit_assistant.main`.
- Module line counts (AST `end_lineno`-max, matching IMPL's own methodology), independently re-derived: `main.py` **147** (< 150, hard AC6 requirement), `core/app.py` **574**, `core/jobs.py` **205**, `core/routing.py` **900**, `core/confirmation.py` **190** — all match IMPL-refactor-s2.md's table exactly.

## Scheduler jobs

All 6 job IDs (`minutely_tick`, `dashboard_day_rollover`, `weekly_review`, `daily_summary`, `grace_tick`, `wrapped_auto`) register with a `_FakeScheduler` when driven through the real `async_main`. Each was individually fired through its `core/app.py`-registered zero-arg forwarding closure with the corresponding `core/jobs.py` function monkeypatched to a spy, confirming the closure genuinely calls into `core/jobs.py` (not a leftover inline copy) — done explicitly for `minutely_tick` and implicitly (via the `run_due_reminders`/`render_weekly_review_charts` compat probes) for `weekly_review_job`; the remaining four (`dashboard_day_rollover_job`, `daily_summary_job`, `grace_tick`, `wrapped_auto_job`) were verified by the direct source diff against the pre-Stage-2 closures (identical bodies) rather than a second spy-based probe, since they take no overridable dependency to spy on.

## Sanctioned `test_riders.py` edit — review

The diff is exactly one dict-value change (`SRC_ROOT / "main.py": 2` → `SRC_ROOT / "core" / "jobs.py": 2`) plus an updated comment explaining why — no change to the sweep's regex, its file enumeration, or its assertion logic. Genuinely mechanical, matching a legitimately-moved internal structure per AC-G1's own carve-out.

**On the "hardcoded file list" gap concern**: the sweep is **not** hardcoded — it iterates `SRC_ROOT.rglob("*.py")` (a live recursive glob over the whole `src/habit_assistant/` tree, only excluding `channels/`), so it already scans `core/routing.py`, `core/jobs.py`, `core/confirmation.py`, and `core/app.py` (files that didn't exist before Stage 2) without any code change. I confirmed this two ways: (1) read the sweep's own source and asserted `'SRC_ROOT.rglob("*.py")' in source` as a regression guard, and (2) wrote a from-scratch, independent re-implementation of the same regex sweep (not calling the existing test's function) and confirmed it finds the identical `found == expected` result, with an explicit assertion that `core/routing.py`, `core/confirmation.py`, and `core/app.py` carry **zero** `disable_notification` call sites. No gap here — the concern in the task brief doesn't apply to this codebase's sweep.

## Startup smoke

- `python -c "import habit_assistant.main"` — clean, no output, no error.
- `python -m habit_assistant.main --dry-run "500ml"` via subprocess against a scratch cwd — exit 0, correct output (see "self-caught bugs" above).
- Full suite run twice consecutively: **202.79s** and **209.38s**, both `4335 passed, 1 skipped, 1 xfailed, 0 failed`.
- `data/habits.db` `LastWriteTime` unchanged (`2026-08-21 08:58:03 AM`) before and after the entire session; `git status --short data/habits.db` reports no modification.

## Regressions detected

None.

## Recommendation

**Ready to ship.** All Stage 2 ACs (AC6, AC7, AC8) plus the cross-cutting gates (AC-G1, AC-G2) pass. Two consecutive full-suite runs are clean (4335/0/1/1xf both times), the live DB was never touched, both of Luna's self-caught bugs stay fixed under adversarial re-testing, monkeypatch back-compat holds for every load-bearing symbol (including through real scheduler-fired jobs, not just direct calls), and the one sanctioned `test_riders.py` edit is confirmed mechanical with no coverage gap. One minor, non-blocking finding (two internal log lines dropped in `reparse_pending_unparsed`, invisible to users/tests) is noted for a future cheap fix but does not block v1.9.2.
