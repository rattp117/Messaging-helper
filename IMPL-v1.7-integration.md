# Implementation — v1.7.0 integration step (per-user custom habits: wiring `habitdef` into `main.py`)

## Files changed

| Path | Status | Description |
|---|---|---|
| `src/habit_assistant/main.py` | modified | Imports `habitdef`; new `ADDHABIT_COMMAND_DESCRIPTIONS`/`DELHABIT_COMMAND_DESCRIPTIONS`; `handle_inbound_message` gains an optional `provider: RegistryProvider \| None = None` param and a new `command.kind in ("addhabit", "delhabit")` routing branch (placed after `checkin`, before `dashboard`) that calls `core/habitdef.execute_addhabit`/`execute_delhabit`; `on_message` now passes its own process-global `provider` through to `handle_inbound_message`; `/addhabit`/`/delhabit` added to the `set_my_commands` public menu (16 commands total, was 14) |
| `src/habit_assistant/core/discoverability.py` | modified | `build_help_text` appends `help_addhabit_cmd`/`help_delhabit_cmd` (both already existed in `core/i18n.py` from the shared-surface pass, just not yet referenced) |
| `tests/test_v17_integration.py` | new | 9 end-to-end tests through the REAL `async_main`/`on_message` wiring (see below) |
| `tests/test_discoverability.py` | modified | `test_command_menu_registers_exactly_the_expected_commands_no_extras`'s expected set/docstring updated: 14 → 16 (adds `addhabit`/`delhabit`) |
| `tests/test_v12_integration.py` | modified | `test_command_menu_public_set_excludes_the_four_admin_only_commands`'s expected set updated the same way |
| `tests/test_v16_integration.py` | modified | `test_menu_has_exactly_14_public_commands_both_languages`'s count updated 14 → 16 (test name left as-is, per this file's own established "mechanical bump, name goes stale" convention — see its `test_v15_integration.py`-style comment) |

I did not touch `core/habitdef.py`, `core/commands.py`'s `/addhabit`/`/delhabit` matching, their i18n copy, `storage/*`, `core/registry_provider.py`, `core/habits.py`, `core/audit.py`/`audit_view.py`, or `config.py`/`config.toml` — all already in place per `IMPL-v1.7-shared.md` and `IMPL-v1.7-habitdef.md`. `tests/test_v17_isolation_sweep.py` (the `sweep` track's own file) also untouched.

## How it works

