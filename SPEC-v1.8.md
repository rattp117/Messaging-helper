# Spec — v1.8.0: One-tap quick-log keyboard + reactions, routines, backfill, gentle riders

> Builds on the **v1.7 TARGET state** (SPEC-v1.7.md): the registry is per-user
> (`RegistryProvider.for_user`), custom habits exist, and every consumer resolves
> the acting user's own registry. This spec treats that state as done; the current
> uncommitted diff is read-only reference.

## 1. Problem statement
Make logging effortless and the bot feel alive, while modelling how people actually
live. Five things ship together: **(1)** a one-tap quick-log inline keyboard whose
buttons are generated from each user's per-user registry (custom habits included),
handled via the existing `callback_query` plumbing; **(2)** instant emoji reactions
on the user's typed log message (Bot API 7.0 `setMessageReaction`), fail-open;
**(3)** routines/habit-stacks — a named bundle logged by one command or one tap;
**(4)** backfill — retroactive logging ("500ml yesterday") that every downstream
aggregation reflects correctly; and **(5)** gentle riders — silent proactive sends,
owner-scoped command menus, and a `/audit` language-preference fix. Success: each
feature is Telegram-native, mostly zero-LLM (only NL backfill phrases may fall
through to the LLM), gentle, bilingual TH/EN, strictly per-user isolated, and
**inert until invoked** — pre-v1.8 behavior stays byte-identical (AC-9).

SemVer: **1.8.0 (MINOR)** — additive features; the one deliberate behavior change is
silent-by-default proactive sends (a gentle default, config-reversible, AC-D1/D4).

## 2. Inputs

### 2.1 Quick-log keyboard
```
/log                      # (Thai alias: บันทึก)  -> pops the inline keyboard
```
Buttons are generated from `provider.for_user(chat_id)` (base + the user's active
custom habits). Callback payloads (Telegram limit 64 bytes; habit ids ≤ 32 per
v1.7 R-V1, so this always fits):
```
log:<habit_id>:<value_base_unit>     # e.g. "log:water:500", "log:pushups:20"
```

### 2.2 Reactions (no user input — automatic)
Fired on the **inbound message_id** of a successful *typed* log. Emoji from a
habit→emoji map. Requires capturing `message_id` (the loop currently keeps only
`chat_id`+`text`).

### 2.3 Routines
```
/routine morning = water 500, stretch 10, meditate 10   # create  (Thai: กิจวัตร)
/routine                                                # list (with run-buttons)
/routine morning                                        # run
/routine delete morning                                 # delete  (Thai tail: ลบ)
```
Item value tokens (`water 500`, `stretch 10`) parse through the **per-user** unit
lookup (`core/units.build_unit_lookup`), same machinery as `/target`. Run-button
callback payload:
```
routine:run:<name>        # name normalized/length-capped so the 64-byte limit holds
```

### 2.4 Backfill (relative-date modifier on an ordinary log)
A leading or trailing date phrase on any log, deterministic (EN + TH), bounded to
`[today - max_days_back, today)`:
```
"500ml yesterday"      "stretched 20 min on Monday"     "diary 2 days ago: ..."
"เมื่อวาน ดื่มน้ำ 500"   "ยืดเส้น 20 นาที วันจันทร์"          "3 วันที่แล้ว 500ml"
```
Recognized EN: `yesterday`, `N days ago`, weekday name (`(on|last) <weekday>`).
Recognized TH: `เมื่อวาน[นี้]`, `N วันที่แล้ว` / `N วันก่อน`, `วัน<จันทร์..อาทิตย์>`.
The LLM extraction path may additionally return an optional integer `date_offset`
(days back), honored only when present and within bounds.

### 2.5 Config (new keys, all defaulted — `config.toml` + `config.py`)
```
[notifications]  silent_proactive = true       # reminders/check-ins/nudges sent silently
[quicklog]       enabled = true                 # /log keyboard on
                 max_buttons_per_habit = 3
[reactions]      enabled = true                 # emoji reactions on typed logs
[backfill]       max_days_back = 14
[routines]       max_per_user = 20
```

## 3. Outputs

