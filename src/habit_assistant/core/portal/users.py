"""SPEC-LINE-PORTAL.md §4 R-USER-* (module USERS, admin web portal, branch
`line-version`): `GET /users` (pending + active listing + invite form),
`POST /users/approve`, `POST /users/block`, and the two-step `POST
/users/invite` (unconfirmed -> full-page confirm interstitial; `confirm=yes`
-> the write) -- SPEC §3.1's route table lists exactly these four routes for
this module, no separate `GET /users/invite`.

UX.md Flow B/E, UI.md Screen 2/Screen 3, §8 Q6/Q7 (both ADOPTED per the
dispatch note): active rows carry a Block confirm too (Q6), and the owner's
own row renders "You (owner)" with no control AND `POST /users/block` on
`deps.owner_id` is refused server-side regardless of what the UI renders
(Q7) -- the omission of the button is not the guard, `_reject_owner_block`
below is.

Every mutation goes through `core/access.py`'s shared `approve_user`/
`block_user` (source="portal", R-USERACT-1) -- this module never writes to
`users`/`audit_log` directly. `register(app, deps)` only registers routes;
`deps` itself travels per-request via `request.app["portal_deps"]` (set once
by `PortalServer.build_app`), matching `security.py`/`_error_middleware`'s
own convention -- `deps` is accepted here only for `RegisterFn` signature
parity (R-INT-1).

Vocabulary gap closed by this pass: `core/audit.py:Source`/`SOURCES` did not
yet include `"portal"` even though SPEC-LINE-PORTAL.md §5 types
`approve_user`/`block_user`'s `source` param as `audit.Source` and R-USERACT-1
explicitly calls for `source="portal"` writes -- the shared-surface pass's
own IMPL doc doesn't list `core/audit.py` as touched. Fixed here (one tuple
entry + one Literal member) since AC16/AC17/AC18 are unsatisfiable without
it; flagged to Archi in IMPL-PORTAL-users.md rather than silently patched
around."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from aiohttp import web

from habit_assistant.core import access, i18n, streaks, timeutil, user_prefs
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.portal import layout

if TYPE_CHECKING:
    from habit_assistant.core.portal.server import PortalDeps

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registration (R-INT-1).
# ---------------------------------------------------------------------------


def register(app: web.Application, deps: "PortalDeps") -> None:
    del deps  # travels per-request via request.app["portal_deps"]
    app.router.add_get("/users", handle_users_index)
    app.router.add_post("/users/approve", handle_approve)
    app.router.add_post("/users/block", handle_block)
    app.router.add_post("/users/invite", handle_invite)


# ---------------------------------------------------------------------------
# Shared per-module helpers.
# ---------------------------------------------------------------------------


def _owner_language(deps: "PortalDeps") -> i18n.Language:
    """Mirrors `core/portal/server.py:_owner_language`'s fail-open shape
    (a private per-module snippet, same convention as `core/access.py:
    _resolve_unprompted_language_for` -- every capture site owns its own
    small language-resolution copy rather than sharing one across
    unrelated modules)."""
    try:
        pref = user_prefs.stored_language_pref(deps.db, deps.owner_id)
    except Exception:
        pref = "auto"
    return i18n.resolve_unprompted_language(deps.config, user_pref=pref)


def _display_name_or_id(db, chat_id: str) -> str:
    if not chat_id:
        return ""
    try:
        row = db.get_user(chat_id)
    except Exception:
        row = None
    if row is not None and row["display_name"]:
        return row["display_name"]
    return chat_id


def _format_ago(ts_str: str | None, now: datetime, lang: i18n.Language) -> str:
    """UX.md §7's own flagged requirement: EN needs singular/plural forms
    ("1 minute ago" vs "N minutes ago"); TH doesn't pluralize, so both TH
    variants read identically -- still two catalog entries per unit so a
    future TH copy change doesn't have to fight a shared EN plural form."""
    if not ts_str:
        return i18n.t("portal_relative_just_now", lang)
    try:
        then = datetime.fromisoformat(ts_str)
    except (TypeError, ValueError):
        return ts_str
    seconds = max(0.0, (now - then).total_seconds())
    if seconds < 60:
        return i18n.t("portal_relative_just_now", lang)
    minutes = int(seconds // 60)
    if minutes < 60:
        return i18n.t("portal_relative_minute" if minutes == 1 else "portal_relative_minutes", lang, n=minutes)
    hours = minutes // 60
    if hours < 24:
        return i18n.t("portal_relative_hour" if hours == 1 else "portal_relative_hours", lang, n=hours)
    days = hours // 24
    return i18n.t("portal_relative_day" if days == 1 else "portal_relative_days", lang, n=days)


def _current_streak(deps: "PortalDeps", user_id: str, now: datetime) -> int:
    """AC19's "current streak" -- this app is multi-habit (`core/habits.py`),
    so "the" streak is ambiguous; this pass takes the MAX `streaks.
    compute_streak` across `user_id`'s own habit registry (base + custom
    habits, `HabitRegistry.for_user`) as the single headline number an
    owner glancing at this row would want -- "your best ongoing streak".
    Not specified by SPEC/UX; flagged in IMPL-PORTAL-users.md. Fail-open
    per-user (one user's streak computation failing must not blank the
    whole Active table)."""
    try:
        registry = HabitRegistry.for_user(deps.config, deps.db, user_id)
        today = timeutil.today_in_timezone(lambda: now, deps.config.app.timezone)
        # v1.3.2+line bug fix: DISPLAY-ONLY `display_streak`, not
        # `compute_streak` -- an owner glancing at this column shouldn't
        # see a false 0 for a user whose streak is intact but who simply
        # hasn't logged yet today (see `streaks.display_streak`'s own
        # docstring; this admin-portal module has no Telegram-edition
        # counterpart, so no PORT TO MAIN note here).
        return max(
            (streaks.display_streak(deps.db, deps.config, habit, today, user_id) for habit in registry),
            default=0,
        )
    except Exception:
        logger.exception("Failed to compute the portal Users page's current-streak for user_id=%r", user_id)
        return 0


def _unavailable_panel(heading: str, lang: i18n.Language) -> str:
    body = (
        f'<p class="empty warn">{layout.escape(i18n.t("portal_panel_unavailable", lang))}</p>'
        f'<p class="meta">{layout.escape(i18n.t("portal_panel_unavailable_hint", lang))}</p>'
    )
    return layout.panel(heading, body)


# ---------------------------------------------------------------------------
# GET /users -- AC15/AC19.
# ---------------------------------------------------------------------------


async def handle_users_index(request: web.Request) -> web.Response:
    deps: "PortalDeps" = request.app["portal_deps"]
    lang = _owner_language(deps)
    now = datetime.now()

    flash_html = _build_flash(deps, lang, request.query)
    body = (
        f"<h1>{layout.escape(i18n.t('portal_users_page_title', lang))}</h1>"
        + _render_pending_section(deps, lang, now)
        + _render_active_section(deps, lang, now)
        + _render_invite_section(lang, request.query)
    )
    html = layout.page(
        lang=lang,
        title=i18n.t("portal_users_page_title", lang),
        current="users",
        pending=layout.pending_count(deps.db),
        body=body,
        path_qs=request.path_qs,
        tz_name=deps.config.app.timezone,
        as_of=layout.format_as_of(deps.config.app.timezone),
        flash=flash_html,
    )
    return web.Response(text=html, content_type="text/html")


def _build_flash(deps: "PortalDeps", lang: i18n.Language, query) -> str:
    ok = query.get("ok")
    err = query.get("err")
    if ok == "approve":
        name = _display_name_or_id(deps.db, query.get("chat", ""))
        return layout.render_flash("ok", i18n.t("portal_users_flash_approved", lang, name=name))
    if ok == "approve_nopush":
        # UX.md §3 Flow B / §7 `portal_flash_approve_nopush` (TEST-PORTAL-
        # users.md Finding 1): the approve succeeded (DB + audit are the
        # source of truth) but the welcome push was NOT confirmed sent --
        # honest, not an error state (still rendered with the "ok" tier,
        # matching UX.md §7's own copy, which reads as a caveat, not a
        # failure).
        name = _display_name_or_id(deps.db, query.get("chat", ""))
        return layout.render_flash("ok", i18n.t("portal_flash_approve_nopush", lang, name=name))
    if ok == "block":
        name = _display_name_or_id(deps.db, query.get("chat", ""))
        return layout.render_flash("ok", i18n.t("portal_users_flash_blocked", lang, name=name))
    if ok == "invite":
        return layout.render_flash("ok", i18n.t("portal_users_flash_invited", lang, chat_id=query.get("chat", "")))
    if err == "chat_unknown":
        return layout.render_flash("stop", i18n.t("portal_users_error_unknown_user", lang))
    if err == "chat_invalid":
        return layout.render_flash("stop", i18n.t("portal_users_error_invalid_id", lang))
    if err == "block_owner":
        return layout.render_flash("stop", i18n.t("portal_users_error_block_owner", lang))
    if err == "save_failed":
        return layout.render_flash("stop", i18n.t("portal_users_error_save_failed", lang))
    return ""


# ---------------------------------------------------------------------------
# Pending section (AC15).
# ---------------------------------------------------------------------------


def _render_pending_section(deps: "PortalDeps", lang: i18n.Language, now: datetime) -> str:
    try:
        rows = [row for row in deps.db.list_users() if row["status"] == "pending"]
    except Exception:
        logger.exception("Failed to read pending users for the portal Users page")
        return _unavailable_panel(i18n.t("portal_users_pending_heading_plain", lang), lang)

    heading = i18n.t("portal_users_pending_heading", lang, n=len(rows))
    if not rows:
        body = layout.empty(
            i18n.t("portal_users_pending_empty", lang),
            tier="ok",
            extra=f'<p class="meta">{layout.escape(i18n.t("portal_users_pending_empty_cta", lang))}</p>',
        )
    else:
        body = "".join(_pending_card(row, lang, now) for row in rows)
    return layout.panel(heading, body)


def _pending_card(row, lang: i18n.Language, now: datetime) -> str:
    chat_id = row["chat_id"]
    name = row["display_name"]
    display_label = name or chat_id
    ago = _format_ago(row["created_at"], now, lang)
    waiting_line = f'<p class="meta">{layout.escape(i18n.t("portal_users_pending_waiting_since", lang, ago=ago))}</p>'
    headline = f"<h3>{layout.escape(name)}</h3>{layout.mono(chat_id)}" if name else f"<h3>{layout.escape(chat_id)}</h3>"

    approve = layout.confirm_disclosure(
        action="/users/approve",
        hidden_fields={"chat_id": chat_id},
        summary_text=i18n.t("portal_users_approve_summary", lang),
        body_text=i18n.t("portal_users_approve_confirm_body", lang, name=display_label),
        submit_text=i18n.t("portal_users_approve_confirm_button", lang),
        cancel_href="/users",
        cancel_text=i18n.t("portal_users_cancel", lang),
    )
    block = layout.confirm_disclosure(
        action="/users/block",
        hidden_fields={"chat_id": chat_id},
        summary_text=i18n.t("portal_users_block_summary", lang),
        body_text=i18n.t("portal_users_block_confirm_body", lang, name=display_label),
        submit_text=i18n.t("portal_users_block_confirm_button", lang),
        cancel_href="/users",
        cancel_text=i18n.t("portal_users_cancel", lang),
        tier="stop",
    )
    return f'<div class="card">{headline}{waiting_line}<div class="actions">{approve}{block}</div></div>'


# ---------------------------------------------------------------------------
# Active section (AC19, Q6/Q7).
# ---------------------------------------------------------------------------

_ACTIVE_COLUMNS = (
    "portal_users_col_name",
    "portal_users_col_chat_id",
    "portal_users_col_last_log",
    "portal_users_col_streak",
    "portal_users_col_digest",
    "portal_users_col_language",
    "portal_users_col_action",
)


def _render_active_section(deps: "PortalDeps", lang: i18n.Language, now: datetime) -> str:
    try:
        rows = [row for row in deps.db.list_users() if row["status"] == "active"]
    except Exception:
        logger.exception("Failed to read active users for the portal Users page")
        return _unavailable_panel(i18n.t("portal_users_active_heading_plain", lang), lang)

    heading = i18n.t("portal_users_active_heading", lang, n=len(rows))
    if not rows:
        body = layout.empty(i18n.t("portal_users_active_empty", lang))
    else:
        body = _active_table(deps, rows, lang, now)
    return layout.panel(heading, body)


def _active_table(deps: "PortalDeps", rows: list, lang: i18n.Language, now: datetime) -> str:
    labels = [i18n.t(msg_id, lang) for msg_id in _ACTIVE_COLUMNS]
    headers = "".join(layout.th_col(label) for label in labels)
    body_rows = "".join(_active_row(deps, row, lang, now, labels) for row in rows)
    return f'<table class="collapse"><thead><tr>{headers}</tr></thead><tbody>{body_rows}</tbody></table>'


def _active_row(deps: "PortalDeps", row, lang: i18n.Language, now: datetime, labels: list[str]) -> str:
    chat_id = row["chat_id"]
    is_owner = chat_id == deps.owner_id
    name_html = i18n.t("portal_users_owner_row", lang) if is_owner else layout.escape(row["display_name"] or chat_id)

    try:
        last_log_row = deps.db.last_log(chat_id)
    except Exception:
        logger.exception("Failed to read last_log for portal Users active row, chat_id=%r", chat_id)
        last_log_row = None
    last_log_value = _format_ago(last_log_row["ts"], now, lang) if last_log_row is not None else i18n.t(
        "portal_users_never_logged", lang
    )

    streak_value = str(_current_streak(deps, chat_id, now))
    digest_value = i18n.t("portal_users_digest_off" if row["digest_opt_out"] else "portal_users_digest_on", lang)
    lang_value = (row["language_pref"] or "auto").upper()

    if is_owner:
        action_html = ""
    else:
        display_label = row["display_name"] or chat_id
        action_html = layout.confirm_disclosure(
            action="/users/block",
            hidden_fields={"chat_id": chat_id},
            summary_text=i18n.t("portal_users_block_summary", lang),
            body_text=i18n.t("portal_users_block_confirm_body", lang, name=display_label),
            submit_text=i18n.t("portal_users_block_confirm_button", lang),
            cancel_href="/users",
            cancel_text=i18n.t("portal_users_cancel", lang),
            tier="stop",
        )

    cells = [
        layout.td_cell(labels[0], name_html, head=True),
        layout.td_cell(labels[1], layout.mono(chat_id)),
        layout.td_cell(labels[2], layout.escape(last_log_value)),
        layout.td_cell(labels[3], layout.escape(streak_value)),
        layout.td_cell(labels[4], layout.escape(digest_value)),
        layout.td_cell(labels[5], layout.escape(lang_value)),
        layout.td_cell(labels[6], action_html),
    ]
    return f"<tr>{''.join(cells)}</tr>"


# ---------------------------------------------------------------------------
# Invite panel (Flow E, R-USER-1's own "pre-approval form").
# ---------------------------------------------------------------------------


def _render_invite_section(lang: i18n.Language, query) -> str:
    invalid = query.get("err") == "chat_invalid"
    value = query.get("val", "") if invalid else ""
    invalid_attrs = ' aria-invalid="true" aria-describedby="flash"' if invalid else ""
    field = (
        f'<label class="vh" for="portal-invite-chat-id">{layout.escape(i18n.t("portal_users_invite_field_label", lang))}</label>'
        f'<input id="portal-invite-chat-id" name="chat_id" type="text" inputmode="text" '
        f'autocapitalize="off" autocomplete="off" value="{layout.escape(value)}"{invalid_attrs}>'
    )
    body = (
        f'<p class="meta">{layout.escape(i18n.t("portal_users_invite_help", lang))}</p>'
        f'<form method="post" action="/users/invite">{field}'
        f'<div class="actions">{layout.btn(i18n.t("portal_users_invite_submit", lang))}</div></form>'
    )
    return layout.panel(i18n.t("portal_users_invite_heading", lang), body)


def _render_invite_interstitial(chat_id: str, lang: i18n.Language) -> str:
    """UI.md §3.22/Screen 3: `.wrap.decide`, brand header, NO nav -- built
    locally (not via `layout.page()`, which always renders nav+footer),
    mirroring `layout.py:render_500`'s own bespoke minimal-shell pattern
    for the one other portal page that also omits the nav."""
    heading = i18n.t("portal_users_invite_confirm_heading", lang)
    body_text = i18n.t("portal_users_invite_confirm_body", lang)
    confirm_text = i18n.t("portal_users_invite_confirm_button", lang)
    cancel_text = i18n.t("portal_users_cancel", lang)
    form = (
        '<form method="post" action="/users/invite">'
        f'<input type="hidden" name="chat_id" value="{layout.escape(chat_id)}">'
        '<input type="hidden" name="confirm" value="yes">'
        f'<div class="actions">{layout.btn(confirm_text)}'
        f'<a class="cancel" href="/users">{layout.escape(cancel_text)}</a></div>'
        "</form>"
    )
    return (
        "<!doctype html>"
        f'<html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{layout.escape(heading)}</title><style>{layout.PORTAL_CSS}</style></head>"
        f'<body><header><div class="wrap"><p class="brand">{layout.escape(layout.BRAND)}</p></div></header>'
        f'<main class="wrap decide" id="main"><h1>{layout.escape(heading)}</h1>'
        f"{layout.id_block(chat_id)}"
        f"<p>{layout.escape(body_text)}</p>"
        f"{form}</main></body></html>"
    )


# ---------------------------------------------------------------------------
# POST /users/approve -- AC16, AC20, AC21.
# ---------------------------------------------------------------------------


async def handle_approve(request: web.Request) -> web.Response:
    deps: "PortalDeps" = request.app["portal_deps"]
    data = await request.post()
    chat_id = str(data.get("chat_id", "")).strip()

    if not chat_id or deps.db.get_user(chat_id) is None:
        raise layout.redirect_with_flash("/users", err="chat_unknown")

    try:
        push_confirmed = await access.approve_user(
            deps.db, deps.channel, deps.config, actor=deps.owner_id, target_chat=chat_id, source="portal"
        )
    except Exception:
        logger.exception("Portal approve failed for chat_id=%r", chat_id)
        raise layout.redirect_with_flash("/users", err="save_failed")

    # UX.md §3 Flow B (explicit MUST, TEST-PORTAL-users.md Finding 1): the
    # flash must not claim delivery when the welcome push wasn't confirmed
    # sent (LINE API outage, or the realtime quota gate silently dropping
    # it) -- `ok="approve_nopush"` picks the honest `_build_flash` branch.
    ok_value = "approve" if push_confirmed else "approve_nopush"
    raise layout.redirect_with_flash("/users", ok=ok_value, chat=chat_id)


# ---------------------------------------------------------------------------
# POST /users/block -- AC17, AC20, AC21, Q7's server-side owner refusal.
# ---------------------------------------------------------------------------


async def handle_block(request: web.Request) -> web.Response:
    deps: "PortalDeps" = request.app["portal_deps"]
    data = await request.post()
    chat_id = str(data.get("chat_id", "")).strip()

    if not chat_id or deps.db.get_user(chat_id) is None:
        raise layout.redirect_with_flash("/users", err="chat_unknown")

    # UX.md §8 Q7 (adopted): the UI never renders a Block control on the
    # owner's own row, but that omission alone is not a guard -- a forged
    # POST (or a stale form from before a role change) must still be
    # refused HERE, server-side, no write, no audit row.
    if chat_id == deps.owner_id:
        raise layout.redirect_with_flash("/users", err="block_owner")

    try:
        await access.block_user(deps.db, deps.channel, deps.config, actor=deps.owner_id, target_chat=chat_id, source="portal")
    except Exception:
        logger.exception("Portal block failed for chat_id=%r", chat_id)
        raise layout.redirect_with_flash("/users", err="save_failed")

    raise layout.redirect_with_flash("/users", ok="block", chat=chat_id)


# ---------------------------------------------------------------------------
# POST /users/invite -- AC18, AC20, AC21 (Flow E: unconfirmed -> interstitial,
# confirm=yes -> the write).
# ---------------------------------------------------------------------------


async def handle_invite(request: web.Request) -> web.Response:
    deps: "PortalDeps" = request.app["portal_deps"]
    data = await request.post()
    raw = str(data.get("chat_id", "")).strip()
    confirm = str(data.get("confirm", "")).strip().lower()
    lang = _owner_language(deps)

    if not raw or access._CHAT_ID_RE.match(raw) is None:
        raise layout.redirect_with_flash("/users", err="chat_invalid", val=raw[:64])

    if confirm != "yes":
        return web.Response(text=_render_invite_interstitial(raw, lang), content_type="text/html")

    try:
        await access.approve_user(deps.db, deps.channel, deps.config, actor=deps.owner_id, target_chat=raw, source="portal")
    except Exception:
        logger.exception("Portal invite failed for chat_id=%r", raw)
        raise layout.redirect_with_flash("/users", err="save_failed")

    raise layout.redirect_with_flash("/users", ok="invite", chat=raw)


__all__ = ["register"]
