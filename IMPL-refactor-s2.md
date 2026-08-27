# Implementation — Refactor Stage 2: main.py decomposition (v1.9.2)

## Files changed

| Path | Created/Modified | One-line description |
|---|---|---|
| `src/habit_assistant/main.py` | Modified (rewritten) | Thin shim: `setup_logging`/`build_arg_parser`/`main()` only, plus back-compat wrapper functions for `async_main`/`handle_inbound_message`/`reparse_pending_unparsed` and straight re-exports (`ordinal`, `_execute_snooze`). 143 lines. |
| `src/habit_assistant/core/app.py` | Created | `async_main`'s real body: config/secrets/db/llm/channel/provider bootstrap, CLI branches (`--seed`/`--dry-run`/`--migrate`/`--backup`/`--restore`/`--test-reminder`), startup announce + command-menu registration, all scheduler-job registration, the Telegram long-poll loop. |
| `src/habit_assistant/core/jobs.py` | Created | The 6 scheduler job bodies (`minutely_tick`, `dashboard_day_rollover_job`, `weekly_review_job`, `daily_summary_job`, `grace_tick`, `wrapped_auto_job`), as plain parameter-taking functions instead of closures. |
| `src/habit_assistant/core/routing.py` | Created | Inbound routing: `on_message`, `on_callback`, `handle_inbound_message`, `reparse_pending_unparsed`, and the command-executor/formatter helpers only these use (`_execute_undo`/`_execute_edit`/`_execute_snooze`/`_react_to_typed_log`/`_send_recovered_generic`). |
| `src/habit_assistant/core/confirmation.py` | Created | The cycle-free confirmation LEAF: `ordinal`, `generic_confirmation`, `confirmation_text` (water/stretch/diary/generic dispatch), `suffix` (milestone + broken-record + celebration-burst) — imported by both `core/routing.py` and `core/quicklog.py`, killing the byte-mirror (rule 11/AC8). |
| `src/habit_assistant/core/quicklog.py` | Modified | Dropped its private `_ordinal`/`_generic_confirmation` mirror; `_log_and_confirm` now calls `core/confirmation.py`'s `confirmation_text`/`suffix` directly. Net -76 lines. |
| `tests/test_riders.py` | Modified (mechanical, AC-G1-sanctioned) | `test_exactly_five_call_sites_pass_disable_notification_ticks_plus_v19_jobs`'s `expected` dict updated: the two `grace_tick`/`wrapped_auto_job` `disable_notification=` call sites moved from `main.py` to `core/jobs.py` — a file-location assertion on internal structure that legitimately moved, not an emitted string/DB row/PNG. |
| `tests/test_refactor_s2_gaps.py` | Created | Stage 2's own exit-bar checks with no pre-existing home: an AST-based import-cycle test across all of `src/`, a direct "no module imports `habit_assistant.main`" check (rule 9's own audit claim), a `main.py < 150 lines` (AST-verified) check, and a `commands.dispatch` runs-exactly-once-per-message proof via `on_message` (AC7). |

## How it works

`main.py` keeps its own module-level names (`load_config`, `load_secrets`, `setup_logging`, `AsyncIOScheduler`, `TelegramChannel`, `OllamaClient`, `HealthMonitor`, `run_due_reminders`, `render_weekly_review_charts`, `parse_message`, `__version__`) exactly as before, but no longer uses most of them itself — instead its `async_main`/`handle_inbound_message`/`reparse_pending_unparsed` are wrapper functions that read these names from `main.py`'s own current globals *at call time* and forward them explicitly into `core/app.py`/`core/routing.py`'s real implementations, which accept them as parameters rather than importing them directly. This is the whole trick: a test doing `monkeypatch.setattr(main_module, "load_config", fake)` then `main_module.async_main(args)` still works, because the wrapper — not the real implementation — is the thing whose globals got patched, and the wrapper always re-reads them fresh.

`on_message`/`on_callback` (formerly closures inside `async_main`) are now plain functions in `core/routing.py` taking `db`/`llm`/`channel`/`config`/`owner_chat_id`/`provider`/`scheduler`/`reminder_state`/`health_monitor` as explicit keyword parameters; `core/app.py` registers two small zero-arg closures that forward to them (the only closures left, pure argument-threading, no logic). The same pattern applies to every scheduler job via `core/jobs.py`.

`on_message` dispatches once (`commands.dispatch(text, user_registry)`) and threads that exact `Command | None` into `handle_inbound_message` via a new `command` parameter, defaulting to an internal sentinel (`_NOT_DISPATCHED`, not `None` — see "Iteration log" below) so every other caller (CLI `--dry-run`, every existing test) is unaffected and still gets a fresh dispatch.

