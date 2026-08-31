# Spec — LINE edition: Admin Web Portal

> Branch product (`line-version`), edition SemVer. **Target baseline:** `1.2.0+line` (assumes the in-flight realtime-mode + `push_cap` release — `SPEC-LINE-1.2.md` — has landed; the portal surfaces its quota state). **This release:** additive, LINE-only, LLM stays permanently OFF. Telegram edition on `main` is byte-unchanged. Baseline design: `SPEC-LINE.md` (aiohttp inbound server, reply-buffer, digest, quota ledger), `SPEC-LINE-1.2.md` (realtime push, `push_cap`).
>
> **UI FLAG FOR ARCHI:** this portal has a real, non-trivial web UI (multiple screens, forms, tables, a gauge). Per the standard workflow, **Maya (UX) then Iris (UI) must run after this spec is approved.** This spec defines features, functions, data contracts, routes, and the security model only — it deliberately does not fix visual design. The HTML skeletons here are structural placeholders for Luna to hang Iris's tokens on, not the final look. Default theme guidance: **Modern & Clean**, must match the existing LINE-edition design language already established (teal accent rich-menu, Thai-first — commit `0f4e310`).

---

## 1. Problem statement

The habit-assistant LINE edition currently has exactly one operator surface: owner-only chat commands (`/users`, `/approve`, `/block`, `/audit`, and the in-digest quota-warning line). That is fine for one-off actions but poor for *situational awareness* — the owner cannot see, at a glance, whether the service is healthy, how much of the monthly LINE push quota is spent, who is waiting for approval, or what the scheduler will do next. We build a **read-first admin web portal**, served **only to the owner's own Tailscale devices** (never the public internet), that renders the live state of the running process and lets the owner action pending approvals with one click. Success = the owner opens one tailnet URL and sees service health, quota, users, and audit in their own language (TH/EN), and can approve/block/invite without typing an opaque `U…` id — with **zero new runtime dependencies** and **zero public exposure of any admin function**.

## 2. Inputs

The portal is a **read model over already-existing runtime state**. It introduces no new user-authored input except three POST actions (approve / block / invite) and, as *could*-tier, a manual digest trigger. Its inputs are:

### 2.1 Config additions (`config.toml`) — new `[portal]` section

```toml
[portal]
enabled = false          # NEW. Master switch. Default OFF -> byte-identical to today (AC1).
bind_host = "127.0.0.1"  # NEW. The portal's own aiohttp listener host.
bind_port = 8081         # NEW. A DIFFERENT port from [line].bind_port (8080). MUST differ (AC7).
owner_login = ""         # NEW. Optional. The Tailscale-User-Login (e.g. "alice@example.com")
                         #   allowed to reach the portal. Empty = accept ANY authenticated
                         #   tailnet identity (network boundary only). Set it to pin to one
                         #   identity (defense in depth vs a multi-user tailnet).
require_identity_header = true  # NEW. When true, a request with NO Tailscale-User-Login
                                #   header is refused (403). Fails closed if the port is ever
                                #   mis-exposed via Funnel. See §Security boundary.
log_ring_size = 200      # NEW. How many recent WARNING+ log records the in-process ring
                         #   buffer keeps for the "recent errors" panel.
```

- All keys defaulted; an absent `[portal]` section uses the class defaults (same convention as `LineConfig`/`DigestConfig`). `enabled=false` by default so a deploy that does nothing new is untouched.
- A field validator MUST reject `bind_port == config.line.bind_port` (they cannot share a port — see §Security boundary, and this is also a Tailscale hard constraint).

### 2.2 Runtime inputs the portal reads (all already present)

| Datum | Source |
|---|---|
| Version | `habit_assistant.__version__` (`"1.2.0+line"`) |
| Channel type, Ollama mode | `config.channel.type`, `config.ollama.enabled` |
| Uptime | new in-process `RuntimeStats.started_at` (set at startup) |
| Webhook last-event time | new in-process `RuntimeStats.last_event_at` (set per inbound event) |
| Scheduler jobs + next-run | `scheduler.get_jobs()` → `job.id`, `job.next_run_time` |
| Push quota (used) | `db.monthly_push_total(yyyymm)` |
| Push cap | `config.digest.push_cap` (realtime) / `config.digest.warn_cap` (digest) |
| DB size | `os.path.getsize(config.app.db_path)` (+ `-wal`/`-shm` sidecars) |
| Media dir size | sum of file sizes under `config.line.media_dir` |
| Backups | glob `config.backup.dir/habits-*.db` (name = timestamp), size, mtime |
| Recent errors | new in-process `RingBufferHandler` (last N WARNING+ log records) |
| Users (pending/active) | `db.list_users()`, `db.get_user(chat_id)` |
| Per-user stats | `db.last_log(user_id)`, `core/streaks.py`, `users.digest_opt_out`, `users.language_pref` |
| Audit rows | `db.recent_audit(limit)` → extended to `(limit, offset)` + `db.audit_total()` |
| Activity feed | `db.recent_logs_metadata(...)` (new; metadata only, never `raw_message` for text habits) |
| Monthly push history | `db.monthly_push_history()` (new; per-`yyyymm` totals + current-month per-user) |

