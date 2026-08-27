# Spec — Behavior-Preserving Refactor (performance + structure)

> Target: habit-assistant @ v1.9.0 · Mode: **behavior-preserving** (zero feature / zero schema / zero user-visible output change) · Gate: **byte-identical outputs** + full suite green (4256/0/1/1xf).

## 1. Problem statement

Nineteen releases of accreted code have left three structural pressure points and several measurable performance inefficiencies, all inside a codebase whose ~4256-test suite is a reliable safety net. This refactor pays down that debt **without changing any observable behavior**: no new features, no schema/migration changes, no change to any string the bot emits or any DB row it writes. Success = the same 4256 tests stay green, byte-identical-output ACs hold, and the measured hot-path costs drop by the floors stated in §8 with no regression anywhere. The work is decomposed into **four independently-shippable, individually-gated stages** (§11) so each can land as its own release and roll back cleanly.

Two audit dimensions were measured (not guessed) against a scratch DB seeded with 3 active users × ~1 year of logs (6570 rows). The measured baseline is embedded in §8 and is the comparison point for every performance AC.

## 2. Inputs

This is a refactor spec — the "inputs" are the current code artifacts and the measured baseline the refactor must preserve/improve:

- **Source tree** (`src/habit_assistant/`): `main.py` (~2505 lines), `core/commands.py` (~2510 lines), `core/i18n.py` (~2101 lines, 387 catalog keys), `storage/db.py` (~1019 lines, 66 methods), + ~40 `core/` modules, `channels/`, `storage/`, `llm/`. (Line counts are AST-verified; a plain newline count undercounts due to line endings.)
- **Schema**: SQLite WAL, `PRAGMA user_version` migrations 001–012 (`storage/migrations.py`). Existing indices: `idx_logs_ts_cat`, `idx_logs_category`, `idx_logs_deleted_at`, `idx_logs_user(user_id,category,ts)`, `idx_audit_ts`, `idx_audit_user(user_id,ts)`, `idx_pauses_user(user_id,habit_id)`; composite PKs cover the small per-user tables.
- **Measured baseline** (§8) — the numbers every performance AC compares against.
- **Test suite**: `tests/*.py`, 2474 test functions / 4256 collected cases, run **sequentially** (no pytest-xdist), ~190s.

## 3. Outputs

- **`SPEC-REFACTOR.md`** (this file).
- Per stage: an `IMPL-refactor-sN.md` (Luna), a `TEST-refactor-sN.md` (Vera), a release commit + tag, and updated `PROGRESS.md`.
- **No change** to: any i18n catalog string emitted, any `logs`/`audit_log`/settings row written, any migration, any config key's meaning, any command grammar, any PNG output. These are the invariants; a diff to any of them is a FAIL.

## 4. Behavior rules (findings the refactor acts on)

Each rule is an evidence-backed finding with a behavior-preserving remedy. Citations are `file:line`.

### Performance

1. **Idle reminder tick is an N+1 that fires every minute, all day.** `core/reminders.run_due_reminders` (`reminders.py:348-391`) runs the *full* per-user × per-habit fan-out on every minute even when nothing is due: `db.active_user_ids()` (1) + per user `get_user` for language (`_user_language_pref`, U) + per user per habit `db.get_reminder_times` (U×H). **Measured: 13 queries/tick** (formula `1 + U×(1+H)`; U=3, H=3) → **18,720 queries/day** for idle ticks alone. Remedies (all byte-identical): (a) bulk-read `user_reminder_times` once per tick into a dict and resolve `effective_reminder_times` in memory (faithfully reproducing the `no-rows→config`, `["off"]→[]` fallback of `reminders.py:211-226`); (b) defer the language `get_user` until a reminder actually fires (language is only consumed inside `send_reminder`, so resolving it lazily changes nothing observable). Floor: idle-tick queries ≤ 3.

2. **Three separate minutely jobs each re-fetch `active_user_ids()`.** `main.py:1953/1976/1993` register `reminder_tick`, `checkin_tick`, `nudge_tick` as three distinct `CronTrigger(second=0)` jobs. `checkin`/`nudge` correctly short-circuit on their time guard **before** any DB work (measured: 0 queries off-hour/off-minute — `checkins.py:266`, `nudge.py:213`), but each still calls `active_user_ids()` when it does run. Consolidating the three into one minutely tick that fetches the active-user list once removes 2 redundant `active_user_ids()` calls/minute. Byte-identical: the three fan-outs are independent and order-free.

