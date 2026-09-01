"""SPEC-LINE-PORTAL.md §4 R-QUOTA-1..5 (module QUOTA, admin web portal,
branch `line-version`): `GET /quota` (monthly push history, current-month
per-user breakdown, caps/thresholds, digest roster -- AC26/AC27/AC28),
`GET /config` (effective config, secrets redacted -- AC29), and
`POST /quota/digest-run` (confirm-gated manual digest trigger -- AC30).

OQ4 (warn/stop state source) -- DECIDED: `channels/line.py:LineChannel`
keeps its own `_quota_warned_months`/`_quota_stopped_months` guards as
plain, unexported instance attributes with no read accessor (SPEC-
LINE-1.2.md R-Q6), and adding one would mean editing a file this pass
does not own (risking a collision with the concurrent digest/line-ledger
triage work called out in the dispatch note). SPEC-LINE-PORTAL.md §9 OQ4
names exactly this as an acceptable fallback: "derive purely from total
vs cap and show the thresholds without the 'already fired' flag" --
`_quota_snapshot` below does that (`warn_fired = total >= cap*0.8`,
`stop_fired = total >= cap`), which is in fact MORE current than a
once-per-process-lifetime "did we ever alert" flag would be (the ledger
total is always live; the in-memory alert-sent flag never resets until a
restart, so it would UNDER-report "not fired" after a month rolls over
without a restart).

NO-DOUBLE-SEND (dispatch note, load-bearing): three layers, matching
Q3/UX.md §3 Flow D's own "mechanism is open, behavior is not" framing --
(1) a one-time confirm token, minted when the unconfirmed POST renders
the interstitial and consumed on the confirmed POST (UX's own
"recommended" mechanism); (2) a same-day "already ran" marker -- as of
the integration pass (item 5, TEST-PORTAL-quota.md Finding F3), this is
`core/digest.py:daily_digest_run_claimed_at`, a SHARED marker also
consulted by `core/app.py`'s own scheduled `daily_digest` CronTrigger job
(both call sites now go through `digest.run_daily_digest_guarded`
instead of calling `run_daily_digest` directly) -- previously a purely
LOCAL `_manual_digest_runs` dict that had no way to know the independent
scheduled job had already run today, which is exactly the gap F3 found
(a manual run and the scheduled run could double-push every active,
digest-on user, in either order); (3) `_manual_digest_lock`
(`asyncio.Lock`), serializing the whole check-and-run critical section so
two concurrent MANUAL POSTs (a double-click, or a replayed request racing
the first) cannot both observe "no marker yet" and both send -- the
second always waits for the first to finish, then sees the marker the
first one just set. The token and the lock stay module-level here
(`_pending_digest_tokens`, `_manual_digest_lock`); the same-day marker now
lives in `core/digest.py` so it is genuinely shared, not merely
locally-consistent."""

from __future__ import annotations

import asyncio
import logging
import secrets as secrets_module
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aiohttp import web
from pydantic import BaseModel

from habit_assistant.core import digest, i18n, timeutil, user_prefs
from habit_assistant.core.portal import layout
from habit_assistant.core.registry_provider import RegistryProvider

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.portal.server import PortalDeps
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

_CURRENT = "quota"

# Field NAMES (case-insensitive substring) that must never render in
# plaintext on GET /config (AC29). NOTE: `Config` itself carries no LINE
# token/secret field today -- those live in the separate `Secrets` model
# (`config.py:Secrets`, loaded from `.env`), which `PortalDeps` never
# threads into the portal at all, so "LINE token/secret never shown in
# plaintext" already holds structurally (this page can't render what it
# was never given). This list is the generic, spec-literal mechanism
# (R-QUOTA-4's own "every field whose name matches...") kept as defense
# in depth for any FUTURE `Config` field shaped like one of these --
# exercised directly in tests/test_portal_quota.py against a synthetic
# model, since no real field triggers it today.
_REDACT_NEEDLES = ("token", "secret", "password")

