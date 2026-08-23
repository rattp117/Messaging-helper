# Spec — v1.4.0: `/history [N]` (personal entry statement)

## 1. Problem statement
Users can log, undo, and edit habit entries but have no way to see a plain, chronological statement of what
they've recorded. v1.4.0 adds a small, **read-only** `/history [N]` command: the caller's last N logged
entries — timestamp (config timezone), habit + value/unit, and the original message text — **including
undone (soft-deleted) entries, clearly marked**. It is **strictly per-user** (U-ISO: the caller sees only
their own rows), **LLM-free**, and works with Ollama down. Purely additive: it reads the existing `logs`
table, needs **no migration**, and every existing behavior stays byte-identical — the full 1511-test suite
must stay green (AC-1).

SemVer: **1.4.0 (MINOR)** — additive, read-only, no breaking change.

## 2. Inputs

### 2.1 Command shapes (deterministic, LLM-free, whole-message-anchored)
```
/history                 # my last 20 entries (default)
/history 10              # my last 10 entries
/history water           # my last 20 water entries (habit filter)
/history water 10        # my last 10 water entries
ย้อนหลัง [ ... ]          # Thai alias, same grammar   (ประวัติ is taken by /audit — do NOT reuse it)
```
Tail grammar: `[<habit>] [<N>]`, in that order. First token: a registry habit id → filter; else digits → N.
A second `\d+` token after a habit → N. An unknown non-numeric first token → a friendly "unknown habit"
reply (§4 R-D2). Anything not matching the whole anchored shape falls through to the normal pipeline (no
false positives on real logs — AC-5).

### 2.2 Data source
The existing `logs` table (per-user via `user_id`, v1.2). Rows carry `ts`, `category`, `value_num`,
`value_text`, `raw_message`, `habit_type`, `deleted_at`. `raw_message` is **user-controlled text** (may
contain newlines, control characters, `{`/`}`) — the renderer must sanitize + truncate it (§4 R-R3).

## 3. Outputs

### 3.1 `/history` — the statement (bilingual, newest-first)
```
🧾 Your last 20 entries:
• 08-23 14:03 · water 500 ml · "drank a big glass, 500ml"
• 08-23 13:10 · stretch 10 min · "10 min stretch"  (undone)
• 08-23 09:30 · diary · "felt good after the walk today…"
```
Each line: reformatted timestamp `MM-DD HH:MM` (config-tz local, as stored), the habit + value/unit
(reusing `undo_ui.describe_log`), the truncated original message in quotes, and a **localized undone marker**
on soft-deleted rows. Header names the effective count and, when filtered, the habit.

### 3.2 Empty
```
🧾 No entries yet.
```

### 3.3 Error / edge
`/history coffee` (unknown habit) → a friendly `history_invalid_habit` reply. Over-length output is repaired
by the shared budget guard (oldest shown rows dropped, "… N more" footer). No traceback ever reaches the
user (read-only, never raises).

## 4. Behavior rules

### Read method (shared surface)
- **R-D1** New `db.recent_logs(user_id, limit, category=None) -> list[sqlite3.Row]`:
  `SELECT * FROM logs WHERE user_id = ? AND category != 'unparsed' [AND category = ?] ORDER BY ts DESC,
  id DESC LIMIT ?`. Key differences from every existing aggregation query: it does **NOT** filter
  `deleted_at IS NULL` (undone rows are included — §1), and it **excludes** `category = 'unparsed'`
  (deferred/unparsed rows are hidden — item 5, recommended default: they are not confirmed entries). Strictly
  scoped to `user_id` (U-ISO, AC-9). Read-only.

