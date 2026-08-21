# Implementation — v1.2.0 Multi-user support (shared surface)

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/storage/models.py` | Modified | `LogEntry` gains `user_id: str` as its 2nd field (positional, breaking) |
| `src/habit_assistant/storage/migrations.py` | Modified | Migration 006: `users` table, `logs.user_id` (+ index), rebuilds `habit_targets` to a surrogate-id PK with `UNIQUE(user_id, habit_id)`, adds `user_reminder_times` |
| `src/habit_assistant/storage/db.py` | Modified | Every scoped accessor takes `user_id` first; adds `attribute_legacy_to_owner`, `get_user`/`upsert_user`/`set_user_language`/`set_user_quiet_hours`/`list_users`/`active_user_ids`, `get_reminder_times`/`set_reminder_times`/`clear_reminder_times` |
| `src/habit_assistant/channels/base.py` | Modified | `Channel` ABC: `send`/`send_image`/`send_actionable` take `chat_id` first; `run(on_message, on_callback=None)` — both callbacks gain `chat_id` first |
| `src/habit_assistant/channels/telegram.py` | Modified | Constructor's owner param renamed `owner_chat_id` (public, defaulting/health only); all `build_send_*_request` take `chat_id`; `run()` extracts chat id from the update for both message and callback paths |
| `src/habit_assistant/channels/line.py` | Modified | Stub signatures updated to match the new ABC |
| `src/habit_assistant/core/habits.py` | Modified | `log_entry_from_result(..., user_id)` — appended last |
| `src/habit_assistant/core/targets.py` | Modified | `effective_goal(db, habit, config, user_id)` — appended last |
| `src/habit_assistant/core/streaks.py` | Modified | `day_qualifies`/`compute_streak`/`crossed_milestone`/`compute_daily_summary`/`run_daily_summary` all take `user_id`; `run_daily_summary`'s `lang` is now a required explicit param |
| `src/habit_assistant/core/review.py` | Modified | `compute_weekly_stats`/`run_weekly_review`/`render_weekly_review_charts` take `user_id`; `lang` required explicit on the async ones |
| `src/habit_assistant/core/charts.py` | Modified | `render_habit_chart`/`render_weekly_charts` take `lang`/`user_id` |
| `src/habit_assistant/core/query.py` | Modified | `_aggregate`/`answer_question` take `user_id` |
| `src/habit_assistant/core/garmin.py` | Modified | `build_garmin_report` takes `user_id` |
| `src/habit_assistant/core/undo_ui.py` | Modified | `send_undo_confirmation` derives `user_id` from the row internally (signature unchanged); `handle_undo_callback` gains `chat_id` as new first positional param + AC-C2 ownership check (`row["user_id"] != chat_id` → refusal, no delete) |
| `src/habit_assistant/core/targets_command.py` | Modified | `execute_target` and its private helpers take `user_id` |
| `src/habit_assistant/core/discoverability.py` | Modified | `build_habits_overview` takes `user_id`, threaded into its goal/today-phrase helpers; `build_help_text` unchanged (no DB access) |
| `src/habit_assistant/core/reminders.py` | Modified | `ReminderState.last_habit_id` becomes `dict[str, str]` (per chat id); adds `effective_quiet_windows`, `effective_reminder_times`, `run_due_reminders` (the new minutely tick); `send_reminder` gains `chat_id`; `schedule_reminders` removed entirely |
| `src/habit_assistant/core/health.py` | Modified | `HealthMonitor.__init__` gains `owner_chat_id` as 3rd positional param; alerts always addressed to the owner |
| `src/habit_assistant/core/i18n.py` | Modified | `resolve_reply_language`/`resolve_unprompted_language` gain an optional `user_pref="auto"` param (no-op by default); 3 empty key-block skeleton sections added to `CATALOG` for `access`/`preferences`/`schedules` |
| `src/habit_assistant/core/commands.py` | Modified | `CommandKind` gains 8 literals (`start`/`approve`/`block`/`users`/`invite`/`lang`/`quiet`/`remind`); `Command` gains `target_chat`/`pref_value`/`times` fields; `dispatch()` body unchanged (no new kinds produced yet) |
| `src/habit_assistant/main.py` | Modified | `handle_inbound_message` gains required `user_id` kwarg; `on_message`/`on_callback` closures gain `chat_id` first; `--seed`/`--dry-run`/main path call `db.attribute_legacy_to_owner(secrets.telegram_chat_id)`; single `run_due_reminders` tick job (`CronTrigger(second=0)`, `coalesce=True`, `max_instances=1`) replaces `schedule_reminders`; `weekly_review_job`/`daily_summary_job` fan out over `db.active_user_ids()`, skipping a user with no data in the window |
| `config.toml` | Modified | Documentation header only (multi-user model, `/lang`/`/quiet`/`/remind`) — no schema/value changes |

Test files fixed as part of this pass (production-adjacent, mine to own per the dispatch): `tests/test_migrations.py`, `tests/test_reminders.py`, `tests/test_adaptive_reminders.py`, `tests/test_v09_gaps.py`, `tests/test_multi_habit_integration.py`, `tests/test_streaks.py`, `tests/test_cli.py`, `tests/test_v11_integration.py`.

## How it works

Every row that belongs to a specific person now carries a `user_id` (the Telegram chat id string) — `logs.user_id`, `habit_targets.user_id`, `user_reminder_times.user_id` — and every DB accessor that reads or writes one of those tables takes `user_id` as an explicit parameter (first for db-layer methods, last for core-layer functions after `lang` when present), so isolation is structural, not conventional. On startup, `attribute_legacy_to_owner(owner_chat_id)` upserts the owner as `active` and backfills any pre-v1.2 `NULL` `user_id` rows to them, idempotently, so the owner's pre-upgrade history becomes theirs and nothing else changes (AC-M3). Inbound messages and callback taps now carry the acting chat id end-to-end: `Channel.run` extracts it from the Telegram update and passes it into `on_message(chat_id, text)` / `on_callback(chat_id, data, source_text, callback_id)`, which thread it into `handle_inbound_message(..., user_id=chat_id)` and `undo_ui.handle_undo_callback(chat_id, ...)` respectively — the latter refuses (no delete, "already undone" reply) if the tapped row's `user_id` doesn't match the tapping `chat_id` (AC-C2). Reminders moved from one APScheduler cron job per configured time to a single minutely tick (`run_due_reminders`, `CronTrigger(second=0)`): each tick, for every active user × every habit, it resolves that user's effective reminder times (`effective_reminder_times` — a per-user override in `user_reminder_times`, falling back to the habit's config default) and sends via `send_reminder`, which independently re-applies that user's quiet-hours and goal-met suppression before actually sending — so a custom-time reminder is suppressed under the exact same rules as a config-time one, and one user's DB hiccup or goal-met state never affects another's reminder in the same tick. Daily-summary and weekly-review stay on their global cron times but fan out over `db.active_user_ids()`, skipping anyone with no data in the relevant window.

## Smoke test done

Full production-only pytest run (no mocks below the Telegram/Ollama boundary), always against `tmp_path`-only SQLite, never `data/habits.db`:
- My own reserved files: `tests/test_migrations.py`, `tests/test_reminders.py`, `tests/test_adaptive_reminders.py`, `tests/test_v09_gaps.py`, `tests/test_multi_habit_integration.py`, `tests/test_streaks.py`, `tests/test_cli.py`, `tests/test_v11_integration.py` — **165/165 passed**.

Ad hoc smoke script (`smoke_v12.py`, not committed, deleted after use — matches this project's established convention, see `IMPL-v1.1-shared.md`), run via `.venv\Scripts\python.exe`, entirely against a scratch `tempfile.mkdtemp()` SQLite path — never `data/habits.db` — exercising the exact seams the three parallel modules will build on:
```
[OK] 1. attribute_legacy_to_owner: idempotent, legacy row attributed
[OK] 2. two-user isolation: targets and sums never leak across users
[OK] 3. run_due_reminders: fan-out respects per-user goal-met (sent to {'friend-chat'})
[OK] 4. undo ownership check: cross-user tap refused, row untouched (sent 34 chars)
[OK] 4b. same-user undo succeeds normally

