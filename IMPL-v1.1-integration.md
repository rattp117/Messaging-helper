# Implementation — v1.1.0 integration (undo menu + per-habit targets)

Scope: **final integration step**, per SPEC-v1.1.md §11. Both parallel modules
(`undo-ui`: `IMPL-v1.1-undo-ui.md`; `targets`: `IMPL-v1.1-targets.md`) reported
done and Vera-verified PASS at module level. This pass wires their public
functions into `main.py` — the seams the shared-surface pass (`IMPL-v1.1-
shared.md`) deliberately left undone — and fixes a stale, pre-existing
`VERSION`-pin test as requested.

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/main.py` | Modified | All 5 integration seams (see "How it works"); `_execute_undo` now delegates to `undo_ui.send_undo_confirmation` and `main.py`'s own `_describe_log` is deleted (R-U8); `_send_recovered_generic` gained a `buttons` param |
| `src/habit_assistant/__init__.py` | Modified | `__version__` bumped `1.0.0` → `1.0.1`, matching `VERSION`/`pyproject.toml` (was silently missed at the v1.0.1 release — the old hardcoded-literal test never caught it because it happened to pin the same stale value) |
| `tests/test_charts.py` | Modified | `test_version_is_consistent_...` rewritten to read `VERSION` dynamically and cross-check `pyproject.toml`/`__init__.py` against it, instead of a hardcoded literal that goes stale at every release; `_FakeTelegramChannel` gained `send_actionable`/`set_my_commands` + widened `run(..., on_callback=None)` |
| `tests/test_reminders.py` | Modified | `_FakeTelegramChannel` gained `send_actionable`/`set_my_commands` + widened `run` |
| `tests/test_streaks.py` | Modified | `_FakeTelegramChannel` gained `send_actionable`/`set_my_commands` + widened `run` |
| `tests/test_fallback.py` | Modified | `_RecordingChannel` (a duck-typed, non-`Channel`-subclassing double) gained `send_actionable` |
| `tests/test_resilience.py` | Modified | `_RecordingChannel` gained `send_actionable` |

No production module outside `main.py` was touched — `core/undo_ui.py`,
`core/targets_command.py`, `core/target_nl.py`, `core/commands.py`, and
`core/i18n.py` are exactly as the two module passes and the shared-surface
pass left them.

## How it works

Five wiring seams landed in `main.py`, per the two modules' own "wiring for
the integrator" sections:

1. **Startup command menu** (R-U1/AC1-AC2): `async_main` merges
   `undo_ui.command_menu_entries()` (`/undo`) with a small local
   `TARGET_COMMAND_DESCRIPTIONS` dict (`/target`) — the `targets` module's
   `IMPL-v1.1-targets.md` never actually built a `command_menu_entries()`
   of its own (the `undo-ui` doc's wiring snippet assumed one that doesn't
   exist), so this integration step supplies `/target`'s menu copy directly,
   mirroring `undo_ui`'s own "no i18n catalog key for Bot API menu copy"
   rationale — and calls `channel.set_my_commands(...)` once, wrapped in the
   same belt-and-suspenders `try/except` as the existing schema probe.
2. **Undo button on every interactive confirmation** (R-U2/R-U3/AC3/AC5):
   `db.insert_log(entry)`'s return value is now captured (`row_id`), and
   every confirmation send in `handle_inbound_message` (water, stretch,
   diary, the generic-habit branch) and `reparse_pending_unparsed`'s
   recovery re-confirmations now go out via `channel.send_actionable(text,
   undo_ui.undo_button(row_id, lang))` instead of plain `send`. Unprompted
   sends (reminders, daily summary, weekly review, health alerts, the
   clarifying question, the deferred-ack) are untouched — still plain
   `send`, no button.
3. **`on_callback` routing** (R-U4/AC6): `async_main` wires
   `on_callback=undo_ui.handle_undo_callback` (via a small closure binding
   `db`/`channel`/`config`/`registry`) into `channel.run(...)` — the
   shared-surface `TelegramChannel.run` already calls
   `answer_callback_query` itself right after, unconditionally.
4. **`"target"` `CommandKind` routing** (R-T10): `handle_inbound_message`'s
   command-dispatch block gained a `command.kind == "target"` branch,
   calling `targets_command.execute_target` and sending the reply with
   plain `send` (a target reply is not a log confirmation — no button).
5. **Full-NL target step** (R-T12-R-T16, OQ3): inserted between the
   health-monitor deferral check and `parse_message`, exactly as
   `IMPL-v1.1-targets.md` specified — reachable only when
   `health_monitor is None or health_monitor.ollama_up` (R-T16: zero LLM
   calls while Ollama is down), gated by the cheap
   `target_nl.looks_like_target_phrasing` check, then
   `target_nl.classify_target_intent` (fail-closed). A hit builds a
   `Command(kind="target", target_action="set", ...)` from the validated
   `TargetIntent` and runs it through the *same* `execute_target` "set"
   path anchored `/target` uses, then returns immediately — no `logs` row
   is written (AC29/AC30). A miss (gate miss, low confidence, any
   classifier failure) falls straight through to `parse_message`
   unchanged, exactly as before (R-C5). `command` is reused implicitly
   (never re-`dispatch`ed) since every branch above it returns, so
   reaching this point already proves `command is None`.

Additionally, R-U8's "one implementation" guarantee is now literally true,
not just byte-identical-by-construction: `_execute_undo` delegates to
`undo_ui.send_undo_confirmation`, and `main.py`'s own `_describe_log` is
deleted (superseded by `undo_ui.describe_log`).

## Smoke test done

1. Full suite: `.venv\Scripts\python.exe -m pytest -q` → **863 passed, 6
   failed, 1 skipped**. The 6 failures are exactly the date-drift flakes
   already documented in `IMPL-v1.1-shared.md`/the two module IMPL docs
   (hardcoded past seed dates in `test_adaptive_reminders.py`/
   `test_v09_gaps.py` vs. the real "today"); the stale `VERSION`-pin
   failure is now fixed (item 6, see below) — matches the coordinator's
   "expect only the 6 date-drift flakes failing" exactly.
2. **First integration run surfaced 18 failures** (11 new beyond the
   documented 7) — root cause: six duck-typed test-double "channel" classes
   across `test_charts.py`, `test_reminders.py`, `test_streaks.py`,
   `test_fallback.py`, `test_resilience.py` do **not** subclass the
   `Channel` ABC (they predate it or were hand-rolled), so they never
   inherited the shared-surface's `send_actionable`/`set_my_commands`
   concrete defaults and broke the moment `main.py` started calling them.
   Fixed by adding the missing methods directly to each fake (mirroring the
   ABC's own degrade-to-`send` behavior), not by making them inherit
   `Channel` (would have been a bigger, riskier diff for no behavioral
   gain). One follow-on regression (`test_charts.py`'s
   `_FakeTelegramChannel.calls[0] == "send"` assertion, which broke once
   `set_my_commands` started appending to the same `calls` list `run()`'s
   docstring documents as a send/send_image-only ordering contract) fixed
   by tracking `set_my_commands` calls on a separate attribute instead.
3. A standalone integration smoke script (not committed, deleted after
   use), fully offline against a `tmp`-only SQLite file with a `FakeChannel`
   and mocked `parse_message`/`classify_target_intent` — never
   `data/habits.db` — walked the whole button lifecycle: a normal log gets
   exactly one undo button (`callback_data="undo:<row id>"`); `/target
   water 2000` replies with no button and stores the override; the very
   next log confirmation reflects the new goal (2000, not 2500); tapping
   the button via `undo_ui.handle_undo_callback` soft-deletes and confirms
   with the override-aware recomputed total; a re-tap gets the friendly
   `already_undone` reply; and the text `/undo` command path produces the
   same shape via the same delegated formatter. Also verified: an
   Ollama-up NL target phrase sets the target with zero `logs` rows
   written; an Ollama-down health monitor makes zero `classify_target_
   intent` calls and falls through to the existing deferred-ack path
   (AC33). All passed.
4. **Live smoke against the real Ollama instance** (`http://mac-mini:11434`,
   explicitly permitted, tmp DB only, never `data/habits.db`, no config/
   service files touched): "from now on I want to drink 2.5 liters of
   water a day" → classified correctly, `water` target set to 2500 ml,
   zero `logs` rows written; immediately followed by a plain "500ml" →
   correctly logged as a normal entry (not swallowed as a target change),
   confirmation carries an undo button. Confirms the full-NL wiring works
   against the real model, not just a mocked one.
