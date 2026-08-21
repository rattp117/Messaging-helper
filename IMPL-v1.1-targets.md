# Implementation — v1.1.0 `targets` module (per-habit target set/show/clear + full-NL target-setting)

Scope: **the `targets` module only**, per SPEC-v1.1.md §11's parallel split. Built against the
shared surface (`core/targets.py`, `storage/db.py`'s target accessors, migration 005, channel
plumbing, i18n catalog keys) already landed by IMPL-v1.1-shared.md — read, not modified. The
sibling `undo-ui` module (`core/undo_ui.py`) and `main.py`'s remaining integration wiring are
**not** part of this pass; wiring instructions for the next (integration) step are at the bottom
of this document.

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/core/commands.py` | Modified | Added the `"target"` `CommandKind` + `Command.target_action` field; `dispatch` now recognizes the slash form `/target [<habit> [<value>\|default]]` and anchored bilingual NL "set" triggers ("set/change `<habit>` goal/target to `<value>`", Thai "ตั้งเป้า/เป้า `<habit>` `<value>`"), checked after snooze and before query (R-T7). |
| `src/habit_assistant/core/targets_command.py` | Created | `execute_target(command, *, db, config, registry, lang) -> str` — validates the `Command` against the live registry/DB and performs the `set`/`clear` write, returning the bilingual reply (R-T10). |
| `src/habit_assistant/core/target_nl.py` | Created | Full-NL target-setting classifier: `TargetIntent`, `looks_like_target_phrasing` (cost gate, R-T13.1), `build_target_intent_schema`, `classify_target_intent` (fail-closed, mirrors `core/query.py`, R-T13-R-T16). |
| `src/habit_assistant/llm/prompts.py` | Modified | Added `build_target_intent_system_prompt(registry)` / `build_target_intent_user_prompt(text)` — the only module touching `prompts.py` this release, per SPEC-v1.1.md §6. |
| `tests/test_targets.py` | Created | `commands.dispatch`'s target-shape parsing + `execute_target` end to end, against a real on-disk SQLite `Database` (AC13–AC20, AC27, AC28, plus R-T9's unit-mismatch usage-error case and the EN/TH NL "set" triggers). |
| `tests/test_target_nl.py` | Created | `looks_like_target_phrasing` + `classify_target_intent` against a real `OllamaClient` over `httpx.MockTransport` (AC29, AC30, the NL-setting half of AC31, AC32, AC34), plus prompt-builder sanity checks. |

## How it works

`commands.dispatch` now recognizes a target command's *shape* only — it never validates whether
the named habit actually exists or is goal-able. A recognized habit token resolves to its real id
(via a lookup built from every configured habit's id + English + Thai label, so `/target water
2000`, `/target น้ำ 2000`, and `ตั้งเป้าน้ำ 2000` all resolve to the same `water` id); an
*unrecognized* token is carried through verbatim (lowercased) as `Command.category`, so
`execute_target`'s own `registry.get(...)` lookup is what reports `target_invalid_habit` — there is
exactly one place that decision is made. A value tail parses as `NUMBER [+ UNIT]` reusing the same
`_build_unit_lookup`/`_resolve_unit` machinery `_parse_edit_value` already uses for `/edit`; an
explicit unit that resolves to a *different* habit than the one named is a usage error (R-T9), and
any tail that doesn't parse at all becomes `Command(kind="target", target_action="usage")` rather
than a `None` fall-through (R-T7's explicit carve-out — unlike edit, a clearly-attempted-but-garbled
target command must not be silently misfiled as a log attempt). `execute_target` then does the
actual registry/goal-ability validation and the DB write (`db.set_target`/`db.clear_target`,
wrapped in `try/except` so a write failure becomes `target_save_failed`, never a traceback), reading
the current/previous goal through the shared `core/targets.effective_goal`/`config_goal` — so a
`/target` write takes effect everywhere else (reminders, streaks, summaries, confirmations) with no
further work on this module's part, since those consumers already read through `effective_goal`.

`core/target_nl.py` mirrors `core/query.py`'s shape almost line for line: `classify_target_intent`
calls `OllamaClient.chat_json` with a target-specific schema/prompt and validates the result
strictly (`_validate_intent`) — habit must be a real, goal-able configured id, `goal` must coerce to
a genuine number `> 0`, and `confidence` must meet `config.ollama.confidence_threshold` (default
0.55, same knob `core/parser.py` uses) — returning `None` on absolutely any failure (never raises).
`looks_like_target_phrasing` is a separate, cheap substring/word-boundary gate — a pure cost
optimization the *caller* (`main.py`, not implemented in this pass) is expected to check before
spending an LLM call; it carries no safety weight of its own, which is why `classify_target_intent`
is independently fail-closed and fully testable without it.

## Smoke test done

1. Interactive REPL smoke (not committed) exercising the full deterministic path end to end against
   a real temp-file `Database` (never `data/habits.db`): `/target water 2000` → `habit_targets` gets
   `(water, 2000)`, reply names the previous goal (2500); `/target water 3 bottles` → 1800; `/target
   water 0` → `target_invalid_value`; `/target coffee 2000` → `target_invalid_habit` listing `water,
   stretch, diary`; `/target diary 5` → `target_not_goalable`; `/target water default` → reverts to
   2500; `/target water` → shows 2500; `/target` (no args) → the full per-habit listing. All matched
   the exact SPEC-v1.1.md §3.4 example text.
2. A second REPL smoke against `core/target_nl.py` using a real `OllamaClient` over
   `httpx.MockTransport` (never a real network call): the gate correctly hits on "from now on I want
   to drink 2.5L a day" / Thai equivalents and misses on plain logs ("I drank 2.5L", "500ml");
   `classify_target_intent` returned the expected `TargetIntent`/`None` for a valid response, an
   `"unknown"` log-shaped response, a non-goalable habit, a negative goal, low confidence, a 503
   transport error, and malformed JSON — every failure mode returned `None`, none raised.
3. Full command: `.venv\Scripts\python.exe -m pytest -q` (never against `data/habits.db`; the live
   Task Scheduler service was not touched) → **844 passed, 7 failed, 1 skipped**. The 7 failures are
   the same pre-existing, unrelated failures IMPL-v1.1-shared.md already documented and verified via
   `git stash` against unmodified `main` (6 date-drift seed-date flakes in `test_adaptive_reminders.py`
   / `test_v09_gaps.py`, 1 stale `VERSION`-pin test in `test_charts.py`) — none touch commands,
   targets, or prompts. `tests/test_commands.py` (pre-existing, not modified by this module) and
   `tests/test_core_targets.py` (shared-surface's own file) both still pass unmodified, confirming
   this module's additions didn't regress the existing command-dispatch or goal-resolver contracts.
4. `tests/test_targets.py` + `tests/test_target_nl.py` run in isolation: 79 passed, 0 failed.

## Maps to acceptance criteria

- **AC13** → `core/commands.py:_match_target_slash`/`_parse_target_value`, `core/targets_command.py:_execute_set` + `tests/test_targets.py::test_ac13_*`.
- **AC14** → `core/commands.py:_parse_target_value`'s unit-alias multiplication + `tests/test_targets.py::test_ac14_*`.
- **AC15** → `core/commands.py:_TARGET_VALUE_RE` (accepts a non-positive/negative number rather than rejecting it at parse time) + `core/targets_command.py:_execute_set`'s `value_num <= 0` check + `tests/test_targets.py::test_ac15_*`.
- **AC16** → `core/targets_command.py:execute_target`'s `registry.get(habit_id) is None` branch + `tests/test_targets.py::test_ac16_*`.
- **AC17** → `core/targets_command.py:execute_target`'s `is_goalable` check + `tests/test_targets.py::test_ac17_*`.
- **AC18** → `core/commands.py:_TARGET_CLEAR_WORDS` + `core/targets_command.py:_execute_clear` + `tests/test_targets.py::test_ac18_*` (all four synonyms parametrized).
- **AC19** → `core/targets_command.py:_render_show`/`_default_note` + `tests/test_targets.py::test_ac19_*`.
- **AC20** → `core/targets_command.py:_render_show_all` + `tests/test_targets.py::test_ac20_*`.
- **AC27** → `core/commands.py:_match_target_slash`/`_build_target_th_set_pattern` (both anchored forms produce an identical `Command`, LLM-free) + the adversarial-corpus guard, both in `tests/test_targets.py::test_ac27_*`.
- **AC28** → `core/targets_command.py:_execute_set`/`_execute_clear`'s `try/except` around the DB write + `tests/test_targets.py::test_ac28_*` (both `set` and `clear` failure paths).
- **AC29** → `core/target_nl.py:classify_target_intent` + `core/targets_command.py:execute_target` (the `set` path is identical code for both entry points) + `tests/test_target_nl.py::test_ac29_*`. The "no `logs` row is written" half is a `main.py`-routing property (this module never touches `logs` at all — proved directly in `test_ac30_end_to_end_sets_water_target_no_log`, which raw-counts `logs` after going through the exact same code path AC29 uses).
- **AC30** → same code path, Thai input + `tests/test_target_nl.py::test_ac30_*`.
- **AC31** (NL-setting half only — the reminder-skip/streak half is shared-surface-owned, already covered by `tests/test_v11_shared_surface.py`) → `tests/test_target_nl.py::test_ac31_stretch_goalless_habit_gets_a_goal_from_nl_intent`, which classifies a stretch-goal NL message, sets it via `execute_target`, confirms `effective_goal` goes from `None` to `20.0`, and confirms clearing reverts it to `None`.
- **AC32** → `core/target_nl.py:_validate_intent`'s category/confidence checks + `tests/test_target_nl.py::test_ac32_*` (an `"unknown"`-classified log, a below-threshold-but-otherwise-valid-shaped response, a custom higher threshold, and a bare "500ml"-style log).
- **AC34** → `core/target_nl.py:classify_target_intent`'s `try/except` + `_validate_intent`'s every rejection branch + `tests/test_target_nl.py::test_ac34_*` (malformed JSON, unconfigured category, non-goalable category, non-positive goal x3, transport error, missing confidence field, unknown+null combo).

Not owned by this pass (shared-surface/integration-owned, per SPEC-v1.1.md §11's table): AC3, AC4,
AC6, AC10, AC12, AC21–AC26 (already verified by IMPL-v1.1-shared.md), the reminder-skip/streak half
of AC31, and AC33 (NL-target outage routing — depends on `main.py`'s deferred wiring, see below).
undo-ui's own ACs (AC1, AC2, AC5, AC7–AC9, AC11) belong to that module's own `IMPL-v1.1-undo-ui.md`.

## Known limitations

1. **`main.py` is untouched, as directed** — this module exposes clean, LLM-mockable functions
   (`commands.dispatch`'s `"target"` kind, `targets_command.execute_target`, `target_nl.
   looks_like_target_phrasing`/`classify_target_intent`) but does not wire them into the live
   message-handling loop. Exact wiring calls the integration step needs, in order:

   a. **Route `command.kind == "target"`** (from `commands.dispatch`, checked in the same place the
      existing `"undo"`/`"edit"`/`"snooze"`/`"query"` kinds are already routed) to:
      ```python
      reply = await targets_command.execute_target(
          command, db=db, config=config, registry=registry, lang=lang
      )
      await channel.send(reply)  # plain send — a target reply is not a log confirmation (R-U2 scope), no undo button
      ```

   b. **Full-NL target step (R-T13)** — insert *between* the existing health-monitor deferral check
      and the call to `parse_message`, gated on the Ollama-up path only, and only for text that is
      neither an anchored command (`commands.dispatch(text, registry)` already returned `None`) nor
      query-shaped:
      ```python
      if health_monitor is None or health_monitor.ollama_up:
          if commands.dispatch(text, registry) is None and target_nl.looks_like_target_phrasing(text):
              intent = await target_nl.classify_target_intent(text, llm, registry, config)
              if intent is not None:
                  set_command = commands.Command(
                      kind="target", category=intent.habit_id,
                      value_num=intent.goal_base_unit, target_action="set",
                  )
                  reply = await targets_command.execute_target(
                      set_command, db=db, config=config, registry=registry, lang=lang
                  )
                  await channel.send(reply)
                  return  # AC29/AC30: no `logs` row is written for a target-intent hit
      # else: fall through to parse_message exactly as today (AC33's outage/deferral behavior
      # already works unchanged, since this whole block is skipped when Ollama is down)
      ```
      Note `commands.dispatch` is called once already by the existing command-routing step earlier
      in `handle_inbound_message` — the integration step should reuse that same `None` result rather
      than calling `dispatch` a second time; shown above only for clarity of the gating condition.

2. **`core/i18n.py`'s `target_show_default_note` template only supports a numeric `{default:g}`** —
   SPEC-v1.1.md §3.4's illustrative `show_all` example shows `stretch: 20 min/day (default: none)`
   for a goal-able habit with an active override but no config default at all. Since I was directed
   to treat the shared i18n catalog as frozen (read, not modify), `_default_note` in
   `targets_command.py` simply omits the default-note clause entirely when `config_goal` is `None`
   (rather than risk a `.format()` crash on a `None` default) — so that specific case renders as
   `🎯 stretch: 20 min/day` with no trailing default clause, not the spec's literal `(default: none)`
   text. This is a cosmetic-only deviation (R-T10's *normative* rule — "default line shown only when
   override ≠ default" — is still satisfied; it's specifically the *textual shape* of a `None`
   default that the current catalog can't express). If Vera or Archi wants the exact illustrative
   text, the fix is a one-line addition to `core/i18n.py` (a `target_show_default_note_nogoal` key, or
   widening the existing template to accept a pre-formatted string) — flagging rather than silently
   deviating, per the playbook.
3. **A single-habit `/target <habit>` (`show`) on a goal-able habit with currently no goal at all**
   (e.g. `/target stretch` before any target has ever been set on it) has no dedicated catalog key
   either — SPEC-v1.1.md §3.4 only illustrates this "no goal" rendering for `show_all`, not the
   single-habit `show`. `_render_show` reuses `target_show_all_line_nogoal` (which includes a
   leading "• " bullet designed for a list context) for this edge case rather than adding a new key.
   Not exercised by any AC; flagged for the same reason as #2 above.
4. **The Thai anchored NL trigger's habit token is restricted to the live registry's actual ids/Thai
   labels** (`core/commands.py:_build_target_th_set_pattern`), rather than a generic
   "any non-digit run" character class. This is a deliberate, more conservative design than a literal
   reading of R-T7b's grammar (`ตั้งเป้า\s*<habit-th-or-id>\s*<value>$`) might suggest, because Thai
   is normally written with no spaces between words: a generic habit-token class risks a false
   positive on an unrelated sentence that happens to start with "เป้า" and later contains a number
   with no intervening space (e.g. "เป้าหมายของฉันคือ2000บาท", a diary-style reflection about a
   personal goal in baht) being misread as an attempted target command on an unrecognized habit. Since
   there is no AC requiring the Thai anchored trigger to detect an unrecognized habit (unlike the
   slash form's AC16), building the alternation from known habit tokens eliminates this false-positive
   class entirely at zero cost to any tested behavior — see `test_thai_diary_style_sentence_about_an_unrelated_goal_is_not_swallowed`
   in `tests/test_targets.py`.
5. Full-NL-vs-anchored precedence: per §10, the LLM classifier only ever produces a `set` intent;
   clearing/showing a target via free-form NL is intentionally unsupported (use the deterministic
   `/target ... default` / `/target ...` forms), matching the spec exactly.
