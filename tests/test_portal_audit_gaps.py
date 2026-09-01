"""Vera's adversarial probe of module AUDIT (`core/portal/audit.py`)
against SPEC-LINE-PORTAL.md AC22-AC25, dispatched separately from
`tests/test_portal_audit.py` (Luna's own 32 tests, already PASS).

Scope, per Archi's dispatch: privacy is the load-bearing surface for this
module. This file does NOT re-prove what `test_portal_audit.py` already
covers -- it targets the specific gaps Archi asked to be probed:

1. The custom-habit "Value" unit gap Luna self-flagged in
   `IMPL-PORTAL-audit.md` ("Known limitations" -- `HabitRegistry.
   from_config`, not `.for_user`, so a per-user custom habit's Value cell
   never carries a unit).
2. Whether `source="portal"` (the vocabulary gap Luna flagged as
   concurrently fixed by the USERS track's `core/audit.py` edit) actually
   composes end-to-end.
3. An exhaustive sweep for content-leak paths beyond the one hostile
   payload `test_portal_audit.py` already tries -- including a path this
   file discovers is a REAL, pre-existing leak (see the "MAJOR FINDING"
   test near the bottom).
4. Pagination boundaries `test_portal_audit.py` doesn't exercise: a
   multi-page dataset where clamping must land on a page > 1 (not just
   page 1), and the exact-page-size / exact-multiple-of-page-size
   off-by-one boundaries.
5. Identity-gate composition: `test_portal_audit.py` drives `audit.
   register(app, deps)` directly (no `identity_gate`, by its own
   docstring's design); `test_portal_security.py` drives `identity_gate`
   against handlers that aren't the real audit routes. Neither file
   proves the two compose against the REAL `/audit`/`/activity` routes
   through the REAL `PortalServer`. This file does.
6. i18n: Thai column headers specifically (not just page heading/chrome).
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from habit_assistant.config import Config
from habit_assistant.core.portal import audit as audit_module
from habit_assistant.core.portal.server import PortalDeps, PortalServer
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import AuditEntry, LogEntry

OWNER = "Uowner00000000000000000000000000"
MEMBER = "Umember0000000000000000000000000"

EN_CONFIG = Config.model_validate({"i18n": {"language": "en"}})
TH_CONFIG = Config.model_validate({"i18n": {"language": "th"}})


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


def _audit_row(
    db: Database,
    i: int,
    *,
    source: str = "admin",
    user_id: str = OWNER,
    action: str = "lang_set",
    entity: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
    target_user_id: str | None = None,
) -> None:
    db.insert_audit(
        AuditEntry(
            id=None,
            ts=datetime(2026, 8, 1, 0, 0).isoformat(timespec="seconds"),
            user_id=user_id,
            action=action,
            entity=entity,
            old_value=old_value if old_value is not None else ("en" if action == "lang_set" else None),
            new_value=new_value if new_value is not None else str(i),
            source=source,
            target_user_id=target_user_id,
        )
    )


def _log_row(
    db: Database,
    *,
    category: str,
    habit_type: str | None,
    value_num: float | None,
    value_text: str | None,
    raw_message: str,
    user_id: str = OWNER,
) -> None:
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
# 1. CUSTOM-HABIT RENDERING GAP -- confirming Luna's self-flagged limitation.
# ===========================================================================


async def test_activity_custom_habit_value_renders_with_unit_after_integration_fix(aiohttp_client_factory, db):
    """FLIPPED (integration pass, item 3, TEST-PORTAL-audit.md's own
    "custom-habit rendering verdict"): this test used to pin the gap that
    `_format_activity_value` resolved the unit via `HabitRegistry.
    from_config(deps.config)` -- the base registry only, which (per
    `core/habits.py:HabitRegistry.for_user`'s own docstring, SPEC-v1.7.md
    R-G1) never includes a user's per-user custom habits. The integration
    pass threads a per-row `RegistryProvider` (`row["user_id"]`'s own
    `.for_user`) instead, so a custom habit's log now renders its unit
    correctly, same as a base habit."""
    db.add_user_habit(
        MEMBER,
        {
            "id": "pushups",
            "type": "numeric",
            "label_en": "Push-ups",
            "label_th": "วิดพื้น",
            "unit_en": "reps",
            "unit_th": "ครั้ง",
            "goal": None,
            "unit_aliases": json.dumps({}),
        },
    )
    _log_row(db, category="pushups", habit_type="numeric", value_num=20.0, value_text=None, raw_message="20 pushups", user_id=MEMBER)
    app = await _make_app(db, config=EN_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()

    assert "20 reps" in body


async def test_activity_base_habit_value_renders_with_unit_control(aiohttp_client_factory, db):
    """Control for the test above: a BASE (non-custom) habit's value DOES
    get its unit, since `HabitRegistry.from_config` covers `config.habits`
    fine -- proving the gap above is specifically about custom habits,
    not a general unit-lookup failure."""
    _log_row(db, category="water", habit_type="numeric", value_num=500.0, value_text=None, raw_message="500ml", user_id=MEMBER)
    app = await _make_app(db, config=EN_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()
    assert "500 ml" in body


async def test_activity_habit_column_shows_raw_category_id_for_both_base_and_custom(aiohttp_client_factory, db):
    """The "Habit" column is NOT looked up via the registry at all --
    `_render_activity_row` renders `row["category"]` verbatim (escaped).
    So there is no risk of a custom habit showing a WRONG or missing
    label (only the Value column's unit is affected) -- confirming the
    gap above is narrowly scoped to unit suffixing, not identity."""
    db.add_user_habit(
        MEMBER,
        {
            "id": "pushups",
            "type": "numeric",
            "label_en": "Push-ups",
            "label_th": "วิดพื้น",
            "unit_en": "reps",
            "unit_th": "ครั้ง",
            "goal": None,
            "unit_aliases": json.dumps({}),
        },
    )
    _log_row(db, category="pushups", habit_type="numeric", value_num=5.0, value_text=None, raw_message="5", user_id=MEMBER)
    app = await _make_app(db, config=EN_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()
    assert "pushups" in body  # the raw id, exactly as a base habit's id would render


# ===========================================================================
# 2. source="portal" vocabulary composition (the flagged cross-track gap).
# ===========================================================================


async def test_audit_row_source_portal_composes_with_the_now_extended_sources_vocabulary(aiohttp_client_factory, db):
    """`IMPL-PORTAL-audit.md`'s "Known limitations" flagged that
    `core/audit.py:SOURCES` did NOT yet include `"portal"` at the time
    AUDIT was built, and noted a concurrent USERS-track fix was expected.
    Verifying directly against `core/audit.py` (not just this module's
    render path, which never depended on the closed vocabulary anyway --
    `source` renders verbatim regardless): the fix HAS landed."""
    from habit_assistant.core import audit as audit_core

    assert "portal" in audit_core.SOURCES
    assert audit_core.SOURCES == ("command", "nl", "button", "admin", "system", "portal")

    # And the composed behavior on the actual page, unlocalized/verbatim,
    # exactly as it was before the vocabulary fix (this module's render
    # path never needed the fix to work -- confirming Luna's own claim).
    _audit_row(db, 1, source="portal", action="user_approve", target_user_id=MEMBER)
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    assert ">portal<" in body


# ===========================================================================
# 3. PRIVACY EXHAUSTIVE SWEEP.
# ===========================================================================


async def test_activity_unparsed_row_never_leaks_raw_message(aiohttp_client_factory, db):
    """A `category='unparsed'` row (`habit_type=None` per `LogEntry`'s own
    docstring -- "NULL for rows whose category has no known type") still
    carries a real `raw_message` in the `logs` table. `recent_logs_
    metadata`'s SELECT list never includes `raw_message` at all (for ANY
    row, not just text-habit ones) -- confirmed here for the unparsed
    case specifically, since it's the one row shape where `habit_type`
    itself doesn't gate anything and a naive implementation might have
    been tempted to fall back to `raw_message` for display."""
    hostile_raw = "my phone number is 555-0142, don't tell anyone <script>alert(1)</script>"
    _log_row(db, category="unparsed", habit_type=None, value_num=None, value_text=None, raw_message=hostile_raw)
    app = await _make_app(db, config=EN_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()
    assert "555-0142" not in body
    assert "phone number" not in body
    assert "<script>" not in body
    assert "—" in body  # the row still renders (habit_type != "text" -> falls through value_num branch -> None -> em-dash)


async def test_activity_non_text_habit_with_stray_value_text_never_renders_it(aiohttp_client_factory, db):
    """Adversarial/malformed-data case: `recent_logs_metadata`'s SQL only
    NULLs `value_text` when `habit_type = 'text'` (`storage/db.py`'s own
    `CASE WHEN habit_type = 'text' THEN NULL ELSE value_text END`). A row
    with a NON-text `habit_type` that nonetheless has `value_text`
    populated (a data-integrity edge case: a migration artifact, a bug
    in an older write path, or a boolean/duration row that was never
    supposed to carry one) is NOT nulled by that SQL. Proving the render
    path is safe anyway: `_format_activity_value` never reads
    `row["value_text"]` at all -- it only ever reads `habit_type` and
    `value_num`. This is defense-in-depth worth pinning explicitly,
    since it is NOT the documented privacy boundary (the SQL is) and a
    future refactor of `_format_activity_value` could silently reintroduce
    a leak if it ever starts reading `value_text` directly."""
    hostile_text = "SSN 123-45-6789 <img src=x onerror=alert(1)>"
    _log_row(db, category="water", habit_type="numeric", value_num=1.0, value_text=hostile_text, raw_message="1ml")
    app = await _make_app(db, config=EN_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()
    assert "123-45-6789" not in body
    assert "onerror" not in body
    assert hostile_text not in body


async def test_activity_deleted_undo_log_is_hidden_not_shown(aiohttp_client_factory, db):
    """Pinning the actual, spec-silent behavior for a DELETED (undone)
    log: `recent_logs_metadata`'s `WHERE deleted_at IS NULL` excludes it
    entirely -- it never appears on `/activity` at all (not shown with a
    "deleted" marker, not shown at all). Confirms `test_portal_audit.py::
    test_activity_excludes_soft_deleted_rows`'s finding holds even when
    OTHER (non-deleted) rows exist in the same result set, i.e. the
    excluded row doesn't just happen to fall off an otherwise-empty page."""
    _log_row(db, category="water", habit_type="numeric", value_num=111.0, value_text=None, raw_message="111ml")
    deleted_row = db.recent_logs_metadata(10)[0]
    db.soft_delete(deleted_row["id"])
    _log_row(db, category="water", habit_type="numeric", value_num=222.0, value_text=None, raw_message="222ml")
    app = await _make_app(db, config=EN_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()
    assert "222" in body
    assert "111" not in body


async def test_audit_actor_column_escapes_a_hostile_display_name(aiohttp_client_factory, db):
    """Realistic vector (unlike a routine name, which `core/routines.py:
    _NAME_RE` restricts to `[a-z0-9_]+`): a LINE `display_name` is
    arbitrary user-controlled text from the user's own LINE profile, with
    NO shape restriction in this codebase. `_actor_display` returns it
    verbatim; `_render_audit_row` must escape it -- verified here with a
    markup payload in the "Who" column."""
    hostile_name = '<img src=x onerror=alert(1)>Nok'
    db.upsert_user(MEMBER, status="active", display_name=hostile_name)
    _audit_row(db, 1, user_id=MEMBER, action="lang_set")
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    # The RAW tag must never appear (that would be an executable XSS
    # sink); the ESCAPED form appearing as inert text is correct and
    # expected -- `escape()` neutralizes the `<`/`>` that make it a tag,
    # it does not (and should not) scrub the word "onerror" itself.
    assert "<img src=x" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;Nok" in body


async def test_activity_user_column_escapes_a_hostile_display_name(aiohttp_client_factory, db):
    """Same vector as above, on `/activity`'s "User" column (also
    `_actor_display`, reused verbatim per this module's own docstring)."""
    hostile_name = '<img src=x onerror=alert(1)>Nok'
    db.upsert_user(MEMBER, status="active", display_name=hostile_name)
    _log_row(db, category="water", habit_type="numeric", value_num=1.0, value_text=None, raw_message="1ml", user_id=MEMBER)
    app = await _make_app(db, config=EN_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()
    assert "<img src=x" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;Nok" in body


async def test_audit_detail_cell_escapes_a_hostile_entity_and_new_value(aiohttp_client_factory, db):
    """`_detail()` (reused from `core/audit_view.py`) concatenates
    `entity` and `old_value`/`new_value` -- all three are, in general,
    caller-supplied strings (e.g. `entity=name` for `routine_create`/
    `routine_delete`, `core/routines.py:221/391`). Even though today's
    ONLY caller of `routine_create`/`routine_delete` restricts the name
    to `[a-z0-9_]+` (so this exact payload can't reach the DB through
    that path today), `_render_audit_row` has no such restriction of its
    own and must not rely on every future/other capture site upholding
    one -- confirmed here with a direct, realistic "hostile new_value"
    row (the shape Archi's dispatch note asked to probe: "an audit row
    whose new_value is <img src=x onerror=...>")."""
    _audit_row(
        db, 1, action="routine_create", entity="<img src=x onerror=alert(1)>",
        old_value=None, new_value="<script>alert(2)</script>",
    )
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    # The raw executable tags must be absent; their escaped/inert forms
    # (entities standing in for the angle brackets) are the correct,
    # safe rendering and are expected to appear.
    assert "<img src=x onerror" not in body
    assert "<script>alert(2)</script>" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in body


# ===========================================================================
# 3b. MAJOR FINDING -- pre-existing diary-content leak via undo's old_value,
# inherited verbatim by /audit (AC23 requires exact parity with chat /audit,
# so this module correctly reproduces it; the bug is upstream, in
# core/undo_ui.py + core/audit_view.py, not in core/portal/audit.py).
# ===========================================================================


async def test_audit_detail_cell_no_longer_leaks_diary_text_via_undo_old_value(aiohttp_client_factory, db):
    """FLIPPED (integration pass, item 2, MAJOR FINDING fix): this test
    used to prove R-AUDIT-3's stated privacy posture was FALSE on the
    real `/audit` page -- a diary-habit `/undo`'s removed text was
    recorded verbatim into `audit_log.old_value` (`core/undo_ui.py`) and
    rendered unredacted via the shared `core/audit_view.py:_detail()`
    formatter (the same one the chat `/audit` command uses).

    Root cause fixed AT THE WRITE SITE (`core/undo_ui.py:
    _redacted_text_marker`, wired into `send_undo_confirmation`): a
    text-habit undo now records a bilingual-neutral marker + a character
    count, never the raw content. This test now drives the REAL capture
    path end-to-end -- `undo_ui.send_undo_confirmation` (not a hand-built
    `AuditEntry`, which would bypass the fix entirely) -- through to the
    real `/audit` page, proving the fix holds through the actual
    production write path, not just in isolation."""
    from habit_assistant.core import undo_ui
    from habit_assistant.core.habits import HabitRegistry

    diary_text = "I think I'm pregnant and haven't told my husband yet"
    db.insert_log(
        LogEntry(
            id=None, user_id=OWNER, ts=datetime(2026, 8, 31, 9, 0).isoformat(timespec="seconds"),
            category="diary", value_num=None, value_text=diary_text, raw_message=diary_text, habit_type="text",
        )
    )
    row = db.last_log(OWNER)

    class _NoopChannel:
        async def send(self, chat_id: str, text: str, **kwargs) -> None:
            return None

    await undo_ui.send_undo_confirmation(
        db, _NoopChannel(), Config(), lambda: datetime(2026, 8, 31, 9, 5),
        HabitRegistry.from_config(Config()), "en", row,
    )

    app = await _make_app(db, config=EN_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    assert "pregnant" not in body, "the diary content must never reach /audit, even through the real undo capture path"
    assert "[text entry removed]" in body
    assert "(52 chars)" in body  # len(diary_text) == 52


# ===========================================================================
# 4. PAGINATION BOUNDARIES not covered by test_portal_audit.py.
# ===========================================================================


@pytest.mark.parametrize(
    "requested_page,expected_marker,forbidden_marker",
    [
        ("0", "→ 119", "→ 20<"),  # page=0 -> page 1 (newest 50: new_value 119..70)
        ("-1", "→ 119", "→ 20<"),  # page=-1 -> page 1
        ("abc", "→ 119", "→ 20<"),  # non-numeric -> page 1
    ],
)
async def test_audit_invalid_page_values_fall_back_to_page_1_on_a_multi_page_dataset(
    aiohttp_client_factory, db, requested_page, expected_marker, forbidden_marker
):
    """`test_portal_audit.py::test_audit_malformed_page_falls_back_to_page_1`
    only checks `status == 200` for these inputs against a 5-row (single-
    page) dataset -- it can't distinguish "fell back to page 1" from "fell
    back to some other page" because there IS only one page. Re-run
    against a 120-row (3-page) dataset and assert the actual CONTENT
    lands on page 1 specifically."""
    for i in range(120):
        _audit_row(db, i)
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get(f"/audit?page={requested_page}")
    assert resp.status == 200
    body = await resp.text()
    assert expected_marker in body
    assert forbidden_marker not in body


async def test_audit_page_far_beyond_last_clamps_to_the_actual_last_page_not_page_1(aiohttp_client_factory, db):
    """`test_portal_audit.py::test_audit_page_beyond_last_clamps_without_error`
    only proves clamping against a 5-row (1-page) dataset, where "clamp to
    last page" and "clamp to page 1" are indistinguishable. This is the
    off-by-one the dispatch flagged as worth checking directly: with a
    120-row (3-page) dataset, `page=99999` MUST land on page 3 (the
    actual last page, showing the OLDEST 20 rows), not silently reset to
    page 1."""
    for i in range(120):
        _audit_row(db, i)  # i=0 oldest (lowest id) .. i=119 newest (highest id)
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit?page=99999")
    assert resp.status == 200
    body = await resp.text()
    assert "Page 3 of 3" in body or "หน้า 3 จาก 3" in body
    # Page 3 = the oldest 20 rows (new_value "19".."0"); NOT the newest 50.
    assert "→ 19<" in body
    assert "→ 0<" in body
    assert "→ 119" not in body
    assert "→ 20<" not in body
    # Last page: Older suppressed, Newer present (symmetric-suppression rule).
    assert 'href="/audit?page=4"' not in body
    assert 'href="/audit?page=2"' in body


async def test_audit_exactly_page_size_rows_is_a_single_page_both_controls_suppressed(aiohttp_client_factory, db):
    """Off-by-one at the PAGE_SIZE boundary itself: exactly 50 rows (==
    `audit.PAGE_SIZE`) must be `_total_pages(50, 50) == 1`
    (`-(-50 // 50) == 1`), a single page with NEITHER pager control --
    not an accidental empty page 2. `test_portal_audit.py`'s own
    symmetric-suppression test only used 3 rows, which doesn't exercise
    the division boundary at all."""
    for i in range(audit_module.PAGE_SIZE):
        _audit_row(db, i)
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    assert "Page 1 of 1" in body or "หน้า 1 จาก 1" in body
    assert 'href="/audit?page=' not in body  # neither Newer nor Older
    assert "→ 49<" in body  # newest row present
    assert "→ 0<" in body  # oldest row ALSO present -- all 50 fit on the one page


async def test_audit_exactly_two_full_pages_boundary(aiohttp_client_factory, db):
    """100 rows == exactly 2 full pages of 50 (`_total_pages(100, 50) ==
    2`, not 3 -- ceil-division off-by-one in the other direction from the
    test above). Page 2 must show exactly the oldest 50, with Newer
    present and Older suppressed (it IS the last page)."""
    for i in range(100):
        _audit_row(db, i)
    app = await _make_app(db)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit?page=2")
    body = await resp.text()
    assert "Page 2 of 2" in body or "หน้า 2 จาก 2" in body
    assert "→ 49<" in body  # oldest 50 = new_value 49..0
    assert "→ 0<" in body
    assert "→ 50<" not in body  # newest 50 (page 1's rows) absent
    assert 'href="/audit?page=1"' in body  # Newer present
    assert 'href="/audit?page=3"' not in body  # Older suppressed -- last page


async def test_audit_pager_newer_label_text_entirely_absent_on_page_1(aiohttp_client_factory, db):
    """UI.md §3.21: "On page 1 the Newer control is not rendered at all."
    `test_portal_audit.py` checks the href is absent; this checks the
    LABEL TEXT itself never appears either (proving it's not rendered as
    a disabled/greyed control with the href stripped -- it is fully
    absent, exactly as Iris's contract requires)."""
    for i in range(60):
        _audit_row(db, i)
    app = await _make_app(db, config=EN_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    assert "Newer" not in body
    assert "Older" in body  # sanity: the control vocabulary does appear when applicable


async def test_audit_page_beyond_last_on_a_totally_empty_log_shows_empty_state_not_a_pager(aiohttp_client_factory, db):
    """UX.md Screen 6's "Page out of range (AC25)" state is specified for
    a log that HAS rows; the zero-rows case is a distinct, spec-documented
    branch (`_total_pages` special-cases `total_rows <= 0` to `1` so
    there's always a valid page to clamp to, and `handle_audit`'s `if not
    rows` empty-state branch fires before any pager is built). Confirms
    `page=99999` against a genuinely empty audit_log renders the EMPTY
    state (UX.md's "No changes recorded yet.", no pager, no table) --
    not an error, and not a stray "Page 1 of 1" pager row floating above
    nothing."""
    app = await _make_app(db, config=EN_CONFIG)  # no _audit_row calls -- audit_log is empty
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit?page=99999")
    assert resp.status == 200
    body = await resp.text()
    assert "No changes recorded yet." in body
    assert 'class="pager"' not in body
    assert "<table" not in body


# ===========================================================================
# 5. IDENTITY-GATE COMPOSITION -- through the REAL PortalServer, not a
# hand-rolled Application (test_portal_audit.py bypasses identity_gate by
# design; test_portal_security.py doesn't register the real audit routes).
# ===========================================================================


async def test_real_portal_server_headerless_get_audit_is_403_with_no_data(aiohttp_client_factory, db):
    """Builds the ACTUAL `PortalServer` (the same class `core/app.py` will
    wire up at integration time) with `audit.register` as one of its
    modules -- proving `identity_gate` (outermost middleware) really does
    intercept `/audit` before `handle_audit` ever runs, and that the 403
    response carries none of the underlying audit content."""
    _audit_row(db, 1, action="user_approve", target_user_id=MEMBER, new_value="a-very-specific-marker-value")
    deps = _deps(db)
    server = PortalServer(bind_host="127.0.0.1", bind_port=0, deps=deps, modules=[audit_module.register])
    app = server.build_app()
    client = await aiohttp_client_factory(app)

    resp = await client.get("/audit")
    assert resp.status == 403
    body = await resp.text()
    assert "a-very-specific-marker-value" not in body
    assert MEMBER not in body
    assert "<table" not in body
    assert "<style>" not in body  # UI.md Screen 9: the 403 body never carries the portal shell either


async def test_real_portal_server_headerless_get_activity_is_403_with_no_data(aiohttp_client_factory, db):
    _log_row(db, category="water", habit_type="numeric", value_num=777.0, value_text=None, raw_message="777ml")
    deps = _deps(db)
    server = PortalServer(bind_host="127.0.0.1", bind_port=0, deps=deps, modules=[audit_module.register])
    app = server.build_app()
    client = await aiohttp_client_factory(app)

    resp = await client.get("/activity")
    assert resp.status == 403
    body = await resp.text()
    assert "777" not in body
    assert "<table" not in body


async def test_real_portal_server_with_correct_header_reaches_audit_and_activity(aiohttp_client_factory, db):
    """The positive composition proof: identity_gate + audit.register
    together, through the real PortalServer, actually deliver data when
    the header IS correct -- so the 403 tests above are proving a real
    gate, not a route that's simply broken/missing."""
    _audit_row(db, 1, action="user_approve", target_user_id=MEMBER, new_value="reachable-marker")
    deps = _deps(db)
    server = PortalServer(bind_host="127.0.0.1", bind_port=0, deps=deps, modules=[audit_module.register])
    app = server.build_app()
    client = await aiohttp_client_factory(app)

    resp = await client.get("/audit", headers={"Tailscale-User-Login": "owner@example.com"})
    assert resp.status == 200
    body = await resp.text()
    assert "reachable-marker" in body

    resp2 = await client.get("/activity", headers={"Tailscale-User-Login": "owner@example.com"})
    assert resp2.status == 200


async def test_real_portal_server_post_headerless_gate_also_covers_audit_module_routes(aiohttp_client_factory, db):
    """AC20's own "GET and POST alike" -- AUDIT registers no POST routes
    of its own, but the gate must still be the OUTERMOST middleware
    regardless of which module's GET routes happen to be registered
    alongside it (mirrors `test_portal_security.py`'s own proof against
    an unregistered route, extended here to prove it holds with a real
    page module actually mounted)."""
    deps = _deps(db)
    server = PortalServer(bind_host="127.0.0.1", bind_port=0, deps=deps, modules=[audit_module.register])
    app = server.build_app()
    client = await aiohttp_client_factory(app)
    resp = await client.post("/users/approve", data={"chat_id": "Uxxx"})  # not even a route AUDIT owns
    assert resp.status == 403  # gate refuses before 404 route-resolution would ever fire


# ===========================================================================
# 6. i18n -- Thai column headers specifically, and the Activity Thai empty
# state (test_portal_audit.py checks Thai HEADING/chrome, not column th's).
# ===========================================================================


async def test_audit_table_column_headers_render_in_thai(aiohttp_client_factory, db):
    _audit_row(db, 1)
    app = await _make_app(db, config=TH_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    for expected_th in ("เวลา", "ใคร", "อะไร", "รายละเอียด", "แหล่งที่มา"):
        assert f'<th scope="col">{expected_th}</th>' in body


async def test_activity_table_column_headers_render_in_thai(aiohttp_client_factory, db):
    _log_row(db, category="water", habit_type="numeric", value_num=500.0, value_text=None, raw_message="500ml")
    app = await _make_app(db, config=TH_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()
    for expected_th in ("เวลา", "ผู้ใช้", "กิจกรรม", "ค่า", "แหล่งที่มา"):
        assert f'<th scope="col">{expected_th}</th>' in body


async def test_activity_empty_state_renders_in_thai(aiohttp_client_factory, db):
    app = await _make_app(db, config=TH_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/activity")
    body = await resp.text()
    assert "ยังไม่มีการบันทึกกิจกรรม" in body
    assert "หน้านี้แสดงเฉพาะข้อมูลสรุป" in body  # the privacy note, in Thai


async def test_audit_pager_labels_render_in_thai(aiohttp_client_factory, db):
    for i in range(60):
        _audit_row(db, i)
    app = await _make_app(db, config=TH_CONFIG)
    client = await aiohttp_client_factory(app)
    resp = await client.get("/audit")
    body = await resp.text()
    assert "เก่ากว่า" in body  # Older
    assert "ใหม่กว่า" not in body  # Newer -- page 1, suppressed even in Thai