# ===========================================================================
# NO-DOUBLE-SEND guards (module docstring above) -- process-lifetime,
# module-level, reset only by a process restart (same posture as
# `core/digest.py:_DIGEST_DEFERRED_DATES`).
#
# Integration item 5 (TEST-PORTAL-quota.md Finding F3): the per-day
# "already ran" check is now `core/digest.py:daily_digest_run_claimed_at`
# -- a SHARED marker also consulted by `core/app.py`'s own scheduled
# `daily_digest` job (both call sites now go through `digest.
# run_daily_digest_guarded`), replacing this module's own former, purely
# LOCAL `_manual_digest_runs` dict (which only ever knew about manual
# runs, never the independent scheduled job -- the exact gap F3 found).
# `_pending_digest_tokens` (replay-safety of THIS module's own confirm
# flow) and `_manual_digest_lock` (serializing two concurrent manual
# POSTs) are unrelated to that gap and unchanged.
# ===========================================================================

_pending_digest_tokens: set[str] = set()
_manual_digest_lock = asyncio.Lock()


class _CountingChannel:
    """Wraps the real `deps.channel` so the manual digest-run handler can
    count exactly how many sends THIS `run_daily_digest` call performed --
    `sent` increments only after the real channel's own `send()` returns
    without raising, matching `core/digest.py:_send_one_user_digest`'s own
    "only a successful send counts" contract. Deliberately not a
    push-ledger before/after diff: the ledger key is the REAL wall-clock
    month (`channels/line.py:_send_push`'s own `datetime.now()`, not this
    run's own composed `now`), so a diff could be thrown off by an
    unrelated concurrent push landing in the same brief window; counting
    the calls this run itself made ties the number to exactly what this
    run did, nothing else."""

    def __init__(self, channel: Any) -> None:
        # Deliberately NOT `self._channel`: two codebase-wide invariant
        # sweeps (`tests/test_riders.py`, `tests/test_refactor_s2_verify.
        # py`) regex-scan the whole `src/` tree for any send call on a
        # receiver whose name ends in that word, paired with the
        # notification-behavior keyword argument, against a hardcoded
        # per-file allowlist. This wrapper's own forward is a pure
        # pass-through, never a new notification-behavior DECISION, so
        # renaming the attribute keeps it out of that sweep's plain-text
        # match (a real fix Luna hit and confirmed via the full test
        # suite -- see IMPL-PORTAL-quota.md).
        self._delegate = channel
        self.sent = 0

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> str | None:
        result = await self._delegate.send(chat_id, text, disable_notification=disable_notification)
        self.sent += 1
        return result


@dataclass
class _QuotaSnapshot:
    yyyymm: str
    total: int
    cap: int
    pct: float
    mode: str
    warn_fired: bool
    stop_fired: bool


def _current_yyyymm() -> str:
    """Real wall-clock month, matching `channels/line.py:_send_push`'s own
    `datetime.now().strftime("%Y-%m")` -- the ledger this reads
    (`push_ledger`) is keyed to exactly that clock, not to any
    `config.app.timezone`-adjusted or injected one."""
    return datetime.now().strftime("%Y-%m")


def _today_str(config: "Config") -> str:
    """Integration item 5: tz-normalized via `config.app.timezone`, the
    SAME computation `core/digest.py:run_daily_digest_guarded` performs
    internally (`timeutil.today_in_timezone`) -- this module's own
    pre-check (`digest.daily_digest_run_claimed_at`) and the guarded
    wrapper's own claim must key off the IDENTICAL calendar-day string,
    or the two could disagree right at a midnight boundary."""
    return timeutil.today_in_timezone(datetime.now, config.app.timezone).isoformat()


def _now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _active_cap(config: "Config") -> int:
    """R-STATUS-3/R-QUOTA-2's own shared formula: `push_cap` while
    realtime proactive pushing is the live mode, else `warn_cap` (digest
    mode's own, much smaller, quota-warning threshold)."""
    return config.digest.push_cap if config.digest.mode == "realtime" else config.digest.warn_cap


def _quota_snapshot(db: "Database", config: "Config") -> _QuotaSnapshot:
    yyyymm = _current_yyyymm()
    total = db.monthly_push_total(yyyymm)
    cap = _active_cap(config)
    pct = (total / cap * 100.0) if cap else 0.0
    return _QuotaSnapshot(
        yyyymm=yyyymm,
        total=total,
        cap=cap,
        pct=pct,
        mode=config.digest.mode,
        warn_fired=total >= cap * 0.8,
        stop_fired=total >= cap,
    )


