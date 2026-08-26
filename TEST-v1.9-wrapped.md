# Test Report — v1.9.0 module `wrapped` (recap card PNG + emoji-burst)

## Summary
- Total (wrapped-scoped): 65 (Luna, `tests/test_wrapped.py`) + 21 (Vera, `tests/test_v19_wrapped_gaps.py`) = **86 tests**
- Passed: 86
- Failed: 0
- Full regression suite: **4205 passed, 1 skipped, 1 xfailed, 0 failed**
- Status: **PASS**

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_wrapped.py` (Luna) | 65 | AC25, AC26, AC27, AC29 dispatch/render/execute_wrapped/celebration_burst — full unit + object-level coverage |
| `tests/test_v19_wrapped_gaps.py` (Vera, new) | 21 | Adversarial probes below — window boundaries, cross-user/registry leak resistance, font engagement, clipping-bug regression, fallback robustness, caption/composition contracts |

## Adversarial probes performed (this session)

1. **Card composition** — window boundary exactness against real card content (not just `_window_days`), a "backfilled log counts by date not insertion order" proof, custom habit via the **real** `RegistryProvider` pipeline with a stronger-than-Luna's-own leak check (B's registry never even *contains* A's custom habit id), and archived-habit exclusion (`db.archive_user_habit` → registry rebuild → card).
2. **4-week window** — confirmed via `_window_days`'s own docstring and a new test that `"4w"` is a fixed 28-day block ending today, **not** ISO-week-aligned (spec text: "rolling last 4 weeks (28 days)" — consistent). Boundary day (today−27) included, day before it (today−28) excluded, verified against rendered card content, not just the date list.
3. **Thai rendering** — Luna's object-level Thai-`Text` proof holds; added a **font-engagement** test confirming `Noto Sans Thai` is genuinely in `font_manager.fontManager.ttflist` (not just named in `rcParams`) and the bundled `.ttf` file exists on disk. Added a **geometry-level regression test** for Luna's self-caught canvas-clipping bug: every text artist's position, run through its own transform into figure-fraction coordinates, is asserted inside `[0,1]×[0,1]` — the exact invariant the old `x=1.03` axes-fraction bug violated. Both pass; the fix holds.
4. **No-matplotlib fallback** — bilingual EN/TH diff proven directly on the fallback string (not just the image caption), `/wrapped`/`/recap`/`การ์ดสรุป` proven to route **byte-identically through the fallback path specifically** (Luna's own alias-equivalence test only exercises the PNG path), a 20-habit fallback proven not to crash and to produce exactly one line per habit. **Finding (not a defect):** `core/wrapped.py:_build_fallback_text` never calls `core/render_budget.py`'s `TELEGRAM_MESSAGE_BUDGET`/`fit_within_budget` — confirmed this is **not** a wrapped-specific gap: `core/heatmap.py:_build_fallback_text` has the exact same omission. Luna's "mirrors heatmap.py R-H2 exactly" claim is accurate, including this shared limitation. Not filed as a failure; noting for the record in case Archi wants a follow-up ticket against both modules.
5. **celebration_burst** — confirmed the append-composition contract (`base + "\n" + burst`) leaves `base` as an exact prefix, and that disabling `celebrate_burst` composes to byte-identical `base` (no dangling newline). The actual `main.py` `confirmation_suffix` wiring is out of `wrapped`'s scope (correctly deferred, see AC29 below) — this test proves the module's own half of the contract.
6. **execute_wrapped / send_image** — caption format matches the spec §3 sample shape exactly; confirmed a `send_image` failure falls through to the **full** per-habit fallback text (not a bare header), matching `execute_heatmap`'s established pattern.
7. **Zero-LLM** — extended Luna's signature-level check to the dispatch grammar's own source (`_match_wrapped`).

## AC coverage

| AC | Description | Test(s) | Result |
|---|---|---|---|
| AC25 | `/wrapped`, `/recap` alias, `month` tail, PNG via `send_image` w/ bilingual caption | `test_dispatch_recognizes_wrapped_shape`, `test_recap_alias_routes_identically_to_wrapped`, `test_execute_wrapped_success_sends_image_and_returns_empty_string`, `test_execute_wrapped_month_period_uses_month_caption`, `test_execute_wrapped_caption_matches_spec_sample_shape` (Vera) | **PASS** |
| AC26 | Per-user isolated, registry-generic (incl. custom), cadence-aware, reuses records/trends/heatmap | `test_render_many_habits_including_custom_and_cadence_habit`, `test_build_figure_cadence_habit_shows_week_wording`, `test_render_isolated_per_user`, `test_custom_habit_via_registry_provider_appears_and_never_leaks_across_users` (Vera, real `RegistryProvider`), `test_archived_habit_is_excluded_from_the_card` (Vera) | **PASS** |
| AC27 | Thai glyphs not tofu; matplotlib-unavailable/render-exception → bilingual text fallback, never raises | `test_build_figure_thai_labels_are_present_as_real_text_objects`, `test_noto_sans_thai_is_actually_registered_in_the_font_manager` (Vera — font-manager-level proof), `test_thai_row_text_falls_within_figure_canvas_bounds_luna_clipping_regression` (Vera — geometry regression for Luna's clipping fix), `test_render_matplotlib_absent_returns_none_and_warns_once`, `test_execute_wrapped_matplotlib_absent_falls_back_to_text_and_sends_no_image`, `test_execute_wrapped_never_raises_when_render_itself_raises`, `test_execute_wrapped_never_raises_when_send_image_fails` | **PASS** |
| AC28 | Month-end `auto_send`, silent, pause/DND-aware; default off | **Correctly deferred** — Rule 26/AC28 is explicitly `main.py` integration scope per SPEC-v1.9.md §6/§11, not module `wrapped`'s file list. Confirmed by direct inspection: `main.py` has zero references to `wrapped`/`auto_send`/any scheduler tick for it — the integration phase has not yet run for this module. `config.wrapped.auto_send` defaults to `false` per `WrappedConfig` (shared surface, verified separately). | **N/A — not yet integrated, not a `wrapped`-module defect** |
| AC29 | Emoji-burst appended to celebration line, gated by `celebrate_burst`, zero-asset | `test_celebration_burst_enabled_by_default_returns_the_bundled_emoji`, `test_celebration_burst_disabled_returns_empty_string`, `test_celebration_burst_is_language_agnostic`, `test_celebration_burst_appended_leaves_base_text_as_exact_prefix` (Vera) | **PASS** (function-level; `main.py` append-wiring itself is integration scope, correctly not yet done) |

## Judgment-call rulings

1. **"Best day" is window-scoped, not the lifetime `/records` record.** Rule 21 lists "best day" among the card's pieces without specifying scope; the rule's own text — "It reuses `records.period_total` ... no new aggregation" — names `period_total`, not `db.get_record`, as the reuse target. Locked in with `test_best_day_is_window_scoped_not_lifetime_record` (Vera): a day outside the window with a far higher value is correctly *not* picked. **Ruling: PASS, with a note flagged to Archi** — if product intent was actually the all-time record, it's a one-function swap per Luna's own IMPL note (`db.get_record(user_id, habit.id, "best_day")`), not a rewrite.
2. **AC28 auto-send is integration scope, correctly deferred.** Verified directly (no `wrapped`/`auto_send` reference anywhere in `main.py`) rather than taking Luna's claim on faith. Confirmed correct per SPEC-v1.9.md §6/§11's explicit module/integration split.

## Deferred (not this module's scope)

- **AC28** (month-end auto-send scheduler wiring) — `main.py` integration, not started for this module yet.
- `celebration_burst`'s actual append into `confirmation_suffix`, and the `cadence`/`pause`/`resume`/`wrapped` command branches in `main.py` — same integration phase.

## Pause-transient loose end (as requested)

- `tests/test_pause.py` run **standalone**: **57 passed**, 0 failed — confirmed green.
- `tests/test_pause.py` also green (57/57) embedded inside the full suite, both full-suite runs this session.
- **However**, a first full-suite run this session (before `tests/test_v19_wrapped_gaps.py` existed) showed **3 failures**, all in a *different* file — `tests/test_v19_pause_gaps.py::TestSemanticEdges` (another Vera's own adversarial suite for the `pause` module, not `tests/test_pause.py` itself, and not owned by this task). A second full-suite run, with no code changes in between, came back **fully green (0 failed)**. This is the same flake *class* Luna already noted for `tests/test_cadence.py` during the parallel build (a transient failure that resolved with no code change) — consistent with test-ordering/shared-state sensitivity while multiple modules' test suites were still landing concurrently in this session, not a `wrapped`-module issue. I did not dig further per the task's own "don't dig deep if not evident" instruction; flagging the specific file/test names here in case Archi wants pause's owning Vera to look at `TestSemanticEdges`'s isolation.

## Failures

None.

## Regressions detected

None. Full suite: 4205 passed / 1 skipped / 1 xfailed / 0 failed on the clean re-run.

## Recommendation

**Ready to ship** — all `wrapped`-owned ACs (AC25, AC26, AC27, AC29) PASS; AC28 is correctly out of this module's scope pending `main.py` integration (not a defect). One judgment call ("best day" window-scoped) is a reasonable spec-consistent reading, flagged to Archi for product confirmation rather than assumed silently. One informational note (fallback text has no `render_budget` cap) mirrors an existing accepted precedent in `heatmap.py`, filed for the record, not as a blocker.
