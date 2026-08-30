# Spec — LINE Official Account edition (branch `line-version`)

> Branch product. Target: Linux VPS (2 cores / 16 GB) with **Tailscale Funnel** as the public HTTPS webhook + media endpoint. Two locked user decisions (2026-08-29): **(1) NO-LLM MODE** — zero LLM dependency; **(2) TRIMMED DIGEST** — at most **one** proactive push per user per day (LINE free-plan quota ≈300 push/month total; **replies are free and unlimited**). Baseline: `v1.10.0` (worktree), Telegram edition unaffected on `main`.

## 1. Problem statement

Port the habit-tracking assistant from Telegram to a LINE Official Account so it can serve the Thai market, running on a small Linux VPS behind Tailscale Funnel. The port must be a **thin channel swap**: `core/` and `storage/` already depend only on the `channels.base.Channel` ABC (verified: `channels/base.py` makes only `send`/`run` abstract, every Telegram-only method is a concrete degrade-to-default), so the LINE work lives almost entirely behind that seam plus two branch-wide behavior changes — a permanent no-LLM mode and a single-daily-digest push model that respects LINE's economics (LINE **Reply API** messages are free and uncounted; **Push/multicast/broadcast** count toward the monthly quota, per LINE pricing docs). Success = a Thai/English LINE bot that logs habits, answers commands, and sends images entirely without an LLM, never exceeds one push per user per day, keeps `core/` diffs minimal so future `main → line-version` merges stay feasible, and keeps the transport-agnostic test suite green.

## 2. Inputs

### 2.1 Inbound LINE webhook event (POST body to `/callback`)
LINE POSTs a signed JSON body. Signature: header `x-line-signature` = `base64(HMAC-SHA256(channel_secret, <raw request body bytes>))` (verified against LINE reference). One POST may carry **multiple** events; each event that can be replied to carries its own single-use `replyToken`.

```json
{
  "destination": "Uxxxxxxxxxxxxxx",
  "events": [
    {
      "type": "message",
      "replyToken": "0f3779fba3b349968c5d07db31eab56f",
      "source": { "type": "user", "userId": "U4af4980629..." },
      "timestamp": 1749000000000,
      "message": { "type": "text", "id": "325708", "text": "500ml" }
    },
    {
      "type": "postback",
      "replyToken": "8cf9239d56244f4197887e939187e19e",
      "source": { "type": "user", "userId": "U4af4980629..." },
      "postback": { "data": "clarify:12345:water:500" }
    }
  ]
}
```

- `source.userId` (opaque string, e.g. `"U4af4980629..."`) is the **`chat_id` analogue** — used verbatim everywhere `user_id`/`chat_id` is used today. Verified: every id column is `TEXT`/`str` (`storage/migrations.py`, `storage/db.py`), so **no schema change is needed** to store LINE userIds.
- `message.type == "text"` → route to `on_message(userId, text, ...)`.
- `postback.data` → route to `on_callback(userId, data, "", <pseudo_id>)`. The `data` string is our own `callback_data` verbatim (see §4 R-A9). **Postback events carry no source-message text** → `on_callback`'s `source_text` argument is `""`.
- Non-text message types (image/sticker/location/etc.) and non-user sources (group/room) → out of scope (§10).

### 2.2 Secrets (`.env`, loaded by `config.Secrets`)
On this branch the channel is selected by `config.toml [channel].type`. LINE fields are required when `type = "line"`; Telegram fields become optional.

```
LINE_CHANNEL_ACCESS_TOKEN=<long-lived channel access token>
LINE_CHANNEL_SECRET=<channel secret, used for X-Line-Signature HMAC>
LINE_OWNER_USER_ID=U...            # the owner's LINE userId (owner attribution + owner-only surfaces)
```

### 2.3 Config additions (`config.toml`)
```toml
[channel]
type = "line"                       # "line" | "telegram" (default "telegram" for parity; the LINE deploy sets "line")

[ollama]
enabled = false                     # NEW master switch. On this branch the DEFAULT and only supported value is false.

[line]
public_base_url = "https://vps-host.tailnet-name.ts.net"   # Funnel origin; used to build media URLs and register the webhook
bind_host = "127.0.0.1"
bind_port = 8080
media_dir = "data/media"
media_ttl_seconds = 3600            # tokened PNGs deleted after this age
rich_menu_image = "assets/richmenu/richmenu.png"           # deployment asset (see §7)

[digest]
enabled = true                      # the single daily push
time = "20:00"                      # one fixed HH:MM/day
warn_cap = 280                      # owner is warned in-digest when the month's push total reaches this
include_weekly_review_day = true    # on the weekly-review weekday, append the review text line to that day's digest
```

### 2.4 Generated image (unchanged upstream)
`/heatmap`, `/wrapped`, and weekly-review charts each hand `send_image(chat_id, image: bytes, caption, *, disable_notification=False)` **raw in-memory PNG bytes** (matplotlib `savefig` → `BytesIO` → `getvalue()`; verified `heatmap.py`, `wrapped.py`, `charts.py`). Never written to disk upstream — the LINE `send_image` is the first and only writer.

## 3. Outputs

### 3.1 Reactive output (free — LINE Reply API)
Every reply to an inbound event is delivered with that event's `replyToken` via `POST https://api.line.me/v2/bot/message/reply`, batched into **one** call carrying up to **5** message objects (LINE max). This does **not** count against the monthly quota.

```jsonc
// reply body
{ "replyToken": "0f37...", "messages": [
  { "type": "text", "text": "💧 water +500 ml — 1,500 / 2,500 ml today (60%)",
    "quickReply": { "items": [
      { "type": "action", "action": { "type": "postback", "label": "↩︎ Undo", "data": "undo:98765" } }
    ] } }
]}
```

### 3.2 Proactive output (costs quota — LINE Push API)
The **only** proactive send is the daily digest: `POST https://api.line.me/v2/bot/message/push` to one `userId`, one text message (≤5 objects). Each push increments `push_ledger` (§4 R-C6).

```jsonc
{ "to": "U4af...", "messages": [ { "type": "text", "text": "<batched digest>" } ] }
```

### 3.3 Image message (reactive or, rarely, proactive)
```jsonc
{ "type": "image",
  "originalContentUrl": "https://vps-host.tailnet.ts.net/media/Ab3xY9_k.png",
  "previewImageUrl":   "https://vps-host.tailnet.ts.net/media/Ab3xY9_k.png" }
```

