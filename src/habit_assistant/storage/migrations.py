"""SQLite schema migration runner (ROADMAP.md v0.3.0 "Migrations &
Backup/Restore").

`PRAGMA user_version` drives an ordered list of migration functions. The
runner applies only the migrations above the DB's current user_version,
each inside its own transaction, and stamps the version forward as it
goes. Re-running on an already-migrated DB applies nothing (AC4.1). No
third-party migration framework -- Alembic is SQLAlchemy-bound, which this
project deliberately doesn't use; a small user_version runner is enough.

Migration 001 adopts the baseline v0.1.0/v0.2.0 schema via CREATE
TABLE/INDEX IF NOT EXISTS. That is deliberate: run against an existing
production DB that already has the `logs` table, it changes nothing --
it only stamps user_version=1 (AC4.2, "no-op-safe baseline adoption").
Every future migration must preserve that property: additive schema
changes only, never DROP/ALTER-destructive against existing columns/rows.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Callable

logger = logging.getLogger("habit_assistant")

Migration = Callable[[sqlite3.Connection], None]


def _migration_001_baseline(conn: sqlite3.Connection) -> None:
    """Baseline schema: the `logs` table + its (ts, category) index, exactly
    as shipped in v0.1.0/v0.2.0. IF NOT EXISTS makes this safe to apply
    against a DB that already has them."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS logs (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          ts          TEXT NOT NULL,
          category    TEXT NOT NULL,
          value_num   REAL,
          value_text  TEXT,
          raw_message TEXT NOT NULL,
          source      TEXT NOT NULL DEFAULT 'reply',
          created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts_cat ON logs(ts, category)")


