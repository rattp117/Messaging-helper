# Implementation — streak-display bug fix (line/v1.3.2)

## Files changed

| Path | Created/modified | Description |
|---|---|---|
| `src/habit_assistant/core/streaks.py` | modified | Added `display_streak(db, config, habit, today, user_id)` — the DISPLAY-ONLY "living streak" helper (see "How it works"). `compute_daily_summary`'s per-line streak now calls it instead of `compute_streak`. |
| `src/habit_assistant/core/dashboard.py` | modified | `render()`'s per-row streak (both the cadence branch and the goal/boolean/count branch) now calls `display_streak`. |
| `src/habit_assistant/core/discoverability.py` | modified | `build_habits_overview()`'s cadence-branch streak suffix (`/habits`) now calls `display_streak`. |
| `src/habit_assistant/core/wrapped.py` | modified | `_streak_text()` (the `/wrapped` recap card + its text fallback) now calls `display_streak`. |
| `src/habit_assistant/core/portal/users.py` | modified | `_current_streak()` (admin portal "Active users" streak column) now calls `display_streak`. |
| `src/habit_assistant/VERSION`, `pyproject.toml`, `src/habit_assistant/__init__.py` | modified | `1.3.1+line` → `1.3.2+line`. |
| `tests/test_streaks.py` | modified | 8 new tests: the `display_streak` truth table (today-met, today-pending, real gap, grace-bridged yesterday, paused-today ×2, cadence pass-through, cadence resurrection negative-control). |
| `tests/test_dashboard.py` | modified | 1 new test: the exact user-reported scenario end-to-end through `dashboard.render` (goal 2500, met ×2, partial today → 2, then crossing → 3). |
| `tests/test_digest.py`, `tests/test_discoverability.py`, `tests/test_wrapped.py`, `tests/test_portal_users_gaps.py` | modified | 1 regression test each, proving the fix reaches every switched surface. |
| `tests/test_line_release_gate.py`, `tests/test_portal_release_gate.py` | modified | Pre-existing hardcoded version-pin literals bumped `1.3.0+line` → `1.3.2+line` (see "Known limitations" — these were already stale before this patch, unrelated to the bug itself). |

No changes to `core/confirmation.py` — see "Investigation: core/confirmation.py" below for why.

## How it works

`compute_streak(db, config, habit, end_date, user_id)` (unchanged) walks backward from `end_date` and returns EXACTLY 0 whenever `end_date` itself doesn't (yet) qualify — by design, not a bug: it's what makes milestone-crossing and record-breaking exact (a streak that hasn't happened yet can't celebrate or break a record). The bug is that every *display* call site was passing `today` as `end_date`, so a real, unbroken streak through yesterday read as "0" all day, every day, until the exact moment today's own goal was met.

`display_streak` (new, in `core/streaks.py`) is the display-only fix: for a daily habit, it returns `compute_streak(today)` if that's `> 0`, else falls back to `compute_streak(today - 1 day)` — the streak as it stood at the end of yesterday, unaffected by anything that has or hasn't happened today. For a cadence (weekly) habit it is a deliberate **pass-through with no fallback at all** — `compute_streak(today)` is already the living streak for a cadence habit (an unmet current week contributes 0 without breaking older completed weeks), and re-anchoring to `today - 1 day` would in some cases (today is a Monday) incorrectly exempt a just-completed, genuinely-missed week from its own break check, resurrecting a streak that correctly ended. Every switched call site just replaced `streaks.compute_streak(...)` with `streaks.display_streak(...)` at the same argument shape — no other logic changed.

## Smoke test done

