# Implementation — v1.3.0 Audit log (shared surface)

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/core/audit.py` | Created | `record(...)` — the single, fail-open audit recorder (R-W1/R-W2); `ACTIONS`/`SOURCES` closed-vocabulary constants; `_stringify_value` (the R-W1 value-shaping rule) |
| `src/habit_assistant/storage/migrations.py` | Modified | `_migration_007_audit_log` — additive-only `audit_log` table + `idx_audit_ts`/`idx_audit_user`, appended to `MIGRATIONS` (now length 7) |
| `src/habit_assistant/storage/db.py` | Modified | `insert_audit`, `recent_audit` (newest-first via `ORDER BY id DESC`), `prune_audit` (returns rows-deleted count); imports `AuditEntry` |
| `src/habit_assistant/storage/models.py` | Modified | `AuditEntry` dataclass (field order matches SPEC-v1.3.md §5 exactly) |
| `src/habit_assistant/config.py` | Modified | `AuditConfig` (`retention_days: int = 365`, validated `>= 0`) + `Config.audit: AuditConfig = AuditConfig()` |
| `config.toml` | Modified | Commented `[audit] retention_days = 365` documentation block (no schema/value change — 365 is already the Python-side default, same convention as every prior version's config.toml note) |
| `src/habit_assistant/main.py` | Modified | `async_main`'s real-service startup path now calls `db.prune_audit(cutoff)` once, right after `attribute_legacy_to_owner`, skipped entirely when `retention_days == 0` |
| `tests/test_audit.py` | Created | 22 tests: `ACTIONS`/`SOURCES` vocabulary, `record()` happy path + value-shaping (every type in R-W1's rule), fail-open (AC-A2 — DB-layer failure, and a pathological value whose `__str__` itself raises), newest-first ordering, injectable clock, and the startup-prune wiring (AC-R1, both `retention_days=365` and `=0`) |
| `tests/test_migrations.py` | Modified | 6 stale `schema_version == 6` assertions bumped to `7` (mechanical, migration 007 pushes the latest version forward by one, same pattern as v1.2's own `5`→`6` bump); added a dedicated migration-007 section: `test_v6_shaped_db_migrates_to_v7_audit_log_touching_nothing_existing`, `test_fresh_db_has_audit_log_table_with_expected_shape`, `test_insert_recent_and_prune_audit_round_trip` |
| `tests/test_commands.py` | Modified | `test_fresh_db_migrates_to_schema_version_6` → renamed/bumped to `..._7` (the file's own established "pinned regression guard, renamed on every migration bump" convention) |
| `tests/test_multi_habit_integration.py` | Modified | 1 stale `schema_version == 6` assertion bumped to `7` |
| `tests/test_v12_integration.py` | Modified | 1 stale `schema_version == 6` assertion bumped to `7` |

I did **not** touch `core/commands.py` or `core/i18n.py` — SPEC-v1.3.md §11 assigns both exclusively to `audit-view` (its own file-ownership table, and its own explicit text: *"`core/i18n.py` and `core/commands.py` are touched only by `audit-view`... so there is no cross-module collision on those shared files"*). Unlike v1.2 (three parallel modules colliding on `commands.py`/`i18n.py`, requiring the shared surface to pre-stub the `CommandKind` enum), v1.3 has only two parallel modules and the spec is explicit that only one of them (`audit-view`) ever touches those two files — so there is nothing for the shared surface to pre-stub there. Flagging this explicitly since the dispatch's item 6 asked me to check what §11 assigns to the shared surface for this; the answer, per the spec's own file-ownership table (not the paraphrased dispatch note), is "nothing."

## How it works

Migration 007 is the first migration since 001 that is **unconditionally, structurally additive** — one `CREATE TABLE IF NOT EXISTS` + two `CREATE INDEX IF NOT EXISTS` statements, no `ALTER`/`DROP` on any existing table, so AC-A1 ("touches no existing table/row") is true by construction, not by careful data preservation the way migration 006's `habit_targets` rebuild had to be. `core/audit.py:record` is the single writer of `audit_log` rows: every capture site (landed by the two parallel modules, and `main.py`'s own `_execute_edit` at integration) calls it with plain Python values it already has in hand — a `float` goal, a `list[str]` of times, a `str` status — and `record` itself handles turning that into a `str | None` via `_stringify_value` (numbers → `"{:g}"`, `list`/`dict` → `json.dumps`, `None` → `NULL`, everything else → `str()`) before constructing an `AuditEntry` and calling `db.insert_audit`. The entire body of `record` is wrapped in one `try/except Exception: logger.exception(...)` — not just the DB call — so a pathological caller-supplied value whose own `__str__` raises is caught too, not only a DB-layer failure; this is what makes the function "structurally hard to misuse" the way the coordinator asked: there is no code path inside `record` that can propagate an exception to a caller, so a capture site never needs its own try/except around the call. Retention is a single `if config.audit.retention_days > 0: db.prune_audit(cutoff)` at the top of `async_main`'s real-service startup path (right after `attribute_legacy_to_owner`, the other "once, at process start" housekeeping call over the same `db`) — `retention_days = 0` skips the cutoff computation and the DELETE entirely rather than computing a cutoff that would prune nothing anyway, and the prune itself is never audited (R-W3's own explicit carve-out — no `record` call at that site).

## The exact recorder contract (for `audit-capture` and `audit-view`)

```python
# src/habit_assistant/core/audit.py
from habit_assistant.core import audit

