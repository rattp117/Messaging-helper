# Spec — v1.10.0: "Never lose a log"

## 1. Problem statement
When Ollama is DOWN, a free-text log the deterministic pre-parser can't handle is deferred as a
`category='unparsed'` row and re-parsed on the next DOWN→UP recovery sweep. But a row the LLM **still**
can't place has **no terminal state**, so `db.pending_unparsed()` returns it on *every* future recovery
sweep — forever. Live production proves it: rows `id=13` ("500") and `id=14` ("Streaching") have been
re-parsed (2 LLM calls each) on every restart/recovery since Aug 25, and **the user was never told their
logs died**. This release makes the bot honest and durable: (1) a row the sweep still can't parse is
**closed** — the user is messaged once (kindly, quoting their words, with a recovery path) and the row is
moved to a terminal state so it permanently exits the pending pool; (2) an unparseable message offers
**conservative tap-to-fix** buttons from deterministic tier-1 guesses; (3) a **reply to a reminder** with a
bare value logs against that reminder's habit with zero LLM; (4) during an outage the bot gives an
**immediate, honest** reply about what still works instead of a bare deferral ack; (5) a `/guide` card gives
newcomers a 20-second orientation; plus two riders (fail-open unification at the 5 pause-gating sites, and
pytest-xdist enablement). Success = the zombie re-parse loop and the silent betrayal are both gone, every
new behavior is zero-LLM / bilingual / per-user-isolated, and `RELEASE_NOTES["1.10.0"]` (EN+TH) announces it.

SemVer: **1.10.0 (MINOR)** — purely additive (migration 013 additive; new config sections default to the
current behavior or a safe ON; `Channel.send`'s new return value and the new `CommandKind`/inbound arg are
additive and back-compatible). No breaking API/schema/CLI change.

## 2. Inputs

### 2.1 The unparsed-state machine (the spine of functionals 1 & 2)
A new nullable column `logs.unparsed_state` (migration 013) gives every `category='unparsed'` row a lifecycle.
Only three non-terminal/terminal values are ever written; **legacy rows (NULL) are treated as `awaiting_llm`**:

