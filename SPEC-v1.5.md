# Spec — v1.5.0: Hourly check-ins + Do-Not-Disturb + LLM-call minimization + release announcements

## 1. Problem statement
Four additions, all **strictly LLM-free** where new:
1. **Hourly check-ins** — a gentle proactive nudge each hour within a per-user window (default 08:00–20:00)
   inviting the user to log, deterministic and bilingual, integrated into the existing minutely reminder
   tick (v1.2). **Opt-in for everyone** (OQ1 resolved (b)): OFF by default for all users including the
   owner; each user enables via `/checkin on`.
2. **Do-Not-Disturb** — the per-user quiet-hours mechanism already exists (`/quiet`, `quiet_hours_json`,
   midnight-crossing). v1.5 does **not** build a parallel system; it **completes the suppression matrix**
   (fixing two gaps found in the audit — see §2.3), adds a `/dnd` alias, and makes check-ins honor it.
3. **LLM-call minimization** — reduce Ollama dependency by (a) a **deterministic pre-parser** that handles
   unambiguous number+unit logs without any LLM call (biggest win, zero-false-positive), (b) a raised/
   configurable **health-probe interval**, and (c) a config gate on the **startup schema probe**. Every
   reduction is **behavior-preserving** (same confirmations) and AC-gated against the suite.
4. **Release announcements** — when the bot starts running a **new version**, it sends every **active** user
   (owner included) a short bilingual "what's new" message, **once per version per user**, from a maintained
   per-version release-notes catalog. Deterministic, LLM-free, per each user's language preference.

Additive: migration 008 adds two nullable `users` columns (`checkin_window`, `last_announced_version`); DND
reuses the existing `quiet_hours_json`; LLM-minimization needs no schema. Full **1599-test suite** must stay
green; per-user isolation preserved.

SemVer: **1.5.0 (MINOR)**.

## 2. Inputs

### 2.1 New/changed commands (deterministic, LLM-free, whole-message-anchored)
```
/checkin                     # show my check-in setting (on/off + window)
/checkin on                  # enable at the default window
/checkin off                 # disable check-ins for me
/checkin 09:00-18:00         # enable with a custom hourly window
/checkin default             # inherit the config default
เช็คอิน [ ... ]               # Thai alias, same grammar
/dnd HH:MM-HH:MM[,…] | off    # alias of /quiet (same parsing, storage, effect)
งดรบกวน [ ... ]               # Thai alias of /dnd  (do-not-disturb)
```
`/dnd` is a pure alias of `/quiet` — same `Command`, same `preferences.execute_quiet`, same
`quiet_hours_json` storage. Thai aliases follow the strict anti-false-positive discipline (whole-message-
anchored; numeric/`off`-anchored tail; adversarial-corpus AC).

### 2.2 Per-user settings
- Check-in: `users.checkin_window` (NEW, nullable) — `NULL` = inherit config; `"off"` = disabled;
  `"HH:MM-HH:MM"` = enabled with that window.
- DND: existing `users.quiet_hours_json` (unchanged) — `NULL` = inherit `config.quiet_hours.windows`;
  `"[]"` = explicit none; `[[start,end],…]` = windows.