audit.ACTIONS  # tuple[str, ...] — the closed action vocabulary (13 values, exact spelling below)
audit.SOURCES  # tuple[str, ...] — ("command", "nl", "button", "admin")

audit.record(
    db,                          # positional, required — the Database instance
    *,
    actor: str,                  # required — who performed the action (the row's user_id)
    action: str,                 # required — one of audit.ACTIONS
    source: str,                 # required — one of audit.SOURCES
    entity: str | None = None,           # a habit id for habit-scoped actions; None otherwise
    old_value: object = None,            # plain Python value (float/list/dict/str/None) — DO NOT pre-stringify
    new_value: object = None,            # same
    target_user_id: str | None = None,   # admin actions done TO another chat; None for a self-action
    clock=datetime.now,                  # injectable for tests; every production caller omits it
) -> None                        # ALWAYS None. Never raises. The return value carries no signal —
                                  # do not branch on it; call record() AFTER your own write, ignore the call entirely.
```

**Calling convention (R-C1/R-C5):** call `record(...)` immediately after your own successful DB write, using the old/new values you already read/computed for your own reply text — never build an `AuditEntry` yourselves, never call `db.insert_audit` directly, never wrap the call in your own try/except (there is nothing to catch). `ACTIONS` = `("undo", "edit", "target_set", "target_clear", "remind_set", "remind_off", "remind_default", "lang_set", "quiet_set", "quiet_off", "user_approve", "user_block", "user_pending")`.

## Smoke test done

1. Full suite: `.venv\Scripts\python.exe -m pytest -q` → **1350 passed, 0 failed, 1 skipped** (baseline before this pass: 1325 passed/1 skipped, matching the coordinator's stated baseline exactly, independently re-confirmed by my own run before starting). The 25-test delta is exactly this pass's own additions (22 in `tests/test_audit.py` + 3 in `tests/test_migrations.py`'s new migration-007 section); zero behavior change anywhere else (AC-A3) — every fix beyond those additions was a stale hardcoded `schema_version == 6` literal, not a behavior change.
2. Ad hoc smoke script (not committed, deleted after use, run via `.venv\Scripts\python.exe`, scratch `tempfile.mkdtemp()` SQLite path — never `data/habits.db`), driving `Database`/`core/audit.py:record`/the three new db accessors directly:
   ```
   schema version: 0 -> 7
   recent_audit count: 3
   {'id': 3, 'action': 'remind_set', 'old_value': None, 'new_value': '["08:00", "12:00"]', ...}
   {'id': 2, 'action': 'lang_set', 'old_value': 'auto', 'new_value': 'th', ...}
   {'id': 1, 'action': 'target_set', 'old_value': '2500', 'new_value': '2000', ...}
   fail-open OK: record() did not raise                    <- forced db.insert_audit to raise RuntimeError
   pruned: 1
   remaining after prune: 3
   re-open schema version: 7 -> 7                            <- idempotent
   ALL SMOKE CHECKS PASSED
   ```
3. Verified `config.toml` still parses cleanly (`tomllib.load`) and `Config()`'s new `audit` field defaults/validates correctly (`retention_days=365` default, `0` accepted, `-1` rejected with a `ValidationError`) directly via the Python REPL before writing any test for it.
4. Never ran the app, `--seed`, `--dry-run`, or any test against `data/habits.db`; the live Task Scheduler service was not stopped, started, or otherwise touched.

## Maps to acceptance criteria

Shared-surface-owned ACs (4, per SPEC-v1.3.md §11's ownership table):

- **AC-A1** (migration additive, idempotent, touches nothing existing) → `storage/migrations.py:_migration_007_audit_log`; `tests/test_migrations.py::test_v6_shaped_db_migrates_to_v7_audit_log_touching_nothing_existing` (a v6-shaped DB with real seeded `users`/`logs`/`habit_targets` rows, byte-for-byte unchanged after migrating; reopen applies nothing further), `::test_fresh_db_has_audit_log_table_with_expected_shape`.
- **AC-A2** (fail-open) → `core/audit.py:record`'s single all-encompassing `try/except`; `tests/test_audit.py::test_record_fail_open_when_insert_audit_raises`, `::test_record_fail_open_logs_the_exception`, `::test_record_fail_open_when_value_stringification_itself_raises` (the value-shaping step, not just the DB call, proving the ENTIRE body is protected).
- **AC-A3** (regression gate, full suite byte-identical + green) → the full 1350-test run above; every one of the pre-existing 1325 tests passes unmodified in behavior (only 8 stale hardcoded-version-number assertions needed a literal bump, zero behavioral changes to any of them).
- **AC-R1** (retention) → `main.py`'s startup `db.prune_audit(cutoff)` call; `tests/test_audit.py::test_startup_prunes_audit_rows_older_than_retention_days` (365-day default, old row pruned/recent row kept, through the REAL `async_main`), `::test_startup_prunes_nothing_when_retention_days_is_zero`.

Not owned by this pass (explicitly deferred to the two parallel modules and the later integration step per SPEC-v1.3.md §11): AC-C1, AC-C3, AC-C4, AC-C5, AC-C6, AC-P1 (`audit-capture`); AC-V1, AC-V2 (`audit-view`); AC-C2 (edit — recorded in `main.py`, integration), AC-C7 (not-audited property, integration), AC-V3 (owner-only routing + menu-hidden, integration).

## Known limitations

- **`core/audit.py` has no `main.py` caller yet beyond the startup prune.** No capture site calls `audit.record` yet — that's `audit-capture`'s own job, landing after this pass, per SPEC-v1.3.md §11. `main.py`'s `_execute_edit` (the one capture site the spec explicitly assigns to the shared-surface/integration pass rather than either parallel module, since it's `main.py`'s own function) is also **not yet wired** — recording the `edit` action (AC-C2) is explicitly an *integration-step* task in §11 ("record the edit path in `_execute_edit`"), not this shared-surface pass's.
- **No `/audit` command exists yet.** `commands.py`/`i18n.py` are untouched (see "Files changed" above) — `audit-view` owns adding the `"audit"` `CommandKind` and the `/audit` copy. Until that module lands, nothing in production ever reads `audit_log` back out.
- **`Action`/`Source` are typed as `Literal[...]` in `core/audit.py` but the two runtime constants (`ACTIONS`/`SOURCES`) are plain tuples, not an enum.** A capture site that passes a typo'd string (e.g. `action="target_sett"`) will not fail at import time or via static typing alone unless the caller's own type checker enforces the `Literal`; at runtime it would simply write a row with that (wrong) string — `record` itself does not validate `action`/`source` against the vocabulary (R-W1 doesn't ask it to, and validating would add a failure mode to a function whose entire design goal is having none). Recommend `audit-capture`/`audit-view` import `ACTIONS`/`SOURCES` directly (`action=audit.ACTIONS[i]` or a module-level constant reference) rather than typing the string literal by hand, to get a real `AttributeError` on a typo instead of a silently wrong row.
- **`prune_audit`'s cutoff comparison is a plain string `<` on `ts`.** Correct today because every `ts` this codebase ever writes is a fixed-width `YYYY-MM-DDTHH:MM:SS` ISO8601 string (lexicographic order == chronological order, the same assumption `logs_between`'s own filters already rely on) — flagging only because it would silently misbehave if a future caller ever wrote a differently-shaped timestamp into this column, which nothing in this codebase does.

## Iteration log

No Vera round yet — this is the initial hand-off. No test from any other module needed changing; the 8 mechanical version-number fixes were all in files this pass itself is responsible for keeping accurate (pinned regression guards whose whole purpose is to catch exactly this class of drift).
