"""SPEC-LINE-PORTAL.md §4 R-SEC-1/R-SEC-5/R-SEC-6/R-INT-1 (shared surface,
admin web portal, branch `line-version`): `PortalServer` -- the SECOND
aiohttp listener (mirrors `channels/line_webhook.py:LineWebhookServer`'s
own AppRunner+TCPSite shape, R-SEC-6), `PortalDeps` (the one dependency
bag every page module's `register(app, deps)` hook receives, R-INT-1),
and the vendored Thai font route (UI.md §11 escalation E2, option (b)).

Serve/Funnel isolation IS the security boundary here (SPEC-LINE-
PORTAL.md's own "Security boundary decision" section): this listener
binds its OWN `aiohttp.web.Application` instance on its OWN port, wired
into `core/app.py` as a SEPARATE `asyncio.create_task` alongside (never
inside) `channels/line_webhook.py:LineWebhookServer`'s app -- the two
never share a router, so the public-Funnel-facing app structurally has
zero admin/portal routes regardless of what this module registers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from aiohttp import web

from habit_assistant.core import i18n, user_prefs
from habit_assistant.core.fonts import FONT_PATH as _THAI_FONT_PATH
from habit_assistant.core.portal import audit, layout, quota, status, users
from habit_assistant.core.portal.security import identity_gate
from habit_assistant.core.portal.stats import RingBufferHandler, RuntimeStats

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from habit_assistant.channels.base import Channel
    from habit_assistant.config import Config
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)


@dataclass
class PortalDeps:
    """SPEC-LINE-PORTAL.md §5: the one dependency bag every page module's
    `register(app, deps)` hook receives (R-INT-1). Built once by
    `core/app.py` and shared by every route on this listener -- the same
    `db`/`channel` instances the LINE channel itself uses (R-SEC-5: no
    second sqlite connection, no thread)."""

    db: "Database"
    config: "Config"
    scheduler: "AsyncIOScheduler"
    channel: "Channel"
    stats: RuntimeStats
    ring: RingBufferHandler
    owner_id: str


RegisterFn = Callable[[web.Application, PortalDeps], None]


async def _handle_font(request: web.Request) -> web.Response:
    """UI.md §11 escalation E2, option (b): serves the same vendored TTF
    `core/fonts.py` already ships for chart PNGs, now ALSO same-origin
    for the portal's own `@font-face` fallback (`layout.py:PORTAL_CSS`).
    Gated by `identity_gate` like every other portal route (tailnet-only,
    per the dispatch note's own "tailnet-only like all portal routes")."""
    del request
    if not _THAI_FONT_PATH.is_file():
        return web.Response(status=404)
    return web.Response(body=_THAI_FONT_PATH.read_bytes(), content_type="font/ttf")


@web.middleware
async def _error_middleware(request: web.Request, handler):
    """SPEC-LINE-PORTAL.md §3.3 (shared surface): "any unhandled handler
    exception -> 500 with a generic localized body; the traceback goes to
    the log (and thus the ring buffer), never to the response." Runs
    INSIDE `identity_gate` (registered second, R-SEC-3 must run first --
    see `PortalServer.build_app`) so an unauthenticated request never
    reaches this at all, and the 500 body is safe to localize (the
    request is already known to be from the owner)."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise  # a deliberate redirect/404/etc. from a handler -- not a crash.
    except Exception:
        logger.exception("Unhandled exception in a portal handler for %s %s", request.method, request.path)
        deps: PortalDeps = request.app["portal_deps"]
        lang = _owner_language(deps)
        return web.Response(status=500, text=layout.render_500(lang), content_type="text/html")


def _owner_language(deps: PortalDeps) -> i18n.Language:
    """Best-effort language resolution for the 500 page -- mirrors
    `core/access.py:_resolve_unprompted_language_for`'s own fail-open
    shape (a DB read that fails here must not turn a 500 into an
    unhandled crash)."""
    try:
        pref = user_prefs.stored_language_pref(deps.db, deps.owner_id)
    except Exception:
        pref = "auto"
    return i18n.resolve_unprompted_language(deps.config, user_pref=pref)


class PortalServer:
    """R-SEC-5/R-SEC-6: runs in the SAME event loop as the LINE channel,
    sharing the one `Database` connection -- no second sqlite handle, no
    thread. `serve()` mirrors `LineWebhookServer.serve`'s own
    AppRunner+TCPSite, run-forever-until-cancelled shape exactly."""

    def __init__(self, *, bind_host: str, bind_port: int, deps: PortalDeps, modules: list[RegisterFn]) -> None:
        self._bind_host = bind_host
        self._bind_port = bind_port
        self._deps = deps
        self._modules = modules

    def build_app(self) -> web.Application:
        """R-INT-1: every page module registers into ONE `Application` via
        its own `register(app, deps)` hook; no module imports another
        module's file. `identity_gate` is listed FIRST so it wraps
        OUTERMOST -- it runs before `_error_middleware` and before any
        page handler, so an unauthenticated request never reaches
        anything past the 403 (AC20: applies to GET and POST alike)."""
        app = web.Application(middlewares=[identity_gate, _error_middleware])
        app["portal_deps"] = self._deps
        app.router.add_get(layout.THAI_FONT_ROUTE, _handle_font)
        for register in self._modules:
            register(app, self._deps)
        return app

    async def serve(self) -> None:
        app = self.build_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._bind_host, self._bind_port)
        await site.start()
        logger.info("Admin portal listening on %s:%d", self._bind_host, self._bind_port)
        try:
            await asyncio.Event().wait()  # runs forever until this task is cancelled
        finally:
            await runner.cleanup()


# SPEC-LINE-PORTAL.md §4 R-INT-1: the four PARALLEL page modules'
# `register(app, deps)` hooks, all four now landed here by the integration
# pass (line/v1.3.0). During parallel development STATUS self-registered
# itself here at build time while USERS/AUDIT/QUOTA left this list for the
# integration step to update (IMPL-PORTAL-shared.md's own "the integration
# step appends each module's register import" note, which only USERS/
# AUDIT/QUOTA actually followed -- confirmed by
# TEST-PORTAL-quota.md's own `test_quota_register_not_yet_in_server_
# registered_modules_known_gap` pin test, which this line flips green).
# RULING (integration, settling the precedent both ways left open): a page
# module NEVER edits this list itself, regardless of how small the change
# looks -- registration into the live `PortalServer` is INTEGRATION's job
# alone, exactly like every other cross-module wiring decision in this
# codebase (`core/app.py`'s own job registration, `core/commands.py`'s own
# dispatch table). STATUS's self-registration during the parallel pass was
# the deviation, not the model to repeat -- kept here (no reason to revert
# working code) but not to be repeated by a future module.
REGISTERED_MODULES: list[RegisterFn] = [status.register, users.register, audit.register, quota.register]


def _noop_mark_event() -> None:
    return None


def build_portal(
    config: "Config",
    db: "Database",
    scheduler: "AsyncIOScheduler",
    channel: "Channel",
    owner_id: str,
    modules: list[RegisterFn] | None = None,
) -> tuple[Callable[[], None], "asyncio.Task | None"]:
    """SPEC-LINE-PORTAL.md §4 R-SEC-1/R-SEC-6/R-STATS-1/R-STATS-2/R-INT-2/
    R-INT-3: the entire conditional portal-construction-AND-launch
    sequence -- kept HERE rather than inlined in `core/app.py`, so that
    reserved wiring file gains only a thin call (the dispatch note's own
    line-count-ceiling instruction). `modules` defaults to
    `REGISTERED_MODULES`. Also creates and returns the running
    `asyncio.Task` (R-SEC-6) -- safe to create this early (before
    `core/app.py`'s own rich-menu registration and `channel.run()`): the
    task only needs `deps`, all of which are already fully constructed by
    the time any caller reaches this function.

    Returns `(_noop_mark_event, None)` unless `config.portal.enabled and
    config.channel.type == 'line'` (R-SEC-1/AC1/AC2) -- the returned
    first element is ALWAYS callable (a shared no-op when the portal is
    off), so `core/app.py`'s `_on_message`/`_on_callback` never need an
    `is not None` guard of their own; it is otherwise `RuntimeStats.
    mark_event`, bound to the one `RuntimeStats` this call constructs."""
    if not (config.portal.enabled and config.channel.type == "line"):
        return _noop_mark_event, None
    stats = RuntimeStats()
    ring = RingBufferHandler(config.portal.log_ring_size)
    logging.getLogger("habit_assistant").addHandler(ring)
    deps = PortalDeps(db=db, config=config, scheduler=scheduler, channel=channel, stats=stats, ring=ring, owner_id=owner_id)
    server = PortalServer(
        bind_host=config.portal.bind_host,
        bind_port=config.portal.bind_port,
        deps=deps,
        modules=modules if modules is not None else REGISTERED_MODULES,
    )
    return stats.mark_event, asyncio.create_task(run_portal_server(server))


async def cancel_task(task: "asyncio.Task | None") -> None:
    """Generic "cancel and await, swallowing the resulting CancelledError"
    helper -- used by `core/app.py` for `portal_task` so that reserved
    wiring file's own task-lifecycle code stays a one-line call (the
    dispatch note's own line-count-ceiling instruction). No portal-
    specific logic; a no-op for `task=None`."""
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def run_portal_server(portal_server: PortalServer) -> None:
    """R-SEC-6: "a failure to start the portal is logged and must not
    crash the main channel loop (fail-open on the operator surface; the
    bot itself keeps serving users)." `core/app.py` wraps `portal_server.
    serve()` in this rather than awaiting it directly inside the task, so
    a bind failure (e.g. the port is already in use) is caught and logged
    instead of becoming an unretrieved task exception. `asyncio.
    CancelledError` is re-raised so normal shutdown (the caller cancels
    this task) still works."""
    try:
        await portal_server.serve()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Admin portal server failed to start/run; continuing without it")


__all__ = ["PortalDeps", "PortalServer", "RegisterFn", "build_portal", "cancel_task", "run_portal_server"]