5. Verified the merged startup command-menu dict shape directly
   (`{"en": [("undo", "Undo your most recent log"), ("target", "View or
   set a habit's daily goal")], "th": [...]}`) — matches
   `TelegramChannel.build_set_my_commands_requests`'s expected input shape
   (already covered by `tests/test_channels.py`'s existing request-builder
   tests from the shared-surface pass).
6. Never ran the app, `--seed`, `--dry-run`, or any test against
   `data/habits.db`; the live Task Scheduler service was not stopped,
   started, or otherwise touched.

## Maps to acceptance criteria

Newly verifiable end-to-end at this integration step (previously "building
block only" per the two module IMPL docs):

- **AC1** (startup `set_my_commands` with `/undo` + `/target`, en+th) →
  `async_main`'s merged `command_menu` + smoke test #5. No dedicated pytest
  added (the merge logic is trivial dict composition over two already-tested
  building blocks: `undo_ui.command_menu_entries()`, shared-surface's
  `TelegramChannel.build_set_my_commands_requests`); flagging for
  integration Vera to add an end-to-end `async_main` assertion if a formal
  regression guard is wanted beyond this report's smoke evidence.
- **AC2** (a `set_my_commands` transport error at startup never crashes) →
  the `try/except` around the new startup call, same posture as the
  pre-existing schema-probe `try/except` immediately above it.
- **AC3** (a log confirmation carries exactly one undo button with the
  right `callback_data`) → `handle_inbound_message`'s `row_id`/
  `undo_buttons` capture + smoke test #3 step 1.
- **AC4** (unprompted sends carry no button) → unchanged call sites
  (reminders/daily summary/weekly review/health alerts/clarifying
  question/deferred-ack all still call plain `send`) + smoke test #3
  step 2 (deferred-ack has no button).
- **AC5** (milestone suffix + button on the same message) → the milestone
  suffix is concatenated into the same string passed to `send_actionable`,
  not a second send.
- **AC6** (`callback_query` routes to `on_callback`, `_offset` advances,
  callback always answered) → `async_main`'s `on_callback` closure +
  shared-surface `TelegramChannel.run` (already tested in
  `tests/test_channels.py`).
- **AC29/AC30** (full-NL target set, no `logs` row) → smoke tests #4 step 1
  (mocked) and the live-Ollama smoke (real model) both confirm zero rows
  written.
- **AC33** (Ollama-down: no NL classification call, deferred as normal,
  `/target` still works) → smoke test #4 step 2 (mocked classifier call
  count assertion) confirms zero calls while down; the deterministic
  `/target` path is unconditional (checked before the health-monitor
  branch entirely) and unaffected.

