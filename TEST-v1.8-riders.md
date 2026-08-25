# Test Report — v1.8.0 "riders" module (silent proactive sends)

## Summary
- Total (riders-scoped): 25 tests — 13 Luna's (`tests/test_riders.py`) + 12 Vera's (`tests/test_v18_riders_gaps.py`)
- Passed: 25
- Failed: 0
- Status: **PASS**

Scope per Archi's dispatch: **R-D1 / AC-D1** (silent proactive, all three ticks)
and **the silent-payload half of R-D4 / AC-D4** (`silent_proactive=false` →
byte-identical to v1.7). **AC-D2** (owner-scoped menu) and **AC-D3** (`/audit`
language fix) are explicitly **out of this verdict** — `main.py` integration
scope, not yet wired, per SPEC-v1.8.md §11's module table and Luna's own
IMPL-v1.8-riders.md.

Riders-relevant regression subset (`test_riders.py` + `test_reminders.py` +
`test_checkins.py` + `test_checkins_gaps.py` + `test_nudge.py` +
`test_nudge_gaps.py` + `test_adaptive_reminders.py` + my new file):
**223 passed, 0 failed.**

Full suite (tree state, for the record — routines/quicklog Lunas are still
mid-edit): **4 failed, 3668 passed, 1 skipped, 1 xfailed.** All 4 failures are
outside riders' scope and outside `core/reminders.py`/`core/checkins.py`/
`core/nudge.py`:
- `tests/test_commands.py::test_fresh_db_migrates_to_schema_version_10`
- `tests/test_heatmap.py::test_heatmap_adds_no_migration_of_its_own`
- `tests/test_history.py::test_no_migration_was_added_for_history`
- `tests/test_v18_shared_surface.py::test_commandkind_reserved_words_do_not_yet_dispatch[บันทึก]`