## 3. Outputs

Server-rendered HTML pages (one process, `text/html; charset=utf-8`), plus POST endpoints that mutate then redirect (`303 See Other`) back to the referring page. No JSON API, no SPA, no client build step.

### 3.1 Routes (all under the portal listener, all owner-gated)

| Method | Path | Purpose | Module |
|---|---|---|---|
| GET | `/` | Status / report dashboard | STATUS |
| GET | `/users` | Pending + active user management | USERS |
| POST | `/users/approve` | Approve a pending chat_id (form: `chat_id`) | USERS |
| POST | `/users/block` | Block a chat_id (form: `chat_id`) | USERS |
| POST | `/users/invite` | Pre-approve a chat_id (form: `chat_id`) | USERS |
| GET | `/audit?page=N` | Paginated audit-log viewer | AUDIT |
| GET | `/activity` | Recent activity feed (metadata only) | AUDIT |
| GET | `/quota` | Monthly push history + digest states | QUOTA |
| GET | `/config` | Effective config, secrets redacted (*could*) | QUOTA |
| POST | `/quota/digest-run` | Manual digest trigger, confirm-gated (*could*) | QUOTA |

### 3.2 Example — status page (structural, pre-Iris)

```html
<h1>Habit Assistant — Status</h1>
<section class="tiles">
  <div class="tile"><span>Version</span><strong>1.2.0+line</strong></div>
  <div class="tile"><span>Channel</span><strong>line</strong></div>
  <div class="tile"><span>Ollama</span><strong>off</strong></div>
  <div class="tile"><span>Uptime</span><strong>3d 4h 12m</strong></div>
  <div class="tile"><span>Last webhook event</span><strong>2026-08-31 14:03</strong></div>
</section>
<section class="quota">
  <h2>Push quota — Aug 2026</h2>
  <meter value="182" max="15000"></meter>
  <p>182 / 15000 (1.2%) · mode: realtime</p>
</section>
<section class="jobs">
  <h2>Scheduler</h2>
  <table><tr><th>Job</th><th>Next run</th></tr>
    <tr><td>minutely_tick</td><td>2026-08-31 14:05:00</td></tr>
    <tr><td>daily_digest</td><td>2026-08-31 20:00:00</td></tr>
  </table>
</section>
<section class="errors"><h2>Recent errors</h2><!-- ring buffer, or empty state --></section>
```

### 3.3 Error / degradation responses

- Unauthorized (missing/wrong identity header): **`403 Forbidden`**, a minimal body (no admin content, no stack trace, no version string).
- A per-panel data read that raises (e.g. `getsize` on a missing file, a DB hiccup) is **caught per panel** and rendered as a localized "unavailable" placeholder — one broken panel never blanks the whole page (mirrors the codebase-wide fail-open posture in `core/audit_view.py`, `core/health.py`).
- A POST whose `chat_id` is missing/unresolvable → the page re-renders with a localized inline error, `303` back, **no** DB write, **no** audit row.
- Any unhandled handler exception → `500` with a generic localized body; the traceback goes to the log (and thus the ring buffer), never to the response.

## Security boundary decision (a / b / c) — DEDICATED SECTION

This is the load-bearing decision. The deployment reality: the app's aiohttp server binds `127.0.0.1:8080` and **Tailscale Funnel** exposes it to the **entire public internet** at `https://notiserver.tail6ea7.ts.net` (that is how LINE reaches `/callback`). An admin portal must **not** be publicly reachable.

### Research findings (verified against Tailscale's own docs — sources below)

1. **Both Funnel and Serve proxy to a local `127.0.0.1` port.** Funnel's own example is `proxy http://127.0.0.1:3000`; Serve's is `tailscale serve 3000` → `127.0.0.1:3000`. **Consequence:** the aiohttp app sees the TCP peer as `127.0.0.1` for *both* public-Funnel traffic and tailnet-Serve traffic. **Source-IP gating on the CGNAT range `100.64.0.0/10` is therefore useless** — the forwarded request never presents the tailnet IP; it presents localhost. This **kills option (b) as literally specified.**
2. **Serve and Funnel cannot share a port.** Tailscale docs, verbatim: *"The same port number cannot be used for Serve (available only within the tailnet) and Funnel (available within the tailnet and to the public) at the same time."* The most recent `serve`/`funnel` command flips the port's visibility wholesale. **Consequence:** you cannot have one port that is simultaneously public (for `/callback`) and tailnet-only (for the portal). A shared-port design is **architecturally impossible**, independent of any header trick. This **independently kills option (b).**
3. **Serve injects identity headers and strips spoofed copies; Funnel does not inject them.** Serve adds `Tailscale-User-Login`, `Tailscale-User-Name`, `Tailscale-User-Profile-Pic`, and (verbatim) *"If Serve finds the following headers on an incoming request, it will remove them for security reasons, to avoid header spoofing."* Public Funnel traffic carries **no** identity header. **Consequence:** a portal served via `tailscale serve` receives a trustworthy `Tailscale-User-Login` that Tailscale guarantees against spoofing — a strong, free identity signal for defense-in-depth, and a fail-closed tripwire if the port is ever mis-Funneled (public requests arrive header-less → 403).