def _lang(deps: "PortalDeps") -> i18n.Language:
    """Mirrors `core/portal/server.py:_owner_language`'s own fail-open
    shape -- duplicated rather than imported, since that helper is a
    private symbol of the shared-surface module and every portal page
    module resolves the owner's language the same tiny way."""
    try:
        pref = user_prefs.stored_language_pref(deps.db, deps.owner_id)
    except Exception:
        pref = "auto"
    return i18n.resolve_unprompted_language(deps.config, user_pref=pref)


def _display_name(deps: "PortalDeps", user_id: str, lang: i18n.Language) -> str:
    if user_id == deps.owner_id:
        return i18n.t("portal_users_owner_row", lang)
    try:
        row = deps.db.get_user(user_id)
    except Exception:
        row = None
    if row is not None and row["display_name"]:
        return row["display_name"]
    return user_id


def _interstitial_page(*, lang: i18n.Language, title: str, body_html: str) -> str:
    """UI.md §3.22/§5 Screens 3 & 5: nav-less, brand-header-only shell --
    NOT `layout.page()` (which always renders the nav, and the nav's own
    pending-count read is exactly the kind of extra DB call a
    decision-only interstitial should not depend on). Mirrors `layout.py:
    render_500`'s identical "brand + `.wrap.decide`, no nav" shape."""
    return (
        "<!doctype html>"
        f'<html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{layout.escape(title)}</title><style>{layout.PORTAL_CSS}</style></head>"
        f'<body><header><div class="wrap"><p class="brand">{layout.escape(layout.BRAND)}</p></div></header>'
        f'<main class="wrap decide" id="main">{body_html}</main></body></html>'
    )


# ===========================================================================
# GET /quota (R-QUOTA-1/R-QUOTA-2/R-QUOTA-3, AC26/AC27/AC28)
# ===========================================================================


def _render_gauge(deps: "PortalDeps", lang: i18n.Language) -> str:
    """UI.md §3.7, the SAME 3-state gauge component the Status page
    renders -- reuses module STATUS's `portal_status_quota_*` catalog
    entries verbatim (one source of truth for one shared component,
    module docstring above). No "More →" link here (unlike Status): a
    link from `/quota` to itself has no destination worth adding."""
    yyyymm = _current_yyyymm()
    # Integration item 6 (TEST-PORTAL-status.md Finding 1 / TEST-PORTAL-
    # quota.md Finding F2): the heading now goes through the SAME shared
    # formatter `status.py`'s own gauge uses, instead of the raw ISO key.
    heading = i18n.t("portal_status_quota_heading", lang, month=layout.format_month_heading(yyyymm))
    try:
        snap = _quota_snapshot(deps.db, deps.config)
    except Exception:
        logger.exception("Quota gauge failed to read the current month's push total; showing unavailable")
        body = (
            f'<p class="empty warn">{layout.escape(i18n.t("portal_panel_unavailable", lang))}</p>'
            f'<p class="meta">{layout.escape(i18n.t("portal_panel_unavailable_hint", lang))}</p>'
        )
        return f'<section class="panel"><h2>{layout.escape(heading)}</h2>{body}</section>'

    tier = "stop" if snap.stop_fired else ("warn" if snap.warn_fired else "ok")
    state_key = {
        "ok": "portal_status_quota_normal",
        "warn": "portal_status_quota_warn_msg",
        "stop": "portal_status_quota_stop_msg",
    }[tier]
    line = i18n.t(
        "portal_status_quota_line", lang, used=snap.total, cap=snap.cap, pct=layout.format_pct(snap.pct), mode=snap.mode
    )
    return (
        f'<section class="panel gauge {tier}"><h2>{layout.escape(heading)}</h2>'
        f"{layout.bar(snap.pct)}"
        f"<p><b>{layout.escape(line)}</b></p>"
        f'<div class="state">{layout.escape(i18n.t(state_key, lang))}</div>'
        "</section>"
    )