### 3.4 HTTP responses from the webhook server
- `POST /callback`: `200 OK` (empty body) once signature verifies and events are enqueued — returned **before** processing. `400` on missing/invalid signature or unparseable body.
- `GET /media/{token}.png`: `200` + `Content-Type: image/png` for a live token; `404` for unknown/expired/invalid token (path-traversal-safe).

### 3.5 Error / degradation responses
- Invalid signature → `400`, event dropped, one WARN log, nothing sent.
- Reply token expired/already used when the worker attempts the reply → log + drop the reactive output (do **not** silently convert to a push — that would spend quota; verified: reply tokens are single-use and expire). The user can re-send.
- `send_image` media-serve failure → the existing caller try/except degrades to a text summary (verified: `execute_heatmap`/`execute_wrapped`/weekly-review job all tolerate a raised `send_image`).

## 4. Behavior rules

Rule ids are grouped by module. `R-A*` = LINE channel/webhook/media, `R-B*` = no-LLM mode, `R-C*` = digest+quota, `R-D*` = deployment, `R-S*` = shared surface, `R-I*` = integration.

### Shared surface (built first, sequentially)
- **R-S1** Add `OllamaConfig.enabled: bool = True` to `config.py`. On this branch `config.toml` sets it `false`; it is the default-and-only supported mode here. No other config field changes meaning.
- **R-S2** Add `ChannelConfig(type: Literal["telegram","line"] = "telegram")` mounted at `Config.channel`, plus `LineConfig` (`public_base_url`, `bind_host`, `bind_port`, `media_dir`, `media_ttl_seconds`, `rich_menu_image`) at `Config.line`, and `DigestConfig` (`enabled`, `time`, `warn_cap`, `include_weekly_review_day`) at `Config.digest`. All defaulted; an absent section uses class defaults (same convention as every prior config addition).
- **R-S3** Extend `Secrets`: add optional `line_channel_access_token`, `line_channel_secret`, `line_owner_user_id`; make `telegram_bot_token`/`telegram_chat_id` optional. `load_secrets` validates that the **selected** channel's secrets are present, raising `ConfigError` with an actionable message otherwise.
- **R-S4** Migration **014** (next free `user_version`; verified max is 013), append-only and additive:
  - `CREATE TABLE IF NOT EXISTS push_ledger (user_id TEXT NOT NULL, yyyymm TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')), PRIMARY KEY (user_id, yyyymm))`.
  - `ALTER TABLE users ADD COLUMN digest_opt_out INTEGER NOT NULL DEFAULT 0` (0 = subscribed; digest is opt-**out**).
  - Existing rows unaffected; a pre-014 DB migrates forward with no data loss (verified runner semantics: per-migration `BEGIN…PRAGMA user_version=N…COMMIT`).
- **R-S5** DB accessors on `Database`: `increment_push(user_id, yyyymm)` (upsert `count = count + 1`), `push_count(user_id, yyyymm) -> int`, `monthly_push_total(yyyymm) -> int` (sum across users), `set_digest_opt_out(user_id, bool)`, `digest_opt_out(user_id) -> bool`. Upsert idiom mirrors `set_target`/`upsert_record`.
- **R-S6** i18n: add all new bilingual keys (TH primary quality) — digest header/section labels, no-LLM command pointers, clarify-on-LINE copy, quota-warning line. Every new key has both `en` and `th`. (Thai copy is the point of this channel; wording is reviewed by Maya/Patty at doc time but keys exist now.)
- **R-S7** Register pytest markers `telegram_only` and `llm_only` (in `pyproject.toml`/`conftest.py`). The **LINE gate** is `pytest -m "not telegram_only and not llm_only"`; it must be green. Telegram-transport and LLM-behavior tests are marked, deselected on this branch, and do not fail the gate (§ gate discipline in §8/§11).
- **R-S8** Add `aiohttp>=3.9` to `pyproject.toml [project.dependencies]` — the one new runtime dependency (justified §7).

