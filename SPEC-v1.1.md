# Spec — v1.1.0: Undo menu (Telegram discoverability) + user-settable per-habit targets

## 1. Problem statement
v0.5.0 already ships working, bilingual, LLM-free undo/edit with soft-delete, but the only way a user
discovers or triggers undo is by typing `/undo` / "ยกเลิก" — there is no visible affordance in Telegram.
Separately, every habit's daily goal is baked into `config.toml` (`[[habits]].goal`, and the legacy
`[reminders.water].goal_ml`), so the user cannot change a target without editing a file and restarting the
24/7 bot. v1.1.0 delivers three improvements: **(1)** make undo *discoverable* in Telegram —
register a bot command menu (`setMyCommands`) and attach an inline "Undo" button to every log confirmation,
wired to the existing soft-delete capability; **(2)** let the user set, view, and clear each habit's daily
target from chat (`/target water 2000`), persisted in the DB across restarts and respected **everywhere a
goal is consulted** — the reminder goal-met skip, streaks, milestones, daily summary, weekly review,
charts, and the running-total percentages in confirmations. Success: the user can undo by tapping a button
and change a target by typing one message, and both survive a restart with zero config edits and no
breaking schema change.

## 2. Inputs

### 2.1 Inbound Telegram text (unchanged transport)
Plain `str` message text via `channels/telegram.py`'s long-poll loop, routed through
`main.handle_inbound_message(text, ...)`. New recognized command shapes (all LLM-free, anchored to the
whole stripped message, same conservatism as existing undo/edit — a false positive must never swallow a
real habit log):

**Deterministic, LLM-free path** (`commands.dispatch`, always works — even when Ollama is down):
```
/target water 2000            # set water's daily goal to 2000 (base unit = ml, from registry)
/target water 3 bottles       # set via a configured unit alias → 3 × 600 = 1800 ml
/target water                 # show water's current effective goal + config default
/target                       # show all habits' current effective goals
/target water default         # clear override → revert to config default   (also: reset | clear | ค่าเริ่มต้น)
ตั้งเป้าน้ำ 2000               # Thai anchored trigger — set water goal to 2000

/help                         # bilingual capability overview   (Thai: ช่วยเหลือ | วิธีใช้)
/habits                       # list every tracked habit + kind/unit/goal/today  (Thai: นิสัย)
```

