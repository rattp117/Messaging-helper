# SPEC — Local Habit-Tracking Assistant

> Provided verbatim by the user (2026-08-19). This is the authoritative spec.
> Dev/build machine: Windows Server (this box) — no Ollama, no system Python (use `uv`).
> Deploy target: Mac Mini M2 Pro, macOS, always-on. launchd plist + README instructions target macOS.
> Tests must mock Ollama and Telegram; live integration is verified on the Mac Mini by the user.

---

You are building a small, local-first personal habit-tracking assistant that runs 24/7 on my Mac Mini (M2 Pro, macOS). It sends me reminders and captures my replies over a chat channel, uses a **local** LLM (Qwen via Ollama) to parse my free-form replies into structured data, stores everything in SQLite, and sends me a weekly review.

Build a **runnable MVP first**, then iterate. Ask me before making large architectural deviations from this spec; otherwise proceed and keep commits small.

## 1. Runtime environment (fixed)

- **Host:** Mac Mini M2 Pro, macOS, always-on. Single long-running Python process.
- **Python:** 3.11+, managed with a `venv`. Use `uv` if available, else `pip`.
- **LLM:** Qwen running locally via **Ollama** at `http://localhost:11434`. Do not call any cloud LLM. The model tag is configurable (I'll set e.g. `qwen3.5` or whatever `ollama list` shows).
- **Channel (MVP):** Telegram Bot API via **long polling** (`getUpdates`) — chosen specifically so no public webhook / tunnel is needed. The bot token and my user/chat ID come from config.

## 2. Hard constraints

- **Local-only data:** the SQLite DB, config, and all diary text stay on disk on this machine. The only outbound network calls are to `api.telegram.org` and `localhost:11434`.
- **Channel-abstracted:** all Telegram-specific code sits behind a `Channel` interface (see §8) so a `LineChannel` can be dropped in later without touching the scheduler, parser, or storage. This is the one seam I care most about — keep it clean.
- **Bilingual:** I write in mixed **Thai + English**. The parser must handle both (e.g. "ดื่มน้ำ 2 แก้ว", "did 10 min stretch", "500ml").
- No web framework needed for MVP (long polling, not webhooks). Keep dependencies minimal.

## 3. Architecture

One persistent process that does two things concurrently:

1. **Scheduler** (`APScheduler`, `AsyncIOScheduler`): fires reminders at configured times and the weekly review job.
2. **Inbound loop:** long-polls Telegram for my messages; every inbound message is routed to the parser regardless of whether it followed a reminder (so I can log "2 glasses" anytime, unprompted).

Use `asyncio` throughout. Prefer `python-telegram-bot` (v21+, async) for the channel, or raw `httpx` long-poll if that keeps the abstraction cleaner — your call, but justify it briefly.

## 4. Project structure

```
habit-assistant/
  pyproject.toml / requirements.txt
  config.toml            # schedule, goals, model name (non-secret)
  .env                   # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (gitignored)
  .env.example
  README.md
  com.ratt.habit-assistant.plist   # launchd keepalive
  data/
    habits.db            # created on first run
  src/habit_assistant/
    __init__.py
    main.py              # wiring: load config, start scheduler + inbound loop
    config.py            # typed config loader (pydantic-settings)
    channels/
      base.py            # Channel ABC: send(text), poll()/on_message hook
      telegram.py        # TelegramChannel
      # line.py          # (future — leave a stub + docstring)
    llm/
      ollama_client.py   # structured JSON extraction via Ollama
      prompts.py         # extraction + weekly-review prompt templates
    storage/
      db.py              # SQLite access (stdlib sqlite3, WAL mode)
      models.py          # dataclasses for a log entry
    core/
      reminders.py       # reminder definitions + scheduling
      parser.py          # message -> structured entry (calls llm + validates)
      review.py          # weekly aggregation + narrative
  tests/
    test_parser.py
    test_db.py
```

## 5. Data model (SQLite)

```sql
CREATE TABLE IF NOT EXISTS logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,              -- ISO8601 local time of the event
  category    TEXT NOT NULL,              -- 'water' | 'stretch' | 'diary'
  value_num   REAL,                       -- water: ml; stretch: minutes; diary: NULL
  value_text  TEXT,                       -- diary text; else NULL
  raw_message TEXT NOT NULL,              -- exactly what I sent
  source      TEXT NOT NULL DEFAULT 'reply', -- 'reply' | 'unprompted'
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_logs_ts_cat ON logs(ts, category);
```

Config (goals, schedule) lives in `config.toml`, **not** the DB.

## 6. Behaviors

**Reminders** (all times/goals configurable in `config.toml`; these are sensible defaults):
- **Water:** 08:00, 10:30, 13:00, 15:30, 18:00, 20:30 — "💧 Time for water. How much did you drink?"
- **Stretch:** 11:00, 16:00 — "🧘 Stretch break — do a few minutes and tell me how long."
- **Diary:** 21:30 — "📓 How was today? A few lines is enough."
- Daily hydration goal default 2500 ml.

**Capture / parse:** on any inbound message, call the parser. The parser sends the message to Qwen and gets back a strict JSON object (§7). Validate it, write a `logs` row, then send a **confirmation** reply:
- water → `✅ 500 ml logged — today 1500 / 2500 ml (60%)`
- stretch → `✅ 10 min stretch logged — 2nd today`
- diary → `✅ Saved. <one gentle one-line reflection from Qwen>`
- `unknown` → ask a short clarifying question, don't write a row.

**Weekly review:** Sunday 20:00. Aggregate the last 7 days from SQLite (water adherence % vs goal per day, total/average, stretch count + current streak, diary entry count). Pass the aggregates to Qwen to write a short, encouraging narrative with 1–2 concrete suggestions. Push it to me over the channel. Keep it factual — no medical advice.

## 7. Qwen / Ollama integration

- Call Ollama's `POST /api/chat` with `stream: false` and **structured outputs**: pass a JSON schema in the `format` field so extraction is reliable. Target schema:

```json
{
  "category": "water | stretch | diary | unknown",
  "water_ml": "integer or null",
  "stretch_min": "integer or null",
  "diary_text": "string or null",
  "confidence": "number 0..1"
}
```

- Normalize casual units in the prompt instructions: 1 glass/แก้ว ≈ 250 ml, 1 bottle/ขวด ≈ 600 ml (make these constants configurable). If I give an explicit ml, use it.
- The model may be a "thinking" Qwen variant — set the Ollama `think: false` option if supported, and in all cases **robustly extract the JSON** (strip any `<think>...</think>` or prose before parsing). Fail closed to `category: "unknown"` on parse failure; never crash the loop.
- Keep the model tag, base URL, and unit constants in config.

## 8. Channel abstraction

`channels/base.py` defines an ABC:

```python
class Channel(ABC):
    async def send(self, text: str) -> None: ...
    async def run(self, on_message: Callable[[str], Awaitable[None]]) -> None: ...
```

`TelegramChannel` implements `send` via the Bot API and `run` via long-poll, calling `on_message(text)` for each of my messages. Leave `channels/line.py` as a documented stub describing exactly what a `LineChannel` would implement (LINE Messaging API: push for `send`, a webhook receiver for inbound — note that LINE, unlike Telegram, needs a public endpoint). Nothing in `core/` or `storage/` may import a concrete channel.

## 9. Keep-alive (macOS)

Provide `com.ratt.habit-assistant.plist` (a launchd LaunchAgent) that runs the process, restarts on crash (`KeepAlive`), and starts at login. Include load/unload instructions in the README. Do **not** use cron — the process is persistent (it long-polls), so scheduling lives in-process via APScheduler; launchd only keeps the process alive.

## 10. Dev affordances

- `python -m habit_assistant.main --test-reminder water` → fires one reminder immediately.
- `python -m habit_assistant.main --seed` → inserts a few days of fake logs so I can test the weekly review without waiting.
- `--dry-run` → parse + print structured output without writing to DB or sending confirmations.
- Structured logging to stdout (launchd captures it) at INFO; DEBUG shows the raw Qwen JSON.

## 11. Build order

1. Config loader + SQLite layer + `Channel` ABC + `TelegramChannel.send`. Prove I can receive a message from the bot.
2. Long-poll inbound + echo, to confirm the loop works.
3. Ollama structured extraction + parser + confirmations (the core loop).
4. APScheduler reminders.
5. Weekly review + `--seed`.
6. launchd plist + README.
7. Tests for parser (mock Ollama) and DB.

Deliver a working step 3 before moving on — that's the real proof.

**Mode: SEQUENTIAL** — the build order is a dependency chain (config → storage/channel → parser → scheduler → review). One Luna, one Vera.

### Acceptance criteria

- AC1: Typed config loads from `config.toml` + `.env` (pydantic-settings); missing token fails with a clear error.
- AC2: SQLite layer creates `logs` table + index per §5 on first run, WAL mode enabled; insert + query round-trips.
- AC3: `Channel` ABC per §8; `TelegramChannel` implements `send` and `run` (long-poll); no `core/` or `storage/` module imports a concrete channel.
- AC4: Parser sends inbound text to Ollama `/api/chat` with `stream: false` and JSON-schema `format`; validates the §7 schema; strips `<think>` blocks / prose; fails closed to `unknown` without crashing.
- AC5: Bilingual normalization: "ดื่มน้ำ 2 แก้ว" → water 500 ml, "did 10 min stretch" → stretch 10 min, "500ml" → water 500 ml (glass/bottle constants configurable).
- AC6: Confirmations per §6 formats, including daily water total vs goal and stretch count ordinal; `unknown` → clarifying question, no DB row.
- AC7: Reminders fire at configured times via AsyncIOScheduler; weekly review Sunday 20:00; all times from `config.toml`.
- AC8: Weekly review aggregates 7 days (water adherence/day, total/avg, stretch count + streak, diary count) and sends narrative via channel.
- AC9: CLI flags `--test-reminder <cat>`, `--seed`, `--dry-run` work per §10.
- AC10: `.env.example`, `.gitignore` (excludes `.env`, `data/`), README with macOS setup (venv, ollama pull, .env, launchd load/unload), and `com.ratt.habit-assistant.plist` exist.
- AC11: Tests: parser tests with mocked Ollama (incl. bilingual cases, think-block stripping, parse-failure fail-closed) and DB tests pass on Windows dev box.

## 12. Non-goals (MVP)

- No web UI / dashboard, no auth, no multi-user.
- No Garmin integration yet — but leave a `TODO` note in `review.py` for a future CSV import that joins Garmin hydration export against the `water` logs (I already analyze Garmin exports elsewhere).
- No cloud deployment.

## 13. Deliverables

- Runnable repo per the structure above, `README.md` with setup (venv, `ollama pull`, `.env`, launchd load), and the `.plist`.
- `.env.example` and a `.gitignore` that excludes `.env` and `data/`.