### 3.1 Quick-log
- `/log` → an inline keyboard (one send). Per goal-bearing numeric/duration habit:
  up to `max_buttons_per_habit` amount buttons, label `"<emoji> <amount><unit>"`.
  Boolean habit → one "done ✓" button (`log:<id>:1`). Text habits are omitted
  (a tap can't carry free text). Empty registry of loggable habits → a friendly
  "nothing to quick-log yet — try /addhabit" reply.
- A tap → the **exact** normal log confirmation (with the undo button) + dashboard
  refresh, identical to typing the same value.

### 3.2 Reaction
No text output; the bot sets one emoji reaction on the user's typed log message.
Silent failure (fail-open) if `setMessageReaction` errors.

### 3.3 Routines (bilingual)
```
# create
✅ Saved routine "morning": 💧500 ml + 🧘10 min stretch + 🧠10 min meditate. Run it with /routine morning.
# run (one compact summary, not N confirmations)
▶️ morning — logged 💧500 ml, 🧘10 min, 🧠10 min (3 of 3). 🗑️ skipped: none.
# run with a since-archived item
▶️ morning — logged 💧500 ml, 🧘10 min (2 of 3). Skipped: meditate (removed).
# delete
🗑️ Deleted routine "morning".
# validation error
🤔 Couldn't save that: "coffee" isn't one of your habits. Use /habits to see them.
```

### 3.4 Backfill confirmation
The normal per-habit confirmation, **prefixed** with the resolved date so the user
sees where it landed, and with **no** milestone/record celebration line:
```
📅 Logged for Mon 18 Aug — 💧 500 ml.   (today's totals unchanged)
```

## 4. Behavior rules

### Shared surface (channel + plumbing + skeletons)
- **R-S1** (silent send) `Channel.send` gains `disable_notification: bool = False`;
  the Telegram override adds `"disable_notification": true` to the `sendMessage`
  payload only when `True`. Default `False` → payload byte-identical to v1.7
  (AC-1). No other send method changes.
- **R-S2** (reaction method) `Channel.set_message_reaction(chat_id, message_id, emoji)`
  — concrete **default no-op** on the ABC (mirrors `send_image`/`send_and_pin`
  degradation, so LINE stub + every test fake are unaffected). `TelegramChannel`
  overrides it via `setMessageReaction` and **never raises** (transport error
  logged + swallowed, fail-open) (AC-2).
- **R-S3** (scoped menu) `Channel.set_my_commands(commands, *, scope_chat_id: str | None = None)`
  — `None` sends the default (global) menu, byte-identical to v1.7; a non-`None`
  value adds `"scope": {"type": "chat", "chat_id": scope_chat_id}` so a chat-scoped
  menu can be registered (AC-3). Additive keyword, defaulted — every existing
  caller is unaffected.
- **R-S4** (inbound message_id) `TelegramChannel.run` extracts `message.message_id`
  (as `str`) and passes it to `on_message` as a trailing defaulted arg; `on_message`
  threads it into `handle_inbound_message(..., inbound_message_id: str | None = None)`.
  Trailing + defaulted → every pre-v1.8 caller/fake (2- or 3-arg `on_message`,
  `--dry-run`, tests) is unaffected (AC-4). Callback-query updates carry no
  loggable inbound message → `None`.
- **R-S5** (command skeleton) `CommandKind` gains `"log"` and `"routine"`;
  `reserved_trigger_words()` gains `log`, `บันทึก`, `routine`, `กิจวัตร` (built from
  the same literals the new matchers will use — R-V3 discipline), so a custom habit
  named after either is rejected by `habitdef` (AC-8).
- **R-S6** (audit vocab) `core/audit.ACTIONS` gains `routine_create` / `routine_delete`
  / `routine_run`; `audit_view` gains their localized labels (AC-6). Quick-log and
  backfill produce ordinary `logs` rows and are **not** audited (logging has never
  been audited in this codebase — only mutations like undo/edit/target/remind are).
- **R-S7** (i18n + release) all new copy through `core/i18n.py` (EN+TH);
  `RELEASE_NOTES["1.8.0"]` (EN+TH) ships for the announce step (AC-7).

### Feature — quick-log keyboard + reactions (module `quicklog`)
- **R-Q1** (keyboard from per-user registry) `/log` (+ `บันทึก`) builds the keyboard
  from `provider.for_user(chat_id)`. For each **goal-bearing or aliased**
  numeric/duration habit: amount buttons = the sorted-unique base-unit multipliers
  of its `unit_aliases`, capped to `max_buttons_per_habit`; if it has no aliases but
  has an effective goal `G`, a derived ladder `[round¼G, round½G, G]`; if neither,
  the habit is skipped. Boolean habit → one done button. Text habit → skipped. So a
  user who defined "pushups | alias=set:10" gets `[💪10]`; water gets its glass/bottle
  amounts (AC-A1). Deterministic, zero-LLM.
- **R-Q2** (`log:` callback) a tap on `log:<habit>:<value>` resolves the habit
  against the **tapping user's** per-user registry, builds a `LogEntry`
  (`log_entry_from_result`-equivalent for the parsed value), inserts it, and sends
  the **same** confirmation the typed path sends (undo button included) + refreshes
  the dashboard (AC-A2). Reuses the shared confirmation path — no second confirmation
  formatter.
- **R-Q3** (ownership + safety) the `on_callback` access gate (active/owner) already
  runs first (v1.2 R-A1). A `log:` payload is then handled only against the tapping
  user's registry: a `habit_id` **not** in that registry (e.g. another user's custom
  habit) → a friendly no-op, no write; malformed / out-of-range payload → logged and
  ignored, no read/write (mirrors `undo_ui` ownership + bounds discipline) (AC-A3).
