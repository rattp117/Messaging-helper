"""SPEC-LINE-PORTAL.md §4 R-STATUS-* (module STATUS, admin web portal,
branch `line-version`): `GET /` -- the status/report page (AC8-AC14),
plus the verdict banner and needs-you block UX.md §3/§4 Flow A and UI.md
§3.3/§3.4/§5 Screen 1 describe as "the page opened every morning" (both
compose only data AC8-AC14 already require -- neither is a new datum).

Built on the shared surface from `core/portal/{server,security,layout,
stats}.py` (IMPL-PORTAL-shared.md): `layout.py`'s escaping (`escape`) and
visual-primitive builders (`tile`/`panel`/`bar`/`tag`/`empty`/`dl`/
`td_cell`/`th_col`) are used for every dynamic value that reaches this
page -- the XSS boundary UI.md §9.2 contract 14 requires. `register(app,
deps)` is the one entry point `core/portal/server.py:REGISTERED_MODULES`
calls (R-INT-1); this module owns exactly one route, `GET /`.

Per-panel error handling (SPEC-LINE-PORTAL.md §3.3): each of the three
data-bearing panels (Scheduler, Storage, the quota gauge) is fetched
inside its own `try`/`except` -- a failure there renders that ONE panel
as "unavailable" (keeping its heading) and feeds the verdict computation
(a failed panel is a "Needs a look" trigger, UX.md's own verdict table),
while the rest of the page renders normally. The "recent errors" panel
reads an in-memory ring buffer (no I/O, effectively cannot fail) but is
wrapped the same way for symmetry with the codebase's fail-open
discipline elsewhere (`core/audit_view.py`, `core/health.py`).
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiohttp import web

from habit_assistant import __version__
from habit_assistant.core import i18n, user_prefs
from habit_assistant.core.backup import BACKUP_PREFIX, BACKUP_SUFFIX
from habit_assistant.core.portal import layout

if TYPE_CHECKING:
    from habit_assistant.core.portal.server import PortalDeps

logger = logging.getLogger(__name__)

_ROUTE = "/"
_CURRENT = "status"


# ---------------------------------------------------------------------------
# Small formatting helpers, private to this module. No existing shared
# helper for byte sizes / uptime durations exists elsewhere in the
# codebase to reuse. Month names deliberately stay English-abbreviated in
# BOTH languages, mirroring `core/wrapped.py`'s own documented precedent
# ("no `locale.setlocale`, so `strftime('%B')` always yields an ENGLISH
# month name" -- used as-is in Thai output too, not just English).
# ---------------------------------------------------------------------------


def _localize(dt: datetime, tz_name: str) -> datetime:
    """Mirrors `layout.format_as_of`'s own naive/aware normalization: a
    naive `dt` is treated as already being in `tz_name` (every timestamp
    this module reads -- `RuntimeStats`, a backup file's mtime -- comes
    from the local system clock); an aware one (a real APScheduler
    `next_run_time`) is converted."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(ZoneInfo(tz_name))


def _format_local(dt: datetime, tz_name: str) -> str:
    return _localize(dt, tz_name).strftime("%Y-%m-%d %H:%M")


def _format_ago(dt: datetime, now: datetime, lang: i18n.Language) -> str:
    """UX.md §7's own relative-time note: Thai doesn't pluralize, English
    does -- reuses the `portal_relative_*` keys module USERS added (its
    own docstring invites STATUS/AUDIT to share them)."""
    seconds = max(0, int((now - dt).total_seconds()))
    if seconds < 60:
        return i18n.t("portal_relative_just_now", lang)
    minutes = seconds // 60
    if minutes < 60:
        key = "portal_relative_minute" if minutes == 1 else "portal_relative_minutes"
        return i18n.t(key, lang, n=minutes)
    hours = minutes // 60
    if hours < 24:
        key = "portal_relative_hour" if hours == 1 else "portal_relative_hours"
        return i18n.t(key, lang, n=hours)
    days = hours // 24
    key = "portal_relative_day" if days == 1 else "portal_relative_days"
    return i18n.t(key, lang, n=days)