### Budget/truncation helper (shared surface — extract, don't copy-paste)
- **R-B1** Extract the generic length machinery currently inside `core/audit_view.py` into a small shared
  module `core/render_budget.py`: `MAX_VALUE_CHARS` (60), `TELEGRAM_MESSAGE_BUDGET` (4096),
  `truncate(text, max_chars=MAX_VALUE_CHARS) -> str` (flat char cut + `…`), and
  `fit_within_budget(header, row_lines, *, render_footer: Callable[[int], str]) -> str` — the structural
  total-length guard that drops the oldest shown rows (tail of a newest-first list) until `header` + kept
  rows + a footer fit, appending `render_footer(dropped_count)`. The footer text is injected by the caller
  (so the helper stays i18n-agnostic): `audit_view` passes its `audit_more_rows` renderer, `history_view`
  passes `history_more_rows`.
- **R-B2** `core/audit_view.py` is refactored to import these from `core/render_budget.py` instead of its
  own private copies. Its rendered output must stay **byte-identical** — the existing audit-view tests are
  the regression guard (AC-3). (This is the one existing file the shared step touches; it is a pure
  extract-and-delegate, no behavior change.)

### Dispatch (module `history`)
- **R-D2** `commands.dispatch` gains a whole-message-anchored, LLM-free `"history"` kind: `^/history(\s+…)?$`
  and `^ย้อนหลัง(\s+…)?$`, parsing the §2.1 tail against the live registry into `Command.category` (filter,
  reusing the existing field) and `Command.limit` (reusing the v1.3 field). A first non-numeric token that
  is **not** a registry id yields `Command(kind="history", category=<that token>)` flagged such that the
  view returns `history_invalid_habit`; a bare/valid tail resolves normally. The anchoring + registry-gated
  tail must not match any real log (AC-5). `ย้อนหลัง` must not collide with `/audit`'s `ประวัติ` (AC-5).

### Rendering (module `history`)
- **R-R1** `history_view.render_history(db, config, registry, lang, *, user_id, category, limit) -> str`:
  newest-first, one line per row from `db.recent_logs(user_id, effective_limit, category)`. Default limit
  **20**, cap **50** (mirrors `/audit`, reusing `render_budget` constants/logic). No rows → `history_empty`.
- **R-R2** Each line reuses `undo_ui.describe_log(row, registry, lang)` for the habit + value/unit segment
  (the codebase's canonical per-type one-liner — built-ins, generic numeric/duration/boolean, and a raw
  fallback for an unknown historical category), plus the reformatted `MM-DD HH:MM` timestamp and the quoted,
  sanitized+truncated `raw_message`. A row with `deleted_at IS NOT NULL` appends the localized undone marker
  (`history_undone_marker`); a live row appends nothing (AC-8).
- **R-R3** `raw_message` is user-controlled and **must not break the renderer**: collapse newlines/carriage
  returns and other control characters to single spaces (so one entry stays one line), then `truncate` to a
  per-line cap. It is passed only as a `.format()` **value** (never as a template), so literal `{`/`}` in the
  message are inert — the renderer never crashes on any input (AC-12).
- **R-R4** The fully-rendered message is checked against `render_budget.TELEGRAM_MESSAGE_BUDGET`; an overflow
  (any cause) is repaired via `render_budget.fit_within_budget` with a `history_more_rows` footer (AC-12).
- **R-R5** All copy is bilingual EN/TH via `core/i18n.py` (`history_*` keys — disjoint from `audit_*`); the
  undone marker is localized.

### Access & availability (integration)
- **R-A1** `/history` is available to **every active user** (their own data) — routed for any acting chat
  that passed the access gate, with **no owner check** (unlike `/audit`). It is dispatched in the command
  branch before the health-monitor deferral check, so it works with Ollama down.
- **R-A2** `/history` **is** added to the public `set_my_commands` menu (item 1 — it's the caller's own
  data), alongside `/undo`/`/target`/`/help`/`/habits`. (`/audit` stays owner-only and hidden.)

