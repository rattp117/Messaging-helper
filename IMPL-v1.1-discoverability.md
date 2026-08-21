# Implementation — v1.1.0 `discoverability` module (`/help` + `/habits`)

Scope: the **`discoverability` module** per SPEC-v1.1.md §11 — a sequential
follow-on landed after the shared surface + `undo-ui`/`targets` integration
were both green (881 passed / 0 failed / 1 skipped, per the coordinator's
integration Vera confirmation). This module edits `core/commands.py` (the
same file the `targets` module already touched) and `main.py`, so it was
explicitly not parallel-safe with the earlier work — hence sequential, last.

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/core/discoverability.py` | Created | `build_help_text(config, lang)` / `build_habits_overview(db, config, registry, clock, lang)` — the two deterministic, LLM-free, read-only formatters (R-D2/R-D3) |
| `src/habit_assistant/core/commands.py` | Modified | `CommandKind` gained `"help"`/`"habits"`; `_HELP_RE`/`_HABITS_RE` (anchored to the five exact literal strings R-D1 names) + `_match_help`/`_match_habits`; wired into `dispatch()` after `target` and before `query` |
| `src/habit_assistant/core/i18n.py` | Modified | All new `help_*`/`habits_overview_*`/`habit_kind_*` catalog keys, EN+TH (R-D5) |
| `src/habit_assistant/main.py` | Modified | `command.kind in ("help", "habits")` routed to the two formatters (before the health-monitor deferral check, alongside `query`/`snooze`/`target`); `DISCOVERABILITY_COMMAND_DESCRIPTIONS` added to the startup `set_my_commands` merge |
| `tests/test_discoverability.py` | Created | AC35–AC40, 38 tests |

No other production file was touched — `core/undo_ui.py`, `core/targets_command.py`, `core/target_nl.py`, and every shared-surface file from the earlier passes are exactly as integration left them.

## How it works

`core/commands.py:dispatch` now recognizes two more whole-message-anchored,
LLM-free shapes: `"help"` (`/help`, `ช่วยเหลือ`, `วิธีใช้`) and `"habits"`
(`/habits`, `นิสัย`), checked right after `target` and before `query` — the
same "anchored to the *whole* stripped message" conservatism as every
existing pattern in that module, so neither can ever fire on a real habit
log (R-C5/AC40). `main.py`'s command-dispatch block routes both kinds to
`core/discoverability.py`'s two formatters and sends the reply with plain
`send` (neither is a log confirmation, so neither gets an undo button) —
both branches sit inside the same `if command is not None:` early-return
block every other anchored command uses, which runs *before* the
health-monitor deferral check, so both work with Ollama down (AC35/AC37).

`build_help_text` assembles one line per required capability section (how
to log, undo, targets, NL queries, streaks/milestones, daily summary,
weekly review, snooze, quiet hours), reading every time/number value live
from `config` — nothing hard-coded (AC36). `build_habits_overview` walks
the registry in order, and for each habit resolves its goal via the shared
`targets.effective_goal(db, habit, config)` and marks it "your target"
(when `db.get_target(habit.id) is not None`), "default", or "no goal"
(AC38) — a genuinely independent second read, not inferred from the goal
value itself. Today's total uses `db.sum_value` for numeric/duration
habits, `db.count_true` for boolean, `db.count` for text (AC39), computed
from `clock().date().isoformat()` — the same pattern every other
clock-injected call site in `main.py`/`core/undo_ui.py` already uses for
"what day is it," so `config.app.timezone` correctness is the caller's
responsibility exactly as it is for those existing call sites, not a new
convention.

`main.py`'s startup `set_my_commands` merge gained a
`DISCOVERABILITY_COMMAND_DESCRIPTIONS` dict (mirroring
`TARGET_COMMAND_DESCRIPTIONS`'s own "no `core/i18n.py` catalog key for Bot
API menu copy" rationale, since `core/discoverability.py`'s public surface
per SPEC-v1.1.md §5 is only the two formatter functions, no
`command_menu_entries()`), so `/help` and `/undo` and `/target` and
`/habits` all register together, in one call, both languages (AC40).

## Smoke test done

1. Full suite: `.venv\Scripts\python.exe -m pytest -q` → **919 passed, 0
   failed, 1 skipped** (was 881 passed / 0 failed / 1 skipped at the
   coordinator's confirmed baseline — the +38 are exactly this module's own
   new tests; nothing else moved, no new failures).
2. `tests/test_discoverability.py` in isolation → **38 passed**.
3. A standalone smoke script (not committed, deleted after use), fully
   offline against a `tmp`-only SQLite file — never `data/habits.db` —
   printed both languages' `/help` text end to end and both an empty and a
   populated `/habits` overview, and asserted: EN/TH log examples both
   present regardless of reply language; `config.weekly_review.time`
   ("20:00"), `config.gamification.daily_summary_time` ("21:45"),
   `config.snooze.default_minutes` ("30"), and `config.gamification.
   milestones` ("3, 7, 30") all appear verbatim; a fresh registry with no
   overrides shows water's config default (2500) and stretch/diary as "no
   goal"; setting a water override flips its line to "your target"/2000 and
   a seeded 500 ml log produces the literal phrase "today 500 ml"; and
   `commands.dispatch` correctly classifies `/help`, `ช่วยเหลือ`, `วิธีใช้`,
   `/habits`, `นิสัย` while still returning `None` for `"500ml"`/`"ดื่มน้ำ 2
   แก้ว"`. All passed, matching the formal test suite's assertions.