Everything else (AC7-AC28, AC31, AC32, AC34) was already verified at the
module level by the two modules' own Vera passes and is unaffected by this
wiring (their functions' internals are untouched).

## Known limitations / deviations

1. **`targets_command.command_menu_entries()` does not exist** —
   `IMPL-v1.1-undo-ui.md`'s wiring snippet referenced it, but
   `IMPL-v1.1-targets.md` never built one (confirmed: no such function in
   `core/targets_command.py`, and the targets module's own IMPL doc never
   mentions it). Resolved by adding `TARGET_COMMAND_DESCRIPTIONS` directly
   in `main.py`, mirroring `undo_ui.UNDO_COMMAND_DESCRIPTIONS`'s own
   "no i18n catalog key for Bot API menu copy" rationale. Not a scope
   deviation from either module — this integration step owns exactly this
   kind of glue.
2. **Six pre-existing test-double "channel" classes needed fixing** (see
   "Smoke test done" #2) — these are test-only files, not part of either
   module's or the shared surface's declared file ownership, but breaking
   silently at integration is exactly the kind of cross-cutting fix this
   step exists to catch and repair.
3. **AC1's exact `async_main`-level assertion is not (yet) formalized in a
   dedicated test** — the shape is proven correct by direct inspection
   (smoke test #5) plus the already-tested building blocks it composes, but
   no test drives `async_main` itself through `set_my_commands` and asserts
   both `/undo` and `/target` arrived with both language sets. Flagging for
   the integration Vera pass rather than writing it here, since Luna's
   playbook role is implementation + smoke-testing, not authoring the
   acceptance-test suite.
4. The 6 remaining test failures (date-drift, hardcoded `2026-08-19` seed
   dates in `test_adaptive_reminders.py`/`test_v09_gaps.py`) were
   deliberately left alone per the coordinator's explicit instruction
   ("integration Vera will handle those").
