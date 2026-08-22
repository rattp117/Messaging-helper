# Implementation — v1.3.0 Audit log (module `audit-view`)

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/core/audit_view.py` | Created, then modified (iteration 1) | `render_recent(db, config, lang, *, limit, owner_chat_id) -> str` (R-V2) — the bilingual, newest-first `/audit` formatter. Deterministic, LLM-free, no channel import. Iteration 1 (TEST-v1.3-view.md): added `_truncate`/`_MAX_VALUE_CHARS` (per-value truncation in `_humanize_stored_value`, SPEC-v1.3.md §9) and `_fit_within_budget`/`_TELEGRAM_MESSAGE_BUDGET` (a structural total-message-length guard that drops the oldest shown rows + appends a footer whenever the rendered reply would exceed Telegram's 4096-char `sendMessage` limit). |
| `src/habit_assistant/core/commands.py` | Modified | `CommandKind` gains `"audit"`; `Command` gains `limit: int \| None = None`; new `_AUDIT_SLASH_RE`/`_AUDIT_TH_RE`/`_parse_audit_limit`/`_match_audit`; `dispatch()` wired to check `_match_audit` right after the `access` block (R-V1). Module docstring gets a new v1.3.0 section documenting the addition. |
| `src/habit_assistant/core/i18n.py` | Modified, then modified again (iteration 1) | 18 new catalog keys total (EN+TH) — corrects this doc's earlier miscount (Vera's minor finding, TEST-v1.3-view.md): `audit_header`, `audit_empty`, `audit_line`, `audit_actor_you`, 13 `audit_action_*` labels (one per `core/audit.py:ACTIONS` value), and (iteration 1) `audit_more_rows` — the "N more" footer `_fit_within_budget` appends when rows had to be dropped. |
| `tests/test_audit_view.py` | Created, then extended twice | 79 tests total: 58 Luna (initial), 16 Vera (`TEST-v1.3-view.md`), 5 Luna (iteration 1, this fix's own coverage — see Iteration log). Covers dispatch shape recognition, an adversarial false-positive corpus, bilingual rendering, AC-V2 limit/cap/default behavior, empty-state, LLM-free structural proof, owner-gating composition, rendering-robustness edge cases (NULL columns, vocabulary drift, actor-lookup failure), and — as of iteration 1 — the message-length structural bound (per-value truncation + the total-length guard, including a worst-case pathological scenario beyond Vera's own reproduction). |

I did **not** touch `main.py` (per the coordinator's explicit instruction — the
menu registration and the `command.kind == "audit"` route are integration's
job, landed once after both parallel modules report done, per
SPEC-v1.3.md §11) or any of the five `audit-capture`-owned execute modules
(`undo_ui`, `targets_command`, `schedules`, `preferences`, `access`) or its
test file.

## How it works

`core/commands.py:dispatch` recognizes `/audit [N]` (fully permissive tail,
mirrors `/target`/`/remind`/`/lang`/`/quiet`'s own slash-form posture — an
explicit `/` prefix is a near-zero false-positive surface) and the optional
Thai alias `ประวัติ [N]` (anchored to the *whole* stripped message with only
a purely-numeric tail allowed — mirrors `/help`'s `_HELP_RE`/`/habits`'s
`_HABITS_RE` "ordinary Thai word, whole-message-only" conservatism, since
"ประวัติ" — "history" — is a real word that opens ordinary prose, unlike a
registry habit token). Either form produces `Command(kind="audit",
limit=<parsed N or None>)`; a missing, non-numeric, or otherwise malformed N
resolves to `limit=None` rather than rejecting the match, per §3.3's own
"`/audit abc` falls back to the default limit" contract. `core/audit_view.
render_recent` then reads `db.recent_audit(effective_limit)` (shared
surface, already newest-first via `ORDER BY id DESC`), applies R-V2's
default-20/cap-50 rule, and renders one bilingual line per row: timestamp
reformatted to `MM-DD HH:MM`, actor resolved to "you" (the owner's own
rows) or the stored `display_name`/raw chat id otherwise, the action's
i18n-localized label, and a humanized `entity · old → new` segment (a
JSON-list old/new value — e.g. remind times — renders as a compact
`[08:00,12:00]` instead of raw JSON; an admin row with `entity=NULL` shows
its `target_user_id` in that slot instead). No rows at all — whether
because nothing has ever been recorded or because the request itself
resolved to 0 — renders the friendly `audit_empty` message. Neither
function ever touches an LLM or a channel; `render_recent` is a plain
synchronous function with no `llm`/`health_monitor` parameter at all, so it
is structurally LLM-free (proven directly, not simulated, in
`test_render_recent_is_synchronous_and_has_no_llm_dependency`).

## Smoke test done

1. Matcher shapes, run directly via the Python REPL against the real
   `HabitRegistry`/`commands.dispatch`: `/audit`, `/audit 5`, `/audit abc`,
   `/audit 999`, `/audit 0`, `ประวัติ`, `ประวัติ 3`, `ประวัติศาสตร์ไทยน่าสนใจ`
   (must NOT match), `please audit the logs` (must NOT match), `500ml`
   (must NOT match) — all produced the expected `Command`/`None`.
2. `render_recent` end-to-end against a real on-disk temp SQLite `Database`
   (`tempfile.mkdtemp()`, never `data/habits.db`; the live Task Scheduler
   service was never touched): seeded four `audit_log` rows spanning
   `undo`/`user_approve`/`remind_set`/`target_set` across an owner row and a
   member row, in true chronological (oldest-first) insert order, then
   confirmed newest-first rendering in both EN and TH:
   ```
   🧾 Recent activity (last 20):
   • 08-22 14:03 · you · target set · water · 2500 → 2000 (command)
   • 08-22 13:58 · Bob · reminder times · water · [08:00,12:00,18:00] → [08:00,12:00] (command)
   • 08-22 11:20 · you · approved · 88899900 · pending → active (admin)
   • 08-22 09:05 · you · undo · water · 500 → — (button)
   ```
   and the Thai variant localizing every action label + "you" → "คุณ"
   correctly. Also verified `limit=2` (2 rows + header "last 2"),
   `limit=999` (capped to "last 50"), `limit=0` (empty-state message), and
   an empty DB in both languages ("No activity recorded yet." /
   "ยังไม่มีการบันทึกกิจกรรมนะ").
3. `tests/test_audit_view.py` (58 tests) run in isolation: **58 passed**.
4. Regression spot-check on the files I touched:
   `pytest -q tests/test_commands.py tests/test_i18n.py
   tests/test_i18n_literals.py tests/test_access.py
   tests/test_discoverability.py` → **248 passed** (no existing test needed
   a single change).
5. Full suite: `.venv\Scripts\python.exe -m pytest -q` → **1440 passed, 1
   skipped**, zero warnings from my own code (fixed a `SyntaxWarning:
   invalid escape sequence '\s'` I introduced in `commands.py`'s docstring —
   a literal `\s`/`\d` inside a non-raw triple-quoted string — by rephrasing
   the regex description in prose instead of literal syntax). This run
   already includes the sibling `audit-capture` module's own concurrent
   landing (`tests/test_audit_capture.py`, ~32 tests) sharing the same
   working tree — the two parallel modules' file sets are disjoint
   (confirmed via `git status`: I only touched `core/commands.py`,
   `core/i18n.py`, and created `core/audit_view.py` +
   `tests/test_audit_view.py`) and coexist cleanly with no cross-module
   conflict. Never ran against `data/habits.db`; the live service was never
   stopped, started, or otherwise touched.

## Wiring for the integrator (main.py, NOT done by this module)

Per SPEC-v1.3.md §11, routing `command.kind == "audit"` and the owner gate
live in `main.py`'s integration step. Two call sites, both already-proven
patterns (see `test_owner_gating_*` in `tests/test_audit_view.py` for a
composition-level proof of this exact recipe):

1. **`on_message` closure** (mirrors the existing `("start", "approve",
   "block", "users", "invite")` interception block, since `/audit` is
   likewise owner-only and needs `owner_chat_id`, which
   `handle_inbound_message` doesn't have):

   ```python
   command = commands.dispatch(text, registry)
   if command is not None and command.kind == "audit":
       if access.classify(db, chat_id) == "owner":
           reply = audit_view.render_recent(
               db, config, lang, limit=command.limit, owner_chat_id=secrets.telegram_chat_id
           )
           await channel.send(chat_id, reply)
       return  # silent no-op for a non-owner (R-V3, "reveals nothing" — same posture as approve/block/users/invite)
   ```

   Place this check alongside (or folded into) the existing
   `command.kind in ("start", "approve", "block", "users", "invite")`
   branch, **before** `handle_inbound_message` is called — same reasoning
   `IMPL-v1.2-access.md` already documented for that block: `/audit` is
   LLM-free and must work with Ollama down, and dispatching it before the
   health-monitor deferral check (which only `handle_inbound_message`
   performs) is what makes that true. Intercepting it here, at the same
   point as the other admin commands, achieves that automatically.

2. **`set_my_commands` / `command_menu`**: do **not** add `"audit"` to
   `command_menu`, `START_COMMAND_DESCRIPTIONS`, or any of the
   `*_COMMAND_DESCRIPTIONS` dicts near the top of `main.py`. `/audit` stays
   admin-hidden, the same convention already established for `/approve`,
   `/block`, `/users`, and `/invite` (see `main.py`'s own comment block
   above `START_COMMAND_DESCRIPTIONS` for the existing rationale — global
   `setMyCommands` would advertise it to every ordinary member).

No other file needs to change for `/audit` to work — `core/audit_view.py`
and `core/commands.py`'s `"audit"` kind are both already complete and
tested in isolation.

## Maps to acceptance criteria

Module-owned ACs (2, per SPEC-v1.3.md §11's ownership table):

- **AC-V1** (recent 20, newest-first, bilingual, ts·actor·action·entity·old→new·source, "you" for owner's own rows, works with Ollama down) → `core/audit_view.py:render_recent`; `tests/test_audit_view.py::test_render_recent_newest_first_with_actor_and_action_and_value_rendering`, `::test_render_recent_thai_language_localizes_labels_and_actor`, `::test_render_recent_actor_falls_back_to_raw_chat_id_when_no_display_name`, `::test_every_audit_action_has_a_localized_label_in_both_languages` (parametrized over all 13 `ACTIONS`), `::test_render_recent_is_synchronous_and_has_no_llm_dependency`, `::test_render_recent_works_with_no_ollama_reachable_at_all`.
- **AC-V2** (`/audit 5` → at most 5 rows; above-cap → 50; non-numeric N → default 20) → `core/commands.py:_match_audit`/`_parse_audit_limit` (shape) + `core/audit_view.py:_effective_limit` (bounds); `tests/test_audit_view.py::test_dispatch_recognizes_audit_shape` (parametrized, incl. `/audit abc`→`None`, `/audit 999`→`999`, `/audit 0`→`0`), `::test_render_recent_default_limit_is_20`, `::test_render_recent_honors_an_explicit_limit_within_cap`, `::test_render_recent_caps_a_request_above_50`, `::test_render_recent_via_full_pipeline_audit_abc_falls_back_to_default`, `::test_render_recent_limit_zero_shows_the_empty_state`. The 50-row cap is meant to produce a *deliverable* response (SPEC-v1.3.md §9), which as of iteration 1 is now structurally guaranteed regardless of row content → `core/audit_view.py:_fit_within_budget`/`_humanize_stored_value`'s `_truncate`; `tests/test_audit_view.py::test_render_recent_50_rows_of_realistic_remind_edits_exceeds_telegram_limit` (Vera's finding, now passing), `::test_render_recent_worst_case_pathological_rows_still_stays_within_telegram_limit`, `::test_fit_within_budget_footer_reports_the_correct_dropped_count`, `::test_humanize_stored_value_truncates_a_long_scalar_string`, `::test_humanize_stored_value_truncates_a_long_json_list`, `::test_humanize_stored_value_leaves_a_short_value_untouched`.

Not owned by this module (per SPEC-v1.3.md §11, verified during
shared-surface/integration instead): AC-A1–AC-A3, AC-R1 (already proven,
`IMPL-v1.3-shared.md`); AC-C1–AC-C7, AC-P1 (`audit-capture`'s own scope);
**AC-V3** (owner-only routing + menu-hidden) is integration-owned per the
spec's own module table, but I pre-validated the *composition* it depends
on: `tests/test_audit_view.py::test_owner_gating_owner_gets_a_reply`,
`::test_owner_gating_non_owner_gets_silent_no_op` (parametrized over a
member and a stranger), `::test_owner_gating_reveals_nothing_even_when_activity_exists`
— all built on the already-landed `access.classify` (shared/access) plus
`audit_view.render_recent` (mine), mirroring exactly the call the "Wiring
for the integrator" section above hands to `main.py`. The remaining half of
AC-V3 (confirming `/audit` is never added to the real `command_menu`) can
only be verified once `main.py` is actually edited — flagged, not silently
skipped.

## Known limitations

- **No polish for numeric units on `target_set`/`edit` rows.** SPEC-v1.3.md
  §3.1's illustrative sample shows `2500 → 2000 ml` (a trailing unit
  suffix); I render `2500 → 2000` (no unit) — adding it would require
  `render_recent` to build a `HabitRegistry` internally just to resolve
  `entity` → unit label, a scope increase not required by any AC's literal
  wording (AC-V1 lists `ts · actor · action · entity · old→new · source`,
  no unit). Flagging as a deliberate smallest-change call, not an
  oversight — easy follow-on if Vera or the user wants it.
- **`config` parameter is currently unused inside `render_recent`.** Kept
  in the signature per SPEC-v1.3.md §5's exact interface and for parity
  with this codebase's other view-builders (`build_help_text`/
  `build_habits_overview`, both of which also take `config`) — nothing
  `render_recent` renders today varies by any config value. `del config`
  at the top of the function makes this explicit rather than silent.
- **The Thai alias `ประวัติ`'s trailing-N is Latin-digit-only** (`\d+`, not
  Thai numerals ๐-๙) — matches every other numeric-tail command in this
  codebase (`/target`, `/remind`, none of which accept Thai numerals
  either), so this is consistent, not a gap specific to this module.
- **`main.py` wiring is untouched**, per explicit instruction — see the
  "Wiring for the integrator" section above for the exact two call sites
  integration needs to add. Until that lands, `/audit` is fully built and
  tested but unreachable from a real Telegram message.
- **The 4096-char budget is checked in Python `len()` units, not UTF-16
  code units** (Telegram's own counting unit for `sendMessage`'s length
  limit). Every character `render_recent` can actually emit — Thai script,
  the box/arrow/bullet punctuation, the two emoji (🧾/…) — is either in the
  Basic Multilingual Plane (1 UTF-16 unit, matches Python's count exactly)
  or a single codepoint counted the same both ways in practice at this
  message's scale; flagged for completeness, not a known real discrepancy,
  and matches the measure `TEST-v1.3-view.md`'s own failing test used.

## Iteration log

### Round 1 (TEST-v1.3-view.md, Vera)

- **Failure:** `test_render_recent_50_rows_of_realistic_remind_edits_exceeds_telegram_limit` — `/audit 50`
  with 50 realistic `remind_set` rows (ordinary 4-time schedules, nowhere near `schedules.py`'s
  `MAX_REMINDER_TIMES=24` cap) rendered **5828 chars**, 42% over Telegram's 4096-char `sendMessage`
  limit. `channels/telegram.py:send()` posts `text` as-is with no length check, so this would 400 in
  production. SPEC-v1.3.md §9 claims "the viewer truncates the value display" as existing behavior; it
  was not implemented anywhere.
- **Root cause:** `_humanize_stored_value` applied no length cap to a rendered `old`/`new` value, and
  `render_recent` joined every row into one string with no check against Telegram's limit anywhere
  before the result could reach `channel.send`. The 50-row cap (R-V2) bounds row *count*, not message
  *length* — those are different guarantees, and only the first was ever implemented.
- **Fix (entirely inside `core/audit_view.py`, no other module touched):**
  1. **Per-value truncation** (Vera's recommendation (a), SPEC-v1.3.md §9's own stated intent): new
     `_truncate`/`_MAX_VALUE_CHARS = 60`, applied inside `_humanize_stored_value` to every rendered
     old/new value (scalar or JSON-list-humanized) — an ellipsis replaces anything past 60 chars.
  2. **Structural total-length guard** (the "go one step further" ask — per-value truncation alone does
     NOT bound the total message: 50 rows of even short, untruncated values can still exceed 4096, which
     is exactly Vera's own failing case, since a 25-char 4-time-schedule value is nowhere near the 60-char
     per-value cap). New `_fit_within_budget`/`_TELEGRAM_MESSAGE_BUDGET = 4096`: `render_recent` now always
     checks the fully-rendered message's length before returning it; on overflow, it drops the *oldest*
     shown rows (the tail of the newest-first list) one at a time and appends a new bilingual
     `audit_more_rows` footer ("… N more" / "… อีก N รายการ") reporting exactly how many rows were
     omitted — so the guarantee holds regardless of *why* the message overflowed (many rows, one long
     value, or both), not just for the one shape Vera happened to reproduce.
- **Verification:** Vera's previously-failing test now passes unmodified. Added my own
  `test_render_recent_worst_case_pathological_rows_still_stays_within_telegram_limit` — 50 rows each at
  the worst *realistic* shape simultaneously (a full 24-time schedule for both old AND new, a long member
  chat id as actor, Thai localization) — confirmed by direct measurement to render **4034 chars (EN) /
  4028 chars (TH)**, both under budget, versus ~17,400 chars for the same input before this fix. Also
  added `test_fit_within_budget_footer_reports_the_correct_dropped_count` (the footer's `N` is exact, not
  approximate) and three `_humanize_stored_value` unit tests (long scalar truncates, long JSON list
  truncates, short values are untouched — no regression on every existing pinned-output test above, which
  all use short values).
- **Result:** `tests/test_audit_view.py`: 79 passed (74 → 79, +5 Luna iteration-1 tests, 0 failed). Full
  suite: **1486 passed, 0 failed, 1 skipped** (target was 1456+/0/1 — exceeded because the sibling
  `audit-capture` module's own Vera pass, `TEST-v1.3-capture.md`/`tests/test_audit_capture_gaps.py`,
  landed concurrently in the same working tree; confirmed via `git status` that my own changes remain
  scoped to `core/audit_view.py`, `core/commands.py`, `core/i18n.py`, and `tests/test_audit_view.py` —
  no capture-owned file or `main.py` touched).