### 2.3 DND suppression matrix — current state (audited) vs v1.5
| Unprompted send | v1.4 suppression | v1.5 |
|---|---|---|
| Per-habit reminder (tick) | per-user `effective_quiet_windows` ✓ | unchanged |
| Snooze one-off | via `send_reminder` (per-user) ✓ | unchanged |
| **Daily summary** | **GLOBAL** `is_quiet_hours_now(config)` ✗ (gap: ignores the user's own DND) | **per-user** `in_dnd_now` |
| **Weekly review + charts** | **none** ✗ (gap: never suppressed) | **per-user** `in_dnd_now` |
| **Hourly check-in** (new) | — | per-user `in_dnd_now` |
| Health/outage alert → owner | none | **none** (ops must always reach the owner) |
| Access-request / access-granted → owner | none | **none** (important, rare) |
| **Release announcement** (new) | — | **none** (rare one-shot per release — send anyway, R-N4) |
| Milestone line on a confirmation | n/a (reply, not unprompted) | unchanged |

### 2.4 LLM call sites (audited) and disposition
| Site | v1.4 | v1.5 |
|---|---|---|
| `parse_message` extraction | LLM per non-command message | **pre-parser skips it** for number+unit logs (R-L1) |
| `target_nl.classify_target_intent` | already cost-gated by `looks_like_target_phrasing` | unchanged (already minimal) |
| `query.classify_query_intent` | gated (only on query-shaped messages) | unchanged |
| Startup schema probe | once per model at startup | **config-gated** (`[ollama] probe_on_startup`, R-L4) |
| Health `/api/version` + Telegram `getMe` | every 60s | **interval raised/configurable** (R-L3) — a liveness ping, not an inference call |
| Diary reflection / weekly-review narrative | LLM (user-facing content) | unchanged (removing them would change output) |

## 3. Outputs

### 3.1 Hourly check-in (deterministic, bilingual, non-nagging)
```
🌤️ Quick check-in
• water: 1200 / 2500 ml
• stretch: not yet today
Log anything you've done? 💧🧘
```
Shows only goal-bearing habits not yet at goal (plus a gentle invite). If the user has **no** goal-bearing
habits, a generic one-line nudge is sent instead. If **all** goal-bearing habits are already met today, the
check-in is **skipped** (non-nagging — §4 R-K3).

### 3.2 `/checkin` / `/dnd` replies
Bilingual acknowledgements (`checkin_set_on` / `checkin_set_off` / `checkin_set_window` / `checkin_show`;
`/dnd` reuses `/quiet`'s existing `quiet_set`/`quiet_cleared` copy). `/help` gains a line mentioning DND and
check-ins.

### 3.3 Pre-parser confirmation
Byte-identical to what the LLM extraction path produces for the same log — the user cannot tell a message
was pre-parsed (R-L2).

### 3.4 Release announcement (on first startup at a new version)
```
🎉 What's new in v1.5.0
• Hourly check-ins — send /checkin on to get gentle nudges (08:00–20:00)
• /dnd — set your do-not-disturb hours (also /quiet)
• Simple logs like "500ml" are now faster and work even if the assistant is offline
```
Rendered in each user's own language from `core/release_notes.py`; sent once per version per user.

## 4. Behavior rules

### Feature 1 — Hourly check-ins (module `checkins`, LLM-free)

- **R-K1** A single new function `checkins.run_due_checkins(channel, config, registry, db, clock)` is called
  from the **same minutely job** that runs the reminder tick. It returns immediately unless the current
  minute is `:00` (check-ins fire on the hour only). On the hour, for each `db.active_user_ids()`: resolve
  the user's effective check-in setting (R-K2); if enabled and the current hour is within the window, and
  not suppressed (R-K3/R-K4), send the check-in to that user's chat. Strictly LLM-free.
- **R-K2** `checkins.effective_checkin(db, config, user_id) -> (enabled: bool, window: tuple[str,str] | None)`
  from `users.checkin_window`: `NULL` → inherit `config.checkin.enabled` + `config.checkin.window`; `"off"`
  → disabled; `"HH:MM-HH:MM"` → enabled with that window. **OQ1 RESOLVED (b): opt-in for everyone** —
  `config.checkin.enabled` defaults to **`false`**, so a user with no override (the owner included) is
  **disabled by default** and receives no check-ins until they run `/checkin on`. Migration 008 adds
  `checkin_window` as an all-`NULL` column with **no backfill**, so this opt-in default holds for every
  existing and future user by construction. Fail-open on a DB read error (treat as the config default =
  disabled). A check-in fires at `HH:00` when `start ≤ HH:00 ≤ end`.
- **R-K3** (all-goals-met skip — non-nagging) Let `goal_bearing` = habits with a non-`None`
  `targets.effective_goal(db, habit, config, user_id)`. If `goal_bearing` is non-empty **and every one is
  already met today** (`db.sum_value ≥ goal`), the check-in is skipped. If `goal_bearing` is empty (e.g. a
  diary-only user), it is **not** skipped on this rule — a generic nudge is sent (so such users still get
  check-ins).
- **R-K4** (DND) A check-in whose fire time is inside the user's effective DND window is suppressed
  (`reminders.in_dnd_now(db, config, user_id, clock)`, R-D1).
- **R-K5** (snooze independence) Snooze governs a single per-habit reminder only; it does **not** suppress
  check-ins (recorded — §9). Check-ins have no per-check-in snooze in v1.5.
- **R-K6** (content) The check-in body is a deterministic bilingual template (`core/i18n.py`): a compact
  progress line per unmet goal-bearing habit (`label: today / goal unit`, or a localized "not yet today"
  when zero) plus one gentle invite line. Language is the user's stored preference
  (`resolve_unprompted_language(config, user_pref=…)`, same as reminders). Per-user isolated (R-K7).
- **R-K7** (isolation) Every read is scoped to `user_id`; A's check-in reflects only A's data/window and is
  delivered only to A (U-ISO).
- **R-K8** (`/checkin` setter) `commands.dispatch` gains an anchored `"checkin"` kind (`^/checkin(\s+…)?$`,
  Thai `เช็คอิน`): tail `on` / `off` / `default` / `HH:MM-HH:MM` / (empty = show). The setter writes
  `users.checkin_window` (`"off"`, `NULL` for default, the window string; `on` stores the config default
  window explicitly so it stays enabled regardless of the config default). Invalid window → a friendly usage
  reply, no write. Never raises; deterministic; works Ollama-down.

### Feature 2 — Do-Not-Disturb (shared surface + module `checkins` owns the alias)

- **R-D1** One shared per-user DND primitive: `reminders.in_dnd_now(db, config, chat_id, clock=datetime.now)
  -> bool` = "is now inside `chat_id`'s effective quiet windows" (built on the existing
  `effective_quiet_windows` + `_in_quiet_hours`, fail-open). It is the single check used by check-ins
  (R-K4), the daily-summary job (R-D2), and the weekly-review job (R-D3).
- **R-D2** The **daily-summary** job switches from the GLOBAL `is_quiet_hours_now(config)` to per-user
  `in_dnd_now(db, config, user_id, …)` evaluated inside the per-user fan-out — a user in their own DND is
  skipped; a user is no longer suppressed merely because a *global* window is active. **Behavior-preserving
  for un-customized users:** with no per-user `quiet_hours_json`, `effective_quiet_windows` falls back to
  `config.quiet_hours.windows`, so the result is identical to today (AC-10).
- **R-D3** The **weekly-review** job (which had no DND check) now suppresses a user's review + charts when
  `in_dnd_now` is true for that user. Default config windows are empty (`[]`), so an un-customized user's
  review still fires exactly as before (AC-11).
- **R-D4** (ops never suppressed) Health/outage alerts and access-request/access-granted notifications to
  the owner are **not** subject to DND (they are operational, not habit nudges). Recorded.
- **R-D5** (`/dnd` alias) `commands.dispatch` recognizes `/dnd` (Thai `งดรบกวน`) as an **exact alias** of
  `/quiet` — it produces the same `Command(kind="quiet", …)` and routes to `preferences.execute_quiet`,
  storing to the same `quiet_hours_json`. No parallel storage/mechanism. `/help` mentions DND + check-ins.

### Feature 3 — LLM-call minimization (module `preparse` + shared/config)

- **R-L1** (deterministic pre-parser) A new `preparse.deterministic_parse(text, registry) -> ExtractionResult
  | None` returns a result **only** when `text.strip()` matches, whole-message-anchored, `NUMBER UNIT` where
  `UNIT` resolves via the **registry unit lookup** (`core/units.py`, extracted from commands.py — R-L5) to a
  single `numeric`/`duration` habit and `NUMBER > 0`. It produces the same `ExtractionResult(category,
  value)` the LLM path would for that log. Anything else — a bare number with no unit, an unrecognized/
  ambiguous unit, any sentence/phrase (e.g. "from now on 2.5L a day"), a question, or any command — returns
  `None`. **Zero false positives** is guaranteed by whole-message anchoring + registry-gated unit
  resolution (adversarial-corpus AC-15).
- **R-L2** (wiring, behavior-preserving) In `handle_inbound_message`, `deterministic_parse` runs **after**
  `commands.dispatch` returns `None` and **before** the health-monitor deferral check and the LLM path. A
  non-`None` result is logged + confirmed through the **exact same** confirmation code the LLM path uses
  (same registry/targets/streaks/milestone logic) — the confirmation is byte-identical (AC-14). A `None`
  result falls through unchanged to the NL-target step / deferral / `parse_message` (AC-15).
- **R-L3** (health interval) `config.health.interval_seconds` default is **raised to 300** (from 60) —
  ~5× fewer liveness pings to Ollama/Telegram. Trade-off: DOWN→UP detection (and the deferred-message
  re-parse it triggers) now happens within this interval; acceptable, and blunted because simple logs no
  longer need Ollama at all (R-L1 + R-L2 handle them during an outage, AC-16). The value stays fully
  configurable; tests pin it explicitly (behavior-preserving, AC-17).
- **R-L4** (startup probe gate) `config.ollama.probe_on_startup` (default `true`, preserving current
  behavior) — set `false` to skip the startup schema-conformance probe(s), saving those startup LLM calls
  (AC-18).
- **R-L5** (units extraction, no copy-paste) `_build_unit_lookup` / `_resolve_unit` / the number+unit
  `_VALUE_RE` are extracted from `core/commands.py` into a shared `core/units.py`; `commands.py` imports
  them (byte-identical behavior — the existing command tests are the guard, AC-2), and `preparse.py` reuses
  them. No duplicated unit logic.
- **R-L6** (regression) Every LLM-minimization change leaves all existing confirmations/replies
  byte-identical; the full 1599-test suite stays green (AC-19).

### Feature 4 — Release announcements (module `announce`, LLM-free)

- **R-N1** (catalog) `core/release_notes.py` holds `RELEASE_NOTES: dict[str, dict[i18n.Language, str]]`
  keyed by version string (e.g. `"1.5.0"`) → `{en, th}` user-facing "what's new" copy (feature summaries,
  **not** the changelog table). `get_release_note(version, lang) -> str | None` returns the note or `None`
  when the version has no entry. v1.5.0's own entry ships as the first catalog row (check-ins opt-in, `/dnd`,
  faster/offline simple logs — §3.4), so this release announces itself. **Process requirement (Archi's
  release checklist):** add the release-note entry for the new version **before** tagging; a version with no
  entry simply announces nothing (never crashes, R-N2).
- **R-N2** (startup step) `announce.announce_release(db, channel, config, version) -> None`, called once at
  startup **after** migrations + `attribute_legacy_to_owner` (same placement rationale as attribution — the
  active-user set must be correct first). If `get_release_note(version, …)` is `None` for the running
  version, it returns immediately (no sends, no error). Otherwise, for each `db.active_user_ids()`: skip if
  `db.get_last_announced_version(user_id) == version` (already announced); else resolve the user's language
  (`resolve_unprompted_language(config, user_pref=…)`), send the note to their chat, and **only on a
  successful send** call `db.set_last_announced_version(user_id, version)`. Fail-open: a send/DB error is
  logged and the version is left unmarked for that user (retried next startup). Sequential sends (fine at
  this scale). Never raises.
- **R-N3** (once per version per user; latest-only) Marking-on-success gives idempotency — a restart at the
  same version announces nothing more (AC-21). When a user's `last_announced_version` is several versions
  behind, **only the current version's note is sent** (intermediate versions are not backfilled — bias
  simple; §10).
