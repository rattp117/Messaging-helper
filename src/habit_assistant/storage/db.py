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
from datetime import date, timedelta
from pathlib import Path

from habit_assistant.storage.migrations import run_migrations
from habit_assistant.storage.models import AuditEntry, LogEntry

_UNSET = object()  # sentinel: "field not given" vs. "field explicitly set to None"


def _day_bounds(day: str) -> tuple[str, str]:
    """SPEC-REFACTOR.md Stage 1 rule 3: turn a 'YYYY-MM-DD' day into a
    half-open `[day, next_day)` string range for an index-friendly `ts >=
    ? AND ts < ?` filter -- replaces the `ts LIKE '{day}%'` pattern that
    can't use `idx_logs_user`'s trailing `ts` column (SQLite's default
    case-insensitive LIKE disables the index range-scan; measured 14x
    slower over a year of rows). `date.fromisoformat(day) + timedelta(days=1)`
    rolls over month/year boundaries correctly (e.g. '2026-12-31' ->
    '2027-01-01'). Byte-identical to the LIKE form across every boundary:
    the real `ts` format always uses the 'T' separator (`'T' > ''`, i.e.
    every same-day timestamp string sorts `>= day` and `< next_day`
    lexicographically, same convention `logs_between`/`prune_audit`/
    `paused_dates` already rely on for ISO timestamp/date comparisons)."""
    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    return day, next_day


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
        # SPEC-REFACTOR.md Stage 1 rule 4 (user-approved durability trade-off,
        # OQ1, 2026-08-26): WAL's default synchronous=FULL fsyncs on every
        # commit (measured 1.45 ms/write); NORMAL only fsyncs at WAL
        # checkpoints, not every commit (measured 0.31 ms/write, ~4.7x).
        # Standard recommendation for WAL mode. The only semantic change is
        # durability on an OS crash / power loss -- the last few committed
        # transactions since the last checkpoint could be lost on restart
        # (never corruption; every already-checkpointed row is safe). No
        # observable output changes for this single-process app.
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        # busy_timeout: retry up to 5s on SQLITE_BUSY (another connection
        # holding the write lock) instead of raising immediately -- cheap
        # insurance now that WAL is in use; this app is single-process/
        # single-writer, so it is never expected to actually wait.
        self._conn.execute("PRAGMA busy_timeout=5000;")
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
        today's total. `water_total_ml` is a thin wrapper.

        SPEC-REFACTOR.md Stage 1 rule 3: filters on a `[day, next_day)`
        range (`_day_bounds`) rather than `ts LIKE '{day}%'` -- byte-
        identical result, but index-friendly (`idx_logs_user(user_id,
        category, ts)` can range-scan `ts` instead of a LIKE-driven table
        scan of the (user_id, category) partition; measured 14x faster
        over a year of rows)."""
        start, end = _day_bounds(day)
        row = self._conn.execute(
            "SELECT COALESCE(SUM(value_num), 0) AS total FROM logs "
            "WHERE user_id = ? AND category = ? AND deleted_at IS NULL AND ts >= ? AND ts < ?",
            (user_id, habit_id, start, end),
        ).fetchone()
        return float(row["total"])

    def count(self, user_id: str, habit_id: str, day: str) -> int:
        """Generic `COUNT(*)` for one user's habit id/day. `stretch_count`/
        `diary_count` are thin wrappers.

        SPEC-REFACTOR.md Stage 1 rule 3: range-bound day filter, see
        `sum_value`'s own docstring for the byte-identity/perf rationale."""
        start, end = _day_bounds(day)
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM logs WHERE user_id = ? AND category = ? AND deleted_at IS NULL "
            "AND ts >= ? AND ts < ?",
            (user_id, habit_id, start, end),
        ).fetchone()
        return int(row["n"])

    def count_true(self, user_id: str, habit_id: str, day: str) -> int:
        """Generic count of one user's "truthy" boolean-habit rows for a
        day (`value_num != 0`, per `log_entry_from_result`'s 1.0/0.0
        encoding of a boolean value).

        SPEC-REFACTOR.md Stage 1 rule 3: range-bound day filter, see
        `sum_value`'s own docstring for the byte-identity/perf rationale."""
        start, end = _day_bounds(day)
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM logs "
            "WHERE user_id = ? AND category = ? AND deleted_at IS NULL AND ts >= ? AND ts < ? AND value_num != 0",
            (user_id, habit_id, start, end),
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

    def recent_logs(self, user_id: str, limit: int, category: str | None = None) -> list[sqlite3.Row]:
        """SPEC-v1.4.md R-D1 (module `history`, `/history [N]`): `user_id`'s
        most recent logged entries, newest-first (`ORDER BY ts DESC, id
        DESC` -- the same tie-break convention `last_log` already uses, so
        two entries sharing a `ts` still resolve deterministically to
        "most recently inserted"). Two deliberate differences from every
        OTHER read in this file:
        - Does **NOT** filter `deleted_at IS NULL` -- a soft-deleted
          (undone) entry is still part of the caller's own history and
          must be shown, clearly marked (`history_view.py`'s job), not
          silently hidden the way every aggregation query hides it.
        - **Excludes** `category = 'unparsed'` unconditionally (even when
          `category` is not given) -- a deferred/still-unparsed row was
          never a confirmed entry (SPEC-v1.4.md §4 R-D1's own "item 5,
          recommended default"), so it has no place in a statement of
          what the user actually logged.
        `category`, when given, filters to exactly that habit id (the
        optional `/history <habit> [N]` filter, R-D2) -- `None` means
        every habit. Strictly scoped to `user_id` (U-ISO, AC-9)."""
        if category is None:
            return self._conn.execute(
                "SELECT * FROM logs WHERE user_id = ? AND category != 'unparsed' "
                "ORDER BY ts DESC, id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return self._conn.execute(
            "SELECT * FROM logs WHERE user_id = ? AND category != 'unparsed' AND category = ? "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (user_id, category, limit),
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

    def get_checkin_window(self, chat_id: str) -> str | None:
        """SPEC-v1.5.md R-K2/R-K8 (`/checkin`, module `checkins`): the raw
        stored value -- `None` (no row, or an un-set column) means
        "inherit `config.checkin.enabled`/`config.checkin.window`"
        (OQ1 resolved (b): that inherited default is itself OFF, AC-8);
        `"off"` means explicitly disabled; any other string is a stored
        `"HH:MM-HH:MM"` window (enabled). This method does no
        interpretation at all -- `checkins.effective_checkin` (module
        `checkins`) is where the three-way meaning above is resolved,
        same "storage returns raw, the owning module interprets" split
        `get_reminder_times`/`effective_reminder_times` already use."""
        row = self.get_user(chat_id)
        return row["checkin_window"] if row is not None else None

    def set_checkin_window(self, chat_id: str, value: str | None) -> None:
        """Upsert -- mirrors `set_user_language`/`set_user_quiet_hours`'s
        own shape exactly. `value = None` reverts to "inherit config"
        (`/checkin default`); `"off"` disables; any other string is the
        window to store verbatim (`/checkin on` stores the CONFIG
        default window explicitly, per R-K8's own "stays enabled
        regardless of the config default" requirement -- that
        resolution happens in `checkins.execute_checkin`, not here)."""
        self._conn.execute(
            "INSERT INTO users (chat_id, checkin_window) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET checkin_window = excluded.checkin_window",
            (chat_id, value),
        )
        self._conn.commit()

    def get_last_announced_version(self, chat_id: str) -> str | None:
        """SPEC-v1.5.md R-N2/R-N3 (module `announce`): `None` means "never
        announced anything to this chat" -- true for every pre-v1.5 row
        (migration 008's own no-backfill design) and for a brand-new
        user, so both cases correctly receive the CURRENT version's note
        on the next `announce_release` run rather than being silently
        skipped."""
        row = self.get_user(chat_id)
        return row["last_announced_version"] if row is not None else None

    def set_last_announced_version(self, chat_id: str, version: str) -> None:
        """Upsert, mirrors the other three `users`-column setters above.
        `announce.announce_release` (R-N2) calls this ONLY after a
        successful send -- a send/DB failure must leave the previous
        value (or `NULL`) in place so that user is retried next startup,
        never marked as caught-up on a failed attempt."""
        self._conn.execute(
            "INSERT INTO users (chat_id, last_announced_version) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET last_announced_version = excluded.last_announced_version",
            (chat_id, version),
        )
        self._conn.commit()

    def get_dashboard_msg_id(self, chat_id: str) -> str | None:
        """SPEC-v1.6.md R-D1 (module `dashboard`): `None` means "no live
        dashboard" (default, migration 009's own no-backfill design);
        any other string is the id of the pinned message currently
        showing that user's board. Storage-only, no interpretation --
        mirrors `get_checkin_window`'s own "raw value, owning module
        resolves it" split."""
        row = self.get_user(chat_id)
        return row["dashboard_msg_id"] if row is not None else None

    def set_dashboard_msg_id(self, chat_id: str, message_id: str | None) -> None:
        """Upsert, mirrors `set_checkin_window`/`set_last_announced_
        version`'s own shape exactly. `message_id = None` disables
        (`/dashboard off`, or R-D4's self-heal path never calls this with
        `None` -- only the setter does); any other string is the current
        pinned message's id."""
        self._conn.execute(
            "INSERT INTO users (chat_id, dashboard_msg_id) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET dashboard_msg_id = excluded.dashboard_msg_id",
            (chat_id, message_id),
        )
        self._conn.commit()

    # -----------------------------------------------------------------
    # habit_records (SPEC-v1.6.md R-R1, migration 009): one row per
    # (user_id, habit_id, record_type). Stored, not re-derived -- "beaten?"
    # is a cheap compare, and `upsert_record` is the ONE write path
    # `core/records.py:update_on_log` (module `insights`) calls after it
    # has already decided a value strictly exceeds the stored one; this
    # layer does no comparison of its own, mirroring `set_target`'s own
    # "storage just stores" split.
    # -----------------------------------------------------------------

    def get_records(self, user_id: str, habit_id: str | None = None) -> list[sqlite3.Row]:
        """Every record row for `user_id`, optionally filtered to one
        habit -- `core/records.py:render`'s own read path for `/records`
        [habit]`. No rows yet (a fresh user, or a habit that's never
        broken a record) is a normal empty list, not an error."""
        if habit_id is not None:
            return self._conn.execute(
                "SELECT * FROM habit_records WHERE user_id = ? AND habit_id = ? ORDER BY record_type",
                (user_id, habit_id),
            ).fetchall()
        return self._conn.execute(
            "SELECT * FROM habit_records WHERE user_id = ? ORDER BY habit_id, record_type", (user_id,)
        ).fetchall()

    def get_record(self, user_id: str, habit_id: str, record_type: str) -> float | None:
        """The stored value for one `(user_id, habit_id, record_type)`,
        or `None` if that record has never been set -- `update_on_log`'s
        own "is this a new record?" comparison reads through here."""
        row = self._conn.execute(
            "SELECT value FROM habit_records WHERE user_id = ? AND habit_id = ? AND record_type = ?",
            (user_id, habit_id, record_type),
        ).fetchone()
        return float(row["value"]) if row is not None else None

    def upsert_record(self, user_id: str, habit_id: str, record_type: str, value: float, achieved_on: str) -> None:
        """Upsert -- `ON CONFLICT(user_id, habit_id, record_type)` matches
        migration 009's own composite `PRIMARY KEY`. Called only after the
        caller has already confirmed `value` strictly exceeds the
        previous one (R-R2); this method does not re-check that itself,
        mirroring `set_target`'s own "storage just stores" split."""
        self._conn.execute(
            "INSERT INTO habit_records (user_id, habit_id, record_type, value, achieved_on) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, habit_id, record_type) DO UPDATE SET "
            "value = excluded.value, achieved_on = excluded.achieved_on",
            (user_id, habit_id, record_type, value, achieved_on),
        )
        self._conn.commit()

    # -----------------------------------------------------------------
    # user_habits (SPEC-v1.7.md R-G1/R-C1/R-C2, migration 010): a per-user
    # habit definition store, `PRIMARY KEY (user_id, id)` -- an id is only
    # reserved within ONE user's own namespace. Storage-only throughout:
    # `unit_aliases` is a pre-JSON-encoded string the caller
    # (`core/habitdef.py`) already built (mirrors `set_user_quiet_hours`'s
    # own "stores what it's given" convention); validation (R-V1-R-V5) and
    # the archive-vs-hard-delete decision (R-C2) are `core/habitdef.py`'s
    # own concern, not this layer's.
    # -----------------------------------------------------------------

    def add_user_habit(self, user_id: str, row: dict) -> None:
        """Insert one new active `user_habits` row (`archived_at` stays
        NULL by construction -- a freshly created habit is always active).
        `row` carries every non-key column this table defines
        (`id`/`type`/`label_en`/`label_th`/`unit_en`/`unit_th`/`goal`/
        `unit_aliases`, each already validated/normalized by the caller,
        R-C1) -- a missing key defaults to `None` (e.g. `unit_en`/`goal`
        for a text/boolean habit, which R-V2 forbids a unit/goal for)."""
        self._conn.execute(
            "INSERT INTO user_habits (user_id, id, type, label_en, label_th, unit_en, unit_th, goal, unit_aliases) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                row["id"],
                row["type"],
                row["label_en"],
                row["label_th"],
                row.get("unit_en"),
                row.get("unit_th"),
                row.get("goal"),
                row.get("unit_aliases"),
            ),
        )
        self._conn.commit()

    def list_user_habits(self, user_id: str, include_archived: bool = False) -> list[sqlite3.Row]:
        """`user_id`'s own habit rows, insertion order (SQLite's own
        rowid order -- this table has no explicit ordering column).
        Default excludes archived rows (`archived_at IS NULL`) -- this is
        `HabitRegistry.for_user`'s own read path (R-G1: only ACTIVE rows
        join the per-user registry); `include_archived=True` is for
        `/habits`-style "show everything, including what you archived"
        views and R-V1's own "an archived id stays reserved" check."""
        if include_archived:
            return self._conn.execute(
                "SELECT * FROM user_habits WHERE user_id = ? ORDER BY created_at", (user_id,)
            ).fetchall()
        return self._conn.execute(
            "SELECT * FROM user_habits WHERE user_id = ? AND archived_at IS NULL ORDER BY created_at", (user_id,)
        ).fetchall()

    def get_user_habit(self, user_id: str, habit_id: str) -> sqlite3.Row | None:
        """One habit row for `user_id`, active OR archived -- R-V1's own
        "not already used by this user (active or archived)" id-collision
        check reads through here, as does `/delhabit`'s own lookup before
        deciding archive vs. hard-delete."""
        return self._conn.execute(
            "SELECT * FROM user_habits WHERE user_id = ? AND id = ?", (user_id, habit_id)
        ).fetchone()

    def archive_user_habit(self, user_id: str, habit_id: str) -> None:
        """R-C2's soft-delete branch (the habit has history): stamp
        `archived_at`, leaving the row (and its id, and every historical
        `logs` row under that id) intact -- it simply drops out of
        `list_user_habits`' own default (active-only) result, and
        therefore out of `HabitRegistry.for_user`'s registry, on the
        caller's next `provider.invalidate(user_id)` rebuild."""
        self._conn.execute(
            "UPDATE user_habits SET archived_at = datetime('now','localtime') WHERE user_id = ? AND id = ?",
            (user_id, habit_id),
        )
        self._conn.commit()

    def delete_user_habit(self, user_id: str, habit_id: str) -> None:
        """R-C2's hard-delete branch (the habit has no logs at all): remove
        the row outright -- the id is freed, so a re-`/addhabit` with the
        same id is immediately possible (an accidental create is fully
        reversible, per R-C2's own explicit rationale)."""
        self._conn.execute("DELETE FROM user_habits WHERE user_id = ? AND id = ?", (user_id, habit_id))
        self._conn.commit()

    def count_active_user_habits(self, user_id: str) -> int:
        """R-V5's own per-user cap check (`config.habits.max_per_user`,
        default 20) -- active (non-archived) rows only; an archived habit
        no longer counts against the cap."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM user_habits WHERE user_id = ? AND archived_at IS NULL", (user_id,)
        ).fetchone()
        return int(row["n"])

    def count_logs_for(self, user_id: str, habit_id: str) -> int:
        """R-C2's own archive-vs-hard-delete decision input: every `logs`
        row ever written under this `(user_id, habit_id)`, INCLUDING
        already-undone (soft-deleted, `deleted_at IS NOT NULL`) ones --
        deliberately broader than `count()`'s own "today's still-live
        rows" scope above, since even an undone entry is still genuine
        history worth an archive (not a hard-delete) rather than silently
        discarding it. Zero means "never logged at all" -- safe to
        hard-delete and free the id."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM logs WHERE user_id = ? AND category = ?", (user_id, habit_id)
        ).fetchone()
        return int(row["n"])

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

    def all_reminder_times(self) -> list[sqlite3.Row]:
        """SPEC-REFACTOR.md Stage 1 rule 1(a): one whole-table read of
        every stored `user_reminder_times` row, for the parallel S1-B/tick
        track's `run_due_reminders' per-tick bulk resolution
        (`core/reminders.py:_bulk_reminder_time_overrides`, consumed via
        `getattr`-based feature detection so it lights up automatically
        the moment this lands) -- replaces that function's own
        per-(user, habit) `get_reminder_times` calls (U*H reads/tick) with
        exactly 1, the last piece of AC1's <=3-queries-per-idle-tick
        floor. Storage-only, no interpretation, mirrors this file's own
        "raw rows, caller resolves meaning" split (e.g. `list_user_habits`/
        `get_checkin_window`) -- ordering matches `get_reminder_times`'s
        own per-key `ORDER BY time`, with `user_id, habit_id` added so
        rows for the same user+habit stay grouped and sorted for the
        caller's own per-key bucketing."""
        return self._conn.execute(
            "SELECT user_id, habit_id, time FROM user_reminder_times ORDER BY user_id, habit_id, time"
        ).fetchall()

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

    # -----------------------------------------------------------------
    # routines / routine_items (SPEC-v1.8.md R-R1-R-R6, module `routines`,
    # migration 011): a per-user habit-stack store, `PRIMARY KEY (user_id,
    # name)` on `routines` -- a routine name is only reserved within ONE
    # user's own namespace, mirroring `user_habits`' own per-user id
    # scoping. Storage-only throughout: `core/routines.py` is where
    # name normalization (R-R1), item validation (habit-token resolution,
    # unit-lookup value parsing), and the cap check (`config.routines.
    # max_per_user`) all happen -- this layer just stores/reads what it's
    # given, same "storage just stores" split `set_target`/`add_user_habit`
    # already establish.
    # -----------------------------------------------------------------

    def add_routine(self, user_id: str, name: str, items: list[tuple[str, float | None]]) -> None:
        """Insert one new routine + its ordered items, in a single
        transaction (both inserts share one `commit()` -- a routine is
        never left half-written). `items` is `[(habit_id, value), ...]`,
        already validated/resolved by the caller (R-C1) -- `seq` is this
        list's own 0-based index, so `list_routines`/`get_routine` can
        always replay items in creation order. The caller is responsible
        for the R-R1 "not already used by this user" / cap checks before
        calling this -- this method itself performs no validation."""
        self._conn.execute("INSERT INTO routines (user_id, name) VALUES (?, ?)", (user_id, name))
        self._conn.executemany(
            "INSERT INTO routine_items (user_id, name, seq, habit_id, value) VALUES (?, ?, ?, ?, ?)",
            [(user_id, name, seq, habit_id, value) for seq, (habit_id, value) in enumerate(items)],
        )
        self._conn.commit()

    def list_routines(self, user_id: str) -> list[tuple[str, list[tuple[str, float | None]]]]:
        """`user_id`'s own routines, in creation order (`routines.
        created_at`/rowid order -- this table has no explicit ordering
        column, mirrors `list_user_habits`' own "insertion order" note),
        each paired with its own items in `seq` order -- the shape
        `core/routines.py`'s `/routine` list view (R-R2) and `execute_
        routine`'s run path (R-R3) both read through."""
        routine_rows = self._conn.execute(
            "SELECT name FROM routines WHERE user_id = ? ORDER BY created_at, name", (user_id,)
        ).fetchall()
        result: list[tuple[str, list[tuple[str, float | None]]]] = []
        for row in routine_rows:
            name = row["name"]
            item_rows = self._conn.execute(
                "SELECT habit_id, value FROM routine_items WHERE user_id = ? AND name = ? ORDER BY seq",
                (user_id, name),
            ).fetchall()
            result.append((name, [(item["habit_id"], item["value"]) for item in item_rows]))
        return result

    def get_routine(self, user_id: str, name: str) -> list[tuple[str, float | None]] | None:
        """One routine's own items (`seq` order), or `None` if `user_id`
        has no routine by that exact `name` -- R-R5's own isolation seam:
        a name owned by a DIFFERENT user simply isn't found here (the
        query is scoped to `user_id` by construction), so a `routine:run:
        <name>` callback tapped by a non-owning chat resolves to `None`
        the same way a genuinely nonexistent name does -- both are the
        same friendly no-op to the caller."""
        routine_row = self._conn.execute(
            "SELECT 1 FROM routines WHERE user_id = ? AND name = ?", (user_id, name)
        ).fetchone()
        if routine_row is None:
            return None
        item_rows = self._conn.execute(
            "SELECT habit_id, value FROM routine_items WHERE user_id = ? AND name = ? ORDER BY seq",
            (user_id, name),
        ).fetchall()
        return [(item["habit_id"], item["value"]) for item in item_rows]

    def delete_routine(self, user_id: str, name: str) -> bool:
        """Remove a routine + its items (R-R4's own hard-delete -- routines
        have no history to preserve the way a habit's `logs` rows do, so
        there is no soft-delete/archive branch here, unlike `user_habits`).
        Returns whether a routine actually existed to delete -- the caller
        uses this to report `routine_delete_not_found` vs. a real success,
        mirroring `clear_target`'s own "no-op, not an error" shape but
        surfacing the outcome instead of staying silent about it."""
        cursor = self._conn.execute("DELETE FROM routines WHERE user_id = ? AND name = ?", (user_id, name))
        existed = cursor.rowcount > 0
        self._conn.execute("DELETE FROM routine_items WHERE user_id = ? AND name = ?", (user_id, name))
        self._conn.commit()
        return existed

    def count_routines(self, user_id: str) -> int:
        """R-R1's own per-user cap check (`config.routines.max_per_user`,
        default 20)."""
        row = self._conn.execute("SELECT COUNT(*) AS n FROM routines WHERE user_id = ?", (user_id,)).fetchone()
        return int(row["n"])

    # -----------------------------------------------------------------
    # habit_cadence / grace_ledger / pauses (SPEC-v1.9.md §5/§6, migration
    # 012): SHARED read accessors -- `core/streaks.py`'s reworked
    # `compute_streak` calls these (once per invocation, R95's own "loads
    # ... once" rule, not per-day) to decide whether a habit is on the
    # daily or weekly walk, and which dates are NEUTRAL (held). The write
    # side of each table (`set_cadence`/`clear_cadence`, `record_grace`/
    # `grace_used_in_week`, `insert_pause`/`clear_pauses`) is each owning
    # module's OWN later, disjoint edit to this file (M1/M2/M3) -- mirrors
    # `user_habits`'/`routines`' own "shared surface reads, module writes"
    # split is not quite right here (both read+write are eventually in
    # this file), so more precisely: this shared surface owns exactly the
    # READ methods every module's write path needs to already coexist
    # with (the engine must be able to read a cadence/pause/grace row the
    # moment ANY module writes one, without further plumbing changes).
    # -----------------------------------------------------------------

    def get_cadence(self, user_id: str, habit_id: str) -> int | None:
        """The stored weekly cadence (`per_week`) for `(user_id,
        habit_id)`, or `None` if this habit has no cadence row -- `None`
        is what `streaks.compute_streak`/`streak_unit` treat as "daily"
        (R1/R5). A fresh/pre-v1.9 install has zero `habit_cadence` rows,
        so every habit reads `None` here by construction (AC2/AC3's own
        byte-identical gate)."""
        row = self._conn.execute(
            "SELECT per_week FROM habit_cadence WHERE user_id = ? AND habit_id = ?", (user_id, habit_id)
        ).fetchone()
        return int(row["per_week"]) if row is not None else None

    def paused_dates(self, user_id: str, habit_id: str, start: str, end: str) -> set[str]:
        """Every 'YYYY-MM-DD' date in `[start, end]` (inclusive) that an
        active pause covers for `(user_id, habit_id)` -- either a
        habit-scoped `pauses` row or an all-habits row (`habit_id IS
        NULL`, R12). Returned already expanded to individual date strings
        (not left as a start/end range) because `streaks.compute_streak`'s
        backward walk/weekly aggregation both test per-day membership;
        expanding here, once, off however many overlapping pause rows
        exist, is cheaper than re-deriving the range on every day/week of
        the walk. A fresh/pre-v1.9 install (or any user with no active
        pause) has zero matching rows, so this is always the empty set by
        construction (AC2/AC3's own byte-identical gate). Plain
        lexicographic string comparison on the ISO date columns is
        correct across a year boundary (e.g. "2026-12-31" < "2027-01-01"),
        the same convention `logs_between`/`prune_audit` already rely on
        for `ts`."""
        rows = self._conn.execute(
            "SELECT start_date, end_date FROM pauses WHERE user_id = ? AND (habit_id = ? OR habit_id IS NULL) "
            "AND start_date <= ? AND end_date >= ?",
            (user_id, habit_id, end, start),
        ).fetchall()
        dates: set[str] = set()
        for row in rows:
            clamped_start = max(row["start_date"], start)
            clamped_end = min(row["end_date"], end)
            day = date.fromisoformat(clamped_start)
            last = date.fromisoformat(clamped_end)
            while day <= last:
                dates.add(day.isoformat())
                day += timedelta(days=1)
        return dates

    def grace_protected_dates(self, user_id: str, habit_id: str, start: str, end: str) -> set[str]:
        """Every 'YYYY-MM-DD' date in `[start, end]` (inclusive) that the
        nightly `grace.evaluate_grace` job has already bridged for
        `(user_id, habit_id)` (R9) -- these dates are NEUTRAL in the
        streak walk (R2), consistently across every subsequent read
        (review/records/dashboard/summary/heatmap, R9's own "the engine
        treats it as NEUTRAL for every subsequent read" guarantee). A
        fresh/pre-v1.9 install (or a habit whose grace was never
        consumed) has zero matching rows, so this is always the empty set
        by construction (AC2/AC3's own byte-identical gate)."""
        rows = self._conn.execute(
            "SELECT protected_date FROM grace_ledger WHERE user_id = ? AND habit_id = ? "
            "AND protected_date >= ? AND protected_date <= ?",
            (user_id, habit_id, start, end),
        ).fetchall()
        return {row["protected_date"] for row in rows}

    def active_pauses(self, user_id: str) -> list[sqlite3.Row]:
        """Every `pauses` row `user_id` currently owns (habit-scoped or
        all-habits), raw -- no date filtering against "today" happens
        here (storage just stores/returns, mirrors `list_user_habits`'s/
        `get_checkin_window`'s own "raw value, owning module interprets"
        split); `/dashboard`/`/habits`' own R17 rendering (module `pause`)
        is where "does this row cover today" is decided. A resumed pause
        is deleted outright (`clear_pauses`, R13), so a row appearing here
        is always one that hasn't been explicitly ended yet -- it may
        still be naturally expired (past `end_date`) until its owner logs
        in and/or a caller checks coverage."""
        return self._conn.execute("SELECT * FROM pauses WHERE user_id = ?", (user_id,)).fetchall()

    # -----------------------------------------------------------------
    # pauses -- M3's own write region (SPEC-v1.9.md §5/§6, module `pause`,
    # R12/R13). `core/pause.py:execute_pause` is the ONLY caller of
    # `insert_pause` (always paired with a preceding `clear_pauses` for
    # the SAME `(user_id, habit_id)` key -- "extend/replace", not "stack
    # overlapping rows for the same scope", R12's own adversarial-edge
    # resolution for "pausing a habit that's already paused": a second
    # `/pause` for the same habit-or-all scope simply REPLACES the prior
    # window rather than creating a second row or being rejected).
    # `execute_resume` is the only caller of `clear_pauses`.
    # -----------------------------------------------------------------

    def insert_pause(self, user_id: str, habit_id: str | None, start: str, end: str) -> None:
        """Insert one new active pause row -- `habit_id=None` stores SQL
        NULL (R12's "all habits"). Callers needing "replace any existing
        pause for this exact scope" call `clear_pauses(user_id, habit_id)`
        first (this method itself performs no such check -- mirrors
        `add_routine`'s own "storage just stores what it's given, caller
        decides the policy" split)."""
        self._conn.execute(
            "INSERT INTO pauses (user_id, habit_id, start_date, end_date) VALUES (?, ?, ?, ?)",
            (user_id, habit_id, start, end),
        )
        self._conn.commit()

    def clear_pauses(self, user_id: str, habit_id: str | None) -> int:
        """Delete every pause row for `user_id` whose `habit_id` EXACTLY
        matches the key given (`None` matches only a NULL/all-habits row,
        never "any row that happens to cover this habit" -- `habit_id IS
        ?` is SQLite's NULL-safe equality, verified against `= ?`'s own
        NULL-comparison pitfall). R13: `/resume <habit>` therefore deletes
        only a HABIT-SPECIFIC row; it does not split or otherwise touch a
        separately-scoped all-habits row that also happens to cover that
        habit (a resume-one-habit-while-others-stay-paused request when
        only an all-habits pause is active returns `pause_none_active`,
        the literal reading of R13's "deletes ... rows for that habit" --
        see IMPL-v1.9-pause.md's "Known limitations"). Returns the number
        of rows actually deleted, so the caller can tell a real resume
        apart from a no-op (R13's own `pause_none_active` idempotent
        case)."""
        cursor = self._conn.execute("DELETE FROM pauses WHERE user_id = ? AND habit_id IS ?", (user_id, habit_id))
        self._conn.commit()
        return cursor.rowcount

    def truncate_pause(self, user_id: str, habit_id: str | None, new_end_date: str) -> int:
        """Shrink the `end_date` of the pause row for `(user_id, habit_id)`
        down to `new_end_date`, but only if it currently runs LATER than
        that (`end_date > ?`) -- never extends a row, only ever pulls its
        end date backward. Added for `core/pause.py:execute_resume`'s
        early-resume fix (R13/R14 tension, Vera's `TEST-v1.9-pause.md`
        finding 3): a `/resume` fired before a pause's natural `end_date`
        must not retroactively un-protect the already-elapsed portion of
        the window (R14's "held" promise), so `execute_resume` truncates
        the row to end YESTERDAY (still covering every already-elapsed
        paused day) instead of deleting it outright -- `clear_pauses`
        stays the right call only when the row hasn't started accumulating
        protected days yet (its own `start_date` is today or later).
        Returns the number of rows actually shrunk (0 or 1, mirrors
        `clear_pauses`'s own rowcount contract) -- a row already ending on
        or before `new_end_date` (e.g. one already naturally expired) is
        left untouched, not rewritten to the same or a later date."""
        cursor = self._conn.execute(
            "UPDATE pauses SET end_date = ? WHERE user_id = ? AND habit_id IS ? AND end_date > ?",
            (new_end_date, user_id, habit_id, new_end_date),
        )
        self._conn.commit()
        return cursor.rowcount

    # -----------------------------------------------------------------
    # habit_cadence WRITE region (SPEC-v1.9.md R18, module `cadence`, M1's
    # own disjoint edit to this file -- the SHARED read accessor
    # `get_cadence` above is what `streaks.py`'s engine reads; these two
    # methods are the only writer `core/cadence.py:execute_cadence` (and,
    # atomically, `core/habitdef.py:execute_addhabit`'s own `cadence=<N>w`
    # pipe-key write) uses, mirroring `set_target`'s own "storage just
    # stores, the caller already validated" split.
    # -----------------------------------------------------------------

    def set_cadence(self, user_id: str, habit_id: str, per_week: int) -> None:
        """Upsert -- a second `/cadence <habit> <N>` for the same
        user+habit replaces the previous value rather than erroring or
        stacking rows. `ON CONFLICT(user_id, habit_id)` matches migration
        012's `PRIMARY KEY(user_id, habit_id)` on `habit_cadence`."""
        self._conn.execute(
            "INSERT INTO habit_cadence (user_id, habit_id, per_week, created_at) "
            "VALUES (?, ?, ?, datetime('now','localtime')) "
            "ON CONFLICT(user_id, habit_id) DO UPDATE SET per_week = excluded.per_week",
            (user_id, habit_id, per_week),
        )
        self._conn.commit()

    def clear_cadence(self, user_id: str, habit_id: str) -> None:
        """Delete the `habit_cadence` row for `(user_id, habit_id)`, if
        any -- a no-op (not an error) when the habit has no cadence row
        (R1/R5: `get_cadence` then reads `None`, so `compute_streak`/
        `streak_unit` revert to the daily walk on the very next read)."""
        self._conn.execute("DELETE FROM habit_cadence WHERE user_id = ? AND habit_id = ?", (user_id, habit_id))
        self._conn.commit()

    # -----------------------------------------------------------------
    # grace_ledger WRITE region (SPEC-v1.9.md R8/R9, module `grace`, M2's
    # own disjoint edit to this file -- the SHARED read accessor
    # `grace_protected_dates` above is what `streaks.py`'s engine reads;
    # these two methods are the only writer/lookup `core/grace.py:
    # evaluate_grace` uses, mirroring `set_target`'s own "storage just
    # stores, the caller already decided" split (evaluate_grace, not this
    # layer, decides WHETHER a date should be bridged).
    # -----------------------------------------------------------------

    def record_grace(self, user_id: str, habit_id: str, protected_date: str, period_key: str) -> None:
        """Write the one `grace_ledger` row for a bridged date (R9).
        `INSERT OR IGNORE` (not a plain INSERT) makes this idempotent
        against the table's own `PRIMARY KEY (user_id, habit_id,
        protected_date)` -- a second call for a date already bridged
        (e.g. `evaluate_grace` re-run after a restart on the same night)
        writes nothing and raises nothing, matching R10's "sent once,
        never repeated" guarantee at the storage layer too (the caller,
        `evaluate_grace`, additionally short-circuits before ever
        reaching this call for an already-bridged date via
        `grace_protected_dates`, so this idempotency is a belt-and-
        suspenders backstop, not the only guard)."""
        self._conn.execute(
            "INSERT OR IGNORE INTO grace_ledger (user_id, habit_id, protected_date, period_key) "
            "VALUES (?, ?, ?, ?)",
            (user_id, habit_id, protected_date, period_key),
        )
        self._conn.commit()

    def grace_used_in_week(self, user_id: str, habit_id: str, period_key: str) -> bool:
        """R8: has `(user_id, habit_id)` already spent its one grace for
        this ISO week (`period_key`, e.g. `"2026-W35"`)? `evaluate_grace`
        checks this before bridging a second miss in the same week (R11);
        `core/grace.py:grace_status_line` also uses this (indirectly, via
        `grace_protected_dates` over the week's own date range, since it
        needs the actual protected date to display, not just the
        boolean) for the `/habits` balance line."""
        row = self._conn.execute(
            "SELECT 1 FROM grace_ledger WHERE user_id = ? AND habit_id = ? AND period_key = ? LIMIT 1",
            (user_id, habit_id, period_key),
        ).fetchone()
        return row is not None
