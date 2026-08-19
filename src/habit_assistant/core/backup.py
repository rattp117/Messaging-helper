"""Backup/restore for the SQLite habit DB (ROADMAP.md v0.3.0 "Migrations &
Backup/Restore"). Stdlib `sqlite3` only -- no new dependency.

`backup()` uses SQLite's online backup API (`sqlite3.Connection.backup`),
which is safe to call while the source DB is open elsewhere in WAL mode:
it snapshots committed pages *and* anything still resident in the WAL, so
the copy includes the most recent writes without requiring the live
process to close its connection or manually checkpoint (AC4.3).

`restore()` validates the candidate file is a readable SQLite DB with the
expected schema *before* touching anything, takes an automatic backup of
the current DB, then atomically swaps files. A corrupt/invalid candidate
is rejected with `BackupError` and the live DB is left untouched (AC4.4).

No channel imports here (SPEC.md §8) — this module only knows about files.
"""

from __future__ import annotations

import datetime
import logging
import shutil
import sqlite3
from pathlib import Path

logger = logging.getLogger("habit_assistant")

BACKUP_PREFIX = "habits-"
BACKUP_SUFFIX = ".db"
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S"

EXPECTED_TABLES = {"logs"}


class BackupError(RuntimeError):
    """Raised when a backup or restore cannot proceed safely. Callers
    should print this and exit non-zero rather than let a raw traceback
    out, mirroring ConfigError's role in config.py."""


def _timestamped_name() -> str:
    now = datetime.datetime.now()
    return f"{BACKUP_PREFIX}{now.strftime(_TIMESTAMP_FORMAT)}-{now.microsecond:06d}{BACKUP_SUFFIX}"


def backup(db_path: str | Path, backup_dir: str | Path, *, retain: int | None = None) -> Path:
    """Write a timestamped, restorable copy of `db_path` into `backup_dir`
    via SQLite's online backup API. Returns the new backup's path.

    Safe to call while `db_path` is open elsewhere (WAL) -- the backup API
    reads a consistent snapshot including WAL-resident pages (AC4.3).

    If `retain` is given, prunes old backups in `backup_dir` down to that
    count immediately after writing (AC4.5).
    """
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)

    if not db_path.exists():
        raise BackupError(f"No database found at {db_path} -- nothing to back up")

    backup_dir.mkdir(parents=True, exist_ok=True)
    dest_path = backup_dir / _timestamped_name()

    source = sqlite3.connect(str(db_path))
    try:
        dest = sqlite3.connect(str(dest_path))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()

    logger.info("Backup written: %s -> %s", db_path, dest_path)

    if retain is not None:
        prune_backups(backup_dir, retain)

    return dest_path


def _validate_sqlite_file(path: Path) -> None:
    """Raise BackupError if `path` isn't a readable SQLite DB with the
    expected schema. Opens read-only so a corrupt/foreign file is never
    modified by the act of checking it."""
    if not path.exists():
        raise BackupError(f"Restore source not found: {path}")
    if path.is_dir():
        raise BackupError(f"Restore source is a directory, not a file: {path}")

    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except (sqlite3.Error, ValueError) as exc:
        raise BackupError(f"Cannot open {path} as SQLite: {exc}") from exc

    try:
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise BackupError(f"{path} is not a valid SQLite database: {exc}") from exc

        if not row or row[0] != "ok":
            raise BackupError(f"{path} failed SQLite integrity check: {row}")

        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        missing = EXPECTED_TABLES - tables
        if missing:
            raise BackupError(
                f"{path} is missing expected table(s): {', '.join(sorted(missing))} "
                "-- doesn't look like a habit_assistant DB"
            )
    finally:
        conn.close()


def restore(
    archive_path: str | Path,
    db_path: str | Path,
    backup_dir: str | Path,
    *,
    retain: int | None = None,
) -> Path:
    """Validate `archive_path`, take an automatic pre-restore backup of the
    current `db_path` (if it exists), then atomically swap `db_path` for
    `archive_path`'s content. Raises BackupError -- leaving `db_path`
    completely untouched -- if `archive_path` is missing, corrupt, or
    lacks the expected schema (AC4.4).
    """
    archive_path = Path(archive_path)
    db_path = Path(db_path)

    _validate_sqlite_file(archive_path)  # raises before anything else happens

    if db_path.exists():
        backup(db_path, backup_dir, retain=retain)

    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Copy into a temp file next to db_path, then atomically rename over it
    # (os.replace / Path.replace is atomic on the same filesystem, both
    # POSIX and Windows/NTFS).
    tmp_path = db_path.with_name(db_path.name + ".restoring")
    shutil.copyfile(archive_path, tmp_path)
    tmp_path.replace(db_path)

    # Drop stale WAL/SHM sidecars from the previous DB generation so a
    # leftover WAL file never shadows the freshly restored content.
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()

    logger.info("Restored %s -> %s (pre-restore backup taken)", archive_path, db_path)
    return db_path


def prune_backups(backup_dir: str | Path, retain: int) -> list[Path]:
    """Delete backups in `backup_dir` beyond the newest `retain` count.
    Never deletes the newest backup, even if `retain <= 0` (AC4.5).
    Returns the list of deleted paths, newest-first-kept ordering."""
    backup_dir = Path(backup_dir)
    if not backup_dir.exists():
        return []

    backups = sorted(
        backup_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"),
        key=lambda p: p.name,
        reverse=True,  # timestamp is zero-padded/fixed-width -> lexical == chronological, newest first
    )
    if not backups:
        return []

    keep_count = max(retain, 1)
    to_delete = backups[keep_count:]
    for path in to_delete:
        path.unlink()
        logger.info("Pruned old backup: %s", path)
    return to_delete