- **R-N4** (DND ignored) A release announcement is a rare, non-recurring one-shot; it is **not** subject to
  DND (send anyway). Deferring it "to the next startup outside the window" is complex and could delay it
  indefinitely; the simpler, defensible call for a per-release message is to send it. (Recorded — §9.)
- **R-N5** (newly-approved users caught up) On `/approve` / `/invite` (in `access.execute_admin`, right after
  the existing `db.upsert_user(target_chat, status="active")`), also set
  `db.set_last_announced_version(target_chat, __version__)` so a user approved **after** a version's release
  does **not** receive that version's announcement — they start caught up and only receive **future**
  release notes (AC-23). Pending/blocked users are never in `active_user_ids()`, so they get nothing (R-N2).
  Existing users at the v1.5 upgrade have `last_announced_version = NULL` (migration adds it with no
  backfill), so they **do** receive the v1.5.0 note on first startup (self-announce).
- **R-N6** (LLM-free, bilingual) The whole path is deterministic and LLM-free; copy is per-user language
  from the `release_notes` catalog. Per-user isolated: each active user gets exactly their own one message.

## 5. Interfaces (signatures)
```python
# storage/migrations.py
def _migration_008_checkin_and_announce(conn: sqlite3.Connection) -> None: ...
#   ALTER TABLE users ADD COLUMN checkin_window TEXT NULL
#   ALTER TABLE users ADD COLUMN last_announced_version TEXT NULL   (both additive, no backfill)

# storage/db.py
def get_checkin_window(self, chat_id: str) -> str | None: ...
def set_checkin_window(self, chat_id: str, value: str | None) -> None: ...
def get_last_announced_version(self, chat_id: str) -> str | None: ...
def set_last_announced_version(self, chat_id: str, version: str) -> None: ...

# core/units.py  (NEW — extracted from commands.py)
def build_unit_lookup(registry) -> dict[str, tuple[str, float]]: ...
def resolve_unit(lookup, unit_lower: str) -> tuple[str, float] | None: ...
VALUE_RE  # ^(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>\S+)?\s*$

# core/preparse.py  (NEW — module `preparse`, LLM-free)
def deterministic_parse(text: str, registry) -> ExtractionResult | None: ...

# core/reminders.py  (shared DND primitive)
def in_dnd_now(db, config, chat_id: str, clock=datetime.now) -> bool: ...

# core/release_notes.py  (NEW — module `announce`)
RELEASE_NOTES: dict[str, dict[i18n.Language, str]]     # keyed by version string; "1.5.0" ships as row 1
def get_release_note(version: str, lang: i18n.Language) -> str | None: ...

# core/announce.py  (NEW — module `announce`, LLM-free)
async def announce_release(db, channel, config, version: str) -> None: ...   # startup step (R-N2)

# core/checkins.py  (NEW — module `checkins`, LLM-free)
def effective_checkin(db, config, user_id: str) -> tuple[bool, tuple[str, str] | None]: ...
def build_checkin_message(db, config, registry, lang, user_id: str, clock) -> str | None: ...  # None => skip (R-K3)
async def run_due_checkins(channel, config, registry, db, clock=datetime.now) -> None: ...
async def execute_checkin(command, *, db, config, lang, user_id: str) -> str: ...  # /checkin setter

# core/commands.py  (extend)
CommandKind = Literal[..., "checkin"]          # plus /dnd -> existing "quiet" kind (alias, R-D5)
# Command reuses pref_value (checkin tail / dnd tail)

# config.py
class CheckinConfig(BaseModel):
    enabled: bool = False                # OQ1 RESOLVED (b): opt-in for everyone — OFF by default (owner incl.); /checkin on enables
    window: str = "08:00-20:00"          # default window applied when a user enables without an explicit one
# Config gains: checkin: CheckinConfig = CheckinConfig()
# OllamaConfig gains: probe_on_startup: bool = True   ;  HealthConfig.interval_seconds default -> 300.0
```

