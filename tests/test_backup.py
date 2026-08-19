"""Backup/restore tests (ROADMAP.md v0.4.0 "Migrations & Backup/Restore",
shipped as v0.3.0, AC4.3-AC4.5) plus CLI-level checks for --migrate/
--backup/--restore.

All against real on-disk SQLite files (tmp_path) -- no mocks, since sqlite3
file I/O is cheap, reliable, local state, and the whole point of AC4.3/4.4
is real WAL/online-backup-API behavior, which a mock would hide. CLI tests
run the actual `python -m habit_assistant.main` entry point as a subprocess
with cwd set to a scratch tmp dir (mirrors tests/test_cli.py's --seed
pattern) so config.toml's relative db_path/[backup].dir never resolve
against the real repo -- data/habits.db (PID 15804, the live bot) and .env
are never touched by anything in this file.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys

import pytest

from habit_assistant.core.backup import BackupError, backup, prune_backups, restore
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry


def _row_count(db_path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    finally:
        conn.close()


def _all_rows(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT ts, category, value_num, value_text, raw_message, source FROM logs ORDER BY id"
            )
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# AC4.3: backup taken while the source connection is open, mid-WAL-writes --
# copy opens, queries identically, includes the most recent write.
# ---------------------------------------------------------------------------


def test_backup_while_source_connection_open_includes_latest_committed_write(tmp_path):
    db_path = tmp_path / "data" / "habits.db"
    backup_dir = tmp_path / "backups"

    db = Database(db_path)  # source connection stays open for the whole test
    db.insert_log(LogEntry(None, "2026-08-19T08:00:00", "water", 250.0, None, "glass 1", "reply"))
    db.insert_log(LogEntry(None, "2026-08-19T09:00:00", "water", 250.0, None, "glass 2", "reply"))
    db.insert_log(LogEntry(None, "2026-08-19T10:00:00", "water", 500.0, None, "the latest write", "reply"))

    # Confirm we're actually exercising WAL, not a rollback journal.
    assert db._conn.execute("PRAGMA journal_mode;").fetchone()[0].lower() == "wal"

    dest = backup(db_path, backup_dir)  # source connection deliberately still open here

    assert dest.exists()
    assert dest.parent == backup_dir

    # The backup is a point-in-time snapshot: it must contain every write
    # committed before the call, including the most recent one.
    backup_rows = _all_rows(dest)
    source_rows = _all_rows(db_path)
    assert len(backup_rows) == 3
    assert backup_rows == source_rows
    assert backup_rows[-1][4] == "the latest write"

    # A write made *after* the backup call must not leak into the snapshot
    # (proves it's a real snapshot, not a live view over the same file).
    db.insert_log(LogEntry(None, "2026-08-19T11:00:00", "water", 100.0, None, "after the backup", "reply"))
    backup_rows_again = _all_rows(dest)
    assert len(backup_rows_again) == 3
    assert "after the backup" not in [r[4] for r in backup_rows_again]

    # Source DB itself remains fully queryable/correct after backup+the extra write.
    assert _row_count(db_path) == 4
    assert db.water_total_ml("2026-08-19") == 250.0 + 250.0 + 500.0 + 100.0

    db.close()

    # The backup file opens and queries identically through the real
    # Database class (not just raw sqlite3).
    reopened = Database(dest)
    assert reopened.water_total_ml("2026-08-19") == 250.0 + 250.0 + 500.0
    reopened.close()


def test_backup_raises_when_source_db_does_not_exist(tmp_path):
    with pytest.raises(BackupError, match="No database found"):
        backup(tmp_path / "nope.db", tmp_path / "backups")


# ---------------------------------------------------------------------------
# AC4.4: restore swaps atomically, creates an automatic pre-restore backup,
# rejects a corrupt/non-SQLite file with a clear error, leaves the current
# DB untouched.
# ---------------------------------------------------------------------------


def test_restore_swaps_db_and_creates_automatic_pre_restore_backup(tmp_path):
    db_path = tmp_path / "data" / "habits.db"
    backup_dir = tmp_path / "backups"

    # Current live DB has "old" data.
    current = Database(db_path)
    current.insert_log(LogEntry(None, "2026-08-01T09:00:00", "water", 111.0, None, "old data", "reply"))
    current.close()

    # A restorable archive with "new" data, built independently.
    archive_db = Database(tmp_path / "archive.db")
    archive_db.insert_log(LogEntry(None, "2026-08-10T09:00:00", "water", 222.0, None, "new data", "reply"))
    archive_db.insert_log(LogEntry(None, "2026-08-10T10:00:00", "water", 333.0, None, "new data 2", "reply"))
    archive_db.close()
    archive_path = tmp_path / "archive.db"

    assert not backup_dir.exists() or list(backup_dir.glob("*.db")) == []

    result_path = restore(archive_path, db_path, backup_dir)

    assert result_path == db_path
    # Live DB now has the archive's content, not the old content.
    rows = _all_rows(db_path)
    assert [r[4] for r in rows] == ["new data", "new data 2"]

    # An automatic pre-restore backup of the *old* data was taken before the swap.
    pre_restore_backups = list(backup_dir.glob("*.db"))
    assert len(pre_restore_backups) == 1
    assert [r[4] for r in _all_rows(pre_restore_backups[0])] == ["old data"]


def test_restore_rejects_garbage_bytes_and_leaves_db_untouched(tmp_path):
    db_path = tmp_path / "data" / "habits.db"
    backup_dir = tmp_path / "backups"

    current = Database(db_path)
    current.insert_log(LogEntry(None, "2026-08-01T09:00:00", "water", 111.0, None, "must survive", "reply"))
    current.close()
    before_bytes = db_path.read_bytes()

    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a sqlite file, just junk bytes 12345")

    with pytest.raises(BackupError):
        restore(garbage, db_path, backup_dir)

    assert db_path.read_bytes() == before_bytes
    assert [r[4] for r in _all_rows(db_path)] == ["must survive"]
    # Validation failed before anything else happened -- no automatic
    # pre-restore backup should have been triggered.
    assert list(backup_dir.glob("*.db")) == [] if backup_dir.exists() else True


def test_restore_rejects_valid_sqlite_file_missing_logs_table(tmp_path):
    db_path = tmp_path / "data" / "habits.db"
    backup_dir = tmp_path / "backups"

    current = Database(db_path)
    current.insert_log(LogEntry(None, "2026-08-01T09:00:00", "water", 111.0, None, "must survive", "reply"))
    current.close()
    before_bytes = db_path.read_bytes()

    # A genuinely valid SQLite file, but the wrong schema (no `logs` table).
    unrelated = tmp_path / "unrelated.db"
    conn = sqlite3.connect(str(unrelated))
    conn.execute("CREATE TABLE something_else (id INTEGER)")
    conn.commit()
    conn.close()

    with pytest.raises(BackupError, match="logs"):
        restore(unrelated, db_path, backup_dir)

    assert db_path.read_bytes() == before_bytes


def test_restore_rejects_missing_archive_file(tmp_path):
    db_path = tmp_path / "data" / "habits.db"
    backup_dir = tmp_path / "backups"
    current = Database(db_path)
    current.close()

    with pytest.raises(BackupError, match="not found"):
        restore(tmp_path / "does-not-exist.db", db_path, backup_dir)


def test_restore_onto_nonexistent_db_skips_pre_restore_backup(tmp_path):
    """If there is no current DB to protect, restore() must still succeed
    and must not attempt to back up something that doesn't exist."""
    db_path = tmp_path / "data" / "habits.db"  # never created
    backup_dir = tmp_path / "backups"

    archive_db = Database(tmp_path / "archive.db")
    archive_db.insert_log(LogEntry(None, "2026-08-10T09:00:00", "water", 222.0, None, "fresh install", "reply"))
    archive_db.close()

    restore(tmp_path / "archive.db", db_path, backup_dir)

    assert db_path.exists()
    assert [r[4] for r in _all_rows(db_path)] == ["fresh install"]
    assert not backup_dir.exists() or list(backup_dir.glob("*.db")) == []


