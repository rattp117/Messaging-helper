# Implementation — v1.8.0 `routines` module (habit stacks)

## Files changed

| Path | Status | Description |
|---|---|---|
| `src/habit_assistant/core/routines.py` | new | `execute_routine` (create/list/run/delete dispatch), `handle_routine_callback` (`routine:run:<name>` tap), plus private helpers (`_create`/`_render_list`/`_run`/`_delete`, `_resolve_item`, `_item_display`) |
| `src/habit_assistant/storage/migrations.py` | modified | `_migration_011_routines` (additive `routines` + `routine_items` tables, idempotent, stamps 11); appended to `MIGRATIONS` |
| `src/habit_assistant/storage/db.py` | modified | `add_routine`/`list_routines`/`get_routine`/`delete_routine`/`count_routines` appended after the `audit_log` section (disjoint region) |
| `src/habit_assistant/core/commands.py` | modified | `Command` gains `routine_action`/`routine_name`/`routine_items`; `_match_routine` + `_parse_routine_items` (disjoint block); one new `dispatch()` branch |
| `src/habit_assistant/core/i18n.py` | modified | 22 `routine_*` catalog entries (EN+TH) filled into the reserved key-block skeleton |
| `tests/test_routines.py` | new | 43 tests covering AC-B1 through AC-B7 |
| `tests/test_commands.py`, `tests/test_heatmap.py`, `tests/test_history.py` | modified | Bumped pinned `len(MIGRATIONS)`/`schema_version` literal guards from 10 → 11 (self-referential staleness caused directly by adding migration 011 — see Iteration log) |
| `tests/test_migrations.py`, `tests/test_multi_habit_integration.py`, `tests/test_v12_integration.py`, `tests/test_v13_integration.py`, `tests/test_v15_integration.py`, `tests/test_v16_integration.py` | modified | Same mechanical `schema_version`/`schema_version_before` literal bump (10 → 11); no behavior change |

## How it works

`core/commands.py:_match_routine` recognizes four shapes — bare `/routine` (list), `/routine <name>` (run), `/routine <name> = <habit> <val>[, ...]` (create), `/routine delete <name>` (delete) — plus a Thai alias `กิจวัตร` for create/run/delete, anchored on the routine name's own `^[a-z0-9_]+$` id-shape (not a registry lookup, since routine names are per-user DB state this dispatch layer can't see) so ordinary Thai prose containing "กิจวัตร" can never misfire. `core/routines.py:execute_routine` is the single dispatch target: it resolves habit tokens/values against the ACTING user's own per-user registry (`provider.for_user`), validates per R-R1, and either returns reply text for the caller to send (create/run/delete) or sends the list view itself via `channel.send_actionable` (so it can attach one run-button per routine) and returns `None`. `run` builds a `LogEntry` per valid item for today, calls `records.update_on_log` with its return discarded (no celebration lines), refreshes the dashboard exactly once (skipped entirely when nothing logged), and records one fail-open `routine_run` audit row. `handle_routine_callback` parses `routine:run:<name>`, builds a synthetic `Command`, and delegates straight into `execute_routine`'s run branch — isolation falls out for free because `db.get_routine`/`db.delete_routine` are scoped to the tapping `user_id` by construction, so a name owned by someone else resolves to the same friendly "not found" reply a nonexistent name gets.

## Smoke test done