## 6. Files to touch
**Shared surface (first, sequentially):**
- `storage/migrations.py` — migration 008 (`users.checkin_window` + `users.last_announced_version`).
- `storage/db.py` — `get/set_checkin_window` + `get/set_last_announced_version`.
- `core/units.py` — NEW: extracted unit machinery; `core/commands.py` — import from it (byte-identical).
- `core/reminders.py` — `in_dnd_now` shared DND primitive.
- `config.py` + `config.toml` — `[checkin]` (enabled=false), `[ollama] probe_on_startup`, `[health] interval_seconds=300`.

**Module `checkins` (parallel):**
- `core/checkins.py` — NEW: check-in tick, message builder, effective-window resolver, `/checkin` setter.
- `core/commands.py` — `"checkin"` kind + `/dnd` alias parsing *(shared file; disjoint keys)*.
- `core/i18n.py` — check-in + `/checkin`/`/dnd` + `/help` copy (EN+TH).
- `tests/test_checkins.py`, `tests/test_dnd.py` — NEW.

**Module `preparse` (parallel):**
- `core/preparse.py` — NEW: `deterministic_parse`.
- `tests/test_preparse.py` — NEW (incl. adversarial corpus).

**Module `announce` (parallel):**
- `core/release_notes.py` — NEW: per-version bilingual catalog (`RELEASE_NOTES` + `get_release_note`; v1.5.0 entry).
- `core/announce.py` — NEW: `announce_release` startup step.
- `core/access.py` — one line in `execute_admin`'s approve/invite branch: catch a newly-approved user up to `__version__` (R-N5).
- `src/habit_assistant/__init__.py` — bump `__version__` to `1.5.0` at release (kept in sync with VERSION).
- `tests/test_announce.py` — NEW.