3. **`ts LIKE '{day}%'` cannot use the ts index; a range bound can (14× faster).** `db.sum_value`/`count`/`count_true` (`db.py:68-98`) filter `ts LIKE ?`. SQLite's default case-insensitive `LIKE` disables the index range-scan; `EXPLAIN QUERY PLAN` confirms the query seeks `idx_logs_user` on `(user_id, category)` then **scans every row** of that partition applying `LIKE`. **Measured: 0.195 ms (LIKE) vs 0.0135 ms (range-bound) — 14×** over 1yr of water rows. Remedy: replace `ts LIKE 'YYYY-MM-DD%'` with `ts >= 'YYYY-MM-DD' AND ts < '<next-day>'`. **Proven byte-identical** across all boundaries (midnight `T00:00:00`, `T23:59:59`, next-day exclusion, soft-deleted exclusion — the real ts format uses the `T` separator, and `'T' > ' '` keeps the range correct). `<next-day>` = `date.fromisoformat(day)+timedelta(days=1)` (rolls over month/year correctly). This aggregation is on the hot path of every confirmation, check-in, nudge, summary, and review.

4. **WAL runs with `synchronous=FULL` (default) — every commit fsyncs (4.7× write cost).** `db.py:37` sets `journal_mode=WAL` but never sets `synchronous`. Every `insert_log`/`insert_audit`/setter does `self._conn.commit()` which fsyncs. **Measured: 1.45 ms/write (FULL) vs 0.31 ms/write (NORMAL) — 4.7×.** Remedy: `PRAGMA synchronous=NORMAL` (the standard WAL recommendation). Behavior-preserving for all observable output; the only semantic change is durability on an OS crash / power loss (the last few committed transactions since the last WAL checkpoint could roll back — **no corruption**). See §9 for the user-awareness flag.

5. **The message pipeline dispatches twice per message.** `on_message` calls `commands.dispatch(text, registry)` (`main.py:2303`) to intercept admin/audit kinds, then `handle_inbound_message` calls `commands.dispatch(text, registry)` **again** (`main.py:833`). `dispatch` is a pure function of `(text, registry)` — **measured 58 µs** for a normal log falling through all ~26 matchers, run twice = ~116 µs/message, half pure waste, plus a correctness-coupling hazard (the two calls must never disagree — the very risk `main.py:2296-2301` comments about). Remedy: dispatch once in `on_message`, thread the resulting `Command` into `handle_inbound_message` (keep an internal fallback dispatch for the CLI/`--dry-run`/direct-call/reparse callers that pass no command). Provably identical because classification is pure.

6. **`get_user` is read redundantly per message and per send.** A typed log resolves language via `_stored_language_pref`→`get_user` in `handle_inbound_message` (`main.py:832`) while `on_message` already read `get_user` in `access.handle_gate`; a firing reminder reads `get_user` for language (`reminders.py:386`) and again for quiet windows (`reminders.py:302`→`effective_quiet_windows`→`get_user`). **Measured typed-log pipeline: 33 queries** (`handle_inbound_message` alone; +2 for the `on_message` gate in production). Remedy: fetch the user row once per request and thread it (or the resolved language) through. Lower priority than 1/3/4; behavior-preserving.

7. **`is_paused` re-fetches the whole pause set per habit.** `pause.is_paused` (`pause.py:84-98`) calls `db.active_pauses(user_id)` on every call; `build_checkin_message`/`build_nudge_message` call it once per habit (`checkins.py:174`, `nudge.py:117`) → H `active_pauses` reads per user per build. Remedy: fetch `active_pauses(user_id)` once per user and reuse across habits. Byte-identical.

8. **Non-hotspots (verified, leave alone):** `i18n.t` is an O(1) `CATALOG[msg_id][lang]` dict lookup + `str.format()` (`i18n.py:109-119`) — already optimal. `matplotlib`/`numpy` imports are **already lazy** (inside functions: `charts.py:29`, `heatmap.py:60`, `wrapped.py:83`, `fonts.py:83`) — no startup cost. `RegistryProvider.for_user` cache is a plain dict, O(1) warm, **measured 1 query cold / 0 warm** (`registry_provider.py:57-72`) — healthy. The `channels.base.Channel` ABC (`channels/base.py`) is a clean, well-degraded contract — healthy. Do not touch these.

