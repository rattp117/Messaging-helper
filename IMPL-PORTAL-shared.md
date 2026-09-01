# Implementation — Admin Web Portal, SEQUENTIAL shared surface

> Branch `line-version`. Consumes `SPEC-LINE-PORTAL.md` (32 ACs, §11 shared-surface
> list), `UX.md` (Maya), `UI.md` (Iris). This pass builds the base for four
> PARALLEL page modules (STATUS/USERS/AUDIT/QUOTA) that land next — no page
> module exists yet, by design (§11: "shared surface, built first, sequentially").

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/config.py` | Modified | Added `PortalConfig` (§2.1) + `Config.portal` + the unconditional port-collision `model_validator` (R-SEC-2/AC7). |
| `src/habit_assistant/core/portal/__init__.py` | Created | Package marker, no side effects at import time. |
| `src/habit_assistant/core/portal/stats.py` | Created | `RuntimeStats` (R-STATS-1) + `RingBufferHandler` (R-STATS-2). |
| `src/habit_assistant/core/portal/security.py` | Created | `identity_gate` middleware (R-SEC-3/R-SEC-4, AC3–AC6/AC20) + the hardcoded, unstyled 403 body (UI.md Screen 9). |
| `src/habit_assistant/core/portal/layout.py` | Created | Iris's UI.md §8 stylesheet (verbatim + one `@font-face` addition, E2), the page shell, nav+pending, flash, footer, POST-redirect-GET helper, escaping, and the shared visual-primitive builders (tile/panel/bar/tag/empty/dl/td_cell/confirm_disclosure/btn/mono/id_block), `render_500`. |
| `src/habit_assistant/core/portal/server.py` | Created | `PortalDeps`, `PortalServer` (second aiohttp listener, R-SEC-5/R-SEC-6), the vendored-font route (E2 option b), `build_portal`/`cancel_task` (the two functions `core/app.py` calls), `REGISTERED_MODULES` (empty — the four page modules append here at integration). |
| `src/habit_assistant/core/access.py` | Modified | Extracted `approve_user`/`block_user` (source-parameterized, R-USERACT-1); `execute_admin` now delegates (behavior byte-identical, regression-tested); `handle_gate`'s `access_request` push gains the optional portal-URL line (Q2 ruling). |
| `src/habit_assistant/storage/db.py` | Modified | `recent_audit(limit, offset=0)` (extended, backward-compatible), `audit_total`, `recent_logs_metadata` (privacy-critical, R-AUDIT-3), `monthly_push_history`, `push_by_user`. |
| `src/habit_assistant/core/i18n.py` | Modified | `portal_access_request_hint` + 15 shared-shell `portal_*` keys (nav, footer, panel-unavailable, skip-link, 500). Per-page microcopy is explicitly NOT here — that's each page module's own pass. |
| `src/habit_assistant/core/app.py` | Modified | Wires the portal in: `build_portal(...)` call, `portal_mark_event()` in `_on_message`/`_on_callback`, the portal task's lifecycle. Net **+1 line** (747 vs. baseline 746) — see "Known limitations" for how the 746/750 ceiling was held. |
| `config.toml.line` | Modified | Commented `[portal]` block, `enabled = false`. |
| `deploy/setup.sh` | Modified | New step 11: prints (never runs) `tailscale serve --bg <port>` when `[portal] enabled = true`; never funnels the port. |
| `docs/DEPLOY-LINE.md` | Modified | New "§9 Admin portal (optional)" section; renumbered old §9→§10, §10→§11. |
| `tests/test_config.py` | Modified | +8 tests: `PortalConfig` defaults, the port-collision validator (enabled and disabled), `load_config` round-trip. |
| `tests/test_access.py` | Modified | +7 tests: `execute_admin` still records `source="admin"` post-extraction (regression guard), `approve_user`/`block_user` with `source="portal"`, push-failure doesn't undo the approve, DB-failure propagates with no audit row. |
| `tests/test_portal_db.py` | Created | 13 tests for the five new/extended `db.py` helpers, including the privacy contract on `recent_logs_metadata`. |
| `tests/test_portal_stats.py` | Created | 9 tests for `RuntimeStats`/`RingBufferHandler`. |
| `tests/test_portal_security.py` | Created | 14 tests for `identity_gate` (AC3–AC6, AC20) + the 403 body's byte-shape. |
| `tests/test_portal_layout.py` | Created | 24 tests: escaping, markup contracts, the stylesheet's budget/font-face/motion-absence. |
| `tests/test_portal_server.py` | Created | 23 tests: `PortalDeps`/`PortalServer.build_app`, middleware ordering, the font route, `build_portal`/`cancel_task`. |
| `tests/test_portal_integration.py` | Created | 5 end-to-end tests through the REAL `core/app.py:async_main` wiring — AC1/AC2, the structural isolation proof (both directions), the ring-buffer install. |
| `tests/test_portal_deploy.py` | Created | 5 tests for the `setup.sh`/`DEPLOY-LINE.md`/`config.toml.line` additions. |

## How it works

`core/app.py:async_main` calls `build_portal(config, db, scheduler, channel, owner_id)`
right after `scheduler.start()`. That single function (in `core/portal/server.py`)
is a no-op returning `(_noop_mark_event, None)` unless `config.portal.enabled and
config.channel.type == "line"`; otherwise it builds `RuntimeStats`, installs a
`RingBufferHandler` on the `"habit_assistant"` logger, wraps everything into
`PortalDeps`, constructs a `PortalServer`, and returns `(stats.mark_event,
asyncio.create_task(run_portal_server(server)))`. `core/app.py` calls the
returned `mark_event` from both `_on_message`/`_on_callback` (always safe — a
callable no-op when disabled) and cancels the returned task in its `finally`
block via the shared `cancel_task` helper, mirroring `health_task`'s own
lifecycle. `PortalServer.build_app()` registers `identity_gate` as the
OUTERMOST middleware (so an unauthenticated request never reaches routing,
error handling, or any page handler), then `_error_middleware` (catches any
handler crash → `layout.render_500`), then the vendored-font route, then every
module in `REGISTERED_MODULES` (empty this pass) via its own `register(app,
deps)` hook. The 8080 LINE-webhook listener and the 8081 portal listener are
two structurally separate `aiohttp.web.Application` instances in two separate
`asyncio.Task`s — Funnel/Serve isolation holds by construction, not by
convention (proven in `test_portal_integration.py`).

## Smoke test done

- `python -c "from habit_assistant.core.portal... import *"` — clean imports,
  no circular-import issues.
- Direct REPL check: `Config()` (defaults) → `portal.enabled=False`,
  `bind_port=8081` vs. `line.bind_port=8080`; `Config(portal={enabled:True,
  bind_port:8080})` → raises `ValueError` ("must differ").
- `py_compile` across every new/modified file — clean.
- **Full LINE gate**: `pytest -m "not telegram_only and not llm_only" -n auto`
  → **5253 passed, 2 failed, 4 skipped, 1 xfailed** (89s). The 2 failures
  (`test_digest.py::test_run_daily_digest_increments_push_ledger_exactly_once_per_user`,
  `test_line_c_gaps.py::test_push_ledger_increments_exactly_once_per_successful_push_not_per_composed_user`)
  are **pre-existing and unrelated** — reproduced identically on the clean
  baseline commit `b9eec9c` via `git stash`, with zero portal code present.
  They touch `core/digest.py`/push-ledger accounting, a module this pass never
  opens. Given today's date rolled to `2026-09-01` mid-session, my best guess
  is a real-clock vs. injected-`clock` mismatch somewhere in the digest push
  path (both fixed-clock tests assert on `"2026-08"` and both got `0`, as if
  the push landed under the real current month instead) — but I have not
  investigated `core/digest.py` itself, since it is out of this shared-
  surface's scope. **Flagging to Archi rather than silently fixing or
  ignoring.**
- Dedicated shared-surface tests: **101 new tests, all green** — 86 in new
  `tests/test_portal_*.py` files, +15 across `tests/test_config.py`/
  `tests/test_access.py`.
- Structural isolation proved live (not just by code inspection):
  `test_line_webhook_app_has_no_admin_or_portal_routes` binds a REAL LINE
  webhook + REAL portal via `main_module.async_main`, then GETs `/`, `/users`,
  `/audit`, `/activity`, `/quota`, `/config`, `/fonts/...` against the LINE
  port and asserts `404` on every one; `test_portal_app_rejects_headerless_requests_on_every_route`
  proves the portal's identity gate refuses GET/POST on both a route that
  exists (the font) and one that doesn't yet (`/`, `403` not `404` —
  proving the gate runs before route resolution).

## Maps to acceptance criteria (shared-surface scope only)

Per SPEC-LINE-PORTAL.md §11, this pass owns **AC1–AC7, AC20 (gate half),
AC31 (shell half), AC32 (integration scaffolding)**. AC8–AC30 belong to the
four parallel modules and are **not yet implemented** (no page exists to
satisfy them) — that is by design, not a gap in this pass.

- **AC1** → `config.py:Config._portal_port_distinct_from_line_port` is a no-op
  gate; `core/app.py:build_portal` returns `(noop, None)` when
  `portal.enabled=False` → `core/portal/server.py:build_portal`;
  live-proved in `test_portal_disabled_by_default_binds_nothing`.
- **AC2** → same function, `channel.type != "line"` branch →
  `test_portal_never_constructs_on_telegram_even_if_enabled`.
- **AC3** → `core/portal/security.py:identity_gate` →
  `test_missing_header_refused_with_no_admin_content`.
- **AC4** → same, `owner_login` mismatch branch →
  `test_owner_login_pin_refuses_any_non_matching_login`.
- **AC5** → same, success path → `test_correct_header_with_no_owner_login_pin_proceeds`.
- **AC6** → same, forged-login branch, parametrized (4 cases) →
  `test_owner_login_pin_refuses_any_non_matching_login`.
- **AC7** → `config.py` `model_validator` → `test_portal_enabled_with_colliding_bind_port_raises_value_error`
  + `test_load_config_toml_portal_bind_port_collision_raises_config_error`.
- **AC20 (gate half — page-agnostic proof)** → `test_post_without_header_refused_no_write_reached`
  + `test_portal_app_rejects_headerless_requests_on_every_route`'s
  `/users/approve` POST case (no USERS module exists yet, but the gate
  already refuses the POST before any handler could run).
- **AC31 (shell half)** → `core/portal/layout.py:page()` sets `<html lang="{lang}">`
  from the caller's resolved language and every shell string
  (nav/footer/skip-link/panel-unavailable/500) resolves through `i18n.t()`
  with `portal_*` keys, both `en`+`th` — `test_page_shell_carries_viewport_meta_and_resolved_lang`,
  `test_render_500_localizes_to_thai`. The one deliberate, flagged exception
  (UI.md Screen 9) is the 403 body — hardcoded bilingual, not `i18n.t()` —
  proved in `test_forbidden_body_carries_no_shell_no_stylesheet_no_version`.
- **AC32 (integration scaffolding)** → `PortalServer.build_app()`'s
  `for register in self._modules: register(app, self._deps)` loop
  (R-INT-1) → `test_build_app_calls_every_registered_module_with_the_app_and_deps`;
  the "disabled portal, none of these routes exist" half is AC1's own test.
  The full AC32 (four real modules registered) is **not yet testable** —
  correctly deferred to the integration Vera pass after STATUS/USERS/AUDIT/
  QUOTA land.

**Not yet covered (explicitly out of this pass's scope, per §11):** AC8–AC19
(STATUS/USERS), AC21–AC30 (USERS/AUDIT/QUOTA page content). Each parallel
module's own Luna+Vera track owns its ACs.

## Known limitations

- **Pre-existing test failures** (2, unrelated) — see "Smoke test done" above.
  Escalating to Archi; did not attempt a fix (out of scope, unfamiliar
  module, and Luna's own discipline against speculative unrelated changes).
- **`core/app.py` line-count ceiling (746→750) was razor-thin.** Getting a
  genuinely new subsystem wired in at net **+1 line** required: creating the
  asyncio task *inside* `build_portal` (not in `core/app.py`), returning a
  bound `mark_event` callable instead of a `RuntimeStats | None` (so call
  sites need no `is not None` guard), and extracting a small
  `cancel_task(task)` helper into `server.py` that `core/app.py` now uses for
  **both** `health_task` and `portal_task` (a minor, behavior-preserving
  simplification of pre-existing code, done only because the ceiling left no
  other clean option — verified with a dedicated regression test on
  `cancel_task` itself). If a future feature needs to touch `core/app.py`
  again, there is currently only ~2 lines of headroom left before 750 —
  worth a note to Irine/Archi that this file's own budget may need revisiting.
- **UI.md §8's stylesheet is not quite byte-verbatim.** One `@font-face` rule
  was added (E2, option (b), per the dispatch note's explicit instruction —
  "beyond Iris's own default"). `local("Noto Sans Thai")` keeps this
  zero-cost for any device that already has the system font; only a bare
  Linux/BSD desktop with none installed would ever fetch the same-origin
  `/fonts/NotoSansThai-Regular.ttf` route. Flagging so Vera/Iris can confirm
  this reading of the escalation is the intended one.
- **`PORTAL_MODULES`/`REGISTERED_MODULES` is empty.** Every portal route
  currently 404s except the vendored-font route — expected and correct for
  a shared-surface-only pass. The integration step appends each module's
  `register` import to `core/portal/server.py:REGISTERED_MODULES` once its
  own Luna+Vera track reports PASS.
- **Q2 (portal URL in the `access_request` push) is implemented** as an
  Archi ruling beyond SPEC-LINE-PORTAL.md §9 OQ2's own conservative default
  — gated on `portal.enabled and portal.public_url and channel.type=="line"`,
  so every existing/default config is unaffected (byte-identical push) until
  an operator both enables the portal AND fills in `public_url`.
- **Version not bumped.** Per the dispatch note ("this releases as
  line/v1.3.0 eventually — do NOT bump yet"), `pyproject.toml`/`__init__.py`
  version strings are untouched.
- Nothing was committed — left for Archi, per the dispatch note.