**Integration seam:**
- `main.py` — call `run_due_checkins` in the minutely job; wire `deterministic_parse` into
  `handle_inbound_message` (before deferral/LLM); switch daily-summary + weekly-review jobs to per-user
  `in_dnd_now` (R-D2/R-D3); route `checkin`/`dnd` command kinds; apply `[health] interval` +
  `probe_on_startup`; call `announce.announce_release(db, channel, config, __version__)` at startup after
  attribution; add `/checkin` to the menu — `/dnd` shares `/quiet`'s existing menu entry.

## 7. External dependencies
None new. Same stack. No new Telegram API surface. Migration 008 is a single additive column.

## 8. Acceptance criteria

### Shared / regression
- **AC-1** (migration 008): Given a v1.4 DB at `user_version=7`, migration 008 adds `users.checkin_window` **and** `users.last_announced_version` (both nullable, no backfill), touches no existing data, is idempotent (stamps 8), and the full 1599-test suite stays green. (R-K2 / R-N storage)
- **AC-2** (units extraction): Given the unit machinery moves to `core/units.py`, `commands.dispatch` behavior is byte-identical (existing command tests green) and `preparse` reuses the same helpers. (R-L5)
- **AC-19** (LLM-min regression): Given all Feature-3 changes, every existing confirmation/reply is byte-identical and the full suite stays green. (R-L6)