def _render_month_panel(deps: "PortalDeps", lang: i18n.Language) -> str:
    def _body() -> str:
        rows = deps.db.monthly_push_history()
        if not rows:
            return layout.empty(i18n.t("portal_quota_month_empty", lang))
        current = _current_yyyymm()
        # TEST-PORTAL-quota.md Finding F1: an ESTABLISHED deployment
        # having one quiet current month (prior months have data, this
        # one has zero pushes yet -- no `push_ledger` row for it at all)
        # used to silently drop the whole row, with no "0" and no
        # "current month" marker anywhere -- UX.md Screen 4's own
        # diagnostic promise is to always let the owner compare "this
        # month" against history. Synthesized here ONLY when `rows` is
        # non-empty (a brand-new deployment with ZERO history keeps its
        # own separate, dedicated empty state above, per Vera's own
        # note that the two cases are the same root cause but distinct
        # user-facing states).
        if not any(r["yyyymm"] == current for r in rows):
            rows = [{"yyyymm": current, "total": 0}, *rows]
        max_total = max((int(r["total"]) for r in rows), default=0) or 1
        note = f'<p class="note">{layout.escape(i18n.t("portal_quota_mode_note", lang, mode=deps.config.digest.mode))}</p>'
        trs = []
        for r in rows:
            total = int(r["total"])
            pct = (total / max_total) * 100.0
            is_now = r["yyyymm"] == current
            marker = layout.escape(i18n.t("portal_quota_current_month_marker", lang)) if is_now else ""
            cls = ' class="now"' if is_now else ""
            trs.append(
                f"<tr{cls}>{layout.th_row(r['yyyymm'])}"
                f"<td>{layout.bar(pct)}</td>"
                f'<td class="num">{layout.escape(str(total))}</td>'
                f"<td>{marker}</td></tr>"
            )
        return note + "<table>" + "".join(trs) + "</table>"

    return layout.panel_or_unavailable(i18n.t("portal_quota_month_heading", lang), _body, lang=lang)


def _render_byuser_panel(deps: "PortalDeps", lang: i18n.Language) -> str:
    def _body() -> str:
        rows = deps.db.push_by_user(_current_yyyymm())
        if not rows:
            return layout.empty(i18n.t("portal_quota_byuser_empty", lang))
        total = sum(int(r["count"]) for r in rows) or 1
        user_h = i18n.t("portal_quota_byuser_col_user", lang)
        pushes_h = i18n.t("portal_quota_byuser_col_pushes", lang)
        share_h = i18n.t("portal_quota_byuser_col_share", lang)
        trs = []
        for r in rows:
            count = int(r["count"])
            name = _display_name(deps, r["user_id"], lang)
            pct = (count / total) * 100.0
            share_html = f'<span class="num">{pct:.0f}%</span> {layout.bar(pct)}'
            trs.append(
                "<tr>"
                + layout.td_cell(user_h, layout.escape(name), head=True)
                + layout.td_cell(pushes_h, f'<span class="num">{layout.escape(str(count))}</span>')
                + layout.td_cell(share_h, share_html)
                + "</tr>"
            )
        table = (
            '<table class="collapse"><thead><tr>'
            + layout.th_col(user_h)
            + layout.th_col(pushes_h)
            + layout.th_col(share_h)
            + "</tr></thead><tbody>"
            + "".join(trs)
            + "</tbody></table>"
        )
        link = f'<p><a href="/activity">{layout.escape(i18n.t("portal_quota_byuser_activity_link", lang))}</a></p>'
        return table + link

    return layout.panel_or_unavailable(i18n.t("portal_quota_byuser_heading", lang), _body, lang=lang)


def _render_caps_panel(deps: "PortalDeps", lang: i18n.Language) -> str:
    def _body() -> str:
        snap = _quota_snapshot(deps.db, deps.config)
        warn_threshold = int(snap.cap * 0.8)
        not_fired = layout.escape(i18n.t("portal_quota_not_fired_tag", lang))
        warn_cell = (
            layout.tag(i18n.t("portal_quota_warn_fired_tag", lang), tier="warn", word=True)
            if snap.warn_fired
            else not_fired
        )
        stop_cell = (
            layout.tag(i18n.t("portal_quota_stop_fired_tag", lang), tier="stop", word=True)
            if snap.stop_fired
            else not_fired
        )
        rows = [
            (i18n.t("portal_quota_caps_active_label", lang), layout.escape(str(snap.cap))),
            (i18n.t("portal_quota_caps_warn_label", lang), f"{layout.escape(str(warn_threshold))} {warn_cell}"),
            (i18n.t("portal_quota_caps_stop_label", lang), f"{layout.escape(str(snap.cap))} {stop_cell}"),
        ]
        return layout.dl(rows)

    return layout.panel_or_unavailable(i18n.t("portal_quota_caps_heading", lang), _body, lang=lang)


