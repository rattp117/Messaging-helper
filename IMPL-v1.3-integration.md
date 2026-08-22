# Implementation — v1.3.0 Audit log (integration step)

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/main.py` | Modified | Imports `audit`/`audit_view` from `habit_assistant.core`; full-NL target call site (`target_nl.classify_target_intent` branch) now passes `source="nl"` to `execute_target`; `_execute_edit` records `action="edit"` via `audit.record` immediately after `db.update_value`, using the pre-fetched `row["value_num"]` as `old_value`; `on_message` routes `command.kind == "audit"` to `audit_view.render_recent` behind an `access.classify(db, chat_id) == "owner"` re-check, silent no-op for a non-owner; `/audit` confirmed absent from `set_my_commands`/`command_menu` (no change needed there — already correctly excluded) |
| `tests/test_v13_integration.py` | Created | 8 end-to-end tests driving the REAL wired `async_main`/`on_message` closure (not re-implemented): the full NL-target→log→edit→owner-`/audit` life-cycle (newest-first, correct `source` per row), AC-C7 (a real plain log + 4 real read-only commands write zero audit rows), AC-V3 (non-owner `/audit` silent + `/audit` absent from the registered menu), AC-A2 at the fully-wired level (a forced `db.insert_audit` failure leaves `/target`'s own reply and write untouched), the deterministic `/remind`/`/lang`/`/quiet`/text-`/undo` sites' `source="command"` default re-verified live, the button-`/undo` path's `source="button"` re-verified live, and AC-R1 re-verified with a realistic mixed-action audit volume across two simulated process starts |

No other file changed — every production file this step needed to touch was already built and unit-tested by the shared surface (`core/audit.py`, `storage/db.py`, migration 007) or the two parallel modules (`core/undo_ui.py`, `core/targets_command.py`, `core/schedules.py`, `core/preferences.py`, `core/access.py`, `core/audit_view.py`, `core/commands.py`, `core/i18n.py`). I independently re-verified (not just trusted) `IMPL-v1.3-capture.md`'s claim that the five deterministic call sites need no `main.py` change — grepped each of `undo_ui.py`/`targets_command.py`/`schedules.py`/`preferences.py` directly and confirmed every `source: str = "command"` default and every `source=source` threading is exactly as documented before writing a single line of `main.py`.

## How it works

`on_message` now has three sequential concerns before it ever reaches `handle_inbound_message`: the access gate (v1.2, unchanged), the five owner-only admin kinds (v1.2, unchanged), and now `"audit"` — checked in that same pre-dispatch block because `audit_view.render_recent` needs `owner_chat_id` (to render the owner's own rows as "you"), which `handle_inbound_message` doesn't have, and because `/audit` must work with Ollama down, which only holds if it's answered before the health-monitor deferral check inside `handle_inbound_message`. A non-owner's `/audit` produces literally no reply — not a usage message, not an error — matching the exact "reveals nothing" posture the four true admin commands already have. Inside `handle_inbound_message`, the full-NL target branch is the one deterministic-vs-non-deterministic distinction this step had to make: the slash-form `/target` call (unchanged) gets `source="command"` for free from `execute_target`'s own default, while the full-NL branch (`target_nl.classify_target_intent` returning a hit) now explicitly passes `source="nl"`, because that's the entire reason the parameter exists for this action — an audit reader must be able to tell "the owner typed `/target water 2000`" apart from "the owner said 'from now on I want to drink 2L a day' and the LLM inferred it." `_execute_edit` is the one capture site that lives in `main.py` itself (not a parallel module's file), so recording `edit` was this step's own job: `row` is already fetched before `db.update_value` overwrites it, so `row["value_num"]` is still the pre-edit value at the exact point `audit.record` is called, one line after the write — no extra DB read needed.

## Smoke test done

1. Full suite: `.venv\Scripts\python.exe -m pytest -q` → **1497 passed, 0 failed, 1 skipped** (baseline before this pass: 1489 passed/1 skipped, matching the coordinator's stated number exactly — independently re-confirmed by my own run before starting any edit). The 8-test delta is exactly `tests/test_v13_integration.py`; zero behavior change anywhere else (AC-A3) — this step touched exactly one production file (`main.py`) and made exactly three surgical, additive changes to it.
2. `python -c "import habit_assistant.main"` — clean import immediately after adding the `audit`/`audit_view` imports and before running any test, catching a circular-import or typo early.
3. Ad hoc smoke script (not committed, deleted after use, run via `.venv\Scripts\python.exe`), driving the real, unmocked production functions directly (not through the full `async_main` harness, which `tests/test_v13_integration.py` already exercises exhaustively) — a real `/target water 2000` dispatch → `execute_target` → confirmed exactly one `audit_log` row auto-recorded by capture's own (unmodified-by-me) wiring, then a real `/audit` dispatch → `audit_view.render_recent`, output matching SPEC-v1.3.md §3.1's own illustrative shape:
   ```
   target reply: ✅ Set water's daily goal to 2000 ml. (was 2500 ml)
   audit rows after real /target: 1
     {'action': 'target_set', 'entity': 'water', 'old_value': '2500', 'new_value': '2000', 'source': 'command', ...}
   render_recent output:
   🧾 Recent activity (last 20):
   • 08-22 20:35 · you · target set · water · 2500 → 2000 (command)
   classify non-owner: unknown
   ```
4. Live-Ollama smoke against `mac-mini:11434` was not re-attempted this pass — `IMPL-v1.2-integration.md`'s own "Known limitations" already documented that this specific sandboxed Python process cannot reach it (network-namespace limitation, `curl` from the shell succeeds, `httpx` from this process does not) despite being explicitly permitted by the constraints; every NL-path test in `tests/test_v13_integration.py` instead monkeypatches `target_nl.classify_target_intent` directly (the same precedent `tests/test_v11_integration.py`/`tests/test_v12_integration.py` already established), which exercises the real `source="nl"` wiring this step added without depending on a live model call.
5. Never ran the app, `--seed`, `--dry-run`, or any test against `data/habits.db`; the live Task Scheduler service was not stopped, started, or otherwise touched.

## Maps to acceptance criteria

Integration-owned ACs (per SPEC-v1.3.md §11's table):

- **AC-C2** (edit) → `main.py:_execute_edit`'s new `audit.record(...)` call → `tests/test_v13_integration.py::test_full_flow_nl_target_then_log_then_edit_then_owner_audit_sees_all_newest_first` (asserts `entity="water"`, `old_value="500"`, `new_value="300"`, `source="command"` for a real `"make that 300ml"` edit).
- **AC-C7** (not-audited property, across capture + log/read paths) → structural (no capture site is reachable from a read-only command or a plain log) → `tests/test_v13_integration.py::test_plain_habit_log_and_read_only_commands_write_no_audit_row` (a real `500ml` log + `/habits` + `/help` + `/target water` show + `/remind water` show, through the real wiring, zero `audit_log` rows).
- **AC-V3** (owner-only routing + menu-hidden) → `main.py:on_message`'s new `command.kind == "audit"` branch → `tests/test_v13_integration.py::test_non_owner_audit_is_silent_and_reveals_nothing`, `::test_audit_never_added_to_the_public_command_menu`.
- **AC-A2** (fail-open), re-confirmed at the wired level → `tests/test_v13_integration.py::test_audit_db_failure_leaves_the_triggering_actions_reply_and_write_unchanged` (a forced `Database.insert_audit` failure through a real `/target water 2000` message — the reply and the DB write are both untouched, zero audit rows).
- **AC-A3** (regression gate) → the full 1497-test run above; every one of the pre-existing 1489 tests passes unmodified in behavior.
- **AC-R1** (retention), re-confirmed with realistic capture volume → `tests/test_v13_integration.py::test_startup_prune_correct_with_a_realistic_mixed_capture_volume` (4 real capture-generated rows from a first simulated process run, 2 backdated/ancient, a second simulated startup prunes exactly the 2 old ones and keeps the 3 un-backdated ones).

Also independently re-verified (not newly implemented, since already correct): the punch list's own explicit "verify that claim" for the five deterministic call sites (`/remind`, `/lang`, `/quiet`, text-`/undo`, button-`/undo`) needing no `main.py` change → `tests/test_v13_integration.py::test_deterministic_remind_lang_quiet_undo_all_record_source_command_by_default`, `::test_button_undo_records_source_button_through_the_real_on_callback`.

## Known limitations

- **Live-Ollama round-trip not re-attempted** — see "Smoke test done" item 4. Not a gap in test coverage (the mocked/monkeypatched suite is exhaustive), only in one specific kind of extra confidence that this particular sandbox can't currently provide.
- **The design note `TEST-v1.3-capture.md` flagged** (re-approving an already-`active` user via `/approve` still writes a `user_approve` audit row, since `execute_admin` doesn't compare the previous status before recording) was **not changed** by this pass — SPEC-v1.3.md's own R-C3/AC-C6 text doesn't ask for that comparison, capture's own Vera explicitly filed it as "a design note for Archi/Sophia, not a defect," and no spec revision request came through the coordinator's punch list for this step. Leaving as-is; flagging again here so it isn't lost between passes.
- **`audit_view.render_recent`'s known limitation (no unit suffix on `target_set`/`edit` values, e.g. `2500 → 2000` not `2500 → 2000 ml`)** — inherited unchanged from `IMPL-v1.3-view.md`; not in scope for this integration step (no AC requires it, and fixing it would mean `render_recent` building a `HabitRegistry` internally, a scope increase not on this step's punch list).

## Iteration log

No Vera round yet — this is the initial hand-off. All 3 `main.py` edits (import, `source="nl"`, the `_execute_edit` recorder call, the `on_message` audit route) worked on the first full-suite run with zero unexpected failures — the two parallel modules' own thorough IMPL docs (exact line numbers, exact code to paste, both independently re-verified against the current file before use) meant there was nothing left to discover at integration.