- **R-Q4** (reaction on typed log, fail-open) after a **successful typed-message**
  log confirmation, and only when `inbound_message_id is not None` and
  `[reactions] enabled`, call `reactions.react(channel, chat_id, message_id, habit)`.
  Emoji from `REACTION_EMOJI` (base ids: water→💧, stretch→💪, diary→✅; a small
  type map for the rest; **✅ ultimate fallback** for any custom habit). The call is
  wrapped fail-open — a reaction failure never affects the log or its confirmation
  (AC-A4).
- **R-Q5** (reaction scope) reactions fire **only** for typed inbound-message logs.
  Quick-log button taps (the tap targets the bot's keyboard message, not a user log),
  undo, commands, clarifying questions, and deferred/unparsed acks get **no** reaction
  (AC-A5).
- **R-Q6** (bilingual, zero-LLM) keyboard labels and confirmations follow the user's
  resolved language; the whole quick-log + reaction path makes no Ollama call (AC-A6).

### Feature — routines / habit stacks (module `routines`)
- **R-R1** (create) `/routine <name> = <habit> <val>[, <habit> <val> ...]` validates:
  name normalized (trim, lowercase, `≤ 32`, `^[a-z0-9_]+$`), not already used by this
  user, `≥ 1` item, each habit token resolves against the user's registry, each value
  parses via the per-user unit lookup; per-user cap `[routines] max_per_user`.
  On success: insert the routine + ordered items, record `routine_create` (fail-open),
  confirm bilingually. Any failure → a friendly error, **no write** (AC-B1).
- **R-R2** (list) `/routine` (bare) lists the user's routines, each with its items and
  an inline **run-button** (`routine:run:<name>`); bilingual; per-user (AC-B2).
- **R-R3** (run) `/routine <name>` or the run-button logs every **valid** item for
  **today** for the acting/tapping user: for each item build+insert a `LogEntry`
  (numeric/duration → value; boolean → true; text item → skipped, can't carry
  free text), skipping items whose habit is no longer active (archived/deleted) and
  noting them; then send **one** compact summary, refresh the dashboard **once**, and
  record `routine_run` (fail-open). Milestone/record celebration lines are
  **suppressed** (kept compact + deterministic), but `records.update_on_log` is still
  called per item and its return discarded, so stored records stay accurate. A run of
  an all-invalid routine → a "nothing to log" summary, no dashboard churn (AC-B3).
- **R-R4** (delete) `/routine delete <name>` (Thai: `/routine <name> ลบ` or `กิจวัตร ...`)
  removes the routine + items, records `routine_delete`, confirms. Non-existent name
  → friendly no-op, no write (AC-B4).
- **R-R5** (isolation) routines are strictly per-user; a `routine:run:<name>` callback
  runs only a routine **owned by the tapping chat** — a name not owned by that chat is
  a friendly no-op (callback ownership, mirrors undo) (AC-B5).
- **R-R6** (storage) migration **011** adds `routines` + `routine_items` (additive,
  idempotent, stamps 11, touches no existing data) (AC-B6). Zero-LLM throughout (AC-B7).

### Feature — backfill / retroactive logging (module `backfill`)
- **R-B1** (deterministic date parse, EN+TH) `backfill.extract_date(text, clock)` finds
  a **leading or trailing** recognized date phrase (§2.4), returns
  `(residual_text, target_date)` or `None`. Anchored to whole leading/trailing
  clause tokens (not substring) and gated on the fixed word-lists / weekday names —
  zero false positives on an ordinary log (AC-C5). Zero-LLM.
- **R-B2** (compose with existing extraction) when a date phrase is found, the
  **residual** text is run through the normal path (`preparse.deterministic_parse`
  first, then the LLM as usual) to get the habit+value; the resulting `LogEntry` is
  inserted with `ts` = the target date at a fixed local time-of-day (noon), so it
  attributes to that day. If the residual doesn't resolve to a habit → the normal
  clarifying question (no backfill write) (AC-C1).
- **R-B3** (aggregations reflect it) because every aggregation filters by the date
  prefix of `ts` (`ts LIKE 'YYYY-MM-DD%'` / `ts BETWEEN`), a backdated row is counted
  by streaks, records, trends, heatmap, `/history`, and the daily summary for its
  resolved date — **not** for today (AC-C2). No aggregation code changes.
- **R-B4** (stay quiet, no retro-celebration) a backfill emits **no** milestone or
  personal-record celebration line and does **not** edit today's live dashboard
  (unless the resolved date == today, which is the normal path). Stored records are
  still recomputed silently so they stay accurate (AC-C3).
- **R-B5** (bounds) a **future** date → friendly error, no write; a date older than
  `max_days_back` → friendly error, no write; a date **== today** → falls straight
  through to the normal (non-backfill) log path unchanged (AC-C4). The optional LLM
  `date_offset` is subject to the same bounds.
- **R-B6** (undo) a backfilled log carries a working **undo button** (by row id, so it
  works even though the row is not the newest by `ts`); undoing removes exactly that
  row (AC-C6).

### Riders (module `riders`)
- **R-D1** (silent proactive) when `[notifications] silent_proactive` (default **true**),
  the three proactive send sites — `reminders.send_reminder`, `checkins.run_due_checkins`,
  `nudge.run_due_nudges` — send with `disable_notification=True`. User-initiated
  confirmations/replies and the one-time dashboard pin stay notifying (AC-D1).
- **R-D2** (owner-scoped menu) startup registers **two** menus: the public menu at
  default scope (the v1.7 public set + `/log` + `/routine`), and an **owner** menu at
  `scope_chat_id = owner_chat_id` that additionally lists the owner-only commands
  (`/invite`, `/approve`, `/block`, `/users`, `/audit`). A non-owner sees only the
  public menu. A transport error never crashes startup (belt-and-suspenders, as today)
  (AC-D2).
- **R-D3** (`/audit` language fix) `on_message` resolves the acting user's stored
  `/lang` preference (`_stored_language_pref`) into its `lang` **before** the `/audit`
  interception, so the owner's audit view (header, action labels, actor "you", footer)
  renders fully in their chosen language. Pre-existing bug: `on_message` built `lang`
  without the stored preference, so `/audit` (intercepted there) followed only the
  input-text/config language (AC-D3).
- **R-D4** (bounded delta) with `silent_proactive = false`, proactive send payloads are
  byte-identical to v1.7; the `/audit` fix changes only language resolution, not row
  content or order (AC-D4).

### Regression gate
- **R-G** (inert until invoked) with no v1.8 feature invoked — no `/log`/`log:` tap,
  no `/routine`, no recognized backfill phrase, `[reactions]`/`[quicklog]` at default
  but not exercised — every typed-log confirmation, command reply, and extraction
  result is byte-identical to v1.7, and the full v1.7 suite stays green. The three
  proactive sends' only delta is the intended `disable_notification` flag (R-D1),
  which its own tests own (AC-9).

## 5. Interfaces (signatures)
```python
# channels/base.py  (shared surface)
async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None: ...
async def set_message_reaction(self, chat_id: str, message_id: str, emoji: str) -> None: ...   # default no-op, never raises
async def set_my_commands(self, commands: dict[str, list[tuple[str, str]]], *, scope_chat_id: str | None = None) -> None: ...

# channels/telegram.py  (shared surface)
def build_set_message_reaction_request(self, chat_id, message_id, emoji) -> tuple[str, dict]: ...
# run(): on_message(chat_id, text, display_name, message_id)   # message_id trailing/defaulted at the call

# main.py  (shared surface — plumbing; feature wiring is integration)
async def handle_inbound_message(..., inbound_message_id: str | None = None) -> None: ...

# core/commands.py  (shared skeleton; matchers are module-owned, disjoint regions)
CommandKind = Literal[..., "log", "routine"]
def _match_log(stripped: str) -> "Command | None": ...          # module quicklog
def _match_routine(stripped: str, registry) -> "Command | None": ...  # module routines

# core/quicklog.py  (NEW — module quicklog)
def build_keyboard(registry, config, db, lang, user_id) -> list[Button]: ...
async def handle_log_callback(chat_id, data, source_text, callback_id, *, db, channel, config, registry, clock) -> None: ...

# core/reactions.py  (NEW — module quicklog)
REACTION_EMOJI: dict[str, str]     # base ids + type fallback; "✅" ultimate default
async def react(channel, chat_id, message_id: str, habit) -> None: ...   # fail-open

# core/routines.py  (NEW — module routines)
async def execute_routine(command, *, db, channel, config, provider, lang, user_id, clock) -> str | None: ...
async def handle_routine_callback(chat_id, data, source_text, callback_id, *, db, channel, config, provider, clock) -> None: ...

# storage/migrations.py + storage/db.py  (module routines)
def _migration_011_routines(conn) -> None: ...
#   routines(user_id TEXT, name TEXT, created_at TEXT, PRIMARY KEY(user_id, name))
#   routine_items(user_id TEXT, name TEXT, seq INT, habit_id TEXT, value REAL, PRIMARY KEY(user_id, name, seq))
def add_routine(self, user_id, name, items: list[tuple[str, float | None]]) -> None: ...
def list_routines(self, user_id) -> list[...]: ...
def get_routine(self, user_id, name) -> ... | None: ...
def delete_routine(self, user_id, name) -> bool: ...
def count_routines(self, user_id) -> int: ...

# core/backfill.py  (NEW — module backfill; pure logic)
def extract_date(text: str, clock, *, max_days_back: int) -> tuple[str, date] | None | "OutOfRange": ...

# core/audit.py  (shared)  ACTIONS += ("routine_create", "routine_delete", "routine_run")
```

## 6. Files to touch

**Shared surface (first, sequentially):**
- `channels/base.py` — `send(disable_notification=)`, `set_message_reaction` default, `set_my_commands(scope_chat_id=)`, `run`/`on_message` message_id note.
- `channels/telegram.py` — implement the three above; extract `message_id` in `run`.
- `main.py` — `handle_inbound_message(inbound_message_id=None)`; thread message_id `run → on_message → handle_inbound_message` (plumbing only; feature wiring is integration).
- `core/commands.py` — `CommandKind += log, routine`; `reserved_trigger_words()` += log/บันทึก/routine/กิจวัตร *(skeleton)*.
- `core/audit.py` + `core/audit_view.py` — routine vocab + labels.
- `core/i18n.py` — key-block skeletons; `core/release_notes.py` — `RELEASE_NOTES["1.8.0"]`.
- `config.py` + `config.toml` — `[notifications]`/`[quicklog]`/`[reactions]`/`[backfill]`/`[routines]`.

**Module `quicklog` (parallel):** `core/quicklog.py`, `core/reactions.py` (NEW); `core/commands.py` `_match_log` + dispatch branch *(disjoint region)*; `core/i18n.py` quicklog/reaction keys *(disjoint keys)*; `tests/test_quicklog.py`, `tests/test_reactions.py` (NEW).

**Module `routines` (parallel):** `core/routines.py` (NEW); `storage/migrations.py` (migration 011); `storage/db.py` routines CRUD *(disjoint region)*; `core/commands.py` `_match_routine` + dispatch branch *(disjoint region)*; `core/i18n.py` routine keys *(disjoint keys)*; `tests/test_routines.py` (NEW).

**Module `backfill` (parallel):** `core/backfill.py` (NEW); `core/i18n.py` backfill keys *(disjoint keys)*; `tests/test_backfill.py` (NEW).

**Module `riders` (parallel):** `core/reminders.py`, `core/checkins.py`, `core/nudge.py` (silent flag at the one send site each); `tests/test_riders.py` (or extend `test_reminders`/`test_nudge`/`test_audit`).

**Integration seam (`main.py`, sequential, after modules):** route `/log` → keyboard + `log:` callback → `quicklog.handle_log_callback`; fire `reactions.react` after a successful typed log (using `inbound_message_id`); route `/routine` → `routines.execute_routine` + `routine:` callback → `routines.handle_routine_callback` (on_callback dispatch by payload prefix `undo:`/`log:`/`routine:`); wire `backfill.extract_date` into `handle_inbound_message` before `preparse`; pass `disable_notification=True` config through to the three ticks; register the two-scope menu; apply the `/audit` stored-preference fix in `on_message`.

## 7. External dependencies
None new. stdlib `sqlite3`, `datetime`, `re`. Telegram Bot API 7.0 `setMessageReaction`
(already reachable via the existing httpx client) and `setMyCommands` `scope`
(already-used endpoint, new field) — no library change. Migration 011 additive.

## 8. Acceptance criteria

### Shared / integration
- **AC-1** (silent send param): `send(..., disable_notification=True)` sets `"disable_notification": true` in the `sendMessage` payload; default `False` leaves the payload byte-identical to v1.7. (R-S1)
- **AC-2** (reaction method): `set_message_reaction` is a no-op default on the ABC (fakes/LINE stub unaffected); `TelegramChannel` posts `setMessageReaction` and never raises on transport error. (R-S2)
- **AC-3** (scoped menu): `set_my_commands(commands)` sends the default menu byte-identical to v1.7; `set_my_commands(commands, scope_chat_id=X)` adds `scope={type:chat,chat_id:X}`. (R-S3)
- **AC-4** (message_id plumbing): `run` passes the inbound `message_id` to `on_message`, which threads it to `handle_inbound_message`; a caller/fake omitting it (defaulted `None`) is unaffected. (R-S4)
- **AC-5** (config defaults): the five new config sections load with their documented defaults; an absent section uses those defaults; no existing key changes meaning. (R-S5/§2.5)
- **AC-6** (audit vocab): `routine_create`/`routine_delete`/`routine_run` are in `ACTIONS` with localized `/audit` labels (EN+TH). (R-S6)
- **AC-7** (release notes): `RELEASE_NOTES["1.8.0"]` (EN+TH) exists and is announced. (R-S7)
- **AC-8** (reserved words): `reserved_trigger_words()` contains `log`,`บันทึก`,`routine`,`กิจวัตร`; a custom habit named after any of them is rejected by `habitdef`. (R-S5)
- **AC-9** (regression gate — inert until invoked): with no v1.8 feature invoked, the full v1.7 suite stays green and every typed-log confirmation / command reply / extraction result is byte-identical to v1.7; the only proactive-send delta is the intended `disable_notification` flag (AC-D1). (R-G)

### Feature — quick-log + reactions (`quicklog`)
- **AC-A1** (keyboard from per-user registry): `/log`/`บันทึก` returns an inline keyboard built from the acting user's registry — amount buttons per goal-bearing/aliased numeric/duration habit (a user's "pushups" yields `[💪10]`), one done button per boolean habit, text habits omitted; an empty loggable set → a friendly hint. (R-Q1)
- **AC-A2** (`log:` callback logs): tapping `log:<habit>:<value>` inserts the log for the tapping user and produces the **same** confirmation (undo button) + dashboard refresh as typing that value. (R-Q2)
- **AC-A3** (ownership + safety): a `log:` payload naming a habit not in the tapping user's registry → friendly no-op, no write; malformed/oversized payload → logged + ignored, no read/write. (R-Q3)
- **AC-A4** (reaction on typed log, fail-open): after a successful typed log, with reactions enabled and a message_id present, the bot reacts with the habit's emoji (✅ fallback for a custom habit); a reaction failure never affects the log/confirmation; disabled → no reaction call. (R-Q4)
- **AC-A5** (reaction scope): reactions fire only for typed inbound-message logs — not for quick-log taps, undo, commands, clarifying questions, or deferred acks. (R-Q5)
- **AC-A6** (bilingual, zero-LLM): keyboard + confirmation follow the user's language; no Ollama call anywhere in the path. (R-Q6)