The first three are migration-011 (`routines` module) landing concurrently in
`storage/migrations.py`; the fourth is the `quicklog` module's own
`commands.py` dispatch work landing concurrently. (Down from the 21 failures
Luna's IMPL-v1.8-riders.md reported at her hand-off time — `routines`/`quicklog`
have made progress since; not riders' concern either way.) Not counted against
this verdict per Archi's brief.

## Test files

| Path | Tests | Covers |
|---|---|---|
| `tests/test_riders.py` (Luna's) | 13 | AC-D1, silent-payload half of AC-D4, source-sweep guard, structural no-channel proof for `execute_checkin`, fail-open re-proof for `run_due_nudges` |
| `tests/test_v18_riders_gaps.py` (Vera's, new) | 12 | AC-D1 (byte-identical text/chat_id cross-check per tick), silent-payload half of AC-D4, negative scope (undo, command replies, dashboard pin/edit), meta-proof of the source-sweep guard's detection power, fail-open/no-fail-open-regression for all three ticks, back-compat (`config=None`) |

## AC coverage

| AC | Test(s) | Result |
|---|---|---|
| **AC-D1** (silent proactive: all three ticks send with `disable_notification=True` under default config; confirmations/pin stay notifying) | `test_riders.py::test_send_reminder_default_config_sends_silently`, `::test_run_due_reminders_default_config_sends_silently`, `::test_run_due_checkins_default_config_sends_silently`, `::test_run_due_nudges_default_config_sends_silently`; `test_v18_riders_gaps.py::test_run_due_reminders_default_tick_sends_silently_text_identical_to_notifying`, `::test_run_due_checkins_default_tick_sends_silently_text_identical_to_notifying`, `::test_run_due_nudges_default_tick_sends_silently_text_identical_to_notifying` | **PASS** |
| **AC-D4, silent-payload half** (`silent_proactive=false` → proactive payloads byte-identical to v1.7; content unchanged, flag simply False) | `test_riders.py::test_send_reminder_silent_proactive_false_is_byte_identical_to_v17`, `::test_send_reminder_called_with_no_config_at_all_defaults_to_notifying`, `::test_run_due_reminders_silent_proactive_false_is_byte_identical_to_v17`, `::test_run_due_checkins_silent_proactive_false_is_byte_identical_to_v17`, `::test_run_due_nudges_silent_proactive_false_is_byte_identical_to_v17`; `test_v18_riders_gaps.py::test_send_reminder_silent_false_payload_matches_pre_v18_default_call_shape` | **PASS** |
| **AC-D2** (owner-scoped menu) | Out of scope — `main.py` integration, not yet wired | **NOT IN THIS VERDICT** |
| **AC-D3** (`/audit` language fix) | Out of scope — `main.py` integration, not yet wired | **NOT IN THIS VERDICT** |
| **Negative scope** (nothing outside the 3 tick sites goes silent) | `test_riders.py::test_exactly_three_call_sites_pass_disable_notification_and_they_are_the_three_ticks`, `::test_execute_checkin_the_user_initiated_reply_path_has_no_channel_parameter_at_all`; `test_v18_riders_gaps.py::test_undo_confirmation_never_goes_silent`, `::test_command_reply_functions_have_no_channel_parameter_structurally_incapable_of_going_silent`, `::test_dashboard_pin_and_edit_methods_do_not_even_accept_disable_notification`, `::test_source_sweep_guard_mechanism_would_catch_a_genuine_fourth_call_site` | **PASS** |
| **Fail-open intact** (a raising channel doesn't silently break the pre-existing contract of each tick) | `test_riders.py::test_run_due_nudges_fail_open_structure_is_unchanged_by_the_silent_flag`; `test_v18_riders_gaps.py::test_run_due_nudges_fail_open_fan_out_survives_flag_threading`, `::test_run_due_checkins_send_failure_propagates_same_as_pre_v18`, `::test_run_due_reminders_send_failure_propagates_same_as_pre_v18` | **PASS** |
| **Back-compat** (`send_reminder(config=None)` legacy call shape) | `test_v18_riders_gaps.py::test_send_reminder_with_config_none_works_and_is_non_silent` | **PASS** |

## Verification notes

**Source-level confirmation of the three send sites** (`git diff` reviewed
directly, not just taken on Luna's word):
- `core/reminders.py:send_reminder` — one-line change:
  `silent = config is not None and config.notifications.silent_proactive`
  then `await channel.send(chat_id, text, disable_notification=silent)`.
  `config=None` (legacy shape) → `silent=False`, matching the pre-existing
  `Config | None = None` back-compat contract. No surrounding structure
  (quiet-hours / goal-met checks) touched.
- `core/checkins.py:run_due_checkins` — one-line change: the existing
  `await channel.send(user_id, message)` gained
  `disable_notification=config.notifications.silent_proactive`. No
  try/except existed around this call before v1.8, and none was added — a
  send failure still propagates unchanged (confirmed by
  `test_run_due_checkins_send_failure_propagates_same_as_pre_v18`, which
  would itself fail if Luna had accidentally introduced new fail-open
  behavior here).
- `core/nudge.py:run_due_nudges` — one-line change inside the **pre-existing**
  two-stage `try/except` fail-open structure (unchanged): the flag is read
  as part of the same `channel.send(...)` call expression, not a new guarded
  block. Fail-open re-confirmed both by Luna's own re-proof and my
  independent `test_run_due_nudges_fail_open_fan_out_survives_flag_threading`.

**Shared surface spot-check** — `channels/base.py`'s `Channel.send` gained
`disable_notification: bool = False` as an additive keyword-only param
(default `False`); `set_my_commands` gained `scope_chat_id` similarly;
`set_message_reaction` was added as a concrete no-op default. All match
R-S1/R-S2/R-S3 as specified. `send_and_pin`/`edit_message` (the dashboard
pin/edit surface) were **not** touched — no `disable_notification` param
exists on either, so the dashboard pin is structurally incapable of going
silent regardless of config, independent of any test.

**Mechanical fake-widening claim spot-verified** (`git diff` on 4 of the 15
files Luna listed: `test_adaptive_reminders.py`, `test_cli.py`,
`test_v12_integration.py`, `test_v17_isolation_sweep.py`): every change is
exactly `async def send(self, chat_id, text) -> None` →
`async def send(self, chat_id, text, *, disable_notification: bool = False) -> None`
(or the equivalent for a 2-arg fake), with the body/assertions untouched.
Claim confirmed — no assertion content changed anywhere spot-checked.

**Negative scope, verified independently, not just via Luna's sweep:**
- `main.py` grepped directly for every `channel.send(...)` call site (23
  call sites found: undo/edit replies, snooze, command replies, deferred
  ack, clarifying question, etc.) — **none** pass `disable_notification=`.
- `core/undo_ui.py::send_undo_confirmation` called directly (no `main.py`
  needed) with a real on-disk DB and a `RecordingChannel`, under a config
  with `silent_proactive=True` (the default) — confirmed the undo
  confirmation still sends with `disable_notification=False`.
- `execute_lang`, `execute_quiet`, `execute_remind`, `execute_checkin`
  inspected via `inspect.signature` — none accept a `channel` or
  `disable_notification` parameter; all four are structurally incapable of
  going silent (Luna proved this for `execute_checkin` alone; extended to
  the three siblings here).
- Luna's source-sweep guard test was not just trusted at face value: I
  replicated its exact regex against a **synthetic** source tree (3 planted
  legitimate sites + 1 injected illegitimate site in a `main.py`-shaped
  file) and confirmed the sweep's match count changes from 3 → 4 and
  correctly names the offending file — proving the guard is a real,
  functioning detector that would catch a genuine fourth call site, not a
  tautology.

**Fail-open, byte-identical text, and back-compat** were all verified with
real calls against a real on-disk SQLite `Database` (no mocks) — matching
this codebase's existing test conventions.

## Failures (if any)

None in riders scope.

## Regressions detected

None. The riders-relevant subset (223 tests) is fully green, and the 4
full-suite failures are pre-existing/concurrent work by the `routines` and
`quicklog` tracks (migration-011 schema-version assertions and a
`commands.py` dispatch-not-yet-wired assertion respectively) — confirmed by
inspecting each failure's target file and confirming it touches none of
`core/reminders.py`, `core/checkins.py`, `core/nudge.py`, or
`tests/test_riders.py`/`tests/test_v18_riders_gaps.py`.

## Recommendation

**Ready to ship** — for the scope actually under test (R-D1/AC-D1, the
silent-payload half of R-D4/AC-D4). AC-D2 and AC-D3 remain to be verified
once the `main.py` integration pass wires the owner-scoped menu and the
`/audit` language fix; those are a separate verdict, not a blocker for this
one.