## 5. Interfaces (signatures)
```python
# storage/db.py
def recent_logs(self, user_id: str, limit: int, category: str | None = None) -> list[sqlite3.Row]: ...

# core/render_budget.py  (NEW — shared; extracted from audit_view)
MAX_VALUE_CHARS = 60
TELEGRAM_MESSAGE_BUDGET = 4096
def truncate(text: str, max_chars: int = MAX_VALUE_CHARS) -> str: ...
def fit_within_budget(header: str, row_lines: list[str], *, render_footer: Callable[[int], str]) -> str: ...

# core/audit_view.py  (refactor: import the three above from render_budget; output byte-identical)

# core/history_view.py  (NEW — module `history`)
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
def render_history(db, config, registry, lang: i18n.Language, *,
                   user_id: str, category: str | None, limit: int | None) -> str: ...

# core/commands.py  (extend)
CommandKind = Literal[..., "history"]     # reuses existing Command.category and Command.limit fields

# core/i18n.py  (module `history` owns these keys, EN+TH)
#   history_header, history_header_filtered, history_line, history_undone_marker,
#   history_empty, history_invalid_habit, history_more_rows
```

## 6. Files to touch
**Shared surface (small, first):**
- `storage/db.py` — `recent_logs`.
- `core/render_budget.py` — NEW: extracted budget/truncation helpers.
- `core/audit_view.py` — refactor to import from `render_budget` (byte-identical output).
- `tests/test_render_budget.py` — NEW (covers extracted helpers + audit byte-identical guard).

**Module `history` (sequential):**
- `core/history_view.py` — NEW: `render_history`.
- `core/commands.py` — `"history"` kind + tail parsing.
- `core/i18n.py` — `history_*` keys (EN+TH).
- `tests/test_history.py` — NEW.

**Integration seam:**
- `main.py` — route `command.kind == "history"` to `history_view.render_history` for the acting `user_id`
  (no owner gate); add `/history` to the public `set_my_commands` menu.

## 7. External dependencies
None new. Reads existing `logs` via stdlib `sqlite3`; renders via `core/i18n.py`. No new Telegram API
surface (`/history` is an ordinary `sendMessage`). No migration.

## 8. Acceptance criteria
- **AC-1** (additive/regression): Given `/history` is present, When the suite runs, Then no migration is added, existing behavior is byte-identical, and all 1511 existing tests stay green. (R-D1, additive)
- **AC-2** (`recent_logs`): Given seeded rows for a user (some soft-deleted, one `category='unparsed'`), When `recent_logs(user, N, category)` runs, Then it returns that user's rows newest-first, **includes** soft-deleted rows, **excludes** `unparsed`, honors the optional `category` filter, and respects the limit. (R-D1)
- **AC-3** (extract, not copy): Given the budget helpers are moved to `core/render_budget.py`, When `audit_view.render_recent` runs, Then its output is byte-identical to v1.3 (existing audit-view tests green) and `history_view` reuses the same helpers. (R-B1/R-B2)
- **AC-4** (dispatch): Given `/history`, `/history 10`, `/history water`, `/history water 10`, and `ย้อนหลัง …`, When dispatched, Then each yields a `history` command with the correct `category`/`limit`. (R-D2)
- **AC-5** (adversarial + no collision): Given the adversarial log corpus (e.g. "500ml", "ดื่มน้ำ 2 แก้ว"), When dispatched, Then none yields a `history` command; and `ย้อนหลัง` does not collide with `/audit`'s `ประวัติ`. (R-D2)
- **AC-6** (invalid habit): Given `/history coffee` (unknown habit), Then the reply is `history_invalid_habit` (no crash, no rows). (R-D2)
- **AC-7** (content + limits): Given a user with entries, When `/history` runs, Then the last 20 are shown newest-first with ts (config tz) · habit+value/unit · quoted original message; `/history 5` shows 5; a request above 50 is capped. (R-R1/R-R2)
- **AC-8** (undone marked): Given a soft-deleted (undone) entry, When `/history` runs, Then it is included and carries the localized undone marker; a live entry carries none. (R-R2)
- **AC-9** (U-ISO): Given two active users A and B, When A runs `/history`, Then only A's entries appear; B's never do. (R-D1)
- **AC-10** (filter): Given `/history water`, Then only the caller's water entries appear. (R-D2/R-R1)
- **AC-11** (unparsed hidden): Given a deferred `category='unparsed'` row, When `/history` runs, Then it never appears. (R-D1)
- **AC-12** (raw-text safety + budget): Given a `raw_message` with newlines/control chars/`{`/`}`, When rendered, Then it is collapsed to one line, truncated, and never breaks the renderer; and a large N of long messages is fit within the 4096-char budget with a `history_more_rows` footer. (R-R3/R-R4)
- **AC-13** (empty): Given a user with no entries, Then `/history` returns `history_empty`. (R-R1)
- **AC-14** (availability): Given Ollama is down, Then `/history` still works (LLM-free); and `/history` (with its Thai alias) appears in the public `set_my_commands` menu for active users; output is bilingual EN/TH. (R-A1/R-A2/R-R5)

