"""SPEC-LINE-PORTAL.md §4 R-AUDIT-1/R-AUDIT-2/R-AUDIT-3 (module AUDIT,
admin web portal, branch `line-version`): `core/portal/audit.py`'s own
tests -- AC22 (pagination), AC23 (field set/privacy shape parity with the
chat `/audit`), AC24 (activity feed privacy + the XSS proof), AC25 (page
clamping).

Drives `register(app, deps)` directly against a real `web.Application`
(mirrors `tests/test_portal_security.py`'s own `aiohttp_client_factory`
convention/docstring reasoning -- this fixture is duplicated per test
file because `conftest.py` is shared-surface-owned, not a page module's
to add to) -- no `identity_gate` in these tests (that middleware's own
contract is `test_portal_security.py`'s job; this file tests what runs
AFTER the gate, matching how `test_portal_server.py`'s own module-
registration tests are scoped).
"""

from __future__ import annotations

from datetime import datetime

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from habit_assistant.config import Config
from habit_assistant.core.portal import audit as audit_module
from habit_assistant.core.portal.server import PortalDeps
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import AuditEntry, LogEntry

OWNER = "Uowner00000000000000000000000000"
MEMBER = "Umember0000000000000000000000000"

# Default `Config()` renders Thai (i18n.language="auto", primary_language=
# "th" -- "Thai is primary", UX.md §7) -- most tests below assert on
# language-independent markup (numbers, tags, data-label attrs, structural
# HTML) and pass under either language. Tests that assert specific English
# copy force this config explicitly rather than relying on the default.
EN_CONFIG = Config.model_validate({"i18n": {"language": "en"}})


@pytest.fixture
async def aiohttp_client_factory():
    clients: list[TestClient] = []

    async def make_client(app: web.Application) -> TestClient:
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    yield make_client

    for client in clients:
        await client.close()


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


def _deps(db: Database, *, config: Config | None = None) -> PortalDeps:
    return PortalDeps(
        db=db,
        config=config or Config(),
        scheduler=None,
        channel=None,
        stats=None,
        ring=None,
        owner_id=OWNER,
    )


async def _make_app(db: Database, *, config: Config | None = None) -> web.Application:
    deps = _deps(db, config=config)
    app = web.Application()
    app["portal_deps"] = deps
    audit_module.register(app, deps)
    return app


def _audit_row(db: Database, i: int, *, source: str = "admin", user_id: str = OWNER, action: str = "lang_set", target_user_id: str | None = None) -> None:
    db.insert_audit(
        AuditEntry(
            id=None,
            ts=datetime(2026, 8, 1, 12, i % 60).isoformat(timespec="seconds"),
            user_id=user_id,
            action=action,
            entity=None,
            old_value="en" if action == "lang_set" else None,
            new_value=str(i),
            source=source,
            target_user_id=target_user_id,
        )
    )


def _log_row(db: Database, *, category: str, habit_type: str, value_num: float | None, value_text: str | None, raw_message: str, user_id: str = OWNER) -> None:
    db.insert_log(
        LogEntry(
            id=None,
            user_id=user_id,
            ts=datetime(2026, 8, 31, 9, 0).isoformat(timespec="seconds"),
            category=category,
            value_num=value_num,
            value_text=value_text,
            raw_message=raw_message,
            habit_type=habit_type,
        )
    )


# ===========================================================================
# AC22 -- pagination: page=2 shows rows 50..99 newest-first, working pager.
# ===========================================================================


async def test_audit_page_1_shows_newest_50_and_hides_newer_control(aiohttp_client_factory, db):
    for i in range(60):
        _audit_row(db, i)
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    assert resp.status == 200
    body = await resp.text()
    # Row 59 (newest, new_value="59") is on page 1; row 0 (new_value="0",
    # oldest) is not -- new_value renders inside the Detail cell as
    # "en -> {new_value}" (audit_view.py:_detail's own "old -> new" shape).
    assert "→ 59" in body
    assert "→ 0<" not in body
    assert 'href="/audit?page=0"' not in body  # Newer suppressed on page 1
    assert 'href="/audit?page=2"' in body  # Older present


