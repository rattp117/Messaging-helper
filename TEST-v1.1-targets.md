# Test Report — v1.1.0 `targets` module (per-habit target set/show/clear + full-NL target-setting)

## Summary
- Scope: 14 ACs owned by the `targets` module (SPEC-v1.1.md §11): AC13–AC20, AC27–AC30, AC32, AC34.
- Total (module files, `tests/test_targets.py` + `tests/test_target_nl.py`): 92 tests (53 + 39), of which 16 are new (Vera additions).
- Full repo suite: 862 passed, 7 failed (all pre-existing/unrelated, see below), 1 skipped.
- Passed: 92 / 92 (module scope), 862 / 869 (full repo, excluding the 7 documented pre-existing failures = 862/862 relevant).
- Failed (in scope): 0
- Status: **PASS**

## Test files
| Path | Tests added (this pass) | Covers which ACs |
|---|---|---|
| `tests/test_targets.py` | 8 new (of 53 total) | AC13–AC20, AC27 (adversarial hardening) |
| `tests/test_target_nl.py` | 8 new (of 39 total) | AC29, AC30, AC32, AC34 (fail-closed hardening) |

New tests added this pass (all passing):
- `test_unconfigured_or_malformed_unit_tokens_fail_closed_to_usage` (parametrized x4: `"2.5 L"`, `"2,000"`, `"2.5 ลิตร"`, `"2 500"`) — unit tokens not in `water`'s configured alias table must fail closed to `usage`, never silently mis-parse.
- `test_configured_unit_alias_with_no_space_still_resolves` — `"2000ml"` (no space) resolves identically to `"2000 ml"`.
- `test_ordinary_log_messages_never_dispatch_as_target` (parametrized x5, including the two literal examples from the dispatch brief: `"ดื่มน้ำ 500"`, `"I drank 2.5L this morning"`) — deterministic path never swallows a log, independent of the LLM gate.
- `test_ac34_llm_client_raising_directly_returns_none_not_a_crash` — a raw `ConnectionError` from the LLM client (not an HTTP-shaped failure) is still caught by `classify_target_intent`'s bare `except Exception`.
- `test_ac34_non_numeric_goal_string_returns_none` — a non-numeric `goal` string (`"a lot more"`) fails closed via the `float()` cast's `ValueError`.
- `test_ac32_a_log_with_a_number_and_unit_but_no_daily_marker_is_never_set_by_the_classifier` — belt-and-suspenders: a mis-answered-but-low-confidence "500ml" response still resolves to `None`.

