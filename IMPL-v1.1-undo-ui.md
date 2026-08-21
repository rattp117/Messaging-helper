# Implementation — v1.1.0 `undo-ui` module (undo discoverability)

Scope: **only** the `undo-ui` parallel module per SPEC-v1.1.md §11 — the
inline "Undo" button, its `callback_query` handler, the shared undo
confirmation formatter (R-U8), and this module's contribution to the
`/undo` bot command menu. Per dispatch instructions, `main.py` is **not
touched** — its remaining integration wiring (startup `set_my_commands`,
attaching the button to every confirmation, routing `on_callback`, and
making `_execute_undo` delegate here) is a later integration step by
another agent. This report documents exactly what that step needs to call.

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/core/undo_ui.py` | Created, then modified (iteration 2) | `undo_button`, `send_undo_confirmation`, `handle_undo_callback`, `command_menu_entries` — the module's full public surface (SPEC-v1.1.md §5). Iteration 2: `handle_undo_callback` bounds-checks a parsed `log_id` against SQLite's 64-bit `INTEGER` range before calling `db.get_log`, fixing an uncaught `OverflowError` Vera found (see "Iteration log"). |
| `tests/test_undo_ui.py` | Created, then extended by Vera (3 tests, not modified by Luna) | Unit tests for every AC this module owns (AC1 building block, AC2 documented, AC5 module-level, AC7, AC8, AC9, AC11); now 36 tests total (33 Luna + 3 Vera) |

No other file was read-modified. Read-only inputs: `channels/base.py`
(`Button`/`Channel`), `core/targets.py` (`effective_goal`), `core/habits.py`
(`HabitRegistry`), `core/i18n.py` (catalog keys `undo_button_label`,
`already_undone`, `undo_removed_*`, `describe_log_*`), `storage/db.py`
(`get_log`, `soft_delete`), `main.py` (read for reference only, to mirror
`_execute_undo`/`_describe_log`'s exact behavior — see "How it works").

## How it works

`undo_button(log_id, lang)` returns the one-row inline keyboard
(`[(label, "undo:<id>")]`) that `main.py`'s integration step will attach to
every interactive confirmation via `channel.send_actionable(...)`.
`handle_undo_callback(data, source_text, callback_id, *, db, channel,
config, clock, registry)` is the `on_callback` body: it parses `data` as
`undo:<int>` (anything else is logged and ignored, R-U6), resolves the
reply language from `source_text` via `i18n.resolve_reply_language`
(honoring a forced `config.i18n.language`), and either sends the friendly
`already_undone` reply (row missing or already soft-deleted, R-U5/AC8) or
soft-deletes the row and sends the removed+recomputed-total confirmation
via `send_undo_confirmation` — the single shared formatter both this
callback path and (once wired) the text `/undo` command path use, so R-U8's
byte-identical guarantee holds. `send_undo_confirmation`/its private
`describe_log` helper are a line-for-line mirror of `main.py`'s existing
`_execute_undo`/`_describe_log` bodies (down to reading
`targets.effective_goal` for goal-aware percentages, R-T5/AC23) — this is
temporary duplication, not a second design; see "Known limitations" below
for why and what the integrator should do about it. `command_menu_entries()`
returns this module's `{"en": [("undo", ...)], "th": [("undo", ...)]}`
contribution to `setMyCommands`, since there's no i18n catalog key for Bot
API menu copy and `core/i18n.py` is a frozen shared-surface file this
module must not touch.

Note: `handle_undo_callback` never calls `channel.answer_callback_query`
itself — `TelegramChannel.run` (shared surface, already built and tested in
`tests/test_channels.py`) already calls it unconditionally right after
awaiting `on_callback`, even on error/no-op (R-U4). `callback_id` is kept
as a parameter only so this function's signature matches the
`Callable[[str, str, str], Awaitable[None]]` shape `main.py` will wire as
`on_callback`.

## Smoke test done

1. `python -c "from habit_assistant.core import undo_ui; ..."` — imported
   cleanly, `command_menu_entries()`/`undo_button()` produce the expected
   shape/content for both languages (verified interactively, output
   captured with `PYTHONIOENCODING=utf-8` since the Windows console can't
   print Thai directly).
2. `.venv\Scripts\python.exe -m pytest -q tests/test_undo_ui.py` →
   **33 passed**. Covers: button shape (en/th), `command_menu_entries()`
   shape/localization, the AC5 module-level guarantee (button + milestone
   suffix travel as one `send_actionable` call), AC7 (soft-delete +
   confirmation + Thai-detection + forced-language override), AC8
   (already-deleted and genuinely-missing ids, both idempotent, no second
   delete), AC9 (7 malformed-`data` variants: no write, nothing sent, and a
   dedicated caplog assertion that it's logged), and AC11 — byte-identical
   comparison between the button path (`undo_ui.send_undo_confirmation`)
   and the unmodified command path (`main.handle_inbound_message("undo"/
   "ยกเลิก", ...)` → `main._execute_undo`) across every branch: water,
   stretch, diary (generic fallback), a goal-bearing generic-numeric habit,
   a goal-less generic-numeric habit (also falls back to generic), a
   generic-duration habit, a generic-boolean habit, and water with an
   active `/target` override (R-T5/AC23 combined with R-U8) — each in both
   English and Thai (2 languages × 8 shapes = 16 byte-identical assertions).
3. Full suite: `.venv\Scripts\python.exe -m pytest -q` → **765 passed, 7
   failed, 1 skipped** (was 732 passed / 7 failed / 1 skipped on the
   shared-surface baseline per IMPL-v1.1-shared.md — the +33 are exactly
   this module's new tests, nothing else moved). The 7 failures are the
   same pre-existing, unrelated ones IMPL-v1.1-shared.md already documented
   and verified against unmodified `main` (6 hardcoded-past-date flakes in
   `test_adaptive_reminders.py`/`test_v09_gaps.py`, 1 stale `VERSION`-pin in
   `test_charts.py`) — none touch undo, callbacks, or channels.
4. **Iteration 2 (post-Vera fix):** `tests/test_undo_ui.py` (now 36 tests,
   Vera's 3 additions included) → **36 passed**. Full suite → **847
   passed, 7 failed, 1 skipped** (855 collected total — the `targets`
   module's own test files landed concurrently in the repo during this
   session, growing the collected count from 773 to 855, per
   TEST-v1.1-undo-ui.md's own note; none of those are in this module's
   scope). The 7 failures are the exact same pre-existing/documented ones
   as before — no regressions from the fix.
5. Never ran the app, `--seed`, `--dry-run`, or any test against
   `data/habits.db`; the live Task Scheduler service was not touched. All
   DB access in tests is `tmp_path`-backed SQLite.

## Maps to acceptance criteria

- **AC1** (`set_my_commands` called at startup with `/undo` present, en +
  th) → **building block only, not fully verifiable without `main.py`**:
  `command_menu_entries()` + `tests/test_undo_ui.py::
  test_command_menu_entries_has_undo_for_both_languages` prove this
  module's contribution is correct and localized. The actual startup call
  (merging this with `targets_command`'s `/target` entries and invoking
  `channel.set_my_commands`) is `main.py`'s integration wiring — see
  "Wiring for the integrator" below for the exact call. Full AC1 needs a
  Vera pass against the integrated `async_main`.
- **AC2** (a `set_my_commands` transport error at startup is logged, never
  crashes) → **not implemented here** — the `try/except` around the
  startup call is `main.py`'s own belt-and-suspenders code (same posture
  as the existing schema-probe `try/except` in `async_main`), not something
  this module can express. Documented in "Wiring for the integrator" so the
  integrator writes it correctly; verified at the integration Vera pass.
- **AC5** (milestone suffix in text AND the button on the same message) →
  **module-level guarantee verified**:
  `test_button_and_milestone_suffix_travel_as_one_actionable_message`
  proves `send_actionable` carries both the full text (including an
  appended milestone-style suffix) and exactly one button as a single
  call. The end-to-end version (a real milestone crossing through
  `handle_inbound_message`) needs `main.py`'s confirmation call sites
  switched to `send_actionable` + `undo_ui.undo_button`, which is
  integration's job (SPEC-v1.1.md §11) — full AC5 verified there.
- **AC7** → `core/undo_ui.py:handle_undo_callback`/`send_undo_confirmation`
  + `test_handle_undo_callback_soft_deletes_and_sends_confirmation` /
  `test_handle_undo_callback_detects_thai_language_from_source_text` /
  `test_handle_undo_callback_forced_language_overrides_source_text`.
- **AC8** → `handle_undo_callback` +
  `test_handle_undo_callback_already_deleted_sends_already_undone_and_no_second_delete`
  / `test_handle_undo_callback_missing_row_sends_already_undone`.
- **AC9** → `handle_undo_callback`'s `_UNDO_CALLBACK_RE` guard +
  `test_handle_undo_callback_malformed_data_no_write_no_send` (7
  parametrized malformed-data shapes) /
  `test_handle_undo_callback_malformed_data_is_logged`; plus the
  `_SQLITE_MAX_INTEGER` bounds check (iteration 2 fix, see "Iteration log")
  + Vera's `test_handle_undo_callback_negative_id_no_write_no_send` /
  `test_handle_undo_callback_astronomically_large_id_does_not_raise`.
- **AC11** → `send_undo_confirmation`/`describe_log` +
  `test_byte_identical_water` / `_stretch` / `_diary_generic_fallback` /
  `_generic_numeric_with_goal` / `_generic_numeric_without_goal_falls_back_to_generic`
  / `_generic_duration` / `_generic_boolean` /
  `_water_with_target_override` (each parametrized over en/th).

Not owned by this module (verified by the `targets` module's own Vera pass,
or at shared-surface/integration): AC3, AC4, AC6, AC10, AC12 and everything
under Feature 2.

## Wiring for the integrator (main.py, not done in this pass)

Exact calls `main.py`'s integration step needs, per SPEC-v1.1.md §6/§11:

1. **Startup command menu** (`async_main`, AC1/AC2) — merge this module's
   entries with `targets_command`'s equivalent per language, call once,
   wrapped exactly like the existing schema-probe:
   ```python
   commands = {
       lang: undo_ui.command_menu_entries()[lang] + targets_command.command_menu_entries()[lang]
       for lang in ("en", "th")
   }
   try:
       await channel.set_my_commands(commands)
   except Exception:
       logger.exception("set_my_commands failed at startup; continuing")
   ```
2. **Attach the button to every interactive confirmation** (R-U2/R-U3,
   AC3/AC5) — every `channel.send(...)` call in `handle_inbound_message`
   that follows a `db.insert_log(...)`/`reclassify_log(...)` (water,
   stretch, diary, the generic-habit branch, and the recovery
   re-confirmations in `reparse_pending_unparsed`) becomes
   `channel.send_actionable(text + milestone_suffix, undo_ui.undo_button(row_id, lang))`,
   where `row_id` is `db.insert_log(entry)`'s return value (currently
   discarded — capture it). Unprompted sends (reminders, daily summary,
   weekly review, health alerts, clarifying question, deferred-ack) stay on
   plain `channel.send(...)`, no button (R-U2).
3. **Route callbacks** (R-U4/AC6, shared surface already supports this) —
   ```python
   await channel.run(
       on_message,
       on_callback=lambda data, source_text, cb_id: undo_ui.handle_undo_callback(
           data, source_text, cb_id, db=db, channel=channel, config=config, clock=datetime.now, registry=registry
       ),
   )
   ```
4. **Delegate `_execute_undo` to the shared formatter** (R-U8, so the two
   confirmations stay byte-identical *by construction* instead of by two
   hand-synced copies) — replace `main.py`'s `_execute_undo` body (after
   its `row = db.last_log()` / "nothing to undo" check, which stays
   command-path-specific) with:
   ```python
   await undo_ui.send_undo_confirmation(db, channel, config, clock, registry, lang, row)
   ```
   `main.py`'s own `_describe_log` can then be deleted in favor of
   `undo_ui.describe_log` (public, same signature) — at that point the
   duplication flagged in "Known limitations" below disappears entirely.

## Known limitations

1. **`send_undo_confirmation`/`describe_log` duplicate `main.py`'s existing
   `_execute_undo`/`_describe_log` logic line-for-line.** This is
   unavoidable while `main.py` is off-limits (per dispatch instructions,
   the delegation happens at the integration step, item 4 above) — R-U8's
   actual "one implementation" guarantee only becomes literally true once
   that delegation lands. Until then, both copies happen to produce
   byte-identical output (proven by AC11's tests), but a future edit to one
   without the other would silently diverge. Flagging this explicitly so
   the integrator prioritizes item 4, not just items 1-3.
2. **AC1/AC2/full AC5 are not verifiable end-to-end without `main.py`**, by
   design (see "Maps to acceptance criteria" above) — this module supplies
   every building block (`command_menu_entries`, `undo_button`,
   `send_actionable` compatibility) and documents the exact wiring calls;
   the end-to-end assertion is the integration Vera pass's job.
3. **No i18n catalog key exists for the `/undo` command-menu description**
   (`core/i18n.py` is a frozen shared-surface file this module must not
   touch) — `UNDO_COMMAND_DESCRIPTIONS` is a small local bilingual dict in
   `core/undo_ui.py` instead. If Archi/Iris later want this centralized in
   the catalog, it's a one-key addition, not a design change.
4. Editing/deleting the button after a tap (`editMessageReplyMarkup`) is
   explicitly out of scope (SPEC-v1.1.md §10) — not implemented, per spec.

## Iteration log

### Round 1 → Vera's `TEST-v1.1-undo-ui.md`: FAIL, 1 bug (AC9)

- **Failure:** `handle_undo_callback("undo:999999999999999999999999999999999", ...)` —
  a digit string that matches `_UNDO_CALLBACK_RE` (`^undo:(\d+)$`) but is far
  outside SQLite's signed 64-bit `INTEGER` range — raised an uncaught
  `OverflowError: Python int too large to convert to SQLite INTEGER` at
  `core/undo_ui.py:215` (`db.get_log(log_id)`), violating this module's own
  "never raises on hostile callback data" contract (R-U6/AC9). Not a
  bot-crashing bug in production (`channels/telegram.py`'s shared-surface
  `on_callback` wrapper swallows it and still answers the callback), but a
  genuine violation at this module's own boundary, per Vera's finding.
  35/36 tests passed otherwise (her 2 other new tests — a negative id and a
  cross-path idempotency case — passed on the first try).
- **Root cause:** `_UNDO_CALLBACK_RE` bounds the shape (`undo:` + digits)
  but not the magnitude; `int(match.group(1))` on an arbitrarily long digit
  string produces a Python int with no upper bound, which is then handed
  straight to `db.get_log` as a SQLite bind parameter — the `OverflowError`
  happens inside `sqlite3`, one layer below this module's own guard. The
  parsing step never asked "is this actually representable as a SQLite
  INTEGER" before trusting it.
- **Fix:** added a bounds check — `_SQLITE_MAX_INTEGER = 2**63 - 1` — right
  after parsing `log_id` and before any DB call in `handle_undo_callback`;
  an out-of-range id is now logged and ignored, identically to a
  regex-malformed payload (no DB read, no DB write, no send). Chose the
  bounds-check approach over widening the regex to `\d{1,18}` (Vera's other
  suggested option) so a legitimate-but-unlikely 19-digit id up to the
  actual SQLite max (`9223372036854775807`) still resolves correctly rather
  than being arbitrarily rejected at 18 digits — same outcome for every
  input Vera's tests exercise, slightly more precise at the boundary.
- **Verified:** `tests/test_undo_ui.py` (36 tests, including Vera's 3,
  unmodified) → 36 passed. Full suite → 847 passed, 7 failed (same 7
  pre-existing/documented), 1 skipped, 855 collected — no regressions.