def _migration_002_category_index(conn: sqlite3.Connection) -> None:
    """ROADMAP.md v0.4.0 "Runtime Resilience & Self-Monitoring": deferred
    messages (received while the LLM was unavailable) are persisted as
    ordinary `logs` rows with category='unparsed'. That's a data-level
    change only -- `category` has never had a CHECK constraint restricting
    its values, so no column change is needed to store it. This migration
    is purely additive: an index so `Database.pending_unparsed()` (the
    startup/recovery re-parse scan) doesn't table-scan as `logs` grows."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_category ON logs(category)")


def _migration_003_soft_delete(conn: sqlite3.Connection) -> None:
    """ROADMAP.md v0.5.0 "Command Layer & Edit/Undo": undo is soft-delete
    (Sophia's default, confirmed by Archi) so a mistaken "undo"/"ยกเลิก" is
    reversible and every log stays auditable (AC5.1, AC5.4). Additive-only:
    a new nullable column, default NULL for every existing row (nothing is
    retroactively marked deleted), plus an index so the `deleted_at IS
    NULL` filter added to every aggregation query in storage/db.py doesn't
    table-scan as `logs` grows."""
    conn.execute("ALTER TABLE logs ADD COLUMN deleted_at TEXT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_deleted_at ON logs(deleted_at)")


def _migration_004_habit_type(conn: sqlite3.Connection) -> None:
    """ROADMAP.md v0.7.0 "Multi-Habit Extensibility" (SPEC-v0.7.md §4 R11):
    additive-only, like every migration before it -- a new nullable
    column, then a one-time backfill of the three categories that existed
    before this version (`water`->'numeric', `stretch`->'duration',
    `diary`->'text'); any other category (notably 'unparsed', and any
    unrecognized historical category) is left NULL rather than guessed at.
    `value_num`/`value_text`/`category`/`ts` of every existing row are
    untouched -- they already sit in the right column/value (AC3/AC7.5)."""
    conn.execute("ALTER TABLE logs ADD COLUMN habit_type TEXT NULL")
    conn.execute(
        """
        UPDATE logs SET habit_type = CASE category
            WHEN 'water' THEN 'numeric'
            WHEN 'stretch' THEN 'duration'
            WHEN 'diary' THEN 'text'
            ELSE NULL
        END
        """
    )


def _migration_005_habit_targets(conn: sqlite3.Connection) -> None:
    """SPEC-v1.1.md "Undo menu + per-habit targets" (§4 R-T1): additive-only,
    like every migration before it -- a new, small override table, no
    ALTER/DROP on `logs`. `habit_id` is the primary key (one override row
    per habit, upserted by `Database.set_target`); `goal` is the daily
    target in the habit's base unit. `IF NOT EXISTS` makes re-running a
    no-op (AC12)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS habit_targets (
          habit_id   TEXT PRIMARY KEY,
          goal       REAL NOT NULL,
          updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
        """
    )


def _migration_006_multiuser(conn: sqlite3.Connection) -> None:
    """SPEC-v1.2.md "Multi-user support" (§4 R-M1): the ONE sanctioned break
    of the additive-only guarantee this project has held since migration
    001 -- `habit_targets` is rebuilt from a `habit_id`-only PK to a
    surrogate-id PK with `UNIQUE(user_id, habit_id)`, because a per-habit
    target now needs to vary per user, not just per habit. Every existing
    row survives the rebuild (copied across with `user_id = NULL` -- the
    migration runner only ever receives a bare `sqlite3.Connection`, never
    `.env`, so it cannot know who the owner is; that backfill is
    `Database.attribute_legacy_to_owner`'s job, R-M2, run once at startup).
    Everything else here is additive, like every migration before it:
    - `users`: one row per authorized/pending/blocked chat.
    - `logs.user_id` (nullable column + index): who a log row belongs to;
      NULL until R-M2 backfills it.
    - `user_reminder_times`: empty at creation, so every user (owner
      included) falls back to the global config reminder times until they
      run `/remind` -- v1.1 reminder behavior is preserved by construction
      (AC-M3/AC-S1), not by a special case.
    Idempotent the same way every migration here is: the runner only
    applies this once, guarded by `PRAGMA user_version` (AC-M1)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
          chat_id                 TEXT PRIMARY KEY,
          role                    TEXT NOT NULL DEFAULT 'member',
          status                  TEXT NOT NULL DEFAULT 'pending',
          display_name            TEXT,
          language_pref           TEXT NOT NULL DEFAULT 'auto',
          quiet_hours_json        TEXT,
          snooze_default_minutes  INTEGER,
          created_at              TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
        """
    )

    conn.execute("ALTER TABLE logs ADD COLUMN user_id TEXT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_user ON logs(user_id, category, ts)")

    # Rebuild habit_targets (the sanctioned break): rename-copy-drop rather
    # than an in-place ALTER, since SQLite can't add a surrogate PK or a
    # new UNIQUE constraint to an existing table.
    conn.execute("ALTER TABLE habit_targets RENAME TO habit_targets_v005")
    conn.execute(
        """
        CREATE TABLE habit_targets (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id    TEXT,
          habit_id   TEXT NOT NULL,
          goal       REAL NOT NULL,
          updated_at TEXT,
          UNIQUE(user_id, habit_id)
        )
        """
    )
    conn.execute(
        "INSERT INTO habit_targets (user_id, habit_id, goal, updated_at) "
        "SELECT NULL, habit_id, goal, updated_at FROM habit_targets_v005"
    )
    conn.execute("DROP TABLE habit_targets_v005")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_reminder_times (
          user_id  TEXT NOT NULL,
          habit_id TEXT NOT NULL,
          time     TEXT NOT NULL,
          PRIMARY KEY (user_id, habit_id, time)
        )
        """
    )


def _migration_007_audit_log(conn: sqlite3.Connection) -> None:
    """SPEC-v1.3.md "Audit log" (§4 R-M1): purely additive -- unlike
    migration 006 (the ONE sanctioned break), this one touches NO existing
    table/column/row at all, just a new table + two indexes (AC-A1). One
    row per state-changing action (`core/audit.py:record`, the single
    writer): `user_id` is the ACTOR, `target_user_id` is who an ADMIN
    action was done TO (NULL for a self-action); `entity`/`old_value`/
    `new_value` are all nullable text (a non-habit-scoped action, e.g.
    `lang_set`, has `entity = NULL`; a create-only transition, e.g.
    `user_pending`, has `old_value = NULL`). `idx_audit_ts` serves
    `prune_audit`'s own `WHERE ts < ?` scan; `idx_audit_user` serves a
    future "this user's own history" lookup (not built by this pass, but
    cheap to index for now rather than retrofit later)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
          id             INTEGER PRIMARY KEY AUTOINCREMENT,
          ts             TEXT NOT NULL,
          user_id        TEXT NOT NULL,
          action         TEXT NOT NULL,
          entity         TEXT,
          old_value      TEXT,
          new_value      TEXT,
          source         TEXT NOT NULL,
          target_user_id TEXT,
          created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id, ts)")


def _migration_008_checkin_and_announce(conn: sqlite3.Connection) -> None:
    """SPEC-v1.5.md §4/§5 (shared surface): two additive, all-`NULL`,
    NO-BACKFILL columns on `users` -- unlike migration 004's habit_type
    backfill or 006's owner-attribution follow-up, neither column here
    gets a value-filling pass, because `NULL` IS the correct value for
    every existing row by construction:
    - `checkin_window`: `NULL` means "inherit the config default", and
      OQ1 resolved (b) -- `config.checkin.enabled` itself defaults to
      `False` -- so a `NULL` row is opted OUT by construction (AC-8),
      not merely "using some default that happens to be on".
      Backfilling anything here would be actively wrong.
    - `last_announced_version`: `NULL` means "never announced anything
      to this chat" -- exactly true for every pre-v1.5 row, so an
      existing user correctly receives the v1.5.0 self-announcement on
      first startup (R-N5's own "existing users... do receive the
      v1.5.0 note" -- the whole reason this column starts empty).
    Idempotent the same way every migration here is: the runner only
    ever applies this once, guarded by `PRAGMA user_version` (AC-1)."""
    conn.execute("ALTER TABLE users ADD COLUMN checkin_window TEXT NULL")
    conn.execute("ALTER TABLE users ADD COLUMN last_announced_version TEXT NULL")


def _migration_009_dashboard_and_records(conn: sqlite3.Connection) -> None:
    """SPEC-v1.6.md §5/§6 (shared surface): two additive pieces, mirroring
    migration 008's own "NULL/empty is correct by construction, no
    backfill" posture:
    - `users.dashboard_msg_id` (nullable TEXT): `NULL` means "no live
      dashboard" -- OQ1 resolved (opt-in via `/dashboard on`), so every
      existing row is correctly disabled by construction (AC-D1); a
      non-NULL value is the pinned message's id once a user enables it.
    - `habit_records` (NEW table, no existing data to touch): one row per
      `(user_id, habit_id, record_type)` -- `record_type IN ('best_day',
      'best_week', 'longest_streak')`, `value` the record's numeric value,
      `achieved_on` the date it was set. Stored, not re-derived (R-R1), so
      "beaten?" is a cheap compare. A fresh/pre-v1.6 install starts with
      zero rows -- the first log that would otherwise set a record is
      itself the first "beaten" crossing, mirroring the milestone
      once-per-crossing design's own "nothing to compare against yet"
      starting state.
    Idempotent the same way every migration here is: the runner only
    ever applies this once, guarded by `PRAGMA user_version` (AC-1)."""
    conn.execute("ALTER TABLE users ADD COLUMN dashboard_msg_id TEXT NULL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS habit_records (
          user_id      TEXT NOT NULL,
          habit_id     TEXT NOT NULL,
          record_type  TEXT NOT NULL,
          value        REAL NOT NULL,
          achieved_on  TEXT NOT NULL,
          PRIMARY KEY (user_id, habit_id, record_type)
        )
        """
    )


def _migration_010_user_habits(conn: sqlite3.Connection) -> None:
    """SPEC-v1.7.md §5/§6 (shared surface): one new table, no existing
    table/column/row touched at all -- purely additive, unlike migration
    006 (the one sanctioned break). `user_habits` is the per-user
    definition store `core/habits.py:HabitRegistry.for_user` reads
    (R-G1): one row per user-defined habit, `PRIMARY KEY (user_id, id)`
    so a habit id is only reserved WITHIN that user's own namespace (two
    different users may each independently define an id, e.g. "reading" --
    R-V1's own "not already used by THIS user" scope, not global).
    `archived_at IS NULL` means active (in the registry); a non-NULL
    timestamp means archived (R-C2's soft-delete branch -- excluded from
    `for_user`'s active registry, but the row itself, and every historical
    `logs` row under that id, survives). `unit_aliases` is a JSON-encoded
    `dict[str, float]` (mirrors `config.toml`'s own `[habits.unit_aliases]`
    shape, just serialized -- sqlite has no native map type). A fresh/
    pre-v1.7 install starts with zero rows, so `for_user` for every
    existing user resolves to exactly `from_config(config)` -- byte-
    identical, by construction, until a user actually creates one (AC-2/
    AC-5, the release's own hard regression gate).
    Idempotent the same way every migration here is: the runner only ever
    applies this once, guarded by `PRAGMA user_version` (AC-1)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_habits (
          user_id       TEXT NOT NULL,
          id            TEXT NOT NULL,
          type          TEXT NOT NULL,
          label_en      TEXT NOT NULL,
          label_th      TEXT NOT NULL,
          unit_en       TEXT,
          unit_th       TEXT,
          goal          REAL,
          unit_aliases  TEXT,
          archived_at   TEXT,
          created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          PRIMARY KEY (user_id, id)
        )
        """
    )


def _migration_011_routines(conn: sqlite3.Connection) -> None:
    """SPEC-v1.8.md §5/§6 (module `routines`, R-R6): two new tables, no
    existing table/column/row touched at all -- purely additive, like
    migration 010 (and every migration except 006's own sanctioned break).
    `routines` is one row per user-defined habit stack (`PRIMARY KEY
    (user_id, name)`, so a routine name is only reserved within THAT
    user's own namespace, mirroring `user_habits`' own per-user id
    scoping). `routine_items` is the ordered item list for each routine
    (`PRIMARY KEY (user_id, name, seq)`, `seq` a zero-based insertion
    index so `core/routines.py` can always replay items in the order the
    user created them -- SQLite gives no ordering guarantee across rows
    without an explicit column to sort by). `habit_id` is stored as a raw
    TEXT reference (not a FOREIGN KEY -- this codebase's schema never uses
    them, e.g. `logs.category`/`habit_targets.habit_id` are likewise
    unconstrained text) so a routine item can keep referencing a habit id
    even after that habit is later archived/deleted (R-R3's own "skip and
    note" behavior for a since-removed item depends on the row surviving
    that habit's own removal). `value` is nullable REAL -- a boolean or
    text habit item carries no meaningful numeric value (R-R3: boolean
    always logs true regardless, text is always skipped), so `NULL` is the
    correct stored value for those, while a numeric/duration item's
    already-validated, already-unit-resolved base-unit value is stored
    directly (no re-parsing at run time). A fresh/pre-v1.8 install starts
    with zero rows in both tables, so `core/routines.py:execute_routine`'s
    "no routines yet" list view is exactly what every existing user sees
    until they create one (AC-9's own "inert until invoked" gate).
    Idempotent the same way every migration here is: the runner only ever
    applies this once, guarded by `PRAGMA user_version` (AC-B6)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routines (
          user_id    TEXT NOT NULL,
          name       TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          PRIMARY KEY (user_id, name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routine_items (
          user_id  TEXT NOT NULL,
          name     TEXT NOT NULL,
          seq      INTEGER NOT NULL,
          habit_id TEXT NOT NULL,
          value    REAL,
          PRIMARY KEY (user_id, name, seq)
        )
        """
    )