### Feature — routines (`routines`)
- **AC-B1** (create + validation): a well-formed `/routine <name> = ...` inserts the routine + ordered items, records `routine_create`, confirms bilingually; each of {bad name, name collision, empty items, unknown habit token, unparseable value, cap reached} → a friendly error with no write. (R-R1)
- **AC-B2** (list): `/routine` lists the user's routines with items + a run-button each, bilingual, per-user. (R-R2)
- **AC-B3** (run): `/routine <name>` (or run-button) logs every valid item for today, sends one compact summary, refreshes the dashboard once, records `routine_run`; a since-archived item is skipped and noted; no milestone/record celebration line appears; stored records stay accurate. (R-R3)
- **AC-B4** (delete): `/routine delete <name>` removes it (records `routine_delete`) and confirms; a non-existent name → friendly no-op, no write. (R-R4)
- **AC-B5** (isolation): user A's routine is invisible to and un-runnable by user B; a `routine:run:<name>` for a routine not owned by the tapping chat is a friendly no-op. (R-R5)
- **AC-B6** (migration 011): adds `routines`+`routine_items` additively, idempotent, stamps 11, touches no existing data, full suite green. (R-R6)
- **AC-B7** (zero-LLM): create/list/run/delete make no Ollama call. (R-R6)

### Feature — backfill (`backfill`)
- **AC-C1** (relative-date parse, EN+TH, zero-LLM): each of "500ml yesterday", "stretched 20 min on Monday", "diary 2 days ago", "เมื่อวาน ดื่มน้ำ 500", "3 วันที่แล้ว 500ml", "ยืดเส้น 20 นาที วันจันทร์" logs to the correct past date with the date shown, no LLM. (R-B1/R-B2)
- **AC-C2** (aggregations reflect it): a backdated log is counted by streaks, records, trends, heatmap, `/history`, and the daily summary for its resolved date, not for today. (R-B3)
- **AC-C3** (no retro-celebration): a backfill emits no milestone/record celebration line and does not edit today's dashboard (unless the resolved date is today); stored records remain accurate. (R-B4)
- **AC-C4** (bounds): a future date and a date older than `max_days_back` are each rejected with a friendly error and no write; a date == today falls through to the normal path unchanged. (R-B5)
- **AC-C5** (conservative, zero false positives): a message with no recognized date phrase logs for today byte-identically to v1.7; the date parser never misfires on an ordinary log/word. (R-B1/R-G)
- **AC-C6** (undo): a backfilled log's undo button removes exactly that row. (R-B6)

