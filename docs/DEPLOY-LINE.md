# Deploying the LINE edition — runbook

SPEC-LINE.md §4 R-D3 (Module D, branch `line-version`). Target: a small
Linux VPS (2 cores / 16 GB is plenty), reached publicly through
**Tailscale Funnel** rather than a paid static IP + reverse proxy + TLS
cert. Written for a competent owner doing their **first Linux deploy of
this app** — every command is spelled out; skip steps you already know.

No LLM is involved anywhere in this deployment (`[ollama] enabled =
false` is permanent on this branch, SPEC-LINE.md header) — there is no
Ollama host to provision, no model to pull. That's the whole point of the
no-LLM decision: this is a much smaller deploy than the Telegram edition.

---

## 0. What you'll end up with

```
Internet ──HTTPS── Tailscale Funnel ──HTTP(127.0.0.1:8080)── aiohttp server ── habit_assistant.main
                                                                                       │
                                                                                  data/habits.db (SQLite)
```

- `habit-assistant-line.service` (systemd) runs the bot as a long-lived
  process, `Restart=on-failure`.
- Tailscale Funnel terminates TLS and forwards `https://<host>.<tailnet>
  .ts.net/` to the bot's local port — no port-forwarding, no cert
  management, no reverse-proxy config of your own.
- LINE POSTs signed webhook events to `.../callback`; the bot serves
  chart/heatmap images back out at `.../media/{token}.png`.
- A nightly timer/cron backs up the SQLite DB, mirroring the Windows
  Task Scheduler job the Telegram edition already uses.

---

## 1. Prerequisites

- A VPS running a systemd-based Linux distro (this runbook assumes
  Debian/Ubuntu; `deploy/setup.sh` uses `apt-get` and falls back to
  "install these yourself" on anything else). 2 vCPU / 16 GB RAM is
  comfortably more than this workload needs.
- SSH access with a sudo-capable user.
- Python **3.11+** available as `python3` (`python3 --version`) — install
  it from your distro's package manager first if it's older.
