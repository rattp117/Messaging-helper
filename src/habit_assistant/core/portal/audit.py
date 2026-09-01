"""SPEC-LINE-PORTAL.md §4 R-AUDIT-1/R-AUDIT-2/R-AUDIT-3 (module AUDIT,
admin web portal, branch `line-version`): `GET /audit?page=N` (paginated
audit-log viewer, AC22/AC23/AC25) and `GET /activity` (recent activity
feed, metadata only, AC24).

Reuses `core/audit_view.py`'s existing row-formatting helpers instead of
duplicating them -- `_ACTION_LABEL_MSG_IDS`/`_action_label` (the
localized action-label lookup), `_actor_display` ("you" for the owner,
else display_name, else raw chat id -- reused for BOTH pages' actor/
"User" column, since UX.md's own wording for each is identical), `_detail`
(entity + old_value -> new_value, the same field set/privacy shape as the
chat `/audit`, AC23), and `_format_ts` (the same "MM-DD HH:MM" compact
timestamp). No import-cycle risk: `core/audit_view.py` imports only
`core/i18n.py` and `core/render_budget.py`, neither of which import
anything from `core/portal/`.

**Privacy (R-AUDIT-3/AC24) is enforced by `db.recent_logs_metadata` itself**
(SPEC-LINE-PORTAL.md §5, shared surface) -- it never SELECTs `raw_message`
at all, and NULLs `value_text` in SQL for `habit_type == 'text'` rows, so
this module structurally cannot render diary content even if it tried
(there is no column to read it from). `tests/test_portal_audit.py` proves
this holds even against a hostile/markup `raw_message` stored on the same
row (the row is fetched via `recent_logs_metadata`, which never surfaces
that column) -- this doubles as the XSS proof, since `escape()` is the one
function every interpolated value in this module passes through (UI.md
§9.2 contract 14, `layout.py`'s own docstring).

**Closed at integration (line/v1.3.0):** `core/audit.py`'s `Source`/
`SOURCES` closed vocabulary now includes `"portal"` (closed by the USERS
track during parallel development, confirmed here) -- `source` still
renders VERBATIM, unlocalized, exactly like every other source value.

Column header / heading / empty-state / pager copy is new (UI.md §7's
microcopy table is this module's own source of truth per the shared
surface's i18n.py docstring) -- added under `portal_audit_*`/
`portal_activity_*`/the two shared `portal_col_*` keys, plus
`portal_pager_*` (this module's own microcopy pass, not the shared
surface's).

**Integration fix (item 3, TEST-PORTAL-audit.md's own "custom-habit
rendering verdict"):** `/activity`'s Value column used to resolve a
numeric row's unit via `HabitRegistry.from_config(deps.config)` -- the
BASE registry only, so a per-user custom habit (SPEC-v1.7.md) never
carried its unit suffix (a correct number, just no "reps"/"ml", since the
category simply wasn't in that registry). `recent_logs_metadata` returns
rows across ALL users, so the fix threads a per-row `RegistryProvider`
(the same cache-per-user shape `core/portal/quota.py`'s own manual-digest
path already builds locally) instead of one shared base-only registry --
each row resolves its OWN user's registry (base + that user's custom
habits), cached within the request so a user appearing in many rows only
pays one registry build.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

from habit_assistant.core import i18n, user_prefs
from habit_assistant.core.audit_view import _action_label, _actor_display, _detail, _format_ts
from habit_assistant.core.portal import layout
from habit_assistant.core.registry_provider import RegistryProvider

if TYPE_CHECKING:
    from habit_assistant.core.portal.server import PortalDeps

logger = logging.getLogger(__name__)

# R-AUDIT-1: page size fixed (SPEC-LINE-PORTAL.md §4's own "default 50").
PAGE_SIZE = 50

# UX.md §8 Q8's resolved default ("single page of the most recent 50, no
# pager in v1"): the Activity feed's own fixed row count.
ACTIVITY_LIMIT = 50


def _owner_language(deps: "PortalDeps") -> i18n.Language:
    """`user_prefs.stored_language_pref` is already fail-open (never
    raises -- defaults to `"auto"` on a missing row or a DB error), so no
    extra try/except is needed here on top of it."""
    pref = user_prefs.stored_language_pref(deps.db, deps.owner_id)
    return i18n.resolve_unprompted_language(deps.config, user_pref=pref)


def _parse_page(request: web.Request) -> int:
    """A missing/non-numeric/non-positive `page` resolves to 1 -- this
    view never 400s on a malformed query string, matching the codebase-
    wide "read-only view never crashes on bad input" discipline
    (`core/audit_view.py`/`core/history_view.py`'s own `_effective_limit`
    precedent)."""
    raw = request.query.get("page", "1")
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return 1
    return page if page >= 1 else 1


def _total_pages(total_rows: int, page_size: int) -> int:
    """AC25: a page beyond the last clamps to the last valid page.
    `total_rows == 0` still yields 1 (a well-formed "page 1 of 1", the
    same page the empty state renders on) rather than 0, so `_parse_page`'s
    output always has a valid page to clamp against."""
    if total_rows <= 0:
        return 1
    return -(-total_rows // page_size)  # ceil division, stdlib-only


def _render_page(request: web.Request, deps: "PortalDeps", lang: i18n.Language, *, current: str, heading: str, body: str) -> web.Response:
    tz_name = deps.config.app.timezone
    html = layout.page(
        lang=lang,
        title=heading,
        current=current,
        pending=layout.pending_count(deps.db),
        body=body,
        path_qs=request.path_qs,
        tz_name=tz_name,
        as_of=layout.format_as_of(tz_name),
    )
    return web.Response(text=html, content_type="text/html")


def _unavailable_body(heading: str, lang: i18n.Language) -> str:
    """UX.md Screen 6/7's own "whole-page, one query" error state: the
    heading survives (an absent heading would read as "this feature
    doesn't exist" -- `layout.py:panel_or_unavailable`'s own reasoning,
    extended here to a page with no `.panel` wrapper), the pager/table
    are replaced by the shared `portal_panel_unavailable` message."""
    return f"<h1>{layout.escape(heading)}</h1>" + layout.empty(i18n.t("portal_panel_unavailable", lang), tier="warn")


# ---------------------------------------------------------------------------
# GET /audit?page=N -- R-AUDIT-1/R-AUDIT-2, AC22/AC23/AC25.
# ---------------------------------------------------------------------------


def _render_pager(lang: i18n.Language, page: int, total_pages: int) -> str:
    """UI.md §3.21: `.btn quiet` Newer/Older links flanking a plain page-
    count `<span>`, labelled by MEANING (Newer/Older) not list direction.
    "On page 1 the Newer control is not rendered at all... a control
    whose only possible outcome is failure is not rendered" (UI.md §3.21,
    citing Maya's UX.md §5 general rule) -- applied here SYMMETRICALLY to
    the last page's Older control too, since Maya's rule is general
    ("A control whose only possible outcome is failure is not rendered",
    UX.md §5), not page-1-specific; UI.md's own text only calls out the
    page-1 case explicitly. Flagging this symmetric extension in
    IMPL-PORTAL-audit.md for Vera/Archi/Iris to confirm."""
    parts = []
    if page > 1:
        parts.append(layout.btn(i18n.t("portal_pager_newer", lang), href=f"/audit?page={page - 1}", quiet=True))
    parts.append(f'<span>{layout.escape(i18n.t("portal_pager_page_of", lang, page=page, total=total_pages))}</span>')
    if page < total_pages:
        parts.append(layout.btn(i18n.t("portal_pager_older", lang), href=f"/audit?page={page + 1}", quiet=True))
    return f'<div class="pager">{"".join(parts)}</div>'


def _render_audit_row(row, *, labels: tuple[str, str, str, str, str], db, owner_id: str, lang: i18n.Language) -> str:
    when_label, who_label, what_label, detail_label, source_label = labels
    detail_text = _detail(row)
    detail_html = f'<span title="{layout.escape(detail_text)}">{layout.escape(detail_text)}</span>'
    cells = (
        layout.td_cell(when_label, layout.escape(_format_ts(row["ts"])), head=True),
        layout.td_cell(who_label, layout.escape(_actor_display(db, row["user_id"], owner_id, lang))),
        layout.td_cell(what_label, layout.escape(_action_label(row["action"], lang))),
        layout.td_cell(detail_label, detail_html, full=True),
        layout.td_cell(source_label, layout.tag(row["source"])),
    )
    return "<tr>" + "".join(cells) + "</tr>"


def _render_audit_table(rows, *, db, owner_id: str, lang: i18n.Language) -> str:
    labels = (
        i18n.t("portal_col_when", lang),
        i18n.t("portal_audit_col_who", lang),
        i18n.t("portal_audit_col_what", lang),
        i18n.t("portal_audit_col_detail", lang),
        i18n.t("portal_col_source", lang),
    )
    head = "<tr>" + "".join(layout.th_col(label) for label in labels) + "</tr>"
    body_rows = "".join(_render_audit_row(row, labels=labels, db=db, owner_id=owner_id, lang=lang) for row in rows)
    return f'<table class="collapse"><thead>{head}</thead><tbody>{body_rows}</tbody></table>'


async def handle_audit(request: web.Request) -> web.Response:
    deps: "PortalDeps" = request.app["portal_deps"]
    lang = _owner_language(deps)
    heading = i18n.t("portal_audit_heading", lang)

    try:
        total = deps.db.audit_total()
        total_pages = _total_pages(total, PAGE_SIZE)
        page = min(_parse_page(request), total_pages)
        rows = deps.db.recent_audit(PAGE_SIZE, (page - 1) * PAGE_SIZE)
    except Exception:
        logger.exception("Audit page failed to read audit_log")
        return _render_page(request, deps, lang, current="audit", heading=heading, body=_unavailable_body(heading, lang))

    if not rows:
        body = f"<h1>{layout.escape(heading)}</h1>" + layout.empty(i18n.t("portal_audit_empty", lang))
        return _render_page(request, deps, lang, current="audit", heading=heading, body=body)

    table = _render_audit_table(rows, db=deps.db, owner_id=deps.owner_id, lang=lang)
    pager = _render_pager(lang, page, total_pages)
    body = f"<h1>{layout.escape(heading)}</h1>{table}{pager}"
    return _render_page(request, deps, lang, current="audit", heading=heading, body=body)


# ---------------------------------------------------------------------------
# GET /activity -- R-AUDIT-3, AC24. No pager in v1 (UX.md §8 Q8).
# ---------------------------------------------------------------------------


def _format_activity_value(row, provider: RegistryProvider, lang: i18n.Language) -> str:
    """UX.md Screen 7 / UI.md §5 Screen 7: a `habit_type == 'text'` row
    renders an em-dash -- "the row exists... but the content is
    deliberately absent", never rendered as if it were merely missing
    data. `value_text` is already NULLed in SQL for these rows (R-AUDIT-3);
    checking `habit_type` here too is defense-in-depth, not the actual
    privacy boundary (that boundary is `db.recent_logs_metadata`'s own
    SQL, which structurally cannot hand this function diary text to
    render even if this check were absent).

    A numeric/duration/boolean value is formatted `"{:g}"` (matches
    `core/audit.py:_stringify_value`'s own number style) with its
    habit's configured unit appended when the category resolves in
    `row["user_id"]`'s OWN registry (`provider.for_user`, integration fix
    item 3 -- base + that user's custom habits, SPEC-v1.7.md) and has a
    unit for `lang` (UX.md's own wireframe: "500 ml", "10 min") -- a
    category that resolves in NEITHER the base config NOR that user's own
    custom habits falls back to the bare number, no unit."""
    if row["habit_type"] == "text":
        return "—"
    value_num = row["value_num"]
    if value_num is None:
        return "—"
    formatted = f"{value_num:g}"
    registry = provider.for_user(row["user_id"])
    habit = registry.get(row["category"])
    if habit is not None:
        unit = habit.unit(lang)
        if unit:
            return f"{formatted} {unit}"
    return formatted


def _render_activity_row(row, *, labels: tuple[str, str, str, str, str], db, owner_id: str, provider: RegistryProvider, lang: i18n.Language) -> str:
    when_label, user_label, habit_label, value_label, source_label = labels
    cells = (
        layout.td_cell(when_label, layout.escape(_format_ts(row["ts"])), head=True),
        layout.td_cell(user_label, layout.escape(_actor_display(db, row["user_id"], owner_id, lang))),
        layout.td_cell(habit_label, layout.escape(row["category"])),
        layout.td_cell(value_label, layout.escape(_format_activity_value(row, provider, lang))),
        layout.td_cell(source_label, layout.tag(row["source"])),
    )
    return "<tr>" + "".join(cells) + "</tr>"


def _render_activity_table(rows, *, db, owner_id: str, provider: RegistryProvider, lang: i18n.Language) -> str:
    labels = (
        i18n.t("portal_col_when", lang),
        i18n.t("portal_activity_col_user", lang),
        i18n.t("portal_activity_col_habit", lang),
        i18n.t("portal_activity_col_value", lang),
        i18n.t("portal_col_source", lang),
    )
    head = "<tr>" + "".join(layout.th_col(label) for label in labels) + "</tr>"
    body_rows = "".join(_render_activity_row(row, labels=labels, db=db, owner_id=owner_id, provider=provider, lang=lang) for row in rows)
    return f'<table class="collapse"><thead>{head}</thead><tbody>{body_rows}</tbody></table>'


async def handle_activity(request: web.Request) -> web.Response:
    deps: "PortalDeps" = request.app["portal_deps"]
    lang = _owner_language(deps)
    heading = i18n.t("portal_activity_heading", lang)
    # UI.md Screen 7: "rendered above the table, ALWAYS" -- the privacy
    # promise is shown regardless of whether the page below it is the
    # table, the empty state, or the read-failure state.
    note = f'<p class="note">{layout.escape(i18n.t("portal_activity_privacy_note", lang))}</p>'

    try:
        rows = deps.db.recent_logs_metadata(ACTIVITY_LIMIT)
    except Exception:
        logger.exception("Activity page failed to read logs")
        body = f"<h1>{layout.escape(heading)}</h1>{note}" + layout.empty(i18n.t("portal_panel_unavailable", lang), tier="warn")
        return _render_page(request, deps, lang, current="activity", heading=heading, body=body)

    if not rows:
        body = f"<h1>{layout.escape(heading)}</h1>{note}" + layout.empty(i18n.t("portal_activity_empty", lang))
        return _render_page(request, deps, lang, current="activity", heading=heading, body=body)

    provider = RegistryProvider(deps.config, deps.db)
    table = _render_activity_table(rows, db=deps.db, owner_id=deps.owner_id, provider=provider, lang=lang)
    body = f"<h1>{layout.escape(heading)}</h1>{note}{table}"
    return _render_page(request, deps, lang, current="activity", heading=heading, body=body)


def register(app: web.Application, deps: "PortalDeps") -> None:
    """R-INT-1: this module's own `register(app, deps)` hook -- appended
    to `core/portal/server.py:REGISTERED_MODULES` by the integration step
    once every parallel module reports PASS (not by this module itself,
    per IMPL-PORTAL-shared.md's own "the integration step appends each
    module's register import" note)."""
    del deps  # both handlers read PortalDeps fresh off request.app -- nothing to close over.
    app.router.add_get("/audit", handle_audit)
    app.router.add_get("/activity", handle_activity)


__all__ = ["register", "handle_audit", "handle_activity", "PAGE_SIZE", "ACTIVITY_LIMIT"]
