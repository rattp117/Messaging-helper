# Habit Assistant

A local-first Telegram habit-tracking assistant. One long-running Python
process: an `APScheduler` reminder schedule + a Telegram long-poll inbound
loop, both backed by a local Qwen model served through Ollama for message
understanding. All data (SQLite DB, config, diary text) stays on disk.
Outbound network calls are limited to `api.telegram.org` and your Ollama
host.

Primary runtime host: **this Windows machine** (24/7). A macOS deploy path
(`launchd`) is also documented below as an alternative target, since the
code is fully cross-platform (`pathlib` throughout, no OS-specific calls
outside the two setup paths below).

## Requirements

- Python 3.11+ (managed via [`uv`](https://docs.astral.sh/uv/); a system
  Python is not required)
- An Ollama server reachable over HTTP, with a Qwen model pulled (see below)
- A Telegram bot token + chat ID (see "Telegram setup")

## Setup — Windows (primary)

```powershell
# from the repo root
uv venv --python 3.12
uv pip install -e ".[dev]"

# optional: weekly-review chart images (ROADMAP.md v1.0.0). Skip this and
# leave [charts] enabled = true in config.toml -- the review just falls
# back to text-only until this extra is installed.
uv pip install -e ".[charts]"

# config.toml is checked in (non-secret: schedule, goals, model tag, Ollama URL).
# Edit it if your Ollama host, reminder times, or hydration goal differ from
# the defaults.

# secrets
Copy-Item .env.example .env
notepad .env   # fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
```

Pull the model on the Ollama host referenced by `config.toml`'s
`[ollama].base_url` (run this on that host, not necessarily this machine):

```bash
ollama pull qwen3.5:9b-mlx   # or whatever [ollama].model is set to
```

Run it directly:

```powershell
.venv\Scripts\python.exe -m habit_assistant.main
```

Or via `uv run` without activating the venv:

```powershell
uv run python -m habit_assistant.main
```

### Keeping it alive on Windows (Task Scheduler)

`start-assistant.ps1` at the repo root resolves the venv and execs the
process — no extra dependencies. Register it as a scheduled task that
starts at logon/boot and restarts on failure:

1. Open **Task Scheduler** → **Create Task…** (not "Basic Task", so you get
   the restart-on-failure options).
2. **General**: name it `Habit Assistant`; "Run whether user is logged on
   or not" if you want it to survive logoff; check "Run with highest
   privileges" only if needed.
3. **Triggers**: New → **At startup** (and/or **At log on**).
4. **Actions**: New → Program/script: `powershell.exe`, arguments:
   `-NoProfile -ExecutionPolicy Bypass -File "C:\path\to\repo\start-assistant.ps1"`.
5. **Settings**: check "If the task fails, restart every" → e.g. `1 minute`,
   attempt up to a high retry count; uncheck "Stop the task if it runs
   longer than" (this is a long-running process by design).
6. Save. Test with `Start-ScheduledTask -TaskName "Habit Assistant"`, then
   check `data\habits.db` and the console/log output.

Equivalent one-liner via `schtasks` (adjust the path):

```powershell
schtasks /Create /TN "Habit Assistant" /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"C:\path\to\repo\start-assistant.ps1`"" /SC ONSTART /RL HIGHEST /F
```

To stop: `Stop-ScheduledTask -TaskName "Habit Assistant"` or find and stop
the `python.exe` process; to unregister: `Unregister-ScheduledTask -TaskName
"Habit Assistant"`.

## Setup — macOS (alternative deploy target)

```bash
cd /path/to/repo
uv venv --python 3.12          # or: python3 -m venv .venv
uv pip install -e ".[dev]"     # or: pip install -e ".[dev]"

cp .env.example .env
$EDITOR .env                   # fill in TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

ollama pull qwen3.5:9b-mlx     # whatever [ollama].model is set to, on the Ollama host
```

Run it directly to confirm it starts:

```bash
.venv/bin/python -m habit_assistant.main
```

### Keeping it alive on macOS (launchd)

`com.ratt.habit-assistant.plist` is a `LaunchAgent` with `RunAtLoad` and
`KeepAlive` (restart on crash, not on clean exit). No cron is involved —
all scheduling (reminders, weekly review) happens in-process via
`APScheduler`; launchd's only job is keeping the process alive.

1. Edit the plist: replace `/Users/YOUR_USERNAME/habit-assistant` with your
   actual clone path (three places: `ProgramArguments`, `WorkingDirectory`,
   the two log paths).
2. Copy it into place and load it:

   ```bash
   cp com.ratt.habit-assistant.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.ratt.habit-assistant.plist
   ```
3. Check it's running: `launchctl list | grep ratt`, and tail
   `data/habit-assistant.log`.
4. To stop/unload: `launchctl unload ~/Library/LaunchAgents/com.ratt.habit-assistant.plist`.
5. After editing the plist or upgrading the code, unload then load again to
   pick up changes.

## Developing on Windows without live services (this repo was built this way)

This repo was implemented on a Windows box with no local Ollama and no
Telegram token. The code is structured so that's fine:

- `channels/telegram.py` and `llm/ollama_client.py` both take an optional
  injected `httpx.AsyncClient` — tests/smoke scripts pass an
  `httpx.MockTransport` instead of hitting the network.
- `storage/db.py` takes a `db_path`; `--seed` + the weekly review run fully
  offline against SQLite.
- `--dry-run "<message>"` exercises the parser end-to-end without touching
  Telegram (it still calls Ollama unless you inject a fake client
  programmatically).

See `IMPL.md` for exactly what was smoke-tested and how, including results
against a real Ollama server once one became reachable.

## Configuration

- `config.toml` — non-secret: reminder times, hydration goal, Ollama base
  URL + model tag, unit constants (glass/bottle ml), timezone, DB path, log
  level, weekly-review schedule. Safe to commit.
- `.env` (gitignored, see `.env.example`) — `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`. Missing/invalid values fail startup immediately with
  a clear error pointing at `.env.example`.
- `data/` (gitignored) — `habits.db` (SQLite, WAL mode), created on first
  run.

### `[charts]` — weekly review images (v1.0.0)

```toml
[charts]
enabled = true   # renders a PNG per chartable habit and attaches it to the
                  # weekly review, IF matplotlib is installed (see below).
```

Chart rendering needs the optional `matplotlib` dependency
(`uv pip install -e ".[charts]"`). Numeric habits with a goal get daily bars
plus a goal line; other numeric/duration/boolean habits get daily count/total
bars; `text` habits (e.g. `diary`) have nothing numeric to chart and are
skipped. If `enabled = true` but matplotlib isn't installed, the app logs a
one-time warning and the review is sent as plain text, exactly like
`enabled = false` — chart rendering never blocks or crashes the review.

### `[garmin]` — hydration cross-check (v1.0.0)

```toml
[garmin]
csv_path = ""                     # empty = feature off (default)
discrepancy_threshold_ml = 300    # flag a day if |self-reported - Garmin| exceeds this
[garmin.column_map]
date = "Date"
hydration_ml = "Hydration(ml)"
```

Point `csv_path` at a local Garmin hydration CSV export to turn this on. Each
weekly review then joins that file (by date) against your own logged `water`
entries and appends a per-day self-reported-vs-Garmin comparison, flagging
days whose difference exceeds `discrepancy_threshold_ml`. `column_map` maps
this app's field names to the CSV's header names — export column naming
varies by device/locale, so adjust it to match your file. The CSV is read
locally only; it is never uploaded anywhere. A missing or malformed file at
review time doesn't fail the review — it appends a bilingual "Garmin data
unavailable" note instead.

## CLI flags

```
python -m habit_assistant.main --test-reminder water    # fire one reminder now, exit
python -m habit_assistant.main --seed                    # insert ~1 week of fake logs
python -m habit_assistant.main --dry-run "500ml water"   # parse only, print JSON, no DB/send
python -m habit_assistant.main                           # normal run: scheduler + inbound loop
```

`--seed` and `--dry-run` don't require Telegram credentials. A normal run
or `--test-reminder` does.

## Tests

```powershell
uv run pytest
```

## Project layout

See `SPEC.md` §4. Channel abstraction lives in `channels/base.py`
(`Channel` ABC); `core/` and `storage/` never import a concrete channel —
only `main.py` wires a concrete `TelegramChannel` in. `channels/line.py` is
a documented stub for a future LINE integration (needs a public webhook,
unlike Telegram's long-poll).

## Non-goals (MVP)

No web UI, no auth, no multi-user, no cloud deployment.

## 1.0 stability

As of `v1.0.0`, `config.toml`'s format and the SQLite schema (`storage/
migrations.py`) are considered **stable**: existing keys keep their meaning
and existing DB rows keep their shape across future releases. New optional
keys/sections may be added (with safe defaults, so an old `config.toml`
keeps working unchanged) and schema changes will keep going through the
`user_version` migration runner (`storage/migrations.py`) rather than ever
requiring a manual DB edit. `--backup`/`--restore` (ROADMAP.md v0.3.0) remain
the supported path for moving data across a schema upgrade.