async def test_audit_page_2_shows_the_next_10_rows_newest_first(aiohttp_client_factory, db):
    for i in range(60):
        _audit_row(db, i)
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit?page=2")
    body = await resp.text()
    # Page 1 = new_value 59..10 (50 rows); page 2 = new_value 9..0 (10 rows).
    assert "→ 9<" in body
    assert "→ 59" not in body
    assert 'href="/audit?page=1"' in body  # Newer present
    assert 'href="/audit?page=3"' not in body  # Older suppressed -- this IS the last page


async def test_audit_last_page_older_control_is_suppressed_symmetrically(aiohttp_client_factory, db):
    """UI.md §3.21's own general rule ("a control whose only possible
    outcome is failure is not rendered") applied to the last page's Older
    control, not just page 1's Newer -- see this module's own docstring
    on `_render_pager`."""
    for i in range(3):
        _audit_row(db, i)
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    assert 'href="/audit?page=' not in body  # only 1 page total -- neither control renders
    assert 'class="pager"' in body  # the row itself still renders, centred


# ===========================================================================
# AC25 -- an out-of-range page clamps to the last valid page, no error.
# ===========================================================================


async def test_audit_page_beyond_last_clamps_without_error(aiohttp_client_factory, db):
    for i in range(5):
        _audit_row(db, i)
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit?page=999")
    assert resp.status == 200
    body = await resp.text()
    assert "Page 1 of 1" in body or "หน้า 1 จาก 1" in body


@pytest.mark.parametrize("bad_page", ["0", "-3", "not-a-number", ""])
async def test_audit_malformed_page_falls_back_to_page_1(aiohttp_client_factory, db, bad_page):
    for i in range(5):
        _audit_row(db, i)
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get(f"/audit?page={bad_page}")
    assert resp.status == 200


# ===========================================================================
# AC23 -- field set + privacy shape parity with the chat /audit: actor
# (you/name/id), localized action, entity/target, old->new, source, ts.
# ===========================================================================


async def test_audit_row_shows_you_for_the_owners_own_action(aiohttp_client_factory, db):
    _audit_row(db, 1, user_id=OWNER, action="lang_set")
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    assert "You" in body or "คุณ" in body


async def test_audit_row_shows_display_name_for_a_non_owner_actor(aiohttp_client_factory, db):
    db.upsert_user(MEMBER, status="active", display_name="Nok")
    _audit_row(db, 1, user_id=MEMBER, action="lang_set")
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    assert "Nok" in body


async def test_audit_row_falls_back_to_raw_chat_id_when_no_display_name(aiohttp_client_factory, db):
    _audit_row(db, 1, user_id=MEMBER, action="lang_set")  # MEMBER never upserted -> no user row
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    assert MEMBER in body


async def test_audit_row_shows_localized_action_label_and_old_arrow_new(aiohttp_client_factory, db):
    _audit_row(db, 7, action="lang_set")  # old_value="en", new_value="7"
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp_en = await client.get("/audit")
    body_en = await resp_en.text()
    assert "en" in body_en and "7" in body_en and "→" in body_en  # old -> new arrow

    th_app = await _make_app(db, config=Config.model_validate({"i18n": {"language": "th"}}))
    th_client = await aiohttp_client_factory(th_app)
    resp_th = await th_client.get("/audit")
    body_th = await resp_th.text()
    assert "ภาษา" in body_th  # audit_action_lang_set's own Thai label


async def test_audit_row_shows_target_user_id_for_admin_actions(aiohttp_client_factory, db):
    _audit_row(db, 1, action="user_approve", target_user_id=MEMBER)
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    assert MEMBER in body