**Full natural-language path** (OQ3 answered: free-form phrasing, LLM intent classification, fail-closed —
mirrors v0.8.0's `core/query.py`). These are classified by the LLM, not by anchored patterns:
```
from now on I want to drink 2.5L a day            # → set water goal to 2500 ml
let's aim for 3 liters of water each day          # → set water goal to 3000 ml
change my water target to 2 bottles a day         # → 2 × 600 = 1200 ml
from now on I want to stretch 20 minutes a day    # → set stretch goal to 20 min (introduces a goal, OQ2)
ต่อไปอยากดื่มน้ำวันละ 2.5 ลิตร                       # → set water goal to 2500 ml
ตั้งแต่นี้ขอตั้งเป้ายืดเส้นวันละ 20 นาที               # → set stretch goal to 20 min
```
A message that merely **logs** an amount ("I drank 2.5L", "500ml", "ดื่มน้ำ 2 แก้ว") must never be read as a
target change (§4 R-T14, AC32).

### 2.2 Inbound Telegram callback query (NEW)
Tapping the inline "Undo" button delivers a Telegram `callback_query` update (not a `message`). Relevant
fields the channel must extract:

```json
{
  "callback_query": {
    "id": "8471...",                         // must be echoed to answerCallbackQuery
    "data": "undo:1234",                     // "undo:<log_id>"  (<= 64 bytes)
    "message": { "text": "💧 500ml logged — 500/2500 ml today (20%)", "message_id": 42 },
    "from": { "id": 1574572064 }
  }
}
```

### 2.3 Existing habit registry (source of a habit's unit and goal-ability)
`HabitRegistry` (`core/habits.py`). A habit's base unit and unit aliases come from `Habit.unit_en/unit_th`
and `Habit.unit_aliases`. Only `type in ("numeric", "duration")` habits can carry a goal (config validator
`HabitConfig._unit_and_goal_match_type` forbids goals on `text`/`boolean`). Shipped config: `water`
(numeric, unit ml, goal 2500), `stretch` (duration, unit min, no goal), `diary` (text, no goal).

### 2.4 Target override store (NEW, DB-backed)
A single new table `habit_targets` (see §4 R-T1). Read live on every goal lookup; written by the target
command.

## 3. Outputs

### 3.1 Bot command menu (startup, once)
`setMyCommands` registers, at minimum, `/undo` and `/target` with bilingual descriptions (English default +
a Thai set via `language_code="th"`). They then appear in Telegram's "/" menu and the command hint UI.

### 3.2 Inline "Undo" button on every interactive log confirmation
Each successful log confirmation (water / stretch / diary / generic numeric / duration / boolean, plus the
Ollama-recovery re-confirmations) is sent with a one-row inline keyboard:

```
💧 500ml logged — 500/2500 ml today (20%)
[ ↩️ Undo ]         # callback_data = "undo:1234"
```

Button label resolves through i18n (`undo_button_label`: en "↩️ Undo" / th "↩️ ยกเลิก").

### 3.3 Undo-via-button result
Tapping soft-deletes **that specific entry** (`undo:<log_id>`), then sends a confirmation identical in shape
to the existing `/undo` confirmation (describes what was removed + the habit's recomputed today total), and
calls `answerCallbackQuery` to dismiss the button's spinner. Example (Thai reply if the original
confirmation was Thai):

```
↩️ ลบ "น้ำ 500 มล." แล้ว — เหลือ 0/2500 มล. วันนี้ (0%)
```

Idempotent: tapping a button whose entry is already deleted (re-tap, or after `/undo`) sends a friendly
"already removed" message and still answers the callback (`already_undone` catalog key).

### 3.4 Target command replies (bilingual)
```
# set:   /target water 2000
✅ Set water's daily goal to 2000 ml. (was 2500 ml)

# clear: /target water default
↩️ Reset water's daily goal to the default 2500 ml.

# show one: /target water
🎯 water: 2000 ml/day (default 2500 ml)          # "(default …)" only shown when an override is active

# show all: /target
🎯 Daily goals:
• water: 2000 ml/day (default 2500 ml)
• stretch: 20 min/day (default: none)
• diary: — (no goal)

# invalid habit:
🤔 "coffee" isn't a habit I track. I track: water, stretch, diary.

# not goal-able (text/boolean habit):
🤔 diary doesn't have a daily goal to set.

# invalid value:
🤔 A daily goal has to be a positive number, e.g. "/target water 2000".
```

### 3.5 Error responses
No new raised exceptions surface to the user. Every new branch either sends a friendly catalog message or
falls through to the existing parser (§4 R-C5). A malformed/unknown `callback_query.data` is answered
(spinner dismissed) and otherwise ignored (logged). A DB write failure in the target command is logged and
replied to with a generic "couldn't save that right now" message (`target_save_failed`), never a traceback.

### 3.6 Discoverability replies (NEW — deterministic, LLM-free)
`/help` → a concise, chat-friendly bilingual capability overview (language via `resolve_reply_language`):
```
🤖 Here's what I can do:
• Log habits — just type it: "500ml", "10 min stretch", "ดื่มน้ำ 2 แก้ว"
• Undo — tap ↩️ Undo under a confirmation, or send /undo
• Daily goals — /target water 2000, or say "from now on 2.5L a day"
• Ask about your data — "how much water this week?"
• Streaks & milestones at 3/7/30 days · daily recap 21:45 · weekly review Sun 20:00
• Snooze a reminder — /snooze 30 · quiet hours respected
Type /habits to see everything I track.
```

`/habits` → one line per registered habit (bilingual name, kind + unit, effective goal marked
default-vs-override, today's total):
```
📋 What I track:
• water / น้ำ — numeric (ml) · goal 2000 ml 🎯 (your target) · today 500 ml
• stretch / ยืดเส้น — duration (min) · no goal · today 2 sessions
• diary / ไดอารี่ — text · no goal · today 1 entry
```
(A habit whose goal comes from config shows "· goal 2500 ml (default)"; one with a DB override shows the
override value + "🎯 (your target)". The `/help` times/values shown are read from `config` — summary time,
weekly-review day/time, milestones, snooze default — not hard-coded.)

## 4. Behavior rules

### Feature 1 — Undo discoverability (`undo-ui`)

- **R-U1** At startup, `async_main` calls the channel's `set_my_commands(...)` once with (at least) `/undo`
  and `/target`, English default + Thai (`language_code="th"`). Failure to register (network error) is
  logged and never crashes startup (belt-and-suspenders `try/except`, same posture as the startup schema
  probe).
- **R-U2** Every **interactive** log confirmation (the ones sent in response to an inbound user log:
  water, stretch, diary, generic numeric/duration/boolean, and the recovery re-confirmations in
  `reparse_pending_unparsed`) is sent via `send_actionable(text, buttons=[(undo_label, "undo:<id>")])`,
  where `<id>` is the row id returned by `db.insert_log(...)` (or `reclassify_log`'s row id) for that
  entry. Unprompted sends (reminders, daily summary, weekly review, health alerts, clarifying question,
  deferred-ack) get **no** button.
- **R-U3** The milestone suffix (v0.10.0) remains appended to the confirmation *text*; the button is
  attached to the same single message.
- **R-U4** `TelegramChannel.run` handles `callback_query` updates in the same poll loop as messages:
  extract `id`, `data`, and `message.text`; invoke the caller's `on_callback(data, source_text, cb_id)`;
  then always call `answerCallbackQuery(cb_id)` (even on no-op/error) so the client spinner clears.
  `_offset` advances for callback updates exactly as for messages (no update is dropped or reprocessed).
- **R-U5** `on_callback` parses `data` as `undo:<int>`. On a valid id: resolve reply language from
  `source_text` (`i18n.detect_language`, overridden by a forced `config.i18n.language`); if the row exists
  and is not soft-deleted, soft-delete it and send the "removed + recomputed total" confirmation (same
  formatter the command-path `/undo` uses); if the row is missing or already deleted, send `already_undone`.
- **R-U6** `data` that doesn't match `undo:<int>` is logged and ignored (still answered per R-U4). No write.
- **R-U7** Non-Telegram channels (`line.py` stub, test fakes) are unaffected: `send_actionable` and
  `set_my_commands` are **concrete default methods on the `Channel` ABC** — `send_actionable` degrades to
  `self.send(text)` (dropping buttons), `set_my_commands` is a no-op — mirroring v1.0.0's `send_image`
  pattern, so no fake or subclass must implement them.
- **R-U8** The button-undo path and the existing text/command undo path share one confirmation formatter
  and one soft-delete call — they must produce byte-identical confirmation copy for the same removed row
  and language (no divergent second implementation).

### Feature 2 — Per-habit targets (`targets`)

- **R-T1** Migration **005** (additive only, preserving post-1.0 stability) creates:
  ```sql
  CREATE TABLE IF NOT EXISTS habit_targets (
    habit_id   TEXT PRIMARY KEY,
    goal       REAL NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
  );
  ```
  No `ALTER`/`DROP` on `logs`. Appended to `MIGRATIONS` after `_migration_004_habit_type`; re-running is a
  no-op (`IF NOT EXISTS`).
- **R-T2** New `Database` methods: `get_target(habit_id) -> float | None`, `set_target(habit_id, goal)`
  (upsert, `ON CONFLICT(habit_id) DO UPDATE`), `clear_target(habit_id)` (delete; no-op if absent),
  `all_targets() -> dict[str, float]`, and `get_log(log_id) -> sqlite3.Row | None` (needed by R-U5). All
  commit immediately (single-process, same as existing methods).
- **R-T3** A single authoritative goal resolver `core/targets.effective_goal(db, habit, config) -> float | None`
  returns: the override from `habit_targets` if one exists **for a goal-able habit**; else the habit's
  **config base goal** = `config.reminders.water.goal_ml` for `water` (legacy source, byte-identical to
  today) and `habit.goal` for every other habit. This **subsumes and replaces** `streaks.effective_goal`.
  An override may exist for **any** goal-able (`numeric`/`duration`) habit, including one whose config
  defines no goal (e.g. `stretch`) — see R-T5b.
- **R-T5b** (OQ2 semantics — a target on a previously goal-less habit) When a target is set on a goal-able
  habit that had **no** config goal (e.g. the duration habit `stretch`), that habit becomes **goal-bearing
  going forward**, and goal-met judgment applies to it:
  - `streaks.day_qualifies` / `compute_streak` / milestones switch from "any entry that day qualifies" to
    "that day's total (`db.sum_value`, e.g. total minutes) ≥ target";
  - the reminder **goal-met skip** (`_goal_already_met`) now applies (once the day's total ≥ target, the
    reminder is suppressed — subject to the habit's own `skip_if_goal_met`, default true);
  - the **daily summary** renders it as goal-bearing (`total/goal + %`, via the existing
    `daily_summary_numeric_goal` branch which already covers `type in (numeric, duration) and goal`);
  - log **confirmations** for that habit show the running % against the target.
  Clearing the target (R-T10 `clear`) reverts the habit to its prior "any entry qualifies" / no-goal
  semantics. **Scope boundary (v1.1):** the weekly-review narrative line and chart **visual type** for a
  duration habit are NOT restructured — the streak they surface is target-driven (via the unified
  `compute_streak`), but the review still renders it as a session count and the chart still draws count
  bars (numeric habits, including water, do get a target-aware goal line — AC22). Restructuring a duration
  habit's review line/chart to minutes-vs-goal is deferred (§10).
- **R-T4** `config_goal(habit, config) -> float | None` (in `core/targets`) returns just the config base
  (no DB read) — used to report the default in `/target` replies and to detect "override differs from
  default".
- **R-T5** Every existing goal-consumption site is switched to `targets.effective_goal(db, habit, config)`:
  streaks (`day_qualifies`, and once-per-operation in `compute_streak` — see R-T6), daily summary,
  weekly review (`_compute_habit_stats`), charts (`render_habit_chart`), the reminder goal-met skip
  (`reminders._goal_already_met`, which currently reads `habit.goal` directly), and **the running-total
  percentages in confirmations/undo/edit** in `main.py` (water branch currently reads
  `config.reminders.water.goal_ml`; generic-numeric branch reads `habit.goal`). After this change, with no
  override set, every one of these must produce byte-identical output to v1.0.0 (regression guard).
- **R-T6** `effective_goal` is resolved **once per habit per aggregation operation**, not per day: the
  backward streak walk (`compute_streak`, up to `_MAX_LOOKBACK_DAYS`) must not issue a `get_target` DB read
  on every iteration. (Resolve the goal once, then compare each day's `sum_value` against it.)
- **R-T7** `commands.dispatch` gains a fifth LLM-free kind `"target"`, checked **before** the `query`
  matcher and after `undo`/`edit`/`snooze`. It recognizes:
  (a) slash form `^/target(\s+<habit>(\s+<rest>)?)?$`;
  (b) conservative bilingual NL triggers — English `^set\s+<habit>\s+(goal|target)\s+to\s+<value>$` and
  `^change\s+<habit>('s)?\s+(goal|target)\s+to\s+<value>$`; Thai `^ตั้งเป้า\s*<habit-th-or-id>\s*<value>$`.
  A trigger whose tail doesn't cleanly resolve returns a `"target"` command flagged as a usage/help request
  (reply = `target_usage`), never a `None` fall-through that would reach the extractor — EXCEPT the bare
  ambiguous case must still never swallow a plain log (the NL triggers are anchored and cannot match a log
  like "500ml" or "ดื่มน้ำ 2 แก้ว"; verified against the existing adversarial corpus).
- **R-T8** `Command` (dataclass) gains fields to carry a target op: reuse `category` (habit id) and
  `value_num` (new goal, in base unit), plus a new `target_action: Literal["set","clear","show","show_all","usage"] | None`.
- **R-T9** Target value parsing reuses the registry unit lookup (`_build_unit_lookup`/`_resolve_unit`): a
  bare number is in the habit's **base unit**; a recognized alias multiplies (`3 bottles` → 1800). If a unit
  token is supplied that resolves to a **different** habit than the one named, the command is a usage error
  (`target_usage`). Value must be `> 0`, else `target_invalid_value`.
- **R-T10** `execute_target` (in `core/targets_command.py`) performs the op and returns the reply string:
  - `set`: habit must exist and be goal-able (`numeric`/`duration`); write `db.set_target(id, goal)`; reply
    `target_set` including the previous effective goal (`was … / none`). Setting a target on a
    `duration` habit that had no config goal is **allowed** — it introduces a goal (§9 OQ2).
  - `clear`: `db.clear_target(id)`; reply `target_cleared` naming the restored config default (or
    `target_cleared_nogoal` if the config base is `None`).
  - `show`: reply `target_show` with effective goal + config default (default line shown only when override
    ≠ default).
  - `show_all`: reply `target_show_all` — one line per registered habit (effective goal, unit, default),
    "no goal" for text/boolean.
  - unknown habit → `target_invalid_habit` (lists tracked ids); non-goal-able habit → `target_not_goalable`;
    `usage` → `target_usage`.
- **R-T11** Overrides persist across restarts (they live in the DB, read live by `effective_goal`) and take
  effect immediately for all subsequent reminders/streaks/summaries/reviews/charts/confirmations — no
  restart, no config edit. Clearing reverts to the config default (§9 OQ4 → default: yes).
- **R-T12** (deterministic path always works) The anchored `/target …` slash form and the anchored Thai
  `ตั้งเป้า <habit> <value>` / `เป้า <habit> <value>` form are handled LLM-free in `commands.dispatch`
  (kind `"target"`, R-T7). They work with zero LLM involvement, including while Ollama is DOWN.

- **R-T13** (full natural-language target-setting — OQ3) For an inbound message that is (a) not an anchored
  command and (b) not query-shaped, `main.py` runs an **NL target-intent step immediately before**
  `parse_message`, and **only on the Ollama-up path** (after the health-monitor deferral check, R-T16):
  1. A cheap, bilingual **gate** `target_nl.looks_like_target_phrasing(text)` decides whether to spend an
     LLM call. It matches forward-looking / daily-goal markers — EN: `goal`, `target`, `from now on`,
     `per day`, `a day`, `each day`, `every day`, `daily`, `aim for … a day`, `want to … a day`; TH: `เป้า`,
     `ต่อไป`, `ตั้งแต่นี้`, `วันละ`, `ต่อวัน`, `อยากได้วันละ`. This is a **cost optimization only** — safety
     comes from the fail-closed classifier (R-T14), not the gate. A genuine target phrasing that lacks any
     marker is still reachable via the deterministic `/target` path.
  2. If the gate matches, `target_nl.classify_target_intent(text, llm, registry, config)` returns a
     validated `TargetIntent(habit_id, goal_base_unit)` or `None`.
  3. A valid intent runs the **same** `execute_target` *set* path (R-T10), sends the `target_set` reply,
     and **returns immediately — no `logs` row is written**.
  4. `None` (gate miss, low confidence, or any classifier failure) → the message proceeds to the existing
     `parse_message` log path **unchanged** (R-C5).

- **R-T14** (fail-closed, mirrors `core/query.py`) `classify_target_intent` **never raises**. It returns
  `None` on: transport/HTTP error, unparseable JSON, an off-schema or `"unknown"` category, a habit that is
  not goal-able, `goal ≤ 0`, or a model-reported **confidence below `config.ollama.confidence_threshold`**
  (0.55 default — the same knob `core/parser.py` uses). The system prompt explicitly instructs the model to
  distinguish "**setting or changing a future daily goal**" from "**logging an amount already done**", and to
  answer category `"unknown"` (or low confidence) for a log or an ambiguous message. **A message that logs
  an amount must never become a target change** — every ambiguous/uncertain case collapses to `None` and is
  logged normally (AC32).

- **R-T15** (unit normalization by the LLM) The classifier's JSON schema returns the goal already expressed
  in the habit's **base unit** (`{category, goal, confidence}`); the model performs the conversion
  ("2.5L" → 2500 ml, "3 liters" → 3000 ml, "2 bottles a day" → 1200 ml). Free-form units beyond the
  registry's alias table are handled by this LLM normalization, not by `_resolve_unit`. Code validates
  `goal > 0` and `is_goalable(habit)` after normalization (R-T14).

- **R-T16** (LLM-outage behavior) If the health monitor reports Ollama **DOWN**, the NL target step is
  skipped entirely — no classification LLM call is made even if the gate matches. The message follows the
  existing deferral path (persisted as `category='unparsed'`, re-parsed on recovery as a **log**, never
  retroactively as a target change). The deterministic `/target` / `ตั้งเป้า` path (R-T12) still sets
  targets during an outage.

- **R-C5** (shared conservatism, unchanged contract) Any inbound text matching none of undo/edit/snooze/
  target/query/help/habits — and not classified as an NL target (R-T13) — falls through to the existing LLM
  parser exactly as before. Zero false positives on real logs.

### Feature 3 — Discoverability (`discoverability`)

- **R-D1** `commands.dispatch` gains two more LLM-free kinds, anchored to the whole stripped message and
  checked alongside the existing anchored commands (before the `query` matcher):
  `"help"` matches `^/help$`, `^ช่วยเหลือ$`, `^วิธีใช้$`; `"habits"` matches `^/habits$`, `^นิสัย$`.
  Both are deterministic and run in the command branch **before** the health-monitor deferral check, so they
  work with **Ollama down**. Neither can match a real habit log (R-C5 contract; verified against the
  existing adversarial corpus — AC40).
- **R-D2** `"help"` → `core/discoverability.build_help_text(config, lang) -> str`: a concise, chat-friendly
  bilingual overview, language via `i18n.resolve_reply_language(text, config)`. It must cover: how to log
  (free-text EN/TH examples), undo (inline button + `/undo`), targets (`/target` + natural-language
  phrasing), NL queries (v0.8), streaks/milestones (from `config.gamification.milestones`), the daily
  summary time (`config.gamification.daily_summary_time`, when `daily_summary` on) and weekly-review day/time
  (`config.weekly_review`), snooze default (`config.snooze.default_minutes`) and quiet hours. Time/number
  values are read from `config`, never hard-coded. All copy via `core/i18n.py` (EN+TH).
- **R-D3** `"habits"` → `core/discoverability.build_habits_overview(db, config, registry, clock, lang) -> str`:
  one line per habit in registry order, each with: bilingual label (`habit.label`), kind (`habit.type`) +
  unit (`habit.unit`, when present), the **effective goal** from `targets.effective_goal(db, habit, config)`
  marked as a **user target** when `db.get_target(habit.id) is not None`, else **default** (or "no goal"
  when the effective goal is `None`), and **today's total** — `db.sum_value` for numeric/duration,
  `db.count_true` for boolean, `db.count` for text (via `clock()` → today, `config.app.timezone` rules as
  elsewhere). Deterministic, read-only, LLM-free.
- **R-D4** `/help` and `/habits` are added to the `set_my_commands` registration (R-U1) alongside `/undo`
  and `/target`, in both the English default set and the Thai (`language_code="th"`) set.
- **R-D5** Purely additive: no schema change, no new dependency, no LLM call on either path. Every new
  string lives in `core/i18n.py` with both `en` and `th` variants.

## 5. Interfaces (signatures)

```python
# channels/base.py  (Channel ABC — concrete defaults, no fake/subclass must implement)
Button = tuple[str, str]  # (label, callback_data)

class Channel(ABC):
    async def send(self, text: str) -> None: ...
    async def run(self, on_message, on_callback=None) -> None: ...        # on_callback optional (back-compat)
    async def send_image(self, image: bytes, caption: str) -> None: ...   # existing
    async def send_actionable(self, text: str, buttons: list[Button]) -> None:
        """Default: drop buttons, send text only."""
        await self.send(text)
    async def set_my_commands(self, commands: dict[str, list[tuple[str, str]]]) -> None:
        """commands = {lang_code: [(command, description), ...]}. Default: no-op."""
        return None
    async def answer_callback_query(self, callback_id: str, text: str | None = None) -> None:
        """Default: no-op (only Telegram implements it)."""
        return None

# channels/telegram.py  (TelegramChannel overrides the four above)
def build_send_actionable_request(self, text, buttons) -> tuple[str, dict]: ...   # sendMessage + reply_markup
def build_set_my_commands_requests(self, commands) -> list[tuple[str, dict]]: ...  # one setMyCommands per lang
# run(): also branch on update.get("callback_query"); call on_callback(data, source_text, cb_id)
#        then answer_callback_query(cb_id). Callback handler `on_callback` type:
#        Callable[[str, str, str], Awaitable[None]]  # (data, source_message_text, callback_id)

# storage/migrations.py
def _migration_005_habit_targets(conn: sqlite3.Connection) -> None: ...
# appended to MIGRATIONS

# storage/db.py
def get_target(self, habit_id: str) -> float | None: ...
def set_target(self, habit_id: str, goal: float) -> None: ...
def clear_target(self, habit_id: str) -> None: ...
def all_targets(self) -> dict[str, float]: ...
def get_log(self, log_id: int) -> sqlite3.Row | None: ...

# core/targets.py  (NEW — shared surface)
def effective_goal(db: Database, habit: Habit, config: Config) -> float | None: ...
def config_goal(habit: Habit, config: Config) -> float | None: ...
def is_goalable(habit: Habit) -> bool:  # habit.type in ("numeric", "duration")
    ...

# core/commands.py  (extend)
CommandKind = Literal["undo", "edit", "query", "snooze", "target", "help", "habits"]
@dataclass(slots=True)
class Command:
    kind: CommandKind
    category: str | None = None
    value_num: float | None = None
    minutes: int | None = None
    target_action: Literal["set", "clear", "show", "show_all", "usage"] | None = None
def dispatch(text: str, registry: HabitRegistry) -> Command | None: ...   # now also emits "target"

# core/targets_command.py  (NEW — Module `targets`)
async def execute_target(
    command: Command, *, db: Database, config: Config, registry: HabitRegistry, lang: i18n.Language
) -> str: ...   # returns the bilingual reply text; performs the DB write

# core/target_nl.py  (NEW — Module `targets`; full-NL intent, mirrors core/query.py)
@dataclass(slots=True)
class TargetIntent:
    habit_id: str          # a real, goal-able configured habit id
    goal_base_unit: float  # already normalized to the habit's base unit, > 0
def looks_like_target_phrasing(text: str) -> bool: ...          # cheap bilingual gate (R-T13.1)
def build_target_intent_schema(category_enum: list[str]) -> dict: ...  # {category, goal, confidence}
async def classify_target_intent(                                # returns None on any failure (R-T14)
    text: str, llm: OllamaClient, registry: HabitRegistry, config: Config
) -> TargetIntent | None: ...

# llm/prompts.py  (extend — owned by Module `targets` this release)
def build_target_intent_system_prompt(registry: HabitRegistry) -> str: ...  # "set-a-future-goal vs log"
def build_target_intent_user_prompt(text: str) -> str: ...

# core/discoverability.py  (NEW — Module `discoverability`; deterministic, LLM-free)
def build_help_text(config: Config, lang: i18n.Language) -> str: ...
def build_habits_overview(
    db: Database, config: Config, registry: HabitRegistry, clock, lang: i18n.Language
) -> str: ...

# core/undo_ui.py  (NEW — Module `undo-ui`)
def undo_button(log_id: int, lang: i18n.Language) -> list[Button]:  # [(undo_button_label, f"undo:{log_id}")]
    ...
async def send_undo_confirmation(   # shared by button-callback AND command `/undo`
    db, channel, config, clock, registry, lang, row
) -> None: ...
async def handle_undo_callback(     # the on_callback body
    data: str, source_text: str, callback_id: str, *,
    db, channel, config, clock, registry
) -> None: ...
```

## 6. Files to touch

**Shared surface (built first, sequentially):**
- `src/habit_assistant/core/i18n.py` — add all new catalog keys for both features (one edit, disjoint keys).
- `src/habit_assistant/channels/base.py` — add `send_actionable`, `set_my_commands`, `answer_callback_query` concrete defaults; widen `run` signature with optional `on_callback`.
- `src/habit_assistant/channels/telegram.py` — override the four channel methods; handle `callback_query` in `run`.
- `src/habit_assistant/channels/line.py` — widen the stub `run` signature to match (still raises).
- `src/habit_assistant/storage/migrations.py` — migration 005.
- `src/habit_assistant/storage/db.py` — `get_target`/`set_target`/`clear_target`/`all_targets`/`get_log`.
- `src/habit_assistant/core/targets.py` — NEW: `effective_goal`/`config_goal`/`is_goalable`.
- `src/habit_assistant/core/streaks.py` — remove local `effective_goal`; call `targets.effective_goal(db, …)`; apply R-T6 (resolve once).
- `src/habit_assistant/core/review.py` — `_compute_habit_stats` uses `targets.effective_goal`.
- `src/habit_assistant/core/charts.py` — `render_habit_chart` uses `targets.effective_goal`.
- `src/habit_assistant/core/reminders.py` — `_goal_already_met` uses `targets.effective_goal(db, habit, config)`.
- `src/habit_assistant/main.py` — integration seams: capture `insert_log`/`reclassify_log` row ids and send interactive confirmations via `send_actionable` + `undo_ui.undo_button`; switch water/generic confirmation & undo/edit percentages to `targets.effective_goal`; register `set_my_commands` at startup; wire `on_callback=undo_ui.handle_undo_callback(...)` into `channel.run`; route `command.kind == "target"` to `targets_command.execute_target`; **add the full-NL target step (R-T13) between the health-monitor deferral check and `parse_message`** (gate → `classify_target_intent` → on a hit, `execute_target` + return, else fall through); make `_execute_undo` delegate to `undo_ui.send_undo_confirmation`.

**Module `undo-ui` (parallel):**
- `src/habit_assistant/core/undo_ui.py` — NEW: button spec + shared confirmation formatter + callback handler.
- `tests/test_undo_ui.py` — NEW.

**Module `targets` (parallel):**
- `src/habit_assistant/core/commands.py` — add `"target"` kind + anchored parsing + `target_action` field.
- `src/habit_assistant/core/targets_command.py` — NEW: `execute_target`.
- `src/habit_assistant/core/target_nl.py` — NEW: full-NL intent classifier (`looks_like_target_phrasing`, `classify_target_intent`), fail-closed, mirrors `core/query.py`.
- `src/habit_assistant/llm/prompts.py` — add `build_target_intent_system_prompt` / `build_target_intent_user_prompt` (only this module touches prompts.py this release).
- `tests/test_targets.py`, `tests/test_target_nl.py` — NEW.

**Module `discoverability` (SEQUENTIAL — lands after the v1.1 shared surface + integration; see §11):**
- `src/habit_assistant/core/discoverability.py` — NEW: `build_help_text`, `build_habits_overview`.
- `src/habit_assistant/core/commands.py` — add `"help"`/`"habits"` anchored kinds (edits the same file the `targets` module touched — hence sequential, after integration).
- `src/habit_assistant/core/i18n.py` — help + habits catalog templates (EN+TH).
- `src/habit_assistant/main.py` — route `command.kind in ("help","habits")` to the discoverability formatters; extend the `set_my_commands` registration to include `/help` and `/habits`.
- `tests/test_discoverability.py` — NEW.

**Docs/config:**
- `config.toml` — add a commented note documenting that `/target` overrides `[[habits]].goal` at runtime and is stored in the DB (no schema field needed).
- `tests/test_channels.py`, `tests/test_migrations.py` — extend for callback handling and migration 005.

## 7. External dependencies
None new. Telegram Bot API methods used are all already reachable via the existing `httpx` client:
`setMyCommands`, `answerCallbackQuery`, and `sendMessage` with a `reply_markup` inline keyboard are standard
Bot API and need no library. `matplotlib` remains the only optional dep (unchanged). SQLite via stdlib
`sqlite3` (unchanged). Python 3.11+ (unchanged).

## 8. Acceptance criteria

### Feature 1 — undo-ui
- **AC1**: Given the bot starts, When `async_main` runs, Then `set_my_commands` is called once with both `/undo` and `/target` present, in an English default set and a Thai (`language_code="th"`) set. (R-U1)
- **AC2**: Given `set_my_commands` raises a transport error at startup, When startup continues, Then the error is logged and the bot still enters the poll loop (no crash). (R-U1)
- **AC3**: Given a user logs "500ml", When the confirmation is sent, Then it goes out via `send_actionable` carrying exactly one inline button whose `callback_data == "undo:<the inserted row id>"` and whose label is the `undo_button_label` for the reply language. (R-U2)
- **AC4**: Given a reminder / daily summary / weekly review / clarifying-question / deferred-ack is sent, When it goes out, Then it carries **no** inline button. (R-U2)
- **AC5**: Given a milestone is crossed on a log, When the confirmation is sent, Then the milestone suffix is in the text AND the single message still carries the undo button. (R-U3)
- **AC6**: Given a `callback_query` update with `data="undo:1234"` arrives, When `TelegramChannel.run` processes it, Then `on_callback("undo:1234", source_text, cb_id)` is invoked and `answerCallbackQuery(cb_id)` is called afterward, and `_offset` advances past the update. (R-U4)
- **AC7**: Given the user taps Undo on entry 1234 (not yet deleted), When `handle_undo_callback` runs, Then row 1234 is soft-deleted and a "removed + recomputed today total" confirmation is sent in the language detected from the source confirmation text. (R-U5)
- **AC8**: Given the user taps Undo on an entry already soft-deleted (re-tap or after `/undo`), When `handle_undo_callback` runs, Then no second delete occurs, an `already_undone` message is sent, and the callback is still answered. (R-U5)
- **AC9**: Given a `callback_query` with malformed `data` (e.g. `"undo:abc"` or `"foo"`), When processed, Then no DB write occurs, it is logged, and `answerCallbackQuery` is still called. (R-U6)
- **AC10**: Given a `FakeChannel`/`LineChannel` that does not override `send_actionable`/`set_my_commands`, When the code calls them, Then `send_actionable` sends the text only (no error) and `set_my_commands` is a silent no-op. (R-U7)
- **AC11**: Given the same row and language, When removed via the inline button vs via `/undo`, Then the confirmation text is byte-identical. (R-U8)

### Feature 2 — targets
- **AC12**: Given migration state at user_version 4, When the DB opens, Then migration 005 creates `habit_targets` and stamps user_version 5; opening again applies nothing (idempotent), and `logs` is untouched. (R-T1)
- **AC13**: Given `/target water 2000`, When dispatched and executed, Then `habit_targets` has `(water, 2000)`, the reply is `target_set` naming the previous goal (2500), and `targets.effective_goal(db, water, config)` returns 2000. (R-T7/R-T8/R-T10)
- **AC14**: Given `/target water 3 bottles`, When executed, Then the stored goal is 1800 (3 × 600 alias). (R-T9)
- **AC15**: Given `/target water 0` or `/target water -5`, When dispatched, Then no write occurs and the reply is `target_invalid_value`. (R-T9)
- **AC16**: Given `/target coffee 2000` (unknown id), When dispatched, Then no write and the reply is `target_invalid_habit` listing the tracked ids. (R-T10)
- **AC17**: Given `/target diary 5` (text habit, not goal-able), When dispatched, Then no write and the reply is `target_not_goalable`. (R-T10)
- **AC18**: Given `/target water default` (also `reset`/`clear`/`ค่าเริ่มต้น`) with an active override, When executed, Then the override row is deleted and `effective_goal` returns the config default (2500), with reply `target_cleared`. (R-T10/R-T11)
- **AC19**: Given `/target water` with an active override of 2000, When executed, Then the reply is `target_show` showing 2000 ml/day and the default 2500. (R-T10)
- **AC20**: Given `/target` (no args), When executed, Then the reply is `target_show_all` with one line per registered habit (effective goal + unit, "no goal" for diary). (R-T10)
- **AC21**: Given a water override of 2000, When the water goal-met reminder skip runs (`reminders._goal_already_met`), Then it compares today's total against 2000 (not 2500). (R-T5)
- **AC22**: Given a water override of 1000 and a day totaling 1000 ml, When `streaks.day_qualifies` / `compute_streak` / daily summary / weekly review / chart goal-line run, Then that day qualifies against 1000 (streak counts it; summary/review %/goal-line use 1000). (R-T5)
- **AC23**: Given a water override of 2000, When the user logs "500ml", Then the confirmation percentage reads against 2000 (i.e. "500/2000 ml today (25%)"), and likewise for undo/edit recomputed totals. (R-T5)
- **AC24**: Given **no** override for any habit, When any goal-consuming path runs (reminders, streaks, milestones, daily summary, weekly review, charts, confirmations, undo, edit), Then output is byte-identical to v1.0.0. (R-T5 regression guard)
- **AC25**: Given a stored override, When the process restarts (new `Database`) and a reminder/summary/confirmation runs, Then the override is still in effect (persistence). (R-T11)
- **AC26**: Given `compute_streak` walking N days for a goal-able habit, When it runs, Then `get_target` is queried at most once for that call, not once per day. (R-T6)
- **AC27**: Given the anchored `/target water 2000` and `ตั้งเป้าน้ำ 2000`, When dispatched LLM-free, Then the same `"target"`/`set` command is produced (works with Ollama down); and given a plain log ("500ml", "ดื่มน้ำ 2 แก้ว") from the existing adversarial corpus, `dispatch` still returns non-target (no false positive). (R-T7/R-T12/R-C5)
- **AC28**: Given a `set_target` DB write fails, When `execute_target` runs, Then it is logged and the reply is `target_save_failed` (no traceback to the user). (R-T10/§3.5)

### Feature 2 — targets, full natural language (OQ3)
- **AC29**: Given "from now on I want to drink 2.5L a day" (EN free-form) and Ollama up, When the NL target step runs, Then `classify_target_intent` returns `(water, 2500)`, `execute_target` sets the target, **no `logs` row is written**, and the reply is `target_set`. (R-T13/R-T15)
- **AC30**: Given "ต่อไปอยากดื่มน้ำวันละ 2.5 ลิตร" (TH free-form), When the NL step runs, Then the water target is set to 2500 and no log is written. (R-T13/R-T15)
- **AC31**: Given "from now on I want to stretch 20 minutes a day" (stretch = duration, no config goal), When the NL step runs, Then stretch gets an effective goal of 20, and subsequently `day_qualifies`/`compute_streak`/`_goal_already_met`/the daily summary treat 20 min/day as the goal; clearing it reverts to "any entry qualifies". (R-T13/R-T5b)
- **AC32** (ambiguity fail-closed): Given "I drank 2.5L" or "500ml" (a log) — or any message the model returns with confidence below `config.ollama.confidence_threshold` — When the NL step runs, Then `classify_target_intent` returns `None`, **no `habit_targets` write occurs**, and the message is logged as a normal water entry. (R-T14)
- **AC33** (LLM outage): Given the health monitor reports Ollama DOWN and the user sends "from now on 2.5L a day", When handled, Then **no target-classification LLM call is made**, no target is set, and the message follows the existing deferral path (persisted `unparsed`, later re-parsed as a log); AND the deterministic `/target water 2500` still sets the target during the outage. (R-T16/R-T12)
- **AC34** (classifier robustness): Given the LLM returns malformed JSON, an `unknown`/unconfigured category, a non-goal-able habit, `goal ≤ 0`, or raises a transport error, When `classify_target_intent` runs, Then it returns `None` (never raises, no write) and the message falls through to the parser. (R-T14)

### Feature 3 — discoverability
- **AC35**: Given `/help` (also `ช่วยเหลือ` / `วิธีใช้`), When dispatched, Then `command.kind == "help"` LLM-free, and the reply is `build_help_text(config, lang)` in the reply language (`resolve_reply_language`); it succeeds with Ollama down. (R-D1/R-D2)
- **AC36**: Given the help reply, When rendered, Then it covers every required capability section (log with EN/TH examples, undo button + `/undo`, `/target` + NL targets, NL queries, streaks/milestones, daily-summary + weekly-review times, snooze + quiet hours), and its time/number values are read from `config` (e.g. changing `config.weekly_review.time` changes the shown time). (R-D2)
- **AC37**: Given `/habits` (also `นิสัย`), When dispatched, Then `command.kind == "habits"` LLM-free, and the reply lists **every** registered habit in registry order with bilingual name, kind + unit, effective goal, and today's total; it succeeds with Ollama down. (R-D1/R-D3)
- **AC38**: Given water has a DB target override of 2000 and stretch has none, When `/habits` runs, Then water's line shows 2000 marked as a user target and stretch's line shows its config state (default goal, or "no goal"); the goal shown comes from `targets.effective_goal` and the mark from `db.get_target`. (R-D3)
- **AC39**: Given a day with a 500 ml water log, When `/habits` runs, Then water's line shows "today 500 ml" (today's total via `db.sum_value` under `config.app.timezone`). (R-D3)
- **AC40**: Given `set_my_commands` runs at startup, Then `/help` and `/habits` appear alongside `/undo` and `/target` in both the English and Thai command sets; AND given a plain log ("500ml", "ดื่มน้ำ 2 แก้ว") from the adversarial corpus, `dispatch` never returns `"help"`/`"habits"`. (R-D4/R-D1)

## 9. Resolved decisions & remaining risks

**Answered by the user (2026-08-21) — now baked into the spec:**
- **OQ1 — Button + menu (RESOLVED: both).** An inline "↩️ Undo" button under every interactive log
  confirmation **and** `/undo` in the bot command menu. Reflected in R-U1/R-U2 and AC1–AC5/AC11.
- **OQ2 — Targets on goal-less habits (RESOLVED: allow, with semantics).** A target may be set on any
  goal-able (`numeric`/`duration`) habit, including one with no config goal (e.g. `stretch`). Once a target
  exists, **goal-met judgment applies to that habit going forward** — streaks/milestones/reminder-skip/daily
  summary/confirmations all switch to "day total ≥ target"; clearing reverts to "any entry qualifies". Full
  semantics in R-T5b; verified by AC31. The v1.1 scope boundary (weekly-review line / chart visual type for
  a duration habit not restructured) is stated in R-T5b and §10.
- **OQ3 — Full natural language (RESOLVED: yes, fail-closed LLM intent).** Free-form phrasing sets targets
  via an LLM intent classifier that **extends the v0.8.0 `core/query.py` pattern** — a new `core/target_nl.py`
  classifier, fail-closed: unsure/low-confidence/unavailable → the message falls through to normal
  logging/parsing. Anchored `/target` and `ตั้งเป้า` remain the deterministic path that always works. Full
  rules in R-T12–R-T16; verified by AC29–AC34.
- **OQ4 — Clear target back to config default (RESOLVED: yes).** `/target … default|reset|clear`
  (R-T10/R-T11).

**Discoverability feature — defaults chosen (no load-bearing open question):**
- **Thai aliases:** `/help` ← `ช่วยเหลือ` / `วิธีใช้`; `/habits` ← `นิสัย`. Chosen for naturalness; trivially
  extendable if the user prefers others. (R-D1)
- **`/habits` shows today's total** per habit (not a full history) — a quick "where am I now" glance,
  consistent with the daily-summary aggregations. (R-D3)
- **`/help` is a single concise message**, not paginated; values (times, milestones, snooze) are pulled live
  from `config` so the help never drifts from actual behavior. (R-D2)

**Remaining risks:**
- **Risk — Goal-source unification touches many files.** `effective_goal` currently has three de-facto
  sources (`config.reminders.water.goal_ml`, `habit.goal`, `streaks.effective_goal`). R-T5 consolidates all
  onto `targets.effective_goal`. Mitigation: AC24 is a hard byte-identical regression guard against the 701
  existing tests with no override present.
- **Risk — Two LLM calls on the log path.** The NL target step runs an LLM classification *before*
  `parse_message`. To avoid doubling latency on the common "just logging" case, R-T13's cheap
  `looks_like_target_phrasing` gate skips the classifier for messages with no daily-goal marker (the vast
  majority of logs). Safety is independent of the gate — the classifier fail-closes (R-T14) even if a log
  slips through the gate. Residual: a target-phrased message still costs classify + (on a miss) parse; and a
  genuine target phrasing lacking any gate marker needs the deterministic `/target`. Acceptable for a
  low-volume personal bot; the gate marker set is tunable.
- **Risk — LLM does the unit conversion (R-T15).** "2.5L → 2500 ml" relies on the model's arithmetic.
  Mitigation: strict post-validation (`goal > 0`, goal-able habit) and the `confidence_threshold` gate; a
  wrong conversion is at worst a wrong-but-plausible target the user can re-issue or `/target`-override.
  A sanity upper bound on goals is **out of scope** (§10) — flag to the user if desired.
- **Risk — Ambiguity (log vs target).** The single most important safety property (AC32): a logging message
  must never be recorded as a target change. Enforced by the fail-closed classifier + confidence threshold +
  an explicit "distinguish set-a-future-goal from log-an-amount" system prompt. Vera must test the
  adversarial corpus (existing logs) against the classifier with a mocked "unknown"/low-confidence response.
- **Risk — Callback language.** Undo-via-button infers reply language from the source confirmation text.
  If the original confirmation was Thai, the undo reply is Thai. A forced `config.i18n.language` still wins.
  Edge case: an empty/absent `source_text` → `detect_language` returns "en"; acceptable (rare).

## 10. Out of scope
- Editing values via inline buttons (only Undo gets a button; `/edit` stays text-only).
- **NL target-setting for anything but *set*** — clearing/showing a target via free-form NL is out; use
  `/target … default` / `/target …`. The LLM classifier only produces a *set* intent.
- Restructuring a **duration** habit's weekly-review narrative line or chart to a minutes-vs-goal visual
  once it has a target (its streak is already target-driven; see R-T5b scope boundary).
- A sanity **upper bound** on target values (R-T15 relies on the LLM's unit conversion + `> 0` validation).
- Per-time-of-day or per-weekday targets; targets are a single daily goal per habit.
- Changing a habit's **type**, unit, label, or reminder schedule from chat (config-only, unchanged).
- Undo of anything other than a single log row (no bulk undo, no redo of a cleared target).
- `editMessageReplyMarkup` to strip the button after undo (nice-to-have; the spinner is dismissed via
  `answerCallbackQuery`, which satisfies the UX requirement). May be added later.
- Migrating away the legacy `config.reminders.water.goal_ml` source (kept as the water config base for
  byte-identical behavior).

## 11. Module split & parallel development

**Total functionals:** 10 — (1) bot command menu, (2) inline undo button + callback handling, (3) target
set (anchored `/target`), (4) target clear/show, (5) effective-goal unification across all consumers,
(6) target store/migration, (7) **full-NL target-intent classification (fail-closed LLM)**, (8) OQ2
goal-less-habit target semantics, (9) `/help` capability overview, (10) `/habits` registry listing. Above
the 5-functional threshold.

**Recommendation:** **PARALLEL for the two v1.1 core features, then a SEQUENTIAL `discoverability`
follow-on.** The two core features (`undo-ui`, `targets`) are genuinely different subsystems (Telegram UI
plumbing vs. goal data model) with disjoint core logic, coupled only through a shared surface (i18n catalog,
channel API, target store + goal-resolver unification, and the `main.py` integration seams) built once,
sequentially, first. The **`discoverability` module lands last, sequentially, after that integration
completes** — it edits `core/commands.py`, `core/i18n.py`, and `main.py` routing/`set_my_commands` (the same
files the integration stabilizes), so it is **not parallel-safe** with the core work. It depends only on
already-built shared pieces (`targets.effective_goal`, `db.get_target`, the `set_my_commands` registration,
the command-dispatch seam), so once integration is green it is a small, self-contained pass.

**Shared surface (built first, sequentially, before the parallel modules start):**
- i18n catalog keys for both features (`core/i18n.py`).
- Channel API: `send_actionable`, `set_my_commands`, `answer_callback_query`, `callback_query` handling in
  `run` (`channels/base.py`, `channels/telegram.py`, `channels/line.py`).
- Target store + goal resolver: migration 005, `Database.get_target/set_target/clear_target/all_targets/get_log`,
  `core/targets.py` (`effective_goal`/`config_goal`/`is_goalable`).
- Effective-goal unification: switch `core/streaks.py`, `core/review.py`, `core/charts.py`,
  `core/reminders.py` onto `targets.effective_goal` (apply R-T6). Guarded by AC24 (byte-identical with no
  override).
- `main.py` integration seams (capture insert ids → `send_actionable` + `undo_ui.undo_button`; startup
  `set_my_commands`; wire `on_callback`; route `"target"` kind; **add the full-NL target step between the
  deferral check and `parse_message`**; switch confirmation/undo/edit percentages to `effective_goal`).
  These call into the two modules' interfaces (§5), so they land at the integration step once both modules
  report done.

| Module | Owned ACs | Owned files | Depends on |
|---|---|---|---|
| `undo-ui` | AC1, AC2, AC5, AC7, AC8, AC9, AC11 | `core/undo_ui.py`, `tests/test_undo_ui.py` | shared: channel API, `db.get_log`/`soft_delete`, `targets.effective_goal` (for recomputed totals), i18n keys |
| `targets` | AC13, AC14, AC15, AC16, AC17, AC18, AC19, AC20, AC27, AC28, AC29, AC30, AC32, AC34 | `core/commands.py`, `core/targets_command.py`, `core/target_nl.py`, `llm/prompts.py`, `tests/test_targets.py`, `tests/test_target_nl.py` | shared: `core/targets.py`, `db.*_target`, i18n keys, registry unit lookup, `OllamaClient.chat_json` |
| `discoverability` (sequential, after integration) | AC35, AC36, AC37, AC38, AC39, AC40 | `core/discoverability.py`, `core/commands.py` (help/habits kinds), `core/i18n.py` (help/habits copy), `main.py` (routing + set_my_commands ext.), `tests/test_discoverability.py` | integration complete: `targets.effective_goal`, `db.get_target`, `set_my_commands`, command-dispatch seam |

The full-NL classifier (`core/target_nl.py`) lives **inside the `targets` module** — it is a self-contained,
LLM-mockable unit exactly like `core/query.py`, testable without `main.py`. Only its **routing** (the gate
placement before `parse_message`, and the Ollama-up/down guard) is a `main.py` integration seam.

ACs verified during the shared-surface/integration step (not owned by a single parallel module): **AC3,
AC4, AC6** (channel + main.py wiring), **AC10, AC12** (channel defaults + migration — shared build),
**AC21, AC22, AC23, AC24, AC25, AC26** (effective-goal unification across consumers), **AC31** (goal-less
target semantics across consumers), **AC33** (NL-target outage routing + deferral in `main.py`). Assign
these to the Vera pass covering the shared surface and integration.

Every AC belongs to exactly one owner (parallel module, shared/integration, or the sequential
`discoverability` follow-on). No AC is owned by two parallel modules. **Total: 40 acceptance criteria.**

**Integration order (after parallel modules complete):**
1. Land `main.py` seams calling `undo_ui.*` and `targets_command.execute_target`; make `_execute_undo`
   delegate to `undo_ui.send_undo_confirmation`.
2. Run the full suite; verify AC24 byte-identical regression against the 701 existing tests (no override
   present) — this is the highest-value gate.
3. Integration tests: end-to-end inbound-log → confirmation-with-button → `callback_query` → soft-delete →
   re-confirmation; `/target water 2000` → subsequent reminder-skip/streak/summary/confirmation all
   reflecting 2000; full-NL "from now on 2.5L a day" → target set + **no log written** (AC29), and the same
   under Ollama-down → deferred as a log, no target (AC33); goal-less-habit target semantics (AC31); and
   restart-persistence (AC25).
4. **After integration is green, run the sequential `discoverability` pass** (`/help` + `/habits` +
   `set_my_commands` extension). One Luna+Vera pair; AC35–AC40. It only adds to `core/commands.py`,
   `core/i18n.py`, and `main.py` routing that are already stabilized, so it cannot conflict with the core
   work once integration has landed.
```