## AC coverage
| AC | Test(s) | Status |
|---|---|---|
| AC13 | `test_ac13_target_water_2000_sets_override_and_replies_with_previous_goal`, `test_ac13_dispatch_shape` | PASS |
| AC14 | `test_ac14_target_water_3_bottles_multiplies_unit_alias`, `test_ac14_dispatch_shape` | PASS |
| AC15 | `test_ac15_non_positive_value_writes_nothing_and_replies_invalid_value` (0, -5) | PASS |
| AC16 | `test_ac16_unknown_habit_writes_nothing_and_lists_tracked_ids` | PASS |
| AC17 | `test_ac17_text_habit_writes_nothing_and_replies_not_goalable` | PASS |
| AC18 | `test_ac18_clear_synonyms_revert_to_config_default` (default/reset/clear/ค่าเริ่มต้น), `test_clear_on_a_previously_goalless_habit_reports_no_goal_variant` | PASS |
| AC19 | `test_ac19_show_single_habit_with_override_shows_default_note`, `test_show_single_habit_with_no_override_omits_default_note` | PASS |
| AC20 | `test_ac20_show_all_lists_every_habit`, `test_show_all_reflects_an_active_override` | PASS |
| AC27 | `test_ac27_slash_and_thai_anchored_forms_produce_the_same_set_command`, `test_ac27_short_thai_trigger_also_works`, `test_ac27_adversarial_corpus_never_dispatches_as_target` (10-message corpus, shared with `test_commands.py`'s AC5.5 corpus), `test_ac27_target_command_never_touches_the_llm`, plus new `test_ordinary_log_messages_never_dispatch_as_target` | PASS |
| AC28 | `test_ac28_set_target_db_failure_replies_save_failed_not_a_traceback`, `test_ac28_clear_target_db_failure_replies_save_failed_not_a_traceback` | PASS |
| AC29 | `test_ac29_english_free_form_classifies_water_2500`, `test_ac29_end_to_end_through_execute_target_sets_the_db_override` | PASS |
| AC30 | `test_ac30_thai_free_form_classifies_water_2500`, `test_ac30_end_to_end_sets_water_target_no_log` | PASS |
| AC32 | `test_ac32_a_log_classified_as_unknown_returns_none`, `test_ac32_low_confidence_valid_shaped_response_still_returns_none`, `test_ac32_confidence_threshold_is_read_from_config`, `test_ac32_500ml_bare_log_classified_unknown_returns_none`, plus new `test_ac32_a_log_with_a_number_and_unit_but_no_daily_marker_is_never_set_by_the_classifier` | PASS |
| AC34 | `test_ac34_malformed_json_returns_none_not_a_crash`, `test_ac34_unconfigured_habit_category_returns_none`, `test_ac34_non_goalable_habit_category_returns_none`, `test_ac34_non_positive_goal_returns_none` (x3), `test_ac34_transport_error_returns_none_not_a_crash`, `test_ac34_missing_confidence_field_defaults_closed_not_open`, `test_ac34_null_goal_with_unknown_category_returns_none`, plus new `test_ac34_llm_client_raising_directly_returns_none_not_a_crash`, `test_ac34_non_numeric_goal_string_returns_none` | PASS |

All 14 owned ACs: **PASS**. No AC in scope is untestable or ambiguous as written.

Supplementary (not owned by `targets` per SPEC-v1.1.md §11's table, but incidentally exercised by this module's tests and confirmed green): the NL-setting half of AC31 (`test_ac31_stretch_goalless_habit_gets_a_goal_from_nl_intent` — a target on the goal-less `stretch` habit takes effect via `effective_goal`, and clearing reverts it to `None`). The reminder-skip/streak/summary half of AC31, and AC33 (outage routing), are shared-surface/integration-owned and out of this pass's scope — not re-verified here.

## Adversarial verification performed (beyond Luna's own tests)

1. **Fail-closed guarantees (AC32/AC34).** Confirmed via mocked `OllamaClient` (real `chat_json` call over `httpx.MockTransport`, never a real network call): malformed JSON, `"unknown"` category, unconfigured/hallucinated category, non-goalable category (`diary`), `goal <= 0` (0, -5, -0.5), non-numeric `goal` string, missing `confidence` field, below-threshold confidence (both default 0.55 and a custom 0.9), a bad HTTP status (503), and — new this pass — the LLM client raising a bare `ConnectionError` directly (not an HTTP-shaped failure). Every path returns `None`, never raises, never writes to `habit_targets`.
2. **Ordinary logging messages never reach the classifier as a target-set, or get swallowed.** Verified at two independent layers: (a) the cost gate `looks_like_target_phrasing` returns `False` for `"ดื่มน้ำ 500"`, `"I drank 2.5L this morning"`, `"500ml"`, `"I drank 2.5L"`, `"did 10 min stretch"` (existing + new coverage); (b) the deterministic `commands.dispatch` never classifies these (or `"drank 2 bottles of water today"`, `"2.5L"`, `"logged 2000ml just now"`) as `"target"`, independent of any LLM involvement — `dispatch` has zero LLM dependency, so this guarantee holds even during an Ollama outage.
3. **Unit normalization (AC29/AC30).** The full-NL path trusts the model's own unit conversion (per R-T15) — verified `"2.5L"` → 2500, and the Thai equivalent `"ต่อไปอยากดื่มน้ำวันละ 2.5 ลิตร"` → 2500, both end-to-end through `execute_target` into the DB. On the **deterministic** path, added coverage for unit tokens NOT in the configured alias table (`"L"`, comma-separated `"2,000"`, Thai `"ลิตร"`, a stray second number) — all fail closed to `target_action="usage"` rather than guessing, and confirmed a no-space alias (`"2000ml"`) still resolves correctly.
4. **Deterministic path during an Ollama outage (AC27 scope).** `test_ac27_target_command_never_touches_the_llm` uses an LLM stub whose `chat_json`/`chat_text` both raise `AssertionError` if called — `/target water 2000` and the equivalent Thai form execute successfully with zero LLM involvement. The adversarial corpus (shared with the v0.5/v0.8 corpus in `tests/test_commands.py`) confirms no false positives on real logs, LLM-free.
5. **Goal-less habit gets a target (R-T5b, incidental to AC31).** `stretch` (duration, no config goal) starts at `effective_goal() is None`; a full-NL intent sets it to 20.0; clearing reverts to `None`. Confirmed via both the deterministic path (`test_ac18`'s sibling `test_clear_on_a_previously_goalless_habit_reports_no_goal_variant`, using `target_cleared_nogoal`) and the NL path.
6. **Bilingual replies.** Every reply assertion compares against `i18n.t(...)` with the exact kwargs the code path uses (not hardcoded strings), so a catalog-template change would be caught; Thai-language assertions (`test_ac30_end_to_end_sets_water_target_no_log` uses `lang="th"`, `test_clear_on_a_previously_goalless_habit_reports_no_goal_variant` mixes Thai clear-word `ค่าเริ่มต้น`) confirm correct language selection with no mojibake (UTF-8 round-trips cleanly through pytest's own reporting).
7. **Mock Ollama, real DB.** Every LLM-touching test in `test_target_nl.py` uses a real `OllamaClient` over `httpx.MockTransport` (exercises the actual `chat_json` call, JSON parsing, and fallback chain — not a hand-rolled stub). Every DB-touching test in `test_targets.py`/`test_target_nl.py` uses a real on-disk SQLite `Database` under `tmp_path`. `data/habits.db` and the live service were never touched by this pass.

## Cross-module failure attribution — `tests/test_undo_ui.py::test_handle_undo_callback_astronomically_large_id_does_not_raise`

**Question posed:** Luna's targets-module report showed 846 passed / 8 failed, with 1 failure new in the `undo-ui`-owned `tests/test_undo_ui.py`, appearing after her `core/commands.py` changes (new `"target"` `CommandKind`, `target_action` field, anchored triggers). Does the targets-module change break something undo-ui legitimately depends on, or is the undo-ui test brittle/flaky independent of the targets changes?

**Investigation:**
1. **Dependency check.** `src/habit_assistant/core/undo_ui.py` imports only `channels.base` (`Button`/`Channel`), `config.Config`, `core.i18n`, `core.targets` (the shared-surface `effective_goal`), `core.habits.HabitRegistry`, and `storage.db.Database`. It imports **nothing** from `core.commands` or `core.targets_command` — the two files this module's changes touched. `handle_undo_callback`'s astronomically-large-id guard (`_SQLITE_MAX_INTEGER` bounds check, `src/habit_assistant/core/undo_ui.py:47,224-226`) is a pure regex-match + integer-comparison, entirely self-contained, with zero code-path overlap with `commands.dispatch`'s new `"target"` kind. There is no mechanism by which the targets module's additive changes to `core/commands.py` (a new `CommandKind` literal member, a new dataclass field with a default, and new pattern-matching branches checked *after* undo/edit/snooze) could alter this test's behavior.
2. **Reproduction.** Ran the failing test in isolation: **1 passed** (immediate, no flake). Ran the full `tests/test_undo_ui.py` module alone: **36 passed** (0 failed). Ran the full repository suite **5 times** after the targets-module changes and the adversarial hardening added in this pass: **7 failed / 862 passed** every single time, with `test_handle_undo_callback_astronomically_large_id_does_not_raise` passing cleanly in all 5 runs. The project has no `pytest-randomly`/`pytest-xdist` (checked `pyproject.toml`'s dependency list) and no test ordering nondeterminism — collection order is fixed (filesystem/alphabetical), so this was not an order-dependency artifact reproducible on demand.
3. **Conclusion.** This is **not** a targets-module regression (attribution **(b)**: owned by `undo-ui`, and specifically a **non-reproducible environmental flake**, not even a genuinely brittle assertion). The guard code is correct and self-contained — the module's own docstring notes it was *already* added in response to a prior Vera finding (`_SQLITE_MAX_INTEGER` bounds check exists specifically to prevent `sqlite3.OverflowError` from an out-of-range id, and it does so *before* any DB call). Luna's one observed failure in her original 846/8 run was most likely a transient resource/timing hiccup during a ~90-second full-suite run (matplotlib chart rendering, multiple SQLite tmp-file databases, etc. running concurrently in the same process) rather than a logic defect triggered by anything in `commands.py`'s additive changes.
4. **No action needed** from either module. Nothing to hand back to Luna (targets) or the undo-ui module's own maintainer — recommend Archi treat this as noise unless it recurs with a captured traceback (in which case the exact assertion and stack trace would immediately show whether it's a real assertion failure vs. an infrastructure error, neither of which was observed here).

## Regressions detected
None. The full repository suite's only failures are the 7 pre-existing, unrelated failures already documented by `IMPL-v1.1-shared.md`/`IMPL-v1.1-targets.md`/`IMPL-v1.1-undo-ui.md` and independently re-confirmed here:
- `tests/test_adaptive_reminders.py::test_send_reminder_skipped_when_goal_already_met`
- `tests/test_adaptive_reminders.py::test_send_reminder_goal_exactly_met_is_skipped`
- `tests/test_adaptive_reminders.py::test_send_reminder_updates_state_only_when_actually_sent`
- `tests/test_v09_gaps.py::test_goal_met_reminder_skipped_via_real_scheduled_job_and_logged`
- `tests/test_v09_gaps.py::test_goal_exactly_met_is_skipped_via_real_scheduled_job_matching_documented_ge`
- `tests/test_v09_gaps.py::test_skip_if_goal_met_false_disables_only_that_habit_others_still_skip`

  (all 6: hardcoded seed date `"2026-08-19T09:00:00"` has drifted behind the real "today", 2026-08-21 — confirmed directly: `test_send_reminder_skipped_when_goal_already_met` fails because the reminder is no longer suppressed, i.e. the seeded log no longer falls on "today" — pure date-drift, unrelated to goal resolution logic)
- `tests/test_charts.py::test_version_is_consistent_across_version_file_pyproject_and_init` (asserts `VERSION == "1.0.0"`; actual is `"1.0.1"` — stale pin since the v1.0.1 release, confirmed by direct read of the `VERSION` file)

None of these touch `core/commands.py`, `core/targets_command.py`, `core/target_nl.py`, `llm/prompts.py`, or their tests.

## Recommendation
**Ready to ship.** All 14 owned ACs (AC13–AC20, AC27–AC30, AC32, AC34) pass, including 16 new adversarial tests added this pass covering unit-token edge cases, ordinary-log non-swallowing at both the gate and dispatch layers, and additional fail-closed classifier failure modes. The `tests/test_undo_ui.py` failure Luna flagged is attributed to `undo-ui` as a non-reproducible environmental flake with zero code coupling to this module's changes — not a targets-module defect, and not currently blocking (0/5 reproductions after the fact). No spec gaps found; no escalation to Archi needed for this module.
