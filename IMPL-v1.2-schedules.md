# Implementation — v1.2.0 Multi-user support (module `schedules`)

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/core/schedules.py` | Created | `execute_remind` — validates and performs a `/remind` set/show/default/off op against the shared `user_reminder_times` store, returns a bilingual reply |
| `src/habit_assistant/core/commands.py` | Modified | Adds the `"remind"` kind: `_match_remind` (slash form `/remind <habit> …` + Thai alias `เตือน <habit> …`), wired into `dispatch()` right after `target` |
| `src/habit_assistant/core/i18n.py` | Modified | Adds 12 keys under the pre-reserved "Module `schedules`" section: `remind_set`, `remind_off`, `remind_cleared`, `remind_show`, `remind_show_off`, `remind_source_custom`, `remind_source_default`, `remind_no_default_times`, `remind_invalid_time`, `remind_too_many_times`, `remind_invalid_habit`, `remind_save_failed` |
| `tests/test_schedules.py` | Created | 56 tests: dispatch-shape, adversarial no-false-positive corpus, AC-S2/AC-S3/AC-S5 coverage, DB-failure handling, and an AC-S4 proof against the real minutely tick |

I touched only `core/commands.py` and `core/i18n.py`'s pre-reserved `schedules` sections (disjoint from the `access`/`preferences` modules' own sections, which landed concurrently in the same files during this session — verified green together, see "Smoke test done"). **`main.py` was not touched**, per my scope — see "Known limitations" for the exact integration-wiring instructions.

## How it works

`core/commands.dispatch` recognizes two trigger shapes for `/remind`: the slash form `/remind <habit> [<HH:MM>...|off|default|reset|clear]` (habit token may be unresolved — carried through raw, mirroring `/target`'s AC16 pattern) and the Thai alias `เตือน <habit> ...` (a literal keyword substitute for the trigger word itself, requiring a mandatory space after it — the same false-positive mitigation the concurrently-landed `preferences` module uses for `ภาษา`/`เงียบ`). Neither branch validates the tail — `Command.times` carries `[]` (show), `["off"]`, `["default"]`, or the raw whitespace-split token list (set), exactly as SPEC-v1.2.md §5 describes. `core/schedules.execute_remind` is where the real work happens: it resolves the habit via the registry (unknown → `remind_invalid_habit`), then for "set" validates every token against `config._HHMM_RE`, rejects the whole set on the first invalid token (no partial write) via `remind_invalid_time`, de-dupes, and enforces the ≤24 cap (`remind_too_many_times`) before writing through `db.set_reminder_times` (delete-then-insert, shared surface). "off"/"default" write `["off"]`/call `db.clear_reminder_times`; "show" reads `db.get_reminder_times` directly to determine the source label (custom/default/off) and calls the shared `core/reminders.effective_reminder_times` for the actual times shown — the same resolver the minutely tick consults, so a `/remind` write is guaranteed to be reflected on the tick's very next run with no scheduler rebuild (AC-S4/R-S6). Every DB write is wrapped in a fail-closed try/except reporting `remind_save_failed`, never a traceback.

## Smoke test done

Ran my own new file in isolation first, then the surrounding shared-surface files it touches, then the full suite (all via `.venv\Scripts\python.exe -m pytest`, always against `tmp_path`-only SQLite — `data/habits.db` and the live Task Scheduler service were never touched):

```
pytest tests/test_schedules.py -q
  56 passed in 1.47s

pytest tests/test_commands.py tests/test_i18n.py tests/test_i18n_literals.py tests/test_targets.py tests/test_reminders.py -q
  208 passed in 14.56s

pytest -q   (full suite)
  1114 passed, 1 skipped, 30 warnings in 99.84s