### Riders (`riders`)
- **AC-D1** (silent proactive): with `silent_proactive=true`, reminders, check-ins, and nudges send with `disable_notification=True`; confirmations and the dashboard pin stay notifying. (R-D1)
- **AC-D2** (owner-scoped menu): startup registers a public default-scope menu (public set + `/log` + `/routine`) and an owner-chat-scoped menu additionally listing `/invite`,`/approve`,`/block`,`/users`,`/audit`; a non-owner sees only public; a transport error never crashes startup. (R-D2)
- **AC-D3** (`/audit` language fix): a Thai-preferring owner's `/audit` view renders fully in Thai (header, action labels, actor "you", footer) because `on_message` resolves the stored `/lang` before the `/audit` interception. (R-D3)
- **AC-D4** (bounded delta): with `silent_proactive=false`, proactive payloads are byte-identical to v1.7; the `/audit` fix changes only language resolution, not row content/order. (R-D4)

## 9. Risks & open questions

**Open questions:** none load-bearing. The decisions below are resolvable from the
codebase and product grain; each is stated with its default so the team can start.

**Decisions (defaults; correct me before Phase 5 if any is wrong):**
- **Silent-proactive defaults ON** (gentle-by-default, the core product value). This is
  the one deliberate behavior change vs v1.7: reminder/check-in/nudge payloads gain
  `disable_notification:true`, so existing proactive-send payload tests must be updated
  to expect it (intended, documented, config-reversible — set `false` for byte-identical).
  If the team prefers zero test churn, flip the default to `false`; the capability ships
  either way.