### Feature 1 — check-ins
- **AC-3** (hourly firing): Given check-ins enabled with window 08:00–20:00, When the minutely job runs at HH:00 for 08 ≤ HH ≤ 20, Then an eligible active user receives a check-in; off-the-hour (MM≠00) or outside the window → none. (R-K1/R-K2)
- **AC-4** (LLM-free content): Given a check-in fires, Then it is a deterministic bilingual progress template with **zero** Ollama calls. (R-K6)
- **AC-5** (all-goals-met skip): Given the user's goal-bearing habits are all met today, the check-in is skipped; given the user has no goal-bearing habits, a generic nudge is still sent. (R-K3)
- **AC-6** (DND honored): Given the fire time is inside the user's effective DND window, the check-in is suppressed. (R-K4/R-D1)
- **AC-7** (`/checkin` setter): Given `/checkin off` / `09:00-18:00` / `on` / `default` / (empty), Then the setting is stored/shown correctly (Thai alias too); an invalid window → usage reply, no write; adversarial log corpus never dispatches as `checkin`. (R-K8)
- **AC-8** (opt-in default): Given the shipped config (`[checkin] enabled=false`) and a user with no override, the user (**owner included**) receives **no** check-ins; after `/checkin on` the user gets check-ins at the default window 08:00–20:00; `/checkin off`/`default`/window behavior unchanged. (R-K2/R-K8 / OQ1 resolved (b))
- **AC-9** (isolation): Given two active users, A's check-in reflects only A's data/window; B is unaffected. (R-K7)

### Feature 2 — DND matrix
- **AC-10** (summary per-user DND): Given a user in their own DND window, their daily summary is skipped; an un-customized user's summary is byte-identical to v1.4. (R-D2)
- **AC-11** (weekly-review DND): Given a user in their own DND window, their weekly review + charts are suppressed; with default (empty) windows, the review fires exactly as before. (R-D3)
- **AC-12** (ops not suppressed): Given DND is active, health/outage alerts and access-request notifications to the owner are still delivered. (R-D4)
- **AC-13** (`/dnd` alias + help): Given `/dnd 22:00-07:00` (and `/dnd off`, Thai alias), Then it behaves identically to `/quiet` (same storage/effect); `/help` mentions DND + check-ins. (R-D5)

### Feature 3 — LLM minimization
- **AC-14** (pre-parser skips LLM): Given "500ml" / "2 แก้ว" / "10 min", When handled, Then it is parsed deterministically with **no Ollama call** and the confirmation is byte-identical to the LLM path. (R-L1/R-L2)
- **AC-15** (zero false positive / adversarial): Given the adversarial corpus (bare numbers w/o unit, unknown units, sentences, "from now on 2.5L a day", questions, commands), Then `deterministic_parse` returns `None` and the message falls through to the LLM path unchanged. (R-L1)
- **AC-16** (works Ollama-down): Given Ollama is down, a number+unit log is still logged + confirmed (no deferral) via the pre-parser. (R-L2)
- **AC-17** (health interval): Given `[health] interval_seconds`, the monitor uses it (default 300); a pinned shorter value still works and DOWN→UP recovery re-parses deferred messages within the interval. (R-L3)
- **AC-18** (probe gate): Given `[ollama] probe_on_startup=false`, the startup schema probe is skipped; default `true` preserves current behavior. (R-L4)