### Option scorecard

| Option | Verdict | Why |
|---|---|---|
| **(a) Second listener on a second port, tailnet-only via `tailscale serve`** | **RECOMMENDED** | The portal binds `127.0.0.1:8081`; only `tailscale serve --bg 8081` points at it (never Funnel). The public Funnel stays on `8080` for `/callback`. The tailnet **is** the auth — only the owner's own Tailscale devices reach it, zero passwords. A misconfiguration would have to *actively* Funnel 8081 (a deliberate wrong command), not merely fail a check — and even then the identity-header requirement (R-SEC-3) fails closed. Optional `owner_login` pin adds a second, spoof-proof factor. |
| **(b) Same port, source-IP / header gating** | **REJECTED** | Fails twice over: forwarded traffic is always `127.0.0.1` (finding 1, source-IP gating impossible), **and** a port cannot be both Funnel and Serve (finding 2). A header-only gate on a *public* port would put every admin function one header-parsing bug away from the internet — exactly the boundary the task says to get right or reject. |
| **(c) Password / token auth on public routes** | **REJECTED** | Weakest: puts the admin surface on the public internet behind a shared secret — exposed to brute force, credential stuffing, and any auth bug, forever, with no network boundary. Unjustified when the owner already runs Tailscale (the entire deployment depends on it). |

### Recommendation (firm)

**Adopt option (a).** Concretely:
- A **second aiohttp listener** on `config.portal.bind_host:config.portal.bind_port` (default `127.0.0.1:8081`), run as an **asyncio task in the same event loop** as the LINE channel (so it shares the one `Database` connection safely — no threads, no second sqlite handle).
- Exposed to the tailnet **only** via `tailscale serve` (never `tailscale funnel`). `deploy/setup.sh` prints the `tailscale serve --bg 8081` command (documentation, not executed — same posture as the existing Funnel line, R-D3).
- A **security middleware** (R-SEC-*) that requires the `Tailscale-User-Login` header (when `require_identity_header=true`) and, when `owner_login` is set, requires it to equal `owner_login` — else `403`. This gates **every** route including POSTs.
- The `habit-assistant-line.service` unit is unchanged in how it runs the process; the portal is just another task inside it. Deploy docs gain the `tailscale serve` step and a warning to **never** `tailscale funnel` the portal port.

## 4. Behavior rules

Rule ids: `R-SEC-*` security/host, `R-STATUS-*`, `R-USER-*`, `R-AUDIT-*`, `R-QUOTA-*`, `R-I18N-*`, `R-INT-*`.

### Shared surface (built first, sequentially)

- **R-SEC-1 (opt-in, LINE-only, off = no-op)** The portal is constructed and its listener started **only** when `config.portal.enabled` is `True` **and** `config.channel.type == "line"`. With `enabled=false` (default) nothing binds, no route exists, and process behavior is byte-identical to the pre-portal build (AC1). On Telegram the portal never constructs regardless of `enabled` (AC2).
- **R-SEC-2 (distinct port)** `config.portal.bind_port` MUST differ from `config.line.bind_port`; a config field validator raises `ConfigError` at load if they are equal (AC7). Default `8081` vs `8080`.
- **R-SEC-3 (identity gate)** A middleware runs before every handler. When `require_identity_header` is true and the request has no `Tailscale-User-Login` header → `403` with a minimal body (AC3). When `owner_login` is non-empty and `Tailscale-User-Login != owner_login` → `403` (AC4). Otherwise the request proceeds (AC5). The gate applies to **GET and POST alike** (AC20). The middleware never reveals *why* it refused beyond a generic 403 (no "wrong user" enumeration).
- **R-SEC-4 (trust model is documented, not inferred)** The middleware trusts `Tailscale-User-Login` **only** under the deployment contract that the port is reached exclusively through `tailscale serve` (which strips client-supplied copies). This contract is stated in `deploy/` docs and in the config comments. The app performs no additional network-layer identity derivation (it cannot — the peer is always `127.0.0.1`, finding 1).
- **R-SEC-5 (same-loop, shared DB)** The portal listener runs as an `asyncio.create_task` in `core/app.py`'s existing event loop, sharing the single `Database` instance already threaded through the app. It MUST NOT open a second sqlite connection or run in a thread (the connection is not thread-safe by construction; the single loop serializes all DB access naturally).
- **R-SEC-6 (graceful lifecycle)** The portal server exposes `serve()` / shutdown mirroring `LineWebhookServer` (AppRunner + TCPSite, cancelled on shutdown in the `finally` of `async_main`). A failure to start the portal is logged and **must not** crash the main channel loop (fail-open on the *operator* surface; the bot itself keeps serving users).
- **R-STATS-1 (runtime stats holder)** A process-lifetime `RuntimeStats` object holds `started_at` (set once at startup) and `last_event_at` (updated on every inbound message/postback via the `_on_message`/`_on_callback` wrappers in `core/app.py`). The portal reads it; it is never persisted. When `last_event_at` is unset (no event since restart), the status page shows a localized "no events since restart" (may fall back to displaying `max(logs.ts, audit.ts)` as a hint).
- **R-STATS-2 (log ring buffer)** A `RingBufferHandler(logging.Handler)` keeps the last `config.portal.log_ring_size` records at level `WARNING`+ in a `collections.deque(maxlen=…)`, installed on the `habit_assistant` logger at startup when the portal is enabled. Process-local; resets on restart. This is the chosen mechanism for "recent errors" — **`journalctl` is rejected** (needs the `habitbot` user to hold journal-read privilege and a subprocess call, fragile under the unit's `NoNewPrivileges=true`/`ProtectSystem=strict`; the ring buffer needs neither).
- **R-USERACT-1 (source-parameterized approve/block)** `core/access.py` gains channel-agnostic helpers `approve_user(db, channel, config, *, actor, target_chat, source)` and `block_user(...)` that perform the DB write + `audit.record(source=source)` + the existing side-effects (access-granted push to the approved user, `set_last_announced_version` catch-up). `execute_admin` is refactored to delegate to them with `source="admin"` (behavior byte-identical to today); the portal calls them with `source="portal"` (AC16/AC17/AC18). This keeps audit provenance honest and reuses the notification/catch-up logic rather than duplicating it.
- **R-I18N-1 (bilingual)** Every user-facing string on every page/route resolves through `core/i18n.py` in the owner's resolved language (`i18n.resolve_unprompted_language(config)` plus the owner's stored `language_pref`); no hardcoded EN/TH literals in handlers (AC31). New keys are prefixed `portal_*` and carry both `en` and `th`.

