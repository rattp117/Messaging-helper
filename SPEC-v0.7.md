# Spec — v0.7.0 Multi-Habit Extensibility (the pivot)

> Re-spec of `ROADMAP.md` §"v0.7.0" for pickup, per that section's own note
> ("Sophia should re-spec v0.7 with a full §11 module split when it's picked up").
> Built on released **v0.6.0**. Grounded in the *current* source, which has evolved
> substantially since the roadmap was written: `core/i18n.py` (bilingual catalog +
> language resolution), `core/commands.py` (regex command router), `core/health.py`,
> `storage/migrations.py` (3 migrations, `user_version`), plus the original MVP files.
> Honors the hard constraints: **local-first**, clean **`Channel` seam**, **bilingual
> th+en**, **minimal deps** (none added), **single process**.

---

## 1. Problem statement

Today the habit set `water | stretch | diary` is baked into code in eight places: the
Ollama JSON-schema enum + the fixed value keys (`water_ml`/`stretch_min`/`diary_text`),
the few-shot extraction prompt, `ExtractionResult`'s shape, `core/parser.py`'s per-category
validation, `storage/db.py`'s per-category aggregation methods, `core/reminders.py`'s
category→time wiring, `main.py`'s per-category confirmation branches, and `core/review.py`'s
water/stretch/diary aggregation. Adding "sleep hours" or "meds taken" today means editing all
eight. **v0.7 makes habits data, not code:** a `[[habits]]` array in `config.toml` defines each
habit's `id`, `type` (`numeric | duration | text | boolean`), bilingual `label`, `unit`, optional
`goal`, `reminder_times`, and parse hints; the extraction schema/prompt, validation, storage,
reminders, confirmations, and review are all generated from that list. **Success = (a)** with the
default config (water/stretch/diary expressed as habits) every user-facing string is **byte-identical
to v0.6.0** (AC7.1), and **(b)** adding a new `[[habits]]` entry makes the bot parse, store, confirm,
remind, and review it with **zero code changes** (AC7.2).

Per the user's directed default (`ROADMAP.md` §4 Q5): **ship the existing three habits generalized,
add no new habits speculatively, four value types are sufficient.**

---

## 2. Inputs

### 2.1 `config.toml` — the `[[habits]]` array (new source of truth)

Each habit is an array-of-tables entry. The default config expresses the current three habits so
`Config()` with no file, and the shipped `config.toml`, both reproduce v0.6.0 exactly.

```toml
[[habits]]
id = "water"
type = "numeric"
goal = 2500
reminder_times = ["08:00", "10:30", "13:00", "15:30", "18:00", "20:30"]
label = { en = "water", th = "น้ำ" }
unit  = { en = "ml",    th = "มล." }
[habits.unit_aliases]        # casual-unit → millilitres (was [units].glass_ml/bottle_ml)
glass = 250
"แก้ว" = 250
bottle = 600
"ขวด" = 600

[[habits]]
id = "stretch"
type = "duration"
reminder_times = ["11:00", "16:00"]
label = { en = "stretch", th = "ยืดเส้น" }
unit  = { en = "min",     th = "นาที" }

[[habits]]
id = "diary"
type = "text"
reminder_times = ["21:30"]
label = { en = "diary", th = "ไดอารี่" }
```

A **new** habit (not shipped; shown for AC7.2 and for docs):

```toml
[[habits]]
id = "sleep"
type = "numeric"
goal = 8
unit  = { en = "h",    th = "ชม." }
label = { en = "sleep", th = "นอน" }
reminder_text = { en = "😴 How many hours did you sleep?", th = "😴 เมื่อคืนนอนกี่ชั่วโมง?" }
```

