# Test Report — v1.7.0 custom habits, track `sweep` (17-surface two-user isolation)

## Summary
- Total: 21 tests (all new)
- Passed: 21
- Failed: 0
- Status: **PASS**

Scope per SPEC-v1.7.md §11's module-split table: `AC-S1` (17-surface two-user
isolation) and `AC-S2` (per-user extraction prompt/schema). Also verified, at
Archi's explicit request: AC-5 from the two-user angle (a user with no custom
habits stays byte-identical to v1.6 even while another user, in the same
database, has one).

Driven entirely by inserting `user_habits` rows **directly** via
`db.add_user_habit` (never `/addhabit`) — no dependency on the parallel
`habitdef` track, per SPEC-v1.7.md §11's own design.

Full relevant suite run alongside the new file: **3099 passed / 0 failed / 1
skipped / 1 xfailed** (baseline before this pass was 3078/0/1/1xf — the
21-test delta is entirely this file; nothing else changed). Whole suite green.

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_v17_isolation_sweep.py` | 21 | AC-S1 (all 17 surfaces), AC-S2, AC-5 (two-user angle) |

## Setup (shared by every test)

Two active users seeded directly via `db.upsert_user`: `OWNER` (user A) and
`MEMBER` (user B). A's custom habit is inserted directly via
`db.add_user_habit`: `id="reading"`, `type="numeric"`, `label_en="reading"` /
`label_th="อ่านหนังสือ"`, `unit_en="pages"` / `unit_th="หน้า"` (deliberately
**non-colliding** with any base habit's unit — `stretch`'s unit is also
"min"/"นาที", the spec's own illustrative example; a non-colliding unit was
chosen so this file tests pure isolation, not the separate unit-collision
degrade rule R-V4/AC-H4, which is `habitdef`'s own scope, not `sweep`'s),
`goal=20`. Per-user registries are built via `HabitRegistry.for_user(config,
db, user_id)` — the exact same entry point every production consumer uses.

## AC coverage

| AC | Surface | Test(s) | Result |
|---|---|---|---|
| AC-S1 | (1) free-text/LLM extraction | `test_surface_1_extraction_resolves_the_custom_habit_only_for_its_owner` | PASS |
| AC-S1 | (2) preparse instant logging (zero-LLM) | `test_surface_2_preparse_instant_log_only_resolves_for_the_owner` | PASS |
| AC-S1 | (3) undo button | `test_surface_3_undo_describes_and_removes_the_custom_habit_only_for_its_owner` | PASS |
| AC-S1 | (4) `/edit` | `test_surface_4_edit_resolves_the_custom_unit_only_for_its_owner` | PASS |
| AC-S1 | (5) `/target` | `test_surface_5_target_only_resolves_the_custom_habit_for_its_owner` | PASS |
| AC-S1 | (6) `/remind` + the reminder tick | `test_surface_6_remind_command_only_resolves_the_custom_habit_for_its_owner`, `test_surface_6_reminder_tick_fires_the_custom_habit_reminder_only_to_its_owner` | PASS |
| AC-S1 | (7) streaks + milestones | `test_surface_7_milestone_reached_only_for_the_habit_owner` | PASS |
| AC-S1 | (8) daily summary | `test_surface_8_daily_summary_only_mentions_the_custom_habit_for_its_owner` | PASS |
| AC-S1 | (9) weekly review + charts | `test_surface_9_weekly_review_only_includes_the_custom_habit_for_its_owner` | PASS |
| AC-S1 | (10) `/habits` | `test_surface_10_habits_listing_only_shows_the_custom_habit_to_its_owner` | PASS |
| AC-S1 | (11) `/history` | `test_surface_11_history_only_shows_and_resolves_the_custom_habit_for_its_owner` | PASS |
| AC-S1 | (12) `/heatmap` | `test_surface_12_heatmap_only_resolves_the_custom_habit_for_its_owner`, `test_surface_12_execute_heatmap_rejects_an_unresolved_habit_for_the_non_owner` | PASS |
| AC-S1 | (13) `/records` | `test_surface_13_records_only_resolve_the_custom_habit_for_its_owner` | PASS |
| AC-S1 | (14) `/trends` | `test_surface_14_trends_only_resolve_the_custom_habit_for_its_owner` | PASS |
| AC-S1 | (15) check-ins | `test_surface_15_checkin_message_only_mentions_the_custom_habit_for_its_owner` | PASS |
| AC-S1 | (16) the nudge | `test_surface_16_nudge_message_only_mentions_the_custom_habit_for_its_owner` | PASS |
| AC-S1 | (17) the dashboard | `test_surface_17_dashboard_only_shows_the_custom_habit_to_its_owner` | PASS |
| AC-S2 | per-user extraction prompt/schema, bounded | `test_ac_s2_extraction_prompt_and_schema_are_per_user` | PASS |
| AC-5 (two-user angle, Archi's explicit ask) | B byte-identical to v1.6 while A has a custom habit; A = base + exactly one extra habit, base habits untouched | `test_ac5_member_stays_byte_identical_to_v16_while_owner_has_a_custom_habit` | PASS |

Every AC-S1 surface listed in SPEC-v1.7.md §8 appears above — 17/17.

## What each surface actually proves (not just "ran without error")

- **Extraction (1) / AC-S2**: an adversarial fake LLM (`_ReadingClaimingLLM`)
  always claims category `"reading"` regardless of whose prompt built the
  call. For A it logs correctly; for B, `core/parser.py:_validate`'s
  `category not in registry.ids()` check rejects it and B gets the
  clarifying question — proving the isolation boundary is the registry
  actually passed in, not model good behavior. `build_extraction_system_
  prompt`/`category_enum()` are asserted directly to differ per registry.
- **Preparse (2)**: `preparse.deterministic_parse("20 pages", ...)` resolves
  for A's registry and returns `None` for B's (no matching unit token) —
  checked directly, then re-proven end-to-end via `handle_inbound_message`
  with a `_RaisingLLM` (any LLM call would fail the test) to confirm the
  zero-LLM fast path is what actually fired.
- **Undo (3)**: text `/undo` correctly describes and removes A's own custom
  log. Button path: B taps a callback naming A's row id — the pre-existing
  per-user ownership check (`row["user_id"] != chat_id`) refuses it
  regardless of which registry B's own `handle_undo_callback` call was
  given, and the row is provably untouched (`deleted_at is None`).
- **Edit (4)**: "make that 15 pages" resolves the custom unit and edits the
  value for A with **zero** LLM calls; for B the same message's unit doesn't
  resolve at all, so `dispatch()` returns `None` and it falls through to the
  ordinary (LLM) log path instead of silently misfiring as an edit.
- **Target (5)**: `/target reading 25` sets A's goal; for B it returns
  `target_invalid_habit` and no row is ever written to `habit_targets` for
  B under that id.
- **Remind (6)**: `/remind reading 09:00` stores the override only for A;
  for B nothing is stored. The reminder tick (`run_due_reminders` with
  `registry_for=RegistryProvider.for_user`) fires exactly one message, to
  A only, at 09:00 — no base habit's default times include that minute, so
  a leak to B would have been caught by the `len(channel.sent) == 1`
  assertion.
- **Streaks/milestones (7)**: three consecutive goal-meeting days for A's
  custom habit cross the default 3-day milestone (🔥 + the habit's own
  label in the very confirmation that triggered it); B's registry has no
  `Habit` object for `"reading"` to even compute a streak against.
- **Daily summary (8) / Weekly review (9)**: both rendered directly from
  each user's own registry — A's includes the custom habit's label, B's
  never does, even though both users logged on the same days in the same
  database.
- **`/habits` (10) / `/history` (11)**: A's listing includes "reading"; B's
  does not (only the three base habits). `/history reading` for B returns
  the friendly `history_invalid_habit` reply — B can't even filter by a
  habit id that exists only in A's namespace.
- **`/heatmap` (12)**: `_resolve_habits` is registry-generic (resolves for
  A, empty for B); `execute_heatmap` with `category="reading"` for B
  short-circuits to `heatmap_invalid_habit` **before** any render/send is
  attempted (`channel.images == []`) — sidesteps the optional-matplotlib
  dependency entirely for the isolation assertion itself.
- **`/records` (13) / `/trends` (14)**: A's log silently seeds a
  `habit_records` row for `("OWNER", "reading", "best_day")`; the SAME
  `(MEMBER, "reading", ...)` row is confirmed to be `None` — B never gets a
  record row for a habit that isn't theirs. Both `render()` calls resolve
  for A and return the friendly invalid-habit reply for B.
- **Check-ins (15) / Nudge (16)**: both message builders called directly
  per registry — A's mentions the custom habit's label (goal-bearing, not
  yet met / "close"), B's never does.
- **Dashboard (17)**: after both users log through the real
  `handle_inbound_message` path with a live pin, A's edited board text
  includes the custom habit's label (resolved in the board's own language,
  independent of the inbound message's language) and B's never does.
- **AC-5 (two-user angle)**: `registry_b.ids()` is byte-identical to the
  plain base-config registry even while A's custom habit exists in the same
  DB; B's water-log confirmation is asserted **character-for-character**
  against the exact pre-v1.7 (v1.6) literal
  (`"✅ 500 ml logged — today 500 / 2500 ml (20%)"`); and, symmetrically,
  A's own registry is exactly base + one extra habit with every base habit
  definition (`registry_a.get("water"/"stretch"/"diary")`) unchanged.

## Failures (if any)

None.

## Regressions detected

None. Full suite: 3099 passed / 0 failed / 1 skipped / 1 xfailed (up from
the pre-sweep baseline of 3078/0/1/1xf — the 21-test delta is entirely this
file; no existing test needed any change).

## Notes for Archi / next steps

- This track does not touch `core/habitdef.py`, `commands.py`'s
  `/addhabit`/`/delhabit` matching, or their i18n copy — those remain the
  `habitdef` track's own deliverable (AC-H1–AC-H6), unverified by this
  report.
- No isolation leak or broken surface was found anywhere in the 17-surface
  checklist. The shared-surface registry rewiring (`HabitRegistry.for_user`,
  `RegistryProvider`, and the per-user `registry`/`registry_for` threading
  IMPL-v1.7-shared.md reports) holds up under adversarial two-user pressure
  on every consumer SPEC-v1.7.md §8 names.

## Recommendation

**Ready to ship** — `sweep` track (AC-S1, AC-S2) is PASS; AC-5's two-user
angle is independently confirmed. Safe to proceed to integration with the
`habitdef` track once it reports done.