- A [Tailscale](https://tailscale.com/) account (free tier is fine) and
  this VPS added as a node. Funnel must be enabled for your tailnet (see
  §4) — it's off by default on new tailnets.
- A LINE account to create the Official Account / Messaging API channel
  (see §3). Free.
- `git` on the VPS, and network access to clone this repo.

---

## 2. Get the code onto the VPS and run setup.sh

```bash
sudo mkdir -p /opt/habit-assistant
sudo chown "$USER" /opt/habit-assistant
git clone <your-repo-url> /opt/habit-assistant
cd /opt/habit-assistant
git checkout line-version   # this branch

bash deploy/setup.sh
```

`deploy/setup.sh` is **idempotent** — re-run it any time after a `git
pull` and it won't clobber an existing `.env` or `config.toml`. What it
does, in order (read the script's own header comment for the exact
list):

1. Installs `python3-venv`/`python3-pip`/build headers via `apt` (skipped
   with a note if `apt-get` isn't found).
2. Creates a dedicated, unprivileged system user `habitbot` to run the
   service as (systemd hardening, SPEC-LINE.md §4 R-D1).
3. Creates the venv at `.venv/` and installs the app with the `[charts]`
   extra (`pip install -e ".[charts]"` — pulls in `aiohttp` too, since
   it's now a base dependency; matplotlib powers `/heatmap`/`/wrapped`/
   the weekly-review charts).
4. Copies `.env.line.example` → `.env` (only if `.env` doesn't already
   exist) and `chmod 600`s it.
5. Copies `config.toml.line` → `config.toml` (only if it doesn't already
   exist).
6. Creates `data/`, `data/media/`, `data/backups/` and `chown -R
   habitbot:habitbot` the whole repo (R-D4: `data/` — SQLite + WAL +
   tokened media PNGs — must be writable by the service user).
7. Installs and enables (but does **not** start) the three systemd units
   in `deploy/` (`habit-assistant-line.service`,
   `habit-assistant-line-backup.{service,timer}`).
8. Prints the exact `tailscale funnel` command for your configured
   `bind_port` — it does not run Tailscale commands itself (see §4).

Stop here and do **not** start the service yet — it needs the LINE
secrets (§3) and your Funnel hostname (§4) filled in first.

---

## 3. LINE Developers console

1. Go to <https://developers.line.biz/console/> and sign in (create a
   LINE account first if you don't have one).
2. **Create a provider** (a free-text organization name — e.g. your own
   name or business name) if you don't already have one.
3. Under that provider, **create a new channel** → **Messaging API**.
   Fill in the channel name/description/category/icon (shown to users in
   LINE) and agree to the terms.
4. Open the new channel → **Messaging API** tab:
   - **Channel access token**: click **Issue** next to "Channel access
     token (long-lived)". Copy it — this is `LINE_CHANNEL_ACCESS_TOKEN`.
   - Scroll to **Webhook settings**:
     - **Webhook URL**: leave blank for now — you'll fill this in after
       §4 gives you the Funnel hostname (comes back to this in step 8
       below).
     - **Use webhook**: turn **ON**.
   - Scroll to **LINE Official Account features**:
     - **Auto-reply messages**: turn **OFF**. (If left on, LINE's own
       canned reply fires *alongside* the bot's — confusing, and wastes
       nothing quota-wise but looks broken.)
     - **Greeting messages**: turn **OFF** (same reason — the bot's own
       `/start`/`/guide` flow replaces it; `/start` still triggers the
       app's own onboarding, unrelated to this LINE-side greeting
       feature).
   - These two toggles live in the **LINE Official Account Manager**
     (a separate site, <https://manager.line.biz/>) under **Settings →
     Response settings** if you don't see them on the Developers console
     page directly — **Chat**: **Response mode = Bot** (not "Chat"),
     **Auto-response messages = Disabled**, **Greeting messages =
     Disabled**.
5. Open the **Basic settings** tab → copy **Channel secret** — this is
   `LINE_CHANNEL_SECRET`.
6. Get your own `userId` for `LINE_OWNER_USER_ID` — easiest path: come
   back to this after §6 (service running, webhook registered): send the
   bot any message from your own LINE account, then read the `userId`
   out of the server log (`journalctl -u habit-assistant-line -f`) for
   that inbound event. Put it in `.env` and restart the service
   (`sudo systemctl restart habit-assistant-line`) — the owner is
   attributed once at startup (`R-I4`).

You now have all three `LINE_*` values for `.env`.

---

## 4. Tailscale + Funnel

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up   # opens an auth URL -- open it in a browser, log in
```

Confirm the node is up and note its tailnet hostname:

```bash
tailscale status
# or:
tailscale ip -4     # this box's tailnet IP
```

Your public hostname will be `https://<machine-name>.<tailnet-name>.ts.net`
— `tailscale status` shows `<machine-name>`; your tailnet's admin console
(<https://login.tailscale.com/admin/dns>) shows `<tailnet-name>`.

**Enable Funnel for the tailnet** (one-time, in the admin console): **DNS**
tab → confirm MagicDNS is on → **Access controls (ACL)** may need a
`nodeAttrs` entry allowing `funnel` for this node on a fresh tailnet —
Tailscale's Funnel onboarding flow prompts for this the first time you run
the command below if it's missing, with a link to fix it.

Once the bot is running (§6), expose it:

```bash
sudo tailscale funnel --bg 8080     # or your configured [line].bind_port
```

`--bg` keeps it running after your SSH session ends. Verify:

```bash
tailscale funnel status
```

This should show `https://<host>.<tailnet>.ts.net` forwarding to
`127.0.0.1:8080`. **This is your public webhook origin** — the full
callback URL LINE will POST to is:

```
https://<host>.<tailnet>.ts.net/callback
```

Go back to the LINE Developers console (§3, Messaging API tab → Webhook
settings) and paste that into **Webhook URL**, then click **Save**.

---

## 5. Fill in config and secrets, then start

```bash
sudo -u habitbot nano /opt/habit-assistant/.env
```

Fill in the three values from §3:

```
LINE_CHANNEL_SECRET=...
LINE_CHANNEL_ACCESS_TOKEN=...
LINE_OWNER_USER_ID=...          # can leave the placeholder for now, see §3 step 6
```

```bash
sudo -u habitbot nano /opt/habit-assistant/config.toml
```

Edit `[line].public_base_url` to your Funnel hostname from §4:

```toml
[line]
public_base_url = "https://<host>.<tailnet>.ts.net"
```

Everything else in `config.toml.line`'s defaults (digest time, habit
catalog, timezone, etc.) is a reasonable starting point — see the file's
own comments for what each section does. Then start the service:

```bash
sudo systemctl start habit-assistant-line.service
sudo systemctl status habit-assistant-line.service    # should show "active (running)"
journalctl -u habit-assistant-line -f                 # tail the logs
```

---

## 6. Rich menu image

The tappable command menu (`/log`, `/habits`, `/heatmap`, `/wrapped`,
`/help`, `/guide`) registers **automatically at startup** — no manual
console step needed (`register_rich_menu()`, Module A, R-A10/R-I3). It
uses whatever PNG `[line].rich_menu_image` points at (default
`assets/richmenu/richmenu.png`).

**That image currently ships as a placeholder** (SPEC-LINE.md §9 OQ3 —
see `assets/richmenu/README.md` for the full note): a plain 6-cell grid,
watermarked "PLACEHOLDER" in each cell, generated with the repo's own
bundled Thai font so the labels render correctly rather than as tofu
boxes. It's fully functional (LINE users can tap it, it just doesn't look
polished) — replace `assets/richmenu/richmenu.png` with a real design any
time and restart the service to re-register it. Registration is
fail-open: if the image is missing or LINE rejects it, the bot logs a
warning and keeps running with no rich menu rather than crashing.

If you'd rather build/verify the menu by hand in the console instead:
LINE Developers console → your channel → **Messaging API** tab → **Rich
menus** → you can inspect what got registered, or create/set one
manually (image + tap-area rects + message actions) if you want to bypass
the app's own registration entirely — not required for normal operation.

---

## 7. Verification checklist

1. **Webhook verify button**: LINE Developers console → Messaging API tab
   → Webhook settings → click **Verify** next to the Webhook URL. Expect
   a green success — LINE sends a signed test POST and expects `200`.
   A failure here almost always means either the Funnel URL is wrong/down
   (`tailscale funnel status`) or the service isn't running
   (`systemctl status habit-assistant-line`).
2. **First message / `/start`**: message the bot from your own LINE
   account. You should get an onboarding reply. Check
   `journalctl -u habit-assistant-line -f` for the inbound event —
   confirm `LINE_OWNER_USER_ID` (§3 step 6) if you haven't yet, then
   `sudo systemctl restart habit-assistant-line`.
3. **A quick log**: send e.g. `500ml` — expect an instant confirmation
   reply (deterministic pre-parser, no LLM anywhere on this branch).
   Tap the "↩︎ Undo" quick-reply button that comes with it — confirm the
   undo works (this exercises the postback → `on_callback` path, R-A9).
4. **An image reply**: send `/heatmap` — expect a chart image to arrive
   as a reply (media URL through Funnel, R-A11/A12). If it fails, check
   `journalctl` for a media-serve error and confirm `data/media/` is
   writable by `habitbot` (`ls -la /opt/habit-assistant/data`).
5. **Rich menu**: check that the 6-button menu appears at the bottom of
   the chat (may take a moment / a chat re-open to show up on first
   registration — a LINE client-side caching quirk, not a bug here).
6. **Digest test**: rather than waiting for `[digest].time` (default
   20:00) to fire naturally, temporarily set it a couple of minutes
   ahead in `config.toml`, `sudo systemctl restart
   habit-assistant-line`, log a couple of habits, and confirm exactly
   **one** push arrives at the configured time (not a reply — a LINE
   push notification, since there's no active reply context for a
   scheduler-fired job). Revert `[digest].time` back to your real
   preference afterward and restart again.
7. **Quota bookkeeping**: after the digest test above,
   `sqlite3 data/habits.db "select * from push_ledger;"` should show a
   `count` of `1` for your `userId` in the current `yyyymm`.

---

## 8. Backup

Two equivalent options ship in `deploy/` — pick **one** (running both
just means two redundant backups a night, harmless but pointless):

**Option A — systemd timer** (matches the rest of this deploy, already
installed + enabled by `setup.sh`):

```bash
systemctl list-timers habit-assistant-line-backup.timer
# fires nightly at 03:30, same clock time as the Windows Task Scheduler
# backup job this replaces
```

**Option B — plain cron**, if you'd rather not use systemd timers:

```bash
sudo crontab -u habitbot deploy/backup.cron
# then disable option A so you don't get both:
sudo systemctl disable --now habit-assistant-line-backup.timer
```

Either way, backups land in `data/backups/` (`[backup].dir`/`.retain` in
`config.toml`, default: keep the most recent 14). Restore with the
existing CLI (`python -m habit_assistant.main --restore <file> --yes`,
same as the Telegram edition — nothing LINE-specific about restore).

---

## 9. Troubleshooting

**Funnel is down / webhook verify fails**
- `tailscale funnel status` — confirm it's still forwarding. A Tailscale
  client update or a `tailscale up` re-auth can sometimes drop an active
  Funnel; re-run `sudo tailscale funnel --bg 8080`.
- `systemctl status habit-assistant-line` — confirm the process itself
  is up (`active (running)`); Funnel forwarding to a dead backend also
  shows as a webhook-verify failure.
- Nothing in this app alerts you proactively if Funnel/the LINE API goes
  down — `HealthMonitor` is intentionally not wired on this branch
  (SPEC-LINE.md §9 "owner connectivity alerting gap": Ollama health is
  meaningless in no-LLM mode, and there's no equivalent liveness concept
  for a webhook). Rely on systemd (`Restart=on-failure` brings the
  process itself back) plus an **external uptime check** against your
  Funnel URL (e.g. a free UptimeRobot/Healthchecks.io ping against
  `https://<host>.<tailnet>.ts.net/media/nonexistent.png`, which should
  reliably return `404` — a `502`/timeout means Funnel or the process is
  down) — this is a documented future add, not built here.

**Signature failures (`400` on every webhook, or `journalctl` shows
"invalid signature" warnings)**
- Almost always a stale/wrong `LINE_CHANNEL_SECRET` in `.env` — re-copy
  it from the console's Basic settings tab (not the access token — these
  are two different values, easy to swap by mistake) and
  `sudo systemctl restart habit-assistant-line`.
- Confirm `.env` has no stray quotes/trailing whitespace around the
  value — `EnvironmentFile=` in systemd parses it literally.
- If you're behind any proxy that might rewrite the request body (not
  the case with a direct Funnel setup, but worth ruling out on a
  customized deploy), the signature is computed over the **raw** body
  bytes — any re-serialization breaks it.