## 9. Resolved decisions & risks
**Decisions (defaults chosen; no load-bearing OQ):**
- **In the public menu** (item 1): `/history` is every active user's own data → added to `set_my_commands`
  (unlike owner-only, hidden `/audit`). Confirmed.
- **Defaults/caps** mirror `/audit`: default 20, cap 50.
- **Habit filter included** (item 2): `/history <habit> [N]` — cheap and clean, reuses `Command.category` and
  the registry already available in `dispatch` (as `/target`/`/remind` do). Grammar is `[<habit>] [<N>]`.
- **Thai alias = `ย้อนหลัง`** (item 3): "retrospective/past", whole-message-anchored, registry/numeric-anchored
  tail, adversarial-corpus AC. `ประวัติ` deliberately avoided (owned by `/audit`).
- **Unparsed rows hidden** (item 5): recommended default — deferred/unparsed rows are not confirmed entries.
- **Undone rows shown + marked** — the whole point of a "statement" is a faithful record, including reversals.

**Risks (minor, non-blocking):**
- Reusing `undo_ui.describe_log` for a **text** habit shows a value_text snippet that can overlap the quoted
  `raw_message`; acceptable (a diary's content and its raw message are effectively the same). Documented.
- `describe_log`'s built-in branches assume `water`/`stretch`/`diary` ids; a renamed/removed historical
  habit falls to its generic/raw fallback (already how `describe_log` behaves) — no crash.

## 10. Out of scope
- Editing/deleting entries from `/history` (it is read-only; `/undo`/`/edit` are the mutation paths).
- Showing `unparsed`/deferred rows, cross-user history, or an owner "all users" view.
- Date-range/free-text search or NL phrasing (deterministic command + Thai alias only).
- Pagination beyond "… N more" (a larger N re-runs the command).

## 11. Module split & parallel development
**Total functionals:** 3 — (1) `recent_logs` + shared budget extraction, (2) `/history` dispatch + renderer,
(3) route + menu integration. Under the 5-functional threshold.

**Recommendation:** **SEQUENTIAL — one module + a small shared extraction + an integration seam.** The
feature is small and read-only; there is no independent second track worth the coordination overhead. Build
order:
1. **Shared surface (small):** `db.recent_logs`; extract `core/render_budget.py` and refactor
   `core/audit_view.py` to delegate (byte-identical, AC-3).
2. **Module `history`:** `core/history_view.py` + `commands.py` `"history"` kind + `history_*` i18n keys +
   `tests/test_history.py`.
3. **Integration:** route `history` in `main.py` (any active user, no owner gate) and add `/history` to the
   public `set_my_commands` menu.

**Integration order / highest-value gates:** AC-1 (1511 existing tests green + no migration), AC-3
(audit byte-identical after extraction), AC-9 (U-ISO), AC-12 (raw-text safety + budget). Every AC is owned
by the single `history` track (with AC-2/AC-3 landing in the shared step). **Total: 14 acceptance criteria.**
```
