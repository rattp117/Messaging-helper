"""SPEC-LINE-PORTAL.md §4/§5 (shared surface, admin web portal, branch
`line-version`): `storage/db.py`'s five new/extended helpers -- AC22,
AC24, AC26 -- `recent_audit(offset)`, `audit_total`, `recent_logs_metadata`
(the privacy-critical one, R-AUDIT-3), `monthly_push_history`,
`push_by_user`.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from habit_assistant.storage.db import Database
from habit_assistant.storage.models import AuditEntry, LogEntry

OWNER = "Uowner00000000000000000000000000"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


def _audit(db: Database, n: int) -> None:
    for i in range(n):
        db.insert_audit(
            AuditEntry(
                id=None,
                ts=datetime(2026, 8, 1, 12, i % 60).isoformat(timespec="seconds"),
                user_id=OWNER,
                action="lang_set",
                entity=None,
                old_value=None,
                new_value=str(i),
                source="admin",
            )
        )


# ===========================================================================
# recent_audit(limit, offset) -- pre-existing callers pass no offset.
# ===========================================================================


def test_recent_audit_default_offset_is_backward_compatible(db):
    _audit(db, 5)
    rows = db.recent_audit(3)  # positional, no offset -- existing call shape
    assert [r["new_value"] for r in rows] == ["4", "3", "2"]


def test_recent_audit_offset_paginates_newest_first(db):
    _audit(db, 5)
    page1 = db.recent_audit(2, 0)
    page2 = db.recent_audit(2, 2)
    assert [r["new_value"] for r in page1] == ["4", "3"]
    assert [r["new_value"] for r in page2] == ["2", "1"]


# ===========================================================================
# audit_total()
# ===========================================================================


def test_audit_total_counts_all_rows(db):
    assert db.audit_total() == 0
    _audit(db, 7)
    assert db.audit_total() == 7


# ===========================================================================
# recent_logs_metadata: AC24's own privacy contract.
# ===========================================================================


def _log(db: Database, *, category: str, habit_type: str | None, value_num, value_text, raw_message: str) -> None:
    db.insert_log(
        LogEntry(
            id=None,
            user_id=OWNER,
            ts=datetime(2026, 8, 31, 9, 0).isoformat(timespec="seconds"),
            category=category,
            value_num=value_num,
            value_text=value_text,
            raw_message=raw_message,
            habit_type=habit_type,
        )
    )


def test_recent_logs_metadata_never_includes_raw_message(db):
    _log(db, category="water", habit_type="numeric", value_num=500.0, value_text=None, raw_message="500ml please log this")
    rows = db.recent_logs_metadata(10)
    assert len(rows) == 1
    assert "raw_message" not in rows[0].keys()


def test_recent_logs_metadata_suppresses_value_text_for_text_habit_rows(db):
    """A `habit_type == 'text'` row's `value_text` IS the diary content --
    AC24 forbids it appearing anywhere on the Activity page. The SQL
    itself nulls it out, so no downstream caller can leak it by
    forgetting a habit_type check."""
    _log(db, category="diary", habit_type="text", value_num=None, value_text="today I felt sad and wrote about it", raw_message="today I felt sad and wrote about it")
    rows = db.recent_logs_metadata(10)
    assert len(rows) == 1
    assert rows[0]["value_text"] is None
    assert rows[0]["habit_type"] == "text"


def test_recent_logs_metadata_keeps_value_text_none_for_numeric_habit_rows(db):
    _log(db, category="water", habit_type="numeric", value_num=500.0, value_text=None, raw_message="500ml")
    rows = db.recent_logs_metadata(10)
    assert rows[0]["value_num"] == 500.0
    assert rows[0]["value_text"] is None


def test_recent_logs_metadata_excludes_soft_deleted_rows(db):
    _log(db, category="water", habit_type="numeric", value_num=500.0, value_text=None, raw_message="500ml")
    row = db.recent_logs_metadata(10)[0]
    db.soft_delete(row["id"])
    assert db.recent_logs_metadata(10) == []


def test_recent_logs_metadata_newest_first_and_offset(db):
    for i in range(3):
        _log(db, category="water", habit_type="numeric", value_num=float(i), value_text=None, raw_message=f"{i}ml")
    rows = db.recent_logs_metadata(2, 1)
    assert [r["value_num"] for r in rows] == [1.0, 0.0]


# ===========================================================================
# monthly_push_history / push_by_user
# ===========================================================================


def test_monthly_push_history_empty_deployment_returns_empty_list(db):
    assert db.monthly_push_history() == []


def test_monthly_push_history_sums_across_users_newest_month_first(db):
    db.increment_push("Ua", "2026-06")
    db.increment_push("Ub", "2026-06")
    db.increment_push("Ua", "2026-07")
    rows = db.monthly_push_history()
    assert [(r["yyyymm"], r["total"]) for r in rows] == [("2026-07", 1), ("2026-06", 2)]


def test_monthly_push_history_respects_months_limit(db):
    for month in ("2026-01", "2026-02", "2026-03"):
        db.increment_push("Ua", month)
    rows = db.monthly_push_history(months=2)
    assert [r["yyyymm"] for r in rows] == ["2026-03", "2026-02"]


def test_push_by_user_sorted_descending_by_count(db):
    for _ in range(5):
        db.increment_push("Ua", "2026-08")
    for _ in range(2):
        db.increment_push("Ub", "2026-08")
    db.increment_push("Uc", "2026-08")
    rows = db.push_by_user("2026-08")
    assert [(r["user_id"], r["count"]) for r in rows] == [("Ua", 5), ("Ub", 2), ("Uc", 1)]


def test_push_by_user_no_pushes_this_month_returns_empty(db):
    assert db.push_by_user("2026-09") == []