**Quota warnings** (`⚠️` line appended to the owner's digest, or the
`push_ledger` monthly total approaching `[digest].warn_cap`, default 280)
- Expected behavior once you're near LINE's free-plan ceiling (~300
  pushes/month total across all users) — not a bug. The digest still
  sends; this is just an early warning (R-C7).
- Each active, non-opted-out user costs ~1 push/day (~30/month) under the
  trimmed-digest design — the free plan comfortably serves roughly 9–10
  such users (SPEC-LINE.md §9 OQ1). Options once you're consistently
  near the cap: ask some users to `/digest off` (opt out — they keep
  full reply-based functionality, just no daily push), or buy LINE's
  paid message-volume tier (out of scope for this deploy, LINE
  Developers console → your channel → billing).
- If the warning is wrong / the count looks stale, check
  `sqlite3 data/habits.db "select yyyymm, sum(count) from push_ledger
  group by yyyymm;"` directly — `monthly_push_total` is a live sum, not a
  cached value, so a mismatch would point at a clock/timezone bug worth
  reporting rather than a real quota issue.

**Service won't start / crash-loops**
- `journalctl -u habit-assistant-line -n 50 --no-pager` for the actual
  traceback.
- A bad `config.toml` (TOML syntax error, or a value failing pydantic
  validation) raises `ConfigError` at startup with an actionable message
  — check the last few log lines.