### Structure

9. **`main.py` (~2505 lines) mixes five responsibilities and is safely splittable.** Responsibilities: bootstrap/wiring + 8 scheduler-job definitions (`async_main`, `main.py:1625-2453`), inbound routing + the ~830-line command dispatcher (`handle_inbound_message`, `651-1481`), command executors (`_execute_undo/_edit/_snooze/_generic_confirmation`, `323-596`), the callback router (`on_callback`, `2365-2438`), and the CLI entry (`main`/`build_arg_parser`, `2456-2536`). **Import-cycle check: no module in `src/` imports `main`** (grep: zero matches) — the split is cycle-safe by construction; the pieces only import downward into `core`/`storage`/`channels`/`llm`. Proposed split in §5.

10. **The `handle_inbound_message` dispatch region is ~18 repetitions of one scaffold.** `main.py:855-1174` repeats `reply = await <module>.execute_<kind>(...); if dry_run: print; return; assert channel; await channel.send(user_id, reply); [dashboard.refresh]; return` for ~18 kinds, with per-arm variations (some `send_actionable`, some send only when reply non-empty, some refresh the board). Collapsible to a handler table (§11 Stage 3) with a per-kind adapter preserving each arm's exact side effects.

11. **`quicklog.py` carries a byte-identical mirror of `main.py`'s confirmation branches** (deliberate, to avoid a `main→quicklog→main` import cycle — documented at `quicklog.py:16-25`). Mirror pairs: `_generic_confirmation` (`main.py:535-596` ↔ `quicklog.py:309-342`) and the milestone/record/burst/water/stretch/generic send block (`main.py:1353-1478` ↔ `quicklog.py:345-437`). Real fix: extract a cycle-free leaf `core/confirmation.py` both import (§11 Stage 2).

12. **Duplication census — 12 clusters** (full evidence from the parallel audit):
    - **EASY (identical bodies, low risk):** (a) language-pref inline copies still un-consolidated in `announce.py:33-46`, `checkins.py:222-231`, `dashboard.py:181-190`, `nudge.py:159-168` (4 siblings; `user_prefs.stored_language_pref` is canonical); (b) `_today*` timezone-date helper duplicated across 8 sites (`records.py:81`, `trends.py:55`, `dashboard.py:161`, `checkins.py:138`, `nudge.py:77`, `heatmap.py:98`, `query.py:161`, `wrapped.py:136`) + `_now_hhmm` in `reminders/checkins/nudge`; (c) `ordinal`/`_ordinal` (`main.py:283` ↔ `quicklog.py:58`); (d) registry-anchored Thai-alias pattern builder, 7 sites in `commands.py` (`537/743/955/1023/1352/1543/1864`); (e) `_week_days`/`week_day_strs`, 4 sites (`charts.py:59`, `garmin.py:50`, `review.py:90`, `records.py:90`).
    - **MEDIUM:** render-budget drop-loop reimplemented in `routines._render_list` (`routines.py:266-280`, needs to drop a parallel button list — generalize `render_budget.fit_within_budget`); test doubles — **82 channel / 35 LLM / 29 scheduler+db** fake classes across the suite with **no shared conftest** (a `RecordingChannel`/`FakeOllamaClient`/`FakeScheduler` trio would remove >100 definitions).
    - **RISKY (structural, dedicated-extraction only):** the dispatch scaffold (rule 10) and the confirmation mirror (rule 11).

13. **`storage/db.py` is a 66-method god-object** (`db.py`). Behavior-preserving repository-splitting (LogsRepo/UsersRepo/HabitsRepo/LifecycleRepo) would touch every caller (high surface, low behavioral payoff) — **document-and-defer**, or apply only a light in-file sectioning. Not a Stage target unless the user wants it.

