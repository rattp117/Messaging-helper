# Spec — v1.7.0: Per-user custom habits

## 1. Problem statement
An active user defines their own tracker from chat ("reading, minutes, goal 30/day", or a Thai equivalent),
and from that moment **everything the bot does for a built-in habit works for their custom habit** —
free-text + preparse logging, undo button, `/target`, `/remind` + reminders, streaks + milestones, daily
summary, weekly review + charts, `/habits` `/history` `/heatmap` `/records` `/trends`, check-ins + nudge,
dashboard. Those surfaces were all deliberately built **registry-generic**, so the core of this feature is
**making the `HabitRegistry` per-user** cleanly (base catalog + the user's own definitions), rebuilding the
per-user matchers/unit-lookups/LLM-prompt without a restart, and **auditing every registry consumer** for a
hidden assumption of the global catalog (§11 checklist — this is where the release lives or dies).

Additive: **migration 010** adds one table (`user_habits`). **Owner and existing users see zero change until
they create a habit** (a user with no `user_habits` rows resolves to a registry byte-identical to today, so
the full **3039-test suite stays green** — AC-5). Bilingual EN/TH; per-user isolated; a v1.7.0 release-notes
entry ships at release.

SemVer: **1.7.0 (MINOR)** — additive, opt-in-by-creation; no change to any existing user's behavior.

## 2. Inputs

### 2.1 Habit-definition commands (deterministic, LLM-free, whole-message-anchored; Thai aliases)
Minimal complete set (rename/retype/unarchive deferred — §10):
```
/addhabit id=reading | type=duration | en=reading | th=อ่านหนังสือ | unit=min/นาที | goal=30
/addhabit ...                # (Thai alias: เพิ่มนิสัย)   — usage reply with a copy-paste example on bad input
/delhabit reading            # archive (history kept) or hard-delete if it has no logs   (Thai: ลบนิสัย)
/habits                      # already lists habits — now shows the user's custom (active) habits too
```
**Grammar** (pipe-separated `key=value`, order-independent, LLM-free, stateless): `id=`, `type=` required;
`en=` required, `th=` optional (defaults to `en=`); `unit=<en>[/<th>]` required for `numeric`/`duration`;
`goal=<n>` optional (numeric/duration); `alias=<tok>:<mult>,…` optional. A guided multi-step wizard is
deferred (§10) — it needs conversational state this stateless bot doesn't have.

### 2.2 Data source / registry
- **Base catalog** = `config [[habits]]` (shared, unchanged).
- **Per-user definitions** = `user_habits` (NEW table, migration 010).
- **Per-user registry** = base Habits + the user's own **active** (non-archived) rows.

## 3. Outputs

### 3.1 Create / delete confirmations (bilingual)
```
# /addhabit reading …
✅ Added "reading" (อ่านหนังสือ) — duration in min, goal 30/day. Log it like "20 min" or use /remind reading.

# /delhabit reading  (has history)
🗄️ Archived "reading" — it's hidden now, but your past entries stay in /history.
# /delhabit reading  (no logs yet)
🗑️ Removed "reading".

# validation error (e.g. id collides / label is a command word / cap reached)
🤔 Couldn't add that: "help" can't be a habit name (it's a command). Try another name.
```

### 3.2 Everything else — identical to a built-in
Once created, the custom habit renders in every surface exactly like `water`/`stretch`/`diary` (generic
templates), in the user's language, scoped to that user.

## 4. Behavior rules

### Registry architecture (shared surface — the core)
- **R-G1** (per-user registry) `HabitRegistry.for_user(config, db, user_id) -> HabitRegistry` = the base
  `config.habits` Habits (first, order preserved) **plus** a `Habit` built from each of the user's **active**
  `user_habits` rows. A user with **no** rows yields a registry byte-identical to `HabitRegistry.from_config
  (config)` (AC-2/AC-5). Archived rows are excluded from the active registry (but their historical `logs`
  survive, R-C2). The `Habit` dataclass and `HabitRegistry` are unchanged — only the *source* becomes
  per-user.
- **R-G2** (provider + cache, rebuild-without-restart) A process-global `RegistryProvider(config, db)` with
  `.for_user(user_id)` (cached) and `.invalidate(user_id)`. Per-user registries are consulted on **every
  message** (the Thai matcher alternations and the preparse unit-lookup are built from the registry), so the
  provider caches the built `HabitRegistry` per user and **invalidates that user's entry on any habit
  create/archive/delete** — the very next message (and the next scheduler fan-out) rebuilds it with **no
  restart** (AC-3). Invalidation is per-user; one user's change never rebuilds another's. Cache starts empty
  on boot (rebuilt lazily); fail-open (a build error falls back to the base registry, logged).
- **R-G3** (per-user rewiring) Every registry consumer resolves the **acting user's** registry:
  `handle_inbound_message` (built for the inbound `chat_id`), the LLM reparse path (built per deferred row's
  `user_id`), and every scheduler fan-out — `run_due_reminders`, `run_due_checkins`, `run_due_nudges`, the
  weekly-review / daily-summary loops, and the 00:00 dashboard rollover — build a **per-user** registry
  inside their `active_user_ids()` loop (via the provider). Their signatures take the provider (or a
  `registry_for: Callable[[str], HabitRegistry]`) in place of a single global `registry`; byte-identical for
  a user with no custom habits (AC-4/AC-5).
- **R-G4** (no base shadowing — OQ1) A user habit **id may not equal a base habit id** (`water`/`stretch`/
  `diary`) — rejected at validation (R-V1). Recommended default: **forbid shadowing** (a base id keeps its
  special byte-identical catalog copy; shadowing would fork behavior and tangle history). Base ids resolve to
  the base habit for everyone.

### Habit definition, validation & lifecycle (module `habitdef`)
- **R-V1** (id) Normalize (trim, lowercase, spaces→`_`); must match `^[a-z0-9_]+$`, length ≤ 32; **not**
  reserved (`unknown`/`unparsed`), **not** a base id (R-G4), **not** already used by this user (active *or*
  archived — an archived id stays reserved, R-C2), and **not** a command trigger word (R-V3).
- **R-V2** (type/unit/goal) `type ∈ {numeric, duration, text, boolean}`; `unit` required for numeric/
  duration (rejected for text/boolean); `goal` optional, `> 0`, numeric/duration only. Mirrors
  `HabitConfig`'s own validators (§config).
- **R-V3** (label & id collision safety — the zero-false-positive discipline extends to user input) A single
  authoritative `commands.reserved_trigger_words() -> frozenset[str]` (built from the *same literals* the
  command matchers use, so it can't drift) lists every deterministic trigger stem + Thai alias (undo/delete/
  snooze/edit-heads/help/habits/history/audit/lang/quiet/dnd/remind/checkin/dashboard/heatmap/records/trends/
  target/addhabit/delhabit and their Thai forms `ยกเลิก`/`เตือน`/`นิสัย`/…). A habit **id or label (en or th)**
  equal (case-insensitively, stripped) to any of these is **rejected** — a habit named "help" or "เตือน"
  cannot exist. Labels are additionally `re.escape`d wherever injected into the Thai matcher alternations
  (already the case, lines 470/670/884/950), so a label with regex metacharacters can never break dispatch.
  A label duplicating another of the user's **active** habit labels (in the same language) is also rejected
  (extraction ambiguity).
- **R-V4** (unit collision degrades safely) A custom unit token colliding with a base/other-habit unit is
  **excluded from the preparse unit-lookup** by the existing `units.build_unit_lookup` cross-habit-collision
  rule (a colliding token falls through to the LLM path rather than being misattributed) — the v1.5 rule now
  operates over the **per-user** registry (AC-H4). Creation is allowed (the collision just means that one
  token won't preparse); the confirmation may note it.
- **R-V5** (per-user cap) At most **20** active custom habits per user (`config`-tunable) — keeps the LLM
  prompt and the dashboard/heatmap/render budgets bounded. Reaching the cap → a friendly rejection.
- **R-C1** (create) `/addhabit` validates (R-V1–R-V5), inserts a `user_habits` row, calls `provider.
  invalidate(user_id)`, records a `habit_create` audit row (fail-open), and confirms. Never raises.
- **R-C2** (delete semantics — OQ2) `/delhabit <id>`: if the habit has **any** `logs` rows, **soft-archive**
  (set `archived_at`; it leaves the active registry — no more logging/reminders/dashboard/etc. — but its
  historical entries remain visible in `/history` and its id stays reserved); if it has **no** logs,
  **hard-delete** the row (id freed — so an immediate mistake is fully reversible). Records
  `habit_archive`/`habit_delete`, invalidates the cache. **Unarchive/reuse of an archived id is deferred**
  (§10). Recommended default (this spec).

### LLM extraction (per-user prompt)
- **R-L1** `parser.build_extraction_system_prompt(registry)` / `build_extraction_schema(registry.category_
  enum())` already build from the passed registry — with the per-user registry (R-G3) the prompt lists the
  user's habits automatically. Prompt size grows by ≤ the cap (R-V5), so ≤ ~23 habits total — bounded.
  Custom **numeric/duration habits with a (non-colliding) unit log instantly via preparse** — zero LLM call
  (R-V4 / the v1.5 preparse fast path, now per-user) — so most custom logging never reaches the prompt at all.
- **R-L2** (Thai-numeral / full-width-digit preparse — normative lock) `units.VALUE_RE`'s `\d` matches
  Unicode decimal digits and Python's `float()` accepts them, so "๕๐๐ ml" and full-width "５００ml" already
  preparse to `500` with no LLM. This is hereby **spec-normative** (locking the v1.5 Vera finding): a future
  regex change must not regress it (AC-6). No code change — an AC guards it.

### Audit, i18n, release (shared surface)
- **R-A1** `core/audit.py` `ACTIONS` gains `habit_create` / `habit_archive` / `habit_delete`; `audit_view`
  gains their localized labels. `/addhabit`/`/delhabit` record one fail-open audit row each (`source=
  "command"`), same pattern as `execute_checkin`.
- **R-A2** All new copy via `core/i18n.py` (EN+TH); `RELEASE_NOTES["1.7.0"]` (EN+TH) ships for the announce
  step; `/addhabit`/`/delhabit` added to the public `set_my_commands` menu.

## 5. Interfaces (signatures)
```python
# storage/migrations.py
def _migration_010_user_habits(conn) -> None: ...
#   CREATE TABLE user_habits (
#     user_id TEXT NOT NULL, id TEXT NOT NULL, type TEXT NOT NULL,
#     label_en TEXT NOT NULL, label_th TEXT NOT NULL, unit_en TEXT, unit_th TEXT,
#     goal REAL, unit_aliases TEXT, archived_at TEXT,
#     created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
#     PRIMARY KEY (user_id, id))

# storage/db.py
def add_user_habit(self, user_id, row: dict) -> None: ...
def list_user_habits(self, user_id, include_archived=False) -> list[sqlite3.Row]: ...
def get_user_habit(self, user_id, habit_id) -> sqlite3.Row | None: ...        # active or archived
def archive_user_habit(self, user_id, habit_id) -> None: ...
def delete_user_habit(self, user_id, habit_id) -> None: ...
def count_active_user_habits(self, user_id) -> int: ...
def count_logs_for(self, user_id, habit_id) -> int: ...                       # decides archive vs hard-delete

# core/habits.py
@classmethod
def for_user(cls, config, db, user_id: str) -> "HabitRegistry": ...           # R-G1

# core/registry_provider.py  (NEW — shared surface)
class RegistryProvider:
    def for_user(self, user_id: str) -> HabitRegistry: ...                     # cached (R-G2)
    def invalidate(self, user_id: str) -> None: ...

# core/commands.py
def reserved_trigger_words() -> frozenset[str]: ...                           # R-V3 single source
CommandKind = Literal[..., "addhabit", "delhabit"]

# core/habitdef.py  (NEW — module `habitdef`)
def validate_and_normalize(fields: dict, base_registry, user_registry, reserved: frozenset[str], cap: int) -> ...
async def execute_addhabit(command, *, db, provider, config, base_registry, lang, user_id) -> str: ...
async def execute_delhabit(command, *, db, provider, lang, user_id) -> str: ...

# config.py
class HabitsConfig(BaseModel):
    max_per_user: int = 20            # R-V5
```

## 6. Files to touch
**Shared surface (first, sequentially — the bulk):**
- `storage/migrations.py` — migration 010 (`user_habits`).
- `storage/db.py` — `user_habits` CRUD + `count_active_user_habits` + `count_logs_for`.
- `core/habits.py` — `HabitRegistry.for_user`.
- `core/registry_provider.py` — NEW: cache + invalidation.
- `core/commands.py` — `reserved_trigger_words()`; `addhabit`/`delhabit` kind skeletons.
- `core/reminders.py`, `core/checkins.py`, `core/nudge.py` — fan-outs take the provider (per-user registry inside the loop).
- `main.py` — build the acting user's registry in `on_message` + the reparse path; thread the provider into the scheduler fan-outs, weekly-review/daily-summary loops, and the 00:00 dashboard rollover.
- `core/audit.py` — `habit_create`/`habit_archive`/`habit_delete`; `core/audit_view.py` labels.
- `config.py` + `config.toml` — `[habits] max_per_user`.
- `core/i18n.py` — habitdef key-block skeletons; `core/release_notes.py` — `RELEASE_NOTES["1.7.0"]`.

**Module `habitdef` (parallel, after shared surface):**
- `core/habitdef.py` — NEW: validate/normalize + create/archive/delete execution.
- `core/commands.py` — `addhabit`/`delhabit` parsing (pipe `key=value` grammar) *(shared file; disjoint keys)*.
- `core/i18n.py` — habitdef copy (EN+TH) *(shared file; disjoint keys)*.
- `tests/test_habitdef.py` — NEW.

**Verification track `sweep` (parallel with `habitdef`, Vera-owned):**
- `tests/test_custom_habit_sweep.py` — NEW: the §11 cross-feature checklist (two-user custom-vs-base isolation
  across all 17 registry consumers). Exercises the shared-surface registry rewiring by inserting `user_habits`
  rows directly, so it can run in parallel with `habitdef`.

**Integration seam (`main.py`):** route `addhabit`/`delhabit` to `habitdef`; add them to the menu; confirm
every fan-out + confirmation site uses the provider.

## 7. External dependencies
None new. stdlib `sqlite3`, `json` (unit_aliases). No new Telegram API. matplotlib unchanged (heatmap/charts
already registry-generic). Migration 010 additive.

## 8. Acceptance criteria

### Shared / registry
- **AC-1** (migration 010): Given a v1.6 DB at `user_version=9`, migration 010 adds `user_habits`, touches no existing data, is idempotent (stamps 10), and the full 3039-suite stays green. (R-G1 storage)
- **AC-2** (per-user registry): `for_user(config, db, user_id)` = base + the user's active rows; a user with **no** rows == `from_config(config)` byte-identical; archived rows are excluded. (R-G1)
- **AC-3** (rebuild without restart): A `/addhabit` (or `/delhabit`) invalidates the acting user's cached registry so the **next** message and the next fan-out reflect it with no restart; another user's cache is untouched. (R-G2)
- **AC-4** (per-user rewiring): `on_message`, the reparse path, and every scheduler fan-out (reminders/check-ins/nudge/review/summary/dashboard) use the per-user registry; a user with no custom habits is byte-identical to v1.6. (R-G3)
- **AC-5** (owner/existing zero change — regression gate): With no `user_habits` rows, the full 3039-suite stays green and the owner's behavior is byte-identical to v1.6. (R-G1/R-G3)
- **AC-6** (Thai-numeral/full-width lock): "๕๐๐ ml" and full-width "５００ml" preparse to `500` with no LLM; locked spec-normative against future regex regressions. (R-L2)
- **AC-7** (audit vocab): `/addhabit`/`/delhabit` record `habit_create`/`habit_archive`/`habit_delete` (fail-open) with localized `/audit` labels. (R-A1)
- **AC-8** (release notes): `RELEASE_NOTES["1.7.0"]` (EN+TH) exists and is announced. (R-A2)

### Feature — habit definition (`habitdef`)
- **AC-H1** (create): `/addhabit id=reading|type=duration|en=reading|th=อ่านหนังสือ|unit=min/นาที|goal=30` inserts the row, appears in the user's registry immediately, and confirms bilingually (Thai alias too). (R-C1)
- **AC-H2** (validation): id normalization + `^[a-z0-9_]+$` + not-reserved + not-base-id + not-duplicate; type/unit/goal rules; the per-user cap (20); each invalid input → a friendly error, **no write**. (R-V1/R-V2/R-V5)
- **AC-H3** (label/id collision safety): an id or label (en or th) equal to any command trigger word (e.g. "help", "เตือน") is **rejected**; a label containing regex metacharacters can't break `dispatch` (escaped). (R-V3)
- **AC-H4** (unit collision degrades): a custom unit colliding with a base/other-habit unit is excluded from the per-user preparse lookup (falls through to the LLM, never misattributed). (R-V4)
- **AC-H5** (delete semantics): `/delhabit` on a habit with history soft-archives it (gone from active surfaces, still in `/history`, id stays reserved); on a habit with no logs, hard-deletes it (id freed). (R-C2)
- **AC-H6** (`/habits`): lists the user's active custom habits alongside base ones, bilingual, per-user. (R-A2)

### Cross-feature sweep (release-critical checklist)
- **AC-S1** (17-surface two-user isolation): Given user A creates a custom habit and user B has none, A's custom habit **works** and is **visible only to A** — and B's every surface is **unchanged** — across each of: **(1)** free-text/LLM extraction, **(2)** preparse instant logging (numeric/duration + unit, zero-LLM), **(3)** undo button, **(4)** `/edit`, **(5)** `/target`, **(6)** `/remind` + the reminder tick, **(7)** streaks + milestones, **(8)** daily summary, **(9)** weekly review + charts, **(10)** `/habits`, **(11)** `/history`, **(12)** `/heatmap`, **(13)** `/records`, **(14)** `/trends`, **(15)** check-ins, **(16)** the nudge, **(17)** the dashboard. Each surface has its own two-user assertion (A sees the custom habit; B never does; B's output is identical to v1.6). (R-G3/R-X)
- **AC-S2** (per-user LLM prompt): the extraction prompt/schema built for A lists A's custom habit and **not** B's; prompt size stays bounded by the cap (R-V5). (R-L1)

## 9. Resolved decisions & open questions

**Open questions:** none remaining — both resolved by the user on **2026-08-24**, each as the recommended default (already specced; no other changes).
- **OQ1 — Base-habit shadowing (RESOLVED 2026-08-24: forbid).** A user id may not equal a base id
  (`water`/`stretch`/`diary`); base habits keep their byte-identical catalog copy, and goal is already
  per-user via `/target`. Baked into R-G4/R-V1/AC-H2.
- **OQ2 — Delete semantics (RESOLVED 2026-08-24: smart delete).** `/delhabit` **soft-archives when the habit
  has history** (hidden from active surfaces, entries kept in `/history`, id stays reserved) and
  **hard-deletes when it has no logs** (id freed); no unarchive/reuse in v1.7. Baked into R-C2/AC-H5.

**Decisions recorded (defaults; not load-bearing):**
- **Per-user cap = 20** active custom habits (`[habits] max_per_user`, tunable) — bounds prompt/render budgets.
- **`/addhabit` grammar = pipe `key=value`** (deterministic, stateless, LLM-free); a guided wizard is deferred
  (needs conversational state this bot doesn't have).
- **Labels: `en` required, `th` defaults to `en`** — a Latin `th` label is harmless in the Thai matcher
  (escaped + anchored, still zero-false-positive). Both go through the reserved-word check.
- **`/edithabit` (rename/retype) deferred** — goal is already editable per-user via `/target`; rename/retype
  carry history/matcher complexity not worth the v1.7 scope (§10).
- **Custom-habit reminders** use the existing per-user `/remind` machinery (a new habit starts with no
  reminders until the user sets them) — no new reminder storage.

**Risks:**
- **The registry rewiring is the deep, cross-cutting change** — every fan-out signature shifts from a global
  registry to the per-user provider. Mitigation: it is the sequential shared surface, gated hard by **AC-5**
  (owner byte-identical, full suite green) and **AC-S1** (17-surface isolation sweep).
- **A hidden global-catalog assumption in any of the 17 surfaces** is the release-killer — hence the explicit
  AC-S1 checklist with a per-surface two-user test.
- **Label/id collision with a command word** would breach the zero-false-positive discipline — defended by
  the single-source `reserved_trigger_words()` check (R-V3), tested in AC-H3.

## 10. Out of scope
- **Base-habit shadowing/override** (forbidden, OQ1) beyond the per-user `/target` goal that already exists.
- **`/edithabit`** (rename/retype/re-unit) and **unarchive / archived-id reuse** — deferred to a later release.
- A **guided creation wizard** (conversational state) — the pipe `key=value` one-liner is v1.7's surface.
- **Cross-user / shared** custom habits (that's the v1.x "family goals" idea, not this) — strictly per-user.
- **Per-habit LLM tuning** — the extraction prompt is generated, not hand-authored, per habit.

## 11. Module split & parallel development
**Total functionals:** 8 — (1) migration 010 + `user_habits` store, (2) per-user `HabitRegistry` +
provider/cache, (3) per-user rewiring of every consumer + fan-out, (4) `reserved_trigger_words` safety
source, (5) `/addhabit`+`/delhabit` command surface + validation, (6) audit vocab, (7) release notes + menu,
(8) the cross-feature isolation sweep. Above the threshold — but the registry rewiring dominates.

**Recommendation:** **LARGE SEQUENTIAL shared surface, then 2 PARALLEL tracks, then integration.** Because
every registry consumer was built registry-generic, "make it work everywhere" is almost entirely the
**shared-surface registry rewiring** (per-user `for_user` + provider/cache + threading it through
`on_message`, the reparse path, and the six fan-out sites) — this is the deep, risky, interdependent core and
**must be built and made green first, sequentially**. After it lands, two tracks run in parallel on **disjoint
surfaces**:
- `habitdef` (dev) — the `/addhabit`/`/delhabit` command module + validation.
- `sweep` (verification, Vera) — the 17-surface two-user isolation checklist (AC-S1), which exercises the
  shared registry rewiring by inserting `user_habits` rows directly, so it does **not** depend on `habitdef`.

**Shared surface (first):** migration 010 + db `user_habits` methods; `HabitRegistry.for_user` +
`RegistryProvider`; the per-user rewiring of `on_message`/reparse/fan-outs (reminders/checkins/nudge/review/
summary/dashboard); `commands.reserved_trigger_words()`; audit vocab; `[habits] max_per_user`;
`CommandKind`/`Command`/i18n **skeletons**; `RELEASE_NOTES["1.7.0"]`.

| Track | Owned ACs | Owned files | Depends on |
|---|---|---|---|
| `habitdef` | AC-H1, AC-H2, AC-H3, AC-H4, AC-H5, AC-H6 | `core/habitdef.py`, `commands.py` (addhabit/delhabit), `i18n.py` (habitdef keys), `tests/test_habitdef.py` | shared: db `user_habits`, `reserved_trigger_words`, provider invalidation |
| `sweep` | AC-S1, AC-S2 | `tests/test_custom_habit_sweep.py` | shared: `for_user`/provider + per-user rewiring |

ACs verified during the shared-surface / integration pass: **AC-1, AC-2, AC-3, AC-4, AC-5, AC-6, AC-7, AC-8**
(migration + per-user registry + cache + rewiring + owner-zero-change + Thai-numeral lock + audit + release
notes). Every AC belongs to exactly one owner. **Total: 16 acceptance criteria** (shared/integration 8,
`habitdef` 6, `sweep` 2).

**Integration order (after both tracks complete):**
1. `main.py`: route `addhabit`/`delhabit` to `habitdef`; add them to the menu; confirm every fan-out +
   confirmation site resolves the per-user registry via the provider.
2. Full suite; highest-value gates: **AC-5** (owner byte-identical, 3039 green), **AC-S1** (17-surface
   isolation — the release lives or dies here), **AC-3** (rebuild-without-restart), **AC-H3** (label/id
   collision safety).
3. Integration tests: two users end-to-end — A creates "reading" and logs/undoes/targets/reminds/reviews it
   while B (base-only) sees zero trace of it; a habit named "help"/"เตือน" is rejected; `/delhabit` archives
   a habit-with-history and hard-deletes an empty one; a Thai-numeral log preparses.
```