### Feature 4 — release announcements
- **AC-20** (announce on new version): Given the bot starts at a version with a release-notes entry and active users whose `last_announced_version` differs, When `announce_release` runs, Then each active user (owner included) receives the bilingual note in their own language, and their `last_announced_version` is set to the current version on successful send. (R-N1/R-N2/R-N6)
- **AC-21** (once per version per user / retry): Given a user already at the current `last_announced_version`, a subsequent startup sends them nothing; given a send that fails, that user is left unmarked and is retried on the next startup. (R-N2/R-N3)
- **AC-22** (no note → no crash): Given the running version has no `RELEASE_NOTES` entry, When `announce_release` runs, Then nothing is sent and it never raises. (R-N1/R-N2)
- **AC-23** (newly-approved caught up): Given a user approved after v1.5.0 shipped, When `/approve` runs, Then their `last_announced_version` is set to the current version so they do **not** receive v1.5.0's announcement; a later version bump does announce to them. (R-N5)
- **AC-24** (audience + DND + latest-only): Given pending/blocked users, they receive no announcement; given an active user inside a DND window, they still receive it (send-anyway); given a user several versions behind, only the current version's note is sent. (R-N2/R-N3/R-N4)

## 9. Resolved decisions & risks

**Open questions:** none remaining.
- **OQ1 — Check-in default enablement (RESOLVED (b): opt-in for everyone).** `[checkin] enabled` defaults to
  **`false`**; check-ins are OFF for all users **including the owner**. Each user enables via `/checkin on`
  (which applies the default window 08:00–20:00 when no explicit window is given); `off` / custom-window /
  `default` behavior is unchanged. Baked into R-K2/R-K8/AC-8; no on-by-default assumption remains elsewhere
  (migration 008 adds `checkin_window` all-`NULL` with no backfill, so the opt-in default holds by
  construction).

**Decisions recorded (defaults chosen; not load-bearing):**
- **Check-in content = progress-aware + all-goals-met skip** (vs a flat generic prompt) — more useful, and
  the skip keeps it non-nagging (gentle-gamification philosophy).
- **DND completes the existing quiet-hours mechanism** — no parallel system; `/dnd` is a pure `/quiet`
  alias; the two suppression gaps (daily summary global-only, weekly review none) are fixed to per-user.
- **Ops sends stay un-suppressed** by DND (health/outage + access notifications to the owner).
- **Health interval default raised to 300s** (configurable) — fewer dependency pings; the pre-parser makes a
  longer outage-detection window acceptable (simple logs work Ollama-down). Trade-off noted, not blocking.
- **Startup schema probe stays on by default**, gated by `probe_on_startup` for those who want to save it.
- **Pre-parser scope = number+unit only** (unit required; bare numbers and everything ambiguous still go to
  the LLM) — the strictly zero-false-positive subset. A bare-number win is deliberately forgone for safety.
- **Release announcements — send-anyway on DND** (R-N4): a per-release one-shot is rare; deferring it around
  DND could delay it indefinitely, so it is sent regardless of DND. **Latest-version-only** (R-N3): a user
  several versions behind gets only the current version's note, not a backfill/rollup — simpler and avoids a
  wall of history. **Newly-approved users are caught up** to the current version at approval (R-N5), so they
  receive only future release notes. **Per-version release note is a release-process step** (R-N1): the
  entry is added before tagging; a missing entry announces nothing (never crashes). `__version__` (in
  `habit_assistant/__init__.py`) is the running-version source, kept in sync with `VERSION` at release.

**Risks:**
- **Pre-parser divergence.** The confirmation must be byte-identical to the LLM path — the same downstream
  confirmation code (registry/targets/streaks/milestone) must be reused, not re-implemented (AC-14). This is
  the main correctness risk; the shared confirmation path in `handle_inbound_message` already exists, so the
  pre-parser only replaces the *extraction* step.
- **Check-in volume.** Up to ~13 fires/day/user before suppression; mitigated by R-K3/R-K4 and the gentle
  template. If still too much, a "skip if the user logged within the last hour" rule is a cheap follow-on
  (out of scope now).
- **Health-interval trade-off.** Slower outage detection at 300s; blunted by the pre-parser and fully
  configurable.