ALL SMOKE CHECKS PASSED
```
This covers: idempotent owner attribution of legacy rows (AC-M2), two-user isolation on `effective_goal`/`sum_value` (AC-U-ISO/AC-U1), the minutely tick correctly skipping a goal-met user while still reminding another (AC-U5), and the undo-callback ownership refusal + same-owner success path (AC-C2).

## Maps to acceptance criteria

Shared-surface/integration ACs (17 total, per SPEC-v1.2.md §11):

- **AC-M1** (migration 006, idempotent) → `storage/migrations.py:_migration_006_multiuser`; `tests/test_migrations.py`.
- **AC-M2** (owner attribution, idempotent) → `storage/db.py:Database.attribute_legacy_to_owner`, called from `main.py:async_main`; `tests/test_migrations.py`; smoke check #1.
- **AC-M3** (owner byte-identical, full v1.1 suite green) → structural: every scoped call now requires `user_id` but the owner's own behavior is unchanged when they're the only/first user. Verification is the full suite passing — see below.
- **AC-C1** (per-chat delivery + stored ownership) → `channels/*` (chat-id-first sends), `main.py:on_message`, `storage/models.py:LogEntry.user_id`; `tests/test_v11_integration.py`, `tests/test_multi_habit_integration.py`.
- **AC-C2** (undo ownership check) → `core/undo_ui.py:handle_undo_callback`; smoke check #4/#4b; `tests/test_undo_ui.py` (owned by the parallel undo/targets/discoverability track — see below).
- **AC-U-ISO** (isolation invariant) → every `storage/db.py` scoped method filters `WHERE user_id = ?`; smoke check #2.
- **AC-U1** (per-user targets) → `core/targets.py:effective_goal`, `storage/db.py:get_target/set_target`; smoke check #2; `tests/test_core_targets.py` (parallel track).
- **AC-U2** (per-user streaks/milestones) → `core/streaks.py`; `tests/test_streaks.py` (39/39).
- **AC-U3** (daily summary fan-out, skip if no data) → `main.py:daily_summary_job`; `tests/test_streaks.py`'s async_main section.
- **AC-U4** (weekly review fan-out, skip if no data) → `main.py:weekly_review_job`; `tests/test_review.py`/`tests/test_charts.py` (parallel-fork-verified, 136/136).
- **AC-U5** (per-user reminder goal-met skip) → `core/reminders.py:run_due_reminders`/`send_reminder`; smoke check #3; `tests/test_v09_gaps.py`.
- **AC-U-SNOOZE** (per-user snooze target) → `core/reminders.py:ReminderState.last_habit_id` (dict keyed by chat id); `tests/test_v09_gaps.py::test_snooze_targets_most_recently_fired_reminder_not_most_recently_logged_habit`.
- **AC-S1** (single tick, owner unchanged) → `core/reminders.py:run_due_reminders` + `main.py`'s single `CronTrigger(second=0)` job; `tests/test_multi_habit_integration.py`, `tests/test_reminders.py`.
- **AC-S4** (no restart on `/remind` change) → mechanism only: `effective_reminder_times` reads `user_reminder_times` live every tick, so there is no per-time job to rebuild — but the `/remind` *write* path itself belongs to the `schedules` module, not yet landed. Read-side ready; full AC needs `schedules` integration.
- **AC-S6** (custom time still honors quiet-hours/goal-met + snooze) → `send_reminder` applies both suppressions unconditionally regardless of which resolver produced the firing time; `tests/test_v09_gaps.py::test_snoozed_followup_is_also_suppressed_when_it_lands_in_quiet_hours`.
- **AC-O1** (health alerts owner-only) → `core/health.py:HealthMonitor.__init__(..., owner_chat_id, ...)`; covered by `tests/test_resilience.py` (parallel track).
- **AC-X1** (sequential inbound processing) → unchanged from v1.1: `TelegramChannel.run`'s poll loop awaits each `on_message`/`on_callback` in turn, no `asyncio.gather` introduced; no dedicated new test needed (structural, verified by reading `channels/telegram.py:run`).

Not owned by this pass (explicitly deferred to the three parallel modules per SPEC-v1.2.md §11): AC-A1–AC-A7 (`access`), AC-P1–AC-P2 (`preferences`), AC-S2/AC-S3/AC-S5 (`schedules`). This pass built only the skeletons they build on: `CommandKind`/`Command` field additions in `core/commands.py`, and 3 empty i18n key-block sections in `core/i18n.py`.

## Known limitations

- **AC-S4 is read-side only.** The tick already re-reads `user_reminder_times` fresh every minute, so no restart/rebuild will ever be needed once `/remind` can write to it — but until the `schedules` module lands, there's no way to populate that table in production, so the AC can't be fully demonstrated yet.
- **The access gate is not wired into `on_message`.** SPEC-v1.2.md's integration-order step 1 explicitly defers this to after all three parallel modules land; today, `on_message` calls `handle_inbound_message` directly with no pending/blocked check. This is intentional scope, not an oversight — flagged in `main.py`'s own comment at the call site.
- **`resolve_reply_language`/`resolve_unprompted_language`'s new `user_pref` param is unused by every call site in this pass** (always defaults to `"auto"`, a complete no-op) — the actual "look up this user's stored language and pass it in" wiring is `preferences`' job, not shared-surface's. This keeps AC-M3 trivially true for language resolution.
- **Process observation (not a defect):** during this session, background forks dispatched with `Agent(subagent_type="fork")` and no `isolation: "worktree"` occasionally edited test files outside their explicitly assigned scope (confirmed via a peer-to-peer identity mix-up mid-session, resolved by direct correspondence). No incorrect code landed as a result — every case was caught by re-reading + re-running pytest before proceeding — but future dispatches of this kind should consider `isolation: "worktree"` per-fork if strict file-ownership boundaries matter.

## Iteration log

No Vera round yet — this is the initial hand-off. Two test files needed a from-scratch rewrite rather than a mechanical signature patch, because their core testing strategy assumed a removed API:

- **`tests/test_v09_gaps.py`**: previously drove reminders through a real per-habit-time APScheduler job fetched by id (`scheduler.get_job("reminder_water_08:00")` → `job.func(*job.args)`) — that job no longer exists (`schedule_reminders` removed, R-S1). Rewrote every such test to call `run_due_reminders(channel, config, registry, db, state, clock=<fixed to the habit's HH:MM>)` directly, preserving each AC9.x assertion's semantic intent (goal-met skip, quiet-hours suppression incl. midnight-crossing, snooze one-shot targeting, fail-open on DB error, and — new for v1.2 — a DB failure evaluating one habit not blocking another habit due in the same tick, the v1.2-shape equivalent of the old "scheduler keeps processing other jobs" claim now that there's one tick instead of one job per habit-time).
- **`tests/test_multi_habit_integration.py`**: same `schedule_reminders`-registration test replaced with an equivalent `run_due_reminders`-fires-at-the-right-time test for both config-only added habits (`sleep`, `meds`); migration assertion bumped from schema version 5 to 6 with an added `attribute_legacy_to_owner` step before the aggregation check (mirroring real `async_main` startup order).

Every other file (`test_migrations.py`, `test_reminders.py`, `test_adaptive_reminders.py`, `test_streaks.py`, `test_cli.py`, `test_v11_integration.py`) needed mechanical signature/fixture updates only: `user_id` threaded through calls, `LogEntry`'s new positional field, `Channel` fakes widened to `chat_id`-first `send`/`send_actionable` and 2-arg `run`, and `_FakeScheduler.add_job` widened to accept the new `coalesce`/`max_instances`/`misfire_grace_time` kwargs `main.py` now passes.

A parallel track (a dispatched fork, `test_undo_ui/discoverability/targets/resilience/channels/v11_shared_surface/fallback/core_targets/target_nl.py` — 9 files, ~380 tests) worked concurrently on the `undo_ui`/`targets`/`discoverability` module's own pre-existing test suite plus the shared-surface's `test_v11_shared_surface.py`. It landed 7 of 9 files clean; on its final report, 4 files still had 20 failing tests (all the same class of mechanical gap: `user_id`/`chat_id` not yet threaded through a handful of call sites its earlier passes had missed) in `test_core_targets.py` (5), `test_fallback.py` (6), `test_target_nl.py` (4), `test_v11_shared_surface.py` (5). I finished these 20 myself rather than round-tripping again: `Database.set_target`/`get_target`/`clear_target`/`all_targets` calls missing the new leading `user_id` arg, `effective_goal`/`render_habit_chart`/`execute_target` calls missing their trailing `user_id`, `handle_inbound_message` calls missing `user_id=`, and two `Channel` fakes (`_RecordingChannel` in `test_fallback.py`) still on the pre-v1.2 `send(text)`/`send_actionable(text, buttons)` shape. Verified via `pytest -q` on the full suite after: 0 failures.

## Full suite status

**AC-M3 hard gate: PASS.** Full suite: **976 passed, 1 skipped, 0 failed** (`pytest -q`, ~94s). The 1 skip (`tests/test_channels.py:232`, "only core/ wires the Channel ABC directly") is a pre-existing conditional architectural-boundary check, unrelated to this pass — matches the v1.1 baseline's own skip count.

No `data/habits.db` or the live Task Scheduler service was ever touched — every test and the standalone smoke script ran against `tmp_path`/`tempfile.mkdtemp()`-only SQLite files.
