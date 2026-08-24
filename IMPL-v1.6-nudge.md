# Implementation — v1.6.0 `nudge` module ("Almost there" end-of-day nudge)

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/core/nudge.py` | Created, then modified (Vera round 1) | `build_nudge_message` (deterministic bilingual body, folds every "close" habit into ONE message) + `run_due_nudges` (the minutely-tick sibling of `run_due_checkins`/`run_due_reminders`, fires exactly once/day at `config.nudge.time`). Round-1 fix: `channel.send` now wrapped in its own `try/except` — see Iteration log. |
| `src/habit_assistant/core/i18n.py` | Modified | New disjoint `nudge` catalog block (EN+TH): `nudge_header`, `nudge_line`. Appended after the existing `help_dnd_cmd` entry, right before the `CATALOG` dict's closing brace — no existing key touched. |
| `tests/test_nudge.py` | Created | 32 tests covering every scenario in scope (see "Maps to acceptance criteria" below). |

Not touched (per scope): `main.py`, `core/commands.py`, `core/i18n.py`'s existing keys, dashboard/heatmap/insights files. The nudge has no command of its own (OQ2), so `commands.py`/`CommandKind` needed no change from this module.

## How it works

`run_due_nudges(channel, config, registry, db, clock)` is a tick function meant to run on the same minutely `CronTrigger(second=0, ...)` job as `run_due_reminders`/`run_due_checkins` (see "Wiring instructions" below) — it returns immediately unless the current wall-clock minute (in `config.app.timezone`) equals `config.nudge.time` exactly, which is what makes it fire once/day by construction, mirroring `run_due_checkins`'s own `:00`-only guard for the same reason.

On that one minute, for each `db.active_user_ids()`: it reads `checkins.effective_checkin(db, config, user_id)` and gates purely on the **enabled** boolean (OQ2 — ignores the check-in *window*, since a user's end-of-day nudge time is independent of their hourly check-in window); then `reminders.in_dnd_now` for DND suppression; then calls `build_nudge_message`, which iterates the registry, computing `targets.effective_goal` (so a per-user `/target` override is respected) and `db.sum_value` for each habit, and folds every habit whose today's total is `>= threshold_pct% of goal` but `< goal` into **one** message (never one send per habit — this is what keeps "at most one nudge/user/day" true even with several close habits at once). A goal-less habit (`effective_goal` returns `None`) never contributes, no special-casing needed. If nothing is close, `build_nudge_message` returns `None` and nothing is sent (silence, not a nag).

The build/eligibility steps and the `channel.send` call are each wrapped in their **own** `try/except` (two stages, per user) — mirrors `core/announce.py:announce_release`'s identical two-stage shape. A bad row or DB hiccup while building one user's message is logged and that user is skipped; a transport failure while *sending* one user's message is independently logged and skipped, without ever aborting the fan-out for subsequent users or letting either exception propagate out of `run_due_nudges` itself (SPEC-v1.6.md §3.4: "the nudge never raises").

## Smoke test done

- Direct script run (`.venv/Scripts/python.exe`, `PYTHONPATH=src`): built a real on-disk `Database`, enabled check-ins for a user via `checkins.execute_checkin`, logged 2100ml against the default 2500ml water goal (84%, above the 80% threshold), and called `nudge.run_due_nudges` with a fixed 20:00 clock. Observed the exact expected send: `🎯 ใกล้ถึงเป้าหมายวันนี้แล้ว!\n• อีกแค่ 400 มล. ก็ถึงเป้าหมายน้ำวันนี้แล้ว สู้ๆ นะ` (Thai, since no `/lang` was set — matches `resolve_unprompted_language`'s documented default, same as every other unprompted-send module in this codebase).
- `pytest tests/test_nudge.py -q` → **32 passed**.
- `pytest tests/ -q` (full suite, first hand-off) → **2674 passed, 0 failed, 1 skipped, 1 xfailed** in 169.08s. Reconciled exactly against the stated 2642 baseline: +32 new tests (`test_nudge.py`), zero regressions, zero newly-skipped.
- **Round 2 (post-Vera fix):** `pytest tests/test_nudge.py tests/test_nudge_gaps.py -q` → **48 passed** (my 32 + Vera's 16, including the previously-failing `test_fail_open_fan_out_one_users_send_failure_does_not_block_the_others`). `pytest tests/ -q` (full suite) → **2893 passed, 0 failed, 1 skipped, 1 xfailed** in 141.08s — above the coordinator's stated 2813+ target because the dashboard/heatmap/insights parallel tracks landed more tests concurrently while this fix was in flight (per the coordinator's own reconciliation note); zero failures, zero regressions attributable to this fix. The only warnings emitted (70, all pre-existing) are matplotlib's known "Thai glyph missing from DejaVu Sans font" notices from `core/charts.py` — unrelated to this module.

## Maps to acceptance criteria

- **AC-N1** (close, once/day) → `core/nudge.py:build_nudge_message` (threshold math) + `run_due_nudges` (the once/day firing guard). Tests: `test_threshold_boundary_79_percent_is_not_close`, `test_threshold_boundary_80_percent_is_close`, `test_threshold_boundary_99_percent_is_close`, `test_threshold_boundary_100_percent_is_already_met_not_close`, `test_far_from_goal_is_not_close`, `test_configurable_threshold_pct_is_honored`, `test_multi_habit_close_folds_into_a_single_message`, `test_at_most_one_nudge_message_per_user_at_the_fixed_minute`, `test_fires_at_exactly_the_configured_time`, `test_does_not_fire_a_minute_before_or_after`, `test_configured_nudge_time_is_honored_not_hardcoded`, `test_timezone_aware_clock_is_converted_to_app_timezone`.
- **AC-N2** (opt-in + DND) → `run_due_nudges`'s `effective_checkin`/`in_dnd_now` gates. Tests: `test_checkin_off_by_default_means_no_nudge`, `test_checkin_on_makes_the_user_nudge_eligible`, `test_checkin_explicitly_off_means_no_nudge`, `test_enablement_rides_checkin_on_off_regardless_of_the_checkin_window`, `test_dnd_suppresses_a_due_nudge`, `test_dnd_is_scoped_to_the_user_in_it`, `test_no_message_when_nothing_qualifies`.
- **AC-N3** (registry-generic + bilingual) → `build_nudge_message`'s registry iteration (no hardcoded habit ids) + `i18n.t` catalog calls. Tests: `test_goal_less_habits_never_nudge`, `test_boolean_and_text_habits_never_nudge`, `test_target_override_is_respected_over_the_config_default`, `test_bilingual`, `test_isolated_per_user`, `test_isolation_a_disabled_far_or_dnd_user_never_leaks_into_an_enabled_users_send`, `test_run_due_nudges_respects_each_users_own_language_preference`, `test_nudge_module_never_imports_or_calls_an_llm`, `test_run_due_nudges_never_calls_ollama_end_to_end`.
- Interplay (nudge minute coinciding with a check-in hour, spec's own R-N1 wording: "runs on the SAME minutely job" — read as an independent sibling call, not a merge/suppression rule; no spec text says one suppresses the other) → `test_nudge_and_checkin_both_fire_independently_when_due_at_the_same_minute`, `test_nudge_still_fires_when_the_checkin_window_excludes_20_00`. Both ticks fire independently and produce separate messages when both are due at the same minute; the nudge's own enablement check is unaffected by whether "now" happens to also fall inside the check-in *window*.

All 3 owned ACs (AC-N1, AC-N2, AC-N3) are covered.

## Known limitations

- **`nudge_close` naming**: SPEC-v1.6.md §5's interface list only names `run_due_nudges` as the public function; I added `build_nudge_message` as a private-surface-but-exported helper (mirrors `core/checkins.py`'s own `build_checkin_message` decomposition) purely for testability — it is not part of the spec's required interface, just an implementation convenience.
- **Message shape for multiple close habits**: SPEC-v1.6.md §3.3's illustrative example shows a single unadorned line ("💧 Just 300 ml to hit your water goal today — you've got this.") for the one-habit case. Since R-N2 requires **at most one message per user per day** even when several habits qualify simultaneously, I implemented a uniform header + one bulleted line per close habit (`nudge_header` + `nudge_line`, mirroring `checkin_header`/`checkin_line_progress`'s established shape in this codebase) for both the single- and multi-habit cases, rather than switching shape based on count. **Resolved: Vera reviewed this judgment call in `TEST-v1.6-nudge.md` and ruled it CONFORMANT** — §3.3's example is the same illustrative-example convention used for every other output in that section, not a byte-exact contract; the normative text (R-N1/R-N2/AC-N1/AC-N2) specifies neither line count nor header presence. No change made; kept as-is per her recommendation.
- **No per-habit emoji**: the spec's example uses 💧 (water-specific). Since the module is registry-generic (R-X1, no hardcoded habit ids), I used a single generic 🎯 emoji for the header instead of a per-habit icon — consistent with how `confirm_numeric_goal`/`checkin_header` etc. already avoid per-habit iconography elsewhere in this codebase.

## Wiring instructions (integration — main.py, NOT done by this pass, per scope)

Two changes, both mirroring the existing `checkin_tick` wiring exactly:

**1. Import** — add `nudge` to the `from habit_assistant.core import (...)` block (`main.py` lines 28-47), alphabetically between `i18n` and `preferences`:
```python
from habit_assistant.core import (
    access,
    announce,
    audit,
    audit_view,
    checkins,
    commands,
    discoverability,
    history_view,
    i18n,
    nudge,          # <-- new
    preferences,
    preparse,
    query,
    schedules,
    streaks,
    target_nl,
    targets,
    targets_command,
    undo_ui,
)
```

**2. Scheduler job** — add a `nudge_tick` job right after the existing `checkin_tick` block (`main.py`, immediately after line 1355's closing `)`, before `review_hour, review_minute = ...`):
```python
    # SPEC-v1.6.md R-N1 (module `nudge`): a sibling of `checkin_tick`/
    # `reminder_tick` on the SAME minutely cadence -- `run_due_nudges`'s own
    # internal `hhmm != config.nudge.time` guard is what limits it to firing
    # once per day, so the cron trigger itself still needs to fire every
    # minute for that guard to be evaluated.
    scheduler.add_job(
        nudge.run_due_nudges,
        trigger=CronTrigger(second=0, timezone=config.app.timezone),
        args=[channel, config, registry, db],
        id="nudge_tick",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=30,
    )