1. `PYTHONPATH=src .venv\Scripts\python.exe -m pytest tests\test_routines.py -q` → **43 passed**.
2. Full suite: `PYTHONPATH=src .venv\Scripts\python.exe -m pytest -q` → **3672 passed, 0 failed, 1 skipped, 1 xfailed** (baseline was 3397/0/1/1xf before any v1.8 module landed; this run also includes the other three parallel Lunas' modules, all green).
3. Direct end-to-end smoke script (`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe smoke_routines.py`, real temp-dir `Database`, real `RegistryProvider`, no mocks) — exercised the full create → list → run → delete cycle in both English and Thai, plus a re-run after delete confirming "not found":
   ```
   >>> '/routine morning = water 500, stretch 10' -> kind=routine
       reply: ✅ Saved routine "morning": 500 ml water, 10 min stretch. Run it with /routine morning.
   >>> '/routine' -> kind=routine
   [send_actionable:u1] 📋 Your routines:
   • "morning": 500 ml water, 10 min stretch buttons=[('▶️ morning', 'routine:run:morning')]
   >>> '/routine morning' -> kind=routine
       reply: ▶️ morning — logged 500 ml water, 10 min stretch (2 of 2).
   >>> '/routine delete morning' -> kind=routine
       reply: 🗑️ Deleted routine "morning".
   >>> '/routine morning' -> kind=routine
       reply: 🤔 No routine named "morning". Use /routine to see yours.
   >>> 'กิจวัตร evening = water 300' -> kind=routine
       reply: ✅ Saved routine "evening": 300 ml water. Run it with /routine evening.
   >>> 'กิจวัตร evening' -> kind=routine
       reply: ▶️ evening — logged 300 ml water (1 of 1).
   >>> 'กิจวัตร evening ลบ' -> kind=routine
       reply: 🗑️ Deleted routine "evening".
   ```
4. Did **not** touch the live `data/habits.db` — every check above used a fresh temp-directory DB.

## Maps to acceptance criteria

- **AC-B1** (create + validation) → `core/routines.py:_create`/`_resolve_item`; `tests/test_routines.py::test_execute_routine_create_success_inserts_and_confirms`, `..._invalid_name_no_write`, `..._duplicate_name_no_write`, `..._empty_items_shape_no_write`, `..._unknown_habit_no_write`, `..._unparseable_value_no_write`, `..._cap_reached_no_write`.
- **AC-B2** (list) → `core/routines.py:_render_list`; `test_execute_routine_list_empty`, `test_execute_routine_list_shows_items_and_one_run_button_each`, `test_execute_routine_list_is_per_user`.
- **AC-B3** (run) → `core/routines.py:_run`; `test_execute_routine_run_logs_items_one_summary_one_refresh`, `test_execute_routine_run_skips_archived_item_and_notes_it`, `test_execute_routine_run_all_invalid_no_dashboard_churn`, `test_execute_routine_run_not_found`, `test_execute_routine_run_suppresses_celebration_but_updates_records`.
- **AC-B4** (delete) → `core/routines.py:_delete`; `test_execute_routine_delete_success`, `test_execute_routine_delete_not_found_no_write`.
- **AC-B5** (isolation) → `storage/db.py:get_routine`/`delete_routine` (user-scoped queries) + `core/routines.py:handle_routine_callback`; `test_isolation_user_b_cannot_see_or_run_user_a_routine`, `test_handle_routine_callback_not_owned_is_friendly_noop`, `test_handle_routine_callback_runs_owned_routine`.
- **AC-B6** (migration 011) → `storage/migrations.py:_migration_011_routines`; `test_migration_011_creates_tables_idempotently`, `test_migration_011_touches_no_existing_data`, plus the full suite staying green (3672/0/1/1xf).
- **AC-B7** (zero-LLM) → structural: `core/routines.py` imports no `llm`/`ollama` module at all; `test_routines_module_never_imports_the_llm_client` (AST-based import check, not a substring scan of comments).

Dispatch-layer coverage (feeds every AC above): `test_routine_slash_create_parses_name_and_items`, `..._bare_is_list`, `..._run_parses_name`, `..._delete_parses_name`, `..._create_malformed_items_still_dispatches_with_none_items`, the three `test_routine_thai_*` tests, and `test_routine_adversarial_corpus_never_false_positives` (7-message parametrized corpus, zero-false-positive discipline on `กิจวัตร`/`routine`).

## Known limitations

- **Item display doesn't use per-habit decorative emoji** (the spec's own §3.3 illustrative examples show `💧500 ml`/`🧘10 min`). That emoji table (`REACTION_EMOJI`) is owned by the `quicklog` module (`core/reactions.py`), and SPEC-v1.8.md §9 explicitly calls out that routines must stay decoupled from quicklog ("not by injecting routine buttons into the `/log` amount keyboard, which would couple `quicklog`↔`routines`"). Routine confirmations therefore render type-generic (`"<value> <unit> <label>"`, one leading `▶️`/`✅`/`🗑️`/`📋` per message), matching this codebase's own existing generic-confirmation style (`main.py:_generic_confirmation`) rather than inventing a second, routines-only emoji map. Informational content (which habits, values, skip reasons) matches the spec exactly; only the decorative per-item icon is absent.
- **Boolean/text-habit routine items**: R-R1's literal text ("each value parses via the per-user unit lookup") only strictly applies to numeric/duration habits (`units.build_unit_lookup` only ever indexes those two types). R-R3's own run-time description explicitly covers boolean (`-> true`) and text (`-> skipped, can't carry free text`) items, which only makes sense if such items can exist at all — so I allow any habit type at creation, with numeric/duration values strictly validated (positive, unit-resolved) and boolean/text values accepted permissively (never re-parsed, since they're either always logged `true` or always skipped). Documented in `core/routines.py:_resolve_item`'s own docstring; covered by `test_execute_routine_create_allows_boolean_and_text_items`.
- **List-view button layout**: `Channel.send_actionable` (the existing, unmodified shared interface) puts every button in one row. For a user with many routines this is a long single row — an existing platform-interface limitation, not something this module's scope includes changing.
- **`main.py` integration is explicitly out of scope for this pass** (per dispatch instructions) — routing `/routine`'s `CommandKind` to `execute_routine` and the `routine:` callback prefix to `handle_routine_callback` is Archi's sequential integration step, not done here. Both entry points are designed to drop into that seam with no further changes (their signatures match SPEC-v1.8.md §5 exactly).

## Iteration log

No Luna↔Vera round happened yet (first pass, direct dispatch). While proving the exit bar ("full suite green"), adding migration 011 correctly bumped the total migration count from 10 to 11 — which broke several **pinned, self-referential regression guards** elsewhere in the suite that hardcode the expected total (`len(MIGRATIONS) == 10` / `schema_version == 10`), exactly the same class of staleness the v1.8 shared-surface Luna's own `IMPL-v1.8-shared.md` documented fixing for a version-literal test. These are a *direct, mechanical, expected* consequence of legitimately adding a new additive migration (every prior migration addition in this codebase's history required the same literal bump) — not a bug in my own code:

