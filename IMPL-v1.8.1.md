# Implementation — v1.8.1 gap-fix: `/help` missing v1.8.0 lines

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/core/i18n.py` | Modified | Added 3 catalog entries: `help_log_cmd` (after the `quicklog` block), `help_routine_cmd` (after the `routines` block), `help_backfill` (after the `backfill` block) — each EN+TH. |
| `src/habit_assistant/core/discoverability.py` | Modified | `build_help_text` now appends the three new lines after the existing `help_delhabit_cmd` append (same "later integration append" pattern as every prior release). `help_backfill` is rendered with `max_days=config.backfill.max_days_back` (live config read, not hard-coded). |
| `tests/test_discoverability.py` | Modified | Added 5 tests: `/log` mention (both languages), `/routine` mention (both languages), backfill syntax mention (both languages), `max_days` tracks `config.backfill.max_days_back` and isn't hard-coded, and a structural check that the new lines are a strict append after `/delhabit` with every pre-existing section still present. |

## How it works

`build_help_text(config, lang)` builds a list of `i18n.t(...)` calls and joins them with `"\n\n"`. The v1.8.0 integration pass added the Telegram command-menu entries for `/log`/`/routine` but never appended the corresponding lines to this function — the last two appended lines were still `help_addhabit_cmd`/`help_delhabit_cmd` from v1.7.0. I appended three more `lines.append(...)` calls at the end (before `return`): `help_log_cmd`, `help_routine_cmd`, then `help_backfill` (the last one templated with the live `config.backfill.max_days_back`, mirroring how `help_snooze` reads `config.snooze.default_minutes`). No other line, order, or behavior changed.

## Smoke test done

Ran `build_help_text(Config(), "en")` and `build_help_text(Config(), "th")` directly (writing to a UTF-8 file rather than the Windows console, which can't encode the emoji) and inspected the full rendered text in both languages — confirmed the three new lines render correctly, in order, after `/delhabit`, with `max_days` showing the live default (14). Also ran `PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/test_discoverability.py -q` (83 passed) before running the full suite.

## Maps to acceptance criteria (gap-fix scope)

1. **Bilingual `/help` additions for `/log`, `/routine`, and backfill syntax** → `src/habit_assistant/core/i18n.py` (`help_log_cmd`, `help_routine_cmd`, `help_backfill`) + `src/habit_assistant/core/discoverability.py:build_help_text` (the three appends). Verified by `tests/test_discoverability.py::test_help_v181_mentions_log_command_in_both_languages`, `::test_help_v181_mentions_routine_command_in_both_languages`, `::test_help_v181_mentions_backfill_syntax_in_both_languages`, `::test_help_v181_backfill_max_days_read_live_from_config_not_hardcoded`, `::test_help_v181_new_lines_appear_after_delhabit_and_structure_otherwise_unchanged`.
2. **Announce-machinery tolerance check** → investigated, no code change needed (see finding below).
3. **Tests extended per convention** → 5 new tests added to `tests/test_discoverability.py`, following the file's existing style (real `Config`, both languages, concrete substring checks, no mocks).

## Announce-machinery finding

`core/announce.py:announce_release` (line 65-66):
```python
if version not in RELEASE_NOTES:
    return
```
**It fail-opens/skips silently.** A version with no `RELEASE_NOTES` entry announces nothing at all — no reads, no writes, no exception, no log line beyond the function simply returning immediately. This is explicitly documented as the intended behavior in both `core/announce.py`'s own docstring ("R-N1: `version` with no catalog entry at all announces nothing... returns immediately, no reads, no writes") and `core/release_notes.py`'s module docstring ("a version with no entry here simply announces nothing").

Per the task's instruction ("if it silently skips, do NOT add one — patch releases shouldn't message users"), **I did not add a `RELEASE_NOTES["1.8.1"]` entry.** Users will receive no announcement for this patch, which is correct — it's an invisible `/help` copy fix, not a feature worth interrupting users for.

## Known limitations

None. This is a pure additive `/help`-text change; no command menu, dispatch, or behavior changes were needed (the task explicitly said not to touch the menu — it was already correct).

## Iteration log

None — passed on the first implementation; no failures reported by a tester loop for this small gap-fix.