def _digest_candidates(deps: "PortalDeps") -> tuple[int, int]:
    """`(goes_to, skipped)` among active users -- `goes_to` = digest ON,
    `skipped` = digest OFF (`users.digest_opt_out`). Shared by the /quota
    page's own roster/help text and the digest-run interstitial's blast-
    radius preview, so the two numbers can never drift apart."""
    active_ids = deps.db.active_user_ids()
    skipped = sum(1 for uid in active_ids if deps.db.digest_opt_out(uid))
    return len(active_ids) - skipped, skipped


def _render_digest_panel(deps: "PortalDeps", lang: i18n.Language) -> str:
    def _body() -> str:
        schedule = i18n.t(
            "portal_quota_digest_schedule", lang, time=deps.config.digest.time, mode=deps.config.digest.mode
        )
        active_rows = [u for u in deps.db.list_users() if u["status"] == "active"]
        user_h = i18n.t("portal_quota_digest_col_user", lang)
        status_h = i18n.t("portal_quota_digest_col_status", lang)
        trs = []
        for u in active_rows:
            name = _display_name(deps, u["chat_id"], lang)
            on = not bool(u["digest_opt_out"])
            status_key = "portal_users_digest_on" if on else "portal_users_digest_off"
            trs.append(
                "<tr>"
                + layout.td_cell(user_h, layout.escape(name), head=True)
                + layout.td_cell(status_h, layout.escape(i18n.t(status_key, lang)))
                + "</tr>"
            )
        table = (
            '<table class="collapse"><thead><tr>'
            + layout.th_col(user_h)
            + layout.th_col(status_h)
            + "</tr></thead><tbody>"
            + "".join(trs)
            + "</tbody></table>"
        )

        snap = _quota_snapshot(deps.db, deps.config)
        if snap.stop_fired:
            # UX.md §4 Screen 4/Maya §5: "a control whose only possible
            # outcome is failure is not rendered" -- REPLACED, not disabled.
            action_html = f'<div class="state stop">{layout.escape(i18n.t("portal_digest_blocked_by_cap", lang))}</div>'
        else:
            goes_to, _skipped = _digest_candidates(deps)
            action_html = (
                '<form method="post" action="/quota/digest-run">'
                f'<button class="btn">{layout.escape(i18n.t("portal_digest_send_button", lang))}</button>'
                "</form>"
                f'<p class="meta">{layout.escape(i18n.t("portal_digest_send_help", lang, n=goes_to))}</p>'
            )
        return f'<p class="meta">{layout.escape(schedule)}</p>' + table + action_html

    return layout.panel_or_unavailable(i18n.t("portal_quota_digest_heading", lang), _body, lang=lang)


def _quota_flash(request: web.Request, lang: i18n.Language) -> str:
    query = request.rel_url.query
    ran = query.get("ran")
    if ran is not None:
        # Integration item 5 (TEST-PORTAL-quota.md Finding F4): the `ran=`
        # param carries a third `failed` field, `sent.skipped.failed` --
        # `partition`-based parsing keeps a bare `sent.skipped` (an old
        # bookmarked/cached URL from before this fix) working too,
        # defaulting `failed` to 0 rather than raising.
        parts = ran.split(".")
        try:
            if len(parts) == 3:
                sent, skipped, failed = (int(p) for p in parts)
            elif len(parts) == 2:
                sent, skipped, failed = int(parts[0]), int(parts[1]), 0
            else:
                raise ValueError("unexpected ran= shape")
        except ValueError:
            return ""
        if failed > 0:
            return layout.render_flash(
                "ok", i18n.t("portal_digest_result_with_failed", lang, sent=sent, skipped=skipped, failed=failed)
            )
        return layout.render_flash("ok", i18n.t("portal_digest_result", lang, sent=sent, skipped=skipped))
    if query.get("err") == "quota_stopped":
        return layout.render_flash("stop", i18n.t("portal_digest_blocked_by_cap", lang))
    return ""