- **`tests/test_commands.py::test_fresh_db_migrates_to_schema_version_10`**, **`tests/test_heatmap.py::test_heatmap_adds_no_migration_of_its_own`**, **`tests/test_history.py::test_no_migration_was_added_for_history`**: each pins `len(MIGRATIONS) == 10` by design (their own docstrings say so, and document the exact same bump happening at v1.6/v1.7). Bumped to `11`, with each docstring's own "CHANGED (vX.Y.0)" convention extended for v1.8.0/migration 011.
- **`tests/test_migrations.py`, `tests/test_multi_habit_integration.py`, `tests/test_v12_integration.py`, `tests/test_v13_integration.py`, `tests/test_v15_integration.py`, `tests/test_v16_integration.py`**: each has one or more `db.schema_version == 10` / `schema_version_before == 10` migration-rehearsal assertions (upgrading a hand-built old-shaped DB and asserting it lands on the current head version). Mechanically bumped every occurrence to `11` — no other change, since these tests' own logic (rehearsing migrations 001-010 against synthetic old-shaped databases) is otherwise correct and unaffected by routines' own migration 011 being purely additive.
- **Incidental tooling note**: my first attempt at this bulk literal bump used PowerShell's `Set-Content -Encoding utf8`, which (per this session's own memory note on the UTF-8 BOM gotcha) silently prepended a BOM to all 9 touched files — `pytest`'s AST-based assertion rewriter chokes on a BOM-prefixed `.py` file in some code paths, and Thai-text-bearing files are especially exposed. Caught immediately (`xxd` showed `efbbbf` on every touched file) and fixed by stripping the first 3 bytes back out (`tail -c +4`) before re-running the suite — no content was lost, only the accidental BOM.

Neither the literal bumps nor the BOM fix touch any file owned by a parallel module (`quicklog`/`backfill`/`riders`), and none touch `main.py`.

One collection-time failure was observed and is **not mine to fix**: `tests/test_quicklog.py` had a `SyntaxError: 'await' outside async function` at one point during this session (another Luna's in-flight module file) — by the time of my final full-suite run it had resolved itself (the owning Luna fixed it independently), and the final run shows it green. Noted here per the dispatch instruction to report cross-module suite failures without touching another module's files.

## Final test status

`PYTHONPATH=src .venv\Scripts\python.exe -m pytest -q`: **3672 passed, 0 failed, 1 skipped, 1 xfailed** (full suite, all four v1.8 parallel modules included).
`routines`-only: `PYTHONPATH=src .venv\Scripts\python.exe -m pytest tests\test_routines.py -q`: **43 passed, 0 failed**.
