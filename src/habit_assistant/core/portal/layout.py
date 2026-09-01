"""SPEC-LINE-PORTAL.md §4/§6 (shared surface, admin web portal, branch
`line-version`) + UI.md §3/§8/§9 (Iris): the bilingual page shell every
portal page renders through, plus the shared HTML-builder primitives the
four page modules (STATUS/USERS/AUDIT/QUOTA) compose their own screens
from -- so the same visual vocabulary (panel, tile, bar, tag, confirm
disclosure, ...) can never drift between modules built by different
passes.

`PORTAL_CSS` is Iris's UI.md §8 stylesheet, pasted verbatim, with exactly
ONE addition: an `@font-face` rule for `E2` (UI.md §11 escalation E2,
option (b) -- adopted per Archi's dispatch note, beyond Iris's own "(a),
ship as designed" default). `local("Noto Sans Thai")` means a device that
already has the system font pays zero network cost, exactly as the
stylesheet's `--sans` fallback stack always assumed; only a machine
WITHOUT it falls through to the same-origin `/fonts/...` route
`core/portal/server.py` serves -- still zero EXTERNAL requests, still
offline-clean. No other rule in the stylesheet is touched.

Escaping (UI.md §9.2 contract 14): `escape()` is the ONE function every
interpolated value must pass through -- display names, chat ids,
`data-label` values, logger names, log messages, everything that reaches
a portal page from outside. This is the XSS boundary; every builder below
escapes its own plain-text parameters internally so a caller who uses
them correctly cannot forget to.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from aiohttp import web

from habit_assistant.core import i18n

logger = logging.getLogger(__name__)

# The vendored Thai font's same-origin route (E2 option (b)) -- kept in
# ONE place (`server.py` imports this constant for its route registration)
# so the CSS `url(...)` and the actual `app.router.add_get(...)` path can
# never drift apart.
THAI_FONT_ROUTE = "/fonts/NotoSansThai-Regular.ttf"

# UI.md §8: 8,160 bytes verbatim, plus the one @font-face addition noted
# in this module's own docstring above (marked inline below too).
PORTAL_CSS = (
    "/* portal.css - the whole admin portal. Light only. No JS, no motion. */\n"
    ":root{\n"
    "--bg:#FAFAFB;--card:#fff;--s2:#F4F6F8;--line:#E3E7EC;--line2:#838B96;\n"
    "--ink:#16181D;--ink2:#454D5A;--ink3:#606A78;\n"
    "--teal:#0F766E;--teal-d:#0C615A;--teal-t:#E7F1F0;\n"
    "--warn:#9A5B00;--warn-t:#F5EFE6;--stop:#B3261E;--stop-t:#F7E9E8;\n"
    "--tier:var(--teal);--tier-t:var(--teal-t);--r1:6px;--r2:10px;\n"
    '--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans Thai","Leelawadee UI",Thonburi,sans-serif;\n'
    "--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}\n"
    # E2 (option b, adopted): same-origin fallback for a desktop with no
    # system Thai font -- local() first, so a machine that already has the
    # font makes zero network requests, exactly as the stack always did.
    '@font-face{font-family:"Noto Sans Thai";src:local("Noto Sans Thai"),'
    f'url({THAI_FONT_ROUTE}) format("truetype");font-display:swap}}\n'
    ".ok{--tier:var(--teal);--tier-t:var(--teal-t)}\n"
    ".warn{--tier:var(--warn);--tier-t:var(--warn-t)}\n"
    ".stop{--tier:var(--stop);--tier-t:var(--stop-t)}\n"
    ".mute{--tier:var(--ink3);--tier-t:var(--s2)}\n"
    "*{box-sizing:border-box}\n"
    "body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 var(--sans);-webkit-text-size-adjust:100%}\n"
    "h1,h2,h3{line-height:1.35;margin:0 0 8px;font-weight:600}\n"
    "h1{font-size:24px}h2{font-size:18px}\n"
    "p{margin:0 0 8px;max-width:68ch}\n"
    "a{color:var(--teal)}\n"
    ":focus{outline:2px solid var(--ink);outline-offset:2px}\n"
    ":focus:not(:focus-visible){outline:none}\n"
    ".vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}\n"
    ".vh:focus{position:static;width:auto;height:auto;clip:auto}\n"
    ".wrap{max-width:960px;margin:0 auto;padding:16px}\n"
    ".decide{max-width:560px;margin-top:32px}\n"
    "header{background:var(--card);border-bottom:1px solid var(--line)}\n"
    ".brand{font-size:14px;font-weight:600;color:var(--ink2);margin:0 0 8px}\n"
    "nav{display:flex;flex-wrap:wrap;gap:8px}\n"
    "nav a{display:flex;align-items:center;min-height:44px;padding:8px 14px;border:1px solid var(--line);"
    "border-radius:var(--r1);background:var(--card);color:var(--ink2);text-decoration:none;font-weight:500}\n"
    "nav a:hover{border-color:var(--line2);color:var(--ink)}\n"
    "nav a.pending{border-color:var(--teal);background:var(--teal-t);color:var(--teal);font-weight:600}\n"
    "nav a[aria-current=page]{background:var(--teal);border-color:var(--teal);color:#fff}\n"
    "footer{margin-top:24px;padding-top:16px;border-top:1px solid var(--line);color:var(--ink3);font-size:14px}\n"
    ".panel{background:var(--card);border:1px solid var(--line);border-radius:var(--r2);padding:16px;margin:0 0 16px}\n"
    ".panel>h2{margin:0 0 12px}\n"
    ".verdict{background:var(--tier-t);border:1px solid var(--line);border-radius:var(--r2);padding:16px;"
    "margin:0 0 16px;font-size:20px;font-weight:600;line-height:1.45}\n"
    ".verdict a{color:var(--ink)}\n"
    ".verdict ul{margin:8px 0 0;padding-left:20px;font-size:16px;font-weight:400}\n"
    ".flash{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--tier);"
    "border-radius:var(--r1);padding:12px 16px;margin:0 0 16px}\n"
    ".needs{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;min-height:64px;"
    "background:var(--card);border:1px solid var(--teal);border-radius:var(--r2);padding:16px;margin:0 0 16px;"
    "color:var(--ink);font-weight:600;text-decoration:none}\n"
    ".needs:hover{background:var(--teal-t)}\n"
    ".needs b{color:var(--teal);white-space:nowrap}\n"
    ".tiles{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin:0 0 16px}\n"
    ".tile{background:var(--card);border:1px solid var(--line);border-radius:var(--r2);padding:12px;font-size:14px;"
    "color:var(--ink3)}\n"
    ".tile b{display:block;margin-top:2px;font-size:18px;color:var(--ink)}\n"
    ".tile small{display:block;font-size:14px;color:var(--ink3)}\n"
    ".wide{grid-column:1/-1}\n"
    ".bar{height:10px;margin:8px 0;background:var(--line);border-radius:999px;overflow:hidden}\n"
    ".bar i{display:block;height:100%;background:var(--tier)}\n"
    ".gauge b{font-size:18px;font-variant-numeric:tabular-nums}\n"
    ".state{display:flex;flex-wrap:wrap;gap:8px;justify-content:space-between;background:var(--tier-t);"
    "border-radius:var(--r1);padding:8px 12px;margin:8px 0 0}\n"
    "table{width:100%;border-collapse:collapse}\n"
    "th,td{padding:10px 12px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}\n"
    "th{background:var(--s2);color:var(--ink2);font-size:14px;font-weight:600;white-space:nowrap}\n"
    ".num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}\n"
    ".now{background:var(--s2)}\n"
    "td .bar{width:100%;min-width:56px;margin:0}\n"
    ".card{background:var(--card);border:1px solid var(--line);border-radius:var(--r2);padding:16px;margin:0 0 12px}\n"
    ".card h3{margin:0 0 4px;font-size:18px}\n"
    ".meta{color:var(--ink3);font-size:14px;margin:0 0 12px}\n"
    ".actions{display:flex;flex-wrap:wrap;gap:8px}\n"
    ".btn,.confirm>summary{display:flex;align-items:center;justify-content:center;gap:8px;min-height:44px;"
    "padding:10px 18px;border:1px solid var(--tier);border-radius:var(--r1);font:inherit;font-weight:600;"
    "text-align:center;text-decoration:none;cursor:pointer}\n"
    ".btn{display:inline-flex;background:var(--tier);color:#fff}\n"
    ".btn:hover{background:var(--teal-d);border-color:var(--teal-d)}\n"
    ".btn.quiet{background:var(--card);color:var(--tier)}\n"
    ".btn.quiet:hover{background:var(--tier-t)}\n"
    ".cancel{display:inline-flex;align-items:center;min-height:44px;padding:0 12px;color:var(--ink2)}\n"
    "details{margin:0}\n"
    "summary{cursor:pointer;list-style:none}\n"
    "summary::-webkit-details-marker{display:none}\n"
    'summary::after{content:" \\25B8"}\n'
    'details[open]>summary::after{content:" \\25BE"}\n'
    ".confirm{flex:1 1 auto;min-width:150px}\n"
    ".confirm[open]{flex-basis:100%}\n"
    ".confirm>summary{background:var(--card);color:var(--tier)}\n"
    ".confirm[open]>summary{border-radius:var(--r1) var(--r1) 0 0;background:var(--tier-t)}\n"
    ".confirm>div{padding:12px;border:1px solid var(--tier);border-top:0;border-radius:0 0 var(--r2) var(--r2);"
    "background:var(--tier-t)}\n"
    ".more>summary{display:inline-flex;align-items:center;min-height:44px;color:var(--teal);font-weight:500}\n"
    "label{display:block;margin:0 0 4px;font-size:14px;font-weight:500;color:var(--ink2)}\n"
    "input{width:100%;max-width:420px;min-height:44px;padding:10px 12px;border:1px solid var(--line2);"
    "border-radius:var(--r1);background:var(--card);color:var(--ink);font:16px/1.5 var(--mono)}\n"
    "input:focus{border-color:var(--teal)}\n"
    "input[aria-invalid=true]{border:2px solid var(--stop)}\n"
    ".tag{display:inline-block;padding:2px 8px;border:1px solid var(--line);border-radius:999px;"
    "background:var(--s2);color:var(--ink2);font:12px/1.5 var(--mono);white-space:nowrap}\n"
    ".tag.word{font:14px/1.5 var(--sans)}\n"
    ".tag.warn{background:var(--warn-t);border-color:var(--warn);color:var(--warn)}\n"
    ".tag.stop{background:var(--stop-t);border-color:var(--stop);color:var(--stop)}\n"
    ".mono{font-family:var(--mono);font-size:14px;color:var(--ink2);word-break:break-all}\n"
    ".id{display:block;padding:12px;margin:0 0 16px;border:1px solid var(--line);border-radius:var(--r1);"
    "background:var(--s2);font-family:var(--mono);font-size:24px;line-height:1.5;letter-spacing:.06em;"
    "word-break:break-all}\n"
    ".note{background:var(--s2);border:1px solid var(--line);border-radius:var(--r1);padding:12px;margin:0 0 12px;"
    "color:var(--ink2);font-size:14px}\n"
    ".empty{background:var(--tier-t);border-radius:var(--r1);padding:12px;color:var(--ink2)}\n"
    ".pager{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:center;margin:16px 0}\n"
    ".pager span{padding:0 8px;color:var(--ink2);font-size:14px}\n"
    ".dl{margin:0}\n"
    ".dl div{display:flex;gap:12px;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--line)}\n"
    ".dl dt{color:var(--ink2);font-size:14px}\n"
    ".dl dd{margin:0;text-align:right;font-weight:500}\n"
    "@media(min-width:600px){\n"
    ".wrap{padding:24px}\n"
    ".tiles{grid-template-columns:repeat(3,1fr)}\n"
    "tbody tr:hover{background:var(--s2)}}\n"
    "@media(min-width:960px){\n"
    ".tiles{grid-template-columns:repeat(4,1fr)}\n"
    ".cols{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}}\n"
    "@media(max-width:599px){\n"
    ".collapse thead{display:none}\n"
    ".collapse tr{display:block;margin:0 0 8px;padding:8px 12px;background:var(--card);border:1px solid var(--line);"
    "border-radius:var(--r2)}\n"
    ".collapse td{display:flex;gap:8px;justify-content:space-between;padding:4px 0;border:0}\n"
    ".collapse td::before{content:attr(data-label);flex:0 0 auto;color:var(--ink3);font-size:14px}\n"
    ".collapse td.head{display:block;font-size:18px;font-weight:600}\n"
    ".collapse td.head::before{display:none}\n"
    ".collapse td.full,.collapse td.full::before{display:block}\n"
    ".dl div{display:block}\n"
    ".dl dd{text-align:left}\n"
    ".pager{flex-direction:column;align-items:stretch}\n"
    ".pager .btn{width:100%}}\n"
)

BRAND = "Habit Assistant · Admin"

# UX.md §2/§4: nav order is FREQUENCY order, not spec/alphabetical order.
# (key, path, i18n msg id). "config" is deliberately absent -- it lives in
# the footer only (UX.md: "a could-tier reference page... doesn't cost a
# slot in the phone nav row").
NAV_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("status", "/", "portal_nav_status"),
    ("users", "/users", "portal_nav_users"),
    ("quota", "/quota", "portal_nav_quota"),
    ("audit", "/audit", "portal_nav_audit"),
    ("activity", "/activity", "portal_nav_activity"),
)


def escape(value: object) -> str:
    """`html.escape(str(value), quote=True)` -- safe in both text and
    (double-quoted) attribute contexts. The ONE escaping function every
    interpolated value in this package passes through (UI.md §9.2
    contract 14)."""
    return html.escape(str(value), quote=True)


def format_as_of(tz_name: str, clock: Callable[[], datetime] = datetime.now) -> str:
    """"As of {time}" (UX.md §4: mandatory on every page, HH:MM:SS so a
    fresh page and one left open feel visibly different). Naive `clock()`
    output is treated as already being in `tz_name` (mirrors `core/
    timeutil.py`'s own clock-normalization convention); an aware one is
    converted to it."""
    now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(tz_name))
    else:
        now = now.astimezone(ZoneInfo(tz_name))
    return now.strftime("%H:%M:%S")


def format_pct(pct: float) -> str:
    """"1.2" / "87" / "100" -- one decimal place, but a whole-number
    result drops the trailing ".0" (matches SPEC-LINE-PORTAL.md §3.2's own
    raw HTML example "(1.2%)" next to UI.md §3.7's own "(87%)"). Integration
    fix, item 6 (TEST-PORTAL-status.md Finding 1 / TEST-PORTAL-quota.md
    Finding F2): promoted here from `status.py`'s own former private copy
    -- `quota.py`'s gauge independently formatted `f"{pct:.1f}"` with no
    trim, so the Status and Quota pages rendered DIFFERENT strings
    ("80%" vs "80.0%") for the identical underlying number. Both now call
    this one function."""
    text = f"{pct:.1f}"
    return text[:-2] if text.endswith(".0") else text


def format_month_heading(yyyymm: str) -> str:
    """"Aug 2026" from a "YYYY-MM" key -- the SAME month-heading format on
    BOTH the Status page's quota gauge and the Quota page's own gauge
    (UI.md §3.7's "the SAME 3-state gauge component"). Integration fix,
    item 6 (TEST-PORTAL-quota.md Finding F2): `quota.py`'s gauge used to
    render the raw ISO key ("2026-08") instead of this format, diverging
    from `status.py`'s own `now.strftime("%b %Y")` for the identical
    month. A malformed `yyyymm` (should never happen -- every caller
    produces it via this app's own `strftime("%Y-%m")`) falls back to the
    raw string rather than raising -- a gauge heading must never 500."""
    try:
        return datetime.strptime(yyyymm, "%Y-%m").strftime("%b %Y")
    except ValueError:
        return yyyymm


def pending_count(db) -> int:
    """UX.md §8 Q1 (shared-surface implication, adopted): the nav's own
    highest-value glanceability feature -- computed on EVERY page render.
    Fail-open on a DB hiccup: never a blank nav, never a 500 (returns 0,
    which renders the plain, count-less "Users" chip -- indistinguishable
    from "genuinely nobody waiting", which is the safe direction to fail
    for a read that merely couldn't confirm the count)."""
    try:
        return sum(1 for row in db.list_users() if row["status"] == "pending")
    except Exception:
        logger.exception("Failed to compute the portal nav's pending-user count; showing a plain chip")
        return 0


def render_nav(*, current: str, lang: i18n.Language, pending: int) -> str:
    """UI.md §3.2/§9.2 contract 4: one `<a>` per destination, `aria-
    current="page"` on the active one, `class="pending"` + the count IN
    the label text (never a separate badge, Iris §11 E1) on Users when
    `pending >= 1`; the plain, count-less label otherwise."""
    links = []
    for key, path, msg_id in NAV_ITEMS:
        classes: list[str] = []
        if key == "users":
            if pending > 0:
                label = i18n.t(msg_id, lang, n=pending)
                classes.append("pending")
            else:
                label = i18n.t("portal_nav_users_plain", lang)
        else:
            label = i18n.t(msg_id, lang)
        attrs = f'href="{escape(path)}"'
        if key == current:
            attrs += ' aria-current="page"'
        if classes:
            attrs += f' class="{" ".join(classes)}"'
        links.append(f"<a {attrs}>{escape(label)}</a>")
    return "<nav>" + "".join(links) + "</nav>"


def render_flash(kind: str, message: str, *, extra: str = "") -> str:
    """UI.md §3.5/§9.2 contract 8: `<div id="flash" class="flash
    ok|stop|mute" role="status" tabindex="-1">`, placed immediately below
    nav. Moving focus there on load is the CALLER's job -- carry the
    `#flash` fragment in the redirect Location (`redirect_with_flash`,
    below)."""
    return f'<div id="flash" class="flash {escape(kind)}" role="status" tabindex="-1">{escape(message)}{extra}</div>'


def render_footer(*, lang: i18n.Language, as_of: str, tz_name: str, path_qs: str, extra: str = "") -> str:
    """UX.md §4: "As of {time} · [Refresh] · All times in {tz}" plus the
    Config link, on every page. `[Refresh]` is a PLAIN link to the
    current URL (`path_qs`, e.g. `request.path_qs`) -- not `?` alone, so
    it re-issues whatever query the page was already rendered with."""
    as_of_line = escape(i18n.t("portal_footer_as_of", lang, time=as_of))
    refresh_link = f'<a href="{escape(path_qs)}">{escape(i18n.t("portal_footer_refresh", lang))}</a>'
    tz_line = escape(i18n.t("portal_footer_tz", lang, tz=tz_name))
    config_link = f'<a href="/config">{escape(i18n.t("portal_footer_config", lang))}</a>'
    return f"<p>{as_of_line} · {refresh_link} · {tz_line}</p><p>{config_link}</p>{extra}"


def page(
    *,
    lang: i18n.Language,
    title: str,
    current: str,
    pending: int,
    body: str,
    path_qs: str,
    tz_name: str,
    as_of: str,
    flash: str = "",
) -> str:
    """UI.md §3.1/§9.2 contracts 1-3: the full page shell -- viewport meta
    (mandatory, contract 1), `<html lang>` = the resolved render language
    (contract 2), and the fixed `header > .wrap` / `main.wrap#main` /
    `footer > .wrap` order (contract 3). NOT used by the 403 handler
    (`security.py` renders its own fixed, unstyled body) or the 500
    handler (`render_500`, below -- brand only, no nav, since the nav
    itself reads the DB)."""
    nav_html = render_nav(current=current, lang=lang, pending=pending)
    skip_text = escape(i18n.t("portal_skip_to_content", lang))
    footer_html = render_footer(lang=lang, as_of=as_of, tz_name=tz_name, path_qs=path_qs)
    return (
        "<!doctype html>"
        f'<html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{PORTAL_CSS}</style></head>"
        f'<body><a class="vh" href="#main">{skip_text}</a>'
        f'<header><div class="wrap"><p class="brand">{escape(BRAND)}</p>{nav_html}</div></header>'
        f'<main class="wrap" id="main">{flash}{body}</main>'
        f"<footer><div class=\"wrap\">{footer_html}</div></footer>"
        "</body></html>"
    )


def render_500(lang: i18n.Language) -> str:
    """UI.md §5 Screen 10/§9.2 contract 13: brand header, NO nav (the nav
    computes a pending count from the DB -- rendering it inside a 500
    handler risks re-raising inside the error path itself), full
    stylesheet (this is post-gate, owner-only -- unlike the 403, there is
    nothing to hide here), a plain link home. Never a traceback."""
    message = i18n.t("portal_500_body", lang)
    home_link = i18n.t("portal_500_home_link", lang)
    return (
        "<!doctype html>"
        f'<html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(message)}</title><style>{PORTAL_CSS}</style></head>"
        f'<body><header><div class="wrap"><p class="brand">{escape(BRAND)}</p></div></header>'
        f'<main class="wrap decide" id="main"><h1>{escape(message)}</h1>'
        f'<p><a href="/">{escape(home_link)}</a></p></main></body></html>'
    )


def redirect_with_flash(path: str, **params: str) -> "web.HTTPSeeOther":
    """UX.md §5: every mutation is `POST -> 303 -> GET`; flash state
    travels in the query string (`?ok=...`/`?err=...`/`?val=...`, there is
    no session store) and the redirect target carries the `#flash`
    fragment so the browser moves focus+scroll to the flash banner on
    load (UI.md §3.5's own "the CALLER's job")."""
    query = urlencode(params)
    location = f"{path}?{query}#flash" if query else f"{path}#flash"
    return web.HTTPSeeOther(location=location)


# ---------------------------------------------------------------------------
# Shared visual primitives (UI.md §3) -- every page module composes its own
# screens from these rather than hand-rolling markup, so the visual
# vocabulary can't drift between modules built in different passes.
# ---------------------------------------------------------------------------


def tile(label: str, value: str, *, small: str | None = None, wide: bool = False) -> str:
    """UI.md §3.6: label-over-value stat tile, part of the `.tiles` grid."""
    cls = "tile wide" if wide else "tile"
    small_html = f"<small>{escape(small)}</small>" if small else ""
    return f'<div class="{cls}">{escape(label)}<b>{escape(value)}</b>{small_html}</div>'


def panel(heading: str, body_html: str) -> str:
    """UI.md §3.9: `<section class="panel"><h2>...</h2>...</section>`.
    `body_html` is pre-built HTML (already escaped by its own builders),
    not plain text."""
    return f'<section class="panel"><h2>{escape(heading)}</h2>{body_html}</section>'


def panel_or_unavailable(heading: str, render_body: Callable[[], str], *, lang: i18n.Language) -> str:
    """SPEC-LINE-PORTAL.md §3.3/UI.md §3.9: a panel keeps its HEADING even
    when its own data read raises -- one broken panel never blanks the
    whole page, and an absent heading would read as "this feature doesn't
    exist" (a worse lie than "this failed"). Mirrors `core/audit_view.py`'s
    own read-only "never crash on a row it didn't write" discipline,
    extended here to "never crash on a PANEL it couldn't read"."""
    try:
        body_html = render_body()
    except Exception:
        logger.exception("Portal panel %r failed to render; showing the unavailable state", heading)
        body_html = (
            f'<p class="empty warn">{escape(i18n.t("portal_panel_unavailable", lang))}</p>'
            f'<p class="meta">{escape(i18n.t("portal_panel_unavailable_hint", lang))}</p>'
        )
    return panel(heading, body_html)


def bar(pct: float) -> str:
    """UI.md §3.7/§3.8/§9.2 contract 7: the ONLY inline style anywhere in
    the portal. `pct` is clamped to [0, 100] and formatted `%.1f` HERE, so
    no caller can interpolate an unclamped/unformatted number into an
    HTML attribute. `aria-hidden="true"` -- the bar is decoration; the
    number beside it is the accessible content (the caller's job)."""
    clamped = max(0.0, min(100.0, pct))
    return f'<div class="bar" aria-hidden="true"><i style="width:{clamped:.1f}%"></i></div>'


def tag(text: str, *, tier: str = "", word: bool = False) -> str:
    """UI.md §3.16: `.tag` for a level/source enum (`ERROR`, `portal`,
    ...); pass `word=True` for a tag holding a LOCALIZED string (14px
    sans, never below 14px, UI.md §2 Typography's own Thai floor)."""
    classes = "tag word" if word else "tag"
    if tier:
        classes += f" {tier}"
    return f'<span class="{classes}">{escape(text)}</span>'


def empty(message: str, *, tier: str = "mute", extra: str = "") -> str:
    """UI.md §3.19: `.empty` -- `tier="ok"` where empty is the GOOD state
    (affirmative copy), `tier="mute"` (default) where empty is merely a
    fact, `tier="warn"` for "can't read this right now"."""
    return f'<p class="empty {tier}">{escape(message)}</p>{extra}'


def dl(rows: list[tuple[str, str]]) -> str:
    """UI.md §3.20/§9.2 contract 10: `<dl class="dl"><div><dt>...</dt>
    <dd>...</dd></div>...</dl>` -- the per-row `<div>` wrapper is required
    by the stylesheet's `.dl div` selector. `dt` (the label) is always
    escaped here; `dd` (the value) is passed through AS GIVEN so a caller
    can embed a pre-built `.tag`/link -- callers passing plain text must
    escape it themselves before calling this."""
    body = "".join(f"<div><dt>{escape(k)}</dt><dd>{v}</dd></div>" for k, v in rows)
    return f'<dl class="dl">{body}</dl>'


def th_col(text: str) -> str:
    return f"<th scope=\"col\">{escape(text)}</th>"


def th_row(text: str) -> str:
    return f"<th scope=\"row\">{escape(text)}</th>"


def td_cell(label: str, value_html: str, *, head: bool = False, full: bool = False) -> str:
    """UI.md §3.10/§9.2 contract 5, the markup contract every `.collapse`
    table cell MUST satisfy: `data-label` = the column's own `<th>` text
    (localized), so the phone card-collapse CSS can print it via
    `content:attr(data-label)`. `head=True` = the row's own headline cell
    (label suppressed, 18px 600); `full=True` = "label above, value
    below, full width" (UI.md's Detail/Message column treatment).
    `value_html` is pre-built HTML (already escaped by the caller), not
    plain text -- most cells hold a plain value the caller has already
    run through `escape()`, but some (a `.mono` id, a `.tag`) legitimately
    hold markup."""
    classes = []
    if head:
        classes.append("head")
    if full:
        classes.append("full")
    class_attr = f' class="{" ".join(classes)}"' if classes else ""
    return f'<td data-label="{escape(label)}"{class_attr}>{value_html}</td>'


def confirm_disclosure(
    *,
    action: str,
    hidden_fields: dict[str, str],
    summary_text: str,
    body_text: str,
    submit_text: str,
    cancel_href: str,
    cancel_text: str,
    tier: str = "",
) -> str:
    """UI.md §3.12/§9.2 contract 6: a `<form method="post">` wrapping
    `<details class="confirm">` containing EXACTLY a `<summary>` and one
    `<div>` -- the stylesheet's `.confirm>div` selector depends on this
    exact shape. `tier="stop"` for a destructive action (Block) -- Iris's
    rule: destructive actions are OUTLINED (this component's default,
    unfilled `<summary>`), never filled, so a red confirm next to a teal
    one never reads as an alarm."""
    classes = "confirm" if not tier else f"confirm {tier}"
    hidden_html = "".join(
        f'<input type="hidden" name="{escape(name)}" value="{escape(value)}">' for name, value in hidden_fields.items()
    )
    return (
        f'<form method="post" action="{escape(action)}">{hidden_html}'
        f'<details class="{classes}"><summary>{escape(summary_text)}</summary>'
        f"<div><p>{escape(body_text)}</p>"
        f'<div class="actions"><button class="btn">{escape(submit_text)}</button>'
        f'<a class="cancel" href="{escape(cancel_href)}">{escape(cancel_text)}</a></div>'
        "</div></details></form>"
    )


def btn(text: str, *, href: str | None = None, quiet: bool = False, tier: str = "") -> str:
    """UI.md §3.14/§9.2 contract 9: `<button class="btn">`, NEVER
    `<input type="submit">` (the bare `input` selector in §8 styles text
    fields). Pass `href` for the `<a class="btn">` link form (pager,
    "Review ->" style secondary actions); omit it for a real form submit
    button."""
    classes = "btn"
    if quiet:
        classes += " quiet"
    if tier:
        classes += f" {tier}"
    if href is not None:
        return f'<a class="{classes}" href="{escape(href)}">{escape(text)}</a>'
    return f'<button class="{classes}" type="submit">{escape(text)}</button>'


def mono(text: str) -> str:
    """UI.md §3.17: the small/secondary monospace id treatment used in
    lists/cards and audit Detail cells."""
    return f'<span class="mono">{escape(text)}</span>'


def id_block(text: str) -> str:
    """UI.md §3.17: `.id` -- the LARGE, letter-spaced monospace block used
    only on the invite confirm interstitial, so a transposed character in
    a 33-character opaque id is visible."""
    return f'<div class="id">{escape(text)}</div>'