**Field semantics** (validated at load; see §4):

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | str | yes | unique, non-empty, `[a-z0-9_]+`; becomes `logs.category` |
| `type` | `numeric\|duration\|text\|boolean` | yes | drives value handling everywhere |
| `label` | `{en,th}` | yes | both languages required |
| `unit` | `{en,th}` \| omitted | numeric/duration only | display unit; omit for text/boolean |
| `goal` | number \| omitted | optional (numeric/duration) | daily goal; enables the `X / goal (%)` confirmation form |
| `reminder_times` | list[str `"HH:MM"`] | optional (default `[]`) | one cron job per entry |
| `reminder_text` | `{en,th}` \| omitted | optional | custom reminder copy for a **new** habit; built-ins ignore it (they reuse their v0.6 catalog entry) |
| `unit_aliases` | table `{alias: multiplier}` | optional | casual units → base-unit multiplier (water's glass/bottle) |

### 2.2 Inbound message (unchanged shape)

A free-form Thai/English string, e.g. `"ดื่มน้ำ 2 แก้ว"`, `"500ml"`, `"did 10 min stretch"`,
`"นอน 7 ชม."`, `"took my meds"`. Delivered to `handle_inbound_message(text, …)` exactly as today.

### 2.3 Existing DB (`data/habits.db`)

Live DB is at `user_version = 3`, columns `id, ts, category, value_num, value_text, raw_message,
source, created_at, deleted_at`. Rows may carry categories `water|stretch|diary|unparsed` (currently
0 rows; historical/seeded DBs have real rows). Migration 004 must carry any such rows forward
losslessly (AC7.5).

---

## 3. Outputs

### 3.1 Confirmations — byte-identical for the three built-ins (AC7.1)

Resolved through `core/i18n.py` exactly as v0.6.0. **Built-in habits reuse their existing catalog
entries verbatim** (guaranteeing byte-identical output); the four examples below are the exact current
strings and must not change:

```
water   (en): ✅ 500 ml logged — today 500 / 2500 ml (20%)
water   (th): ✅ บันทึกน้ำ 500 มล. แล้ว — วันนี้ดื่มไป 500 / 2500 มล. (20%)
stretch (en): ✅ 10 min stretch logged — 1st today
diary   (th): ✅ บันทึกแล้วนะ <reflection>
```

### 3.2 Confirmations — type-generic for a new habit (AC7.2, AC9-generic)

A habit **without** a bespoke catalog entry renders via a neutral, type-driven template parameterized
by `label` + `unit`:

```
sleep   (en, numeric+goal): ✅ 7 h logged — today 7 / 8 h (88%)
sleep   (th, numeric+goal): ✅ บันทึกนอน 7 ชม. แล้ว — วันนี้ 7 / 8 ชม. (88%)
steps   (en, numeric no-goal): ✅ 8000 steps logged today
meds    (en, boolean):      ✅ meds — done today
```

### 3.3 Reminders / weekly review

Reminders: built-ins reuse `reminder_water/stretch/diary` catalog entries (byte-identical); a new
habit uses its `reminder_text` or a generic `reminder_generic` template. Weekly review (default config
resolves to **Thai** for this unprompted send) is byte-identical to v0.6.0 for the three, and includes
any new habit generically.

### 3.4 Error / unmatched

An input matching no configured habit → `ExtractionResult.unknown()` → the existing bilingual
clarifying question, no row (unchanged). Invalid config (`[[habits]]` with a bad `id`, missing label,
`goal` on a text habit, etc.) → `ConfigError` at startup with a clear message (existing pattern).

---

## 4. Behavior rules

**Config & registry**

1. `Config.habits: list[HabitConfig]` is the single source of truth. Its **default value** (used when
   `config.toml` is absent or omits `[[habits]]`) is exactly the three built-ins above, so `Config()`
   is behaviourally identical to v0.6.0.
2. Validation at load (raise `ConfigError`, existing style): `id` matches `^[a-z0-9_]+$` and is unique
   across the array; `label.en`/`label.th` both non-empty; `unit` present iff `type ∈ {numeric,duration}`;
   `goal` allowed only for `numeric|duration`; `reminder_times` entries match `HH:MM`. The reserved ids
   `unknown` and `unparsed` may **not** be used as a habit id.
3. `HabitRegistry.from_config(config)` builds an ordered registry from `config.habits`.
   `registry.get(id)`→`Habit|None`; `registry.ids()`→ordered habit ids; `registry.category_enum()`→
   `ids() + ["unknown"]`; iteration yields `Habit` objects in config order.

**Extraction schema & prompt (generated from the registry)**

4. `build_extraction_schema(category_enum)` returns a JSON schema with `category` (enum = habit ids +
   `"unknown"`), a single `value` field typed `["number","string","boolean","null"]`, and `confidence`
   (`number`), all three `required`. The schema size is **independent of habit count** (one `value`
   field, not one per habit) — deliberate, to avoid growing the payload the schema-weak MLX backend
   already struggles with (see `IMPL.md` v0.1.0/v0.2.0 findings; the v0.2 fallback chain remains the
   safety net).
5. `build_extraction_system_prompt(registry)` generates the system prompt dynamically: a categories
   block (one line per habit: `id`, human description from `label.en`, and for numeric/duration its
   `unit` + any `unit_aliases` with multipliers), the "explicit unit wins" rule, and 1–2 few-shot
   examples per habit covering its type. For the default three the generated prompt must preserve
   current extraction quality (water ml/glass/bottle, stretch minutes, diary text, unknown).
6. `chat_json(system, user, schema, valid_categories)` and `probe_schema_support(system, user, schema)`
   take the valid category set / built prompt as parameters instead of module globals. The lightweight
   fallback gate `_has_recognizable_category(data, valid_categories)` accepts any `category` in
   `valid_categories` (habit ids **and** `"unknown"`), unchanged in spirit from v0.2.

**Parsing & per-type validation (AC7.3, AC7.4)**

7. `parse_message(text, llm, registry, confidence_threshold)` builds the schema+prompt from `registry`,
   calls `chat_json`, then `_validate(data, registry, threshold)`. Fails closed to
   `ExtractionResult.unknown()` on any error; never raises (unchanged contract).
8. `_validate`: `category` must be in `registry.ids()` else `unknown`. Then per the matched habit's
   `type`, coerce/validate `value`:
   - **numeric / duration** — coerce to a number (accept `500`, `"500"`, `7.5`); reject `≤ 0` → `unknown`.
   - **text** — require a non-empty string (after `strip()`) → else `unknown`.
   - **boolean** — coerce to bool: truthy = `true`/`1`/`"done"`/`"yes"`/`"ครบ"`/`"แล้ว"`;
     falsy = `false`/`0`/`"no"`/`"ยัง"`. A value that can't be coerced → `unknown`.
   - Then the existing confidence gate: a genuinely-numeric `confidence < threshold` → `unknown`
     (v0.2 AC2.3 behavior, preserved).
9. `ExtractionResult` becomes generic: `category: str`, `value: float | str | bool | None`,
   `confidence: float`. `unknown()` = `("unknown", None, 0.0)`.

**Storage mapping (AC7.5)**

10. `log_entry_from_result(habit, result, ts, raw_message, source)` maps a validated result to a
    `LogEntry`: `category = habit.id`, `habit_type = habit.type`; **numeric/duration** →
    `value_num = float(value)`, `value_text = None`; **text** → `value_text = str(value)`,
    `value_num = None`; **boolean** → `value_num = 1.0 if value else 0.0`, `value_text = None`.
11. Migration **004** (append-only, additive): `ALTER TABLE logs ADD COLUMN habit_type TEXT NULL`,
    then backfill existing rows: `water→'numeric'`, `stretch→'duration'`, `diary→'text'`; leave
    `unparsed`/`unknown`/any other category `NULL`. `value_num`/`value_text` of existing rows are
    **untouched** (they already sit in the right column). A fresh DB reports `schema_version == 4`.
12. `Database` gains generic, soft-delete-aware aggregations, all filtering `deleted_at IS NULL` and
    `ts LIKE '{day}%'`: `sum_value(habit_id, day)→float` (`SUM(value_num)`), `count(habit_id, day)→int`
    (`COUNT(*)`), `count_true(habit_id, day)→int` (`COUNT(*) WHERE value_num != 0`, for boolean).
    The v0.6 methods `water_total_ml`/`stretch_count`/`diary_count` are retained as thin wrappers over
    these (so nothing that still calls them changes behavior). `insert_log` writes the new
    `habit_type` column.

**Confirmations (AC7.1 built-ins, AC9 generic)**

13. `handle_inbound_message` resolves `habit = registry.get(result.category)` then dispatches on
    `habit.type`. For a **built-in** id (`water|stretch|diary`) it uses that habit's existing v0.6
    catalog entry with the same params → **byte-identical**. For any **other** habit it uses the
    type-generic template:
    - numeric **with** goal → `confirm_numeric_goal` (value, unit, today-total via `sum_value`, goal, pct).
    - numeric **without** goal → `confirm_numeric_nogoal` (value, unit).
    - duration → `confirm_duration` (value, unit, label, ordinal from `count`).
    - text → `confirm_text` (LLM reflection, same diary-reflection path).
    - boolean → `confirm_boolean` (label, done/not-done).
14. Undo, edit, `_describe_log`, and `reparse_pending_unparsed` follow the same built-in-vs-type-generic
    resolution. Undo/edit still operate on `value_num` for numeric/duration (edit of text/boolean is out
    of scope, §10). Recovery (`reparse_*`) re-parses each `unparsed` row through the new `parse_message`
    and reclassifies via `reclassify_log(..)` (now also stamping `habit_type` — extend
    `reclassify_log` to set `habit_type`).

**Reminders (AC8)**

15. `schedule_reminders(scheduler, channel, config, registry)` iterates **every** habit in the registry
    and schedules one cron job per `reminder_times` entry, binding `(channel, habit, language)`. A habit
    with empty `reminder_times` schedules nothing. `send_reminder(channel, habit, language)` resolves
    copy: built-in id → its `reminder_water/stretch/diary` catalog entry (byte-identical); else
    `habit.reminder_text` if set; else generic `reminder_generic` with `label`. `--test-reminder`'s
    choices come from `registry.ids()`.

**Review (AC10, AC7.5)**

16. `compute_weekly_stats(db, config, registry, end_date)` aggregates **per habit** over the 7-day
    window using the generic db methods, producing per-habit stats appropriate to type
    (numeric/duration → per-day totals/goal%/streak like water/stretch do today; text/boolean → counts).
    `format_stats_summary(stats, registry, lang)` renders each habit's block. For the default three the
    rendered Thai summary is **byte-identical** to v0.6.0 (built-ins reuse the existing
    `stats_water_*`/`stats_stretch_summary`/`stats_diary_summary` catalog entries with the same params;
    the per-habit generic path is used only for new habits). The narrative + "no medical advice"
    constraint are unchanged.

**Cross-cutting**

17. No module in `core/` or `storage/` imports a concrete channel (seam preserved). No new dependency.
    The whole feature is a single process, `asyncio`, unchanged.

---

## 5. Interfaces (signatures)

These are the **cross-track contract**. The shared surface fixes them; the parallel modules implement
their side; callers are wired in the shared surface. All type hints `from __future__ import annotations`.

```python
# --- config.py (SHARED) -----------------------------------------------------
class HabitLabel(BaseModel):
    en: str
    th: str

class HabitConfig(BaseModel):
    id: str
    type: Literal["numeric", "duration", "text", "boolean"]
    label: HabitLabel
    unit: HabitLabel | None = None
    goal: float | None = None
    reminder_times: list[str] = []
    reminder_text: HabitLabel | None = None
    unit_aliases: dict[str, float] = {}
    # model_validator: id regex+reserved-word check; unit required iff numeric/duration;
    # goal only for numeric/duration; reminder_times HH:MM.

class Config(BaseModel):
    ...                                   # existing fields kept (reminders/units retained, unused)
    habits: list[HabitConfig] = [ <water>, <stretch>, <diary> ]   # defaults reproduce v0.6.0

# --- core/habits.py (SHARED, NEW) -------------------------------------------
@dataclass(frozen=True, slots=True)
class Habit:
    id: str
    type: str                     # 'numeric'|'duration'|'text'|'boolean'
    label_en: str
    label_th: str
    unit_en: str | None
    unit_th: str | None
    goal: float | None
    reminder_times: tuple[str, ...]
    reminder_text_en: str | None
    reminder_text_th: str | None
    unit_aliases: dict[str, float]
    def label(self, lang: i18n.Language) -> str: ...
    def unit(self, lang: i18n.Language) -> str | None: ...

class HabitRegistry:
    def __init__(self, habits: Sequence[Habit]) -> None: ...
    @classmethod
    def from_config(cls, config: Config) -> "HabitRegistry": ...
    def get(self, habit_id: str) -> Habit | None: ...
    def ids(self) -> list[str]: ...
    def category_enum(self) -> list[str]: ...          # ids() + ["unknown"]
    def __iter__(self) -> Iterator[Habit]: ...

def log_entry_from_result(habit: Habit, result: ExtractionResult,
                          ts: str, raw_message: str, source: str) -> LogEntry: ...

BUILTIN_IDS: frozenset[str] = frozenset({"water", "stretch", "diary"})  # reuse v0.6 catalog copy

# --- llm/ollama_client.py (SHARED) ------------------------------------------
@dataclass(slots=True)
class ExtractionResult:
    category: str
    value: float | str | bool | None
    confidence: float
    @classmethod
    def unknown(cls) -> "ExtractionResult": ...

def build_extraction_schema(category_enum: list[str]) -> dict[str, Any]: ...

class OllamaClient:
    async def chat_json(self, system_prompt: str, user_prompt: str,
                        json_schema: dict, valid_categories: set[str]) -> str | None: ...
    async def probe_schema_support(self, system_prompt: str, user_prompt: str,
                                   json_schema: dict) -> dict[str, bool]: ...

# --- storage/models.py (SHARED) ---------------------------------------------
@dataclass(slots=True)
class LogEntry:
    id: int | None
    ts: str
    category: str
    value_num: float | None
    value_text: str | None
    raw_message: str
    source: str = "reply"
    created_at: str | None = None
    habit_type: str | None = None          # NEW

# --- storage/db.py (SHARED) -------------------------------------------------
class Database:
    def sum_value(self, habit_id: str, day: str) -> float: ...
    def count(self, habit_id: str, day: str) -> int: ...
    def count_true(self, habit_id: str, day: str) -> int: ...
    def reclassify_log(self, log_id, category, value_num, value_text, habit_type=None) -> None: ...
    # water_total_ml/stretch_count/diary_count kept as wrappers; insert_log writes habit_type

# --- core/parser.py (MODULE M1) ---------------------------------------------
async def parse_message(text: str, llm: OllamaClient, registry: HabitRegistry,
                        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> ExtractionResult: ...
def _validate(data: dict, registry: HabitRegistry, confidence_threshold: float) -> ExtractionResult: ...

# --- llm/prompts.py (MODULE M1) ---------------------------------------------
def build_extraction_system_prompt(registry: HabitRegistry) -> str: ...
def build_extraction_user_prompt(message: str) -> str: ...

# --- core/commands.py (MODULE M1) -------------------------------------------
def dispatch(text: str, registry: HabitRegistry) -> Command | None: ...   # edit value→habit via registry

# --- core/reminders.py (MODULE M2) ------------------------------------------
async def send_reminder(channel: Channel, habit: Habit, language: i18n.Language = "en") -> None: ...
def schedule_reminders(scheduler, channel: Channel, config: Config, registry: HabitRegistry) -> None: ...

# --- core/review.py (MODULE M3) ---------------------------------------------
def compute_weekly_stats(db: Database, config: Config, registry: HabitRegistry, end_date: date) -> WeeklyStats: ...
def format_stats_summary(stats: WeeklyStats, registry: HabitRegistry, lang: i18n.Language = "en") -> str: ...
async def run_weekly_review(db: Database, config: Config, registry: HabitRegistry,
                            llm: OllamaClient, today: date | None = None) -> str: ...

# --- main.py (SHARED) -------------------------------------------------------
# async_main builds registry = HabitRegistry.from_config(config); builds schema; probes;
# threads registry into parse_message / schedule_reminders / run_weekly_review;
# handle_inbound_message + reparse_pending_unparsed do the built-in-vs-type-generic dispatch.
```

**New `core/i18n.py` catalog entries** (data, SHARED; en+th each): `confirm_numeric_goal`,
`confirm_numeric_nogoal`, `confirm_duration`, `confirm_text`, `confirm_boolean`,
`undo_removed_numeric`/`_duration`/`_boolean`, `edit_updated_numeric`/`_duration`,
`describe_log_numeric`/`_duration`/`_boolean`, `recovered_numeric`/`_duration`/`_boolean`,
`reminder_generic`, `stats_generic_numeric_header`/`_line`/`_total`, `stats_generic_duration_summary`,
`stats_generic_count_summary`. Existing built-in entries (`water_confirmation`, `reminder_water`,
`stats_water_*`, …) are **unchanged**.

---

## 6. Files to touch

**Shared surface (built first, sequentially):**
- `config.py` — add `HabitLabel`, `HabitConfig`, validators, `Config.habits` (+ default = the three).
- `config.toml` — add `[[habits]]` for water/stretch/diary (drop `[units]`/`[reminders.*]` usage; may leave the sections, they're ignored).
- `core/habits.py` — **new**: `Habit`, `HabitRegistry`, `log_entry_from_result`, `BUILTIN_IDS`.
- `storage/models.py` — `LogEntry.habit_type`.
- `storage/migrations.py` — append `_migration_004_habit_type` (ADD COLUMN + backfill).
- `storage/db.py` — `sum_value`/`count`/`count_true`; `insert_log` writes `habit_type`; `reclassify_log` stamps `habit_type`; keep v0.6 methods as wrappers.
- `llm/ollama_client.py` — generic `ExtractionResult`; `build_extraction_schema`; `chat_json`/`probe_schema_support`/`_has_recognizable_category` take params instead of globals.
- `core/i18n.py` — add the type-generic catalog templates (built-in entries unchanged).
- `main.py` — build registry/schema; thread `registry` through all call sites; generic confirmation + reparse dispatch; `--test-reminder` choices from `registry.ids()`.

**Module M1 — Extraction:**
- `llm/prompts.py` — `build_extraction_system_prompt(registry)` + user template.
- `core/parser.py` — generic `parse_message`/`_validate` (per-type).
- `core/commands.py` — `dispatch(text, registry)`; edit value+unit → habit via registry aliases/units.

**Module M2 — Reminders:**
- `core/reminders.py` — registry-driven `schedule_reminders`; type/id-aware `send_reminder`.

**Module M3 — Review:**
- `core/review.py` — per-habit generic aggregation + `format_stats_summary`.

**Tests** (owned per track, see §11): `test_config.py`, `test_migrations.py`, `test_db.py`,
`test_confirmations.py`, `test_bilingual_confirmations.py`, `test_habits.py` (new) → shared;
`test_parser.py`, `test_commands.py`, `test_fallback.py` → M1; `test_reminders.py` → M2;
`test_review.py` → M3; `test_multi_habit_integration.py` (new) → integration.

---

## 7. External dependencies

**None added.** Everything is stdlib (`sqlite3`, `re`, `json`) plus the already-present `pydantic`
(config models) and `httpx`/`apscheduler`. `tomllib` already parses `[[habits]]` arrays natively. This
honors the minimal-deps constraint; no justification needed because nothing new is introduced.

---

## 8. Acceptance criteria

Each AC maps to exactly one track (module) — see §11. Cross-references to `ROADMAP.md` ACs 7.1–7.5 are
noted. Every behavior rule in §4 is covered.

**Shared surface (verified before fan-out):**
- **AC1 [→R4.1, R4.2]:** Given a `config.toml` with the three `[[habits]]` (and given no file at all),
  When config loads, Then `Config().habits` and the loaded habits are equal in `id/type/label/unit/goal/
  reminder_times/unit_aliases`; a habit with a bad id (`"UP"`, `"unknown"`, `"a b"`), a missing label
  language, `unit` on a `text` habit, or `goal` on a `text` habit raises `ConfigError`. *(covers R1,R2)*
- **AC2 [→R3]:** Given a loaded `Config`, When `HabitRegistry.from_config(config)` runs, Then `get("water")`
  returns a numeric `Habit` with goal 2500 and aliases `{glass:250, แก้ว:250, bottle:600, ขวด:600}`;
  `ids()==["water","stretch","diary"]`; `category_enum()==[...,"unknown"]`; `get("nope")` is `None`.
- **AC3 [→R11, AC7.5]:** Given a DB created by v0.1.0 with rows `water/stretch/diary/unparsed` (real
  `value_num`/`value_text`), When it opens under v0.7, Then `schema_version` goes `3→4`, a `habit_type`
  column exists with `numeric/duration/text/NULL` respectively, and **every** pre-existing row's
  `value_num`/`value_text`/`category`/`ts` is byte-for-byte unchanged (row count identical). A fresh DB
  reports `schema_version==4`. Re-running migrates nothing (idempotent).
- **AC4 [→R12]:** Given seeded logs across habits/days including one soft-deleted row, When
  `sum_value/count/count_true` run, Then results equal hand-computed values and exclude the soft-deleted
  row; `water_total_ml`/`stretch_count`/`diary_count` return identical numbers to v0.6.0.
- **AC5 [→R4]:** Given the default registry, When `build_extraction_schema(registry.category_enum())`
  runs, Then `category.enum == ["water","stretch","diary","unknown"]`, `value` is
  `type:["number","string","boolean","null"]`, and `required==["category","value","confidence"]`; the
  schema is identical in size for a 3-habit and a 30-habit registry (one `value` field).
- **AC9 [→R13, AC7.1]:** Given a mocked parser returning each built-in result, When
  `handle_inbound_message` runs with the default config, Then the water(en/th), stretch(en/th),
  diary(en/th), and unknown confirmations are **byte-identical** to v0.6.0 (the existing
  `test_confirmations.py`/`test_bilingual_confirmations.py` assertions pass, adjusted only for the new
  `registry` wiring, not for copy). And given a synthetic `numeric+goal` / `numeric no-goal` /
  `duration` / `text` / `boolean` habit, the type-generic templates render per §3.2.

**Module M1 — Extraction:**
- **AC6 [→AC7.3]:** Given the default registry and a mocked LLM, When a message matches a configured
  habit, Then `parse_message` returns that `category`+`value`; When the LLM returns a `category` not in
  `registry.ids()` (or off-schema JSON), Then the result is `unknown` and no row is implied.
- **AC7 [→AC7.4]:** Per-type validation: numeric/duration with `value ≤ 0` → `unknown`; numeric/duration
  with `"7"`/`7.5` → accepted number; text with `""`/whitespace → `unknown`, non-empty → accepted;
  boolean with `"done"`/`true`/`1` → `True`, `"no"`/`0` → `False`, un-coercible → `unknown`; a
  schema-valid result with numeric `confidence < threshold` → `unknown` (v0.2 AC2.3 preserved).
- **AC8 [→R5, R6]:** Given a registry including a new `sleep` habit, When
  `build_extraction_system_prompt(registry)` runs, Then the prompt contains a `sleep` category line with
  its unit and ≥1 few-shot example; and for the default three the generated prompt still yields correct
  live/mocked extraction for water(ml/glass/bottle)/stretch/diary/unknown (no regression vs v0.6).
- **AC12 [→R14 edit]:** Given `dispatch("make that 300ml", registry)` and `dispatch("แก้เป็น 300 มล.",
  registry)`, Then both classify as `edit`, `category="water"`, `value_num=300`; `"edit that to 15 min"`
  → `stretch`, 15; a garbled edit tail → `None`; every message in the existing false-positive corpus
  still → `None` (AC5.5 preserved).

**Module M2 — Reminders:**
- **AC13 [→R15, AC7.1-reminders]:** Given the default config, When `schedule_reminders(…, registry)` runs,
  Then exactly the same 9 cron jobs (6 water, 2 stretch, 1 diary) are registered at the same times, and
  `send_reminder(channel, water_habit, "th")` sends the **byte-identical** v0.6 Thai water reminder.
- **AC14 [→R15]:** Given a new habit with `reminder_times=["07:00"]` and a `reminder_text`, When
  scheduling runs, Then one job is added for it and firing it sends that `reminder_text` in the resolved
  language; a new habit **without** `reminder_text` sends the `reminder_generic` template with its label;
  a habit with empty `reminder_times` schedules zero jobs.

**Module M3 — Review:**
- **AC15 [→R16, AC7.1-review]:** Given the default config and seeded data, When `run_weekly_review`
  runs (default → Thai), Then the stats block is **byte-identical** to v0.6.0 (water per-day %/total,
  stretch count+streak, diary count) and the narrative path (LLM + fallback + "no medical advice") is
  unchanged.
- **AC16 [→R16, AC7.5-review]:** Given seeded pre-v0.7 rows, When the review aggregates them, Then the
  numbers are unchanged from v0.6.0; and given a configured `sleep` (numeric+goal) habit with data, the
  review includes a `sleep` block using the generic numeric template.

**Integration (final Vera pass):**
- **AC11 [→AC7.2]:** Given the shipped code and a `config.toml` that **adds** a `sleep` habit
  (`numeric`, unit `h`, goal 8) with **no code change**, When `"นอน 7 ชม."` arrives, Then it is parsed
  as `sleep`/7, stored (`category='sleep'`, `habit_type='numeric'`, `value_num=7`), confirmed against
  goal, and appears in the weekly review — end-to-end through the real parser/reminders/review.
- **AC17 [→AC7.1 composite]:** Given the default config, When the full suite runs, Then confirmations,
  reminders, and the weekly review are **jointly** byte-identical to v0.6.0 (the pre-v0.7 test corpus
  passes with only registry-wiring edits, never copy edits).

---

## 9. Risks & open questions

**No open question blocks the default path.** The user's directed default (ship the three generalized,
four types, add nothing) is fully specified above. The items below are design notes / low-risk defaults
already chosen — surfaced for Archi's awareness, not requiring an answer to start:

1. **Shared surface dominates (~65–70% of the work); parallel savings are modest.** This matches
   `ROADMAP.md`'s own warning ("Strongly SEQUENTIAL internally … the one candidate for a split once its
   shared surface is built first"). The three parallel leaves (M1/M2/M3) are genuinely independent and
   each has its own test file, so the fan-out is real but bounded. **A SEQUENTIAL build is a defensible
   alternative** if the team prefers lower coordination overhead; I recommend PARALLEL per the task
   brief and because the leaves don't share files. *Default: PARALLEL (§11).*
2. **Byte-identical guaranteed by construction, not by re-derivation.** Built-in habits **reuse their
   existing v0.6 catalog entries unchanged** (confirmation/reminder/stats), rather than reconstructing
   that copy from generic templates. This is a deliberate de-risking choice: it makes AC7.1 provable and
   avoids Thai-verb hazards (water's `ดื่มไป`/`เหลือ` are habit-specific and a neutral template can't
   reproduce them). New habits use neutral type templates. *Default: keep built-in copy in the catalog,
   keyed by `BUILTIN_IDS`.*
3. **`habit_type` column vs registry-only typing.** A row's type is derivable from `registry.get(category)`,
   so the column is technically redundant — but it makes AC7.5's "map existing rows to the new
   representation" a concrete, testable migration and keeps rows self-describing if a habit is later
   removed from config. *Default: add the column and backfill (§4 R11).* 
4. **Generic `value` union type on the schema-weak MLX backend.** A single `["number","string","boolean",
   "null"]` field is easier for the model than N per-habit keys, but the MLX backend already ignores
   `format` (documented). The v0.2 fallback chain + strict `_validate` remain the safety net; per-type
   coercion in `_validate` tolerates the model returning a stringified number. *No action; monitor live.*
5. **Boolean and numeric-no-goal types have no shipped habit.** Their parse/validate/confirm/review
   paths are spec-defined but only exercised by a synthetic habit in unit + integration tests, not by a
   real default. This is inherent to "add none speculatively." *Default: cover them with a synthetic
   test habit; do not ship one.*
6. **Edit disambiguation across same-unit habits.** `commands.dispatch` maps an edit value's unit → a
   habit; if two duration habits both use `min`, the mapping is ambiguous. The default three have no
   collision (only stretch uses `min`, only water uses ml/glass/bottle). *Default: first-match in
   registry order; document; revisit only if a user configures a collision.*

---

## 10. Out of scope

- **No new shipped habits.** Only water/stretch/diary, generalized. `sleep`/`meds`/`steps` appear only
  as examples and test fixtures.
- **Editing text/boolean values** via the command router (edit stays numeric/duration, as v0.5).
- **NL queries, adaptive reminders, streaks, charts, Garmin** — those are v0.8–v1.0 and are *enabled by*
  this pivot but not built here.
- **Per-habit confirmation copy in config** (bespoke templates for arbitrary habits). New habits get
  type-generic copy only; richer per-habit copy is a future enhancement.
- **Hot-reload of `config.toml`.** Adding a habit requires a process restart (matches every other config
  value today).
- **Dropping the legacy `[units]`/`[reminders.*]` config sections and their pydantic models.** They are
  retained (ignored) for backward-compatible loading; removing them is future cleanup.
- **Removing/renaming a habit that has historical rows** (orphaned-category review rendering beyond the
  `habit_type` fallback).

---

## 11. Module split & parallel development

**Total functionals:** 10 — config/registry, dynamic schema, dynamic prompt, per-type validation,
migration 004 + generic storage, generic confirmations, generic undo/edit/reparse, generic reminders,
generic review, command-router generalization. (> 5 → PARALLEL considered.)

**Recommendation:** **PARALLEL** — after a large shared surface is built sequentially, three leaf
modules with **disjoint file ownership** fan out. (See §9 Risk 1: SEQUENTIAL is a defensible fallback;
PARALLEL is recommended because the leaves share no files and each carries its own test file.)

### Shared surface — built first, sequentially, before any module starts

The registry + storage + schema + catalog + `main.py` dispatch. Everything the three leaves compile
against. Its own verification (unit-scoped, does **not** need the leaves — confirmations are tested with
a **mocked** `parse_message`, exactly as `test_confirmations.py` already does):

- `config.py`, `config.toml` — `[[habits]]` model + defaults + validators. **(AC1)**
- `core/habits.py` — `Habit`, `HabitRegistry`, `log_entry_from_result`, `BUILTIN_IDS`. **(AC2)**
- `storage/models.py`, `storage/migrations.py` (migration 004), `storage/db.py` (generic aggregations +
  `habit_type` write). **(AC3, AC4)**
- `llm/ollama_client.py` — generic `ExtractionResult`, `build_extraction_schema`, parameterized
  `chat_json`/`probe_schema_support`. **(AC5)**
- `core/i18n.py` — type-generic catalog templates (built-in entries untouched).
- `main.py` — registry/schema wiring; built-in-vs-type-generic confirmation + reparse dispatch;
  `--test-reminder` from `registry.ids()`. **(AC9)**

**Exit gate for the shared surface:** AC1–AC5 + AC9 green; full pre-v0.7 suite passes with only
registry-wiring edits to callers; live DB migration `3→4` dry-run verified on a copy.

### Parallel modules (one Luna+Vera pair each)

| Module | Owned ACs | Owned files (exclusive) | Depends on |
|---|---|---|---|
| **M1 Extraction** | AC6, AC7, AC8, AC12 | `llm/prompts.py`, `core/parser.py`, `core/commands.py`; tests `test_parser.py`, `test_commands.py`, `test_fallback.py` | shared: `HabitRegistry`, `build_extraction_schema`, `ExtractionResult`, `chat_json` contract |
| **M2 Reminders** | AC13, AC14 | `core/reminders.py`; test `test_reminders.py` | shared: `HabitRegistry`, `Habit`, i18n reminder templates |
| **M3 Review** | AC15, AC16 | `core/review.py`; test `test_review.py` | shared: `HabitRegistry`, `Database.sum_value/count/count_true`, i18n stats templates |

Every AC belongs to exactly one track: AC1–AC5, AC9, AC17→shared/integration; AC6/7/8/12→M1;
AC13/14→M2; AC15/16→M3; AC11→integration. No AC is shared between two modules. File ownership is
disjoint (M1/M2/M3 touch no common file; `main.py`, `core/i18n.py`, `storage/*`, `config.*`,
`core/habits.py`, `llm/ollama_client.py` are shared-surface-owned and frozen before fan-out).

**Shared surface (built first, sequentially):**
- The `[[habits]]` config model + `HabitRegistry` (`core/habits.py`).
- The dynamic extraction **schema** builder + generic `ExtractionResult` (`llm/ollama_client.py`).
- Migration 004 + generic storage aggregations (`storage/`).
- The type-generic i18n catalog templates (`core/i18n.py`).
- `main.py` registry wiring + the built-in-vs-type-generic confirmation/reparse dispatch.

*(Note: the dynamic **prompt** builder and per-type **validation** live in M1, not the shared surface —
they depend only on the frozen `HabitRegistry` contract and touch M1-owned files.)*

**Integration order (after M1/M2/M3 each pass):**
1. Wire the real `parse_message` (M1), `schedule_reminders`/`send_reminder` (M2), and
   `run_weekly_review` (M3) into `main.py`'s already-generic call sites; run the full suite.
2. **AC17** — assert composite byte-identical for the default config across confirmations + reminders +
   review (pre-v0.7 corpus passes with only wiring edits).
3. **AC11** — with a temporary `config.toml` that adds a `sleep` habit (no code change), drive
   `"นอน 7 ชม."` end-to-end (parse → store `habit_type='numeric'` → goal confirmation → weekly review);
   also add a synthetic `boolean` habit to exercise the untested-by-default boolean path.
4. Live smoke (Ollama reachable): default config extraction unchanged for water/stretch/diary; the added
   `sleep` habit parses live. Confirm the running production bot and `data/habits.db` are untouched
   (work on temp copies), then release as **v0.7.0** (MINOR bump).