# ---------------------------------------------------------------------------
# AC4.5: retention pruning honors `retain`, never deletes the newest backup.
# ---------------------------------------------------------------------------


def _make_fake_backup(backup_dir, timestamp: str, content: str = "x") -> None:
    """Create a file matching backup.py's glob pattern
    (habits-<sortable-timestamp>-<suffix>.db) with controlled, distinct
    names so ordering is deterministic without depending on wall-clock
    microsecond spacing between rapid real backup() calls."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"habits-{timestamp}.db"
    path.write_text(content, encoding="utf-8")


def _timestamps(n: int) -> list[str]:
    """n distinct, lexically-sortable fake timestamps, oldest first --
    matches backup.py's fixed-width "%Y%m%dT%H%M%S-ffffff" shape closely
    enough that lexical order == chronological order."""
    return [f"202608{10 + i:02d}T000000-000000" for i in range(n)]


def test_prune_backups_keeps_only_the_newest_retain_count(tmp_path):
    backup_dir = tmp_path / "backups"
    timestamps = _timestamps(6)
    for ts in timestamps:
        _make_fake_backup(backup_dir, ts)

    deleted = prune_backups(backup_dir, retain=3)

    remaining = sorted(p.name for p in backup_dir.glob("*.db"))
    assert len(remaining) == 3
    assert len(deleted) == 3
    # The 3 newest (last 3 in oldest-first order) survive.
    assert remaining == [f"habits-{ts}.db" for ts in timestamps[-3:]]


def test_prune_backups_never_deletes_the_newest_even_at_retain_zero(tmp_path):
    backup_dir = tmp_path / "backups"
    timestamps = _timestamps(4)
    for ts in timestamps:
        _make_fake_backup(backup_dir, ts)

    prune_backups(backup_dir, retain=0)

    remaining = list(backup_dir.glob("*.db"))
    assert len(remaining) == 1
    assert remaining[0].name == f"habits-{timestamps[-1]}.db"  # the newest


def test_prune_backups_never_deletes_the_newest_at_negative_retain(tmp_path):
    backup_dir = tmp_path / "backups"
    for ts in _timestamps(3):
        _make_fake_backup(backup_dir, ts)

    prune_backups(backup_dir, retain=-5)

    remaining = list(backup_dir.glob("*.db"))
    assert len(remaining) == 1


def test_prune_backups_on_empty_or_missing_dir_is_a_noop(tmp_path):
    backup_dir = tmp_path / "backups"
    assert prune_backups(backup_dir, retain=3) == []  # dir doesn't exist yet

    backup_dir.mkdir()
    assert prune_backups(backup_dir, retain=3) == []  # dir exists but empty


def test_backup_auto_prunes_after_each_write_when_retain_given(tmp_path):
    db_path = tmp_path / "data" / "habits.db"
    backup_dir = tmp_path / "backups"
    db = Database(db_path)
    db.insert_log(LogEntry(None, "2026-08-19T08:00:00", "water", 250.0, None, "x", "reply"))

    for _ in range(5):
        backup(db_path, backup_dir, retain=2)

    remaining = list(backup_dir.glob("*.db"))
    assert len(remaining) == 2
    db.close()


# ---------------------------------------------------------------------------
# CLI: --migrate / --backup / --restore, as real subprocesses against an
# isolated cwd (no Telegram secrets needed, mirrors test_cli.py's --seed
# pattern -- config.toml's db_path/[backup].dir resolve relative to cwd).
# ---------------------------------------------------------------------------


def _run_cli(args, cwd, timeout=30):
    return subprocess.run(
        [sys.executable, "-m", "habit_assistant.main", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_cli_migrate_creates_db_and_is_idempotent(tmp_path):
    result1 = _run_cli(["--migrate"], tmp_path)
    assert result1.returncode == 0, f"stdout={result1.stdout!r} stderr={result1.stderr!r}"
    assert "0 ->" in result1.stdout

    db_path = tmp_path / "data" / "habits.db"
    assert db_path.exists()

    result2 = _run_cli(["--migrate"], tmp_path)
    assert result2.returncode == 0
    # Second run: from == to (no-op). stdout also carries INFO log lines
    # (setup_logging streams to stdout), so pick out the "Migrated schema"
    # line specifically rather than assuming it's the whole/first line.
    migrated_lines = [line for line in result2.stdout.splitlines() if line.startswith("Migrated schema ")]
    assert len(migrated_lines) == 1, result2.stdout
    body = migrated_lines[0].removeprefix("Migrated schema ")
    from_v, to_v = (part.strip() for part in body.split("->"))
    assert from_v == to_v


def test_cli_backup_runs_without_telegram_secrets_and_writes_restorable_file(tmp_path):
    seed_result = _run_cli(["--seed"], tmp_path)
    assert seed_result.returncode == 0

    backup_result = _run_cli(["--backup"], tmp_path)
    assert backup_result.returncode == 0, f"stdout={backup_result.stdout!r} stderr={backup_result.stderr!r}"

    backups = list((tmp_path / "data" / "backups").glob("habits-*.db"))
    assert len(backups) == 1

    conn = sqlite3.connect(str(backups[0]))
    try:
        n = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        assert n > 0
    finally:
        conn.close()


def test_cli_restore_without_yes_refuses_and_leaves_db_untouched(tmp_path):
    seed_result = _run_cli(["--seed"], tmp_path)
    assert seed_result.returncode == 0
    backup_result = _run_cli(["--backup"], tmp_path)
    assert backup_result.returncode == 0
    backups = list((tmp_path / "data" / "backups").glob("habits-*.db"))

    db_path = tmp_path / "data" / "habits.db"
    before_bytes = db_path.read_bytes()

    result = _run_cli(["--restore", str(backups[0])], tmp_path)  # no --yes

    assert result.returncode == 1
    assert "--yes" in result.stderr
    assert db_path.read_bytes() == before_bytes


def test_cli_restore_rejects_corrupt_file_leaving_live_db_untouched(tmp_path):
    seed_result = _run_cli(["--seed"], tmp_path)
    assert seed_result.returncode == 0
    db_path = tmp_path / "data" / "habits.db"
    before_bytes = db_path.read_bytes()

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a real sqlite file at all, just junk bytes")

    result = _run_cli(["--restore", str(corrupt), "--yes"], tmp_path)

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert db_path.read_bytes() == before_bytes


def test_cli_restore_valid_backup_with_yes_succeeds(tmp_path):
    seed_result = _run_cli(["--seed"], tmp_path)
    assert seed_result.returncode == 0
    backup_result = _run_cli(["--backup"], tmp_path)
    assert backup_result.returncode == 0
    backups = list((tmp_path / "data" / "backups").glob("habits-*.db"))
    db_path = tmp_path / "data" / "habits.db"

    conn = sqlite3.connect(str(backups[0]))
    try:
        expected_count = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    finally:
        conn.close()

    result = _run_cli(["--restore", str(backups[0]), "--yes"], tmp_path)

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert db_path.exists()
    conn2 = sqlite3.connect(str(db_path))
    try:
        actual_count = conn2.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    finally:
        conn2.close()
    assert actual_count == expected_count