4. Never ran the app, `--seed`, `--dry-run`, or any test against
   `data/habits.db`; the live Task Scheduler service was not touched.

## Maps to acceptance criteria

- **AC35** → `core/commands.py:_match_help` + `main.py`'s `"help"` branch
  (routed before the deferral check, `_NeverCalledLLM` proves zero LLM
  dependency) + `tests/test_discoverability.py::test_ac35_dispatch_
  recognizes_help_triggers` / `test_ac35_help_reply_matches_build_help_
  text_and_works_with_ollama_down` / `test_ac35_help_reply_language_
  follows_resolve_reply_language`.
- **AC36** → `core/discoverability.py:build_help_text` +
  `test_ac36_help_text_covers_every_required_section` (all nine sections,
  one assertion each) / `test_ac36_help_text_values_are_read_live_from_
  config_not_hardcoded` (two configs, two different times, no
  cross-contamination) / `test_ac36_help_text_daily_summary_off_omits_a_
  time_but_still_has_a_section` / `test_ac36_help_text_quiet_hours_empty_
  still_has_a_section` / `test_ac36_help_text_snooze_minutes_change_with_
  config`.
- **AC37** → `core/commands.py:_match_habits` + `main.py`'s `"habits"`
  branch + `test_ac37_dispatch_recognizes_habits_triggers` /
  `test_ac37_habits_reply_matches_build_habits_overview_and_works_with_
  ollama_down` / `test_ac37_every_registered_habit_appears_in_registry_
  order` (4 habit types, one of each, order-checked).
- **AC38** → `core/discoverability.py:_goal_phrase` (independent
  `effective_goal`/`get_target` reads) + `test_ac38_override_marked_as_
  your_target_vs_default_vs_no_goal` / `test_ac38_config_default_marked_
  default_not_your_target` / `test_ac38_clearing_the_override_reverts_the_
  mark_to_default` / `test_ac38_target_on_a_previously_goalless_habit_is_
  marked_your_target`.
- **AC39** → `core/discoverability.py:_today_phrase` +
  `test_ac39_todays_water_total_shown_correctly` (literal "today 500 ml") /
  `test_ac39_only_todays_logs_count_not_other_days` (yesterday's 9999
  excluded) / `test_ac39_boolean_and_text_totals_use_count_not_sum`
  (`count_true` vs `count` distinction, including a falsy boolean entry
  correctly excluded).
- **AC40** → `main.py`'s `DISCOVERABILITY_COMMAND_DESCRIPTIONS` merge +
  `test_ac40_startup_registers_help_and_habits_alongside_undo_and_target`
  (a real `async_main` run, both languages, all four commands present,
  genuinely localized not copy-pasted) + `test_ac40_adversarial_corpus_
  never_dispatches_as_help_or_habits` (16 messages, including
  discoverability-specific mid-sentence "help"/"habits"/"ช่วยเหลือ"/"นิสัย"
  cases beyond the pre-existing corpus).

Every AC35–AC40 is verified. No other AC changed — undo-ui/targets/shared-
surface ACs are untouched by this pass.

## Known limitations

None. This module is small, self-contained, purely additive (no schema
change, no new dependency, no LLM call on either path — R-D5), and lands
cleanly on top of an already-stabilized integration. `core/discoverability.py`
never imports a concrete channel (mirrors `core/reminders.py`/`core/
query.py`'s existing "no channel imports" seam) and never writes to the DB.