### Status / report page (`R-STATUS-*`)

- **R-STATUS-1** `GET /` renders: version (`__version__`), channel type, Ollama mode (`off` when `enabled=false`), uptime (from `RuntimeStats.started_at`), webhook last-event time (AC8/AC9/AC10).
- **R-STATUS-2** A scheduler table lists every `scheduler.get_jobs()` entry by `job.id` with its `next_run_time`, formatted in `config.app.timezone` local wall-clock (AC11).
- **R-STATUS-3** A push-quota gauge shows `used = db.monthly_push_total(current_yyyymm)`, `cap = digest.push_cap` when `digest.mode == "realtime"` else `digest.warn_cap`, plus the percent and the active mode label (AC12).
- **R-STATUS-4** A storage panel shows DB file size (+ `-wal`/`-shm` sidecars if present), media-dir total size, the backup list (filename, timestamp parsed from the name, size, mtime) and the newest backup's time as "last backup" (AC13).
- **R-STATUS-5** A "recent errors" panel shows the ring buffer newest-first (timestamp, level, logger, message), with a localized empty state when the buffer holds nothing (AC14).

### User management (`R-USER-*`)

- **R-USER-1** `GET /users` lists all `status == "pending"` rows (display_name or chat_id, and the raw chat_id), each with Approve and Block buttons (AC15); and all `status == "active"` rows with per-user stats: last-log time (`db.last_log`), current streak, `digest_opt_out`, `language_pref` (AC19). A pre-approval (invite) form takes a raw chat_id.
- **R-USER-2** `POST /users/approve` (form `chat_id`) calls `access.approve_user(..., source="portal")`: the row becomes `active`, an audit row is recorded (`action="user_approve"`, `source="portal"`, `actor=owner_id`), the approved user gets the `access_granted` push, then `303` back to `/users` (AC16).
- **R-USER-3** `POST /users/block` calls `access.block_user(..., source="portal")` → `blocked`, audit `action="user_block"`, `source="portal"` (AC17).
- **R-USER-4** `POST /users/invite` is an alias of approve (pre-approve a chat_id that has never contacted the bot) via `approve_user(..., source="portal")` (AC18). The chat_id shape is validated with the existing `access._CHAT_ID_RE`; an invalid shape → inline localized error, no write.
- **R-USER-5** Every POST re-checks the identity gate (R-SEC-3); an unauthorized POST returns `403` with **no** DB write (AC20). A missing/unresolvable `chat_id` → localized inline error, `303` back, no write, no audit row (AC21).

### Audit viewer + activity feed (`R-AUDIT-*`)