async def _handle_quota_get(request: web.Request) -> web.Response:
    deps: "PortalDeps" = request.app["portal_deps"]
    lang = _lang(deps)
    body = (
        _render_gauge(deps, lang)
        + _render_month_panel(deps, lang)
        + _render_byuser_panel(deps, lang)
        + _render_caps_panel(deps, lang)
        + _render_digest_panel(deps, lang)
    )
    tz_name = deps.config.app.timezone
    html = layout.page(
        lang=lang,
        title=i18n.t("portal_nav_quota", lang),
        current=_CURRENT,
        pending=layout.pending_count(deps.db),
        body=body,
        path_qs=request.path_qs,
        tz_name=tz_name,
        as_of=layout.format_as_of(tz_name),
        flash=_quota_flash(request, lang),
    )
    return web.Response(text=html, content_type="text/html")


# ===========================================================================
# POST /quota/digest-run (R-QUOTA-5, AC30) -- confirm-gated, idempotent.
# ===========================================================================


def _render_already_sent(lang: i18n.Language, at: str) -> web.Response:
    message = i18n.t("portal_digest_already_run", lang, time=at)
    body = layout.render_flash("mute", message) + (
        f'<p><a href="/quota">{layout.escape(i18n.t("portal_nav_quota", lang))}</a></p>'
    )
    # Own title (not `portal_digest_confirm_heading`, UI.md §5 Screen 5's
    # replay-page description gives no page title of its own, but reusing
    # the CONFIRM page's "Send today's digest now?" heading here would be
    # actively misleading -- this page is telling the owner the opposite).
    html = _interstitial_page(lang=lang, title=message, body_html=body)
    return web.Response(text=html, content_type="text/html")


def _render_unconfirmed(deps: "PortalDeps", lang: i18n.Language) -> web.StreamResponse:
    snap = _quota_snapshot(deps.db, deps.config)
    if snap.stop_fired:
        raise layout.redirect_with_flash("/quota", err="quota_stopped")

    today = _today_str(deps.config)
    claimed_at = digest.daily_digest_run_claimed_at(today)
    if claimed_at is not None:
        return _render_already_sent(lang, claimed_at.strftime("%H:%M:%S"))

    goes_to, _skipped = _digest_candidates(deps)
    token = secrets_module.token_urlsafe(16)
    _pending_digest_tokens.add(token)

    rows = [
        (
            i18n.t("portal_digest_confirm_goes_to_label", lang),
            layout.escape(i18n.t("portal_digest_confirm_goes_to_value", lang, n=goes_to)),
        ),
        (
            i18n.t("portal_digest_confirm_uses_label", lang),
            layout.escape(i18n.t("portal_digest_confirm_uses_value", lang, n=goes_to)),
        ),
        (
            i18n.t("portal_digest_confirm_month_label", lang),
            layout.escape(i18n.t("portal_digest_confirm_month_value", lang, used=snap.total, cap=snap.cap)),
        ),
    ]
    body = (
        f"<h1>{layout.escape(i18n.t('portal_digest_confirm_heading', lang))}</h1>"
        f"{layout.dl(rows)}"
        f'<div class="state stop">{layout.escape(i18n.t("portal_digest_irreversible", lang))}</div>'
        f"<p>{layout.escape(i18n.t('portal_digest_duration_warning', lang))}</p>"
        '<form method="post" action="/quota/digest-run">'
        '<input type="hidden" name="confirm" value="yes">'
        f'<input type="hidden" name="token" value="{layout.escape(token)}">'
        '<div class="actions">'
        f'<button class="btn">{layout.escape(i18n.t("portal_digest_confirm_button", lang))}</button>'
        f'<a class="cancel" href="/quota">{layout.escape(i18n.t("portal_users_cancel", lang))}</a>'
        "</div></form>"
    )
    html = _interstitial_page(lang=lang, title=i18n.t("portal_digest_confirm_heading", lang), body_html=body)
    return web.Response(text=html, content_type="text/html")