### Module A — LINE channel + webhook + media + rich menu
- **R-A1 (signature)** The webhook verifies `x-line-signature` = `base64(HMAC-SHA256(channel_secret, raw_body))` using `hmac.compare_digest` **before** parsing or enqueuing. Mismatch/missing → `400`, drop, WARN log. This is computed over the **raw** request bytes (aiohttp `await request.read()`), never the re-serialized JSON.
- **R-A2 (fast 200 / enqueue-then-process)** After signature verification, parse `events`, push each onto an `asyncio.Queue` in array order, and return `200` immediately. Processing happens in a worker, not in the HTTP handler. A body that fails JSON parsing → `400`.
- **R-A3 (single-worker FIFO ordering)** One worker task drains the queue in FIFO order and processes events sequentially, `await`-ing each fully before the next. This guarantees global — hence **per-user** — ordering, and preserves the documented single-instance / single-asyncio-process invariant. (No-LLM processing is deterministic and near-instant, so a single worker cannot meaningfully back up; the queue may be bounded with backpressure.)
- **R-A4 (reply aggregation)** `LineChannel` maintains a per-event **reply context** (a `contextvars.ContextVar` holding `{replyToken, buffer: list[message-object]}`). The worker sets it before `await on_message/on_callback(...)` and flushes after. While a context is active, `send`/`send_actionable`/`send_image` **append** a message object to the buffer instead of calling the API. On flush, the buffer is sent as **one** reply call (≤5 objects; a 6th+ object is dropped with a WARN — core emits ≤2 in practice). This keeps `core/` unchanged: core still calls `channel.send(...)` N times per event; the channel batches them into one free reply.
- **R-A5 (reply is free, single-use)** The reply uses the event's `replyToken` exactly once. If LINE rejects it (expired/used), log and drop (R-3.5); never fall back to a push.
- **R-A6 (push when no reply context)** When `send`/`send_image` is called with **no** active reply context (i.e. a scheduled/proactive send — only the digest), it goes out via the **Push API** to `chat_id` and increments `push_ledger` for the current `yyyymm` (R-C6). `send` returns `None` on LINE (no per-message id contract).
- **R-A7 (text limits)** LINE text objects allow up to 5,000 chars (more generous than Telegram's 4,096), so existing render-budget caps are safe; no new truncation needed.
- **R-A8 (quick replies = the button surface)** `send_actionable(chat_id, text, buttons)` renders `text` as one message object with a `quickReply` of up to **13** items (LINE max). Each `(label, callback_data)` becomes `{type:"action", action:{type:"postback", label, data: callback_data}}`. `callback_data` is passed **verbatim** into `data` (all our payloads — `undo:…`, `log:…`, `clarify:…`, `routine:run:…` — are ≤300 chars, LINE's postback `data` limit). More than 13 buttons → keep the first 13, WARN (quicklog/clarify keyboards for the default catalog are well under 13).
- **R-A9 (postback → on_callback verbatim)** A `postback` event routes to `on_callback(userId, data, "", pseudo_id)` where `data` is the LINE `postback.data` unchanged. The existing prefix router (`log:` → quicklog, `routine:run:` → routines, `clarify:` → clarify, else `undo_ui`) works **unmodified**. `source_text=""` → language falls back to the user's stored `/lang` pref, then `[i18n].primary_language` (Thai). `answer_callback_query` stays the base no-op (LINE has no spinner to dismiss).
- **R-A10 (rich menu = the command surface)** At startup, `LineChannel` registers **one static default rich menu** from the deployment image asset (`[line].rich_menu_image`): `POST /v2/bot/richmenu` (areas), `POST /v2/bot/richmenu/{id}/content` (image), `POST /v2/bot/user/all/richmenu/{id}` (set default). Tappable areas use **message actions** whose text is a command (e.g. `/log`, `/heatmap`, `/habits`, `/wrapped`, `/help`, `/guide`), which arrive as ordinary inbound messages and route through the existing dispatch unchanged. `set_my_commands` stays the base no-op. The menu image itself is a deployment asset, not generated (§7). Registration is fail-open (a failure logs and continues startup).
- **R-A11 (send_image via public media URL)** `LineChannel.send_image(chat_id, image: bytes, caption, *, disable_notification=False)`:
  1. writes `image` to `{media_dir}/{token}.png` where `token = secrets.token_urlsafe(16)`;
  2. builds `url = f"{public_base_url}/media/{token}.png"`;
  3. appends (buffer if reply context, else push) a `text` object for `caption` **and** an `image` object with `originalContentUrl=url`, `previewImageUrl=url` (same URL; matplotlib PNGs are ≪1 MB, under LINE's 10 MB/1 MB limits).
  Media-serve/token errors propagate as an exception → the caller's existing try/except degrades to text (R-3.5).
- **R-A12 (media server)** `GET /media/{token}.png` serves the file from `media_dir` iff `token` matches `^[A-Za-z0-9_-]{1,64}$` and the file exists; else `404`. No path segments/`..` accepted. `Content-Type: image/png`.
- **R-A13 (media TTL cleanup)** A periodic task (or on-write sweep) deletes media files older than `[line].media_ttl_seconds` (default 3600). Cleanup never raises into a send path.
- **R-A14 (ABC conformance / degradations)** `LineChannel(Channel)` implements `send`, `run`, `send_actionable`, `send_image`; **inherits** the base no-op/degrade defaults for `send_and_pin` (→ plain send, returns `None`), `edit_message` (→ `False`), `unpin`, `set_message_reaction`, `answer_callback_query`, `set_my_commands`. No crash on any of them. This is exactly what makes the live dashboard, reactions, and message-editing degrade cleanly (§ degradation table).
- **R-A15 (run loop)** `LineChannel.run(on_message, on_callback=None)` starts the aiohttp `AppRunner`/`TCPSite` on `bind_host:bind_port`, registers the two routes, starts the worker + TTL tasks, and awaits until cancelled (mirroring `TelegramChannel.run`'s long-poll-forever shape). Outbound LINE API calls reuse a shared `httpx.AsyncClient` (consistent with `TelegramChannel`).

### Module B — No-LLM mode
Master gate: `config.ollama.enabled`. When `false` (this branch's default), every LLM call site short-circuits deterministically. Full call-site table in §5.2.
- **R-B1 (no deferral rows, ever)** In `core/routing.py:handle_inbound_message`, **delete the Ollama-down deferral block** (the `if not dry_run and health_monitor is not None and not health_monitor.ollama_up:` branch that writes `db.insert_log(LogEntry(..., "unparsed", ...))`). In no-LLM mode a preparse miss must **never** write an `unparsed`/`awaiting_llm` row. `pending_unparsed()` therefore stays permanently empty.
- **R-B2 (preparse miss → clarify tier-1)** After `preparse.deterministic_parse` misses (and the reply-attribution shortcut misses), go **straight** to the existing `clarify.tier1_guesses` (deterministic, zero-LLM). Guesses exist → offer tap-to-fix quick replies (writes at most an `awaiting_clarify` row, per the existing clarify machinery). No guesses → the generic bilingual clarifying question + the `/log` quick-reply surface. This is the "disabled → clarify machinery" the cancelled v1.11 design specified.
- **R-B3 (no target-NL)** Skip the `looks_like_target_phrasing` / `classify_target_intent` block entirely. NL target-setting has no deterministic fallback; the reply points the user at the explicit, LLM-free `/target` command.
- **R-B4 (NL query → command pointers)** `query.answer_question` never calls the classifier; it returns a friendly pointer to the deterministic `/records`, `/trends`, `/dashboard` commands (reuse/extend the existing `query_cant_answer` copy). No wrong numbers, no writes (already the fail-closed contract).
- **R-B5 (confirmations LLM-free)** The diary/`text`-habit confirmation reflection uses the static `diary_reflection_fallback` line unconditionally; `chat_text` is never called. Every numeric/duration/boolean confirmation is already LLM-free.
- **R-B6 (review narrative LLM-free)** The weekly-review narrative uses `weekly_review_fallback_narrative` unconditionally; the factual stats block and charts (deterministic) are unchanged.
- **R-B7 (no probe)** The startup schema-conformance probe is skipped when `ollama.enabled=false` (guard the existing `probe_on_startup` block).
- **R-B8 (no Ollama health/recovery)** No Ollama liveness ping, no `ollama_down` owner alert, no `on_ollama_recovered` wiring, no startup/recovery `reparse_pending_unparsed` sweep. (On LINE, `HealthMonitor`'s Telegram half is also N/A; the monitor is not wired on this branch — connectivity is implicit in webhook delivery. Owner alerting for LINE API failures is a documented future add, §9.)
- **R-B9 (no OllamaClient construction)** When disabled, `core/app.py` does not construct `OllamaClient` (or constructs a null/stub that raises if ever called) — proving no accidental dependence.

### Module C — Trimmed daily digest + quota bookkeeping
- **R-C1 (one push/user/day)** A new scheduler job at `[digest].time` (default 20:00, once/day) composes, for each **active, non-opted-out** user with something worth saying, **one** push message batching: (a) **due-reminders summary** — habits still short of goal / not yet logged today, computed at digest time from existing deterministic progress helpers; (b) **daily summary** — the end-of-day recap (`streaks.compute_daily_summary`); (c) **almost-there nudge** line, if the user is nudge-eligible and ≥ threshold; (d) **grace notification** line, if grace was consumed for that user that day; (e) **release announcement** line, if a new version hasn't been announced to that user. One `channel.send` (push).
- **R-C2 (suppress all other proactive pushes)** On the LINE branch, the per-time **reminders**, **hourly check-ins**, **almost-there nudge**, **daily-summary job**, **grace notification**, **release-announcement fan-out**, and **month-end wrapped auto-send** do **not** send independently. Their content is either folded into the digest (R-C1) or made on-demand (R-C5). Suppression is gated inside `core/jobs.py` on `config.channel.type == "line"` (module C owns `jobs.py`), so `reminders.py`/`checkins.py`/`nudge.py` themselves are untouched (core diff minimal).
- **R-C3 (check-ins unavailable)** Hourly check-ins (up to 13 pushes/day) are structurally incompatible with one push/day; `/checkin` still parses but produces no pushes on LINE (the digest's due-reminders line is the substitute). Documented in the degradation table.
- **R-C4 (opt-out)** A user with `users.digest_opt_out = 1` receives no digest push. Provide a command to toggle it (reuse a `/digest on|off` matcher, or fold into `/quiet`/preferences — Luna's choice, must be LLM-free and audited like other preference writes).
- **R-C5 (weekly review + wrapped on-demand)** The weekly review and monthly recap are reply-only: available via commands (`/wrapped`, `/heatmap`, and a new `/review` that renders the weekly review as a reply — text + up to a few chart images via media URLs, ≤5 objects/reply). Optionally, on the weekly-review weekday, a one-line "your weekly review is ready — /review" is appended to that day's digest (`[digest].include_weekly_review_day`). No auto-pushed review, no auto-pushed charts.
- **R-C6 (quota bookkeeping)** Every push (i.e. every digest send, and any other push that ever occurs) calls `db.increment_push(user_id, yyyymm)` where `yyyymm` is the send's local month. This is the channel's responsibility on the push path (R-A6) so the count is authoritative regardless of caller.
- **R-C7 (owner quota warning)** When composing the **owner's** digest, if `db.monthly_push_total(yyyymm) >= config.digest.warn_cap` (default 280), append a bilingual warning line naming the current total and the free-plan ceiling (~300). This is the "owner warning as quota approaches the cap" the decision requires. It never blocks the digest.
- **R-C8 (per-user isolation preserved)** Digest composition reads only the target user's own logs/prefs; no cross-user leakage (same U-ISO discipline as every existing per-user surface).

### Module D — Deployment (Linux)
- **R-D1** A `systemd` unit runs the app on Linux: `ExecStart={venv}/bin/python -m habit_assistant.main`, `WorkingDirectory=<repo>`, `EnvironmentFile=<repo>/.env`, `Restart=on-failure`, `RestartSec=…`. systemd owns the process (no orphan-killer / single-instance guard needed — that Windows `.ps1` logic is dropped).
- **R-D2** A `run.sh` (venv activate + launch) for manual/dev runs; a `config.toml.line` template (channel=line, ollama.enabled=false, `[line]`/`[digest]` filled); a `.env.example` carrying the three `LINE_*` vars.
- **R-D3** `docs/DEPLOY-LINE.md` documents, spec-level, what runs where: venv creation (Python ≥3.11), `pip install -e .` (pulls aiohttp), the Tailscale Funnel command (`tailscale funnel <bind_port>` exposing `https://<host>.<tailnet>.ts.net/`), registering that URL + `/callback` in the LINE Developers console, the SQLite `db_path`, and a **backup cron** (`* --backup` via cron/systemd-timer, replacing the Windows Task Scheduler backup). Funnel and the LINE console steps are documentation, not code.
- **R-D4 (Linux-clean runtime)** The runtime path has no Windows-isms (verified: `pyproject.toml` force-include uses forward slashes; `core/fonts.py` uses `pathlib`; paths are relative). The `.ps1` launchers and the `.plist` are not used on this branch and are replaced by R-D1/R-D2. `data/` (SQLite + WAL + media) must be writable by the service user.

### Module — Integration (sequential, last)
- **R-I1 (channel selection)** `core/app.py` constructs `LineChannel(...)` when `config.channel.type == "line"` (else `TelegramChannel`, unchanged), and calls `channel.run(_on_message, on_callback=_on_callback)`. The health monitor, Ollama client, probe, and recovery sweep are **not** wired when `ollama.enabled == False` (R-B7/R-B8/R-B9).
- **R-I2 (job wiring)** Register the digest job (Module C) at `[digest].time`. On LINE, the minutely tick / weekly-review / daily-summary / grace / wrapped-auto jobs either are not registered or run in their suppressed form (R-C2). The dashboard day-rollover job is a no-op on LINE (no live dashboard).
- **R-I3 (rich menu at startup)** Call `channel`'s rich-menu registration at startup, after the app is up, fail-open.
- **R-I4 (owner)** `db.attribute_legacy_to_owner(secrets.line_owner_user_id)` and every owner-only surface (`/audit`, `/users`, admin commands, the digest quota warning) key off the LINE owner userId.
- **R-I5 (dashboard → on-demand)** `/dashboard` on LINE renders the "Today" board as a one-shot reply (no pin, no live edits); the `dashboard_msg_id` machinery is inert (base `send_and_pin` returns `None`, which the dashboard code already treats as "no live board").

## 5. Interfaces (signatures)

### 5.1 The LINE channel (satisfies `channels.base.Channel` exactly)
```python
# src/habit_assistant/channels/line.py
class LineChannel(Channel):
    def __init__(self, channel_access_token: str, channel_secret: str, owner_user_id: str,
                 config: Config, db: Database, *, client: httpx.AsyncClient | None = None) -> None: ...

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> str | None: ...
    async def send_actionable(self, chat_id: str, text: str, buttons: list[Button]) -> None: ...   # → quickReply postback (≤13)
    async def send_image(self, chat_id: str, image: bytes, caption: str, *, disable_notification: bool = False) -> None: ...  # → media URL
    async def run(self, on_message, on_callback=None) -> None: ...  # aiohttp server + queue + worker + TTL
    async def register_rich_menu(self) -> None: ...                 # startup, fail-open
    async def aclose(self) -> None: ...
    # send_and_pin / edit_message / unpin / set_message_reaction / answer_callback_query / set_my_commands: inherited no-op defaults
```

### 5.2 No-LLM call-site table (NORMATIVE — every LLM touch and its no-LLM disposition)
`config.ollama.enabled == False` on this branch. `parse_message` is the **only** model-backed extractor; the other `chat_json` callers do classification, the two `chat_text` callers produce prose.

| # | Site (file · function) | LLM method | Today's down/low-conf fallback | No-LLM-mode disposition (this branch) |
|---|---|---|---|---|
| 1 | `core/routing.py:handle_inbound_message` → `core/parser.py:parse_message` | `chat_json` | Deferral row if `ollama_up==False`; else fail-closed → `unknown` → clarify | **Skip.** preparse miss → `clarify.tier1_guesses` directly (R-B2). **No deferral row** (R-B1). |
| 2 | `core/routing.py:reparse_pending_unparsed` → `parse_message` | `chat_json` | Recovery sweep re-parses `pending_unparsed()` | **Dead.** No deferral rows ⇒ queue always empty. Not scheduled, not wired (R-B8). |
| 3 | `core/routing.py:handle_inbound_message` → `core/target_nl.py:classify_target_intent` | `chat_json` | Gated by `ollama_up`; fail-closed → `None` → normal parse | **Skip whole block** (R-B3). Point at `/target`. |
| 4 | `core/routing.py` (`kind=="query"`) → `core/query.py:answer_question` → `classify_query_intent` | `chat_json` | Fail-closed → `query_cant_answer` | **Skip classify** → pointer to `/records`/`/trends`/`/dashboard` (R-B4). |
| 5 | `core/confirmation.py:confirmation_text`/`generic_confirmation` (text/diary only) | `chat_text` | Static `diary_reflection_fallback` | **Force fallback** (R-B5). Numeric/duration/boolean already LLM-free. |
| 6 | `core/jobs.py:weekly_review_job` → `core/review.py:run_weekly_review` | `chat_text` | Static `weekly_review_fallback_narrative` | **Force fallback** (R-B6). Stats + charts unchanged. |
| 7 | `core/app.py:async_main` → `probe_schema_support` | probe | Gated by `probe_on_startup`; try/except | **Skip** (R-B7). |
| 8 | `core/health.py:run_once` → `check_ollama` (`GET /api/version`) | HTTP ping | Alerts owner on UP→DOWN; fires recovery sweep | **Skip Ollama half**; monitor not wired on LINE (R-B8). |

### 5.3 The digest job
```python
# src/habit_assistant/core/digest.py
async def run_daily_digest(db, channel, config, provider, *, clock=datetime.now) -> None: ...
def compose_digest(db, config, registry, lang, user_id, *, now) -> str | None: ...   # None = nothing to say → no push
```

### 5.4 Webhook + media (aiohttp)
```
POST /callback         # verify x-line-signature → enqueue events → 200 (400 on bad sig / bad body)
GET  /media/{token}.png  # serve tokened PNG (404 unknown/expired/invalid)
```

## 6. Files to touch

**New**
- `src/habit_assistant/channels/line.py` — real `LineChannel` (replaces the stub). *(Module A)*
- `src/habit_assistant/channels/line_webhook.py` — aiohttp server, signature verify, queue+worker, media routes, reply-buffer/contextvars. *(Module A)* — (may live inside `line.py`; separate file keeps `line.py` focused.)
- `src/habit_assistant/core/digest.py` — digest composition + job. *(Module C)*
- `assets/richmenu/richmenu.png` + a small `assets/richmenu/README.md` — deployment image asset spec. *(Module D)*
- `deploy/habit-assistant-line.service` (systemd), `deploy/run.sh`, `config.toml.line`, `docs/DEPLOY-LINE.md`. *(Module D)*

**Modified**
- `config.py` — `[channel]`, `OllamaConfig.enabled`, `[line]`, `[digest]`, `Secrets` LINE fields. *(Shared)*
- `storage/migrations.py` + `storage/db.py` — migration 014 + push/opt-out accessors. *(Shared)*
- `core/i18n.py` — new bilingual keys. *(Shared)*
- `pyproject.toml` — add `aiohttp>=3.9`; register pytest markers; assets force-include already forward-slash-clean. *(Shared)*
- `.env.example` — LINE vars. *(Module D)*
- `core/routing.py` — no-LLM gates: delete deferral block, skip target-NL, preparse-miss→clarify. *(Module B)*
- `core/query.py`, `core/target_nl.py`, `core/confirmation.py`, `core/review.py`, `core/health.py` — no-LLM short-circuits. *(Module B)*
- `core/jobs.py` — suppress per-time proactive sends on LINE; the digest job body. *(Module C)*
- `core/app.py` — channel selection, skip Ollama/probe/health when disabled, register digest job + rich menu, LINE owner. *(Integration)*
- `conftest.py` — extend shared doubles for LINE (a `RecordingLineChannel` or reply-buffer assertions); marker plumbing. *(Shared/Integration)*
- `VERSION` + `RELEASE_NOTES` (in `core/release_notes.py`) — branch version (§7). *(Shared)*

**Deliberately NOT touched** (keeps `core/` merge-clean): `reminders.py`, `checkins.py`, `nudge.py`, `dashboard.py`, `clarify.py`, `preparse.py`, `quicklog.py`, `undo_ui.py`, `routines.py`, `heatmap.py`, `wrapped.py`, `charts.py`, `records.py`, `trends.py`, `streaks.py`, `commands.py`, `parser.py`. Their behavior is reached unchanged through the channel seam + the config gates.

## 7. External dependencies

- **`aiohttp>=3.9`** — the one new runtime dependency. *Justification:* LINE has no long-poll; inbound requires a public HTTPS webhook, and images require an HTTPS media host — both need an HTTP **server**, which the current stack lacks (verified: httpx is client-only; no aiohttp/starlette/fastapi/uvicorn present). aiohttp is **one** dep, pure-asyncio (runs its `AppRunner` inside the existing event loop alongside APScheduler + the worker — no thread bridging, no second process), and provides both the `/callback` POST route and the `/media` GET route. **Rejected alternatives:** stdlib `http.server` (blocking/synchronous — fights the asyncio+APScheduler loop, needs a thread bridge for the queue), and `starlette`+`uvicorn` (**two** deps + transitive, violating the one-dep budget). Outbound LINE API calls reuse the existing **`httpx.AsyncClient`** (no new client dep), consistent with `TelegramChannel`.
- **Existing, unchanged:** `httpx>=0.27`, `apscheduler>=3.10,<4`, `pydantic>=2.6`, `pydantic-settings>=2.2`, `tzdata>=2024.1`; optional `matplotlib>=3.8` (`[charts]`).
- **LINE Messaging API** (verified facts the design relies on): Reply API messages are **free/uncounted**; Push/multicast/broadcast **count** (per recipient). Reply token is **single-use** and expires (worker replies immediately, well within the window). **≤5** message objects per reply/push. **≤13** quick-reply items. Postback `data` **≤300** chars. Text **≤5000** chars. Signature = `x-line-signature`, HMAC-SHA256, base64, over the raw body. Rich menu ≤20 areas, image 2500×1686 or 2500×843.
- **Tailscale Funnel** — provides the public HTTPS origin + valid TLS cert fronting `bind_host:bind_port` (infrastructure; documented, not code).
- **Branch version scheme (recommendation).** Use `VERSION = 1.0.0-line`, git tag `line/v1.0.0`, and bump the branch's own SemVer thereafter (`1.1.0-line` tagged `line/v1.1.0`, etc.). *Argument:* the LINE build is a distinct product edition (different transport, no LLM, different push model), so it deserves its **own** SemVer line starting at `1.0.0` rather than being a pre-release of the Telegram `2.0.0` (a `2.0.0-line.1` tag would sort *before* `2.0.0` and wrongly imply a main-line major bump). The `line/` tag namespace guarantees no collision with `main`'s `vX.Y.Z` tags and makes lineage obvious in `git tag`. One-time branch adjustment: the version-pin test and the `RELEASE_NOTES`/announce keying must accept the `-line` suffix (announce is folded into the digest anyway, R-C1). *(Alternative, if a plain string is preferred: `VERSION = line-1.0.0`, tag `line-v1.0.0` — human-clear but not SemVer-parseable; I recommend the suffixed form to keep SemVer tooling working.)*

## 8. Acceptance criteria

> Format: Given / When / Then. Every rule in §4 is covered by at least one AC. Module ownership in brackets and in §11.

- **AC1** *[Shared]* Given `config.toml` with `[channel] type="line"`, `[ollama] enabled=false`, and `.env` with the three `LINE_*` vars (no `TELEGRAM_*`), When the config+secrets load, Then it succeeds; with a LINE var missing, `load_secrets` raises `ConfigError` naming the missing var. (R-S1/S2/S3)
- **AC2** *[Shared]* Given a DB at schema 013, When the app opens it, Then it migrates to 014 creating `push_ledger` and adding `users.digest_opt_out` (default 0), with all existing rows intact and re-running the migration a no-op. (R-S4)
- **AC3** *[Shared]* Given a fresh month, When `increment_push(u,"2026-09")` is called 3×, Then `push_count(u,"2026-09")==3` and `monthly_push_total("2026-09")` sums across users; `set_digest_opt_out`/`digest_opt_out` round-trip. (R-S5)
- **AC4** *[Shared]* Given the test suite on this branch, When run as `pytest -m "not telegram_only and not llm_only"`, Then it is green; the `telegram_only`/`llm_only`-marked tests are deselected and do not fail the gate. (R-S7)
- **AC5** *[A]* Given a POST to `/callback` with a body and a correct `x-line-signature`, When received, Then it returns `200` and enqueues the events; Given a wrong/missing signature, Then `400` and nothing is enqueued or sent. (R-A1)
- **AC6** *[A]* Given a valid POST carrying 3 events, When received, Then `/callback` returns `200` **before** any handler runs, and the 3 events are processed FIFO by a single worker (order asserted). (R-A2/A3)
- **AC7** *[A]* Given one inbound text event whose handler calls `channel.send(...)` twice, When the event finishes, Then exactly **one** LINE **reply** call is made with 2 message objects using that event's `replyToken`, and **no push** and **no `push_ledger`** increment occur. (R-A4/A5)
- **AC8** *[A]* Given a proactive send with no active reply context, When `send`/`send_image` runs, Then it calls the **Push** API to `chat_id` and increments `push_ledger` for the current month. (R-A6/C6)
- **AC9** *[A]* Given `send_actionable(chat_id, text, [(l1,d1),…])`, When rendered, Then the message carries a `quickReply` with one postback item per button, `data==` the callback string verbatim, truncated to 13 items with a WARN if more. (R-A8)
- **AC10** *[A]* Given a `postback` event with `data="undo:98765"` (and likewise `log:`/`clarify:`/`routine:run:`), When processed, Then `on_callback(userId,"undo:98765","",…)` runs and the existing prefix router handles it unchanged; `source_text==""` and language falls back to stored pref/primary. (R-A9)
- **AC11** *[A]* Given `send_image(chat_id, png_bytes, caption)`, When called, Then a `{token}.png` is written under `media_dir`, an `image` message with `originalContentUrl`/`previewImageUrl = {public_base_url}/media/{token}.png` is sent (buffered as reply if in context, else push), `GET /media/{token}.png` returns the bytes with `image/png`, and a request for `/media/../secret` or an expired token returns `404`. (R-A11/A12)
- **AC12** *[A]* Given `media_ttl_seconds=1`, When a media file ages past it and the cleanup runs, Then the file is deleted and later `GET` returns `404`; cleanup never raises into a send. (R-A13)
- **AC13** *[A]* Given a `LineChannel`, When `send_and_pin`/`edit_message`/`unpin`/`set_message_reaction`/`answer_callback_query`/`set_my_commands` are called, Then they use the base no-op/degrade defaults without error (`send_and_pin`→`None`, `edit_message`→`False`). (R-A14)
- **AC14** *[A]* Given `[line].rich_menu_image` present, When the app starts, Then a default rich menu is created, its image uploaded, and it is set as the all-users default; a registration failure logs and startup continues. (R-A10/I3)
- **AC15** *[B]* Given `ollama.enabled=false` and an inbound message that the deterministic pre-parser cannot parse, When handled, Then **no** `unparsed`/`awaiting_llm` row is written and **no** LLM call is made. (R-B1)
- **AC16** *[B]* Given the same unparsed message, When `clarify.tier1_guesses` returns guesses, Then tap-to-fix quick replies are offered (writing at most an `awaiting_clarify` row); When it returns none, Then the generic clarifying question + `/log` surface is sent. (R-B2)
- **AC17** *[B]* Given NL target phrasing ("from now on 3L a day"), When handled in no-LLM mode, Then no goal is set and the reply points at `/target`; Given an NL question ("how much water this week?"), Then the reply points at `/records`/`/trends`/`/dashboard`; neither makes an LLM call. (R-B3/B4)
- **AC18** *[B]* Given a `diary` (text-habit) log, When confirmed in no-LLM mode, Then the static `diary_reflection_fallback` line is used and `chat_text` is never called; Given the weekly review runs, Then the fallback narrative is used and the stats/charts are unchanged. (R-B5/B6)
- **AC19** *[B]* Given `ollama.enabled=false` at startup, When the app boots, Then no schema probe runs, no `OllamaClient` is constructed (or a stub that raises if called), and no Ollama health ping / recovery sweep is wired. (R-B7/B8/B9)
- **AC20** *[C]* Given an active, non-opted-out user with logs today, When the digest job fires at `[digest].time`, Then that user receives **exactly one** push whose body batches due-reminders + daily-summary + (nudge if eligible) + (grace if any) + (announcement if pending). (R-C1)
- **AC21** *[C]* Given `channel.type=="line"`, When a day passes, Then the per-time reminder/check-in/nudge/daily-summary/grace/announcement/wrapped-auto sends produce **zero** independent pushes (only the digest pushes). (R-C2/C3)
- **AC22** *[C]* Given `users.digest_opt_out=1`, When the digest job fires, Then that user receives no push; a `/digest off` then `/digest on` toggles the flag and is audited. (R-C4)
- **AC23** *[C]* Given the digest job sends a push, When it completes, Then `push_ledger` for the user's current month is incremented by exactly one per push. (R-C6)
- **AC24** *[C]* Given the running month's `monthly_push_total >= [digest].warn_cap` (280), When the **owner's** digest is composed, Then it includes a bilingual quota-warning line naming the total and the ~300 ceiling; the digest still sends. (R-C7)
- **AC25** *[C]* Given `/wrapped`, `/heatmap`, or `/review`, When invoked, Then the result is delivered as a free **reply** (images via media URLs, ≤5 objects), and none of these is ever auto-pushed. (R-C5)
- **AC26** *[D]* Given the systemd unit on Linux, When enabled and started, Then the app launches under the venv Python with `Restart=on-failure`, `EnvironmentFile=.env`, and a writable `data/`; no `.ps1`/Task-Scheduler logic is required. (R-D1/D4)
- **AC27** *[D]* Given `docs/DEPLOY-LINE.md`, `config.toml.line`, `.env.example`, and `run.sh`, When followed, Then a fresh VPS reaches a running bot: venv → `pip install -e .` → Funnel exposing `bind_port` → webhook URL `/callback` registered in the LINE console → backup cron installed. (R-D2/D3)
- **AC28** *[Integration]* Given `config.channel.type=="line"`, When `async_main` runs, Then it constructs `LineChannel`, does **not** construct the Ollama client/probe/health monitor, runs the webhook server (not long-poll), registers the digest job + rich menu, and attributes the owner to `LINE_OWNER_USER_ID`; Given `type=="telegram"`, the Telegram path is byte-unchanged. (R-I1/I2/I3/I4)
- **AC29** *[Integration]* Given a running LINE process, When a user sends "500ml", taps an Undo quick reply, sends `/heatmap`, and the digest fires — all for the same user — Then: the log is confirmed via a free reply; the undo works via postback; the heatmap arrives as a free reply image; the digest is the only push (ledger +1); and a second user's data never appears in the first user's replies. (R-I5, U-ISO, end-to-end)
- **AC30** *[Integration]* Given `/dashboard` on LINE, When invoked, Then a one-shot "Today" board is sent as a reply with no pin and no live editing, and `dashboard_msg_id` stays inert. (R-I5)

## 9. Risks & open questions

- **OQ1 — Digest default: opt-out vs opt-in, given the quota ceiling.** The decision says "per-user opt-out" (⇒ default ON). But one user on a daily digest ≈ 30 pushes/month, so the ~300/month free quota is exhausted at ~9–10 active digest subscribers (research: defaults alone would be 326+/mo/user *without* the digest collapse; the digest fixes per-user volume to ~30/mo). **Default:** ship digest **opt-out (ON)** per the locked decision, with the owner warning (AC24) as the guardrail. **Who answers:** user. **If unanswered:** keep opt-out ON and document that beyond ~9 subscribers the owner must either buy LINE message capacity or switch new users to opt-in. *(Load-bearing: it caps how many users the free plan serves.)*
- **OQ2 — Reminder timeliness loss is inherent.** Collapsing 9 timed reminders into one 20:00 digest changes reminders from "ping me at 08:00" to "here's what's still due tonight." This is unavoidable under one-push/day and is the biggest UX change. **Who answers:** user (accept as designed). **If unanswered:** proceed as specified; the digest's due-reminders line is the substitute. *(Flagged, not blocking — it follows directly from the locked TRIMMED-DIGEST decision.)*
- **OQ3 — Rich-menu image is a deployment asset.** The 2500×1686 (or ×843) PNG with tappable regions must be produced (design task, not code). **Default:** ship a plain 6-button layout (`/log /habits /heatmap /wrapped /help /guide`) as a placeholder; Maya/Iris refine. **Who answers:** designer. **If unanswered:** placeholder ships; rich menu is fail-open so a missing asset just means no menu.
- **OQ4 — `/digest` command surface.** Whether the opt-out is a new `/digest on|off` matcher or folded into existing preferences. **Default:** new `/digest` matcher (Thai alias e.g. `สรุปรายวัน`), audited like `/quiet`. **Who answers:** Archi/Luna at build. Non-blocking.
- **Risk — reply-token timing.** If the worker ever can't reply within the token's validity, reactive output is dropped (never auto-pushed, to protect quota). Mitigated by no-LLM determinism (near-instant processing) and immediate reply in the worker. Documented (R-3.5/R-A5).
- **Risk — `reply_to_reminder` degrades.** LINE inbound events don't carry a reliable "reply-to-a-specific-bot-message" reference the way Telegram does, and per-time reminders don't fire on LINE anyway (digest). So the v1.10 reply-to-reminder attribution is inert on this branch. Documented in the degradation table; no code depends on it.
- **Risk — owner connectivity alerting gap.** With `HealthMonitor` unwired (R-B8), the owner isn't alerted if the LINE API or Funnel goes down. **Default:** rely on systemd + external uptime checks on the Funnel URL. A LINE-side health ping to the owner is a documented future add.

## 10. Out of scope

- Non-text inbound (image/sticker/location/audio/video), and group/room sources — user-only text (§2.1).
- Any LLM feature: NL extraction beyond the deterministic pre-parser, NL target-setting, NL Q&A answers, diary/review LLM prose. (Replaced by deterministic paths + command pointers.)
- Live pinned dashboard, message reactions, message editing, silent-vs-notifying push control — no LINE equivalent (degradation table §"Feature degradation").
- Hourly check-in pushes (quota-incompatible; `/checkin` parses but pushes nothing).
- Multiple pushes/user/day of any kind; multicast/broadcast; LINE Flex Messages / carousels (the digest is plain text; Flex is a future polish).
- Migrating the full ~118-file Telegram-transport + LLM-behavior test suites to LINE — those are marked branch-N/A (§8 AC4); only the transport-agnostic core, the no-LLM paths, and the new LINE tests are the gate.
- Buying LINE paid message capacity / LINE Login / LIFF.

### Feature degradation table (NORMATIVE)
| Telegram capability | LINE disposition | Mechanism |
|---|---|---|
| Live pinned "Today" dashboard (edit/pin) | **On-demand render only** — `/dashboard` sends a one-shot reply; opt-in inclusion of a line in the digest | LINE cannot pin/edit; base `send_and_pin`→`None`, `edit_message`→`False` (R-I5/R-A14) |
| Message reactions (emoji on a log) | **Skipped silently**, structure preserved | Base `set_message_reaction` no-op (R-A14) |
| Message editing (in place) | **None** | Base `edit_message`→`False` (R-A14) |
| Silent sends (`disable_notification`) | **No LINE equivalent** — replies are free (and notify per user settings), the one push notifies | `disable_notification` accepted but ignored on LINE (R-A7 note) |
| Images (heatmap / wrapped / charts) | **Served via public HTTPS media URL** through Funnel, tokened + TTL | `send_image` writes PNG → `/media/{token}.png` → LINE image message (R-A11/A12/A13) |
| Inline keyboard (callback_data) | **Quick replies (≤13) via postback**, `data` verbatim | `send_actionable`→`quickReply` (R-A8/A9) |
| Command menu (`setMyCommands`) | **Static rich menu** (message-action buttons) | `register_rich_menu` at startup; `set_my_commands` no-op (R-A10) |
| Per-time reminders / hourly check-ins / nudge / daily summary / grace / announcements | **Batched into one daily digest push** (check-ins dropped) | Suppressed in `jobs.py`; digest composes (R-C1/C2/C3) |
| Weekly review / monthly wrapped | **On-demand reply** (`/review`, `/wrapped`, `/heatmap`); optional digest-day line | R-C5 |
| Reply-to-reminder attribution | **Inert** (no LINE reply-ref; reminders don't fire per-time) | Documented, no dependency |
| `/guide`, `/help`, `/history`, `/records`, `/trends`, backfill, routines, cadence, pause, custom habits (`/addhabit`) | **Work as-is** — text/deterministic, reply-only | Unchanged through the channel seam |
| Long-poll inbound (`getUpdates`) | **Webhook** (`/callback`) + queue + worker | R-A1/A2/A3 |

## 11. Module split & parallel development

**Total functionals:** 11 — (1) LINE channel adapter, (2) inbound webhook server, (3) media server + `send_image`, (4) reply aggregation, (5) rich-menu command surface, (6) no-LLM mode, (7) trimmed daily digest, (8) push-quota bookkeeping + owner warning, (9) feature-degradation wiring, (10) on-demand weekly review/wrapped, (11) Linux deployment.

**Recommendation:** **PARALLEL** — one sequential shared surface, then **4 parallel modules** with disjoint file ownership, then a sequential integration pass. The graph is genuinely separable: the channel (Module A) touches only `channels/`; no-LLM (Module B) touches only core LLM-call-site leaves; the digest (Module C) touches only `core/digest.py` + `core/jobs.py`; deployment (Module D) is all new files. The only central file, `core/app.py`, is reserved for integration so no module edits it.

| Module | Owned ACs | Owned files | Depends on |
|---|---|---|---|
| **Shared surface** (sequential, first) | AC1, AC2, AC3, AC4 | `config.py`, `storage/migrations.py`, `storage/db.py`, `core/i18n.py`, `pyproject.toml`, `VERSION`, `core/release_notes.py`, `conftest.py` (markers + LINE doubles) | — |
| **A · line-channel** | AC5, AC6, AC7, AC8, AC9, AC10, AC11, AC12, AC13, AC14 | `channels/line.py`, `channels/line_webhook.py` | shared config/secrets, migration 014 accessors (for push increment on the push path) |
| **B · no-llm** | AC15, AC16, AC17, AC18, AC19 | `core/routing.py`, `core/query.py`, `core/target_nl.py`, `core/confirmation.py`, `core/review.py`, `core/health.py` | shared `[ollama].enabled` |
| **C · digest** | AC20, AC21, AC22, AC23, AC24, AC25 | `core/digest.py`, `core/jobs.py` | shared migration 014 + push accessors; reads B/A nothing (composes from existing deterministic helpers) |
| **D · deployment** | AC26, AC27 | `deploy/*.service`, `deploy/run.sh`, `config.toml.line`, `.env.example`, `docs/DEPLOY-LINE.md`, `assets/richmenu/*` | shared config field names (to fill the template) |
| **Integration** (sequential, last) | AC28, AC29, AC30 | `core/app.py`, `main.py` (thin), integration tests | A, B, C, D all complete |

Every AC belongs to exactly one module (no AC assigned twice).

**Shared surface** (built first, sequentially, before parallel modules start):
- `config.py`: `[channel].type`, `OllamaConfig.enabled`, `[line]`, `[digest]`, `Secrets` LINE fields.
- Migration **014** (`push_ledger` + `users.digest_opt_out`) + `Database` accessors (`increment_push`/`push_count`/`monthly_push_total`/`set_digest_opt_out`/`digest_opt_out`).
- i18n keys (bilingual) for digest, no-LLM pointers, clarify-on-LINE, quota warning.
- `pyproject.toml`: `aiohttp>=3.9`, pytest markers `telegram_only`/`llm_only`.
- Test scaffolding: a `RecordingLineChannel` (or reply-buffer assertions) in `conftest.py`, and the marker-based **LINE gate** selection.
- Branch `VERSION`/`RELEASE_NOTES` (`1.0.0-line`).

**Integration order** (after parallel modules complete):
1. Wire channel selection in `core/app.py` (LINE vs Telegram) and gate Ollama/probe/health construction on `ollama.enabled` (consumes A + B).
2. Register the digest job at `[digest].time` and the rich-menu registration at startup; confirm all per-time proactive jobs are suppressed on LINE (consumes C + A).
3. Owner attribution to `LINE_OWNER_USER_ID`; `/dashboard` on-demand degrade check.
4. Run the end-to-end integration tests (AC28–AC30) against a `LineChannel` driven by a fake aiohttp request/queue: inbound log → free reply; postback undo; `/heatmap` → reply image via media; digest → single push (+ledger); two-user isolation.
5. Confirm the LINE gate (`pytest -m "not telegram_only and not llm_only"`) is green.