- **R-AUDIT-1** `GET /audit?page=N` renders `audit_log` rows newest-first (`db.recent_audit(limit, offset)`), page size fixed (default 50), with prev/next navigation bounded by `db.audit_total()` (AC22/AC25). Out-of-range `page` clamps to the valid range, never crashes.
- **R-AUDIT-2** Each row renders actor (owner → localized "you"; else display_name, falling back to chat_id), localized action label (reuse `core/audit_view.py`'s `_ACTION_LABEL_MSG_IDS`), entity/`target_user_id`, `old_value → new_value`, `source`, and formatted `ts` — the same field set and privacy shape as the chat `/audit` (AC23).
- **R-AUDIT-3 (privacy)** `GET /activity` shows recent **log metadata only**: user (display_name/id), habit category, numeric/duration value, `ts`, `source`. It MUST NOT render `logs.raw_message` for text/diary habits (raw diary content) — matching the established posture that the owner's `/audit` never exposes another user's message content (AC24). The new `db.recent_logs_metadata(...)` helper selects only the safe columns and omits `raw_message` for `habit_type == "text"` rows (or omits it entirely; see OQ2).

### Quota & digest (`R-QUOTA-*`)

- **R-QUOTA-1** `GET /quota` shows monthly push history: for each of the last K months (default 12), the `push_ledger` total (`db.monthly_push_history()`), and for the current month a per-user breakdown (`SUM`/rows from `push_ledger` where `yyyymm = current`) (AC26).
- **R-QUOTA-2** The page shows the active cap, the 80% warn and 100% stop thresholds, and whether the warn/stop have fired this month — read from the realtime in-memory month guards if `SPEC-LINE-1.2.md`'s `LineChannel` exposes them, else derived from `total` vs `cap` (AC27; see OQ4).
- **R-QUOTA-3** A digest section lists each active user's `digest_opt_out` state and the global digest send time (`config.digest.time`, mode) (AC28).
- **R-QUOTA-4 (could)** `GET /config` renders the effective `Config` with **secrets redacted** — every field whose name matches token/secret/access_token/password is rendered as `••••`; the LINE tokens and channel secret are never shown (AC29).
- **R-QUOTA-5 (could)** `POST /quota/digest-run` (confirm-gated: a two-step form, or a `confirm=yes` field) invokes `digest.run_daily_digest(db, channel, config, provider)` on demand and renders a result summary. It spends real push quota and fans out to all users; it is owner-only (whole portal is) and must be explicitly confirmed (AC30).

### Integration (`R-INT-*`)

- **R-INT-1** All four module route-sets register into one portal aiohttp `Application` via a uniform `register(app, deps)` hook each module exposes; `deps` carries `db, config, scheduler, channel, stats, owner_id, ring_buffer`. No module imports another module's file.
- **R-INT-2** `core/app.py` (the reserved wiring file) constructs `RuntimeStats`, installs the `RingBufferHandler`, builds the portal server with all registered modules, and runs it as a task alongside `channel.run(...)`, cancelling it on shutdown — only when `config.portal.enabled and config.channel.type == "line"`.
- **R-INT-3** With `config.portal.enabled=false`: `core/app.py` skips all portal construction, installs no ring handler, and the build is byte-identical to the pre-portal baseline (AC1). Telegram is unaffected regardless (AC2).

## 5. Interfaces (signatures)

```python
# src/habit_assistant/config.py
class PortalConfig(BaseModel):
    enabled: bool = False
    bind_host: str = "127.0.0.1"
    bind_port: int = 8081
    owner_login: str = ""
    require_identity_header: bool = True
    log_ring_size: int = 200
    # + a Config-level validator (or model_validator) that raises ConfigError
    #   when portal.enabled and portal.bind_port == line.bind_port.

# src/habit_assistant/core/portal/stats.py
class RuntimeStats:
    started_at: datetime
    last_event_at: datetime | None
    def mark_event(self) -> None: ...

class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int) -> None: ...
    def records(self) -> list[logging.LogRecord]: ...   # newest-first snapshot

# src/habit_assistant/core/portal/security.py
@web.middleware
async def identity_gate(request: web.Request, handler) -> web.StreamResponse: ...
#   403 unless the Tailscale-User-Login rules (R-SEC-3) pass.

# src/habit_assistant/core/portal/server.py
class PortalServer:
    def __init__(self, *, bind_host: str, bind_port: int, deps: PortalDeps,
                 modules: list) -> None: ...
    async def serve(self) -> None: ...          # AppRunner + TCPSite, run-forever

@dataclass
class PortalDeps:
    db: Database
    config: Config
    scheduler: AsyncIOScheduler
    channel: Channel
    stats: RuntimeStats
    ring: RingBufferHandler
    owner_id: str

# Each page module exposes:
def register(app: web.Application, deps: PortalDeps) -> None: ...

# src/habit_assistant/core/access.py  (shared-surface refactor, R-USERACT-1)
async def approve_user(db, channel, config, *, actor: str, target_chat: str,
                       source: str) -> None: ...
async def block_user(db, channel, config, *, actor: str, target_chat: str,
                     source: str) -> None: ...

# src/habit_assistant/storage/db.py  (additive helpers)
def recent_audit(self, limit: int, offset: int = 0) -> list[sqlite3.Row]: ...  # extend
def audit_total(self) -> int: ...
def recent_logs_metadata(self, limit: int, offset: int = 0) -> list[sqlite3.Row]: ...  # no raw diary text
def monthly_push_history(self, months: int = 12) -> list[sqlite3.Row]: ...     # (yyyymm, total)
def push_by_user(self, yyyymm: str) -> list[sqlite3.Row]: ...                   # current-month breakdown
```

## 6. Files to touch

**Shared surface (built first):**
- `src/habit_assistant/config.py` — add `PortalConfig`, mount on `Config`, add the port-collision validator.
- `src/habit_assistant/core/portal/__init__.py` — new package.
- `src/habit_assistant/core/portal/server.py` — `PortalServer` (second aiohttp listener), `PortalDeps`, module registration.
- `src/habit_assistant/core/portal/security.py` — `identity_gate` middleware.
- `src/habit_assistant/core/portal/layout.py` — bilingual base HTML shell + shared render helpers (nav, tiles, tables, escaping). Structural only; Iris supplies tokens.
- `src/habit_assistant/core/portal/stats.py` — `RuntimeStats`, `RingBufferHandler`.
- `src/habit_assistant/core/access.py` — extract `approve_user`/`block_user` (`source` param); `execute_admin` delegates.
- `src/habit_assistant/storage/db.py` — `recent_audit(offset)`, `audit_total`, `recent_logs_metadata`, `monthly_push_history`, `push_by_user`.
- `src/habit_assistant/core/i18n.py` — `portal_*` keys (en+th).
- `src/habit_assistant/core/app.py` — construct stats + ring handler + `PortalServer`; run as a task; update `_on_message`/`_on_callback` to `stats.mark_event()`; cancel on shutdown; all gated on `portal.enabled and type=="line"`.
- `config.toml.line` — a commented `[portal]` block.
- `deploy/setup.sh`, `deploy/DEPLOY-LINE.md` (docs) — print the `tailscale serve --bg 8081` step; warn never to Funnel the portal port.

**Parallel modules:**
- `src/habit_assistant/core/portal/status.py` — STATUS (`GET /`).
- `src/habit_assistant/core/portal/users.py` — USERS (`GET /users`, POST approve/block/invite).
- `src/habit_assistant/core/portal/audit.py` — AUDIT (`GET /audit`, `GET /activity`).
- `src/habit_assistant/core/portal/quota.py` — QUOTA (`GET /quota`, `GET /config`, `POST /quota/digest-run`).

**Tests:** `tests/test_portal_security.py`, `tests/test_portal_status.py`, `tests/test_portal_users.py`, `tests/test_portal_audit.py`, `tests/test_portal_quota.py`, `tests/test_portal_integration.py`, plus a `test_config` case for the port-collision validator and a `test_access` case that `execute_admin` still records `source="admin"` after the refactor.

## 7. External dependencies

- **None new.** `aiohttp>=3.9` is already a base dependency (`pyproject.toml`), used by the existing LINE webhook. The portal reuses it. HTML is built with stdlib string templating / small inline builders in `layout.py` — **no jinja2**. Justification: the pages are a handful of tables/tiles/forms with no template inheritance needs; a template engine would earn its place only at ~dozens of templates with shared partials, which this is not, and adding it violates the LEAN/zero-new-dep bias. Reassess if the UI grows past ~10 templates.
- Python `>=3.11` (unchanged). Runtime infra: Tailscale (already required for the existing Funnel deployment) — the portal uses `tailscale serve` (already installed, no new package).

## 8. Acceptance criteria

Every AC is one observable behavior. Tier: **[M]** must, **[S]** should, **[C]** could.

- **AC1 [M]** (Shared) Given `[portal] enabled=false` (default), When the process starts, Then no portal port is bound, no portal route answers, and the existing LINE integration test suite passes unchanged.
- **AC2 [M]** (Shared) Given `enabled=true` but `channel.type != "line"`, When the process starts, Then the portal is not constructed.
- **AC3 [M]** (Shared) Given `require_identity_header=true`, When a request arrives with no `Tailscale-User-Login` header, Then the response is `403` with no admin content in the body.
- **AC4 [M]** (Shared) Given `owner_login="alice@example.com"`, When a request carries `Tailscale-User-Login: bob@example.com`, Then the response is `403`.
- **AC5 [M]** (Shared) Given the correct identity header (and matching `owner_login` if set), When `GET /` is requested, Then the status page renders `200`.
- **AC6 [M]** (Shared) Given `owner_login` set, When a forged `Tailscale-User-Login` not equal to it is supplied, Then the request is refused `403` (the gate never treats a non-matching login as authorized).
- **AC7 [M]** (Shared) Given `enabled=true` and `portal.bind_port == line.bind_port`, When config loads, Then a `ConfigError` is raised; and when they differ, the portal binds its own port distinct from `8080`.
- **AC8 [M]** (Status) Given the portal is up, When `GET /`, Then it shows version `== __version__`, channel `line`, and Ollama `off`.
- **AC9 [M]** (Status) When `GET /`, Then it shows a process uptime derived from the recorded start time.
- **AC10 [S]** (Status) Given at least one inbound event has been processed since start, When `GET /`, Then the "last webhook event" time reflects it; given none, it shows the localized "no events since restart".
- **AC11 [M]** (Status) When `GET /`, Then every scheduler job id is listed with its next-run time.
- **AC12 [M]** (Status) When `GET /`, Then the quota gauge shows `monthly_push_total(current_yyyymm)` as used, the active-mode cap, and the percent.
- **AC13 [M]** (Status) When `GET /`, Then it shows DB size, media-dir size, and the backup list (name, size, time) with the newest as "last backup".
- **AC14 [S]** (Status) Given a `WARNING+` record was logged, When `GET /`, Then it appears in the recent-errors panel; with none, the panel shows a localized empty state.
- **AC15 [M]** (Users) Given ≥1 `pending` user, When `GET /users`, Then each is listed with display name (or chat_id) and Approve/Block controls.
- **AC16 [M]** (Users) Given a pending `chat_id`, When `POST /users/approve`, Then the row becomes `active`, an audit row with `action="user_approve"` and `source="portal"` is written, the user receives the `access_granted` push, and the page reflects active status.
- **AC17 [M]** (Users) Given any `chat_id`, When `POST /users/block`, Then it becomes `blocked` with an audit row `action="user_block"`, `source="portal"`.
- **AC18 [S]** (Users) Given a never-seen `chat_id` of valid shape, When `POST /users/invite`, Then a row is created `active` with audit `source="portal"`.
- **AC19 [M]** (Users) When `GET /users`, Then each active user shows last-log time, current streak, digest opt-out state, and language pref.
- **AC20 [M]** (Users) Given no/invalid identity header, When any `POST /users/*` is attempted, Then it returns `403` and performs no DB write.
- **AC21 [M]** (Users) Given a missing or unresolvable `chat_id`, When a `POST /users/*` is submitted, Then the page shows a localized inline error and no audit row is written.
- **AC22 [M]** (Audit) When `GET /audit?page=2`, Then rows `50..99` (newest-first) render with working prev/next.
- **AC23 [M]** (Audit) When `GET /audit`, Then each row shows actor (you/name/id), localized action, target/entity, old→new, source, and ts.
- **AC24 [M]** (Audit) When `GET /activity`, Then log metadata renders (user, category, value, ts) and no `raw_message` diary text of any user appears anywhere on the page.
- **AC25 [M]** (Audit) Given `audit_total()` rows exist, When a `page` beyond the last is requested, Then it clamps to the last page without error.
- **AC26 [M]** (Quota) When `GET /quota`, Then it shows per-month push totals and a current-month per-user breakdown.
- **AC27 [S]** (Quota) When `GET /quota`, Then it shows the active cap, the 80%/100% thresholds, and whether warn/stop have fired this month.
- **AC28 [S]** (Quota) When `GET /quota`, Then each active user's digest opt-out state and the global digest time/mode are shown.
- **AC29 [C]** (Quota) When `GET /config`, Then the effective config renders with every secret field redacted (LINE token/secret never shown in plaintext).
- **AC30 [C]** (Quota) Given an explicit confirm, When `POST /quota/digest-run`, Then `run_daily_digest` executes and a result summary renders; without confirm, nothing is sent.
- **AC31 [M]** (Shared/i18n) Given the owner's language is Thai, When any portal page renders, Then its chrome and labels are Thai (no hardcoded English literals), and English when the pref is English.
- **AC32 [M]** (Integration) Given all four modules registered, When each route is requested through the identity middleware, Then each renders its page; and with the portal disabled, none of these routes exist.

Coverage: every R-* rule maps to ≥1 AC (R-SEC→AC1-7,20,31; R-STATS→AC9,10,14; R-USERACT→AC16-18; R-STATUS→AC8-14; R-USER→AC15-21; R-AUDIT→AC22-25; R-QUOTA→AC26-30; R-I18N→AC31; R-INT→AC1,2,32).

## 9. Risks & open questions

- **OQ1 — `owner_login` default.** Ship with `owner_login=""` (any authenticated tailnet identity) or force the owner to set it? **Default if unanswered:** ship empty (network boundary alone is already strong for a single-user tailnet) but make `deploy/DEPLOY-LINE.md` strongly recommend setting it. **Who:** user/owner.
- **OQ2 — activity feed granularity.** Should `/activity` omit `raw_message` for *all* habits, or show it only for non-text (numeric/duration, where the "message" is just `"500ml"`) and hide it only for `habit_type == "text"`? Numeric raw messages aren't private, but the simplest safe rule is to omit `raw_message` everywhere and render structured `category + value` only. **Default if unanswered:** omit `raw_message` everywhere; render structured fields only (strictly safe). **Who:** user (privacy call) — Archi can decide.
- **OQ3 — manual digest trigger (AC30) in v1?** It spends real quota and fans out to all users. **Default if unanswered:** ship as a *could*, disabled behind a `[portal]` sub-flag defaulting off, so the button only appears if explicitly enabled. **Who:** user.
- **OQ4 — quota warn/stop state source.** `SPEC-LINE-1.2.md` keeps the once-per-month warn/stop guards as in-memory state on `LineChannel` (R-Q6). To show "warn fired / stop fired" (AC27) the portal needs read access to those guards. **Default if unanswered:** expose a read-only `LineChannel.quota_state(yyyymm)` accessor as a small addition to the 1.2 work, or (fallback) derive purely from `total` vs `cap` and show the thresholds without the "already fired" flag. **Who:** Archi (cross-spec coordination with the 1.2 release).
- **OQ5 — restart button.** Researched and **not recommended for v1.** The unit runs as unprivileged `habitbot` with `NoNewPrivileges=true`; it cannot `systemctl restart` itself without a polkit rule or NOPASSWD sudoers entry (both widen attack surface, and `NoNewPrivileges` actively blocks sudo escalation). The owner can restart from a tailnet SSH session. **Default:** omit; document a polkit-rule recipe as an optional operator opt-in only if the user asks. **Who:** user.
- **RISK — mis-Funneling the portal port.** If an operator ever runs `tailscale funnel 8081`, the portal becomes public. Mitigations already in the design: `require_identity_header=true` fails closed for header-less public traffic (R-SEC-3), and the deploy docs warn against it. Residual risk if the operator *also* disables the header requirement — documented as a foot-gun not to be done.
- **RISK — heatmap/wrapped gallery from media dir.** Considered and **left out of scope**: the media dir holds transient (TTL 3600s), per-user tokened PNGs belonging to whichever user generated them; surfacing them to the owner would leak other users' chart images and they vanish within the hour. Low value, real privacy cost.

## 10. Out of scope

- Any visual design decisions (Maya + Iris own the UX/UI after approval).
- A JSON/REST API, an SPA, or any client-side build tooling.
- Editing/deleting users' habit logs or diary content from the portal (read-only over user data; the only writes are approve/block/invite).
- Multi-admin roles / RBAC — the portal is single-operator (the owner) by construction.
- Service restart / process control (OQ5), config *editing* (the config viewer is read-only + redacted), and the media-preview gallery (privacy).
- Any change to the public `/callback` or `/media` routes, the reply/push economics, or the Telegram edition.
- Authentication by password/token (rejected — option c).

## 11. Module split & parallel development

**Total functionals:** 8 — (1) portal host + tailnet security gate, (2) status/report page, (3) user management, (4) audit viewer, (5) activity feed, (6) quota & digest page, (7) config viewer, (8) bilingual i18n. Feeds (5) and (7) fold into the audit and quota modules respectively.

**Recommendation:** **PARALLEL** — 4 modules over a shared surface. The four page modules own disjoint files and disjoint ACs; they touch each other only through the shared surface (server, security, layout, stats, db helpers, access refactor, i18n keys), which is built first, sequentially. This is a clean split because every page is an independent set of `register(app, deps)` GET/POST handlers on its own file.

**Shared surface (built first, sequentially, before any module starts):**
- `config.py` `PortalConfig` + port-collision validator.
- `core/portal/server.py`, `security.py`, `layout.py`, `stats.py` (host, identity gate, HTML shell, `RuntimeStats` + `RingBufferHandler`).
- `core/access.py` source-parameterized `approve_user`/`block_user` (+ `execute_admin` delegation).
- `storage/db.py` helpers: `recent_audit(offset)`, `audit_total`, `recent_logs_metadata`, `monthly_push_history`, `push_by_user`.
- `core/i18n.py` `portal_*` keys (en+th).
- `core/app.py` wiring (construct/run/cancel the portal task; `stats.mark_event()` in the on_message/on_callback wrappers; ring handler install) — gated on `portal.enabled and type=="line"`.

| Module | Owned ACs | Owned files | Depends on |
|---|---|---|---|
| **STATUS** | AC8, AC9, AC10, AC11, AC12, AC13, AC14 | `core/portal/status.py`; `tests/test_portal_status.py` | shared: stats, scheduler handle, db, layout |
| **USERS** | AC15, AC16, AC17, AC18, AC19, AC20, AC21 | `core/portal/users.py`; `tests/test_portal_users.py` | shared: `access.approve_user`/`block_user`, layout, security |
| **AUDIT** | AC22, AC23, AC24, AC25 | `core/portal/audit.py`; `tests/test_portal_audit.py` | shared: `recent_audit`/`audit_total`/`recent_logs_metadata`, layout |
| **QUOTA** | AC26, AC27, AC28, AC29, AC30 | `core/portal/quota.py`; `tests/test_portal_quota.py` | shared: `monthly_push_history`/`push_by_user`, digest module, layout; OQ4 accessor |

Shared-surface ACs (AC1–AC7, AC31, AC32) are owned by whoever builds the shared surface + integration. Every AC belongs to exactly one owner; no AC is shared between two modules.

**Integration order (after parallel modules complete):**
1. Register all four modules' routes into `PortalServer` via their `register(app, deps)` hooks (`R-INT-1`).
2. Run `tests/test_portal_integration.py` — an end-to-end request per route through the identity middleware, plus the disabled-portal no-op assertion (AC1/AC32).
3. Confirm the `execute_admin` refactor left the chat-command path byte-identical (`source="admin"` still recorded).

---

### Sources (security-boundary research)
- [Tailscale Funnel · Tailscale Docs](https://tailscale.com/docs/features/tailscale-funnel) — Funnel proxies to `127.0.0.1`; Serve/Funnel cannot share a port.
- [Tailscale Serve · Tailscale Docs](https://tailscale.com/docs/features/tailscale-serve) — identity headers injected and spoofed copies stripped; proxies to `127.0.0.1`; different ports for Serve vs Funnel.
- [tailscale-dev/id-headers-demo](https://github.com/tailscale-dev/id-headers-demo) — `Tailscale-User-Login` / `Tailscale-User-Name` / `Tailscale-User-Profile-Pic` on proxied Serve requests.