14. **Dispatch chain: 27 ordered pure matchers with three table-design invariants.** `commands.dispatch` (`commands.py:2129-2343`) is an ordered `if _match_X(): return Command(...)` chain over `(text, registry)` with **no DB access, no mutation, and no shared/module-level mutable state** (AST-verified: no `global`, no cache, every `.add()`/`.append()` targets a fresh local). Order (27 branches): undo → edit → snooze → target → remind → access → audit → lang → quiet → checkin → dnd → dashboard → history → heatmap → records → trends → wrapped → addhabit → delhabit → log → routine → **cadence** → pause → resume → help → habits → **query** (dnd is a pure alias emitting `kind="quiet"`; corrected 2026-08-27 per Vera's TEST-refactor-s3.md finding — the prior draft of this list omitted `pause`/`resume`, added in v1.9.0, and misplaced `query` before `help`/`habits` instead of last, contradicting invariant (iii) below; this is now the verified ground-truth order, confirmed against the actual pre-Stage-3 `commands.py` if-chain). Invariants a precedence-preserving table **must** encode: (i) **cadence MUST precede query** (`commands.py:2306-2314` and `1788-1799` — `กี่ครั้งต่อสัปดาห์` contains the query anchor `กี่`); (ii) `edit` is **commit-on-trigger** — if `_EDIT_TRIGGER` matches but the tail fails `_parse_edit_value`, dispatch `return None` **terminally** (`commands.py:2166-2172`), skipping matchers #3–#27 (observable: `"change it to what?"` matches edit, fails parse, returns `None` without reaching query despite the trailing `?`) — every other matcher is pure fall-through, so the table needs a per-row "commit" flag for edit only; (iii) `_match_query` is the **only substring/`.search` matcher** (all others are whole-message-anchored), so it must stay **last**. Everything else is documented disjoint-trigger (placement-independent). Reorderable into a data table when these three invariants hold — provable via a golden precedence corpus. (Minor perf note: the registry-anchored `_build_*_th_pattern` helpers recompile their regex on every call — a table can hoist/cache per-registry without behavior change.)

15. **Dead code — the sweep came back nearly clean (a good sign, small Stage-4 surface).** A full AST + reference-count sweep (all 370 `core/` functions/methods, all 387 catalog keys, imports in the four large files) found: (a) **exactly 3 dead i18n keys** — `dashboard_line_goal_weeks`/`_boolean_weeks`/`_count_weeks` (`i18n.py:2037/2041/2045`), referenced nowhere (the cadence branch renders via `cadence.cadence_status_line`); (b) **one unused import** — `BUILTIN_IDS` in `main.py:66` (the other three names on that line are used); (c) **zero dead functions/methods** (the only zero-reference symbols, `HabitRegistry.__iter__`/`__len__`, are live via implicit `for`/`len`); (d) **no other dead keys**. Several `commands.py` "never actually hit in practice" branches (`1908-1910`, `1873-1883`, remind's Thai fallback) are documented defensive guards — **keep them, not dead code**. Net Stage-4 removal surface: 3 keys + 1 import.

16. **Test-suite health:** runs **sequentially** (no pytest-xdist; `pyproject.toml` `[tool.pytest.ini_options]` has only `testpaths`), ~190s. The "parallel-edit flake" is a **dev-workflow race** (concurrent Luna agents running pytest on the same tree), not a suite-ordering bug — but **4 files write to a fixed shared `Temp` path** instead of pytest's `tmp_path`: `test_announce.py`, `test_discoverability.py`, `test_heatmap_gaps.py`, `test_v07_m3_review_extra.py` — the actual race surface. Converting these to `tmp_path` removes it (and is a prerequisite before any future xdist enablement).

## 5. Interfaces (signatures / structural targets)

**Stage 1 (DB + tick):**
```python
# storage/db.py __init__, after journal_mode=WAL:
self._conn.execute("PRAGMA synchronous=NORMAL;")
self._conn.execute("PRAGMA busy_timeout=5000;")

# storage/db.py — day-scoped aggregations switch LIKE -> range:
def _day_bounds(day: str) -> tuple[str, str]:
    from datetime import date, timedelta
    return day, (date.fromisoformat(day) + timedelta(days=1)).isoformat()
# sum_value/count/count_true: "... AND ts >= ? AND ts < ?" using _day_bounds(day)

# core/reminders.run_due_reminders — bulk reminder-times read + lazy language:
# one SELECT user_id, habit_id, time FROM user_reminder_times per tick -> dict;
# resolve language only inside the `if current_hhmm in times:` branch.
```

**Stage 2 (main.py decomposition — proposed module layout):**
```
main.py            # thin: setup_logging, build_arg_parser, main() entry only
core/app.py        # async_main: config/secrets/db/llm/channel/provider wiring + job registration
core/jobs.py       # the 8 scheduler job bodies (weekly_review/daily_summary/grace_tick/... )
core/routing.py    # on_message, on_callback, handle_inbound_message (dispatch arms)
core/confirmation.py  # LEAF: _generic_confirmation + water/stretch/diary/generic + suffix formatter
                      # imported by BOTH core/routing.py and core/quicklog.py (kills the mirror)
```
`handle_inbound_message` gains an optional `command: Command | None = None` param (dispatch once upstream, pass it in; fall back to `dispatch(text, registry)` when `None`).

**Stage 3 (dispatch table):**
```python
# core/commands.py — ordered table preserving EXACT precedence + edit early-stop:
_MATCHERS: list[Matcher] = [ ... ]   # same order as the current if-chain
def dispatch(text, registry) -> Command | None: ...  # walks the table, models edit's stop-on-reject
```

## 6. Files to touch (by stage)

- **Stage 1:** `storage/db.py` (pragmas + range-bound aggregations), `core/reminders.py` (tick batching + lazy language), `core/checkins.py`/`core/nudge.py` (reuse one `active_pauses` per user), `src/.../main.py` (consolidate 3 tick jobs into 1).
- **Stage 2:** `main.py` → new `core/app.py`, `core/jobs.py`, `core/routing.py`, `core/confirmation.py`; `core/quicklog.py` (import the leaf, drop the mirror).
- **Stage 3:** `core/commands.py` (matcher table), `core/routing.py` (handler table), `core/user_prefs.py` consumers (`announce/checkins/dashboard/nudge`), new `core/timeutil.py` (+ `_today*`/`_now_hhmm`/`week_days` consumers), `core/commands.py` (shared Thai-alias helper).
- **Stage 4:** `core/i18n.py` (drop 3 dead keys), `main.py` (drop unused `BUILTIN_IDS` import, line 66), `tests/conftest.py` (shared doubles), the 4 fixed-path test files (`test_announce.py`, `test_discoverability.py`, `test_heatmap_gaps.py`, `test_v07_m3_review_extra.py`).

## 7. External dependencies

None added. Optional Stage-4 dev-only: `pytest-xdist` (if the user later wants parallel test runtime — **gated behind** the `tmp_path` fixes; not required by this spec).

## 8. Acceptance criteria

**Cross-cutting (every stage):**
- **AC-G1** (byte-identical gate): the full existing suite (4256 passed / 0 failed / 1 skipped / 1 xfailed) stays green after each stage, unmodified except where a test asserts on an internal structure that legitimately moved (e.g. an import path in Stage 2) — no test asserting on an **emitted string, DB row, or PNG** may change.
- **AC-G2**: a byte-identical output probe (a fixed corpus of inbound messages → captured `channel.send*` payloads, and a fixed set of tick fan-outs → captured sends) produces identical bytes before and after the stage.

**Stage 1 (DB + tick):**
- **AC1**: Given the seeded 3-user/1-year DB, When the idle reminder tick runs (no reminder due), Then it issues **≤ 3 DB queries** (baseline 13) with identical (empty) sends.
- **AC2**: Given a `500ml` typed log, When `sum_value`/`count`/`count_true` run, Then results are **byte-identical** to the `LIKE` implementation across day boundaries (midnight, 23:59:59, next-day, soft-deleted) — proven by a boundary corpus test.
- **AC3**: Given `synchronous=NORMAL`, When 300 `insert_log` calls run, Then per-write time is **≥ 3× faster** than the FULL baseline (1.45 ms → ≤ 0.5 ms) with identical rows written.
- **AC4**: Given the three minutely jobs consolidated, When a tick fires, Then `active_user_ids()` is called **once** (not 3×) and every reminder/checkin/nudge send is byte-identical to baseline.
- **AC5**: Given check-ins enabled for a user with H habits, When `build_checkin_message` runs, Then `active_pauses(user_id)` is read **once** (baseline H) with identical message output.

**Stage 2 (decomposition):**
- **AC6**: `main.py` is reduced to a thin entry (< 150 lines); `handle_inbound_message`, `async_main`, jobs, and the confirmation formatter live in their own modules — with **no import cycle** (verified by a static cycle check).
- **AC7**: Given any inbound message, When routed, Then `commands.dispatch` is invoked **once** per message (baseline 2), with byte-identical routing.
- **AC8**: `core/quicklog.py` imports the confirmation formatter from `core/confirmation.py` — the byte-mirror is deleted, and quicklog's existing byte-identical confirmation tests still pass.

**Stage 3 (dispatch table + EASY dedup):**
- **AC9**: Given a golden precedence corpus (≥ 40 cases exercising all three invariants of rule 14 — cadence-vs-query `กี่ครั้งต่อสัปดาห์`, the `edit` commit-on-trigger `"change it to what?"`→`None` despite trailing `?`, query-must-stay-last, plus `audit ประวัติ` vs `history ย้อนหลัง` and every `_match_*` boundary), When dispatched through the table, Then every case yields the **identical `Command | None`** the current if-chain yields.
- **AC10**: The 4 remaining language-pref copies, the 8 `_today*` sites, `ordinal`/`_ordinal`, the 7 Thai-alias builders, and the 4 `week_days` sites are consolidated — each producing **byte-identical** output (regression corpus per cluster).

**Stage 4 (dead code + test health):**
- **AC11**: The 3 dead `dashboard_line_*_weeks` keys and the unused `BUILTIN_IDS` import (`main.py:66`) are removed (the sweep confirmed these are the *only* removable dead symbols — no dead functions/methods, no other dead keys); `tests/test_i18n_literals.py`-style catalog tests still pass.
- **AC12**: `tests/conftest.py` provides shared `RecordingChannel`/`FakeOllamaClient`/`FakeScheduler`; migrated tests still assert on identical surfaces.
- **AC13**: The 4 fixed-`Temp`-path test files use `tmp_path`; running the suite twice concurrently no longer races on a shared file.

> Coverage: every §4 rule maps to ≥ 1 AC — rules 1/2/7→AC1,AC5; 3→AC2; 4→AC3; 5→AC7; 6→(AC-G2, no isolated AC, covered by pipeline byte-identity); 8→(no-op, verified untouched); 9→AC6; 10→AC10; 11→AC8; 12→AC10,AC12; 13→(deferred, §9); 14→AC9; 15→AC11; 16→AC13.

## 9. Risks & open questions

- **OQ1 — `synchronous=NORMAL` durability trade-off (needs user awareness; default: apply).** Relaxes durability on OS crash / power loss (last few committed transactions since the last WAL checkpoint may roll back; **no corruption, no output change**). Standard for WAL and universally fine for a personal habit tracker. *Decision needed from:* user. *Default if no answer:* apply it (it is the single largest write win, 4.7×). If the user objects, drop AC3 from Stage 1; the rest of Stage 1 stands.
- **OQ2 — Release cadence: 4 staged PATCHes vs 1 bundled PATCH (default: staged).** See §10-adjacent SemVer note below. *Decision from:* user. *Default:* four PATCH releases v1.9.1–v1.9.4 (cleaner rollback boundaries).
- **OQ3 — main.py decomposition scope (risk, not a blocker).** `on_message`/`on_callback` are closures capturing ~10 locals (`db`, `llm`, `channel`, `config`, `secrets`, `provider`, `scheduler`, `reminder_state`, `health_monitor`). Extraction must thread these as explicit params or a small context object — mechanical but broad. Cycle-safe (rule 9). *Default:* proceed; if the closure surface proves too coupled, split only the confirmation leaf (still kills the biggest duplication) and defer the routing/jobs split.
- **OQ4 — `db.py` god-object (defer).** Repository-splitting is high-surface / low-payoff under a behavior-preserving constraint. *Default:* document-and-defer (rule 13); not in any stage unless the user asks.
- **OQ5 — pytest-xdist (defer to post-Stage-4).** Enabling `-n auto` needs the `tmp_path` fixes (AC13) landed first and full test isolation verified. *Default:* out of scope; note as a follow-up.

## 10. Out of scope

- Any feature, config-key, grammar, schema, migration, or emitted-string change.
- `db.py` repository split (OQ4).
- Enabling pytest-xdist / parallel test execution (OQ5).
- Rewriting the LLM/extraction path, the channels layer (healthy — rule 8), or `i18n.t` (optimal — rule 8).
- Caching/altering `RegistryProvider` semantics beyond what it already does (healthy — rule 8).
- The MEDIUM/RISKY test-double consolidation beyond the three shared doubles named in AC12 (the exotic scripted/raising variants stay per-file).

## 11. Module split & parallel development

**Total functionals:** 4 (one per stage — each a distinct, independently-shippable optimization workstream).

**SemVer treatment (recommended):** **one PATCH per stage → v1.9.1, v1.9.2, v1.9.3, v1.9.4.** Rationale: the whole initiative is behavior-preserving — no feature, no API/schema/CLI change — so by SemVer every stage is a PATCH, and a MINOR would misrepresent it. Each stage is independently gated (AC-G1/AC-G2 + its own ACs) and independently shippable, which is exactly the project's "one verified change = one release" discipline. Alternative (if the user prefers fewer releases): bundle all four into a single **v1.9.1**; the staging still governs build order internally. **Default: staged PATCHes** for clean rollback boundaries.

**Recommendation:** **SEQUENTIAL across stages** — the stages form a real dependency chain and share hot files:
- Stage 2 (main.py decomposition) is far safer once Stage 1's DB layer is stable, and it *creates* the `core/routing.py` that Stage 3's handler-table edits.
- Stage 3's dispatch table is cleaner after Stage 2 has separated routing from wiring.
- Stage 4 (dead code / test health) goes last so it sweeps against the final structure.
- Byte-identical gating means each stage must be **fully green before the next starts** — parallelizing stages would fight over `db.py`/`main.py`/`commands.py` and blur the per-stage byte-identical gate.

**Intra-stage parallelism** (only where file ownership is disjoint): within **Stage 1**, two tracks can run concurrently — Track A owns `storage/db.py` (pragmas + range-bound aggregations), Track B owns `core/reminders.py`/`checkins.py`/`nudge.py` + the `main.py` job consolidation (tick batching + `active_pauses` reuse). They touch disjoint files; each has its own Luna+Vera. Stages 2–4 are single-track (they intentionally converge on the shared files being restructured).

| Stage (track) | Owned ACs | Owned files | Depends on |
|---|---|---|---|
| **S1-A** DB layer | AC2, AC3 | `storage/db.py` | (none) |
| **S1-B** Tick fan-out | AC1, AC4, AC5 | `core/reminders.py`, `core/checkins.py`, `core/nudge.py`, `main.py` (job wiring) | reads `storage/db.py` (no edits) |
| **S2** Decomposition | AC6, AC7, AC8 | `main.py`, new `core/app.py`/`jobs.py`/`routing.py`/`confirmation.py`, `core/quicklog.py` | S1 green |
| **S3** Dispatch table + dedup | AC9, AC10 | `core/commands.py`, `core/routing.py`, `core/timeutil.py` (new), `core/{announce,checkins,dashboard,nudge}.py`, dedup consumers | S2 green |
| **S4** Dead code + test health | AC11, AC12, AC13 | `core/i18n.py`, large-file imports, `tests/conftest.py`, 4 fixed-path test files | S3 green |

**Shared surface (built first, before S1's two tracks fan out):**
- `storage/db.py:_day_bounds` helper (S1-A owns it; S1-B does not touch `db.py`) — so the two S1 tracks stay file-disjoint. If the reminder bulk-read needs a new `db.py` read method, S1-A adds it first, then S1-B consumes it.

**Integration order (after each stage's tracks complete):**
- After S1: run the full suite + the AC-G2 byte-identical probe + re-run the §8 benchmark; confirm the measured floors (AC1 ≤3 queries, AC3 ≥3× writes) before tagging v1.9.1.
- After S2: static import-cycle check + dispatch-once assertion (AC7) + full suite before v1.9.2.
- After S3: golden precedence corpus (AC9) + per-cluster dedup regression corpora (AC10) before v1.9.3.
- After S4: concurrent double-suite run (AC13) + dead-code sweep clean/documented before v1.9.4.
