# Implementation — v1.5.0 `checkins` module (hourly check-ins, `/checkin`, `/dnd` alias)

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/core/checkins.py` | Created | `effective_checkin` (R-K2, tri-state resolver), `build_checkin_message` (R-K3/R-K6, deterministic bilingual template), `run_due_checkins` (R-K1, the minutely-tick sibling), `execute_checkin` (R-K8, the `/checkin` setter). |
| `src/habit_assistant/core/commands.py` | Modified | Added `"checkin"` to `CommandKind`; `_match_checkin`/`_checkin_tail_has_valid_shape` (slash form permissive, Thai `เช็คอิน` shape-gated); `_match_dnd` (pure alias — produces `Command(kind="quiet", ...)`, the same shape `_match_quiet` produces, for `/dnd`/Thai `งดรบกวน`); both wired into `dispatch()` right after the existing `quiet_command` check. Disjoint keys only — did not touch any other module's kinds/sections. |
| `src/habit_assistant/core/i18n.py` | Modified | New `checkins` catalog block: `checkin_set_on/_off/_window`, `checkin_show`/`checkin_show_off`, `checkin_usage`, `checkin_save_failed`, the check-in message body (`checkin_header`, `checkin_line_progress`, `checkin_line_not_yet`, `checkin_invite`, `checkin_generic_nudge`), and the `/help` copy data (`help_checkin_cmd`, `help_dnd_cmd`). `/dnd` intentionally added **no** new keys — it reuses `quiet_set`/`quiet_cleared`/`quiet_usage`/`quiet_invalid_window`/`preferences_save_failed` verbatim (R-D5: same storage, same reply catalog). |
| `tests/test_checkins.py` | Created | 62 tests — AC-3, AC-4, AC-5, AC-6, AC-7, AC-8, AC-9 (dispatch shape + adversarial corpus, `execute_checkin`, `effective_checkin`, `build_checkin_message`, `run_due_checkins`). |
| `tests/test_dnd.py` | Created | 21 tests — AC-13 (`/dnd` alias shape + adversarial corpus + behavioral byte-identity with `/quiet`, plus a structural check that the `/help` copy exists and is bilingual). |

Nothing else touched. `main.py`, `core/release_notes.py`/`core/announce.py`, `core/preparse.py`, and every other module's own `core/commands.py`/`core/i18n.py` sections are untouched by this pass, per scope.

## How it works

`core/commands.dispatch` recognizes `/checkin`/`เช็คอิน` as a new `"checkin"` kind (shape only — slash form fully permissive, mirroring `/quiet`/`/target`; the Thai alias is whole-message-anchored with a valid-argument-shape tail gate, mirroring `เตือน`/`ย้อนหลัง`'s own hardening) and `/dnd`/`งดรบกวน` as a **pure alias** that produces the identical `Command(kind="quiet", pref_value=...)` shape `_match_quiet` already produces — so `/dnd` needs **zero** new routing or storage; it already flows through the existing `preferences.execute_quiet` once `main.py` routes `"quiet"` (already wired, pre-v1.5).

`core/checkins.py` owns the check-in tri-state: `db.get_checkin_window` returns the raw stored value (`None`/`"off"`/`"HH:MM-HH:MM"`), and `effective_checkin` is the ONE place that's interpreted into `(enabled, window)`, falling open to "inherit the config default" (which ships `enabled=false`, OQ1 resolved (b)) on any DB read error. `execute_checkin` is the `/checkin` setter: `on` stores the **literal** config-default window string (not the token `"on"`) so it stays enabled even if the config default later changes; `off` stores `"off"`; `default` clears to `NULL` and then replies with the resulting effective state (reusing the same "show" builder — SPEC-v1.5.md §3.2 names only four reply ids, so `default` doesn't get a fifth); an explicit `HH:MM-HH:MM` is validated and stored verbatim; anything else is a `checkin_usage` reply with no write. `run_due_checkins` is a sibling of `run_due_reminders`: it no-ops unless the current minute is `:00`, then for each active user checks `effective_checkin` → in-window (`start <= HH:00 <= end`, **both ends inclusive**, per R-K2's own explicit formula) → not in DND (`reminders.in_dnd_now`) → `build_checkin_message` isn't `None` (R-K3's all-goals-met skip), and sends. `build_checkin_message` reads `targets.effective_goal` + `db.sum_value` for every registered habit (the same aggregation every other goal-consuming module already reads through) to build the progress lines, or falls back to a generic nudge when the user has no goal-bearing habits at all.

## Smoke test done

- Manual script (`PYTHONPATH=src PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "..."`, output redirected to a scratch file to dodge the Windows console's cp1252 codec on emoji): built a fresh on-disk `Database`, ran `/checkin on` → `db.get_checkin_window` == `"08:00-20:00"`, confirmed bilingual reply text; ran bare `/checkin` → `checkin_show`; logged 1200ml water and called `build_checkin_message` directly at a fixed clock → got exactly the spec §3.1 example shape (`🌤️ Quick check-in / • water: 1200 / 2500 ml / Log anything you've done? 💬`); logged enough to meet the goal → `build_checkin_message` returned `None` (skip).
- A second script drove `run_due_checkins` end-to-end with a `FakeChannel`: fired at `09:00` for an enabled owner, silent at `09:15` (off-the-hour); set DND via the Thai `/dnd` alias (`งดรบกวน 00:00-23:59`) through `commands.dispatch` → `preferences.execute_quiet` and confirmed the very next `run_due_checkins` tick was suppressed for that user — proving the `/dnd` alias's storage write is read correctly by `in_dnd_now`/`run_due_checkins` end-to-end, not just unit-tested in isolation.
- `PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/test_i18n.py tests/test_i18n_literals.py tests/test_commands.py -q` → **133 passed** (catalog integrity + full command-dispatch regression, confirming the new `"checkin"` kind and `/dnd` alias didn't shadow any existing v0.5–v1.4 command).
- `PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/test_checkins.py -q` → **62 passed**.
- `PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/test_dnd.py -q` → **21 passed**.
- Full suite: `PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/ -q` → **1912 passed, 1 skipped, 0 failed** in 165s. (The other two parallel tracks — `preparse`, `announce` — had already landed in the shared tree by the time this ran, so this run also covers their tests; the baseline handed to this module was 1643/0/1, and this module alone adds 83 new tests (62 + 21) — the total delta beyond that, ~186 tests, is `preparse`/`announce`'s own contribution, not double-counted or conflicting with anything here.)
- Ran directly against the **venv's** `.venv/Scripts/python.exe` with `PYTHONPATH=src` (not `uv run`) throughout, and never touched `data\habits.db` — a live Task-Scheduler-managed instance of this app uses that file and this venv; `uv run`'s implicit sync attempted a package reinstall against the live install and hit a Windows file-lock (`Access is denied` removing an old `.dist-info`), so all commands in this pass avoided `uv run`/`uv sync` entirely once that was discovered.

## Maps to acceptance criteria

- **AC-3** (hourly firing) → `core/checkins.py:run_due_checkins`; `tests/test_checkins.py::test_ac3_*` (fires at 08:00 and at the **inclusive** 20:00 end boundary, silent at 07:00/21:00/off-the-hour, honors a custom window).
- **AC-4** (LLM-free content) → `core/checkins.py:build_checkin_message` (no `llm`/Ollama parameter or import anywhere in the module); `tests/test_checkins.py::test_checkins_module_never_imports_or_calls_an_llm` + `test_run_due_checkins_never_calls_ollama_end_to_end`.
- **AC-5** (all-goals-met skip / generic nudge) → `core/checkins.py:build_checkin_message`; `tests/test_checkins.py::test_build_checkin_message_skips_when_all_goal_bearing_habits_already_met` + `test_build_checkin_message_generic_nudge_for_a_user_with_no_goal_bearing_habits`.
- **AC-6** (DND honored) → `core/checkins.py:run_due_checkins` (calls the shared-surface `reminders.in_dnd_now`); `tests/test_checkins.py::test_ac6_dnd_suppresses_a_due_checkin` + `test_ac6_dnd_is_scoped_to_the_user_in_it`.
- **AC-7** (`/checkin` setter) → `core/commands.py:_match_checkin` + `core/checkins.py:execute_checkin`; `tests/test_checkins.py`'s dispatch-shape + adversarial-corpus + `execute_checkin` sections (on/off/default/window/show/invalid/db-failure, Thai alias).
- **AC-8** (opt-in default, owner included) → `core/checkins.py:effective_checkin` (config default `enabled=False`); `tests/test_checkins.py::test_effective_checkin_disabled_by_default_for_everyone_including_owner` + `test_ac8_disabled_by_default_no_one_gets_a_checkin_owner_included`.
- **AC-9** (isolation) → every DB read in `core/checkins.py` is scoped to `user_id`; `tests/test_checkins.py::test_build_checkin_message_isolated_per_user` + `test_ac9_isolation_a_disabled_off_hour_or_dnd_user_never_leaks_into_an_enabled_users_send`.
- **AC-13** (`/dnd` alias + `/help` mention) → **partially covered here, see Known limitations**. The alias half is fully implemented and tested: `core/commands.py:_match_dnd`; `tests/test_dnd.py`'s dispatch-shape + adversarial-corpus + behavioral-byte-identity sections. The `/help`-mentions-DND-and-check-ins half: the catalog copy (`help_checkin_cmd`, `help_dnd_cmd`) is landed and tested for existence/bilingualism (`tests/test_dnd.py::test_help_checkin_and_dnd_copy_exists_and_is_bilingual`), but **wiring those two lines into `core/discoverability.py:build_help_text`'s actual output is NOT done by this module** — see Known limitations below for why and the exact one-line fix.

## Known limitations

1. **`/help`'s actual output doesn't yet include the check-in/DND lines.** SPEC-v1.5.md §6/§11 lists this module's owned files as `core/checkins.py`, `core/commands.py` (checkin+dnd kinds only), `core/i18n.py` (checkin/dnd/help **keys**), and the two test files — `core/discoverability.py` is not among them. This mirrors the *exact* precedent already on file in this codebase: `core/discoverability.py:build_help_text`'s own comment block notes that `help_lang`/`help_quiet_cmd`/`help_remind_cmd` "were added after this module itself landed" as a later append by integration (IMPL-v1.2-preferences.md's own documented "Known limitations" #3). **Exact fix for integration** (append after the existing `lines.append(i18n.t("help_remind_cmd", lang))` line in `build_help_text`):
   ```python
   lines.append(i18n.t("help_checkin_cmd", lang))
   lines.append(i18n.t("help_dnd_cmd", lang))
   ```
2. **No audit-log entry for `/checkin` writes.** `core/audit.py`'s `Action` is a closed `Literal`, and `core/audit.py` is not listed among this module's (or any module's) files to touch in SPEC-v1.5.md §6. Rather than silently extending a shared closed enum out of scope, `execute_checkin` does not call `audit.record` at all — `/checkin` changes are invisible to `/audit`. If audit coverage is wanted, that's a spec/`core/audit.py` change (new `"checkin_set"`/`"checkin_off"` actions) — flag to Archi/Sophia rather than have me extend a shared enum unreviewed.
3. **A `/checkin` window with `start > end` (a hypothetical "crossing midnight" window) would silently never fire.** R-K2's own firing formula is the simple, explicit `start <= HH:00 <= end` (no midnight-wraparound handling, unlike `/quiet`'s `_in_quiet_hours`). `execute_checkin`'s validation (mirroring `/quiet`'s own permissiveness, including its own documented "start == end is accepted, just never fires" precedent in `tests/test_preferences.py`) only checks HH:MM *shape*, not ordering — so a user could set e.g. `/checkin 22:00-06:00` and it would validate and store, but never actually fire (every hour fails `"22:00" <= HH:00 <= "06:00"` as a plain string comparison). No AC requires check-in windows to cross midnight, and R-K2 gives no wraparound formula, so this is left as specified rather than invented. Not observed in any test — the tested windows are all same-day.
4. **A bare Thai `เช็คอิน`, with no tail, dispatches** (as "show", per R-K8's own "empty = show" grammar) even though `เช็คอิน` is a common transliterated loanword that could plausibly appear as a complete, unrelated message (a location check-in note). This mirrors the established precedent already in this codebase for `ย้อนหลัง`'s own bare-match behavior (`_build_history_th_pattern`) — a grammar that defines a meaning for the empty tail gets a bare match; one that doesn't (`เตือน`, which always needs a habit token) doesn't. Documented as a deliberate, precedent-following call, not an oversight; every *non-bare* continuation is still gated to a valid argument shape (on/off/default/window), closing the higher-volume false-positive class.
5. **`main.py` integration is not done by this pass** (explicitly out of scope per dispatch) — see the wiring instructions below for the exact calls integration needs to make.

## Wiring instructions for `main.py` (integration step — not applied by this pass)

1. **Import**: add `checkins` to the existing `from habit_assistant.core import (...)` block (alphabetically, between `audit_view` and `commands`).

2. **Minutely tick**: register `run_due_checkins` as its own job on the **same** `CronTrigger(second=0, timezone=config.app.timezone)` cadence the reminder tick already uses (its own internal `hhmm.endswith(":00")` guard is what limits it to firing once per hour — the cron trigger itself still needs to fire every minute so that guard gets evaluated):
   ```python
   scheduler.add_job(
       checkins.run_due_checkins,
       trigger=CronTrigger(second=0, timezone=config.app.timezone),
       args=[channel, config, registry, db],
       id="checkin_tick",
       replace_existing=True,
       coalesce=True,
       max_instances=1,
       misfire_grace_time=30,
   )
   ```
   Placed right after the existing `reminder_tick` `scheduler.add_job(...)` call.

3. **Command routing** in `handle_inbound_message`, alongside the existing `"quiet"`/`"lang"` branches:
   ```python
   if command.kind == "checkin":
       reply = await checkins.execute_checkin(command, db=db, config=config, lang=lang, user_id=user_id)
       if dry_run:
           print(reply)
           return
       assert channel is not None, "channel is required outside dry-run"
       await channel.send(user_id, reply)
       return
   ```
   `/dnd` needs **no** new branch — it already dispatches as `kind="quiet"`, already routed.

4. **Command menu**: add a `CHECKIN_COMMAND_DESCRIPTIONS` dict (mirroring `QUIET_COMMAND_DESCRIPTIONS` right above it):
   ```python
   CHECKIN_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
       "en": "Get hourly check-in nudges (off by default)",
       "th": "เปิดแจ้งเตือนเช็คอินรายชั่วโมง (ปิดโดยค่าเริ่มต้น)",
   }
   ```
   and add `+ [("checkin", CHECKIN_COMMAND_DESCRIPTIONS[lang])]` to the `command_menu` dict-comprehension's list (e.g. right after the `("quiet", ...)` entry). `/dnd` shares `/quiet`'s existing menu entry (SPEC-v1.5.md §6) — no separate menu line.

5. **`/help` text** — see Known limitations #1 above for the exact two-line append to `core/discoverability.py:build_help_text`.

## Iteration log

No Vera round yet — first hand-off for this module.
