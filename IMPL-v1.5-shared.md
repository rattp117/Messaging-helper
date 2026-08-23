# Implementation — v1.5.0 shared surface (migration 008, units extraction, DND primitive + matrix fix, config)

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/storage/migrations.py` | Modified | Migration 008: additive `users.checkin_window` + `users.last_announced_version` (both `NULL`, no backfill). `MIGRATIONS` now length 8. |
| `src/habit_assistant/storage/db.py` | Modified | `get/set_checkin_window`, `get/set_last_announced_version` — upsert-on-write, storage-only (no interpretation), mirroring `get_reminder_times`/`set_user_quiet_hours`'s own convention. |
| `src/habit_assistant/core/units.py` | Created | `VALUE_RE`, `build_unit_lookup(registry)`, `resolve_unit(lookup, unit_lower)` — extracted byte-identical from `core/commands.py`. `_TARGET_VALUE_RE` (accepts a leading `-`) deliberately NOT extracted — spec's interface list names only `VALUE_RE`. |
| `src/habit_assistant/core/commands.py` | Modified | `_VALUE_RE = units.VALUE_RE`; `_build_unit_lookup`/`_resolve_unit` now aliases (`_build_unit_lookup = units.build_unit_lookup`) instead of owning the logic. Every call site unchanged. |
| `src/habit_assistant/core/reminders.py` | Modified | Added `in_dnd_now(db, config, chat_id, clock=datetime.now) -> bool` — the one shared per-user DND primitive, built on `effective_quiet_windows` + `_in_quiet_hours`. Removed `is_quiet_hours_now(config)` entirely (dead code once its one call site migrated; zero test references). |
| `src/habit_assistant/main.py` | Modified | `daily_summary_job`: replaced the old GLOBAL `is_quiet_hours_now(config)` pre-check with a PER-USER `in_dnd_now` check inside the fan-out loop (R-D2 fix). `weekly_review_job`: added a PER-USER `in_dnd_now` check inside its fan-out loop — this job previously had no DND check at all (R-D3 fix). |
| `src/habit_assistant/config.py` | Modified | `CheckinConfig` (`enabled: bool = False`, `window: str = "08:00-20:00"`), `Config.checkin` field; `OllamaConfig.probe_on_startup: bool = True`; `HealthConfig.interval_seconds` default `60.0` → `300.0`. |
| `config.toml` | Modified | Documentation-only: comments above `[ollama]`/`[health]` explaining the new gate/default; new `[checkin]` section (`enabled = false`, `window = "08:00-20:00"`, matching the code default, self-documenting per this file's own established style). The live pinned `[health] interval_seconds = 60` is left unchanged (AC-17: a pinned shorter value still works). |
| `tests/test_units.py` | Created | 15 tests: `VALUE_RE`/`build_unit_lookup`/`resolve_unit` direct unit tests. |
| `tests/test_dnd_matrix.py` | Created | 15 tests: `in_dnd_now` direct unit tests (AC groundwork) + AC-10 (daily-summary per-user DND) + AC-11 (weekly-review per-user DND) + AC-12 (ops sends never suppressed — structural + one live `HealthMonitor` check). |
| `tests/test_config.py` | Modified | 8 new tests appended: `[checkin]` defaults/override/toml-load, `probe_on_startup` default/toml-load, `health.interval_seconds` new default + pinned-override still works. |
| `tests/test_commands.py` | Modified | Mechanical: `len(MIGRATIONS) == 8`, `schema_version == 8` (was 7); test renamed to `..._schema_version_8`. |
| `tests/test_migrations.py` | Modified | Mechanical: ~10 "final version" assertions 7→8 (fixture "before" assertions at 3/4/5/6/7 left untouched); one test renamed; `users` exact-column-set check extended with the two new columns; **6 new tests** added for migration 008 itself (fresh-DB shape, v7→v8 cascade touching nothing existing, idempotency, get/set round trips for both new columns, nonexistent-user reads return `None`). |
| `tests/test_history.py` | Modified | Mechanical: `len(MIGRATIONS) == 8`, `db.schema_version == 8`. |
| `tests/test_multi_habit_integration.py` | Modified | Mechanical: `db.schema_version == 8`. |
| `tests/test_v12_integration.py` | Modified | Mechanical: `db.schema_version == 8`. |
| `tests/test_v13_integration.py` | Modified | Mechanical: `db.schema_version == 8`. |

## How it works

Migration 008 adds two nullable, un-backfilled columns to `users`; `db.py` gets thin get/set pairs for each, following the codebase's storage-only convention (no window-string parsing, no version-comparison logic — that's the `checkins`/`announce` modules' own job). `core/units.py` is a pure extraction: `commands.py` now imports it and aliases its old private names back onto the new public ones, so every existing call site is untouched and the byte-identical guard is the pre-existing test suite passing unmodified. `in_dnd_now` composes the existing `effective_quiet_windows` (per-user override → global config fallback) with the existing `_in_quiet_hours` (midnight-crossing aware) — it is the one shared DND check the three parallel modules and the two fixed jobs all call. `daily_summary_job`/`weekly_review_job` each now call `in_dnd_now(db, config, user_id)` (no explicit `clock=`, matching production's real-wall-clock intent) inside their per-user fan-out loop, `continue`-ing past a suppressed user instead of sending; an un-customized user (no override, empty global default) is unaffected, preserving v1.4 behavior exactly.

## Smoke test done

- `python -c "from habit_assistant.config import Config; c = Config(); print(c.checkin.enabled, c.checkin.window, c.ollama.probe_on_startup, c.health.interval_seconds)"` → `False 08:00-20:00 True 300.0`.
- `python -c "from habit_assistant.config import load_config; c = load_config(); print(c.checkin.enabled, c.ollama.probe_on_startup, c.health.interval_seconds)"` (against the repo's real `config.toml`) → `False True 60.0` — confirms the live pinned `interval_seconds = 60` is preserved unchanged while new fields load with their defaults.
- `python -c "import habit_assistant.main"` — clean import after the `in_dnd_now` rewiring (no `is_quiet_hours_now` reference left).
- Direct script: called `reminders.in_dnd_now(db, config, chat_id, clock=...)` with per-user override present/absent, global-fallback, and midnight-crossing windows — all four resolved correctly; also confirmed a raising `db.get_user` fails open (returns `False`, never raises).
- `pytest tests/test_units.py` → 15 passed.
- `pytest tests/test_commands.py tests/test_targets.py tests/test_target_nl.py tests/test_core_targets.py` → 208 passed (byte-identical extraction guard).
- `pytest tests/test_streaks.py tests/test_charts.py tests/test_garmin.py tests/test_reminders.py tests/test_v09_gaps.py tests/test_adaptive_reminders.py` → 155 passed (reminders/main-adjacent, post `in_dnd_now` wiring).
- `pytest tests/test_migrations.py` → 30 passed.
- `pytest tests/test_dnd_matrix.py` → 15 passed (direct `in_dnd_now` unit tests + AC-10/AC-11/AC-12).
- `pytest tests/test_config.py` → 37 passed (29 pre-existing + 8 new).
- `pytest tests/` (full suite) → **1643 passed, 0 failed, 1 skipped** in 114.40s. Reconciles exactly against the coordinator's stated 1599/0/1 baseline: +44 new tests this pass (15 `test_units.py` + 15 `test_dnd_matrix.py` + 8 `test_config.py` + 6 new in `test_migrations.py`), zero regressions, zero newly-skipped.

## Maps to acceptance criteria

Per SPEC-v1.5.md §11, this pass owns/verifies: **AC-1, AC-2, AC-19** (migration + extraction + regression), **AC-10, AC-11, AC-12** (DND matrix), **AC-17, AC-18** (health interval / probe gate). AC-16 (pre-parser Ollama-down wiring) is `preparse`'s own module — not covered here.

- **AC-1** (migration 008) → `storage/migrations.py:_migration_008_checkin_and_announce`; `tests/test_migrations.py::test_v7_shaped_db_migrates_to_v8_checkin_and_announce_touching_nothing_existing` + 5 sibling tests.
- **AC-2** (units extraction, byte-identical) → `core/units.py` + `core/commands.py`'s aliasing; guarded by the unmodified `tests/test_commands.py`/`test_targets.py`/`test_target_nl.py`/`test_core_targets.py` (208 passed) plus `tests/test_units.py`'s own direct coverage.
- **AC-10** (daily-summary per-user DND) → `main.py:daily_summary_job`; `tests/test_dnd_matrix.py::test_ac10_daily_summary_skips_a_user_in_their_own_dnd_window` + `test_ac10_uncustomized_user_summary_is_byte_identical_to_v14`.
- **AC-11** (weekly-review per-user DND) → `main.py:weekly_review_job`; `tests/test_dnd_matrix.py::test_ac11_weekly_review_suppresses_a_user_in_their_own_dnd_window` + `test_ac11_uncustomized_user_review_fires_exactly_as_before`.
- **AC-12** (ops sends never suppressed) → structural: neither `core/health.py` nor `core/access.py` call `in_dnd_now` (untouched by this pass) — `tests/test_dnd_matrix.py::test_ac12_health_monitor_never_calls_in_dnd_now`, `test_ac12_access_module_never_calls_in_dnd_now`, plus a live `HealthMonitor._alert` confirmation with the owner in an always-on DND window.
- **AC-17** (health interval) → `config.py:HealthConfig.interval_seconds` default 300; `tests/test_config.py::test_health_interval_seconds_default_raised_to_300` + `test_load_config_toml_can_pin_a_shorter_health_interval`. (The interval actually gating `HealthMonitor`'s poll loop, and the pinned-value-still-works DOWN→UP behavior, is `main.py`'s startup wiring — unchanged by this pass since it already reads `config.health.interval_seconds`; not re-verified end-to-end here, that's `preparse`/integration's own AC-17 close-out per §11 step 2.)
- **AC-18** (probe gate) → `config.py:OllamaConfig.probe_on_startup` default `True`; `tests/test_config.py::test_ollama_probe_on_startup_defaults_to_true` + `test_load_config_toml_can_disable_probe_on_startup`. Actually *gating* the startup probe call in `main.py` is explicitly Integration-order work (§11 step 1: "apply ... `probe_on_startup`") — this pass only lands the config surface the gate will read; the gate itself is out of shared-surface scope.
- **AC-19** (regression, 1599-test baseline stays green) → full-suite run, see Smoke test above.

## Known limitations

- `in_dnd_now`'s `clock` parameter is bound to `datetime.now` at function-definition time (Python default-argument semantics) — a caller that omits `clock` gets the real wall clock even under `monkeypatch.setattr("habit_assistant.core.reminders.datetime", ...)`, since that only affects code that references the module-level `datetime` name directly inside a function body, not an already-bound default. `main.py`'s two call sites correctly omit `clock` (these jobs should react to the real time). Tests that need determinism use explicit `clock=` overrides (direct `in_dnd_now` tests) or time-robust "always DND" / "never DND" window fixtures (the two-window full-day-coverage trick, `test_dnd_matrix.py`'s `_ALWAYS_DND_WINDOWS`) rather than freezing a specific real-clock value — mirrors the same testability note already on file for `send_reminder`'s own quiet-hours check.
- Per §11's own explicit ownership split, this pass does **not** touch `core/commands.py`'s `CommandKind`/`core/i18n.py` catalog — SPEC-v1.5.md §6/§11 assigns the `"checkin"` kind, the `/dnd` alias, and all checkin/dnd/help i18n keys exclusively to module `checkins` (no shared-surface skeleton needed; disjoint from `preparse`/`announce`).
- `[ollama] probe_on_startup` and `[health] interval_seconds`'s *effect* (actually skipping the startup probe; actually driving `HealthMonitor`'s poll cadence) is not wired into `main.py` by this pass — per SPEC-v1.5.md §11's Integration order step 1, that wiring happens after all three parallel modules land. This pass only lands the config surface (fields + defaults + config.toml documentation) the three modules and the integration step build on.
- `config.toml`'s live `[health] interval_seconds = 60` was deliberately left unchanged (not bumped to the new 300 default) — the dispatch specified "config.toml documentation comments only," and AC-17 explicitly requires a pinned shorter value to keep working, so this is exercised behavior, not an oversight.

## Contracts for the three parallel modules (`checkins` / `preparse` / `announce`)

- **`db.get_checkin_window(chat_id: str) -> str | None`** / **`db.set_checkin_window(chat_id: str, value: str | None) -> None`** — raw storage only. `None` = inherit config default; `"off"` = disabled; any other string is a raw `"HH:MM-HH:MM"` window. No validation, no parsing — `checkins.effective_checkin` interprets the value.
- **`db.get_last_announced_version(chat_id: str) -> str | None`** / **`db.set_last_announced_version(chat_id: str, version: str) -> None`** — raw storage only, `None` = never announced to this user. `announce.announce_release` owns comparison/gating logic.
- **`core/units.VALUE_RE`** (compiled regex, `num`/`unit` named groups, positive numbers only) / **`build_unit_lookup(registry: HabitRegistry) -> dict[str, tuple[str, float]]`** / **`resolve_unit(lookup, unit_lower: str) -> tuple[str, float] | None`** — pure functions, no I/O, safe for `preparse` to import directly alongside `core/commands.py`.
- **`reminders.in_dnd_now(db: Database, config: Config, chat_id: str, clock=datetime.now) -> bool`** — `True` iff `chat_id` is inside DND right now (per-user `quiet_hours_json` override, else `config.quiet_hours.windows`, midnight-crossing aware). Fails open (`False`) on any DB read error. Omit `clock` for real-time production call sites (as `main.py`'s two jobs do); pass it explicitly only in tests.
- **`config.CheckinConfig`**: `enabled: bool = False`, `window: str = "08:00-20:00"`, exposed as `Config.checkin`. `checkins.effective_checkin` is expected to read `config.checkin.enabled`/`.window` as the fallback for a user with `checkin_window is None`.
- **`config.OllamaConfig.probe_on_startup: bool = True`**, **`config.HealthConfig.interval_seconds: float = 300.0`** — both are plain config fields; reading and acting on them is `main.py`'s own Integration-order step (§11), not yet wired.

## Iteration log

No Vera round yet — first hand-off for this shared-surface pass.