- **Reactions fire on typed logs only**, not on quick-log button taps (a tap targets the
  bot's keyboard message, not a user log) — matches IDEAS #3's "reacts to the user's log
  message". Quick-log taps are acknowledged by the confirmation + the callback spinner.
- **Routine run shows one compact summary** and suppresses per-item milestone/record
  celebration (avoids a wall of messages; keeps it deterministic), while still updating
  stored records silently so history stays accurate.
- **Routine-as-quick-log-button** is delivered via the run-button in the `/routine` list
  view (module-disjoint), not by injecting routine buttons into the `/log` amount keyboard
  (which would couple `quicklog`↔`routines`). A single unified keyboard is a future polish.
- **Quick-log amount ladder** = alias multipliers (≤ `max_buttons_per_habit`), else a
  goal-derived `[¼G, ½G, G]` ladder, else skip. Tunable; the exact roundings are Luna's to
  pick sanely.
- **Backfill look-back = 14 days**, weekday names resolve to the most recent past
  occurrence, time-of-day for a backdated row = local noon.

**Risks:**
- **`commands.py` is a shared file** two parallel modules edit (`_match_log`, `_match_routine`
  + a dispatch branch each). Mitigated by the v1.7 precedent (habitdef did the same with
  disjoint regions); the `CommandKind`/`reserved_trigger_words` skeleton lands first in the
  shared surface, and the two dispatch-branch insertions are disjoint lines resolved at
  integration.
- **`main.py` is the integration seam** touched by all four features (callback router,
  reaction call, backfill branch, silent flags, two-scope menu, `/audit` fix). This is
  sequential integration by design, not parallel module work — modules ship self-contained
  files + their disjoint `commands.py`/`i18n.py` regions; main.py wiring is the final pass.
- **Reaction/silent are decorative/gentle → fail-open** is load-bearing: a reaction or a
  silent-flag path must never break a log or a proactive send. Guarded by AC-2/AC-A4/AC-D4.
- **Backfill false positives** would silently misfile a log to the wrong day — defended by
  whole-clause anchoring + fixed word-lists (AC-C5), same zero-false-positive discipline the
  command matchers already prove.

## 10. Out of scope
- **Multi-habit-in-one-message** ("500ml and stretched 15 min" → two logs) — a real
  extraction-schema change (IDEAS-v1.8 #3), deferred; each message still yields one log.
- **Free-text NL routine phrasing** ("log my morning routine") — routines are run by
  `/routine <name>` or the run-button only; deterministic surface, no NL.
- **Backfill of text/diary via a quick-log button**, and **editing** a past day's value
  (backfill only *adds* a row for a past day; correcting it uses the existing undo/edit).
- **A single unified quick-log keyboard** merging routine buttons with amount buttons.
- **`sendDocument`-based export** (IDEAS-v1.8 #9) and any new channel file surface beyond
  `setMessageReaction` — honesty flag from IDEAS-v1.8, still deferred.
- **Reactions on proactive/bot messages** — reactions are on the user's own typed log only.

## 11. Module split & parallel development

**Total functionals:** 7 — (1) quick-log keyboard, (2) emoji reactions, (3) routines,
(4) backfill, (5) silent proactive sends, (6) owner-scoped menus, (7) `/audit` language
fix.

**Recommendation:** **SMALL SEQUENTIAL shared surface, then 4 PARALLEL modules, then
integration.** Above the 5-functional threshold, and the features are genuinely
independent once a thin shared surface exists (the channel-method additions + the inbound
`message_id` plumbing + the command/audit/i18n/config skeletons). Each module owns its own
new file(s) and a disjoint region of the shared multi-writer files (`commands.py`,
`i18n.py`), exactly like the v1.7 `habitdef` split. `main.py` is the integration seam
(sequential, last), not parallel work.

**Shared surface (built first, sequentially):**
- Channel: `send(disable_notification=)`, `set_message_reaction`, `set_my_commands(scope_chat_id=)` (`channels/base.py` + `channels/telegram.py`).
- Inbound `message_id` plumbing: `run → on_message → handle_inbound_message` (signature/threading only).
- `commands.py` skeleton: `CommandKind += log, routine`; `reserved_trigger_words()` additions.
- Audit vocab (`routine_*`) + `audit_view` labels; i18n key-block skeletons; `RELEASE_NOTES["1.8.0"]`.
- Config: the five new sections + defaults.

| Module | Owned ACs | Owned files | Depends on (shared) |
|---|---|---|---|
| `quicklog` | AC-A1..A6 | `core/quicklog.py`, `core/reactions.py`; `commands.py` (`_match_log`, disjoint); `i18n.py` (quicklog/reaction keys); `tests/test_quicklog.py`, `tests/test_reactions.py` | message_id plumbing, `set_message_reaction`, callback-router seam |
| `routines` | AC-B1..B7 | `core/routines.py`; `storage/migrations.py` (011); `storage/db.py` (routines CRUD, disjoint); `commands.py` (`_match_routine`, disjoint); `i18n.py` (routine keys); `tests/test_routines.py` | `routine_*` audit vocab, per-user unit lookup, dashboard refresh |
| `backfill` | AC-C1..C6 | `core/backfill.py`; `i18n.py` (backfill keys); `tests/test_backfill.py` | (none beyond integration into `handle_inbound_message`) |
| `riders` | AC-D1..D4 | `core/reminders.py`, `core/checkins.py`, `core/nudge.py` (silent flag, one send site each); `tests/test_riders.py` | `send(disable_notification=)`, `set_my_commands(scope_chat_id=)` |

ACs verified during the shared-surface / integration pass: **AC-1..AC-9**. Every AC belongs
to exactly one owner. **Total: 32 acceptance criteria** (shared/integration 9, `quicklog` 6,
`routines` 7, `backfill` 6, `riders` 4).

**Integration order (after all four modules complete):**
1. `main.py`: `on_callback` dispatches by payload prefix (`undo:` → undo_ui, `log:` → quicklog, `routine:run:` → routines); route `/log`/`/routine` kinds; wire `backfill.extract_date` into `handle_inbound_message` **before** `preparse`; fire `reactions.react` after a successful typed log using `inbound_message_id`; pass the silent flag into the three ticks; register the two-scope menu; apply the `/audit` stored-preference fix.
2. Full suite; highest-value gates: **AC-9** (inert-until-invoked, v1.7 byte-identical), **AC-A3/AC-B5** (per-user callback ownership), **AC-C5** (backfill zero false positives), **AC-C2** (backdated aggregations), **AC-D3** (audit language).
3. Integration tests, two users end-to-end: A taps `/log` and a button (logs + reaction on a typed log); A creates + runs a routine while B sees no trace; A backfills "yesterday" and it lands on the right day in `/history`+heatmap without touching today's dashboard or firing a milestone; the owner's menu shows admin commands and a non-owner's does not; a Thai-preferring owner's `/audit` is fully Thai.