- `systemctl status` showing `failed` after 5 restarts within 60s
  (`StartLimitBurst=5`/`StartLimitIntervalSec=60` in the unit) means it's
  crash-looping on something persistent, not a transient blip — fix the
  root cause, then `sudo systemctl reset-failed
  habit-assistant-line && sudo systemctl start habit-assistant-line`.

**`RuntimeError: ... NumPy 1.x cannot be run in ...` / `RuntimeError:
Numpy baseline X86_V2` / crash-loop right after enabling `[charts]`**
- The VPS's CPU predates the x86-64-v2 baseline (missing SSE4.2) that
  numpy 2.x's manylinux wheels are compiled to require — numpy raises
  (or segfaults) on import, which shows up as a crash loop the moment
  `[charts]` is installed, not a chart-rendering bug.
- Fixed permanently: `pyproject.toml`'s `[charts]` extra pins
  `numpy>=1.26,<2`, which has no such baseline requirement. A normal
  `pip install -e ".[charts]"` (what `deploy/setup.sh` step 4 runs)
  already picks up the pin — nothing else to do on a fresh or re-run
  deploy.
- If you hit this on a box that already has a numpy 2.x wheel installed
  (e.g. a venv built before this pin existed), force the downgrade
  directly: `pip install 'numpy<2'` (or `1.26.4` specifically, the
  version verified working), then restart the service.

---

## 10. Updating

```bash
cd /opt/habit-assistant
git pull
bash deploy/setup.sh          # idempotent -- re-installs deps, leaves .env/config.toml alone
sudo systemctl restart habit-assistant-line.service
```

Migrations (if any shipped in the update) apply automatically on next
startup (`storage/migrations.py`'s own `user_version` runner) — no
manual `--migrate` step needed, though it's available
(`python -m habit_assistant.main --migrate`) if you want to apply and
inspect the version bump before restarting the live service.
