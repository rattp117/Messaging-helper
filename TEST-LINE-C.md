# Test Report — LINE Module C (Trimmed Daily Digest)

## Summary

- **Round 2 (this revision).** Re-checks Luna's round-2 fix to `core/digest.py` (section reordering + budget/compaction/truncation + split fail-open/fail-closed log wording) against round-1's findings. Round 1's report is preserved below (§"Round 1 findings, as resolved") with each item marked resolved/still-open.
- **Subset run** (foreground, worktree cwd `C:\Users\Demo\OneDrive - Ngow Hock Agency Co,Ltd\Claude-Cowork\Messaging-line`, main venv python, per Archi's instruction — not the full LINE gate):
  `pytest tests/test_digest.py tests/test_line_c_gaps.py tests/test_commands.py tests/test_audit.py tests/test_refactor_s3.py -q`
- Total: **381** tests (39 Luna's own `test_digest.py` + **52** `test_line_c_gaps.py` [40 round-1 + 12 round-2: order-sanity, budget-mechanism boundary probes, Thai-compaction-rendering, 300-habit truncation-fallback proof, and the fail-open/fail-closed relabel-verification replacing round 1's now-stale "mislabeled" test] + 290 across `test_commands.py`/`test_audit.py`/`test_refactor_s3.py`)
- Passed: **381**
- Failed: **0**
- Status: **PASS**
- Tree state: unchanged from round 1 — worktree still has uncommitted concurrent work from modules A (its own fix round, per Archi) and D, not touched by this pass. Full LINE-wide gate not re-run (per Archi's standing redirect to the foreground, C-scoped subset for verdicts).

## Round 2 re-check — Archi's four asks

### 1. Priority order matches the ruling (summary/due/nudge/announcement/owner-warning full fidelity, grace last)

**Confirmed, with hard evidence, not just a read-through.** New test `test_section_priority_order_matches_the_round_2_ruling_grace_last` builds a kitchen-sink digest (all seven possible sections present) and asserts relative *position* via `str.index` (a substring-inclusion check, as round 1's kitchen-sink test did, would pass even if sections were shuffled — this one wouldn't):

```
due_idx < summary_idx < nudge_idx < announcement_idx < warning_idx   (the protected group, in order)
warning_idx < review_idx < grace_idx                                  (grace is LAST of all)
```

Reading `core/digest.py:293-323` directly confirms the same order in the source: due → summary → nudge → announcement → owner-warning → review-day line → grace (via `_grace_bridged`, appended last with `grace_index = len(sections)` recorded for the compaction step). Matches the docstring at `compose_digest`'s own top (lines 257-265): "in that priority order (highest first)... the grace section is deliberately LAST and the first thing compacted/dropped."

**Order-sanity read (note, not a failure — no AC or spec rule pins exact render order):** the resulting reading order is sane. Today's outstanding items (due) come first, then the full recap (summary), then encouragement (nudge), then informational/operational items (announcement, owner-warning), then a forward-looking pointer (review-ready), and finally a retrospective note about *yesterday* (grace) — landing last is defensible on its own merits, not just as a truncation-safety trick.

### 2. Compaction line renders real Thai

**Confirmed.** New test `test_grace_compact_line_renders_real_thai_with_correct_count` calls `digest._grace_compact_line("th", 7)` directly and checks: `{count}` was actually substituted (not left as a literal template token), no U+FFFD replacement character (encoding corruption marker), and every *letter* in the string falls in the Thai Unicode block (U+0E00–U+0E7F) — i.e. not mojibake, not a stray Latin fallback. (First pass of this check flagged the 🛟 emoji as "non-Thai" — a bug in my own filter, not the product; fixed to restrict the check to `ch.isalpha()` so emoji/punctuation are correctly ignored.) Separately, `test_compaction_actually_fires_and_renders_in_a_real_compose_digest_call` drives the compact line through a *real* `compose_digest()` call (15-habit registry sized to land in the compaction band) in both languages and confirms the aggregate line — not 15 repeats of the full per-habit sentence — is what actually appears.

### 3. The trailing `assert` can't fire in the truncation path

**Confirmed, via direct boundary probes of the private budget functions** (Archi's own phrasing acknowledged landing a real `compose_digest()` output at an *exact* byte count via habit fixtures "if constructible" — it isn't, practically: i18n strings/streak numbers/per-habit line lengths don't compose to round numbers on demand — so this probes the mechanism itself):

| Test | What it proves |
|---|---|
| `test_truncate_to_budget_boundary_exactly_at_budget_is_untouched` | assembled length == exactly `_LINE_TEXT_BUDGET` (4900) → returned untouched, nothing dropped |
| `test_truncate_to_budget_boundary_one_over_forces_a_drop` | one char over 4900 → at least one line dropped, result ≤ 4900 |
| `test_truncate_to_budget_never_exceeds_budget_regardless_of_input_size` (parametrized 4999/5000/5001/5002/50000) | regardless of how oversized the input, output is always `≤ 4900` and `< 5000` |
| `test_truncate_to_budget_floor_case_header_plus_footer_alone_fits` | the degenerate floor (every line dropped) — `header + footer` alone — is realistically well under budget, not just "structurally reachable" |
| `test_300_habit_registry_still_stays_under_budget_via_truncation_fallback` | a real `compose_digest()` call, sized so even due+summary+nudge alone overflow (forcing full truncation, not just grace-compaction) → `< 5000`, footer present |

The mechanism: `compose_digest` targets `_LINE_TEXT_BUDGET = 4900` (a 100-char margin below the 5000 hard limit) for both the compaction and truncation branches — either branch fires whenever assembled length exceeds 4900, so the final result is always ≤ 4900, well clear of the 5000 assert. The assert is confirmed to be a structural backstop, not a live code path: no test (round 1's adversarial ones included) has ever tripped it.

**Round-1 regression re-measured:** the original 20-habit maximal-stress case (which measured 5,651 EN / 6,232 TH pre-fix) now measures **3,700 EN / 3,994 TH** — matches Archi's reported numbers exactly.

### 4. The "fail-open" contrast wording in the sibling path isn't misleading

**Confirmed fixed**, and round 1's test that exercised this (`test_opt_out_read_error_log_line_is_mislabeled_as_fail_open`) has been **replaced** — kept as-is it would have kept trivially "passing" post-fix for the wrong reason (the new opt-out message still contains the literal substring `"fail-open"`, inside its own accurate contrastive clause `"unlike an ordinary composition error below, which is fail-open"` — a bare substring check can no longer distinguish the two states). The new test, `test_opt_out_read_error_and_composition_error_are_now_distinctly_labeled`, checks:
- the opt-out-read-error message's **leading** disposition word is `"fail-closed"`, appearing *before* any `"fail-open"` mention (i.e. `"fail-open"` only appears inside the contrast clause, not as the primary label);
- the composition-error message says `"(fail-open)"` and does **not** also claim `"fail-closed"`;
- both failure classes still produce a real, `caplog`-visible log record (not silent) for the respective user.

`core/digest.py:373-401` confirms the code change: the opt-out read now has its **own** `try/except` (lines 373-382), separate from composition's (lines 386-392) — no longer sharing one message between two genuinely different dispositions. Finding 3 is resolved.

## AC coverage (module C's scope, AC20–AC25)

All six unchanged from round 1 in disposition — **PASS**, and AC20 no longer carries a caveat:

- **AC20** → **PASS, no caveat.** Round 1's Finding 1 (5000-char overflow) is fixed; the maximal/300-habit/compaction/order tests all confirm correct, budget-safe composition.
- **AC21** → **PASS** (unchanged).
- **AC22** → **PASS** (unchanged).
- **AC23** → **PASS** (unchanged).
- **AC24** → **PASS** (unchanged).
- **AC25** → **PASS for module C's owned half** (unchanged; `/review` remains Integration's scope per standing ruling).

## Round 1 findings, as resolved

### Finding 1 (was: FAILING test) — LINE 5000-char budget overflow

**RESOLVED.** `core/digest.py` now targets a 4,900-char soft budget with a two-stage fallback (grace section compacted to one aggregate line first; if still over, an order-preserving drop-from-the-tail truncation with a footer) plus a structural `assert len(text) < 5000` backstop. Verified in round 2 §3 above via direct boundary probes and a re-measurement of the original failing case (5,651/6,232 → 3,700/3,994) and a new 300-habit stress case. `test_maximal_user_digest_stays_under_line_5000_char_text_limit` (round 1's originally-failing test, left otherwise unmodified) now **passes**.

### Finding 2 — Double-push / once-per-day: restart-safe, not concurrency-safe

**Not addressed by this round's fix (correctly out of scope — this is an Integration/deployment-layer item, not module C's).** Re-affirmed unchanged from round 1:
- A same-process crash-then-restart *after* the fire time is safe (APScheduler's in-memory job store, `CronTrigger` searches forward from `now`, confirmed empirically).
- The real risk is **two concurrent scheduler instances** both computing the identical fire instant — nothing in the code (nor should it, arguably) prevents that; SPEC-LINE.md R-A3's "single-instance invariant" is an assumption, not an enforced lock.
- The digest job is still **not wired into `core/app.py`** as of this round (`test_digest_job_is_not_yet_wired_in_app_py_integration_pending` still passes) — still Integration's open item.
- **Recommendation unchanged:** rely on systemd's single-instance guarantee (module D), or add a lightweight "already sent today" guard if that guarantee is ever in doubt. Not a code defect to hold this module's verdict on.

### Finding 3 — fail-closed opt-out-read-error mislabeled as fail-open in logs

**RESOLVED.** See round 2 §4 above. `core/digest.py` now splits the two failure classes into separate `try/except` blocks with distinctly-worded, non-overlapping-in-primary-label log messages.

## Ownership audit (formal, from round 1 — unchanged, no new evidence this round)

`core/routing.py` is explicitly listed under **Module B**'s owned files in SPEC-LINE.md §11, not module C's or the shared surface's — yet `IMPL-LINE-C.md` describes (and `git diff` confirms) module C adding one 11-line `if command.kind == "digest":` branch there.

- **Verdict: genuine ownership violation, functionally harmless.** C's branch is a byte-for-byte structural match to every sibling branch in that if-chain (`checkin`, `dashboard`, etc.), each an independent `if ...: ...; return` with zero interaction with B's own no-LLM gating logic elsewhere in the same function.
- **Distinguish from precedent:** `commands.py`/`audit.py` (also touched by C) are *not* violations — every module extends those two by established, explicit convention, and SPEC-LINE.md's file table doesn't single-assign either. `routing.py` has one named owner (B) and C wrote to it anyway. A process finding for Archi to note for future module splits, not a functional defect — no action needed for this verdict.

## Regressions detected

None, round 1 or round 2. Round 2's new tests are purely additive; round 1's structural-test diff review (migration count, reserved-word/closed-vocabulary/golden-order entries — all pure additions, nothing weakened) stands unchanged.

## Recommendation

**Ready to ship** (module C's own scope). All owned ACs (AC20–AC25) PASS; the round-1 defect (Finding 1) is fixed and re-verified with direct boundary/mechanism probes, not just a re-run of the original stress test; the log-wording fix (Finding 3) is verified with a test that specifically distinguishes it from the pre-fix state (not a stale assertion that would have kept passing either way); the section-reorder is verified to match the ruling with position-based (not just inclusion-based) assertions.

Carried forward for Archi to route, neither blocking this module's verdict:
- **Escalate to Archi** — Finding 2 (double-push under concurrent scheduler instances) belongs to the Integration pass / module D's systemd unit review, not module C.
- **Note to Archi** — the `core/routing.py` ownership violation (harmless this time) is worth a word to the team about module-split discipline before a less trivial concurrent edit collides.

This closes module C's verification track.
