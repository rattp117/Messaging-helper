"""SQLite access layer. Stdlib sqlite3 only, WAL mode, schema per SPEC.md §5.

No channel imports here (SPEC.md §8) — this module only knows about logs.

SPEC-v1.2.md "Multi-user support" (R-D1/R-D2/R-D4): every scoped
read/aggregate/mutate method below now takes a leading `user_id` (or, for
row-addressed ops like `get_log`/`soft_delete`/`update_value`, the caller
is responsible for an ownership check via `get_log` first — R-C3). This is
the structural half of the isolation invariant (AC-U-ISO): no scoped query
in this file runs without a `user_id` filter, so a caller literally cannot
forget to scope a read — the parameter is required, not optional.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from habit_assistant.storage.migrations import run_migrations
from habit_assistant.storage.models import AuditEntry, LogEntry

_UNSET = object()  # sentinel: "field not given" vs. "field explicitly set to None"


class Database:
    """Thin wrapper around one sqlite3 connection. Not thread-safe by
    design — the app is a single asyncio process; all DB calls happen on
    the event-loop thread (sqlite3 calls are fast/local so this is fine
    without wrapping in a threadpool)."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        # ROADMAP v0.3.0: schema now evolves through storage/migrations.py's
        # user_version-based runner instead of a single inline executescript.
        self.schema_version_before, self.schema_version = run_migrations(self._conn)

    def close(self) -> None:
        self._conn.close()

    # -----------------------------------------------------------------
    # logs (SPEC-v1.2.md R-D1: every row carries user_id; every read
    # filters by it)
    # -----------------------------------------------------------------

    def insert_log(self, entry: LogEntry) -> int:
        cur = self._conn.execute(
            "INSERT INTO logs (user_id, ts, category, value_num, value_text, raw_message, source, habit_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.user_id,
                entry.ts,
                entry.category,
                entry.value_num,
                entry.value_text,
                entry.raw_message,
                entry.source,
                entry.habit_type,
            ),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def sum_value(self, user_id: str, habit_id: str, day: str) -> float:
        """Generic `SUM(value_num)` for one user's habit id, day:
        'YYYY-MM-DD'. Excludes soft-deleted rows (ROADMAP.md v0.5.0
        AC5.4) -- an undone/edited-away entry must not count toward
        today's total. `water_total_ml` is a thin wrapper."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(value_num), 0) AS total FROM logs "
            "WHERE user_id = ? AND category = ? AND deleted_at IS NULL AND ts LIKE ?",
            (user_id, habit_id, f"{day}%"),
        ).fetchone()
        return float(row["total"])

    def count(self, user_id: str, habit_id: str, day: str) -> int:
        """Generic `COUNT(*)` for one user's habit id/day. `stretch_count`/
        `diary_count` are thin wrappers."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM logs WHERE user_id = ? AND category = ? AND deleted_at IS NULL AND ts LIKE ?",
            (user_id, habit_id, f"{day}%"),
        ).fetchone()
        return int(row["n"])

    def count_true(self, user_id: str, habit_id: str, day: str) -> int:
        """Generic count of one user's "truthy" boolean-habit rows for a
        day (`value_num != 0`, per `log_entry_from_result`'s 1.0/0.0
        encoding of a boolean value)."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM logs "
            "WHERE user_id = ? AND category = ? AND deleted_at IS NULL AND ts LIKE ? AND value_num != 0",
            (user_id, habit_id, f"{day}%"),
        ).fetchone()
        return int(row["n"])

    def water_total_ml(self, user_id: str, day: str) -> float:
        """Thin wrapper over `sum_value` (ROADMAP.md v0.7.0 R12) -- kept so
        every pre-v0.7 caller's behavior is unchanged."""
        return self.sum_value(user_id, "water", day)

    def stretch_count(self, user_id: str, day: str) -> int:
        """Thin wrapper over `count` (ROADMAP.md v0.7.0 R12)."""
        return self.count(user_id, "stretch", day)

    def diary_count(self, user_id: str, day: str) -> int:
        """Thin wrapper over `count` (ROADMAP.md v0.7.0 R12)."""
        return self.count(user_id, "diary", day)

    def logs_between(self, user_id: str, start_ts: str, end_ts: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM logs WHERE user_id = ? AND ts >= ? AND ts <= ? AND deleted_at IS NULL ORDER BY ts",
            (user_id, start_ts, end_ts),
        ).fetchall()

    def pending_unparsed(self) -> list[sqlite3.Row]:
        """ROADMAP.md v0.4.0 AC3.3: rows deferred while the LLM was
        unavailable, still waiting to be re-parsed. A plain query against
        persisted state, not an in-memory queue -- so this also finds rows
        deferred by a *previous* process run (survives a restart). Excludes
        soft-deleted rows (ROADMAP.md v0.5.0 AC5.4): an undone deferred
        message should not be recovered/re-parsed.

        SPEC-v1.2.md R-D1: deliberately stays GLOBAL (no `user_id` filter,
        unlike every other read in this file) -- the recovery job needs
        every deferred row across every user in one pass. Each row still
        carries its own `user_id` column (SELECT *), so the caller
        (`main.py:reparse_pending_unparsed`) addresses each confirmation
        to the right chat (R-D3)."""
        return self._conn.execute(
            "SELECT * FROM logs WHERE category = 'unparsed' AND deleted_at IS NULL ORDER BY ts"
        ).fetchall()

    def reclassify_log(
        self,
        log_id: int,
        category: str,
        value_num: float | None,
        value_text: str | None,
        habit_type: str | None = None,
    ) -> None:
        """Convert a deferred 'unparsed' row to its real category once it
        has been successfully re-parsed (ROADMAP.md v0.4.0 AC3.3).
        `ts`/`raw_message`/`source`/`user_id` are left untouched -- only
        the parsed-out fields change. Row-addressed by `log_id`; the row
        already carries its own `user_id` from `insert_log`, so no
        `user_id` param is needed here."""
        self._conn.execute(
            "UPDATE logs SET category = ?, value_num = ?, value_text = ?, habit_type = ? WHERE id = ?",
            (category, value_num, value_text, habit_type, log_id),
        )
        self._conn.commit()

    def last_log(self, user_id: str, category: str | None = None) -> sqlite3.Row | None:
        """Most recent non-deleted log for `user_id` (ROADMAP.md v0.5.0
        AC5.1/AC5.3), optionally restricted to one category. Ties on `ts`
        (e.g. seeded/backdated data) break on `id DESC` so "most recent"
        is always well-defined as "most recently inserted"."""
        if category is None:
            return self._conn.execute(
                "SELECT * FROM logs WHERE user_id = ? AND deleted_at IS NULL ORDER BY ts DESC, id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return self._conn.execute(
            "SELECT * FROM logs WHERE user_id = ? AND deleted_at IS NULL AND category = ? "
            "ORDER BY ts DESC, id DESC LIMIT 1",
            (user_id, category),
        ).fetchone()

    def soft_delete(self, log_id: int) -> None:
        """Undo (ROADMAP.md v0.5.0 AC5.1): mark a row deleted without
        removing it, so it stays out of every aggregation (AC5.4) but
        remains in the table for audit/recovery. Row-addressed by
        `log_id` -- SPEC-v1.2.md R-C3: callers MUST verify ownership via
        `get_log(log_id).user_id` before calling this; this method itself
        performs no ownership check (it has no way to -- it isn't told who
        is asking)."""
        self._conn.execute(
            "UPDATE logs SET deleted_at = datetime('now','localtime') WHERE id = ?", (log_id,)
        )
        self._conn.commit()

    def get_log(self, log_id: int) -> sqlite3.Row | None:
        """Fetch a single log row by id, regardless of `deleted_at` --
        callers need to tell "already soft-deleted" from "genuinely
        missing" apart from "still live", so this deliberately does NOT
        filter on `deleted_at IS NULL` the way every aggregation query
        does. SPEC-v1.2.md R-C3: the returned row carries `user_id`,
        which is the ownership-check seam -- the undo-button callback
        compares it against the tapping chat id before soft-deleting."""
        return self._conn.execute("SELECT * FROM logs WHERE id = ?", (log_id,)).fetchone()

    def update_value(self, log_id: int, value_num=_UNSET, value_text=_UNSET) -> None:
        """Edit (ROADMAP.md v0.5.0 AC5.3): update only the field(s) given.
        `_UNSET` (not `None`) is the "leave alone" default so a numeric-only
        edit ("make that 300ml") never clobbers an existing `value_text`
        (and vice versa) -- `None` is itself a valid value to set.
        Row-addressed by `log_id`; the caller (`main.py:_execute_edit`)
        already resolved the row via `last_log(user_id, ...)`, so no
        `user_id` param is needed here."""
        fields: list[str] = []
        params: list = []
        if value_num is not _UNSET:
            fields.append("value_num = ?")
            params.append(value_num)
        if value_text is not _UNSET:
            fields.append("value_text = ?")
            params.append(value_text)
        if not fields:
            return
        params.append(log_id)
        self._conn.execute(f"UPDATE logs SET {', '.join(fields)} WHERE id = ?", params)
        self._conn.commit()

    # -----------------------------------------------------------------
    # habit_targets (SPEC-v1.2.md R-D2: per-user, migration 006 rebuild)
    # -----------------------------------------------------------------

    def get_target(self, user_id: str, habit_id: str) -> float | None:
        """The stored override goal for `(user_id, habit_id)`, or `None`
        if none is set. Read live (no caching) so a `/target` write takes
        effect on the very next call."""
        row = self._conn.execute(
            "SELECT goal FROM habit_targets WHERE user_id = ? AND habit_id = ?", (user_id, habit_id)
        ).fetchone()
        return float(row["goal"]) if row is not None else None

    def set_target(self, user_id: str, habit_id: str, goal: float) -> None:
        """Upsert -- a second `/target` for the same user+habit replaces
        the previous override rather than erroring or stacking rows.
        `ON CONFLICT(user_id, habit_id)` matches migration 006's
        `UNIQUE(user_id, habit_id)` constraint on the rebuilt table."""
        self._conn.execute(
            "INSERT INTO habit_targets (user_id, habit_id, goal, updated_at) "
            "VALUES (?, ?, ?, datetime('now','localtime')) "
            "ON CONFLICT(user_id, habit_id) DO UPDATE SET goal = excluded.goal, updated_at = excluded.updated_at",
            (user_id, habit_id, goal),
        )
        self._conn.commit()

    def clear_target(self, user_id: str, habit_id: str) -> None:
        """Delete the override row for `(user_id, habit_id)`, if any; a
        no-op (not an error) when no override is currently set."""
        self._conn.execute("DELETE FROM habit_targets WHERE user_id = ? AND habit_id = ?", (user_id, habit_id))
        self._conn.commit()

    def all_targets(self, user_id: str) -> dict[str, float]:
        """Every currently-overridden habit id -> its goal, for this
        user's `/target` (no args) "show all" reply."""
        rows = self._conn.execute(
            "SELECT habit_id, goal FROM habit_targets WHERE user_id = ?", (user_id,)
        ).fetchall()
        return {row["habit_id"]: float(row["goal"]) for row in rows}

    # -----------------------------------------------------------------
    # users (SPEC-v1.2.md R-M1/R-A*/R-P1/R-P2 -- migration 006)
    # -----------------------------------------------------------------

    def attribute_legacy_to_owner(self, owner_chat_id: str) -> None:
        """SPEC-v1.2.md R-M2: startup attribution, called once in
        `async_main` right after `load_secrets` (identity-aware -- the
        owner id only exists in `.env`, unreachable from the migration
        runner). Idempotent: (a) upserts the owner's `users` row as
        `role='owner', status='active'` (never a downgrade -- this always
        writes exactly those two values, so calling it again is a no-op
        write of the same state); (b)/(c) backfill every previously-NULL
        `logs`/`habit_targets` row to the owner. After the first run no
        NULL `user_id` rows remain, so a second run's UPDATEs affect zero
        rows (AC-M2)."""
        self.upsert_user(owner_chat_id, role="owner", status="active")
        self._conn.execute("UPDATE logs SET user_id = ? WHERE user_id IS NULL", (owner_chat_id,))
        self._conn.execute("UPDATE habit_targets SET user_id = ? WHERE user_id IS NULL", (owner_chat_id,))
        self._conn.commit()

    def get_user(self, chat_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()

    def upsert_user(
        self, chat_id: str, *, role: str | None = None, status: str | None = None, display_name: str | None = None
    ) -> None:
        """Create the `users` row for `chat_id` if it doesn't exist yet
        (new pending contact, R-A2), else update only the fields given --
        so `/approve <id>` (status only) never clobbers a previously
        captured `display_name`, and `attribute_legacy_to_owner`'s
        `role="owner", status="active"` call never touches columns it
        wasn't given. `role`/`status` default to `"member"`/`"pending"`
        on first creation (matching the table's own column defaults) when
        this is the row's very first write and the caller didn't specify
        them."""
        existing = self.get_user(chat_id)
        if existing is None:
            self._conn.execute(
                "INSERT INTO users (chat_id, role, status, display_name) VALUES (?, ?, ?, ?)",
                (chat_id, role or "member", status or "pending", display_name),
            )
            self._conn.commit()
            return

        fields: list[str] = []
        params: list = []
        if role is not None:
            fields.append("role = ?")
            params.append(role)
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if display_name is not None:
            fields.append("display_name = ?")
            params.append(display_name)
        if not fields:
            return
        params.append(chat_id)
        self._conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE chat_id = ?", params)
        self._conn.commit()

    def set_user_language(self, chat_id: str, pref: str) -> None:
        """SPEC-v1.2.md R-P1 (`/lang` write, module `preferences`):
        upserts so setting a language preference before any other contact
        (unlikely, but not this method's problem to forbid) still works."""
        self._conn.execute(
            "INSERT INTO users (chat_id, language_pref) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET language_pref = excluded.language_pref",
            (chat_id, pref),
        )
        self._conn.commit()

    def set_user_quiet_hours(self, chat_id: str, windows_json: str | None) -> None:
        """SPEC-v1.2.md R-P2 (`/quiet` write, module `preferences`).
        `windows_json = None` means "inherit `config.quiet_hours.windows`"
        (the column's own default); an explicit `"[]"` means "no quiet
        hours for me" -- distinct states, both valid values here."""
        self._conn.execute(
            "INSERT INTO users (chat_id, quiet_hours_json) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET quiet_hours_json = excluded.quiet_hours_json",
            (chat_id, windows_json),
        )
        self._conn.commit()

    def list_users(self) -> list[sqlite3.Row]:
        """SPEC-v1.2.md R-A4 (`/users`, module `access`): every user, in
        the order they first contacted the bot."""
        return self._conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()

    def active_user_ids(self) -> list[str]:
        """SPEC-v1.2.md R-S1/R-S3: the fan-out set for the reminder tick
        and the daily-summary/weekly-review jobs. `status = 'active'`
        includes the owner (owner ⊂ active, R-A1) since
        `attribute_legacy_to_owner` stamps the owner's own row
        `status='active'`."""
        rows = self._conn.execute(
            "SELECT chat_id FROM users WHERE status = 'active' ORDER BY created_at"
        ).fetchall()
        return [row["chat_id"] for row in rows]

    # -----------------------------------------------------------------
    # user_reminder_times (SPEC-v1.2.md R-S4/R-S5 -- migration 006)
    # -----------------------------------------------------------------

    def get_reminder_times(self, user_id: str, habit_id: str) -> list[str]:
        """`[]` = no override stored for this user+habit (the caller,
        `reminders.effective_reminder_times`, falls back to the habit's
        config `reminder_times`); `["off"]` = the explicit sentinel
        (no reminders); anything else = the stored custom `HH:MM` list,
        already sorted by the `ORDER BY` below."""
        rows = self._conn.execute(
            "SELECT time FROM user_reminder_times WHERE user_id = ? AND habit_id = ? ORDER BY time",
            (user_id, habit_id),
        ).fetchall()
        return [row["time"] for row in rows]

    def set_reminder_times(self, user_id: str, habit_id: str, times: list[str]) -> None:
        """Delete-then-insert (R-S5): replaces any existing override for
        this user+habit wholesale, including the `["off"]` sentinel --
        callers pass exactly what they want stored, this method doesn't
        interpret the list's meaning."""
        self._conn.execute(
            "DELETE FROM user_reminder_times WHERE user_id = ? AND habit_id = ?", (user_id, habit_id)
        )
        self._conn.executemany(
            "INSERT INTO user_reminder_times (user_id, habit_id, time) VALUES (?, ?, ?)",
            [(user_id, habit_id, t) for t in times],
        )
        self._conn.commit()

    def clear_reminder_times(self, user_id: str, habit_id: str) -> None:
        """Delete all override rows for this user+habit -- reverts to the
        config fallback (R-S4's "no rows" case)."""
        self._conn.execute(
            "DELETE FROM user_reminder_times WHERE user_id = ? AND habit_id = ?", (user_id, habit_id)
        )
        self._conn.commit()

    # -----------------------------------------------------------------
    # audit_log (SPEC-v1.3.md R-M1/R-W1 -- migration 007). The only
    # writer is `core/audit.py:record`, which wraps `insert_audit` in its
    # own fail-open try/except (R-W2) -- this method itself is a plain,
    # unprotected insert, exactly like `insert_log`'s own shape; the
    # fail-open contract lives one layer up, not duplicated here.
    # -----------------------------------------------------------------

    def insert_audit(self, entry: AuditEntry) -> int:
        cursor = self._conn.execute(
            "INSERT INTO audit_log (ts, user_id, action, entity, old_value, new_value, source, target_user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.ts,
                entry.user_id,
                entry.action,
                entry.entity,
                entry.old_value,
                entry.new_value,
                entry.source,
                entry.target_user_id,
            ),
        )
        self._conn.commit()
        return cursor.lastrowid

    def recent_audit(self, limit: int) -> list[sqlite3.Row]:
        """SPEC-v1.3.md R-V2: newest-first (`ORDER BY id DESC`, not `ts` --
        `id` is a strictly monotonic insert order even when two rows
        share the same second-resolution `ts`, which `ts` alone can't
        guarantee)."""
        return self._conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def prune_audit(self, cutoff_ts: str) -> int:
        """SPEC-v1.3.md R-W3: delete every row strictly older than
        `cutoff_ts` (plain string comparison -- `ts` is a fixed-width
        ISO8601 local timestamp, so lexicographic order == chronological
        order, the same convention `logs_between`'s own `ts >= ?/<= ?`
        filters already rely on). Returns the number of rows deleted, so
        a caller can log a non-zero prune without a second query."""
        cursor = self._conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff_ts,))
        self._conn.commit()
        return cursor.rowcount