```

The full-suite count (1114+1, up from the 976+1 baseline stated in my dispatch) reflects that the `access` and `preferences` parallel tracks landed concurrently in the same working tree during this session, touching the same shared files (`core/commands.py`, `core/i18n.py`) in their own disjoint sections — I re-verified after each of my own edits that their content was untouched and the combined file still parses/tests clean. No regression, no skip-count change, no `data/habits.db` involvement anywhere.

## Maps to acceptance criteria

- **AC-S2** (per-user set: `/remind water 08:00 12:00` fires at 08:00/12:00 and not the old config times; B unaffected; other habits unaffected) → `core/commands.py:_match_remind` (shape) + `core/schedules.py:_execute_set`/`execute_remind` (write) + `core/reminders.py:effective_reminder_times`/`run_due_reminders` (read side, shared surface, unmodified by me) → `tests/test_schedules.py::test_ac_s2_*` (4 tests, including two that exercise the real `run_due_reminders` tick).
- **AC-S3** (show reports effective times + source custom/default/off; default reverts to config; off suppresses only that user) → `core/schedules.py:_execute_show`/`_execute_default`/`_execute_off` → `tests/test_schedules.py::test_ac_s3_*` (6 tests, including the isolation check that B still gets reminders after A turns theirs off).
- **AC-S5** (validation: non-HH:MM token rejected with `remind_invalid_time`, no write; dedupe; ≤24 cap) → `core/schedules.py:_validate_and_dedupe_times`/`_execute_set` → `tests/test_schedules.py::test_ac_s5_*` (7 parametrized/direct tests: 7 invalid-token shapes, partial-set-rejects-whole-set, dedupe, cap boundary at 24, cap exceeded at 25).

Not owned by this module but exercised here per my dispatch instructions: **AC-S4** (no restart, no scheduler rebuild) is formally owned by the shared-surface/integration pass (SPEC-v1.2.md §11), but since the write path is entirely this module's own code, I wrote `test_ac_s4_remind_write_is_picked_up_by_the_next_tick_with_no_scheduler_rebuild` — it calls the real `core/reminders.run_due_reminders` twice around a real `/remind` write (through `commands.dispatch` + `execute_remind`, no mocks), with no scheduler/job object constructed anywhere in the test, proving there is nothing to rebuild.

## Known limitations

- **`main.py` is not wired.** Per my dispatch scope ("Do NOT touch `main.py` — routing lands at integration"), `handle_inbound_message` does not yet route `command.kind == "remind"` anywhere — a live `/remind` message today falls through main.py's existing `if dry_run: print(...)` / normal dispatch chain unhandled (it would hit none of the existing `if command.kind == ...` branches and fall through to the bottom, past the LLM-classification gate, into `parse_message` — i.e. currently a no-op from a user's perspective until integration wires it). Exact integration steps for Archi/the integration pass:
  1. **Import**: add `schedules` to `main.py`'s core import line: `from habit_assistant.core import commands, discoverability, i18n, query, schedules, streaks, target_nl, targets, targets_command, undo_ui`.
  2. **Dispatch branch**: in `handle_inbound_message`, add a branch for `command.kind == "remind"` immediately after the existing `if command.kind == "target": ...` block (same shape, mirrors it exactly):
     ```python
     if command.kind == "remind":
         reply = await schedules.execute_remind(
             command, db=db, config=config, registry=registry, lang=lang, user_id=user_id
         )
         if dry_run:
             print(reply)
             return
         assert channel is not None, "channel is required outside dry-run"
         await channel.send(user_id, reply)
         return
     ```
  3. **Command menu**: add a `REMIND_COMMAND_DESCRIPTIONS` dict near `TARGET_COMMAND_DESCRIPTIONS` (same shape):
     ```python
     REMIND_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
         "en": "View or set your reminder times for a habit",
         "th": "ดูหรือตั้งเวลาแจ้งเตือนของกิจกรรม",
     }
     ```
     and fold `("remind", REMIND_COMMAND_DESCRIPTIONS[lang])` into the `command_menu` dict built at startup (currently `undo_command_menu[lang] + [("target", desc)] + DISCOVERABILITY_COMMAND_DESCRIPTIONS[lang]`) — note the `access`/`preferences` modules will each want their own menu entries too, so integration needs to merge all of these together, not just mine.
  4. Nothing else — `execute_remind`'s signature already matches the calling convention every other command handler in `main.py` uses (`db=`, `config=`, `registry=`, `lang=`, `user_id=`).
- **`user_id` is a required kwarg on `execute_remind` not listed verbatim in SPEC-v1.2.md §5's interface line** (`async def execute_remind(command, *, db, config, registry, lang) -> str`). I added it because the function cannot scope any DB read/write without it, and it's exactly the same deviation the already-landed `core/targets_command.execute_target` made from its own §5-adjacent listing — I treated §5 as representative/illustrative here, consistent with that precedent, rather than escalating a spec gap that the shared surface had already implicitly resolved the same way.
- **Full NL phrasing for `/remind` is deliberately not built** (§10, explicitly out of scope) — only the deterministic `/remind`/`เตือน` command surface exists.
- **The Thai alias `เตือน` accepts an unresolved habit token through to a friendly `remind_invalid_habit` reply**, same as the slash form and same as `/target`'s AC16 precedent, rather than silently falling through when the "habit" word doesn't resolve — this was a deliberate design choice made for consistency with the `access`/`preferences` modules' own Thai-alias convention (both landed concurrently using "mandatory space after the trigger word, unrestricted/unvalidated tail" rather than a registry-anchored alternation), not an oversight. The mandatory `\s+` after `เตือน` (matching the slash form's own required space) is what prevents the normal no-space Thai spelling of "เตือน" immediately followed by more words (e.g. "เตือนตัวเอง") from ever matching at all — verified in the adversarial corpus.
- **`/remind`'s ordering in `dispatch()`** is placed right after `target`, before the (concurrently-landed) `access`/`lang`/`quiet` blocks and before `help`/`habits`/`query`. Since every v1.2 command's trigger text is disjoint from every other, exact placement doesn't change behavior — documented in `dispatch()`'s own docstring.

## Iteration log

No Vera round yet — this is the initial hand-off.

## Post-landing audit: Thai-alias false-positive class (fixed)

**Trigger:** after Vera's PASS on this module, the coordinator flagged that sibling module `preferences`'s own Vera pass caught a false-positive class on the Thai aliases `ภาษา`/`เงียบ`: correctly-spelled Thai puts a space before particles like the mai-yamok "ๆ" and other trailing words, so a bare "mandatory space, then anything" gate misfires on ordinary sentences (e.g. "เงียบ ๆ หน่อยนะ"). Asked to audit `_match_remind`'s Thai path (`เตือน`) for the same class.

**Finding: it had the same bug.** `_match_remind` originally used exactly that gate (`^(?:/remind|เตือน)\s+(?P<rest>\S.*)$`, then split `rest` into a habit token + tail with no further restriction). Empirically confirmed all 5 of the coordinator's example shapes misfired before the fix:

| Input | Before fix |
|---|---|
| `เตือน ๆ หน่อยนะ` | `Command(kind='remind', category='ๆ', times=['หน่อยนะ'])` |
| `เตือน ฉันด้วยนะ` | `Command(kind='remind', category='ฉันด้วยนะ', times=[])` |
| `เตือน แล้วนะ` | `Command(kind='remind', category='แล้วนะ', times=[])` |
| `เตือน น้ำ ท่วมด้วย` | `Command(kind='remind', category='water', times=['ท่วมด้วย'])` |
| `เตือน ลืมไปแล้วว่าต้องทำอะไร` | `Command(kind='remind', category='ลืมไปแล้วว่าต้องทำอะไร', times=[])` |

**Fix** (`src/habit_assistant/core/commands.py`, `_match_remind` + its supporting helpers — my section only, `access`/`preferences` untouched): split the Thai path from the slash path and added two restrictions to the Thai path only:
1. `_build_remind_th_pattern` — the habit token must resolve to a REAL configured habit (id or Thai label) via a registry-built alternation, mirroring `_build_target_th_set_pattern`'s own existing precedent (`/target`'s Thai trigger already worked this way). Kills cases 1/2/3/5 above (none name a real habit).
2. `_remind_tail_has_valid_shape` — even when the habit token IS real, any trailing tail must itself look like a valid remind argument: a clear/off word, or a whitespace-separated list of digits-and-colon-shaped tokens (not yet validated as REAL `HH:MM` — that stays `execute_remind`'s job, R-S5, so a shape-like-but-invalid time like "เตือน น้ำ 25:99" still dispatches and still reaches `remind_invalid_time`). Kills case 4 above ("ท่วมด้วย" doesn't have that shape).

The **slash form (`/remind ...`) was left untouched** — it was never at risk (nobody types "/remind" by accident in prose), and its permissive "unresolved habit token still produces a Command" behavior is intentional (mirrors `/target`'s AC16 pattern, gives a friendly `remind_invalid_habit` reply instead of silent drop).

**Verification:** all 8 previously-legitimate Thai/slash cases (`เตือน น้ำ 08:00 12:00`, `เตือน น้ำ off`, `เตือน น้ำ default`, `เตือน น้ำ` (bare show), `เตือน น้ำ 25:99`, plus three slash-form cases) still dispatch correctly — no regression. Added `THAI_ALIAS_FALSE_POSITIVE_CASES` (the 5 confirmed-misfiring shapes, parametrized) plus two explicit regression tests (`test_thai_habit_label_bare_form_is_still_show`, `test_thai_habit_label_with_shape_like_but_semantically_invalid_time_still_dispatches`) to `tests/test_schedules.py`; the 5 false-positive cases are also folded into `ADVERSARIAL_MESSAGES`. Full suite after the fix: **1271 passed, 4 failed, 1 skipped** — the 4 failures are `preferences`' own pre-existing cluster (`tests/test_preferences.py`, being fixed in parallel by that module's Luna), not mine; my own file (`tests/test_schedules.py`) is 106/106 green.

**Status: fixed, not "protected as-is"** — the coordinator's premise ("your regex requires a live-registry habit token … which may already protect you") did not hold for the code as it stood; it now does.
