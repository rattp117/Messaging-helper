# Test Report — v1.10.0 Module M3 (pause fail-open unification + pytest-xdist)

> Record note (Archi): this file was reconstructed verbatim from the M3 Vera's delivered report
> (2026-08-27) — the agent reported in-message but omitted writing the file, caught by the
> release-gate Vera's audit. Content below is her report as delivered, unedited in substance.

## Summary
- **Scope:** M3 only (AC16, AC17) — checkins/nudge/streaks/review pause routing, `pyproject.toml`/README xdist docs, `tests/test_pause_failopen.py` (Luna's 12), `tests/test_v19_release_gate.py` update, plus adversarial probe `tests/test_v110_m3_gaps.py` (10 new).
- **M3-scoped tests run:** 366 — **366 passed, 0 failed**.
- **Full suite, serial:** 4717 passed, 1 failed (M1-owned, not M3), 1 skipped, 1 xfailed (209.5s).
- **Full suite, `-n auto`:** 4831 passed, 1 failed (same M1-owned), 1 skipped, 3 xfailed (76.9s).
- **Status: PASS (M3 scope).**

## AC coverage

| AC | Coverage | Result |
|---|---|---|
| AC16 (pause fail-open unified, all 5 sites, fail-open + no fan-out abort) | test_pause_failopen.py (12) + test_v110_m3_gaps.py positive controls (5) + test_v19_release_gate.py (6, updated) | PASS |
| AC17 (pytest-xdist: dev dep, documented, suite green serial + parallel, no order-dependence, testpaths/asyncio_mode intact) | pip list / pytest --collect-only header / deliverables meta-test + full-suite serial & -n auto runs | PASS |

## Site-by-site verification (source-read, not just tests)

| Site | Call | Routed through |
|---|---|---|
| 1 (reference) | reminders.py:430 | pause.is_paused_safe (pre-existing, shared surface) |
| 2 | checkins.py:184 build_checkin_message | pause.active_pauses_safe |
| 3 | nudge.py:137 build_nudge_message | pause.active_pauses_safe |
| 4 | streaks.py:364 compute_daily_summary | pause.is_paused_safe |
| 5a | review.py:162 compute_weekly_stats | pause.is_paused_safe |
| 5b | review.py:320 run_weekly_review trends filter | pause.is_paused_safe |
| 5c | review.py:343 render_weekly_review_charts | inherits via compute_weekly_stats (charts.py itself has zero pause reads); behaviorally verified — raising db still yields a real PNG |

- `is_paused_safe`/`active_pauses_safe` (core/pause.py:119-146) wrap in try/except with `logger.exception` — **not silent** (3 caplog tests), matches R-SS9.
- Fan-out shapes confirmed at source: daily_summary_job / weekly_review_job have uncaught per-user loops (so fail-open at the read matters); run_due_nudges has a pre-existing per-user try/except — R18's fix goes further (nudge actually *delivered*, not just non-crashing), verified.
- Positive controls added (a genuinely-paused habit stays suppressed at multi-user fan-out level, all 5 sites) — previously untested anywhere.
- Correctly out of M3 scope: core/jobs.py wrapped_auto_job still called raw `pause.is_paused` — flagged; adopted `is_paused_safe` at the v1.10 integration pass.

## test_v19_release_gate.py update ruling
**Faithful strengthening, not weakened.** Sites 2/4/5 flipped from `pytest.raises` to positive content assertions (correct — raising was the bug being fixed). Site 3 (nudge) got *stricter*: a genuinely-close habit added and delivery now asserted, which the old test never proved. Site 1 untouched as the reference posture. No assertion deleted or loosened without an equal-or-stronger replacement.

## Venv health
- `.venv\Scripts\python.exe -c "import habit_assistant.main"` clean; xdist-3.8.0 registered; pip list matches pyproject (base + dev + charts extras, no strays).
- Minor: `pip show habit-assistant` metadata stale at 1.4.0 (old editable install) — refresh with `pip install -e .` at next natural touch-point; does not affect imports/tests.

## Findings
None against M3's delivery. One outside-scope flag at the time (M1's ownership gap) was surfaced to Archi and separately fixed/closed in the M1 track.

## Recommendation
**Ready to ship — M3 scope.** AC16 and AC17 PASS with independent adversarial verification beyond Luna's suite.
