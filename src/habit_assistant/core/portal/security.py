"""SPEC-LINE-PORTAL.md §4 R-SEC-3/R-SEC-4 (shared surface, admin web
portal, branch `line-version`): the identity gate every portal request
passes through -- GET and POST alike (AC20) -- before any page handler
runs.

R-SEC-4's trust model, stated once here rather than inferred: this
middleware trusts the `Tailscale-User-Login` header ONLY under the
deployment contract that the portal port is reached exclusively through
`tailscale serve` (which injects the header on tailnet traffic and
STRIPS any client-supplied copy of it -- see SPEC-LINE-PORTAL.md's own
"Security boundary decision" section, finding 3, and `deploy/setup.sh`'s
own printed warning never to Funnel this port). The app performs no
additional network-layer identity derivation -- it cannot: Tailscale
Funnel/Serve both proxy to `127.0.0.1`, so the TCP peer this process
actually sees is never the real tailnet IP either way (finding 1).
"""

from __future__ import annotations

import logging

from aiohttp import web

logger = logging.getLogger(__name__)

_IDENTITY_HEADER = "Tailscale-User-Login"

# UI.md Screen 9 (Iris, verbatim -- "gets no stylesheet, no shell, no
# brand, no nav, no footer, no version, no link, no favicon -- nothing"):
# the hardcoded bilingual 403 body. Deliberately NOT `i18n.t()` (the
# requester's language is unknown at this point -- there is no gated
# `owner_login`/`language_pref` to resolve from -- and a config/catalog
# read that raised must never turn a clean 403 into a 500 that leaks a
# traceback) and deliberately NOT built from `layout.py`'s page shell (an
# ~8KB stylesheet with product-specific class names is itself a
# fingerprint on the one page a hostile stranger might actually see, if
# the port is ever mis-Funneled). ~150 bytes, byte-identical on every
# request regardless of config (UI.md §5 Screen 9's own "byte-identical
# on every request" requirement).
FORBIDDEN_BODY = (
    '<!doctype html><html lang="th"><meta charset="utf-8"><title>403</title>'
    "<p>ไม่มีสิทธิ์เข้าถึง · Not authorized</p>"
)


def _forbidden() -> web.Response:
    return web.Response(status=403, text=FORBIDDEN_BODY, content_type="text/html")


@web.middleware
async def identity_gate(request: web.Request, handler):
    """R-SEC-3: registered as the OUTERMOST middleware in `PortalServer`'s
    `web.Application(middlewares=[identity_gate, ...])`, so it runs before
    any other middleware or page handler -- applies to every route,
    including the vendored-font route and every `POST`, no exceptions
    (AC20). Never reveals WHY a request was refused beyond the generic
    403 -- no "wrong user" enumeration, R-SEC-3's own closing line.

    Reads `PortalDeps` off `request.app["portal_deps"]` (set once by
    `PortalServer.build_app`) rather than closing over config directly,
    so this stays a plain, parameter-free `@web.middleware` function
    matching SPEC-LINE-PORTAL.md §5's own interface signature exactly."""
    deps = request.app["portal_deps"]
    portal_config = deps.config.portal

    login = request.headers.get(_IDENTITY_HEADER)
    if portal_config.require_identity_header and not login:
        return _forbidden()
    if portal_config.owner_login and login != portal_config.owner_login:
        return _forbidden()

    return await handler(request)
