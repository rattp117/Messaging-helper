# Spec — v1.3.0: Audit log (durable who/when/what trail)

## 1. Problem statement
Now that the bot is multi-user (v1.2.0: `users` table, access gate, per-user targets/schedules/prefs), the
owner has no durable record of who changed what. v1.3.0 adds an **append-only audit trail**: every
state-changing action writes one row capturing **who** (actor chat id), **when** (timestamp), **what**
(action + entity), **old value → new value**, and **how** (source: text command / natural language / inline
button / admin). The owner reads it from chat via an owner-only `/audit`. The feature is **purely additive**:
migration 007 adds one new table (no schema break this version), the recorder is **fail-open** (an audit
write can never break the user's action), and every existing behavior stays byte-identical except for the
new writes — the full 1325-test suite must stay green (AC-A3). Pending/blocked users' message content is
still never stored (v1.2 privacy); the audit records only the *state transition* (e.g. "chat X became
pending"), never their message text.

SemVer: **1.3.0 (MINOR)** — additive feature, no breaking change (migration 007 is additive-only, unlike
006's sanctioned break).

## 2. Inputs

### 2.1 State-changing actions to capture (from the existing execute paths)
Each is recorded at the point its DB write succeeds, with old/new in hand:

| Action (stored `action`) | Where it fires | `entity` | `old_value` → `new_value` | `source` | actor / target |
|---|---|---|---|---|---|
| `undo` | `undo_ui.send_undo_confirmation` (text `/undo` **and** button) | habit id | removed value → `NULL` | `command` / `button` | actor = row owner |
| `edit` | `main._execute_edit` | habit id | old `value_num` → new `value_num` | `command` | actor |
| `target_set` | `targets_command._execute_set` | habit id | prev effective goal → new goal | `command` / `nl` | actor |
| `target_clear` | `targets_command._execute_clear` | habit id | prev override → `NULL` | `command` | actor |
| `remind_set` | `schedules._execute_set` | habit id | prev times (JSON) → new times (JSON) | `command` | actor |
| `remind_off` | `schedules._execute_off` | habit id | prev times (JSON) → `"off"` | `command` | actor |
| `remind_default` | `schedules._execute_default` | habit id | prev times (JSON) → `NULL` (config) | `command` | actor |
| `lang_set` | `preferences.execute_lang` | `NULL` | prev pref → new pref | `command` | actor |
| `quiet_set` | `preferences.execute_quiet` | `NULL` | prev json → new json | `command` | actor |
| `quiet_off` | `preferences.execute_quiet` (off) | `NULL` | prev json → `"[]"` | `command` | actor |
| `user_approve` | `access.execute_admin` (approve/**invite**) | `NULL` | prev status → `active` | `admin` | actor=owner, `target_user_id` |
| `user_block` | `access.execute_admin` (block) | `NULL` | prev status → `blocked` | `admin` | actor=owner, `target_user_id` |
| `user_pending` | `access.handle_gate` (unknown → pending) | `NULL` | `NULL` → `pending` | `admin` | actor=`target_user_id`=that chat |

### 2.2 Deliberately NOT audited (recorded reasoning — §9)
- **Plain habit-log creation** ("500ml", "10 min stretch") — every log is already a durable, append-only
  `logs` row (its own `ts`/`user_id`/`category`/`value`/`raw_message`); auditing it would duplicate the
  entire `logs` table into `audit_log` for zero added information. Only *mutations* of a log (undo/edit) are
  audited, because those change state (`deleted_at`/`value_num`) that `logs` alone doesn't cleanly surface.
- **Read-only commands** — `/habits`, `/help`, `/users`, `/audit`, `/target` (show), `/remind` (show), NL
  queries. No state change, nothing to audit.
- **Garmin import** — a read-only weekly-review cross-check (`core/garmin.py` never writes rows).
- **`--backup` / `--restore` / `--migrate` CLI** — process/ops-level, not per-user chat actions; already
  logged to the app log, and `--restore` replaces the whole DB (including `audit_log`), so an audit row
  would be of no lasting value there.

### 2.3 Owner identity & roles (unchanged from v1.2)
`secrets.telegram_chat_id` = owner; `access.classify(db, chat_id)` gives `owner|active|pending|blocked|
unknown`. `/audit` is owner-only (like `/approve`/`/block`/`/users`).

## 3. Outputs

### 3.1 `/audit` — owner-only recent-actions view (bilingual, LLM-free)
`/audit` (optionally `/audit N`) → the most recent N audit rows, newest first, one line each. Works with
Ollama down (deterministic). Hidden from the bot command menu (admin convention). Non-owner → silent no-op.

```
🧾 Recent activity (last 20):
• 08-22 14:03 · you · target set · water · 2500 → 2000 ml (command)
• 08-22 13:58 · 88899900 · reminder times · water · [08:00,12:00,18:00] → [08:00,12:00] (command)
• 08-22 11:20 · you · approved · 88899900 (admin)
• 08-22 09:05 · you · undo · water · removed 500 (button)
```
Actor renders as "you" for the owner's own rows, else the chat id (or stored `display_name` when available).
Action/labels localize via `core/i18n.py` (EN+TH); the raw values (`2000`, `[08:00,12:00]`) are shown
verbatim.

### 3.2 Empty / no rows
```
🧾 No activity recorded yet.
```

### 3.3 Error responses
The recorder never surfaces an error to the user (fail-open, §4 R-W2). `/audit` from a non-owner produces no
reply (silent, like other admin commands). `/audit abc` (non-numeric N) falls back to the default limit.

## 4. Behavior rules

### Storage & recorder (shared surface)

- **R-M1** Migration **007** is **additive-only** (no `ALTER`/`DROP` on any existing table — unlike 006):
  ```sql
  CREATE TABLE audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,              -- ISO8601 local, when the action happened
    user_id        TEXT NOT NULL,              -- actor (chat that performed the action)
    action         TEXT NOT NULL,              -- see §2.1 vocabulary
    entity         TEXT,                       -- habit id for habit-scoped actions; NULL otherwise
    old_value      TEXT,                       -- previous value as text/JSON; NULL when N/A
    new_value      TEXT,                       -- new value as text/JSON; NULL when N/A
    source         TEXT NOT NULL,              -- 'command' | 'nl' | 'button' | 'admin'
    target_user_id TEXT,                       -- admin actions on another chat; NULL otherwise
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
  );
  CREATE INDEX idx_audit_ts   ON audit_log(ts);
  CREATE INDEX idx_audit_user ON audit_log(user_id, ts);
  ```
  Appended to `MIGRATIONS` after `_migration_006_multiuser`; re-running is a no-op (`user_version` guard,
  stamps 7). No existing row/table/column is touched (AC-A1/AC-A3).
- **R-W1** `core/audit.py` is the single recorder. `record(db, *, actor, action, source, entity=None,
  old_value=None, new_value=None, target_user_id=None, clock=datetime.now) -> None` stringifies `old_value`/
  `new_value` (numbers → `"{:g}"`-style text, lists/dicts → `json.dumps`, `None` → `NULL`) and inserts one
  `audit_log` row. `action`/`source` values come from module-level constants (`ACTIONS`, `SOURCES`) so
  spelling can't drift between the capture sites and the viewer.
- **R-W2** (**fail-open** — the load-bearing safety rule) `record` **never raises and never blocks the user
  action**: the entire body is wrapped so any exception (DB locked, table missing, bad value) is logged and
  swallowed. A capture site calls `record(...)` **after** its own successful write and **ignores the
  result** — an audit failure leaves the user's action and reply exactly as they would have been without
  auditing (AC-A2). Recording is best-effort observability, never a correctness dependency.
- **R-W3** (retention) `db.prune_audit(cutoff_ts)` deletes rows older than the cutoff. Run **once at
  startup** (cheap, not per-insert) using `config.audit.retention_days` (default **365**; `0` = keep
  forever, no prune). Pruning is housekeeping and is itself **not** audited.

### Capture wiring (module `audit-capture`)

- **R-C1** Each execute path calls `audit.record(...)` immediately after its successful DB write, passing the
  old value it already read (or reads once, before the write) and the new value. The value-shaping already
  present in each module is reused (e.g. `targets_command._execute_set` already computes `previous_goal`;
  `schedules`/`preferences` read the prior stored value before overwriting). `source` is supplied by the
  caller: the execute functions gain an **optional `source` parameter (default `"command"`)** used only for
  the audit row — `main.py` passes `"nl"` for the full-NL target path and `undo_ui.handle_undo_callback`
  passes `"button"`; every other path keeps the `"command"` default.
- **R-C2** `undo` is recorded inside `undo_ui.send_undo_confirmation` (the one shared formatter for both the
  text `/undo` and the button paths, R-U8) so both paths record identically except `source`
  (`command` vs `button`). `entity` = the removed row's `category`, `old_value` = its removed value, `new_value`
  = `NULL`.
- **R-C3** Admin transitions are recorded in `access`: `execute_admin` records `user_approve`/`user_block`
  (actor = owner, `target_user_id` = the affected chat, old = prior status, new = `active`/`blocked`);
  `handle_gate`'s unknown→pending branch records `user_pending` (**no message content** — item 7 / R-C4).
- **R-C4** (privacy — unchanged from v1.2) A pending/blocked user's *message* is still never stored. The
  audit row for a transition stores only the structured state change (`new_value = "pending"`/`"blocked"`),
  never `text`/`raw_message`. `raw_message` continues to be stored **only** for `active` users' real logs, in
  `logs`, exactly as before.
- **R-C5** (additivity) No capture site changes any reply text, any existing DB row, or any control flow. The
  only new effect is the extra `audit_log` insert (fail-open). Existing per-module tests stay green; the
  execute functions keep their "structured op in → formatted string out, never raises" contract.

### Read surface (module `audit-view`)

- **R-V1** `commands.dispatch` gains an anchored, LLM-free `"audit"` kind: `^/audit(\s+\d+)?$` (optional
  Thai alias `ประวัติ`). `Command` gains `limit: int | None` (the parsed N, else `None`).
- **R-V2** `audit_view.render_recent(db, config, lang, *, limit) -> str` returns the bilingual, newest-first
  list from `db.recent_audit(limit)`. Default limit **20**, capped at **50**; a missing/invalid N uses the
  default. Actor renders as the owner-facing "you" for the owner's own rows, else `display_name` or chat id.
  Deterministic and LLM-free (works with Ollama down).
- **R-V3** `/audit` is **owner-only** and routed with the same owner re-check as the other admin commands
  (`access.classify(db, chat_id) == "owner"`); a non-owner gets a **silent no-op** (reveals nothing). `/audit`
  is **not** added to `set_my_commands` (admin-hidden convention, like `/approve`/`/block`/`/users`). Because
  it is LLM-free and dispatched in the command branch before the health-monitor deferral check, it works
  while Ollama is down.

## 5. Interfaces (signatures)

```python
# storage/migrations.py
def _migration_007_audit_log(conn: sqlite3.Connection) -> None: ...     # appended to MIGRATIONS

# storage/models.py  (or a small dataclass in core/audit.py)
@dataclass(slots=True)
class AuditEntry:
    id: int | None
    ts: str
    user_id: str
    action: str
    entity: str | None
    old_value: str | None
    new_value: str | None
    source: str
    target_user_id: str | None = None

# storage/db.py
def insert_audit(self, entry: AuditEntry) -> int: ...
def recent_audit(self, limit: int) -> list[sqlite3.Row]: ...            # newest first (ORDER BY id DESC)
def prune_audit(self, cutoff_ts: str) -> int: ...                       # returns rows deleted

# core/audit.py  (NEW — shared surface)
ACTIONS = ("undo", "edit", "target_set", "target_clear", "remind_set", "remind_off",
           "remind_default", "lang_set", "quiet_set", "quiet_off",
           "user_approve", "user_block", "user_pending")
SOURCES = ("command", "nl", "button", "admin")
def record(db, *, actor: str, action: str, source: str,
           entity: str | None = None, old_value=None, new_value=None,
           target_user_id: str | None = None, clock=datetime.now) -> None:
    """Fail-open (R-W2): stringify old/new, insert one audit_log row; swallow any exception."""

# config.py
class AuditConfig(BaseModel):
    retention_days: int = 365      # 0 = keep forever
# Config gains: audit: AuditConfig = AuditConfig()

# core/audit_view.py  (NEW — module `audit-view`)
def render_recent(db, config, lang: i18n.Language, *, limit: int, owner_chat_id: str) -> str: ...

# core/commands.py  (extend)
CommandKind = Literal[..., "audit"]
# Command gains: limit: int | None = None

# capture-site signature additions (optional `source`, default "command"; used only for the audit row):
#   targets_command.execute_target(..., source: str = "command")
#   schedules.execute_remind(..., source: str = "command")
#   preferences.execute_lang(..., source: str = "command")   /  execute_quiet(..., source: str = "command")
#   undo_ui.send_undo_confirmation(..., source: str = "command")   # handle_undo_callback passes "button"
```

## 6. Files to touch

**Shared surface (built first, sequentially):**
- `storage/migrations.py` — migration 007 (`audit_log` + indexes).
- `storage/db.py` — `insert_audit`, `recent_audit`, `prune_audit`.
- `storage/models.py` — `AuditEntry` (or define it in `core/audit.py`).
- `core/audit.py` — NEW: `record` (fail-open recorder), `ACTIONS`/`SOURCES` constants.
- `config.py` — `AuditConfig` + `Config.audit`; `config.toml` — commented `[audit] retention_days = 365`.
- `main.py` — call `db.prune_audit(...)` once at startup (retention).

**Module `audit-capture` (parallel, after shared surface):**
- `core/undo_ui.py` — record `undo` in `send_undo_confirmation`; `handle_undo_callback` passes `source="button"`.
- `core/targets_command.py` — record `target_set`/`target_clear`; add optional `source` param.
- `core/schedules.py` — record `remind_set`/`remind_off`/`remind_default`.
- `core/preferences.py` — record `lang_set`/`quiet_set`/`quiet_off`.
- `core/access.py` — record `user_approve`/`user_block` (execute_admin) + `user_pending` (handle_gate).
- `tests/test_audit_capture.py` — NEW.

**Module `audit-view` (parallel, after shared surface):**
- `core/commands.py` — `"audit"` kind + `limit` field + parsing.
- `core/audit_view.py` — NEW: `render_recent`.
- `core/i18n.py` — `/audit` copy + action-label keys (EN+TH) *(view owns these keys)*.
- `tests/test_audit_view.py` — NEW.

**Integration (after both modules):**
- `main.py` — thread `source` into the execute calls (`"nl"` for the full-NL target path, else default);
  record the **edit** path in `_execute_edit`; route `command.kind == "audit"` to `audit_view.render_recent`
  behind an owner check; keep `/audit` out of `set_my_commands`.

## 7. External dependencies
None new. Same stack (Python 3.11+, stdlib `sqlite3`, `httpx`, APScheduler, pydantic-settings). `json` (stdlib)
for value serialization. No new Telegram Bot API surface (`/audit` is an ordinary `sendMessage`).

## 8. Acceptance criteria

### Storage, recorder, regression
- **AC-A1**: Given a v1.2 DB at `user_version=6`, When it opens, Then migration 007 creates `audit_log` + its indexes, touches no existing table/row, and re-opening applies nothing (idempotent, stamps 7). (R-M1)
- **AC-A2** (fail-open): Given `db.insert_audit` raises (e.g. table missing / DB locked), When a capture site calls `audit.record(...)`, Then the exception is swallowed and logged, and the user's action + reply are unaffected (identical to no-audit). (R-W2)
- **AC-A3** (regression gate): Given the audit feature is present, When the full suite runs, Then every existing behavior is byte-identical except the new `audit_log` rows, and all 1325 existing tests stay green. (R-C5)
- **AC-R1** (retention): Given `[audit] retention_days = 365`, When the process starts, Then `prune_audit` deletes rows older than the cutoff and keeps newer ones; given `retention_days = 0`, Then nothing is pruned. (R-W3)

### Capture
- **AC-C1** (undo): Given a text `/undo`, Then one row is recorded `action=undo, source=command, entity=<habit>, old=<removed value>, new=NULL, user_id=<actor>`; given a button undo of the same log, `source=button`. (R-C2)
- **AC-C2** (edit): Given `/edit`/"make that 300ml", Then one row `action=edit, entity=<habit>, old=<old value>, new=<new value>, source=command`. (R-C1, integration)
- **AC-C3** (target): Given `/target water 2000`, Then `action=target_set, source=command, old=<prev goal>, new=2000`; given the full-NL "from now on 2.5L a day", `source=nl`; given `/target water default`, `action=target_clear, old=<prev override>, new=NULL`. (R-C1)
- **AC-C4** (remind): Given `/remind water 08:00 12:00`, Then `action=remind_set, old=<prev times JSON>, new=[08:00,12:00]`; `/remind water off` → `remind_off`; `/remind water default` → `remind_default`. (R-C1)
- **AC-C5** (lang/quiet): Given `/lang th`, Then `action=lang_set, old=<prev>, new=th`; `/quiet 22:00-07:00` → `quiet_set`; `/quiet off` → `quiet_off`. (R-C1)
- **AC-C6** (admin): Given owner `/approve 889…`, Then `action=user_approve, source=admin, actor=<owner>, target_user_id=889…, old=<prev status>, new=active`; `/block` → `user_block`; an unknown chat's first message → `user_pending` (target=that chat, new=pending). (R-C3)
- **AC-C7** (not audited): Given a plain habit log ("500ml") or any read-only command (`/habits`, `/users`, `/help`, `/audit`, a target/remind *show*, an NL query), Then **no** `audit_log` row is written. (§2.2)
- **AC-P1** (privacy): Given a pending/blocked user's message, Then no message content is stored anywhere (audit row has `new=pending`/`blocked` only, no `text`); `logs.raw_message` is still written only for active users' real logs. (R-C4)

### View
- **AC-V1**: Given the owner sends `/audit`, Then the recent 20 audit rows are returned newest-first, bilingual, each line showing ts · actor · action · entity · old→new · source; the owner's own rows render as "you"; it works with Ollama down. (R-V1/R-V2)
- **AC-V2**: Given `/audit 5`, Then at most 5 rows are shown; a request above the cap is limited to 50; a non-numeric N uses the default 20. (R-V2)
- **AC-V3** (owner-only + hidden): Given a non-owner sends `/audit`, Then there is no reply (silent no-op); `/audit` is not present in `set_my_commands`. (R-V3, integration)

## 9. Resolved decisions & risks

**Decisions recorded (defaults chosen; no load-bearing OQ):**
- **Read surface = owner-only `/audit`** (recommended, chosen). A chat surface is the point — the owner sees
  who did what without opening the DB. Members do not get `/audit` (owner-only, admin-hidden). DB-only with
  no chat surface was rejected as less useful.
- **Log creation is NOT audited** — the `logs` table already durably records every entry (`raw_message`
  included); auditing it would duplicate the whole table for no added signal. Only log *mutations*
  (undo/edit) are audited. (§2.2)
- **Garmin import and backup/restore/migrate CLI are NOT audited** — Garmin is read-only; CLI ops are
  process-level (already app-logged), and `--restore` would wipe `audit_log` anyway. (§2.2)
- **Retention default = 365 days**, pruned once at startup, `0` = keep forever. Configurable via `[audit]
  retention_days`. Long enough to be useful on a personal bot, bounded enough to not grow without limit.
- **Fail-open recorder** — an audit-write failure must never break a user action (R-W2). This is the single
  most important safety property; every capture site records after its own write and ignores the result.
- **Privacy preserved** — pending/blocked message content stays unstored; audit captures only the transition
  (R-C4, item 7).

**Genuinely load-bearing open questions:** none. (One minor, non-blocking flag below.)

**Risks / minor flags:**
- **Value serialization shape.** `old_value`/`new_value` are stored as text/JSON, not typed columns — fine
  for an audit trail (human-readable, one viewer). If a future feature needs to *query* by numeric delta,
  it would parse these back; out of scope now. (Flag, non-blocking.)
- **`/audit` output length.** A large N could produce a long message; the 50-row cap (R-V2) plus the default
  of 20 keeps it within Telegram's message-size limits. If a row's `old→new` is very long (e.g. many remind
  times), the viewer truncates the value display; format detail left to the view module.
- **Actor display.** The viewer shows the owner's own rows as "you" and others by `display_name`/chat id;
  `display_name` may be absent for a user onboarded before it was captured — falls back to the chat id.

## 10. Out of scope
- Auditing plain **log creation** (the `logs` table is its own record — §2.2).
- Auditing **read-only** commands, **Garmin** import, or **CLI** ops (§2.2).
- Per-user "see my own audit" — `/audit` is **owner-only** in v1.3; a self-audit view is a possible follow-on.
- **Editing/deleting** audit rows from chat, or exporting the audit trail to a file (owner reads the DB
  directly for bulk/export needs).
- Natural-language phrasing for `/audit` (deterministic command + Thai alias only).
- Tamper-evidence (hash chaining / signing) of the audit trail — the table is append-only by convention, not
  cryptographically sealed.

## 11. Module split & parallel development

**Total functionals:** 10 — (1) migration 007 + `audit_log`, (2) `core/audit.py` fail-open recorder,
(3) retention prune, (4) capture: undo, (5) capture: edit, (6) capture: target set/clear, (7) capture:
remind set/off/default, (8) capture: lang/quiet, (9) capture: admin/pending, (10) `/audit` viewer. Above the
5-functional threshold.

**Recommendation:** **SEQUENTIAL shared surface, then 2 PARALLEL modules, then integration.** The recorder,
migration, db methods, config, and startup prune are a small, tightly-coupled shared surface that both
feature modules build on — built first, sequentially. After it lands, the two modules touch **disjoint file
sets** and can run in parallel:
- `audit-capture` edits the five execute modules (`undo_ui`, `targets_command`, `schedules`, `preferences`,
  `access`) — small additive `record(...)` calls, no reply/flow changes.
- `audit-view` adds the `/audit` command in `commands.py` + a new `core/audit_view.py` + `core/i18n.py`
  copy.
Neither module touches the other's files; `core/audit.py` (their shared contract) and `main.py` (the
integration seam) are the only meeting points, and `main.py` changes land once at integration. `core/i18n.py`
and `core/commands.py` are touched only by `audit-view` (capture stores raw values, needs no i18n and no new
command kind), so there is no cross-module collision on those shared files.

**Shared surface (built first, sequentially):**
- Migration 007 + `db.insert_audit`/`recent_audit`/`prune_audit`; `AuditEntry`.
- `core/audit.py` (`record`, `ACTIONS`, `SOURCES`) — the fail-open recorder both modules call.
- `AuditConfig` + `config.toml` note; startup `prune_audit` call in `main.py`.

| Module | Owned ACs | Owned files | Depends on |
|---|---|---|---|
| `audit-capture` | AC-C1, AC-C3, AC-C4, AC-C5, AC-C6, AC-P1 | `core/undo_ui.py`, `core/targets_command.py`, `core/schedules.py`, `core/preferences.py`, `core/access.py`, `tests/test_audit_capture.py` | shared: `audit.record`, `ACTIONS`/`SOURCES` |
| `audit-view` | AC-V1, AC-V2 | `core/audit_view.py`, `core/commands.py` (audit kind), `core/i18n.py` (audit keys), `tests/test_audit_view.py` | shared: `db.recent_audit`, action-label mapping |

ACs verified during the shared-surface / integration pass (not owned by a parallel module): **AC-A1, AC-A2,
AC-A3, AC-R1** (migration + recorder + regression + retention), **AC-C2** (edit — recorded in `main.py`),
**AC-C7** (not-audited property across capture + log/read paths), **AC-V3** (owner-only routing + menu-hidden).
Every AC belongs to exactly one owner. **Total: 15 acceptance criteria** (shared/integration 7,
`audit-capture` 6, `audit-view` 2).

**Integration order (after both modules complete):**
1. In `main.py`: thread `source` into the execute calls (`"nl"` for the full-NL target path; default
   `"command"` elsewhere), record the **edit** path in `_execute_edit`, and route `command.kind == "audit"`
   to `audit_view.render_recent` behind an `access.classify(...) == "owner"` check; leave `/audit` out of
   `set_my_commands`.
2. Run the full suite; **AC-A3** (1325 existing tests byte-identical + green) and **AC-A2** (fail-open) are
   the highest-value gates.
3. Integration tests: perform one of each audited action across two users, then `/audit` as the owner shows
   them newest-first with correct actor/old→new/source; a non-owner `/audit` is silent; a forced
   `insert_audit` failure leaves the triggering action's reply unchanged (fail-open).