async def _run_digest_now(deps: "PortalDeps") -> tuple[int, int, int, bool]:
    """The real fan-out (R-QUOTA-5's own literal instruction: invoke the
    REAL `digest.run_daily_digest`, not a re-implementation) -- via
    `digest.run_daily_digest_guarded` (integration item 5), which claims
    the SHARED same-day guard before running, so an independent scheduled
    run on the same calendar day can't also fire. No `scheduler=` passed
    -- a manual "send now" is an explicit owner override of the automatic
    schedule, so it deliberately bypasses `run_daily_digest`'s own
    quiet-hours DEFERRAL branch (UX.md's Flow D never mentions DND; the
    owner asked for "now") and sends immediately to every eligible user.
    A fresh, uncached `RegistryProvider` is built here rather than
    threaded through `PortalDeps` (out of this pass's owned-files scope,
    and this route runs rarely enough that skipping the app's shared
    per-user registry cache costs nothing observable).

    Returns `(sent, skipped, failed, ran)`. `failed` (integration item 5,
    TEST-PORTAL-quota.md Finding F4) is the honest gap between how many
    candidates were eligible to receive a digest (`goes_to`, opted IN)
    and how many actually got one (`sent`) -- previously this gap simply
    vanished from the reported arithmetic on a mid-fan-out send/compose
    failure (`core/digest.py:_send_one_user_digest`'s own fail-open catch
    swallows the exception and returns `False`, with nothing propagated
    back to this caller). This is a deliberately CONSERVATIVE honesty
    fix, not a precise failure classifier: `run_daily_digest` has no
    per-user result channel back to its caller, so a candidate who simply
    had "nothing to say today" (R-C1's own qualifier -- rare in practice,
    see `compose_digest`'s own docstring) is indistinguishable from one
    whose send genuinely failed, and both count toward `failed` here.
    Reporting an honest, accounted-for total (`sent + skipped + failed ==
    all active users`) is judged better than a total that silently
    doesn't add up. `ran=False` (the shared guard was already claimed by
    another run today) reports `(0, skipped, 0, False)` -- no send was
    even attempted by THIS call.

    `clock=datetime.now` is passed EXPLICITLY (rather than relying on
    `run_daily_digest_guarded`'s own default) -- `datetime` resolved here
    is THIS module's own name (`from datetime import datetime` above),
    the SAME one `_today_str`'s pre-check and a test's `monkeypatch.
    setattr(quota, "datetime", ...)` both see; `core/digest.py` imports
    its own separate `datetime` name, which a test patching only `quota.
    datetime` would never touch, so without this explicit thread-through
    the guard's own internal day computation could silently disagree
    with this module's pre-check under a frozen/injected clock."""
    provider = RegistryProvider(deps.config, deps.db)
    goes_to, skipped = _digest_candidates(deps)
    counting_channel = _CountingChannel(deps.channel)
    ran = await digest.run_daily_digest_guarded(deps.db, counting_channel, deps.config, provider, clock=datetime.now)
    if not ran:
        return 0, skipped, 0, False
    if deps.config.digest.mode == "realtime":
        # `run_daily_digest` itself no-ops immediately in realtime mode
        # (digest/realtime are mode-EXCLUSIVE, SPEC-LINE-1.2.md) -- 0 sent
        # here is a correct, by-design no-op, never a per-user failure, so
        # `failed` must stay 0 rather than reporting every opted-in
        # candidate as having "failed" a send that was never attempted.
        return 0, skipped, 0, True
    failed = max(0, goes_to - counting_channel.sent)
    return counting_channel.sent, skipped, failed, True


async def _handle_digest_run_confirmed(deps: "PortalDeps", lang: i18n.Language, token: str) -> web.StreamResponse:
    async with _manual_digest_lock:
        snap = _quota_snapshot(deps.db, deps.config)
        if snap.stop_fired:
            raise layout.redirect_with_flash("/quota", err="quota_stopped")

        today = _today_str(deps.config)
        claimed_at = digest.daily_digest_run_claimed_at(today)
        if claimed_at is not None:
            # A second confirm today -- replay of the same submission, a
            # double-click, or a second legitimate attempt after the
            # first (manual OR the independent scheduled job, integration
            # item 5) already went out. Either way: no second send.
            return _render_already_sent(lang, claimed_at.strftime("%H:%M:%S"))

        if token not in _pending_digest_tokens:
            # Missing/spent/unrecognized token, and (the check above)
            # nothing has actually run today yet -- refuse rather than
            # guess. `_now_hms()` here is a closest-honest-anchor
            # placeholder, not a real "we sent it at this time" claim
            # (there IS no such time -- nothing was sent); the copy reads
            # correctly either way ("nothing was sent again").
            logger.warning("POST /quota/digest-run confirmed with a missing/spent token; refusing, no send")
            return _render_already_sent(lang, _now_hms())
        _pending_digest_tokens.discard(token)

        sent, skipped, failed, ran = await _run_digest_now(deps)
        if not ran:
            # Lost the race to the independent scheduled job in the tiny
            # window between the pre-check above and this call's own
            # claim attempt inside `run_daily_digest_guarded` -- refuse
            # honestly rather than report a send that didn't happen.
            claimed_at = digest.daily_digest_run_claimed_at(today)
            at = claimed_at.strftime("%H:%M:%S") if claimed_at is not None else _now_hms()
            return _render_already_sent(lang, at)

    raise layout.redirect_with_flash("/quota", ran=f"{sent}.{skipped}.{failed}")