## 10. Out of scope
- Per-check-in snooze / "logged recently → skip" heuristics (R-K5; possible follow-on).
- Extending the pre-parser to bare numbers, label-first phrasing ("น้ำ 500"), or multi-habit messages
  (those still use the LLM).
- Any change to diary-reflection / weekly-review-narrative LLM usage (user-facing content; behavior-preserving
  means keeping them).
- Per-check-in language/timezone beyond the existing per-user language + global timezone.
- **Rollup/backfill of multiple release notes** (only the current version's note is announced, R-N3); a
  user-facing "release history" command; announcements to pending/blocked users; deferring an announcement
  around DND.

## 11. Module split & parallel development
**Total functionals:** 8 — (1) migration 008 + check-in/announce store, (2) units extraction, (3) DND
primitive + matrix completion, (4) hourly check-ins + `/checkin`, (5) `/dnd` alias, (6) deterministic
pre-parser, (7) health-interval/probe config, (8) release announcements + per-user version tracking. Above
the 5-functional threshold.

**Recommendation:** **SEQUENTIAL shared surface, then 3 PARALLEL modules, then integration.** The DND-matrix
work touches shared reminder/scheduler code and the check-in DND primitive, so it lives in the shared
surface; the three feature bodies (`checkins`, `preparse`, `announce`) are otherwise separable, touching
disjoint files (release notes live in their own `core/release_notes.py`, not the shared i18n catalog, so
`announce` never collides with `checkins`).

**Shared surface (built first, sequentially):**
- Migration 008 (`checkin_window` + `last_announced_version`) + `db.get/set_checkin_window` +
  `db.get/set_last_announced_version`.
- `core/units.py` extraction + `commands.py` refactor (byte-identical).
- `reminders.in_dnd_now` DND primitive.
- Config: `[checkin]` (enabled=false), `[ollama] probe_on_startup`, `[health] interval_seconds=300`.

| Module | Owned ACs | Owned files | Depends on |
|---|---|---|---|
| `checkins` | AC-3, AC-4, AC-5, AC-6, AC-7, AC-8, AC-9, AC-13 | `core/checkins.py`, `core/commands.py` (checkin + dnd kinds), `core/i18n.py` (checkin/dnd/help keys), `tests/test_checkins.py`, `tests/test_dnd.py` | shared: `in_dnd_now`, `db.*_checkin_window`, `[checkin]` config |
| `preparse` | AC-14, AC-15 | `core/preparse.py`, `tests/test_preparse.py` | shared: `core/units.py`, `ExtractionResult` |
| `announce` | AC-20, AC-21, AC-22, AC-23 | `core/release_notes.py`, `core/announce.py`, `core/access.py` (approve catch-up), `src/habit_assistant/__init__.py` (version bump), `tests/test_announce.py` | shared: `db.*_last_announced_version`, `active_user_ids`, per-user language |

ACs verified during the shared-surface / integration pass: **AC-1, AC-2, AC-19** (migration + extraction +
regression), **AC-10, AC-11, AC-12** (DND matrix in the summary/review jobs), **AC-16, AC-17, AC-18**
(pre-parser Ollama-down wiring + health/probe config), **AC-24** (announce audience/DND/latest-only, verified
at the startup-loop integration). Every AC belongs to exactly one owner. **Total: 24 acceptance criteria**
(shared/integration 10, `checkins` 8, `preparse` 2, `announce` 4).

**Integration order (after the three modules complete):**
1. `main.py`: call `run_due_checkins` in the minutely job; wire `deterministic_parse` into
   `handle_inbound_message` before the deferral/LLM path; switch daily-summary + weekly-review to per-user
   `in_dnd_now`; route `checkin`/`dnd`; apply `[health] interval` + `probe_on_startup`; call
   `announce.announce_release(db, channel, config, __version__)` at startup after attribution.
2. Full suite; highest-value gates: **AC-19** (behavior-preserving regression), **AC-14/AC-15** (pre-parser
   byte-identical + zero-false-positive), **AC-10/AC-11** (DND matrix, owner unchanged).
3. Integration tests: two users — check-ins fire hourly in-window, skip when all goals met or in DND, honor
   `/checkin off`; `/dnd` suppresses summary + review for the setting user only; a number+unit log is
   confirmed with no Ollama call (and during a simulated outage); health probes at the configured interval;
   the v1.5.0 release note reaches both active users once (and not a newly-approved user, AC-23).
```