`core/confirmation.py` holds the water/stretch/diary/generic confirmation-text builder and the milestone/record/burst suffix builder as pure(ish) functions taking already-resolved `habit`/`value`/`lang`/etc — no send, no dashboard refresh, no reaction (those side effects still differ between the typed-log path and the quick-log-tap path, so they stay in each caller). Both `core/routing.py:handle_inbound_message` and `core/quicklog.py:_log_and_confirm` call the SAME two functions now, instead of each carrying its own copy.

## Smoke test done

- `PYTHONPATH=src .venv/Scripts/python.exe -m pytest` (foreground, no `-n`): **4308 passed, 1 skipped, 1 xfailed** — the full suite (4306 baseline tests, unmodified except the one sanctioned `test_riders.py` edit) plus the 4 new Stage 2 gap tests, all green.
- `PYTHONPATH=src .venv/Scripts/python.exe -c "import habit_assistant.main"` — clean, no output, no error.
- `python -m habit_assistant.main --dry-run "500ml"` / `--dry-run "/help"` / `--dry-run "undo"`, run against a **scratch** `Config`/`Database` (never `config.toml`/`data/habits.db` — `main_module.load_config`/`load_secrets` swapped for a scratch-path `Config` + fake secrets before calling `async_main` directly, since the CLI itself has no `--config` override flag): all three produced correct output (`{'category': 'water', 'value': 500.0, ...}`, the full bilingual help text, `{'kind': 'undo', ...}`), and a scratch db file was created at the scratch path. `data/habits.db`'s `LastWriteTime` was checked before and after (unchanged, 2026-08-21) — the live db was never touched.
- `tests/test_backup.py`/`tests/test_cli.py` (which spawn the real `python -m habit_assistant.main --seed`/`--migrate`/`--backup`/`--restore` subprocess against a `tmp_path`-scoped cwd) — all 26 pass.
- Line counts (AST-verified, `end_lineno` max, matching SPEC-REFACTOR.md's own methodology):

| File | Lines (AST-verified) |
|---|---|
| `main.py` (before, Stage 1 baseline) | ~2505 per SPEC-REFACTOR.md §2 baseline citation; 2523 raw lines in the actual pre-Stage-2 file read at the start of this stage |
| `main.py` (after) | **147** |
| `core/app.py` (new) | 574 |
| `core/jobs.py` (new) | 205 |
| `core/routing.py` (new) | 900 |
| `core/confirmation.py` (new) | 190 |
| `core/quicklog.py` (before → after) | ~438 → 362 (-76, mirror deleted) |
| **Sum of the 5 main.py-lineage files** | **2016** (vs. 2523 before — the byte-mirror deletion + collapsing the water/stretch/diary/generic and milestone/record/burst duplication into shared calls nets a real line reduction, not just a relocation) |

## Symbol-compatibility map

Every symbol any test imports from, or monkeypatches on, `habit_assistant.main` (found via `grep -rn "monkeypatch.*main" tests/` and `grep -rn "from habit_assistant.main import" tests/`) — where it now actually lives, and how `main.py` keeps it reachable:

| Symbol on `habit_assistant.main` | Real home now | How `main.py` keeps it working |
|---|---|---|
| `load_config`, `load_secrets` | `habit_assistant.config` (unchanged) | Still imported directly into `main.py`; `async_main` wrapper reads them fresh at call time and forwards to `core/app.py:async_main` |
| `setup_logging` | `main.py` (unchanged — stays here per spec's own target layout) | Same as above — forwarded by value, not imported back from anywhere |
| `AsyncIOScheduler`, `TelegramChannel`, `OllamaClient`, `HealthMonitor` | 3rd-party / `channels.telegram` / `llm.ollama_client` / `core.health` (unchanged) | Still imported directly into `main.py`; forwarded into `core/app.py:async_main` as keyword params |
| `__version__` | `habit_assistant/__init__.py` (unchanged) | Still imported into `main.py`; forwarded as `async_main`'s `version=` param |
| `run_due_reminders` | `habit_assistant.core.reminders` (unchanged) | Still imported into `main.py`; forwarded into `core/app.py:async_main` → `core/jobs.py:minutely_tick`'s `run_due_reminders=` param |
| `render_weekly_review_charts` | `habit_assistant.core.review` (unchanged) | Still imported into `main.py`; forwarded into `core/app.py:async_main` → `core/jobs.py:weekly_review_job`'s `render_weekly_review_charts=` param |
| `parse_message` | `habit_assistant.core.parser` (unchanged) | Still imported into `main.py`; `handle_inbound_message`/`reparse_pending_unparsed` wrappers forward it explicitly as `core/routing.py`'s `parse_message=` param (default there is the SAME real function, for every caller that bypasses `main.py`'s wrapper) |
| `Config` | `habit_assistant.config` (unchanged) | Still imported directly (read-only access, e.g. `main_module.Config.model_validate(...)`; never monkeypatched) |
| `handle_inbound_message` | Body → `core/routing.py:handle_inbound_message` | `main.py` defines a same-named wrapper (`text, **kwargs`) that injects `parse_message` and forwards to `routing.handle_inbound_message` |
| `reparse_pending_unparsed` | Body → `core/routing.py:reparse_pending_unparsed` | Same pattern, explicit signature (not `**kwargs`, since the original had none) |
| `async_main` | Body → `core/app.py:async_main` | `main.py`'s `async_main(args)` forwards every late-bound dependency explicitly (see table above) |
| `ordinal` | Body → `core/confirmation.py:ordinal` | Straight re-export (`from ... import ordinal`) — never monkeypatched, no wrapper needed |
| `_execute_snooze` | Body → `core/routing.py:_execute_snooze` | Straight re-export — internally calls `send_reminder` (not monkeypatched), so no forwarding needed |
| `CLARIFYING_QUESTION`, `DEFERRED_ACK_MESSAGE`, `NOTHING_TO_UNDO_MESSAGE`, `NOTHING_TO_EDIT_MESSAGE` | Unchanged (were already dead constants pre-Stage-2, kept only for import compat) | Still computed in `main.py` exactly as before |
| `BUILTIN_IDS` (unused import, not exported by tests) | `habit_assistant.core.habits.BUILTIN_IDS` (unchanged) | No longer imported into `main.py` at all — nothing there ever used it, and no test imports it from `main` (only from `core.habits` directly); see "Known limitations" |

## Maps to acceptance criteria

- **AC6** (`main.py` < 150 lines; `handle_inbound_message`/`async_main`/jobs/confirmation formatter in their own modules; no import cycle) → `main.py` is 147 lines (AST-verified, `tests/test_refactor_s2_gaps.py::test_main_py_is_a_thin_entry_under_150_lines`); the four bodies live in `core/routing.py:handle_inbound_message`, `core/app.py:async_main`, `core/jobs.py` (6 job functions), `core/confirmation.py:confirmation_text`/`suffix`; no cycle, proven by `test_no_import_cycle_anywhere_in_src` (general AST-based cycle detector over the whole `src/` tree, including imports inside function bodies) and `test_no_module_in_src_imports_main_except_main_itself` (the specific rule-9 audit claim).
- **AC7** (`commands.dispatch` invoked once per message, not twice) → `core/routing.py:on_message` dispatches once and threads the `Command | None` into `handle_inbound_message` via the new `command` parameter; verified by `test_dispatch_called_exactly_once_per_message_via_on_message` (a `commands.dispatch` call-count spy driven through the real `on_message`).
- **AC8** (`core/quicklog.py` imports the confirmation formatter from `core/confirmation.py`; byte-mirror deleted; quicklog's byte-identical tests still pass) → `quicklog.py`'s own `_ordinal`/`_generic_confirmation` are gone; `_log_and_confirm` calls `confirmation.confirmation_text`/`confirmation.suffix`, the SAME functions `core/routing.py:handle_inbound_message` calls; `tests/test_quicklog.py`'s `test_byte_identical_*` suite (6 tests comparing the typed-log path against the tap path) passes unmodified — now genuinely proving both callers invoke the same code, not two independently-maintained copies.
- **AC-G1** (full suite green, unmodified except a legitimately-moved internal-structure assertion) → 4306 pre-existing tests all pass unmodified except `tests/test_riders.py`'s one file-location dict entry (`main.py` → `core/jobs.py` for the 2 `disable_notification=` call sites that physically moved there) — no test asserting on an emitted string, DB row, or PNG changed.
- **AC-G2** (byte-identical output probe) → covered structurally: every confirmation-text/suffix code path is either an unmodified copy relocated verbatim, or (water/stretch/diary/generic; milestone/record/burst) a line-for-line-verified consolidation into one shared call per case — see "Known limitations" for the one thing I did not independently re-verify byte-for-byte beyond the existing suite's own coverage.

## Known limitations

- **Byte-identical proof relies on the existing suite, not a fresh corpus.** I did not build a NEW fixed-corpus-of-inbound-messages → captured-`channel.send*`-bytes probe myself; I relied on the pre-existing 4306-test suite (which includes exactly this kind of assertion throughout `test_confirmations.py`, `test_bilingual_confirmations.py`, `test_quicklog.py`'s byte-identical suite, etc.) passing completely unmodified as the byte-identical evidence. If Vera wants an independent AC-G2 probe as a *new*, from-scratch artifact (rather than "the existing suite is that probe and it's green"), that's not yet built.
- **`main.py`'s dead `BUILTIN_IDS` import (rule 15/AC11) is already gone.** Rewriting `main.py` from scratch, I simply didn't re-add an import nothing in the new thin file needs — `BUILTIN_IDS` was already unused before Stage 2 (confirmed: no test imports it from `main`, only from `core.habits` directly). This incidentally satisfies half of Stage 4's AC11 a stage early; flagging it explicitly rather than silently letting Stage 4 discover it as a no-op.
- **`command`'s default is a private sentinel, not `None`.** `core/routing.py:handle_inbound_message`'s `command` parameter defaults to a module-private `_NOT_DISPATCHED` object, not `None` — required because `commands.dispatch` legitimately returns `None` for an ordinary habit message, so `None` can't mean both "dispatch yourself" and "here's the dispatch result, it's empty." This is an implementation detail invisible to every caller (nobody passes `command=` except `on_message` itself), but worth flagging since it deviates from the naive `Command | None = None` shape SPEC-REFACTOR.md's own §5 interface sketch shows.
- **Stage 3's own targets are untouched, as expected.** The 18-armed `handle_inbound_message` dispatch-kind chain, the 27-matcher `commands.dispatch` if-chain, and the EASY dedup clusters (language-pref copies, `_today*`, `ordinal`/`_ordinal` beyond the one just consolidated, Thai-alias builders, `week_days`) are all Stage 3 scope and were not touched here, per SPEC-REFACTOR.md §11's sequencing.

## Iteration log (self-caught during my own smoke testing, before handoff to Vera)

- **Failure:** `python -m habit_assistant.main --seed` (and the `test_cli.py`/`test_backup.py` subprocess-driven CLI tests) exited 0 but created no `data/habits.db` at all — silent no-op.
  **Root cause:** my rewritten `main.py` dropped the trailing `if __name__ == "__main__": main()` guard the original file had (I never read past line 2520 of the original 2523-line file in my initial full read, and the guard lived in the last 3 lines I never saw).
  **Fix:** added the guard back to the end of `main.py`.

- **Failure:** my own new `test_dispatch_called_exactly_once_per_message_via_on_message` test failed with `commands.dispatch` called twice (`['500ml', '500ml']`) for an ordinary (non-command) log message, even though `on_message` was already threading its dispatch result through.
  **Root cause:** `handle_inbound_message`'s new `command` parameter defaulted to `None`, and `commands.dispatch("500ml", registry)` legitimately RETURNS `None` (no matcher fires for a bare habit value) — so `on_message` correctly passed `command=None`, and `handle_inbound_message`'s `if command is None: command = commands.dispatch(...)` re-dispatched anyway, silently defeating AC7 for the single most common message shape (a plain log).
  **Fix:** replaced the `None` default with a private sentinel object (`_NOT_DISPATCHED`) so "caller didn't pass `command`" is distinguishable from "caller passed `command=None` on purpose."

- **Failure (Vera, TEST-refactor-s2.md, post-handoff residual):** two operator-log-only calls inside `reparse_pending_unparsed` were silently dropped in the move to `core/routing.py`: `logger.info("Re-parsing %d deferred message(s)", len(pending))` (right after the `if not pending: return` guard, before the loop) and `logger.warning("Deferred message id=%s still unparseable after Ollama recovery; left as 'unparsed': %r", row["id"], text)` (inside the loop's `if habit is None:` branch, before its `continue`). No test caught this because nothing asserted on these specific log lines/logger — a real behavior gap (operator visibility into stuck-unparseable rows), just not a user-facing or test-covered one.
  **Root cause:** transcribing `reparse_pending_unparsed`'s body from the original `main.py` into `core/routing.py`, I dropped both `logger.*` calls and never added a `logger` to the new module at all (verified: `core/routing.py` had no `import logging`/`logger = ...` prior to this fix) — a plain copy-paste omission, not a deliberate simplification.
  **Fix:** restored both lines verbatim (confirmed byte-for-byte against `git show v1.9.1:src/habit_assistant/main.py`, lines 1534 and 1548-1552) at their original positions inside `core/routing.py:reparse_pending_unparsed`, and added `import logging` + `logger = logging.getLogger(__name__)` to `core/routing.py` (matching the `logging.getLogger(__name__)` convention already used in the other three new Stage 2 modules — `core/app.py`, `core/jobs.py`, `core/confirmation.py` doesn't need one). No test asserts on this logger's name or these exact messages (confirmed via `grep -rn "Re-parsing\|still unparseable" tests/*.py` — only one unrelated behavioral assertion in `test_resilience.py`), so this is a pure operator-log restoration with zero risk to any existing assertion. Verified: `pytest tests/test_resilience.py tests/test_refactor_s2_gaps.py tests/test_refactor_s2_verify.py -q` → 48 passed, 0 failed; full suite re-run → 4335 passed / 1 skipped / 1 xfailed, 0 failed.