def _migration_012_lifecycle(conn: sqlite3.Connection) -> None:
    """SPEC-v1.9.md §5/§6 (shared surface, the streak-engine rework's own
    storage): three new tables, no existing table/column/row touched at
    all -- purely additive, like every migration except 006's own
    sanctioned break. Each is read by `core/streaks.py`'s reworked
    `compute_streak` (SHARED read accessors in `storage/db.py`:
    `get_cadence`/`paused_dates`/`grace_protected_dates`) but WRITTEN only
    by its own later, disjoint module (M1 `cadence`/M2 `grace`/M3 `pause`)
    -- this migration only lays down the shape, mirroring migration 011's
    own "shared surface creates the tables, the owning module(s) add the
    CRUD" split.

    `habit_cadence` (module `cadence`, R18): `PRIMARY KEY (user_id,
    habit_id)` -- one row per user+habit that has declared a weekly
    cadence; a habit's absence from this table is what `streak_unit`
    treats as "daily" (R5), so a fresh/pre-v1.9 install's byte-identical
    gate (AC3) holds by construction (zero rows -> `compute_streak` never
    takes the weekly-walk branch for anyone).

    `grace_ledger` (module `grace`, R8/R9): `PRIMARY KEY (user_id,
    habit_id, protected_date)` -- one row per date a nightly
    `evaluate_grace` run has already bridged; `period_key` is the ISO
    `"<year>-W<week>"` string `grace_used_in_week` groups by (R8's "at
    most one grace per ISO week" rule) -- stored as a plain column
    (indexed implicitly via the PK's leading `user_id, habit_id`) rather
    than derived at read time, so `grace_used_in_week` is a cheap indexed
    lookup, not a per-call ISO-calendar recomputation over every row.

    `pauses` (module `pause`, R12): a plain `AUTOINCREMENT` id (unlike the
    other two tables' composite PKs) because a user can have MULTIPLE
    overlapping/sequential pause rows over time (e.g. pause water, later
    pause everything) and `/resume` needs to address/delete a specific
    active set, not a single natural key. `habit_id IS NULL` means
    "all habits" (R12's own "no habit token = pause all"). `start_date`/
    `end_date` are inclusive 'YYYY-MM-DD' strings -- `storage/db.py:
    paused_dates`'s own range-overlap query relies on plain lexicographic
    string comparison staying correct across a year boundary (ISO date
    strings sort correctly year-over-year, e.g. "2026-12-31" < "2027-01-01"),
    same convention `logs_between`/`prune_audit` already rely on for `ts`.
    `idx_pauses_user` serves both `paused_dates`'s own per-user+habit
    range scan and `active_pauses`'s per-user scan.

    A fresh/pre-v1.9 install starts with zero rows in all three tables, so
    `compute_streak`'s daily walk for every existing user/habit takes
    exactly the pre-v1.9 code path (AC2/AC3's own hard byte-identical
    gate). Idempotent the same way every migration here is: the runner
    only ever applies this once, guarded by `PRAGMA user_version`."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS habit_cadence (
          user_id    TEXT NOT NULL,
          habit_id   TEXT NOT NULL,
          per_week   INTEGER NOT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          PRIMARY KEY (user_id, habit_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS grace_ledger (
          user_id        TEXT NOT NULL,
          habit_id       TEXT NOT NULL,
          protected_date TEXT NOT NULL,
          period_key     TEXT NOT NULL,
          created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          PRIMARY KEY (user_id, habit_id, protected_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pauses (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id    TEXT NOT NULL,
          habit_id   TEXT NULL,
          start_date TEXT NOT NULL,
          end_date   TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pauses_user ON pauses(user_id, habit_id)")


def _migration_013_unparsed_state(conn: sqlite3.Connection) -> None:
    """SPEC-v1.10.md §4 R-SS1 (shared surface, the "never lose a log"
    unparsed-state machine's own storage): one new nullable column, no
    existing table/column/row touched at all -- purely additive, like every
    migration except 006's own sanctioned break. `logs.unparsed_state`
    gives every `category='unparsed'` row a lifecycle (`NULL`/`'awaiting_
    llm'` -> `'awaiting_clarify'` -> `'closed'`, or reclassified away from
    `'unparsed'` entirely) instead of the pre-1.10 world where a row that
    the LLM could never place had no terminal state and was silently
    re-parsed (2 LLM calls) on every future DOWN->UP recovery sweep,
    forever, with the user never told (§1's own "id=13"/"id=14" production
    zombies).

    Deliberately **no data-migration UPDATE** here (unlike migration 004's
    `habit_type` backfill) -- every existing `'unparsed'` row (including
    those two zombies) simply keeps `NULL`, which `Database.pending_
    unparsed()`'s new predicate (R-SS2) and both CAS methods' `from_states`
    predicate (R-SS3) treat as `'awaiting_llm'` by construction: a legacy
    row is automatically eligible for the very next recovery sweep, and --
    if that sweep still can't place it -- gets closed (R1) exactly like a
    fresh post-1.10 deferral would. This is what lets those two production
    rows enter the new machinery and finally terminate on the first
    post-1.10 recovery, with no separate backfill pass required.

    Idempotent the same way every migration here is: the runner only ever
    applies this once, guarded by `PRAGMA user_version` (AC1)."""
    conn.execute("ALTER TABLE logs ADD COLUMN unparsed_state TEXT")


def _migration_014_line_digest(conn: sqlite3.Connection) -> None:
    """SPEC-LINE.md §4 R-S4 (shared surface, branch `line-version`): two
    additive pieces, no existing table/column/row touched at all -- purely
    additive, like every migration except 006's own sanctioned break.

    `push_ledger` (module C, R-C6): one row per `(user_id, yyyymm)`
    tracking how many LINE Push API sends that user has received this
    month -- the quota-bookkeeping half of the "at most one push/user/day"
    decision (LINE's free plan counts ~300 pushes/month total, uncounted
    Reply API sends don't touch this table at all). `count` starts at 0 and
    is only ever incremented via `Database.increment_push`'s upsert (R-S5);
    `updated_at` is informational only, not read by any query here.

    `users.digest_opt_out` (module C, R-C4): `0` (subscribed) by default --
    the locked user decision (2026-08-29, SPEC-LINE.md's own header) is
    "digest default ON, per-user OPT-OUT", so every existing row (and every
    fresh one, via the table's own column default) starts subscribed,
    exactly matching that decision without a backfill pass. Mirrors
    migration 008's/009's own "the column default alone encodes the right
    starting state, no UPDATE needed" posture.

    A fresh/pre-LINE install starts with zero `push_ledger` rows and every
    `users` row un-opted-out, so `monthly_push_total`/`push_count` read 0
    and `digest_opt_out` reads `False` for everyone until the digest job
    (module C, not yet wired) actually sends something (AC2/AC3's own
    idempotent-rerun gate). Idempotent the same way every migration here
    is: the runner only ever applies this once, guarded by `PRAGMA
    user_version`."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS push_ledger (
          user_id    TEXT NOT NULL,
          yyyymm     TEXT NOT NULL,
          count      INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          PRIMARY KEY (user_id, yyyymm)
        )
        """
    )
    conn.execute("ALTER TABLE users ADD COLUMN digest_opt_out INTEGER NOT NULL DEFAULT 0")


# Ordered list of migrations. Index 0 -> user_version 1, index 1 ->
# user_version 2, etc. Append-only: never reorder or remove an entry once
# it has shipped, or a DB stamped at that version will silently skip it.
MIGRATIONS: list[Migration] = [
    _migration_001_baseline,
    _migration_002_category_index,
    _migration_003_soft_delete,
    _migration_004_habit_type,
    _migration_005_habit_targets,
    _migration_006_multiuser,
    _migration_007_audit_log,
    _migration_008_checkin_and_announce,
    _migration_009_dashboard_and_records,
    _migration_010_user_habits,
    _migration_011_routines,
    _migration_012_lifecycle,
    _migration_013_unparsed_state,
    _migration_014_line_digest,
]


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def run_migrations(
    conn: sqlite3.Connection, migrations: list[Migration] | None = None
) -> tuple[int, int]:
    """Apply every migration above the DB's current user_version, in
    order, each inside its own transaction (schema change + version stamp
    commit/rollback together). Returns (from_version, to_version).

    Idempotent: a DB already at len(migrations) applies nothing and
    returns (N, N).
    """
    migrations = MIGRATIONS if migrations is None else migrations
    target_version = len(migrations)

    previous_isolation = conn.isolation_level
    conn.isolation_level = None  # explicit BEGIN/COMMIT/ROLLBACK control
    try:
        from_version = current_version(conn)

        if from_version >= target_version:
            logger.info(
                "DB schema up to date at version %d; no migrations applied", from_version
            )
            return from_version, from_version

        version = from_version
        for idx in range(from_version, target_version):
            migration = migrations[idx]
            name = getattr(migration, "__name__", str(migration))
            next_version = idx + 1
            conn.execute("BEGIN")
            try:
                migration(conn)
                conn.execute(f"PRAGMA user_version = {next_version}")
            except Exception:
                conn.rollback()
                logger.exception(
                    "Migration %d (%s) failed; rolled back, DB left at version %d",
                    next_version,
                    name,
                    version,
                )
                raise
            else:
                conn.commit()
                version = next_version
                logger.info("Applied migration %d/%d (%s)", version, target_version, name)

        logger.info("DB schema migrated %d -> %d", from_version, version)
        return from_version, version
    finally:
        conn.isolation_level = previous_isolation
