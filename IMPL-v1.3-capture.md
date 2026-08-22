# Implementation — v1.3.0 Audit log (module `audit-capture`)

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/core/undo_ui.py` | Modified | `send_undo_confirmation` gains `*, source: str = "command"`; records `action="undo"` (entity=`row["category"]`, old=removed value, new=`None`) right after `db.soft_delete`, before formatting the reply. `handle_undo_callback` now passes `source="button"`. |
| `src/habit_assistant/core/targets_command.py` | Modified | `execute_target` gains `source: str = "command"`, threaded to `_execute_clear`/`_execute_set`. `_execute_clear` records `target_clear` (old=prior raw override via `db.get_target`, new=`None`) after `db.clear_target` succeeds. `_execute_set` records `target_set` (old=prior *effective* goal, already computed as `previous_goal`; new=`value_num`) after `db.set_target` succeeds. |
| `src/habit_assistant/core/schedules.py` | Modified | `execute_remind` gains `source: str = "command"`, threaded to `_execute_off`/`_execute_default`/`_execute_set`. Each reads `db.get_reminder_times(user_id, habit.id)` *before* its write to capture the prior times, then records `remind_off` (new=`"off"` literal), `remind_default` (new=`None`), or `remind_set` (new=the validated times list) after the write succeeds. |
| `src/habit_assistant/core/preferences.py` | Modified | `execute_lang` gains `source: str = "command"`; reads `db.get_user(user_id)` before the write to capture `previous.language_pref` (or `None`), records `lang_set` after `db.set_user_language` succeeds. `execute_quiet` gains `source: str = "command"`; reads the prior `quiet_hours_json` before the write, records `quiet_off` (new=`[]`) or `quiet_set` (new=the parsed `windows` list) after `db.set_user_quiet_hours` succeeds. |
| `src/habit_assistant/core/access.py` | Modified | `handle_gate`'s unknown→pending branch records `user_pending` (actor=`target_user_id`=the chat itself, new=`"pending"`) **inside** the same `try` as `db.upsert_user`, so a failed write never produces a phantom row. `execute_admin`'s approve/invite branch reads `db.get_user(target_chat)` before the write to capture the prior status, then records `user_approve` (actor=owner `chat_id`, target=`target_chat`, new=`"active"`) after `db.upsert_user` succeeds; the block branch is the mirror, recording `user_block` (new=`"blocked"`). |
| `tests/test_audit_capture.py` | Created | 32 tests: one happy-path test per action in the module's scope (undo × text/button/diary-value, target set × command/nl + clear, remind set/off/default, lang set, quiet set/off, admin approve/invite/block, handle_gate unknown→pending), read-only-does-not-record checks (target show/show_all/invalid, remind show, lang/quiet invalid, non-owner approve, repeat pending), one fail-open test per capture site (undo, target, remind, lang, quiet, admin), and the privacy test (AC-P1). |

I did **not** touch `core/commands.py`, `core/i18n.py`, or `main.py` — per the dispatch's scope and SPEC-v1.3.md §11's file-ownership table, those belong to `audit-view` and to the later integration step respectively. (`core/commands.py`/`core/i18n.py` already carry the sibling `audit-view` module's landed changes in this working tree, confirmed via `git status` — untouched by this pass.)

## How it works

Each of the five execute modules already had a "read what's there → write → format a reply" shape from v1.1/v1.2; this pass inserts one `audit.record(...)` call immediately after each module's own successful DB write, using values the module was already computing (or now reads one extra time, before the write, when the prior value wasn't already in hand — e.g. `schedules._execute_off`/`_execute_default` didn't previously need the prior times, `access.execute_admin` didn't previously need the prior status). Every capture site follows the same three-part pattern: (1) read the "old" value before the write if not already available, (2) perform the write exactly as before — completely unchanged — inside its existing try/except, (3) call `audit.record(db, actor=..., action=..., source=..., entity=..., old_value=..., new_value=...)` *after* the write succeeds, passing plain Python values (never pre-stringified) and ignoring the call's return. Because `audit.record` is itself fail-open (it swallows every exception internally, per the shared surface's `core/audit.py`), no capture site wraps the call in its own try/except — a forced `db.insert_audit` failure (exercised in every capture site's own fail-open test) leaves the site's write, return value, and reply completely unaffected. Read-only branches (`/target show`/`show_all`, `/remind show`, and every validation-failure early return) never reach a write, so they never call `record` either, keeping `AC-C7`'s "no audit row for a read-only action" property.

## Smoke test done

1. `.venv\Scripts\python.exe -m pytest -q tests/test_audit_capture.py` → **32 passed**.
2. `.venv\Scripts\python.exe -m pytest -q tests/test_undo_ui.py tests/test_targets.py tests/test_core_targets.py tests/test_target_nl.py tests/test_schedules.py tests/test_preferences.py tests/test_access.py tests/test_v12_access_gaps.py` → **459 passed** — every pre-existing test for the five modules this pass touched stays green (R-C5: no reply/flow change), confirming the new `source`/audit-recording additions are additive-only.
3. Full suite: `.venv\Scripts\python.exe -m pytest -q` → **1440 passed, 1 skipped**, run twice for stability (no flakiness). This total already includes the sibling `audit-view` module's own 58 tests (landed concurrently in this shared working tree, confirmed via `test_audit_view.py`'s presence and `commands.py`/`i18n.py` diffs I did not author) on top of the shared surface's own 1350 and this pass's 32 — `1350 + 58 + 32 = 1440`, zero failures anywhere, zero regressions from either parallel module.
4. Ad hoc syntax/import check: `python -c "import ast; ast.parse(...)"` on all five edited files, then `python -c "from habit_assistant.core import undo_ui, targets_command, schedules, preferences, access, audit"` — both clean before running pytest.
5. Never touched `data\habits.db`; every test uses `tmp_path`-backed on-disk SQLite `Database` instances (mirrors every prior version's own test convention). The live Task Scheduler service was not stopped, started, or otherwise touched.

## Maps to acceptance criteria

- **AC-C1** (undo) → `core/undo_ui.py:send_undo_confirmation` (records `undo`, `source` defaults `"command"`, `handle_undo_callback` passes `"button"`) → `tests/test_audit_capture.py::test_undo_text_path_records_command_source`, `::test_undo_button_path_records_button_source`, `::test_undo_diary_records_text_value`.
- **AC-C3** (target) → `core/targets_command.py:_execute_set`/`_execute_clear` → `::test_target_set_records_command_source_with_prev_and_new_goal`, `::test_target_set_full_nl_path_records_nl_source` (calls `execute_target(..., source="nl")` directly — the actual `main.py` call site that will pass this is documented below for integration), `::test_target_clear_records_prev_override_and_null_new`.
- **AC-C4** (remind) → `core/schedules.py:_execute_set`/`_execute_off`/`_execute_default` → `::test_remind_set_records_prev_times_json_and_new_times`, `::test_remind_set_records_prior_custom_times_as_old_value`, `::test_remind_off_records_prev_times_and_off_literal`, `::test_remind_default_records_prev_times_and_null_new`.
- **AC-C5** (lang/quiet) → `core/preferences.py:execute_lang`/`execute_quiet` → `::test_lang_set_records_prev_and_new_pref`, `::test_quiet_set_records_prev_json_and_new_windows`, `::test_quiet_off_records_prev_json_and_empty_list_new`.
- **AC-C6** (admin) → `core/access.py:execute_admin`/`handle_gate` → `::test_admin_approve_records_user_approve_with_target_and_prev_status`, `::test_admin_invite_is_recorded_as_user_approve`, `::test_admin_block_records_user_block_with_prev_status`, `::test_admin_approve_by_non_owner_does_not_record`, `::test_handle_gate_unknown_chat_records_user_pending`, `::test_handle_gate_pending_chat_second_message_does_not_record_again`.
- **AC-P1** (privacy) → `core/access.py:handle_gate`'s unknown→pending branch never passes `text` to `audit.record` → `::test_pending_transition_never_stores_message_text` (asserts the actual message text appears nowhere in the row, and that `logs` still has no row for the pending chat).

Also covered, though owned collectively per §11 rather than by this module alone: the read-only half of **AC-C7** for these five modules' own show/usage/invalid-input branches (`::test_target_show_does_not_record`, `::test_target_show_all_does_not_record`, `::test_target_invalid_value_does_not_record`, `::test_remind_show_does_not_record`, `::test_lang_invalid_value_does_not_record`, `::test_quiet_invalid_window_does_not_record`), and a fail-open test per capture site beyond `core/audit.py`'s own generic ones (`::test_undo_fail_open_when_recorder_raises`, `::test_target_fail_open_when_recorder_raises`, `::test_remind_fail_open_when_recorder_raises`, `::test_lang_fail_open_when_recorder_raises`, `::test_quiet_fail_open_when_recorder_raises`, `::test_admin_fail_open_when_recorder_raises`).

Not owned by this pass (explicitly deferred to integration per SPEC-v1.3.md §11): **AC-C2** (edit, `main.py:_execute_edit`), the plain-habit-log/read-only-command half of **AC-C7** across the whole app, **AC-V1/AC-V2/AC-V3** (`audit-view`'s own scope).

## Integration wiring instructions (for the integration step, `main.py`)

Three changes, all in `src/habit_assistant/main.py`, none of which this pass made:

1. **Full-NL target path — add `source="nl"`.** At the `execute_target` call inside the `target_nl.classify_target_intent` branch (currently around line 761-763):
   ```python
   reply = await targets_command.execute_target(
       set_command, db=db, config=config, registry=registry, lang=lang, user_id=user_id, source="nl"
   )
   ```
   The deterministic `/target` command call (around line 690-692) needs **no change** — it already gets `source="command"` for free from the new parameter's default.

2. **`_execute_edit` — record the `edit` action (AC-C2).** `_execute_edit` (line 225) already has both values in hand at the right point: `row` is fetched at line 252 *before* the write, so `row["value_num"]` is still the pre-update value when read right after line 257's `db.update_value(...)` call. Add, immediately after that line:
   ```python
   db.update_value(row["id"], value_num=command.value_num)
   audit.record(
       db,
       actor=user_id,
       action="edit",
       source="command",
       entity=command.category,
       old_value=row["value_num"],
       new_value=command.value_num,
   )
   ```
   (requires `from habit_assistant.core import audit` added to `main.py`'s existing import block — `main.py` does not import `core.audit` yet). There is no NL/button variant of edit (§2.1's table lists only `source=command` for `edit`), so no parameter threading is needed here, unlike `target`.

3. **No change needed for**: the deterministic `/remind`, `/lang`, `/quiet` call sites (lines 643, 653, 661) — none of them has an NL/button counterpart, so the new `source` parameters' `"command"` defaults are already correct as called. Likewise `_execute_undo` (line 725, delegates to `undo_ui.send_undo_confirmation` with no `source` argument, correctly defaulting to `"command"`) and the button path (`handle_undo_callback`, already passes `"button"` internally in `core/undo_ui.py` — no `main.py` involvement at all for that one).

After wiring step 1 and 2, re-run the full suite — expect **1440 + (whatever `audit-view` module's own pending work adds)** passing, plus any new integration-level tests Vera/the integration pass adds for AC-C2 and the full end-to-end `/audit` cross-check (§11 "Integration order" step 3).

## Known limitations

- **`access.py`'s pre-write "previous status/text" reads are best-effort, not transactional.** `db.get_user(target_chat)` (approve/block) and the equivalent reads in `preferences.py`/`schedules.py` happen as a separate statement just before the write, not inside a single transaction with it. This app is single-threaded async (one event loop, no concurrent request handling), so there is no real TOCTOU window in practice — flagging only because a future multi-worker deployment would need to revisit this, exactly the same caveat every pre-v1.3 "read old value, then write" call site in this codebase already carries (e.g. `targets_command._execute_set`'s own `previous_goal` read).
- **`quiet_off`'s `new_value` is passed as `[]` (an empty Python list), not the literal string `"off"`** — unlike `remind_off`, which spec's §2.1 table explicitly gives the string literal `"off"`. This is deliberate: quiet's own DB write already stores `"[]"` (an empty JSON array, R-P2's own "no quiet hours for me" semantics) rather than any `"off"` sentinel string the way `user_reminder_times` does, so the audit row mirrors what's actually stored for that column rather than inventing a new sentinel word `remind` doesn't share.
- **Edit (AC-C2) is fully unimplemented until integration wires it** — by design, since `_execute_edit` lives in `main.py`, which this module's dispatch explicitly excludes. See "Integration wiring instructions" above for the exact patch.
- **No test exercises the true end-to-end NL target path** (`target_nl.classify_target_intent` → `main.py`'s NL branch → `execute_target(..., source="nl")`) — `test_target_set_full_nl_path_records_nl_source` calls `execute_target` directly with `source="nl"`, proving this module's own parameter threading works; the full `main.py` wire-up (step 1 above) and its own integration test are explicitly deferred.

## Iteration log

No Vera round yet — this is the initial hand-off. One self-caught issue during development: my first draft of `tests/test_audit_capture.py` used non-numeric placeholder chat ids (`"owner-chat"`, `"member-chat"`) for the admin tests, which `core/access.py`'s existing `_CHAT_ID_RE = re.compile(r"^-?\d+$")` validation (pre-existing, unrelated to this pass) rejected as malformed, silently routing every admin test through the `admin_usage` reply instead of the approve/block path — caught by `_only_row`'s assertion failing with zero rows, not by a wrong-value assertion. Fixed by switching to numeric-string chat ids (`"1574572064"`/`"88899900"`/`"55544433"`, matching `tests/test_access.py`'s own existing convention) — a test-fixture bug, not a production-code bug; no source file needed a change for this.
