# Test Report — SPEC-v1.10.md "Never lose a log", Release Gate

## Summary

**Verdict: PASS. Recommend ship + announce.**

All 18 acceptance criteria are proven at the wired, end-to-end level. The zombie-loop bug that motivated this release (production rows id=13/"500", id=14/"Streaching" re-parsed forever, user never told) is fixed and independently re-proven twice — once through the existing integration suite's own stand-in, and once through this gate's own real `core/parser.py:parse_message` driven by a genuinely LLM-shaped fake. The critical row-ownership vulnerability M1's own Vera round 1 found is fixed, and independently re-confirmed present-and-correct by direct code reading (`core/clarify.py:handle_clarify_callback`). All 27 pre-existing test files (28 including `tests/conftest.py`) that this release's default-behavior changes touched were diff-audited line-by-line; none were weakened.

- M1/M2/M3/shared-surface/integration subset (`tests/test_v110_*.py` + `tests/test_clarify.py` + `tests/test_unparsed_closure.py` + `tests/test_reply_to_reminder.py` + `tests/test_outage_honesty.py` + `tests/test_guide.py` + `tests/test_pause_failopen.py`, including this gate's own new file): **391 passed, 0 failed**.
- Full repo suite, three consecutive runs, all matching: `-n auto` **4878 passed / 0 failed / 1 skipped / 1 xfailed in 77.04s**; serial **4878 / 0 / 1 / 1xf in 218.85s**; `-n auto` again **4878 / 0 / 1 / 1xf in 75.88s**. No order-dependence, no flakiness.
- Baseline reconciliation: the integration hand-off reported 4863/0/1/1xf. `4878 − 15 (this gate's own new test file) = 4863` — exact match. Nothing drifted between hand-off and this gate; the 15 new tests are pure addition.
- `data/habits.db` untouched throughout (`git status`/`git diff` show nothing under `data/`); `docs/` untouched; `VERSION`/`pyproject.toml` still read `1.9.4` (no bump — that is Archi's Phase 6.5 step, not this gate's).
- No production code was edited during this pass. Only `tests/test_v110_release_gate.py` was added.

**One process finding, non-blocking:** `TEST-v1.10-m3.md` is referenced in `PROGRESS.md`'s own session log ("M3 VERA PASS (TEST-v1.10-m3.md: ...)") but does not exist on disk — only `TEST-v1.10-m1.md`/`TEST-v1.10-m2.md` were actually written as files; M3's Vera pass appears to have happened without its own report artifact being saved. This gate does not treat it as blocking: M3's actual work product (`core/checkins.py`/`core/nudge.py`/`core/streaks.py`/`core/review.py`'s fail-open adoption, `tests/test_pause_failopen.py`'s 12 tests, `pyproject.toml`'s `pytest-xdist` dependency) was independently re-verified by this gate directly against the source and the test suite (see AC16/AC17 below) and is sound. Recommend Archi have M3's Vera write the missing report file for the historical record, but this does not gate the release.

---

## 1. 18-AC coverage map

Every AC below is backed by at least one passing test at the **wired** level (real `core/routing.py`/`core/app.py`/`core/clarify.py` closures, not a module-level restatement), independently confirmed either by this gate's own code reading or by re-running the cited tests.