```

No other `main.py` change is needed for this module: no command routing (OQ2 — no `/nudge` command), no help-text addition (spec doesn't list one; nudge rides `/checkin`'s existing `help_checkin_cmd` framing), no public command-menu entry.

## Iteration log

### Vera round 1 (`TEST-v1.6-nudge.md`) — FAIL, 1 bug, now fixed

**Failure:** `tests/test_nudge_gaps.py::test_fail_open_fan_out_one_users_send_failure_does_not_block_the_others`. Vera's scenario: 3 active users (`OWNER`, `MEMBER`, `THIRD`), all check-in-enabled and squarely "close" on the same habit at 20:00; `channel.send` raises for `MEMBER` only. Expected: `OWNER` and `THIRD` both still get nudged, `run_due_nudges` returns normally. Actual: `run_due_nudges` raised `RuntimeError` and aborted mid-fan-out — `THIRD` (processed after the failing `MEMBER`) never ran.

**Root cause:** `core/nudge.py`'s per-user `try/except` (originally lines 186-198) wrapped `effective_checkin`/`in_dnd_now`/`build_nudge_message`, but the `await channel.send(user_id, message)` call sat *after* that `try` block closed — a send failure for one user was therefore uncaught, both aborting every subsequent user in the fan-out and propagating out of `run_due_nudges` itself, contradicting SPEC-v1.6.md §3.4's "the nudge never raise[s]" contract.

**Fix:** wrapped `channel.send` in its own `try/except Exception: logger.exception(...); continue`, a second independent stage per user, mirroring `core/announce.py:announce_release`'s established two-stage shape (one try around building what to send, a separate try around the send call itself) for exactly this class of failure. No other logic changed.

**Verification:** re-ran `tests/test_nudge_gaps.py` (16/16 pass, including the previously-failing one) + `tests/test_nudge.py` unmodified (32/32 still pass) + full suite (2893 passed, 0 failed, 1 skipped, 1 xfailed — see Smoke test above).

**Judgment-call note:** Vera's report also reviewed my "Known limitations" entry on the header+bullet message shape (vs. §3.3's bare single-line illustrative example) and ruled it **CONFORMANT** — no change made there.

---

The 10 test-authoring bugs found and fixed while I wrote `tests/test_nudge.py` myself during the first pass (not a Vera round) are noted here for transparency:

- **Water-id goal collision** (10 of the 32 tests, initially failing): `core/targets.py:config_goal` special-cases the exact habit id `"water"` to `config.reminders.water.goal_ml` (the legacy default, 2500.0) regardless of what goal a test's own custom `Habit("water", ..., goal=1000.0)` declares. Several of my own test fixtures used a custom habit with id `"water"` and a hand-picked goal (1000.0) to get round percentage boundaries, which silently got overridden to 2500.0 by that special case, making the boundary math wrong. Root-caused via `ast`/direct smoke-test tracing, fixed by renaming those custom-goal fixtures to a non-builtin id (`"juice"`) — `DEFAULT_REGISTRY`'s real `"water"` habit (used elsewhere with the real 2500ml default and 2000.0/2500.0 log values) was unaffected and correct throughout.
- **Default-language assumption**: several `run_due_nudges` tests asserted English-specific content (`"water" in message`, `i18n.t(..., "en")`) without first setting the test user's `/lang` preference. `i18n.resolve_unprompted_language` defaults to Thai (this codebase's documented AC6.3 default) for a user with no stored preference, so those sends were correctly Thai and the assertions were wrong, not the production code. Fixed by explicitly calling `execute_lang(... "/lang en" ...)` before the content-sensitive assertions, matching the existing `test_checkins.py::test_run_due_checkins_respects_each_users_own_language_preference` convention.