`main.py`'s `on_message` closure already resolved the acting user's per-user registry via the process-global `provider.for_user(chat_id)` (shared-surface work) and used it for its own pre-check `commands.dispatch()` call; the only missing piece was threading that same `provider` instance into `handle_inbound_message` (where the real per-kind routing lives) and adding the `addhabit`/`delhabit` branch there. That branch now does exactly what `target`/`checkin`/`dashboard` do: build a reply via the module's `execute_*` function and send it — `execute_addhabit` needs `provider` (to invalidate the cache, R-G2/AC-3) and a fresh `base_registry = HabitRegistry.from_config(config)` (for the R-G4 "no base shadowing" check); `execute_delhabit` needs only `provider`. Because `provider` is a new *optional* parameter on `handle_inbound_message` (default `None`), every pre-v1.7 caller (tests, the `--dry-run` CLI path) is unaffected — when omitted, the branch builds a one-off `RegistryProvider(config, db)` on the spot, which is correct for a single call but doesn't persist a cache across messages the way the real `on_message`-supplied provider does. `/habits` needed **zero** code change (as `habitdef`'s own report already found — `build_habits_overview` is registry-generic); `/help` needed two new lines using i18n keys the shared surface had already written but left unreferenced; the startup menu needed two more `(name, description)` tuples in the `command_menu` dict that's already built per-language from a chain of `..._COMMAND_DESCRIPTIONS` dicts (mirrored the existing `RECORDS_COMMAND_DESCRIPTIONS`/`TRENDS_COMMAND_DESCRIPTIONS` pattern exactly).

## Smoke test done

1. `python -c "import habit_assistant.main"` — clean.
2. Direct production-code script (not mocked, no `provider` passed — exercises the CLI/`--dry-run`-style fallback path): built a temp DB, called `handle_inbound_message("/addhabit id=smoke|type=numeric|en=smoke|unit=u|goal=5", ..., dry_run=True)` with no `provider` argument — printed the exact confirmation `✅ Added "smoke" (smoke) — numeric in u, goal 5/day. Log it like "20 u" or use /remind smoke.` and confirmed `db.get_user_habit("999", "smoke")` returned the written row. Proves the fallback `RegistryProvider(config, db)` construction inside the new branch works standalone.
3. Full pytest suite: **3332 passed, 0 failed, 1 skipped, 1 xfailed** (up from the pre-integration baseline of 3323/0/1/1xf — the 9-test delta is entirely `tests/test_v17_integration.py`; the three menu-count test edits changed assertions, not test counts).
4. `tests/test_v17_integration.py` run in isolation: 9/9 passed.
5. `python -m py_compile` on `main.py` and `discoverability.py`.
6. Did not touch `data/habits.db` — every check used a fresh temp-directory DB (`tmp_path` fixtures or `tempfile.mkdtemp()`).

## Maps to acceptance criteria (integration scope)

This pass doesn't own any AC directly (per SPEC-v1.7.md §11, AC-1 through AC-8 were verified during the shared-surface pass; AC-H1–AC-H6 by `habitdef`; AC-S1/AC-S2 by `sweep`). It closes the deliberately-deferred wiring gap both tracks flagged in their own "Known limitations":

- **`habitdef`'s "`main.py` is not wired up"** → closed: `command.kind in ("addhabit", "delhabit")` now routes to `core/habitdef.execute_addhabit`/`execute_delhabit` inside `handle_inbound_message`; `tests/test_v17_integration.py` exercises this through 7 different real-dispatch scenarios (create, no-restart-preparse, dashboard/records/habits/help pickup, reserved-word rejection ×2, hard-delete + id-reuse, archive + reserved-id, menu registration, AC-5 regression).
- **`habitdef`'s "help_addhabit_cmd/help_delhabit_cmd not yet referenced by build_help_text"** → closed: both lines now appended unconditionally (every user can always run both commands).
- **§11's own named integration scenarios** (re-quoted from the spec):
  - "A creates 'reading' [here: 'pages'] and logs/undoes/targets/reminds/reviews it while B sees zero trace" — the undo/target/remind/review depth is `sweep`'s own AC-S1 scope (already PASS, verified via direct `user_habits` seeding); this pass adds the missing *real-dispatch* half — A creates it via a genuine `/addhabit` message, logs it via a genuine follow-up message with **no restart in between** (`test_addhabit_end_to_end_no_restart_dashboard_records_habits_help_pick_it_up`), and B's `/habits`/`/help` show zero trace of it (`test_member_sees_zero_trace_of_owners_custom_habit`).
  - "a habit named 'help'/'เตือน' is rejected" → `test_addhabit_id_help_is_rejected_through_real_dispatch`, `test_addhabit_thai_label_เตือน_is_rejected_through_real_dispatch` — both through the real `/addhabit` message path, both assert no DB write.
  - "`/delhabit` archives a habit-with-history and hard-deletes an empty one" → `test_delhabit_hard_deletes_an_empty_habit_and_frees_the_id_through_real_dispatch`, `test_delhabit_archives_a_habit_with_history_and_reserves_its_id_through_real_dispatch` — both also prove the freed-vs-reserved id behavior on an immediate re-add.
  - "a Thai-numeral log preparses" → `test_thai_numeral_log_preparses_with_no_llm_through_the_real_wired_path` (re-checked end-to-end through the post-v1.7 routing, not just at the `units.py` unit level).
- **AC-5 byte-identical gate, re-checked at this pass's own surface** → `test_ac5_owner_with_no_custom_habits_is_still_byte_identical_through_real_dispatch` asserts the exact pre-v1.7 water confirmation string, through the same `handle_inbound_message` function the new `addhabit`/`delhabit` branch was just added next to.
- **Public menu / `/help` wiring (R-A2)** → `test_startup_menu_includes_addhabit_and_delhabit`; the three pre-existing menu-exact-set tests (`test_discoverability.py`, `test_v12_integration.py`, `test_v16_integration.py`) updated from 14→16 and now pass with the real menu contents.

## Known limitations

- The `alias=tok:mult,...` grammar and `/edithabit` remain out of scope per SPEC-v1.7.md §10 (unchanged from `habitdef`'s own report).
- `test_v16_integration.py`'s test function is still literally named `..._exactly_14_public_commands...` — left as-is (only the body/count changed) to keep the diff minimal, matching this codebase's own established precedent of leaving a stale name/docstring number in place once a prior release's own comment already documents the growth history inline (e.g. `test_v15_integration.py`'s version-pin literal, and this same file's own `dashboard`/`heatmap`/`records`/`trends` count-growth comments). Flagging for Archi/Vera in case a rename is preferred.
- No change was needed to `core/discoverability.py:build_habits_overview` (AC-H6) — already registry-generic, confirmed by `habitdef`'s own tests and re-confirmed here through the real dispatch path.

## Final test status

`PYTHONPATH=src .venv\Scripts\python.exe -m pytest -q`: **3332 passed, 1 skipped, 1 xfailed, 0 failed**.
Baseline at hand-off (both tracks PASS) was 3323/0/1/1xf; the 9-test delta is entirely `tests/test_v17_integration.py`. Same skip/xfail set as baseline. `python -c "import habit_assistant.main"` clean.