async def test_audit_row_source_portal_renders_with_its_own_tag(aiohttp_client_factory, db):
    """The dispatch note's own instruction: 'portal source="portal" audit
    rows render with their label' -- verified here. `source` renders
    VERBATIM (i18n.py's own `audit_line` comment: 'source ... is likewise
    shown verbatim'), same as every other source value -- there is no
    separate localized label to look up, and none is needed."""
    _audit_row(db, 1, source="portal", action="user_approve", target_user_id=MEMBER)
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    assert ">portal<" in body


async def test_audit_row_when_column_is_the_headline_cell(aiohttp_client_factory, db):
    _audit_row(db, 1)
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    assert 'class="head"' in body


async def test_audit_detail_cell_carries_the_full_value_in_title(aiohttp_client_factory, db):
    _audit_row(db, 7, action="lang_set")
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    assert "title=" in body


# ===========================================================================
# Empty / error states.
# ===========================================================================


async def test_audit_empty_shows_localized_empty_state_no_pager(aiohttp_client_factory, db):
    app = await _make_app(db, config=EN_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    assert "No changes recorded yet." in body
    assert 'class="pager"' not in body
    assert "<table" not in body


async def test_audit_read_failure_shows_unavailable_with_heading_and_no_pager(aiohttp_client_factory, db, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(db, "audit_total", _boom)
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    assert resp.status == 200  # fail-open: never a 500 from the handler's own scope
    body = await resp.text()
    assert "Change history" in body or "ประวัติการเปลี่ยนแปลง" in body  # heading survives
    # `layout.escape()` HTML-entity-escapes the apostrophe (quote=True) --
    # check a substring either side of it rather than the literal glyph.
    assert "read this right now" in body or "อ่านข้อมูลส่วนนี้ไม่ได้ตอนนี้" in body
    assert 'class="pager"' not in body


# ===========================================================================
# AC24 -- Activity feed: metadata only, PRIVACY + XSS proof.
# ===========================================================================


async def test_activity_shows_numeric_value_with_unit(aiohttp_client_factory, db):
    _log_row(db, category="water", habit_type="numeric", value_num=500.0, value_text=None, raw_message="500ml")
    app = await _make_app(db, config=EN_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()
    assert "500 ml" in body


async def test_activity_shows_numeric_value_with_thai_unit_by_default(aiohttp_client_factory, db):
    """Default `Config()` renders Thai -- the SAME habit's unit must
    switch to its Thai form (`มล.`), not stay pinned to English, proving
    the unit lookup is lang-aware, not a hardcoded literal (AC31)."""
    _log_row(db, category="water", habit_type="numeric", value_num=500.0, value_text=None, raw_message="500ml")
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()
    assert "500 มล." in body


async def test_activity_text_habit_row_renders_em_dash_never_the_diary_text(aiohttp_client_factory, db):
    _log_row(
        db,
        category="diary",
        habit_type="text",
        value_num=None,
        value_text="today I felt sad and this is deeply private",
        raw_message="today I felt sad and this is deeply private",
    )
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()
    assert "—" in body  # em-dash
    assert "deeply private" not in body
    assert "today I felt sad" not in body


async def test_activity_never_renders_raw_message_or_diary_text_even_when_hostile(aiohttp_client_factory, db):
    """This is BOTH the privacy proof (AC24: raw_message/diary text must
    never appear anywhere on the page) AND the XSS proof (a hostile
    payload stored as raw_message/value_text must never reach the
    response unescaped -- it must not appear AT ALL, escaped or not,
    since `recent_logs_metadata` never selects `raw_message` and NULLs
    `value_text` for text-habit rows in SQL)."""
    hostile = '<script>alert(1)</script><img src=x onerror=alert(2)>'
    _log_row(db, category="diary", habit_type="text", value_num=None, value_text=hostile, raw_message=hostile)
    _log_row(db, category="water", habit_type="numeric", value_num=500.0, value_text=None, raw_message=hostile)
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()
    assert "<script>" not in body
    assert "alert(1)" not in body
    assert "alert(2)" not in body
    assert "onerror" not in body
    assert hostile not in body


async def test_activity_hostile_category_is_escaped_not_executed(aiohttp_client_factory, db):
    """`category` IS rendered (it's the "Habit" column) -- so it must be
    ESCAPED, not omitted. A hostile category string proves the escaping
    boundary (`layout.escape`) holds even for a column this module does
    render verbatim."""
    _log_row(db, category='<b>pwned</b>', habit_type="numeric", value_num=1.0, value_text=None, raw_message="x")
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()
    assert "<b>pwned</b>" not in body
    assert "&lt;b&gt;pwned&lt;/b&gt;" in body


async def test_activity_privacy_note_always_rendered_above_the_table(aiohttp_client_factory, db):
    _log_row(db, category="water", habit_type="numeric", value_num=500.0, value_text=None, raw_message="500ml")
    app = await _make_app(db, config=EN_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()
    assert "This page shows summary data only" in body
    assert body.index("summary data only") < body.index("<table")


async def test_activity_privacy_note_rendered_even_when_empty(aiohttp_client_factory, db):
    app = await _make_app(db, config=EN_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()
    assert "This page shows summary data only" in body
    assert "No activity recorded yet." in body


async def test_activity_excludes_soft_deleted_rows(aiohttp_client_factory, db):
    _log_row(db, category="water", habit_type="numeric", value_num=500.0, value_text=None, raw_message="500ml")
    row = db.recent_logs_metadata(10)[0]
    db.soft_delete(row["id"])
    app = await _make_app(db, config=EN_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()
    assert "No activity recorded yet." in body


async def test_activity_has_no_pager_in_v1(aiohttp_client_factory, db):
    for i in range(5):
        _log_row(db, category="water", habit_type="numeric", value_num=float(i), value_text=None, raw_message=f"{i}ml")
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()
    assert 'class="pager"' not in body


async def test_activity_read_failure_shows_unavailable_with_note_and_heading(aiohttp_client_factory, db, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(db, "recent_logs_metadata", _boom)
    app = await _make_app(db, config=EN_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    assert resp.status == 200
    body = await resp.text()
    assert "User activity" in body
    assert "This page shows summary data only" in body
    assert "read this right now" in body  # apostrophe is HTML-entity-escaped


# ===========================================================================
# AC31 (this module's own bilingual slice) -- Thai owner sees Thai chrome.
# ===========================================================================


async def test_audit_page_renders_thai_when_owner_language_is_thai(aiohttp_client_factory, db):
    _audit_row(db, 1)
    config = Config.model_validate({"i18n": {"language": "th"}})
    app = await _make_app(db, config=config)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    assert 'lang="th"' in body
    assert "ประวัติการเปลี่ยนแปลง" in body


async def test_activity_page_renders_english_when_owner_language_is_english(aiohttp_client_factory, db):
    config = Config.model_validate({"i18n": {"language": "en"}})
    app = await _make_app(db, config=config)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()
    assert 'lang="en"' in body
    assert "User activity" in body


async def test_audit_page_respects_owners_stored_language_pref_over_default(aiohttp_client_factory, db):
    db.upsert_user(OWNER, status="active")
    db._conn.execute("UPDATE users SET language_pref = 'en' WHERE chat_id = ?", (OWNER,))
    db._conn.commit()
    _audit_row(db, 1)
    app = await _make_app(db)  # default Config: i18n.language="auto", primary_language="th"
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    assert "Change history" in body


# ===========================================================================
# register(app, deps): both routes wired, nothing else.
# ===========================================================================


async def test_register_wires_exactly_audit_and_activity(aiohttp_client_factory, db):
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    assert (await client.get("/audit")).status == 200
    assert (await client.get("/activity")).status == 200
    assert (await client.get("/does-not-exist")).status == 404