1. `pytest -n auto -q` — full LINE suite, run three times (once on baseline via `git stash` to confirm pre-existing failures, twice on the finished change): **5696 passed, 4 skipped, 1 xfailed, 0 failed**, in each of the two post-change runs.
2. Manual scratch-DB script (`smoke_display_streak.py`, never touches `data/habits.db`, per this repo's own post-incident rule) reproducing the exact live scenario from the bug report — goal 2500, met 2026-09-02 + 2026-09-03, 2026-09-04 partial at 1250/2500:
   ```
   compute_streak(today, partial 1250/2500) = 0   <- the bug, unchanged (exact semantics)
   display_streak(today, partial 1250/2500) = 2   <- the fix
   display_streak(today, now 2500/2500 total) = 3   <- crosses today, extends for real
   SMOKE OK
   ```

## Caller-by-caller table (every `compute_streak`/`crossed_milestone`-adjacent call site)

| # | Site | `end_date` passed | Keep / Switch | Why |
|---|---|---|---|---|
| 1 | `streaks.py:crossed_milestone` (milestone-crossing, internal) | `today`, but only ever reached *after* `day_qualifies(today)` is already confirmed `True` | **Keep** `compute_streak` | Task explicitly excludes milestone-crossing. Also inherently unaffected by construction: the function returns early (`None`) before computing a streak unless `today` already qualifies, so it never observes the "not-yet-met" 0 case. |
| 2 | `streaks.py:compute_daily_summary` → digest's "🌙 Today's Summary" section + Telegram-mode `daily_summary_job` | `today` | **Switch** to `display_streak` | Explicit target ("the digest's summary lines"). Single shared call site — fixes both the LINE digest and the Telegram-edition standalone daily summary at once. |
| 3 | `dashboard.py:render` (cadence branch) | `today` | **Switch** | Explicit target; cadence habit takes `display_streak`'s pass-through path. |
| 4 | `dashboard.py:render` (goal/boolean/count branch) | `today` | **Switch** | Explicit target — this is the literal "0" the user saw, sent right after every log via `dashboard.refresh`. |
| 5 | `digest.py:_grace_bridged` | `day_before_yesterday` (always a fully-elapsed past day) | **Keep** `compute_streak` | Not a "today, still open" computation — no ambiguity to fix. Must exactly mirror `grace.py:evaluate_grace`'s own re-derivation of the same number (its own docstring: "RE-DERIVED via the identical `streaks.compute_streak(..., day_before_yesterday, ...)` call"); switching would risk the digest reporting a different protected-streak number than the number grace itself decided on. |
| 6 | `grace.py:evaluate_grace` | `day_before_yesterday` (fixed past day) | **Keep** | The actual bridging decision's source of truth — a fixed, fully-elapsed past date, no "today in progress" ambiguity. |
| 7 | `records.py:update_on_log` (`longest_streak` record check) | `today` | **Keep** | Task explicitly excludes "records longest-streak tracking." Exactness required: a streak that hasn't been earned yet today must not falsely tie/break a stored record and fire an early celebration. |
| 8 | `review.py:_compute_habit_stats` (duration streak, weekly review) | `end_date` (the review's own reference day, effectively "today" when the review runs) | **Keep**, per Archi's explicit instruction | Task explicitly excludes "the weekly review's own historical computations (review shows completed periods)." Noting for the record: the review runs at a fixed time (`[weekly_review] time`, default 20:00) and reports on a point-in-time snapshot as of that run, not a continuously-refreshed live display like the dashboard — so leaving it as `compute_streak` is defensible on its own terms too, not just as a directive to follow. |
| 9 | `wrapped.py:_streak_text` (`/wrapped` card + text fallback) | `today` | **Switch** | Live "current streak" glance, same class as the dashboard row — a partial today shouldn't put a false 0 in an otherwise celebratory recap. |
| 10 | `portal/users.py:_current_streak` (admin "Active users" table) | `today` | **Switch** | Live display for an admin glancing at the table; no record/milestone logic depends on this number. LINE-only module (no Telegram-edition counterpart), so no PORT TO MAIN note there. |
| 11 | `discoverability.py:build_habits_overview` (cadence branch, `/habits`) | `today` | **Switch** | Explicit target ("/habits listing"). The non-cadence branch of `/habits` shows no streak at all, so nothing else in that file needed a change. |

## Cadence (weekly) truth table — determined and documented

Traced `_weekly_walk` directly rather than assuming daily-habit logic transfers:

- **Current (partial) week not yet met, prior weeks still an active streak** → `compute_streak(today)` already returns the correct total (the unmet current week contributes 0 but does **not** break the walk through older completed weeks — Rule 4's own "never over-reported mid-week"). `display_streak` is a straight pass-through here; proven by `test_display_streak_cadence_pass_through_when_prior_weeks_still_active`.
- **Current (partial) week not yet met, and the immediately preceding *completed* week genuinely missed its quota (a real break)** → `compute_streak(today)` already, correctly, returns 0. Applying the daily habit's "fall back to `today - 1 day`" formula here would be **actively wrong**: when `today` is a Monday, `today - 1 day` is last week's own Sunday, which re-anchors the walk so that just-missed week becomes the new call's "current" (break-exempt) week — silently resurrecting whatever older streak existed before it. `display_streak` therefore takes **no fallback at all** for a cadence habit; proven as a negative control by `test_display_streak_cadence_never_resurrects_a_genuinely_broken_week` (constructs exactly this scenario and asserts the result stays 0).

Conclusion documented in `display_streak`'s own docstring: cadence is a deliberate pass-through, not an oversight.

## Investigation: `core/confirmation.py`

The dispatch named `core/confirmation.py` as "the user's reported surface." Investigation found `confirmation.py` calls **no** `compute_streak`-derived value except `streaks.crossed_milestone` (the milestone-crossing suffix) and `streaks.streak_unit` — it carries no bare "current streak" number in any of its templates (`water_confirmation`, `stretch_confirmation`, `confirm_numeric_goal`, etc. — checked every one in `core/i18n.py`). `crossed_milestone` is unaffected by this bug by construction (see table row 1), so there is no code path in `confirmation.py` that could have literally rendered "0."

The surface the user actually saw is `dashboard.refresh` → `dashboard.render`'s per-row `streak {n}d` suffix: both `core/routing.py`'s typed-log handler and `core/quicklog.py`'s `_log_and_confirm` call `dashboard.refresh(...)` immediately after sending the confirmation, in the same logging flow (the "dashboard-in-reply" feature, v1.2.0) — that combined reply (confirmation text, then the live dashboard) is what a user experiences as "did my streak register?" I wrote the confirmation-surface acceptance test (`test_render_streak_shows_living_streak_not_zero_when_today_is_partial` in `tests/test_dashboard.py`) against that real code path rather than inventing a new streak line in `confirmation.py` — adding user-visible text that doesn't currently exist would be a product change, not a bug fix, and outside "smallest change that satisfies the spec." No production line in `confirmation.py` was touched; `Files changed` above reflects that.

If this reasoning is wrong and Archi wants a dedicated always-on streak line inside `confirmation.py` itself, flag it back — that is a small, additive follow-up (one new suffix branch in `confirmation.suffix()`, `display_streak`-backed) I can add on request.

## Interaction checks

- **Grace-bridged yesterday**: `_daily_walk(end_date=yesterday)` already treats a NEUTRAL (grace-protected) day as "held" (skip, don't increment, don't break) — `display_streak`'s fallback call inherits this for free, no special-casing needed inside `display_streak` itself. Proven by `test_display_streak_grace_bridged_yesterday_shows_the_held_streak`.
- **Paused today**: a paused `today` with no voluntary log classifies as NEUTRAL, not MISSED (Rule 2) — `_daily_walk` already holds through it without breaking, so `compute_streak(today)` is *already* the correct held count (not 0) and `display_streak` returns it directly, never reaching the fallback branch. If a real gap existed *before* the pause started, the held count is legitimately 0 and stays 0 either way. Proven by `test_display_streak_paused_today_shows_the_held_streak_directly` and `test_display_streak_paused_today_with_a_real_gap_before_still_shows_zero`.
- **Telegram-mode surfaces**: every file switched (`streaks.py`, `dashboard.py`, `discoverability.py`, `wrapped.py`) is shared `core/`, identical on the Telegram (`main`) branch, and `compute_daily_summary` specifically feeds both `core/digest.py` (LINE) and `core/jobs.py:daily_summary_job` (Telegram) through the same function — this patch is byte-portable. Each edited shared-`core/` call site carries a `PORT TO MAIN` comment (consolidated into one detailed note on `display_streak` itself, plus a one-line pointer at each call site) flagging it for the Telegram edition. `core/portal/users.py` has no Telegram counterpart (LINE-only admin portal), so it carries no such note.

## Maps to acceptance criteria (from the dispatch)

- Unit truth table (today-met / today-pending / yesterday-gap / grace-yesterday / paused-today / cadence variants) → `tests/test_streaks.py`, 8 tests, all passing.
- Confirmation-surface test proving the user's exact scenario → `tests/test_dashboard.py::test_render_streak_shows_living_streak_not_zero_when_today_is_partial` (see "Investigation" above for why this exercises `dashboard.render`, the real surface, rather than `confirmation.py`).
- Dashboard row parity → same test (asserts `streak 2d` then `streak 3d` in the actual rendered row).
- Telegram-mode surfaces byte-identical where they share code paths → satisfied by construction (shared `core/`, single switched call sites); no Telegram checkout exists in this worktree to run directly, noted as a porting item rather than independently re-verified here.
- Version bump 1.3.1+line → 1.3.2+line, 3 files → done (`VERSION`, `pyproject.toml`, `src/habit_assistant/__init__.py`).
- Exit bar (foreground): full LINE gate `-n auto`, 0 failed → **5696 passed, 4 skipped, 1 xfailed, 0 failed**, confirmed on two consecutive runs.

## Known limitations

- `core/confirmation.py` was not modified — see "Investigation" above. If Archi's intent was a literal new streak line in the confirmation text itself (not the dashboard-in-reply), that's an additive follow-up, not implemented here.
- `tests/test_line_release_gate.py` and `tests/test_portal_release_gate.py` had hardcoded `1.3.0+line` version-pin literals that were already stale (the working tree was at `1.3.1+line` before this patch touched anything — confirmed via `git stash`, these two tests failed on baseline too). Bumped both to `1.3.2+line` as part of this patch's own version bump, per each test's own in-file comment ("a future bump must update this literal too"). Pre-existing gap, not introduced by this change, but fixed here since the exit bar requires 0 failures.
- Did not independently verify the Telegram (`main`) branch, since this worktree only contains `line-version`; the `PORT TO MAIN` comments are the hand-off for that.
