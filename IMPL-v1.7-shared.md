# Implementation — v1.7.0 shared surface (per-user custom habits, registry rewiring)

## Context: resumed from a killed session

A prior Luna was killed mid-build by a session restart. Her work was on disk, uncommitted. I read
`SPEC-v1.7.md` end-to-end, then verified — file by file, via `git diff` against the last commit
(`6dc4b60`, v1.6.0) — every item her interim report claimed as done, rather than trusting the report.
Everything she reported as done **was** done, correctly, and to a high standard (extensive docstrings
citing spec sections, comprehensive test coverage including anti-drift checks). I found one unrelated
pre-existing bug (below) and fixed it; otherwise this report documents her work plus that one fix.

## Files changed

| Path | Status | Description |
|---|---|---|
| `src/habit_assistant/storage/migrations.py` | modified | Migration 010: `CREATE TABLE user_habits` (additive), appended to `MIGRATIONS` |
| `src/habit_assistant/storage/db.py` | modified | 7 new `user_habits` CRUD methods: `add_user_habit`, `list_user_habits`, `get_user_habit`, `archive_user_habit`, `delete_user_habit`, `count_active_user_habits`, `count_logs_for` |
| `src/habit_assistant/core/habits.py` | modified | `HabitRegistry.for_user(config, db, user_id)` — base catalog + user's active custom habits (R-G1) |
| `src/habit_assistant/core/registry_provider.py` | new | `RegistryProvider` — process-global per-user registry cache with `.for_user()`/`.invalidate()`, fail-open on build error (R-G2) |
| `src/habit_assistant/core/commands.py` | modified | `CommandKind` gains `"addhabit"`/`"delhabit"` literals (skeleton only); `reserved_trigger_words()` — single-source trigger-word set for `habitdef`'s validation (R-V3) |
| `src/habit_assistant/core/reminders.py` | modified | `run_due_reminders` gains optional `registry_for: Callable[[str], HabitRegistry]` kwarg, resolved per user inside the fan-out loop |
| `src/habit_assistant/core/checkins.py` | modified | `run_due_checkins` — same `registry_for` addition |
| `src/habit_assistant/core/nudge.py` | modified | `run_due_nudges` — same `registry_for` addition (inside the existing per-user try/except, so a bad build fails open per-user) |
| `src/habit_assistant/main.py` | modified | Builds one process-global `RegistryProvider`; threads it through `on_message` (registry resolved once per inbound message, reused for dispatch + `handle_inbound_message`), `on_callback` (undo taps), `reparse_pending_unparsed` (per-row per-user registry), and all 6 scheduler fan-outs: `reminder_tick`/`checkin_tick`/`nudge_tick` (via `registry_for=provider.for_user`), the 00:00 dashboard rollover, the weekly-review job, and the daily-summary job |
| `src/habit_assistant/core/audit.py` | modified | `ACTIONS`/`Action` gain `habit_create`/`habit_archive`/`habit_delete` |
| `src/habit_assistant/core/audit_view.py` | modified | Localized-label mapping for the 3 new actions |
| `src/habit_assistant/config.py` | modified | `HabitsConfig` (`max_per_user: int = 20`), mounted as `Config.custom_habits` — **not** `Config.habits`, since that name is already the `[[habits]]` base-catalog list (see Known limitations) |
| `config.toml` | modified | Documents the (commented-out, default-20) `[custom_habits]` section |
| `src/habit_assistant/core/i18n.py` | modified | Audit-label catalog entries for the 3 new actions; `help_addhabit_cmd`/`help_delhabit_cmd` skeleton keys (EN+TH) |
| `src/habit_assistant/core/release_notes.py` | modified | `RELEASE_NOTES["1.7.0"]` (EN+TH) |
| `tests/test_registry_provider.py` | new | 8 tests: cache hit/miss, per-user invalidation isolation, lazy empty-start, fail-open + no-cache-on-failure, retry-after-failure |
| `tests/test_habits.py` | modified | 5 tests for `HabitRegistry.for_user`: byte-identical with no rows (AC-2/AC-5 gate), append-after-base, archived-excluded, per-user isolation, missing-aliases defaults to `{}` |
| `tests/test_commands.py` | modified | Migration count bump (9→10); `CommandKind` skeleton validity test; `reserved_trigger_words()` anti-drift test (calls real `dispatch()` for every claimed word against its expected outcome) + reserved-but-not-yet-live test + exclusion test |
| `tests/test_reminders.py`, `tests/test_checkins.py`, `tests/test_nudge.py` | modified | `registry_for` resolves per-user (spy-based proof) + `registry_for=None` falls back to old behavior, for each of the 3 fan-outs |
| `tests/test_config.py` | modified | `HabitsConfig`/`Config.custom_habits` default, override, TOML load, and no-collision-with-`[[habits]]` tests |
| `tests/test_audit.py` | modified | `ACTIONS` vocabulary assertion extended with the 3 new actions |
| `tests/test_migrations.py` | modified | Migration 010 rehearsal test (v9-shaped DB → v10, touches nothing existing, idempotent) + fresh-DB shape test + `user_habits` CRUD round-trip/isolation tests; schema-version literal bumps 9→10 throughout |
| `tests/test_heatmap.py`, `tests/test_history.py`, `tests/test_multi_habit_integration.py`, `tests/test_v12_integration.py`, `tests/test_v13_integration.py`, `tests/test_v16_integration.py` | modified | Mechanical `schema_version`/`MIGRATIONS` literal bumps 9→10 |
| `tests/test_v15_integration.py` | modified | Schema-version bump 9→10 (mechanical) **+ my own fix**: `test_current_pinned_version_announces_to_active_users_today` hard-codes `assert current_version == "1.5.0"`; the test's own docstring predicts it needs updating on every version bump. It was never updated when v1.6.0's Phase 6.5 bumped `__init__.py:__version__` to `"1.6.0"` (that bump happens *after* Vera's pre-release test run, so this self-referential assertion was never re-run against the new value until now). Confirmed pre-existing by running the test against the clean committed v1.6.0 HEAD (`git stash`) — it fails there too, unrelated to any v1.7 change. Updated the literal and docstring to `"1.6.0"`. |
| `PROGRESS.md` | modified | Status line tracking (prior Luna's session-restart note; not touched further by me) |

## How it works

`RegistryProvider(config, db)` is constructed once in `async_main`, right after `db`. Every place that
used to read the single global `registry` now has a choice: `main.py`'s live message/callback handlers
resolve `provider.for_user(chat_id)` fresh per event (cheap — cached after the first call); the three
minutely scheduler ticks (`reminder_tick`/`checkin_tick`/`nudge_tick`) pass `registry_for=provider.for_user`
so each fan-out's own per-user loop resolves the right registry; the dashboard rollover, weekly-review, and
daily-summary jobs call `provider.for_user(user_id)` directly inside their own `for user_id in
active_user_ids()` loops. `HabitRegistry.for_user(config, db, user_id)` builds the registry itself: base
`from_config(config)` habits first (verbatim), then a `Habit` per active `user_habits` row. A create/archive/
delete (owed to the `habitdef` track) calls `provider.invalidate(user_id)`, so the very next message or
scheduler tick rebuilds that one user's cache entry — no restart. Every `registry_for`/`provider` parameter
is additive and optional; every pre-v1.7 call site that omits it keeps using the single `registry` positional
unchanged, which is what keeps a no-custom-habits user byte-identical (AC-5).

## Smoke test done

1. Full pytest suite (see below).
2. Direct production-code smoke script (not mocked) — built a temp DB, constructed a real `RegistryProvider`,
   and exercised: no-rows byte-identical (`ids() == ['water','stretch','diary']`), cache staleness until
   `invalidate()`, rebuild-without-restart after invalidation, per-user isolation (a second user's cache
   entry untouched), `reserved_trigger_words()` contains `help`/`addhabit`/`เตือน` (51 words total), the 3 new
   audit actions are in `audit.ACTIONS`, and `RELEASE_NOTES["1.7.0"]` has both `en`/`th`. All assertions
   passed — command and full output captured in this session.
3. `import habit_assistant.main` — confirms the whole app (including the new `RegistryProvider` import and
   all the fan-out signature changes) still imports and wires up cleanly.
4. Did **not** touch the live `data/habits.db` — every check above used a fresh temp-directory DB.

## Maps to acceptance criteria (shared-surface scope: AC-1 through AC-8)

- **AC-1** (migration 010) → `storage/migrations.py:_migration_010_user_habits`; `tests/test_migrations.py::test_v9_shaped_db_migrates_to_v10_user_habits_touching_nothing_existing` + `test_fresh_db_has_user_habits_shape` (idempotent, additive-only, full suite green).
- **AC-2** (per-user registry) → `core/habits.py:HabitRegistry.for_user`; `tests/test_habits.py::test_for_user_with_no_rows_is_byte_identical_to_from_config`, `test_for_user_appends_active_custom_habits_after_the_base_catalog`, `test_for_user_excludes_archived_habits_from_the_registry`.
- **AC-3** (rebuild without restart) → `core/registry_provider.py:RegistryProvider`; `tests/test_registry_provider.py::test_for_user_caches_across_calls_even_after_a_direct_db_write`, `test_invalidate_is_scoped_to_exactly_one_user`. Also proven live in my smoke script.
- **AC-4** (per-user rewiring across all consumers) → `main.py` (`on_message`, `on_callback`, `reparse_pending_unparsed`, dashboard rollover, weekly-review job, daily-summary job) + `registry_for` kwarg on `core/{reminders,checkins,nudge}.py`; per-module tests prove `registry_for` is actually consulted (spy-based, not just signature presence) in `tests/test_{reminders,checkins,nudge}.py`.
- **AC-5** (owner/existing zero change) → the hard gate is the full suite: 3078 passed / 1 skipped / 1 xfailed (up from the 3039/0/1/1xf baseline — the delta is entirely new v1.7 shared-surface tests; no pre-existing test needed behavior changes, only the mechanical `schema_version` 9→10 literal bumps and one unrelated pre-existing bug fix, see Files changed). Every existing caller path that omits `registry_for`/uses `provider.for_user` on a user with zero `user_habits` rows is unchanged.
- **AC-6** (Thai-numeral/full-width lock) → no code change per spec (R-L2 is a normative lock on existing behavior); already covered by the pre-existing `units.py`/`preparse.py` test suite, unmodified and still green.
- **AC-7** (audit vocab) → `core/audit.py` `ACTIONS`, `core/audit_view.py` label map; `tests/test_audit.py::test_actions_matches_the_spec_vocabulary_exactly`.
- **AC-8** (release notes) → `core/release_notes.py:RELEASE_NOTES["1.7.0"]`; verified present with EN+TH in my smoke script. (Announcement delivery mechanism itself is unmodified v1.5 code, already tested.)

AC-H1–AC-H6 (`habitdef`) and AC-S1/AC-S2 (`sweep`) are **out of this report's scope** — owed to the two
parallel tracks per SPEC-v1.7.md §11. The shared surface provides everything they depend on:
`db.add_user_habit`/`list_user_habits`/`get_user_habit`/`archive_user_habit`/`delete_user_habit`/
`count_active_user_habits`/`count_logs_for`, `HabitRegistry.for_user`, `RegistryProvider.invalidate`,
`commands.reserved_trigger_words()`, `commands.CommandKind` literals `"addhabit"`/`"delhabit"`,
`config.custom_habits.max_per_user`, and the 3 audit actions.

## Known limitations

- **Naming deviation from SPEC.md §5's illustrative snippet**: the spec shows `class HabitsConfig(BaseModel):
  max_per_user: int = 20` with no explicit mount point, and §6 says "`config.py` + `config.toml` — `[habits]
  max_per_user`". `Config.habits` is already the existing `[[habits]]` array-of-tables (the base catalog) —
  TOML doesn't allow a `[[habits]]` array-of-tables and a sibling `[habits]` settings table under the same
  key in one document, and Pydantic can't have two fields of different shapes share one name either. The
  prior Luna mounted it as `Config.custom_habits` / `[custom_habits]` instead — a deliberately disjoint name,
  documented in the `HabitsConfig` docstring. This is a naming-only deviation; the field, default (20), and
  behavior (R-V5's cap) are exactly as specced. Flagging it explicitly here per Archi's "push back through
  Archi if the stack is materially harder" instruction — I did not consider this worth re-litigating with
  Archi mid-flight since it's a pure naming collision with an obvious, narrowly-scoped resolution and no
  behavioral consequence; `habitdef`'s implementer just needs to reference `config.custom_habits.max_per_user`,
  which I've noted above.
- `core/habitdef.py`, the `/addhabit`/`/delhabit` matcher in `commands.py`, and their i18n copy are not yet
  written — that's the `habitdef` track's own scope, deliberately left as skeletons here (bare `CommandKind`
  literals, reserved words, a help-menu i18n stub) so the parallel track has no shared-file collision to
  resolve.
- `tests/test_custom_habit_sweep.py` (AC-S1/AC-S2) does not exist yet — that's the `sweep` track's own
  deliverable, run in parallel with `habitdef` per SPEC-v1.7.md §11.
- The `test_v15_integration.py` version-pin fix I made will need the same one-line update again at the next
  release (v1.8.0's Phase 6.5 bump) — this is a known, accepted pattern in this test (its own docstring says
  so), not something I've tried to eliminate structurally, since that's outside this session's scope.

## Iteration log

No Luna↔Vera round happened in this session — I resumed and completed the prior Luna's in-flight work
myself, verified it against `git diff`, ran the full suite, and fixed one pre-existing failure I found along
the way:

- **Failure**: `tests/test_v15_integration.py::test_current_pinned_version_announces_to_active_users_today`
  failed with `AssertionError: assert '1.6.0' == '1.5.0'`.
- **Root cause**: the test hard-codes the expected `__version__` string as documentation of "the exact
  post-release state this test relies on." It was last updated for the v1.5.0 release. v1.6.0's Phase 6.5
  version bump (already committed, `__init__.py` says `"1.6.0"`) never triggered a re-run/update of this
  literal, because the bump happens after Vera's pre-release test pass — a structural gap in the release
  choreography, not something introduced by v1.7 work. Confirmed pre-existing by reproducing on the clean
  committed v1.6.0 HEAD via `git stash`.
- **Fix**: updated the literal and docstring from `"1.5.0"` to `"1.6.0"` (mirroring the exact update the
  test's own docstring said would eventually be needed). Out of scope to redesign the test to stop being
  release-fragile — flagged as a known limitation above instead.

## Final test status

`PYTHONPATH=src .venv\Scripts\python.exe -m pytest -q`: **3078 passed, 1 skipped, 1 xfailed** (0 failed).
Baseline before v1.7 work was 3039/0/1/1xf; the 39-test delta is entirely new shared-surface tests added in
this pass (registry provider, `for_user`, `reserved_trigger_words`, migration 010, `registry_for` per-fan-out,
config, audit). Same skip/xfail set as baseline.