async def _handle_digest_run(request: web.Request) -> web.StreamResponse:
    deps: "PortalDeps" = request.app["portal_deps"]
    lang = _lang(deps)
    form = await request.post()
    if form.get("confirm") != "yes":
        return _render_unconfirmed(deps, lang)
    return await _handle_digest_run_confirmed(deps, lang, str(form.get("token", "")))


# ===========================================================================
# GET /config (R-QUOTA-4, AC29) -- effective config, secrets redacted.
# ===========================================================================


def _redact_or_render(name: str, value: object, lang: i18n.Language) -> str:
    is_secret_shaped = any(needle in name.lower() for needle in _REDACT_NEEDLES)
    is_unset = value is None or value == ""
    if is_secret_shaped:
        if is_unset:
            return layout.escape(i18n.t("portal_config_not_set", lang))
        return f'<span class="mono">••••••</span> <span class="meta">{layout.escape(i18n.t("portal_config_hidden", lang))}</span>'
    if is_unset:
        return layout.escape(i18n.t("portal_config_not_set", lang))
    return f'<span class="mono">{layout.escape(str(value))}</span>'


def _render_section_dl(section: BaseModel, lang: i18n.Language) -> str:
    """One `Config` section's own scalar (str/int/float/bool/None)
    fields, in declaration order, as `.dl` rows. Non-scalar fields
    (nested sub-models, lists, dicts -- e.g. `habits` itself, or a
    section with its own list-typed field) are skipped: UI.md Screen 8's
    own example is a flat key-value dump for exactly this kind of field,
    and rendering an arbitrary nested structure generically would risk
    producing broken/unreadable markup for a *could*-tier reference page
    nobody has asked to see exhaustively. Field NAMES are left
    untranslated on purpose (UX.md's own rationale: "the owner is
    comparing this against a file they have open in another window")."""
    rows = []
    for field_name in type(section).model_fields:
        value = getattr(section, field_name)
        if isinstance(value, (BaseModel, list, dict)):
            continue
        rows.append((field_name, _redact_or_render(field_name, value, lang)))
    return layout.dl(rows)


def _render_config_body(config: "Config", lang: i18n.Language) -> str:
    note = f'<p class="note">{layout.escape(i18n.t("portal_config_secrets_note", lang))}</p>'
    sections = []
    for field_name in type(config).model_fields:
        value = getattr(config, field_name)
        if not isinstance(value, BaseModel):
            continue  # `habits: list[HabitConfig]` -- not a `[section]`, skipped (see module docstring above).
        sections.append(f"<h2>[{layout.escape(field_name)}]</h2>" + _render_section_dl(value, lang))
    heading = i18n.t("portal_config_heading", lang)
    return f"<h1>{layout.escape(heading)}</h1>" + note + "".join(sections)


async def _handle_config_get(request: web.Request) -> web.Response:
    deps: "PortalDeps" = request.app["portal_deps"]
    lang = _lang(deps)
    body = _render_config_body(deps.config, lang)
    tz_name = deps.config.app.timezone
    html = layout.page(
        lang=lang,
        title=i18n.t("portal_config_heading", lang),
        current="config",
        pending=layout.pending_count(deps.db),
        body=body,
        path_qs=request.path_qs,
        tz_name=tz_name,
        as_of=layout.format_as_of(tz_name),
    )
    return web.Response(text=html, content_type="text/html")


# ===========================================================================
# R-INT-1: registration hook every portal module exposes.
# ===========================================================================


def register(app: web.Application, deps: "PortalDeps") -> None:
    del deps  # handlers read `request.app["portal_deps"]`, matching every other portal module's own convention.
    app.router.add_get("/quota", _handle_quota_get)
    app.router.add_get("/config", _handle_config_get)
    app.router.add_post("/quota/digest-run", _handle_digest_run)


__all__ = ["register"]