| `unparsed_state` | Meaning | In the recovery-sweep queue? | Can be tapped? |
|---|---|---|---|
| `NULL` (legacy) or `awaiting_llm` | Deferred during an outage; the LLM has not yet answered. | **Yes** | No |
| `awaiting_clarify` | The LLM answered "unknown" (or a bare number it couldn't place) and tap-to-fix guess buttons were offered; the LLM is **not retried** on this row. | **No** | Yes |
| `closed` | The user was notified once (no tier-1 guess available); the row is terminal. | **No** | No |
| (row reclassified to a real habit id) | Resolved — `category` changes away from `'unparsed'`, so it naturally leaves every unparsed query. | No | — |

### 2.2 Inbound message (Telegram) — extended plumbing
`TelegramChannel.run` already passes `on_message(chat_id, text, display_name, message_id)` (v1.8). This
release adds a **5th trailing, defaulted arg** `reply_to_message_id: str | None` — the `message.reply_to_message.message_id`
(as `str`) when the inbound message is a Telegram *reply*, else `None`. Same additive shape as `display_name`/`message_id`.

Concrete example — user replies to the bot's water reminder:
```
Bot (message_id=8801): 💧 เวลาดื่มน้ำแล้ว!
User (reply_to_message_id=8801): 500
```

### 2.3 Tier-1 clarify guesses (deterministic, zero-LLM)
Input = the raw text + the **acting user's per-user registry** (base + customs, via `provider.for_user`).
Two guess sources only (tier-2 fuzzy Thai matching is out of scope, §10):
- **Label / alias / unit match** — a token of the text (or the whole stripped text) exactly matches, or is a
  length-≥3 prefix of, a habit's `label_en`/`label_th`/`unit_en`/`unit_th`/alias key (case-insensitive). Value =
  the number in the text if present, else the habit's `targets.effective_goal` (boolean → 1); a match with no
  derivable value is dropped.
- **Bare-number unit-plausibility** — the whole text is a bare positive number `N` (`units.VALUE_RE` matches
  with `unit is None`, *or* the unit token didn't resolve). Each numeric/duration habit whose effective goal
  `G` makes `N` plausible (`G*lower ≤ N ≤ G*upper`, defaults `lower=0.05`, `upper=5.0`) yields guess `(habit, N)`.

Worked example against the default registry (water goal 2000 ml, stretch goal 30 min):
- `"500"` → water plausible (`100 ≤ 500 ≤ 10000`), stretch **not** (`500 > 150`) → one guess `(water, 500)`.
- `"stretch"` / `"stre"` → label/prefix match → guess `(stretch, 30)` (effective goal).
- `"Streaching"` (typo) → no exact/prefix match, no number → **no tier-1 guess** → generic closure path.

### 2.4 New config (all additive; defaults preserve or safely enable behavior)
```toml
[outage]
honest_reply = true          # v1.10: immediate honest outage reply (false = pre-1.10 bare deferred_ack)

[clarify]
enabled = true               # false = generic clarifying question only, no guess buttons
max_guesses = 4              # cap on tap-to-fix buttons offered
plausibility_lower = 0.05    # bare-number guess window: G*lower <= N <= G*upper
plausibility_upper = 5.0

[reply_to_reminder]
enabled = true               # attribute a bare-value reply-to-reminder to that reminder's habit
context_cap = 32             # per-chat in-memory reminder→habit map size (oldest evicted)
```

## 3. Outputs

### 3.1 Closure notification (recovery sweep still can't parse; no tier-1 guess) — sent ONCE
```
EN:  🧠 I couldn't make sense of "Streaching" — my language brain was offline when you sent it, and I
     still can't place it. Nothing was logged. If you'd like to log it, tap a habit below, or type it
     like "500 ml".
     [ 💧 500ml ] [ 💪 15min ] [ ✅ diary ]        ← the /log keyboard (recovery path)
TH:  🧠 ฉันยังไม่เข้าใจ "Streaching" — ตอนที่คุณส่งมาระบบภาษาออฟไลน์อยู่ และตอนนี้ก็ยังจับใจความไม่ได้
     ยังไม่มีการบันทึกใดๆ ถ้าต้องการบันทึก แตะกิจกรรมด้านล่าง หรือพิมพ์แบบ "500 ml"
     [ 💧 500ml ] [ 💪 15min ] [ ✅ diary ]
```
Row → `closed`. Never re-swept, never re-notified.

### 3.2 Tap-to-fix clarify offer (guesses exist; recovery-fail OR live LLM-unknown)
```
EN:  🤔 I couldn't parse "500". Did you mean one of these? (Or type it like "500 ml".)
     [ 💧 water 500ml ]
TH:  🤔 ฉันแยกแยะ "500" ไม่ได้ หมายถึงอันไหนนี้ไหม (หรือพิมพ์แบบ "500 ml")
     [ 💧 water 500ml ]
```
Each button `callback_data = "clarify:<row_id>:<habit_id>:<value>"`. Row → `awaiting_clarify`.

### 3.3 Clarify tap confirmation (ordinary log)
Tapping `[ 💧 water 500ml ]` reclassifies the row and confirms exactly like a recovered log (reuses the
existing `recovered_water`/`recovered_stretch`/`recovered_diary`/`recovered_*` copy + the inline Undo button),
then refreshes the pinned dashboard.

### 3.4 Outage-honesty reply (Ollama DOWN + preparse miss), config-gated ON
```
EN:  🧠 My language brain is offline right now, so I saved "went for a run" and will sort it out when it's
     back. These still work instantly: a number+unit like "500 ml", the /log buttons below, or a /routine.
     [ 💧 500ml ] [ 💪 15min ] …
TH:  🧠 ตอนนี้ระบบภาษาออฟไลน์อยู่ ฉันเลยเก็บ "went for a run" ไว้ และจะจัดการให้เมื่อกลับมา
     สิ่งที่ยังใช้ได้ทันที: ตัวเลข+หน่วยแบบ "500 ml", ปุ่ม /log ด้านล่าง หรือ /routine
     [ 💧 500ml ] [ 💪 15min ] …
```
The deferral row is still written (`awaiting_llm`) and recovery is unchanged. `[outage] honest_reply=false`
→ the pre-1.10 `deferred_ack` (byte-identical to v1.9).

### 3.5 Reply-to-reminder log (bare value, zero-LLM, works offline)
`reply_to_message_id=8801 → water`, text `"500"` → logs 500 ml water and sends the normal water confirmation
(with Undo + the ✅ reaction on the reply). A reply that doesn't map, or isn't a bare value, → normal path.

### 3.6 `/guide` card (in-chat, forwardable, bilingual)
A compact getting-started card (header + how to log + key commands + message syntax), one `channel.send`,
built like `/help` (a list of `i18n.t("guide_*")` lines joined `"\n\n"`); not budget-capped (fixed size).

### 3.7 `RELEASE_NOTES["1.10.0"]` (EN+TH) — mandatory, announced once/user
Bullets: never-lose-a-log closure + tap-to-fix; reply-to-reminder logging; outage honesty; `/guide`.

## 4. Behavior rules

### Shared surface — state machine, channel, plumbing
- **R-SS1 (migration 013)** `ALTER TABLE logs ADD COLUMN unparsed_state TEXT`. Purely additive, no row
  rewrite, default `NULL`; idempotent via `PRAGMA user_version`; stamps 13. No existing table/column/row
  touched. Existing `'unparsed'` rows keep `NULL`, which the sweep treats as `awaiting_llm` (R-SS2) — so the
  production zombies `id=13`/`id=14` enter the new machinery on the first post-1.10 recovery and are closed
  (R1). **No data-migration UPDATE is written** (NULL-as-`awaiting_llm` avoids it).
- **R-SS2 (pending pool)** `db.pending_unparsed()` gains `AND (unparsed_state IS NULL OR unparsed_state='awaiting_llm')`.
  `awaiting_clarify`/`closed`/reclassified rows never appear in it again.
- **R-SS3 (atomic CAS transitions)** Two new **guarded** `Database` methods return `bool` (`rowcount == 1`):
  - `resolve_unparsed(log_id, *, from_states, category, value_num, value_text, habit_type)` — reclassify to a
    real habit and clear `unparsed_state` to NULL, only if the row is still `category='unparsed'` and in
    `from_states`.
  - `mark_unparsed_state(log_id, *, from_states, to_state)` — state-only transition under the same guard.
  Both build the `from_states` predicate as `(unparsed_state IS NULL OR unparsed_state IN (…))` so `NULL` can
  be an expected origin. These CAS methods are the **race guard** (R11).
- **R-SS4 (LogEntry)** `LogEntry` gains trailing `unparsed_state: str | None = None`; `insert_log` writes it.
  Every existing caller is byte-identical (writes `NULL` as before); the deferral insert is **unchanged**
  (its `LogEntry` has no `unparsed_state`, i.e. NULL = `awaiting_llm`).
- **R-SS5 (`Channel.send` returns the message id)** `Channel.send` return type becomes `str | None` (the sent
  message's id, `None` when the channel can't provide one) — mirroring `send_and_pin`'s existing contract.
  `TelegramChannel.send` returns `str(resp.json()["result"]["message_id"])`. Additive: every existing caller
  ignores the return, byte-identical send behavior. Test fakes that return `None` are unaffected; the shared
  `RecordingChannel` (conftest) returns a synthetic incrementing id so reply-attribution tests can map it.
- **R-SS6 (reminder→habit context map)** `ReminderState` (the existing process-global, already threaded into
  both the send and inbound paths) gains a bounded per-chat map and two methods:
  `remember_reminder(chat_id, message_id, habit_id)` (evicts oldest beyond `context_cap`) and
  `habit_for_reply(chat_id, message_id) -> str | None`. `send_reminder`, after a send that returns a
  message id, records `(chat_id, msg_id) → habit.id`. **In-memory, lost on restart, by design** (R14).
- **R-SS7 (inbound reply plumbing)** `TelegramChannel.run` extracts `reply_to_message.message_id` and passes
  it as the 5th arg to `on_message`; `on_message` threads it to `handle_inbound_message(reply_to_message_id=…)`.
- **R-SS8 (`/guide` recognition)** `CommandKind` gains `"guide"`; a whole-message-anchored matcher
  `^(?:/guide|คู่มือ)$` (re.IGNORECASE) via `_bool_matcher`, placed before the `query` row (invariants hold);
  `reserved_trigger_words()` gains `"guide"`/`"คู่มือ"` so a custom habit can't be named after it.
- **R-SS9 (pause fail-open helpers)** `core/pause.py` gains two fail-open wrappers used by the 5 sites:
  `is_paused_safe(db, config, user_id, habit_id, when) -> bool` (any read error → logged, returns `False` =
  not paused) and `active_pauses_safe(db, user_id) -> list` (any read error → logged, returns `[]`). This puts
  the "treat a pauses-read failure as not-paused" decision in one place.

### Functional 1 — Unparsed closure with terminal state (recovery sweep)
- **R1 (close on final failure)** In `reparse_pending_unparsed`, for a row whose re-parse still yields no
  registry habit: compute `clarify.tier1_guesses`. **If none**, CAS `mark_unparsed_state(from=(NULL,'awaiting_llm'),
  to='closed')`; **only if it wins** (rowcount 1), send the ONE bilingual closure notification (§3.1) quoting
  `row["raw_message"]`, with the `/log` keyboard attached (empty keyboard → the friendly hint instead). The
  CAS-gate guarantees exactly-once notification even under overlapping sweeps.
- **R2 (no zombies)** A `closed` (or `awaiting_clarify`) row never re-enters `pending_unparsed()` (R-SS2), so no
  future DOWN→UP sweep re-parses it — killing both the silent betrayal and the 2-LLM-calls-forever loop.
- **R3 (recovery success unchanged, now guarded)** A row that DOES re-parse is reclassified via
  `resolve_unparsed(from=(NULL,'awaiting_llm'), …)`; only the winner sends the existing recovered-* confirmation
  + dashboard refresh. (Same user-visible behavior as v1.9, now race-safe.)
- **R4 (single-flight sweep)** `reparse_pending_unparsed` acquires a non-blocking module-level guard: if a sweep
  is already running, the new trigger logs and returns immediately (the running sweep's snapshot already covers
  everything deferred up to the outage's end). Defense-in-depth on top of the per-row CAS.

### Functional 2 — Conservative tap-to-fix clarify
- **R5 (tier-1 only, zero-LLM)** Guesses come **only** from the deterministic tier-1 sources of §2.3 against
  the acting user's per-user registry (incl. customs). No deep/fuzzy Thai matching (§10). Capped at
  `config.clarify.max_guesses`, de-duplicated by `(habit_id, value)`, exact matches before prefix/plausibility.
- **R6 (offer on live LLM-unknown)** In `handle_inbound_message`, when `parse_message` returns no registry
  habit (reachable only when Ollama is UP — the outage path returned earlier): **with guesses** → `insert_log`
  a fresh `awaiting_clarify` row (raw_message = text) and send the guess offer (§3.2); **without guesses** →
  send the generic bilingual clarifying question **plus** the `/log` keyboard (no row written — byte-compatible
  with today's `clarifying_question`, plus the keyboard). `config.clarify.enabled=false` → generic path always.
- **R7 (offer on recovery-fail)** In the sweep, a final-failure row **with guesses** → CAS
  `mark_unparsed_state(to='awaiting_clarify')`, and only the winner sends the guess offer (§3.2). This is the
  guess-bearing counterpart of R1's closure.
- **R8 (awaiting-clarify vs awaiting-LLM — the sweep exclusion)** `awaiting_clarify` is a distinct state from
  `awaiting_llm`: the LLM is **never retried** on an `awaiting_clarify` row (excluded from `pending_unparsed`),
  whereas `awaiting_llm` is the sweep's queue. This distinction is what makes the offer a terminal step for the
  LLM while leaving the tap open.
- **R9 (tap logs — ordinary log)** `on_callback` routes `clarify:` → `clarify.handle_clarify_callback`, which:
  validates the payload shape (`^clarify:(?P<row>\d+):(?P<habit>[a-z0-9_]{1,32}):(?P<value>-?\d{1,15}(?:\.\d{1,6})?)$`,
  `re.ASCII`) and value bounds (mirrors `quicklog`); resolves the habit against the **tapping user's** registry
  (unknown/foreign id → friendly no-op, no write); then CAS `resolve_unparsed(from=('awaiting_clarify',),
  category=habit.id, value_num=…, habit_type=…)`. **Only the winner** sends the recovered-* confirmation +
  Undo + dashboard refresh. A clarify-tap log is an ordinary log — **not audited** (see R12).
- **R10 (no guesses anywhere → generic + /log)** If neither the live nor recovery path can produce a tier-1
  guess, the user gets a generic bilingual question / closure with the `/log` keyboard — never silence, never a
  guess the bot can't stand behind.
- **R11 (sweep-vs-tap race guard — normative)** Every state-advancing write is a guarded CAS (R-SS3). The tap's
  reclassify is guarded on `awaiting_clarify`; the sweep's reclassify/close is guarded on `NULL`/`awaiting_llm`.
  Because the tap and the sweep act on **disjoint origin states**, and each transition is an atomic
  compare-and-swap, whichever commits first flips the row and the other observes `rowcount == 0` and does
  nothing further — **no double log, no double notification** — regardless of interleaving. The single-flight
  guard (R4) additionally prevents two sweeps from co-processing the same `awaiting_llm` row.
- **R12 (audit posture — argued)** Ordinary new logs are **not** audited anywhere in this codebase (audit
  captures corrections/administrative mutations: undo/edit/target/remind/lang/quiet/approve/…, never a raw
  log write or the existing recovery `reclassify_log`). A **clarify-tap log** is an ordinary log → **no audit
  row** (consistent). A **closure notification** is an outbound message with no DB mutation → **nothing to
  audit**. No new audit vocab is introduced. (This is the "argue" the panel asked for; the answer is: leave
  audit untouched, consistent with the established rule.)

### Functional 3 — Reply-to-reminder attribution
- **R13 (deterministic attribution)** In `handle_inbound_message`, **after** backfill extraction and **before**
  preparse: if `reply_to_message_id` is set, `config.reply_to_reminder.enabled`, and
  `reminder_state.habit_for_reply(user_id, reply_to_message_id)` resolves to a habit in the acting registry,
  compute `reply_attribution.resolve_reply_value(text, habit)`. If it returns a value, set
  `result = ExtractionResult(habit.id, value, 1.0)` and fall through to the shared write+confirm block (exactly
  like a preparse hit) — zero LLM, works while Ollama is DOWN, fires the reaction, refreshes the dashboard.
- **R14 (conservative + honest degradation)** `resolve_reply_value` returns a value **only** for a **bare
  positive number** (`units.VALUE_RE`, `unit is None`) → `value_num = N` in the habit's base unit; a boolean
  habit + an affirmative token → `1.0`; **everything else → `None`** (a number+unit resolving to another habit,
  or non-value text, falls through to preparse / the normal path). The mapping is **in-memory and bounded**
  (R-SS6): only **per-habit reminder** messages are mapped (check-in and nudge prompts are multi-habit and
  therefore **never** mapped — a reply to them is ambiguous and takes the normal path). On restart the map is
  empty; a reply to a pre-restart reminder simply falls through — no wrong attribution, no data loss.

### Functional 4 — Outage honesty
- **R15 (immediate honest reply)** When `health_monitor` reports Ollama DOWN and preparse misses (the existing
  deferral branch), still write the `awaiting_llm` deferral row and keep the recovery machinery, but replace the
  bare `deferred_ack` send with the outage-honesty message (§3.4): bilingual, names the instant-working paths
  (number+unit, `/log`, `/routine`), quotes the saved text, and attaches the `/log` keyboard. Gated by
  `config.outage.honest_reply` (default `true`); `false` restores the pre-1.10 `deferred_ack` byte-for-byte.

### Functional 5 — /guide command
- **R16 (`/guide` card)** `handle_inbound_message` routes `command.kind == "guide"` to
  `discoverability.build_guide_text(config, lang)` (a fixed-size bilingual card built from `i18n.t("guide_*")`
  lines joined `"\n\n"`, mirroring `build_help_text`; **not** budget-capped by precedent), sent in one
  `channel.send`. Content: how to log (free text / number+unit / `/log`), the key commands, and the message
  syntax; a compact companion to Patty's full manual.
- **R17 (menu)** `/guide` is added to the public `set_my_commands` menu (public **22 → 23**); the owner menu,
  being a strict superset, becomes **27 → 28**. New `GUIDE_COMMAND_DESCRIPTIONS` (EN+TH) in `core/app.py`,
  inserted after `wrapped` in the public list.

### Functional 6 — Riders
- **R18 (fail-open unification at the 5 pause-gating sites)** Every proactive site treats a pauses-read failure
  as **not paused** (fail-open, per-user), matching `reminders.send_reminder`'s existing posture, via the R-SS9
  helpers:
  - `reminders.send_reminder` — already fail-open; adopt `is_paused_safe` (byte-identical).
  - `checkins.build_checkin_message` — the `db.active_pauses(user_id)` fetch → `active_pauses_safe` (error → no
    suppression → check-in still sent); a per-user error no longer aborts the tick for later users.
  - `nudge.build_nudge_message` — the `db.active_pauses(user_id)` fetch → `active_pauses_safe` (error → habit
    treated not-paused → still a "close" candidate), rather than dropping the whole user's nudge.
  - `streaks.compute_daily_summary` — per-habit `pause.is_paused` → `is_paused_safe`.
  - `review.compute_weekly_stats` (and the `run_weekly_review` trends filter + the chart-render path) — per-habit
    `pause.is_paused` → `is_paused_safe`.
  Net: a pauses-table hiccup for user A never suppresses A's send incorrectly **and** never aborts the run for
  users B, C… at any of the 5 sites.
- **R19 (pytest-xdist)** Add `pytest-xdist>=3.5` to the `dev` optional-dependency group. Document the parallel
  invocation (`pytest -n auto`). The `tmp_path` order-dependence blockers were fixed in v1.9.4 (refactor AC13);
  Vera verifies no residual order-dependence — the full suite must stay green **both** serially (`pytest`) and
  in parallel (`pytest -n auto`). `[tool.pytest.ini_options]` stays with `asyncio_mode="auto"` +
  `testpaths=["tests"]` (a meta-test asserts `testpaths` is present); `-n auto` is **documented, not forced via
  `addopts`** (so a single-worker debug run needs no override, and CI opts in explicitly).

## 5. Interfaces (signatures)

```python
# storage/migrations.py
def _migration_013_unparsed_state(conn: sqlite3.Connection) -> None: ...
#   ALTER TABLE logs ADD COLUMN unparsed_state TEXT      # NULL default; append to MIGRATIONS (index 12 -> v13)

# storage/models.py  (LogEntry — trailing, defaulted)
unparsed_state: str | None = None

# storage/db.py
def insert_log(self, entry: LogEntry) -> int: ...                       # now writes unparsed_state
def pending_unparsed(self) -> list[sqlite3.Row]: ...                    # + AND (unparsed_state IS NULL OR ='awaiting_llm')
def resolve_unparsed(self, log_id: int, *, from_states: tuple[str | None, ...],
                     category: str, value_num: float | None, value_text: str | None,
                     habit_type: str | None) -> bool: ...               # guarded CAS reclassify -> rowcount==1
def mark_unparsed_state(self, log_id: int, *, from_states: tuple[str | None, ...],
                        to_state: str) -> bool: ...                     # guarded CAS state-only -> rowcount==1

# channels/base.py + channels/telegram.py
async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> str | None: ...  # returns message_id

# channels/base.py Channel.run / channels/telegram.py TelegramChannel.run
#   extracts reply_to_message.message_id; passes on_message(chat_id, text, display_name, message_id, reply_to_message_id)

# core/reminders.py  (ReminderState — additive fields/methods)
class ReminderState:
    last_habit_id: dict[str, str]
    reminder_context: dict[str, "OrderedDict[str, str]"]               # chat_id -> {message_id: habit_id}, bounded
    def remember_reminder(self, chat_id: str, message_id: str, habit_id: str, *, cap: int) -> None: ...
    def habit_for_reply(self, chat_id: str, message_id: str) -> str | None: ...

# core/pause.py  (shared surface — fail-open wrappers)
def is_paused_safe(db, config, user_id: str, habit_id: str, when) -> bool: ...      # error -> False (not paused)
def active_pauses_safe(db, user_id: str) -> list: ...                                # error -> []

# core/clarify.py  (NEW — module M1)
AWAITING_LLM = "awaiting_llm"; AWAITING_CLARIFY = "awaiting_clarify"; CLOSED = "closed"
def tier1_guesses(text: str, registry: "HabitRegistry", db, config, user_id: str) -> list[tuple[str, float]]: ...
def build_guess_buttons(guesses: list[tuple[str, float]], row_id: int,
                        registry: "HabitRegistry", lang: i18n.Language) -> list[Button]: ...
async def offer_clarify(channel, db, config, registry, lang, user_id, *, row_id: int, text: str) -> None: ...
async def send_closure(channel, db, config, registry, lang, user_id, *, text: str) -> None: ...
async def handle_clarify_callback(chat_id, data, source_text, callback_id, *,
                                  db, channel, config, registry, clock=datetime.now) -> None: ...

# core/reply_attribution.py  (NEW — module M2)
def resolve_reply_value(text: str, habit: "Habit") -> float | None: ...             # bare number / boolean-affirmative

# core/discoverability.py  (module M2)
def build_guide_text(config: "Config", lang: i18n.Language) -> str: ...

# core/commands.py  (shared surface)
CommandKind = Literal[..., "guide"]                                                 # + _match_guide + _MATCHERS row
# reserved_trigger_words() += {"guide", "คู่มือ"}

# config.py
class OutageConfig(BaseModel): honest_reply: bool = True
class ClarifyConfig(BaseModel):
    enabled: bool = True; max_guesses: int = 4
    plausibility_lower: float = 0.05; plausibility_upper: float = 5.0
class ReplyToReminderConfig(BaseModel): enabled: bool = True; context_cap: int = 32
# Config gains: outage, clarify, reply_to_reminder (append after `wrapped`, before `habits`)

# core/release_notes.py
RELEASE_NOTES["1.10.0"] = {"en": "🎉 What's new in v1.10.0\n• …", "th": "🎉 มีอะไรใหม่ใน v1.10.0\n• …"}
```

## 6. Files to touch

**Shared surface (first, sequentially):**
- `storage/migrations.py` — `_migration_013_unparsed_state` + append to `MIGRATIONS`.
- `storage/models.py` — `LogEntry.unparsed_state`.
- `storage/db.py` — `insert_log` (write column); `pending_unparsed` (predicate); `resolve_unparsed`,
  `mark_unparsed_state` (guarded CAS).
- `channels/base.py`, `channels/telegram.py` — `send -> str | None`; `run` extracts + passes
  `reply_to_message_id`.
- `core/reminders.py` — `ReminderState` context map + methods; `send_reminder` captures the message id;
  adopt `pause.is_paused_safe` (byte-identical).
- `core/pause.py` — `is_paused_safe`, `active_pauses_safe`.
- `core/commands.py` — `CommandKind "guide"`, `_match_guide`, `_MATCHERS` row, `reserved_trigger_words()`.
- `core/i18n.py` — key skeletons: closure/clarify (M1) + outage/guide (M2) blocks (disjoint keys).
- `core/release_notes.py` — `RELEASE_NOTES["1.10.0"]` (EN+TH).
- `config.py` + `config.toml` — `[outage]`, `[clarify]`, `[reply_to_reminder]`.
- `tests/conftest.py` — `RecordingChannel.send` returns a synthetic incrementing message id.

**Module M1 — closure + clarify (parallel):**
- `core/clarify.py` — NEW: `tier1_guesses`, `build_guess_buttons`, `offer_clarify`, `send_closure`,
  `handle_clarify_callback`, state constants.
- `core/i18n.py` — closure + clarify copy (disjoint keys).
- `tests/test_clarify.py`, `tests/test_unparsed_closure.py` — NEW.

**Module M2 — reply-to-reminder + outage + /guide (parallel):**
- `core/reply_attribution.py` — NEW: `resolve_reply_value`.
- `core/discoverability.py` — `build_guide_text`.
- `core/i18n.py` — outage + guide copy (disjoint keys).
- `tests/test_reply_to_reminder.py`, `tests/test_outage_honesty.py`, `tests/test_guide.py` — NEW.

**Module M3 — riders (parallel, disjoint):**
- `core/checkins.py`, `core/nudge.py`, `core/streaks.py`, `core/review.py` — route pause reads through the
  R-SS9 fail-open helpers.
- `pyproject.toml` — `pytest-xdist>=3.5` in `dev`; document `pytest -n auto`.
- `tests/test_pause_failopen.py` — NEW.

**Integration seam (`core/routing.py` + `core/app.py`, sequential after M1/M2):**
- `core/routing.py` — `reparse_pending_unparsed` (single-flight guard + guess/closure via M1 + CAS terminal
  states); `handle_inbound_message` (reply-attribution block before preparse; outage-honesty message in the
  deferral branch; live LLM-unknown → M1 clarify offer/generic; `/guide` dispatch branch;
  `reply_to_message_id` param); `on_callback` (`clarify:` prefix → `clarify.handle_clarify_callback`).
- `core/app.py` — `GUIDE_COMMAND_DESCRIPTIONS` + public menu entry (22→23, owner 27→28); thread
  `reply_to_message_id` through the `_on_message` closure; construct/thread the extended `ReminderState`.

## 7. External dependencies
- **New dev-only:** `pytest-xdist>=3.5` (R19) — parallel test execution; already a documented deferred
  follow-up (refactor OQ5), unblocked by the v1.9.4 `tmp_path` fixes.
- No new runtime dependency. stdlib `sqlite3`; Telegram Bot API `sendMessage` (already used; we now read its
  `result.message_id`) and inbound `reply_to_message` (already delivered in the update). Migration 013 additive.

## 8. Acceptance criteria

### Shared / integration
- **AC1** (migration 013): Given a v1.9 DB at `user_version=12`, migration 013 adds `logs.unparsed_state`
  (default NULL), touches no existing data, is idempotent (stamps 13), and the full suite stays green. (R-SS1)
- **AC2** (`send` returns id): `TelegramChannel.send` returns `str(result.message_id)`; the default-`False`
  payload is byte-identical to v1.9; a fake/`send_and_pin`-style `None` return is a valid "no id"; every
  existing caller ignoring the return is unaffected. (R-SS5)
- **AC3** (CAS state machine): `pending_unparsed()` returns only `NULL`/`awaiting_llm` rows;
  `resolve_unparsed`/`mark_unparsed_state` succeed (return `True`, rowcount 1) only from an expected
  `from_state` and are no-ops (return `False`) otherwise; `insert_log` persists `unparsed_state`. (R-SS2/3/4)
- **AC4** (`/guide` recognized & reserved): `/guide` and `คู่มือ` dispatch to `kind="guide"`; a habit
  id/label equal to `guide`/`คู่มือ` is rejected by `reserved_trigger_words()`; dispatch invariants still hold. (R-SS8)

### Module M1 — closure + tap-to-fix clarify
- **AC5** (zombie loop killed): After one recovery sweep can't parse a row, it is moved to a terminal state and
  `pending_unparsed()` never returns it again; a subsequent DOWN→UP recovery does **not** re-parse it (no LLM
  call). Verified specifically for texts `"500"` and `"Streaching"`. (R1/R2/R8)
- **AC6** (closure once, no-guess): A recovery-fail row with no tier-1 guess sends exactly ONE bilingual
  closure message quoting the original raw text with the `/log` keyboard, sets the row `closed`, and is never
  re-notified even across repeated recovery sweeps. (R1)
- **AC7** (tier-1 guesses deterministic): `tier1_guesses` returns exact/prefix label+alias+unit matches and
  bare-number unit-plausibility guesses against the acting per-user registry (incl. a custom habit), zero-LLM;
  `"500"`→`(water,500)` only, `"stretch"`/`"stre"`→`(stretch, goal)`, `"Streaching"`→`[]`; capped at
  `max_guesses`. (R5)
- **AC8** (guess offer + state): A recovery-fail row **with** guesses, and a live LLM-unknown message **with**
  guesses, present `clarify:<row>:<habit>:<value>` buttons; the row is `awaiting_clarify` and is excluded from
  every later recovery sweep (LLM not retried). (R6/R7/R8)
- **AC9** (live LLM-unknown): With Ollama UP and `parse_message` unknown — **no** guesses → the generic
  bilingual clarifying question **plus** the `/log` keyboard and **no** pending row; **with** guesses → a fresh
  `awaiting_clarify` row + the offer. `clarify.enabled=false` → generic path always. (R6/R10)
- **AC10** (clarify tap = ordinary log): Tapping a guess reclassifies the row to that habit, sends the
  recovered-style confirmation + Undo + reaction + dashboard refresh, and writes **no audit row**; a payload
  naming a habit the tapping user doesn't own, or an already-resolved/closed row, is a friendly no-op with no
  write. (R9/R12)
- **AC11** (sweep-vs-tap race guard): With a clarify tap and a recovery reclassify racing the same row, exactly
  one commits (guarded CAS) and the other observes `rowcount == 0` and does nothing — no double log, no double
  confirmation, no double notification; two concurrently-triggered sweeps do not co-process the same
  `awaiting_llm` row (single-flight). (R11/R4)

### Module M2 — reply-to-reminder + outage + /guide
- **AC12** (reply-to-reminder logs): Replying to a bot **reminder** message (message id mapped to its habit)
  with a bare positive number logs that value against the reminder's habit, zero-LLM, works while Ollama is
  DOWN, and fires the normal confirmation + Undo + reaction; a boolean habit + an affirmative reply logs `1`. (R13)
- **AC13** (reply-attribution conservatism + degradation): The attribution fires **only** for a mapped reply
  **and** a bare value; a number+unit reply resolving to a different habit, an unmapped reply, a reply to a
  check-in/nudge prompt, or non-value text all take the normal path; after a restart (empty map) a reply to a
  pre-restart reminder falls through with no wrong attribution. (R14)
- **AC14** (outage honesty): Ollama DOWN + preparse miss still writes the `awaiting_llm` deferral row and, with
  `outage.honest_reply=true` (default), sends the bilingual outage message (names number+unit / `/log` /
  `/routine`, quotes the saved text, attaches the `/log` keyboard); with `false` it sends the pre-1.10
  `deferred_ack` byte-for-byte. (R15)
- **AC15** (`/guide`): `/guide` (and `คู่มือ`) returns a compact bilingual getting-started card in one send,
  well under the 4096-char budget; the public menu is 23 and the owner menu is 28. (R16/R17)

### Module M3 — riders
- **AC16** (pause fail-open unified): Injecting a pauses-read error for user A at each of the 5 proactive sites
  (reminders, check-ins, nudge, daily summary, weekly review) leaves A treated as not-paused (its send proceeds
  where applicable) and does **not** abort the run for users B and C — matching `reminders.send_reminder`'s
  reference posture, via the shared `is_paused_safe`/`active_pauses_safe` helpers. (R18/R-SS9)
- **AC17** (pytest-xdist): `pytest-xdist` is a `dev` dependency, `pytest -n auto` is documented, and the full
  suite passes **both** `pytest` and `pytest -n auto` with identical results (no order-dependence);
  `[tool.pytest.ini_options]` retains `testpaths`/`asyncio_mode`. (R19)

### Release
- **AC18** (release notes announced): `RELEASE_NOTES["1.10.0"]` has both `en` and `th`, headed
  `🎉 What's new in v1.10.0` / `🎉 มีอะไรใหม่ใน v1.10.0`, bulleting closure+tap-to-fix, reply-to-reminder,
  outage honesty, and `/guide`; `announce_release` sends it once per active user per version. (§3.7)

> Every §4 behavior rule maps to ≥1 AC: R-SS1→AC1, R-SS2/3/4→AC3, R-SS5→AC2, R-SS6/R13→AC12, R-SS7→AC12/13,
> R-SS8→AC4, R-SS9/R18→AC16, R1/R2→AC5/AC6, R3→AC5, R4/R11→AC11, R5→AC7, R6/R10→AC9, R7/R8→AC8, R9/R12→AC10,
> R14→AC13, R15→AC14, R16/R17→AC15, R19→AC17.

## 9. Risks & open questions

**No blocking open questions** — scope is user-approved (2026-08-27). Decisions taken as defaults (not
load-bearing; user may override):
- **Reminder→habit map is in-memory** (bounded, `context_cap=32/chat), not DB-backed. Rationale: reply-to-reminder
  is a convenience fast-path that degrades safely to the normal logging path when the map is empty; persisting
  every proactive message id would add write amplification to the just-optimized minutely tick for the marginal
  gain of attributing a reply that arrives **after** a process restart (rare — replies come within minutes and
  the bot is long-running). *Alternative if the user wants cross-restart attribution:* a small `reminder_context`
  table (documented, deferred).
- **Bare-number plausibility window** `G*[0.05, 5.0]` is a heuristic (config-tunable). It cleanly separates the
  known cases (`500`→water not stretch) but is a judgment call at the margins; tuning it changes only which
  *guesses* are offered, never a log that gets written.
- **Clarify is offered only when Ollama is UP or at recovery time**, never at deferral time — even though tier-1
  guesses are deterministic and would work offline. Rationale: keep the deferred row purely `awaiting_llm` so the
  LLM gets its fair recovery shot before we hand the user a guess, and keep the outage reply (functional 4) a
  single clear guidance message. (Deliberate; see §10.)

**Risks:**
- **`core/routing.py` is the convergence point** for functionals 1–5. Mitigation: the substantive per-module
  logic lives in disjoint new files (`clarify.py`, `reply_attribution.py`), and all routing edits are a
  **single sequential integration seam** owned by one track (mirrors v1.8's main.py seam) — the parallel modules
  never edit `routing.py`.
- **The race guard is load-bearing** (double-log / double-notify would be user-visible). Mitigation: the guard is
  a deterministic per-row CAS (R11) with an AC (AC11) that exercises the interleaving, plus a single-flight sweep
  guard (R4).
- **`Channel.send` return-type change** touches an ABC used by ~90 fakes. Mitigation: additive (callers ignore
  the return); only the shared `RecordingChannel` is updated; legacy 2-arg fakes are unaffected (AC2).

## 10. Out of scope
- **Tier-2 / fuzzy Thai matching** for clarify guesses (typo tolerance like "Streaching"→stretch) — a later
  release; v1.10 is exact/prefix + unit-plausibility only.
- **Clarify buttons at deferral time (offline)** — deliberately not offered; the outage message is the offline
  response (see §9).
- **DB-persisted reminder→habit context** (survives restarts) — deferred; v1.10 is in-memory.
- **Reply-to-reminder for multi-habit prompts** (check-in / nudge) — ambiguous, excluded by design.
- **Auditing raw logs** (clarify-tap logs, recoveries) — consistent with the existing "audit corrections, not
  logs" rule; unchanged.
- **Forcing `-n auto` via `addopts`** — parallel is documented/opt-in, not the default runner config.
- **Re-parse retry policy tuning** (multiple LLM attempts before closing) — one recovery attempt then close;
  the same text against the same model won't parse on a later identical attempt.

## 11. Module split & parallel development

**Total functionals:** 6 — (1) unparsed closure + terminal state, (2) conservative tap-to-fix clarify,
(3) reply-to-reminder attribution, (4) outage honesty, (5) `/guide`, (6) riders (pause fail-open + pytest-xdist).

**Recommendation:** **PARALLEL — a large sequential shared surface, then 3 parallel modules with disjoint file
ownership, then a sequential `routing.py`/`app.py` integration seam.** Rationale: functionals 1–5 all converge
on `core/routing.py` (`handle_inbound_message`, `reparse_pending_unparsed`, `on_callback`), so that file is
**not** split across tracks — its per-module logic is extracted into disjoint new files (`core/clarify.py`,
`core/reply_attribution.py`) built in parallel, and every `routing.py` edit is the single, one-owner
integration seam at the end (the same shape v1.7/v1.8 used). Functional 6 (riders) is genuinely independent and
runs fully in parallel.

**Shared surface (built first, sequentially — every module depends on part of it):**
- Migration 013 + `LogEntry.unparsed_state` + db methods (`insert_log`, `pending_unparsed`, `resolve_unparsed`,
  `mark_unparsed_state`) — the state machine.
- `Channel.send -> str | None` (base + Telegram) + `RecordingChannel` update.
- `ReminderState` context map + methods + `send_reminder` message-id capture.
- Inbound `reply_to_message_id` plumbing (`run` → `on_message`).
- `pause.is_paused_safe` / `active_pauses_safe`.
- `commands.py` `/guide` matcher + `CommandKind` + reserved words.
- i18n key skeletons (closure/clarify/outage/guide), `RELEASE_NOTES["1.10.0"]`, config sections.

| Module | Owned ACs | Owned files | Depends on |
|---|---|---|---|
| **M1 — closure + clarify** | AC5, AC6, AC7, AC8, AC9, AC10, AC11 | `core/clarify.py` (new), `core/i18n.py` (closure/clarify keys), `tests/test_clarify.py`, `tests/test_unparsed_closure.py` | shared: state machine (db CAS methods), state constants, `provider.for_user`, `quicklog.build_keyboard` (read-only reuse) |
| **M2 — reply + outage + guide** | AC12, AC13, AC14, AC15 | `core/reply_attribution.py` (new), `core/discoverability.py` (`build_guide_text`), `core/i18n.py` (outage/guide keys), `tests/test_reply_to_reminder.py`, `tests/test_outage_honesty.py`, `tests/test_guide.py` | shared: `ReminderState` map, `Channel.send` id, inbound reply plumbing, `/guide` matcher |
| **M3 — riders** | AC16, AC17 | `core/checkins.py`, `core/nudge.py`, `core/streaks.py`, `core/review.py`, `pyproject.toml`, `tests/test_pause_failopen.py` | shared: `pause.is_paused_safe`/`active_pauses_safe` |

ACs verified during the **shared-surface / integration** pass: **AC1, AC2, AC3, AC4, AC18** (migration,
`send` return, CAS state machine, `/guide` recognition+reservation, release notes). Every AC belongs to
exactly one owner. **Total: 18 acceptance criteria** (shared/integration 5, M1 7, M2 4, M3 2).

**Integration order (sequential, after M1/M2/M3 pass):**
1. `core/routing.py`: wire `reparse_pending_unparsed` to M1 (guess/closure + CAS terminal states +
   single-flight); wire `handle_inbound_message` (reply-attribution before preparse; outage message in the
   deferral branch; live LLM-unknown → M1 offer/generic; `/guide` dispatch; `reply_to_message_id` param);
   `on_callback` `clarify:` prefix → `clarify.handle_clarify_callback`.
2. `core/app.py`: add `/guide` to both menus (22→23 / 27→28); thread `reply_to_message_id` and the extended
   `ReminderState`.
3. Full suite green **serially and with `-n auto`**. Highest-value gates: **AC5** (zombie loop killed — the
   release's reason for existing), **AC11** (race guard), **AC12/AC13** (reply attribution + conservatism),
   **AC14** (outage honesty), **AC16** (fail-open unification). Then deploy: verify migration 013 applied,
   `RELEASE_NOTES["1.10.0"]` announced, both menus registered (23/28).