def _format_uptime(started_at: datetime, now: datetime) -> str:
    seconds = max(0, int((now - started_at).total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _format_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _unavailable_body(lang: i18n.Language) -> str:
    """The exact fallback body `layout.panel_or_unavailable` builds
    internally (UI.md §3.9), reused here as a plain string rather than
    through that helper: this module needs to KNOW a panel failed (to
    feed the verdict computation, UX.md's own "any panel read failed ->
    Needs a look" rule), a signal `panel_or_unavailable` doesn't expose
    to its caller -- only the rendered HTML. Both call sites share the
    same `portal_panel_unavailable`/`portal_panel_unavailable_hint` keys
    from the shared surface, so the rendered markup is identical either
    way."""
    return (
        f'<p class="empty warn">{layout.escape(i18n.t("portal_panel_unavailable", lang))}</p>'
        f'<p class="meta">{layout.escape(i18n.t("portal_panel_unavailable_hint", lang))}</p>'
    )


def _panel_with_id(anchor: str, heading: str, body_html: str) -> str:
    """`layout.panel()` with an added `id`, so the verdict banner can link
    an in-page cause straight at its panel (UX.md Flow A: "an in-page
    anchor for errors/scheduler..."). `layout.panel()` itself has no `id`
    parameter -- this is a one-line local variant, not a shared-file
    change."""
    return f'<section class="panel" id="{layout.escape(anchor)}"><h2>{layout.escape(heading)}</h2>{body_html}</section>'


def _level_tier(levelname: str) -> str:
    if levelname in ("ERROR", "CRITICAL"):
        return "stop"
    if levelname == "WARNING":
        return "warn"
    return ""


# ---------------------------------------------------------------------------
# Tiles (AC8/AC9/AC10) + needs-you banner (UX.md Flow A step 2).
# ---------------------------------------------------------------------------


def _render_tiles(deps: "PortalDeps", lang: i18n.Language, tz_name: str, now: datetime) -> str:
    ollama_state = "on" if deps.config.ollama.enabled else "off"
    uptime = _format_uptime(deps.stats.started_at, now)
    tiles = [
        layout.tile(i18n.t("portal_status_tile_version", lang), __version__),
        layout.tile(i18n.t("portal_status_tile_channel", lang), deps.config.channel.type),
        layout.tile(i18n.t("portal_status_tile_ollama", lang), ollama_state),
        layout.tile(i18n.t("portal_status_tile_uptime", lang), uptime),
    ]
    if deps.stats.last_event_at is None:
        tiles.append(
            layout.tile(i18n.t("portal_status_tile_last_event", lang), i18n.t("portal_status_no_events", lang), wide=True)
        )
    else:
        tiles.append(
            layout.tile(
                i18n.t("portal_status_tile_last_event", lang),
                _format_ago(deps.stats.last_event_at, now, lang),
                small=_format_local(deps.stats.last_event_at, tz_name),
                wide=True,
            )
        )
    return f'<div class="tiles">{"".join(tiles)}</div>'


def _render_needs_you(pending: int, lang: i18n.Language) -> str:
    """UX.md Flow A step 2 / UI.md §3.4: rendered ONLY when pending >= 1 --
    an explicit "0 waiting" is noise."""
    if pending <= 0:
        return ""
    text = i18n.t("portal_status_needs_you", lang, n=pending)
    link_text = i18n.t("portal_status_needs_you_link", lang)
    return f'<a class="needs" href="/users">{layout.escape(text)} <b>{layout.escape(link_text)}</b></a>'


# ---------------------------------------------------------------------------
# Scheduler panel (AC11).
# ---------------------------------------------------------------------------


def _build_scheduler_body(jobs: list, tz_name: str, lang: i18n.Language) -> tuple[str, list[str]]:
    """Returns (body_html, dead_job_ids). UI.md §3.20: a job whose
    `next_run_time is None` renders `<span class="tag word stop">` instead
    of a time -- the datum Maya's verdict table calls "a dead job"."""
    if not jobs:
        return layout.empty(i18n.t("portal_status_scheduler_empty", lang), tier="mute"), []
    dead_jobs: list[str] = []
    rows: list[tuple[str, str]] = []
    for job in jobs:
        if job.next_run_time is None:
            dead_jobs.append(job.id)
            value = layout.tag(i18n.t("portal_status_job_not_scheduled", lang), tier="stop", word=True)
        else:
            value = layout.escape(_format_local(job.next_run_time, tz_name))
        rows.append((job.id, value))
    return layout.dl(rows), dead_jobs


# ---------------------------------------------------------------------------
# Storage panel (AC13).
# ---------------------------------------------------------------------------


def _build_storage_body(deps: "PortalDeps", tz_name: str, lang: i18n.Language) -> str:
    db_path = Path(deps.config.app.db_path)
    db_value = layout.escape(_format_bytes(db_path.stat().st_size))
    sidecars = []
    for suffix in ("wal", "shm"):
        sidecar = db_path.with_name(db_path.name + f"-{suffix}")
        if sidecar.exists():
            sidecars.append((suffix, sidecar.stat().st_size))
    if sidecars:
        note = i18n.t(
            "portal_status_storage_db_sidecars",
            lang,
            wal=next((_format_bytes(sz) for name, sz in sidecars if name == "wal"), "0 B"),
            shm=next((_format_bytes(sz) for name, sz in sidecars if name == "shm"), "0 B"),
        )
        db_value += f"<br><small>{layout.escape(note)}</small>"

    media_dir = Path(deps.config.line.media_dir)
    media_size = sum(f.stat().st_size for f in media_dir.iterdir() if f.is_file()) if media_dir.is_dir() else 0

    backups_dir = Path(deps.config.backup.dir)
    backup_files = (
        sorted(backups_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"), key=lambda p: p.name, reverse=True)
        if backups_dir.is_dir()
        else []
    )

    rows = [
        (i18n.t("portal_status_storage_db", lang), db_value),
        (i18n.t("portal_status_storage_media", lang), layout.escape(_format_bytes(media_size))),
    ]
    if backup_files:
        newest_mtime = datetime.fromtimestamp(backup_files[0].stat().st_mtime)
        rows.append((i18n.t("portal_status_storage_last_backup", lang), layout.escape(_format_local(newest_mtime, tz_name))))
    else:
        rows.append(
            (i18n.t("portal_status_storage_last_backup", lang), layout.escape(i18n.t("portal_status_storage_no_backups", lang)))
        )
    body = layout.dl(rows)

    if backup_files:
        table_rows = "".join(
            "<tr>"
            + layout.td_cell(i18n.t("portal_status_backup_col_file", lang), layout.mono(p.name), head=True)
            + layout.td_cell(i18n.t("portal_status_backup_col_size", lang), layout.escape(_format_bytes(p.stat().st_size)))
            + layout.td_cell(
                i18n.t("portal_status_backup_col_time", lang),
                layout.escape(_format_local(datetime.fromtimestamp(p.stat().st_mtime), tz_name)),
            )
            + "</tr>"
            for p in backup_files
        )
        table_html = (
            "<table class=\"collapse\"><thead><tr>"
            f'{layout.th_col(i18n.t("portal_status_backup_col_file", lang))}'
            f'{layout.th_col(i18n.t("portal_status_backup_col_size", lang))}'
            f'{layout.th_col(i18n.t("portal_status_backup_col_time", lang))}'
            f"</tr></thead><tbody>{table_rows}</tbody></table>"
        )
        summary = i18n.t("portal_status_backups_summary", lang, n=len(backup_files))
        body += f'<details class="more"><summary>{layout.escape(summary)}</summary>{table_html}</details>'

    return body


# ---------------------------------------------------------------------------
# Quota gauge (AC12).
# ---------------------------------------------------------------------------


def _build_gauge(deps: "PortalDeps", lang: i18n.Language, now: datetime) -> tuple[str, float]:
    """Returns (panel_html, pct). R-STATUS-3: cap = `digest.push_cap` in
    realtime mode, else `digest.warn_cap`."""
    yyyymm = now.strftime("%Y-%m")
    used = deps.db.monthly_push_total(yyyymm)  # the one call that can raise (DB hiccup)
    mode = deps.config.digest.mode
    cap = deps.config.digest.push_cap if mode == "realtime" else deps.config.digest.warn_cap
    pct = (used / cap * 100) if cap > 0 else 0.0

    if pct >= 100:
        tier = "stop"
        state_text = i18n.t("portal_status_quota_stop_msg", lang)
    elif pct >= 80:
        tier = "warn"
        state_text = i18n.t("portal_status_quota_warn_msg", lang)
    else:
        tier = "ok"
        state_text = i18n.t("portal_status_quota_normal", lang)

    heading = i18n.t("portal_status_quota_heading", lang, month=layout.format_month_heading(yyyymm))
    line = i18n.t("portal_status_quota_line", lang, used=used, cap=cap, pct=layout.format_pct(pct), mode=mode)
    more_text = i18n.t("portal_status_quota_more", lang)

    html_out = (
        f'<section class="panel gauge {tier}"><h2>{layout.escape(heading)}</h2>'
        f"{layout.bar(pct)}"
        f"<p><b>{layout.escape(line)}</b></p>"
        f'<div class="state"><span>{layout.escape(state_text)}</span>'
        f'<a href="/quota">{layout.escape(more_text)}</a></div>'
        "</section>"
    )
    return html_out, pct


# ---------------------------------------------------------------------------
# Recent errors panel (AC14).
# ---------------------------------------------------------------------------


def _build_errors_body(deps: "PortalDeps", tz_name: str, lang: i18n.Language) -> str:
    records = deps.ring.records()  # newest-first (RingBufferHandler's own contract)
    if not records:
        return (
            f'{layout.empty(i18n.t("portal_status_errors_empty", lang), tier="ok")}'
            f'<p class="meta">{layout.escape(i18n.t("portal_status_errors_empty_note", lang))}</p>'
        )
    note = ""
    if deps.ring.at_capacity():
        note = f'<p class="note">{layout.escape(i18n.t("portal_status_errors_at_capacity", lang, n=deps.ring.capacity))}</p>'
    rows = "".join(
        "<tr>"
        + layout.td_cell(
            i18n.t("portal_status_errors_col_when", lang),
            layout.escape(_format_local(datetime.fromtimestamp(r.created), tz_name)),
        )
        + layout.td_cell(i18n.t("portal_status_errors_col_level", lang), layout.tag(r.levelname, tier=_level_tier(r.levelname)))
        + layout.td_cell(i18n.t("portal_status_errors_col_logger", lang), layout.mono(r.name))
        + layout.td_cell(i18n.t("portal_status_errors_col_message", lang), layout.escape(r.getMessage()), full=True)
        + "</tr>"
        for r in records
    )
    table = (
        "<table class=\"collapse\"><thead><tr>"
        f'{layout.th_col(i18n.t("portal_status_errors_col_when", lang))}'
        f'{layout.th_col(i18n.t("portal_status_errors_col_level", lang))}'
        f'{layout.th_col(i18n.t("portal_status_errors_col_logger", lang))}'
        f'{layout.th_col(i18n.t("portal_status_errors_col_message", lang))}'
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )
    return note + table


# ---------------------------------------------------------------------------
# Verdict (UX.md "The verdict, precisely" table; UI.md §3.3).
# ---------------------------------------------------------------------------


def _verdict_html(tier: str, causes: list[tuple[str, str]], lang: i18n.Language) -> str:
    """`causes` is a same-tier list of (already-localized text, href).
    Deliberately does NOT re-escape the built `headline`/`what` string
    before embedding it in the final `<div>` -- it already contains
    real, deliberately-unescaped `<a>`/`<ul>` markup built from
    pre-escaped pieces, the same "pre-built HTML, caller's job already
    discharged" contract `layout.dl()`'s own `dd` parameter documents."""
    key = "portal_status_verdict_warn" if tier == "warn" else "portal_status_verdict_stop"
    if len(causes) == 1:
        text, href = causes[0]
        what = f'<a href="{layout.escape(href)}">{layout.escape(text)}</a>'
        headline = i18n.t(key, lang, what=what)
        return f'<div class="verdict {tier}"><span>{headline}</span></div>'
    what = i18n.t("portal_status_verdict_multi", lang, n=len(causes))
    headline = i18n.t(key, lang, what=what)
    items = "".join(f'<li><a href="{layout.escape(href)}">{layout.escape(text)}</a></li>' for text, href in causes)
    return f'<div class="verdict {tier}"><span>{headline}</span><ul>{items}</ul></div>'


def _compute_verdict(
    *,
    dead_jobs: list[str],
    quota_pct: float | None,
    ring_nonempty: bool,
    panel_failures: list[tuple[str, str]],
    lang: i18n.Language,
) -> str:
    """UX.md's own precedence: highest severity wins, and only THAT
    tier's causes are named (a lower-severity issue stays visible in its
    own panel, just not in the top banner)."""
    stop_causes: list[tuple[str, str]] = []
    warn_causes: list[tuple[str, str]] = []

    for job_id in dead_jobs:
        stop_causes.append((i18n.t("portal_status_cause_job_dead", lang, job=job_id), "#jobs"))
    if quota_pct is not None and quota_pct >= 100:
        stop_causes.append((i18n.t("portal_status_cause_quota_stopped", lang), "/quota"))

    if ring_nonempty:
        warn_causes.append((i18n.t("portal_status_panel_errors", lang), "#errors"))
    warn_causes.extend(panel_failures)
    if quota_pct is not None and 80 <= quota_pct < 100:
        warn_causes.append((i18n.t("portal_status_cause_quota_warn", lang), "/quota"))

    if stop_causes:
        return _verdict_html("stop", stop_causes, lang)
    if warn_causes:
        return _verdict_html("warn", warn_causes, lang)
    return f'<div class="verdict ok">{layout.escape(i18n.t("portal_status_verdict_ok", lang))}</div>'


# ---------------------------------------------------------------------------
# Handler + registration.
# ---------------------------------------------------------------------------


def _resolve_lang(deps: "PortalDeps") -> i18n.Language:
    """Mirrors `core/access.py:_resolve_unprompted_language_for` exactly:
    the owner's stored `language_pref` (fail-open to "auto" internally,
    `user_prefs.stored_language_pref`'s own contract), resolved against
    `config.i18n` (R-I18N-1/AC31)."""
    pref = user_prefs.stored_language_pref(deps.db, deps.owner_id)
    return i18n.resolve_unprompted_language(deps.config, user_pref=pref)


async def _handle_status(request: web.Request, deps: "PortalDeps") -> web.Response:
    lang = _resolve_lang(deps)
    now = datetime.now()
    tz_name = deps.config.app.timezone
    pending = layout.pending_count(deps.db)

    tiles_html = _render_tiles(deps, lang, tz_name, now)
    needs_you_html = _render_needs_you(pending, lang)

    panel_failures: list[tuple[str, str]] = []

    scheduler_heading = i18n.t("portal_status_panel_scheduler", lang)
    try:
        jobs = deps.scheduler.get_jobs()
        scheduler_body, dead_jobs = _build_scheduler_body(jobs, tz_name, lang)
        scheduler_html = _panel_with_id("jobs", scheduler_heading, scheduler_body)
    except Exception:
        logger.exception("Portal status page: scheduler panel failed to render")
        dead_jobs = []
        panel_failures.append((scheduler_heading, "#jobs"))
        scheduler_html = _panel_with_id("jobs", scheduler_heading, _unavailable_body(lang))

    storage_heading = i18n.t("portal_status_panel_storage", lang)
    try:
        storage_body = _build_storage_body(deps, tz_name, lang)
        storage_html = _panel_with_id("storage", storage_heading, storage_body)
    except Exception:
        logger.exception("Portal status page: storage panel failed to render")
        panel_failures.append((storage_heading, "#storage"))
        storage_html = _panel_with_id("storage", storage_heading, _unavailable_body(lang))

    try:
        gauge_html, pct = _build_gauge(deps, lang, now)
    except Exception:
        logger.exception("Portal status page: quota gauge failed to render")
        pct = None
        panel_failures.append((i18n.t("portal_nav_quota", lang), "/quota"))
        heading = i18n.t("portal_status_quota_heading", lang, month=layout.format_month_heading(now.strftime("%Y-%m")))
        gauge_html = layout.panel(heading, _unavailable_body(lang))

    errors_heading = i18n.t("portal_status_panel_errors", lang)
    try:
        ring_nonempty = len(deps.ring) > 0
        errors_body = _build_errors_body(deps, tz_name, lang)
        errors_html = _panel_with_id("errors", errors_heading, errors_body)
    except Exception:
        logger.exception("Portal status page: recent-errors panel failed to render")
        ring_nonempty = False
        panel_failures.append((errors_heading, "#errors"))
        errors_html = _panel_with_id("errors", errors_heading, _unavailable_body(lang))

    verdict_html = _compute_verdict(
        dead_jobs=dead_jobs,
        quota_pct=pct,
        ring_nonempty=ring_nonempty,
        panel_failures=panel_failures,
        lang=lang,
    )

    cols_html = f'<div class="cols"><div>{gauge_html}{scheduler_html}</div><div>{storage_html}{errors_html}</div></div>'
    body = verdict_html + needs_you_html + tiles_html + cols_html

    as_of = layout.format_as_of(tz_name)
    title = f"{i18n.t('portal_nav_status', lang)} · {layout.BRAND}"
    html_out = layout.page(
        lang=lang,
        title=title,
        current=_CURRENT,
        pending=pending,
        body=body,
        path_qs=request.path_qs,
        tz_name=tz_name,
        as_of=as_of,
    )
    return web.Response(text=html_out, content_type="text/html")


def register(app: web.Application, deps: "PortalDeps") -> None:
    """R-INT-1: the one hook `core/portal/server.py:PortalServer.build_app`
    calls for this module. Closes over `deps` (the same bag every route
    shares, per `PortalDeps`) rather than re-reading `request.app[
    "portal_deps"]`, since `register` is already handed it directly."""

    async def _status(request: web.Request) -> web.Response:
        return await _handle_status(request, deps)

    app.router.add_get(_ROUTE, _status)


__all__ = ["register"]