| AC | What it requires | Wired-level proof | Verdict |
|---|---|---|---|
| **AC1** | Migration 013 additive, idempotent, full suite green | `storage/migrations.py:_migration_013_unparsed_state` (read directly: pure `ALTER TABLE ADD COLUMN`, no backfill, appended to `MIGRATIONS`, stamps 13 via the existing `user_version` runner — idempotent by the runner's own `from_version >= target_version` early-return); `tests/test_v110_shared_surface.py` (a real v1.9→13 upgrade with a pre-existing zombie row); full-suite green ×3 | **PASS** |
| **AC2** | `send` returns `str \| None`, byte-identical default payload | `channels/telegram.py:TelegramChannel.send` read directly — defensive extraction (documented, Archi-accepted deviation from the spec's literal unconditional cast; degrades to `None` rather than raising on a bare-shaped test double, byte-identical real payload) | **PASS** |
| **AC3** | CAS state machine: `pending_unparsed` predicate, guarded `resolve_unparsed`/`mark_unparsed_state` | `storage/db.py` read directly — `_unparsed_from_states_predicate` correctly builds a disjoint `IS NULL` vs `IN (...)` fragment per caller; both CAS methods are single atomic `UPDATE ... WHERE id=? AND category='unparsed' AND <predicate>` under SQLite's single-writer serialization; `tests/test_v110_shared_surface.py`'s CAS suite | **PASS** |
| **AC4** | `/guide`/`คู่มือ` dispatch + reserved, dispatch invariants hold | `core/commands.py` read directly (`_match_guide`, `_MATCHERS` insertion before `query`); `tests/test_v110_integration.py::test_ac15_public_and_owner_menus_are_23_and_28_and_guide_reaches_the_real_on_message` drives it through the REAL `async_main`/`on_message` | **PASS** |
| **AC5** | Zombie loop killed for "500"/"Streaching" specifically | `tests/test_v110_integration.py::test_ac5_ac6_zombie_rows_close_or_offer_then_never_reparsed_again` **+** this gate's own `tests/test_v110_release_gate.py::test_zombie_proof_real_ollama_shaped_fake_closes_and_offers_then_zero_llm_calls_on_resweep` — the latter drives the REAL `core/parser.py:parse_message` via a genuinely LLM-shaped fake (`chat_json` returning real "unknown" JSON, call-counted: 4 calls first sweep, 0 on the second — a raising-if-called double, not just an empty query), across TWO different pre-existing users, each user id-shaped like production `id=13`/`id=14` | **PASS** |
| **AC6** | Closure sent exactly once, ever, quotes raw text, `/log` keyboard | Same two tests; this gate additionally verifies each user gets **exactly one** closure message, never zero/doubled/cross-delivered | **PASS** |
| **AC7** | Tier-1 guesses deterministic, zero-LLM | `core/clarify.py:tier1_guesses` read directly; `tests/test_clarify.py` (50 tests, incl. spec's own worked examples) — re-confirmed the SHIPPED default registry's `stretch` habit has **no goal set** (`goal=None`), so a bare `"stretch"` alone correctly yields `[]` (a match with no derivable value is dropped, §2.3) — the spec's illustrative "stretch goal 30 min" worked example only holds under a `/target` override, not the raw default; this is correct code behavior, not a doc bug, but worth flagging so nobody mistakes the shipped default for matching the spec prose literally | **PASS** |
| **AC8** | Guess offer + `awaiting_clarify` state, excluded from later sweeps | `tests/test_v110_integration.py` (AC8/AC9 tests) + this gate's zombie-proof test | **PASS** |
| **AC9** | Live LLM-unknown: guesses→offer+row; no guesses→generic+`/log` keyboard, no row; `clarify.enabled=false`→always generic | `tests/test_v110_integration.py::test_ac9_*` (3 tests) + this gate's `test_inert_gate_does_NOT_extend_to_the_live_clarify_generic_question_by_spec_design` (proves the `/log` keyboard is unconditional even with `clarify.enabled=False` — see §4 below) | **PASS** |
| **AC10** | Clarify tap = ordinary log, no audit row; unknown/foreign/closed row = friendly no-op | `tests/test_v110_integration.py::test_ac8_ac10_tap_wired_through_on_callback_logs_confirms_undo_no_audit`; `core/clarify.py:handle_clarify_callback` read directly — the row-ownership pre-check (`row["user_id"] != chat_id`) from M1's round-2 fix is present and correctly placed before the habit lookup; this gate's `test_clarify_tap_after_the_guessed_habit_is_archived_mid_wait_is_a_friendly_noop` additionally proves a habit archived *between* the offer and the tap degrades safely (no crash, no force-resolve, row stays tappable) | **PASS** |
| **AC11** | Sweep-vs-tap race guard; two concurrent sweeps don't co-process a row | `tests/test_v110_integration.py::test_ac11_*`; this gate's `test_single_flight_guard_holds_under_real_concurrent_sweep_triggers` drives TWO `reparse_pending_unparsed` calls via genuine `asyncio.gather` concurrency (not a directly-called re-entrancy probe) — `parse_message` is called exactly once total, one closure ever sent | **PASS** |
| **AC12** | Reply-to-reminder logs, zero-LLM, works Ollama-down, boolean affirmative→1 | `tests/test_v110_integration.py::test_ac12_*` (both Ollama up/down) | **PASS** |
| **AC13** | Conservatism + degradation (number+unit/unmapped/checkin-nudge/post-restart fall through) | `tests/test_v110_integration.py::test_ac13_*`; this gate's `test_reply_to_reminder_number_plus_unit_falls_through_to_deterministic_preparse_zero_llm_while_down` proves a number+unit reply to a DIFFERENT habit than the reminder's own still logs correctly (via preparse, not wrongly against the reminder's habit), zero-LLM, even Ollama-down | **PASS** |
| **AC14** | Outage honesty default-on wired; `false`→byte-identical `deferred_ack` | `core/routing.py` read directly — the deferral branch sends `outage_honest_reply` with the quote capped via `render_budget.truncate(text, max_chars=_OUTAGE_QUOTE_MAX_CHARS=200)` (the integration pass's own fix for M2's Vera-flagged overflow finding — verified the cap is real, at the routing.py call site); `tests/test_v110_integration.py::test_ac14_*` | **PASS** |
| **AC15** | `/guide` bilingual dispatch; menus 23/28 both languages | `tests/test_v110_integration.py::test_ac15_*` (both EN/TH direct dispatch, and the real wired `async_main` menu registration, both languages, both public/owner scopes); this gate's `test_guide_and_help_render_within_telegram_budget` (parametrized EN+TH, both well under the 4096 budget) | **PASS** |
| **AC16** | Pause fail-open unified at all 5 sites, no fan-out abort | Read directly: `core/reminders.py` (`is_paused_safe`), `core/checkins.py`/`core/nudge.py` (`active_pauses_safe`), `core/streaks.py`/`core/review.py`×2 (`is_paused_safe`) — all 5 named sites confirmed adopted, plus the sanctioned 6th site (`core/jobs.py:wrapped_auto_job`); `tests/test_pause_failopen.py` (12 tests, all 5 sites, each with a direct fail-open assertion AND a multi-user fan-out-continues assertion); `tests/test_v19_release_gate.py`'s rewritten fail-open-posture test independently confirmed (via a dedicated audit fork) to inject a REAL `sqlite3.OperationalError` and assert genuine content, not just "doesn't crash" | **PASS** |
| **AC17** | `pytest-xdist` documented dependency; suite green both serial and `-n auto`, identical results | `pyproject.toml`/`README.md` read directly; `pytest-xdist 3.8.0` confirmed installed in the live venv; `[tool.pytest.ini_options]` still carries `testpaths`/`asyncio_mode` (guarded by `tests/test_deliverables.py`'s own meta-test, which passed in every run); **3 consecutive full-suite runs this gate itself ran** — see Summary — all three report identical `4878/0/1/1xf` | **PASS** |
| **AC18** | `RELEASE_NOTES["1.10.0"]` EN+TH, correct heading, announced once/user, idempotent | `core/release_notes.py:RELEASE_NOTES["1.10.0"]` read directly (both languages, correct heading, all 4 topics present); `tests/test_v110_integration.py::test_ac18_release_1_10_0_announced_once_per_user` (real `announce.announce_release`, idempotent second call); this gate's own `test_release_notes_1_10_0_content_matches_spec_bullets_both_languages` (catalog-level content spot-check, independent of the announce plumbing) | **PASS** |

**18/18 PASS.**

---

## 2. The 19-edit audit — actually 27 files (28 incl. `conftest.py`)

The dispatch described "19 pre-existing test files edited for the new mandated defaults." `git status` shows the true count is **27 pre-existing test files modified**, plus `tests/conftest.py` (28 total) — this gate audited **all 28**, not a cherry-picked 19, since the dispatch's count appears to undercount (it likely reflects only the integration pass's own edits, not the shared-surface/M2/M3 passes' mechanical touches on top). Every file was diffed and classified: **(a)** config-override opt-out preserving the original point, **(b)** assertion updated to match a new mandated default (verified as strong as the old, and matched to a SPEC rule), or **(c)** weakened/vacuous → FAIL.

Audited in 4 parallel batches (each independently diff-read against `git diff -- <file>`, not against IMPL's own summary of itself).

### Batch 1 — mechanical migration-count bumps (7 files)

| File | Change | Class | Verdict | Justification |
|---|---|---|---|---|
| `test_db.py` | `unparsed_state` added to expected column set | b | PASS | R-SS1, set-equality stays exhaustive |
| `test_heatmap.py` | migration count 12→13 | b | PASS | R-SS1, mechanical |
| `test_history.py` | migration count 12→13 | b | PASS | R-SS1, mechanical |
| `test_migrations.py` | 9 upgrade-path assertion pairs 12→13 | b | PASS | R-SS1; idempotency re-checks kept intact |
| `test_routines.py` | schema_version 12→13, before-version untouched | b | PASS | R-SS1, mechanical |
| `test_v13_integration.py` | schema_version 12→13 | b | PASS | R-SS1, mechanical |
| `test_v18_routines_gaps.py` | byte-identical-migration "before" baseline gains `unparsed_state: None` per row | b | PASS | R-SS1 — strengthens the comparison (now also proves the new column lands NULL), not a loosening |

No opt-outs needed in this batch; no weakening found.

### Batch 2 — plumbing / dispatch-table / signature widening (7 files)

Audited directly by this gate (`git diff -- <file>`, full hunks read, not a summary) after the dedicated parallel audit fork for this batch ran long — read myself instead of blocking the report on it.

| File | Change | Class | Verdict | Justification |
|---|---|---|---|---|
| `test_v19_shared_surface.py` | 1 assertion: `schema_version` 12→13 | b | PASS | R-SS1, mechanical |
| `test_wrapped.py` | `len(MIGRATIONS)`/`schema_version` 12→13, comment updated to name both 012/013 | b | PASS | R-SS1, mechanical |
| `test_channels.py` | 7 fake `on_message` signatures widened with `reply_to_message_id: str \| None = None` | b | PASS | R-SS7; purely mechanical, zero assertion logic touched in any of the 7 |
| `test_v18_shared_surface.py` | 2 fake signatures widened (same as above) + 1 docstring note | b | PASS | R-SS7, mechanical |
| `test_refactor_s3.py` | `_EXPECTED_ROW_ORDER` (the EXHAUSTIVE dispatch-table ground truth) gains `"guide"` inserted immediately before `"query"` | b | PASS | R-SS8's own stated placement; confirmed nothing else in the 28-row list was touched — the test still asserts the FULL ordered list, not a subset |
| `test_commands.py` | (1) `test_normal_message_while_llm_down_is_still_deferred_not_intercepted`: `outage.honest_reply=False` override; (2) migration count 12→13; (3) `_RESERVED_WORD_EXPECTED_KIND` (an EXHAUSTIVE dict, asserted via `.keys() == words`) gains `"guide": None, "คู่มือ": "guide"` | a / b / b | PASS | (1) override real, original "deferred not intercepted" point preserved; (2) R-SS1 mechanical; (3) R-SS8, confirmed exhaustive-equality assertion still holds — nothing else silently added or dropped from the reserved-word ground truth |
| `test_resilience.py` | (1) 5 fake signatures widened; (2) `test_deferred_message_acks_writes_unparsed_row_and_never_calls_llm`: `outage.honest_reply=False` override; (3) `test_reparse_on_recovery_reclassifies_confirms_and_reincludes_in_aggregations`: same override; (4) **`test_reparse_leaves_genuinely_unparseable_row_as_unparsed`: rewritten from asserting the row stays a zombie forever (`pending==1 row`, `sent==[]`) to asserting the NEW terminal-closure behavior (`pending==[]`, `unparsed_state=='closed'`, exactly one closure sent, quoting the raw text) — while still correctly preserving the ORIGINAL test's own point that `category` stays `'unparsed'`, never force-classified** | b / a / a / b | PASS | (1) mechanical; (2)/(3) overrides real, original points (deferral row written + LLM never called; recovery reclassify+confirm) both independently confirmed still exercised under the override; (4) is the single most load-bearing rewrite in this entire audit — it is the pre-existing test that used to lock in the EXACT zombie-forever bug this release's own name refers to (SPEC-v1.10.md §1); the new assertion is strictly stronger (proves terminal state + notification content, not just silence) and is independently corroborated by this gate's own from-scratch zombie-proof test (§3 below) |

**Batch 2 verdict: 7/7 PASS, no weakening.** Combined with batches 1/3/4: **all 28 pre-existing test files audited, 0 FAIL, 0 weakened/vacuous edits found.**

### Batch 3 — highest-risk: default-behavior fallout from R6/R10/R15 (7 files)

| File | Change | Class | Verdict | Justification |
|---|---|---|---|---|
| `test_fallback.py` | 3 tests: `clarify.enabled=False` override added | a | PASS | Override real (wired into the config the call under test uses); each test's own original point (threshold behavior, outage-survival) verified intact under it |
| `test_v11_integration.py` | `test_ac4_clarifying_question_carries_no_button` — rewritten to assert a real `/log`-prefixed button set instead of `== []` | b | PASS | Strictly stronger than the old assertion; matches R10 |
| `test_v11_integration.py` | `test_ac4_deferred_ack_carries_no_button` — rewritten to assert `outage_honest_reply` text + non-empty buttons | b | PASS | Matches R15's mandated default |
| `test_v11_integration.py` | `test_ac33_ollama_down_skips_nl_step...` — `outage.honest_reply=False` override | a | PASS | Downstream `deferred_ack`/pending-row assertions confirmed still exercised under the override |
| `test_v15_integration.py` | `test_ac16_ambiguous_text_still_defers...` — override | a | PASS | Isolates R15 from the test's real point (deferral gate placement) |
| `test_v15_integration.py` | migration count 12→13 | b | PASS | Mechanical |
| `test_v18_release_gate.py` | quicklog-independence test — `outage.honest_reply=False` override | a | PASS | Preserves the test's own point |
| `test_v18_release_gate.py` | menu-count 22→23/27→28 | b | PASS | R17, exact bump |
| `test_v12_integration.py` | exact-set add of `"guide"` only + migration bump | b | PASS | R17/R-SS1, nothing else silently added/dropped |
| `test_v16_integration.py` | menu count 22→23 + explicit `"guide" in names` + migration bump | b | PASS | R17/R-SS1, strengthened not loosened |
| `test_discoverability.py` | exact-set add of `"guide"` only | b | PASS | R17, verified nothing else silently changed |

No weakened/vacuous edits found. Every config-override in this batch was confirmed to be a real, load-bearing argument actually reaching the code path under test (not a declared-and-ignored override) — the specific failure mode this audit was watching hardest for.

### Batch 4 — `{label}` i18n fix + release-gate rewrite (7 files)

| File | Change | Class | Verdict | Justification |
|---|---|---|---|---|
| `test_confirmations.py` | 2 assertions, `confirm_numeric_goal`/`nogoal` EN strings | b | PASS | New string carries real `{label}` interpolation, matches TH's existing shape — an Archi-sanctioned bug fix, not a spec rule, verified factually correct |
| `test_multi_habit_integration.py` | 1 confirmation string + schema-version 12→13 | b | PASS | Same `{label}` fix + R-SS1 mechanical bump |
| `test_v17_integration.py` | 1 assertion — "pages pages logged" (label==unit=="pages" doubling is correct for this synthetic habit) | b | PASS | Exact substring match, not loosened; incidentally re-proves `{label}` interpolation for custom habits |
| `test_v17_release_gate.py` | 2 confirmation strings + menu-count/tail assertions | b | PASS | Same `{label}` logic; menu assertions strengthened (exact len + tail-order + `guide` position) |
| `test_v19_integration.py` | menu-count 22→23 + `"guide" in names` | b | PASS | Strictly additive (old check kept, new one added), R17 |
| `test_v19_release_gate.py` | **fail-open-posture test rewritten**: 4 of 5 sites flipped from `pytest.raises(...)` to real-content assertions; +menu-count bump | b | **PASS — verified strong, not a hack** | Specifically scrutinized for "fixed the test to match broken code" — confirmed the rewrite retains genuine fault injection (`sqlite3.OperationalError` raised from a real `active_pauses` override) and each site's new assertion demands actual output (`message is not None`, `lines` truthy, `stats.habits` truthy, and the nudge site goes further — seeds an 80%-close habit so "the nudge is actually delivered" is provable, not just "no crash"). This is the correct R18 flip. |
| `tests/conftest.py` | `RecordingChannel.send` returns a synthetic incrementing id | b | PASS | Additive (`-> str \| None`); every existing caller ignoring the return is provably unaffected; matches R-SS5 |

No weakened/vacuous edits found in batch 4.

---

## 3. The zombie proof

`tests/test_v110_release_gate.py::test_zombie_proof_real_ollama_shaped_fake_closes_and_offers_then_zero_llm_calls_on_resweep`, run and independently re-confirmed by this gate:

- Seeded 4 rows across 2 pre-existing users, id-shaped exactly like production `id=13`("500")/`id=14`("Streaching") — `category='unparsed'`, `unparsed_state` NULL (the untouched legacy shape).
- First sweep drove the REAL `core/routing.py:reparse_pending_unparsed` through the REAL `core/parser.py:parse_message` (not a stand-in), behind a fake Ollama client whose `chat_json` returns genuine "unknown"-category JSON. **4 real LLM round-trips** (one per row).
- Result: both users' "500" rows → `awaiting_clarify` + the water tap-to-fix offer; both "Streaching" rows → `closed` + the ONE bilingual closure notification. `pending_unparsed()` empty afterward.
- Each user received **exactly one** message about each of their two rows — never zero, never doubled, never cross-delivered to the other user.
- Second sweep: the fake Ollama client now **raises** if `chat_json` is called at all. `pending_unparsed()` stays empty and the sweep completes cleanly — **zero LLM calls**, proven by construction (an exception would have propagated and failed the test), not merely inferred from an empty query result.

This is a strictly deeper proof than the existing integration suite's own AC5/AC6 test, which bypasses `parser.py` entirely via a `parse_message=` stand-in — this gate's version exercises one more real layer.

---

## 4. Cross-feature probes

| Probe | Result |
|---|---|
| Reply-to-reminder during an Ollama outage | Confirmed: a **bare-value** reply still attributes zero-LLM even while Ollama is down (existing suite). This gate additionally probed a **number+unit** reply to a *different* habit than the reminder's own — `resolve_reply_value` correctly returns `None` (R14), and the message falls through to deterministic preparse, which resolves it correctly and zero-LLM even with Ollama down — proving R14's conservatism doesn't accidentally force a needless deferral when preparse itself can handle it. |
| Clarify tap after the guessed habit is archived mid-wait | New scenario, not covered elsewhere. A custom habit is created, offered as a tap-to-fix guess, then archived (soft-delete, `/delhabit`'s own real path — `db.archive_user_habit` + `provider.invalidate`) before the tap arrives. The tap correctly resolves against the tapping user's *freshly rebuilt* registry, finds the habit gone, and produces the same friendly "unknown habit" no-op AC10 already defines for a foreign/unknown habit id — no crash, no write, the row stays `awaiting_clarify` (not silently dropped, not force-resolved). |
| Backfill phrase + clarify interplay | Two scenarios. (1) "500ml yesterday" — backdates and logs immediately via deterministic preparse, unaffected by v1.10, zero-LLM even Ollama-down. (2) **"yesterday 500" — a genuine, documented, non-blocking FINDING**: the row's `raw_message` (and everything `clarify.py` reads) keeps the FULL original text including "yesterday" — backfill's date-stripping only ever touches the routing.py-local `parse_text`, never the DB row or the clarify computation. On recovery, `clarify.tier1_guesses`'s bare-number-plausibility check (`_is_bare_number`) is **whole-message-anchored** (`VALUE_RE.match`, not `.search`), so "yesterday 500" does not start with a digit and the bare-number guess that a plain "500" would get is silently missed. The row still safely reaches the terminal `closed` state (not a zombie) and the user still gets the one closure notification quoting their own exact words — **no data loss, no silence, no AC violation** (no SPEC-v1.10.md AC governs the backfill/clarify interaction; backfill is v1.8-scoped) — just a less helpful guess than a bare "500" alone would produce. Flagged as a backlog item, not a release blocker. |
| Quicklog tap unaffected by `clarify.enabled=False` | Confirmed: `on_callback`'s `log:`/`clarify:` prefixes are dispatched independently with no shared config gate; a quicklog tap logs and confirms normally regardless of the clarify toggle. |
| Single-flight guard under a REAL concurrent sweep trigger | Confirmed via genuine `asyncio.gather` concurrency (not a directly-called re-entrancy probe): `parse_message` is called exactly once total across two concurrently-launched sweeps, and exactly one closure notification is ever sent — the second call's own `_sweep_in_progress` check correctly short-circuits before touching the row at all. |
| Does closure/offer respect `silent_proactive`? | **No, and it structurally cannot** — `Channel.send_actionable` (used by `offer_clarify`, `send_closure`-with-buttons, and the outage message-with-buttons) has **no `disable_notification` parameter in its ABC signature at all**, confirmed via `inspect.signature`. This matches the established codebase rule (grep-confirmed across every source file): `silent_proactive` is threaded ONLY through the three genuinely proactive, unprompted sends (`reminders.send_reminder`, `checkins.build_checkin_message`, `nudge.build_nudge_message`). Every v1.10 closure/offer/outage send is REACTIVE (answering a message the user already sent, or a previously-deferred one) — the same category as an ordinary log confirmation, which has never honored `silent_proactive` either. **Rule: correct, consistent, no gap.** |

---

## 5. Announce readiness

`RELEASE_NOTES["1.10.0"]` verified at the catalog level (both languages present, correctly headed `🎉 What's new in v1.10.0` / `🎉 มีอะไรใหม่ใน v1.10.0`, all 4 topics — never-lose-a-log/tap-to-fix, reply-to-reminder, outage honesty, `/guide` — present in both languages) and at the plumbing level (`tests/test_v110_integration.py::test_ac18_...` — announced once per active user, idempotent second call, via the real `announce.announce_release`). **No version bump was made by this gate** — `VERSION`/`pyproject.toml` remain at `1.9.4`, correctly left for Archi's Phase 6.5 step.

---

## 6. Inert gate — precisely which paths are byte-identical, and which one deliberately isn't

With `[outage] honest_reply=false`, `[clarify] enabled=false`, `[reply_to_reminder] enabled=false` all set at once:

- **Byte-identical to pre-1.10**: the Ollama-down deferral (`deferred_ack`, no keyboard); a mapped reminder reply that can't preparse on its own (falls through to a plain deferral, the map is built but never consulted); an ordinary preparse-hit log.
- **Genuine, spec-mandated, UNCONDITIONAL exception (documented, not a gap)**: the generic clarifying-question path. R10's own wording ("never silence... the `/log` keyboard") has **no config gate** — `clarify.enabled=false` only suppresses the GUESS BUTTONS and the `awaiting_clarify` row (R6's own scope); it does **not** restore the pre-1.10 zero-button clarifying question. This gate's `test_inert_gate_does_NOT_extend_to_the_live_clarify_generic_question_by_spec_design` locks this in explicitly so a future audit doesn't mistake it for a regression, and so an accidental future config-gating of R10 itself would be caught as the regression it would actually be.

---

## 7. Menus / `/guide` / `/help`

Public menu 23, owner menu 28, both EN and TH, confirmed through the REAL wired `async_main`/`set_my_commands` calls (existing suite). `/guide` dispatches bilingually end-to-end (existing suite, both direct dispatch and through the real `on_message`). This gate additionally confirms `build_guide_text`/`build_help_text` both render well under Telegram's 4096-char budget in both languages, and that `/guide` is reliably the shorter of the two (its own stated design intent, R16).

---

## 8. Full-suite run log

| Run | Mode | Result | Time |
|---|---|---|---|
| 1 | `pytest -n auto` | 4878 passed, 0 failed, 1 skipped, 1 xfailed | 77.04s |
| 2 | `pytest` (serial) | 4878 passed, 0 failed, 1 skipped, 1 xfailed | 218.85s |
| 3 | `pytest -n auto` | 4878 passed, 0 failed, 1 skipped, 1 xfailed | 75.88s |

All three identical. No order-dependence, no flakiness. `PYTHONPATH=src`, `.venv\Scripts\python.exe -m pytest`, foreground, no `uv`.

---

## Recommendation

**Ship.** All 18 ACs PASS at the wired level. The release's own reason to exist (the zombie-reparse loop and the silent-log-death betrayal) is fixed and doubly proven. The one critical security finding from M1's own Vera round (cross-user row hijack) is fixed and its fix independently re-verified by direct code reading here. All 28 pre-existing test-file edits (27 test files + `conftest.py`) audited are legitimate — real opt-outs or genuinely stronger assertions, never weakened. One documented, non-blocking backlog item (backfill-phrase text defeats the bare-number tier-1 guess, §4 above) and one process gap (missing `TEST-v1.10-m3.md` file, though M3's actual work is independently verified sound). Announce readiness confirmed at the catalog level; version bump intentionally left to Archi.
