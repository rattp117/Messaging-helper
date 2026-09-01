# Test Report — Admin Web Portal, module AUDIT (AC22–AC25)

> Verifies `src/habit_assistant/core/portal/audit.py` (Luna's `IMPL-PORTAL-audit.md`)
> against `SPEC-LINE-PORTAL.md` §4 R-AUDIT-1/R-AUDIT-2/R-AUDIT-3 (AC22–AC25),
> `UX.md` Screens 6–7, `UI.md` §3.10/§3.21/Screens 6–7. Branch `line-version`,
> worktree-only. Privacy was dispatched as the load-bearing surface; this
> pass probes it adversarially, beyond Luna's own 32 tests.

## Summary

- Total: 59 tests (32 Luna's `tests/test_portal_audit.py` + 27 new `tests/test_portal_audit_gaps.py`)
- Passed: 58
- Failed: 1 — **intentional, documents a real finding** (see below), not a broken test
- Regression sweep (shared portal surface + base audit/i18n/access/undo suites, 393 tests total across both AUDIT files + `test_portal_db/deploy/integration/layout/security/server/stats.py` + `test_i18n/test_audit/test_audit_capture/test_audit_capture_gaps/test_access/test_config/test_undo_ui.py`): **392 passed, 1 failed** (the same intentional finding), zero other regressions.
- Status: **AC22/AC23/AC25 PASS. AC24 PASS (structurally verified, exhaustively).** One **MAJOR FINDING** outside this module's own file ownership, requiring escalation — see below. One low-severity FINDING (custom-habit unit display) already self-flagged by Luna, confirmed real.

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_portal_audit.py` (Luna's, pre-existing) | 32 | AC22, AC23, AC24, AC25, this module's own AC31 slice, UI.md §3.21 pager, register() wiring |
| `tests/test_portal_audit_gaps.py` (new, this pass) | 27 | Custom-habit rendering gap confirmation, `source="portal"` vocabulary composition, exhaustive privacy sweep (unparsed rows, malformed non-text rows carrying `value_text`, soft-deleted rows, hostile display names, hostile audit entity/values), the diary-text-leak finding, deep multi-page pagination boundaries (off-by-one at exact page size / exact multiples, clamp-to-a-later-page), real identity-gate + real route composition (403/200), Thai column headers/pager/empty-state |

## AC coverage

| AC | Requirement | Test(s) | Result |
|---|---|---|---|
| AC22 | `GET /audit?page=2` → rows 50..99 newest-first, working pager | `test_audit_page_1_shows_newest_50_and_hides_newer_control`, `test_audit_page_2_shows_the_next_10_rows_newest_first` (Luna) + `test_audit_exactly_page_size_rows_is_a_single_page_both_controls_suppressed`, `test_audit_exactly_two_full_pages_boundary` (mine, off-by-one boundaries at 50 and 100 rows) | **PASS** |
| AC23 | Each row shows actor (you/name/id), localized action, target/entity, old→new, source, ts | 8 tests in Luna's file + `test_audit_row_source_portal_composes_with_the_now_extended_sources_vocabulary` (mine) | **PASS** |
| AC24 | `/activity` shows metadata only; no `raw_message`/diary text anywhere | `test_activity_text_habit_row_renders_em_dash_never_the_diary_text`, `test_activity_never_renders_raw_message_or_diary_text_even_when_hostile`, `test_activity_hostile_category_is_escaped_not_executed` (Luna) + `test_activity_unparsed_row_never_leaks_raw_message`, `test_activity_non_text_habit_with_stray_value_text_never_renders_it`, `test_activity_deleted_undo_log_is_hidden_not_shown`, `test_activity_user_column_escapes_a_hostile_display_name` (mine) | **PASS** — see "Custom-habit verdict" and "Privacy sweep" below for the exhaustive trace |
| AC25 | Page beyond last clamps to last valid page, no error | `test_audit_page_beyond_last_clamps_without_error`, `test_audit_malformed_page_falls_back_to_page_1` (Luna) + `test_audit_page_far_beyond_last_clamps_to_the_actual_last_page_not_page_1`, `test_audit_invalid_page_values_fall_back_to_page_1_on_a_multi_page_dataset`, `test_audit_page_beyond_last_on_a_totally_empty_log_shows_empty_state_not_a_pager` (mine — Luna's own clamp tests only used ≤5-row datasets, where "clamp to last page" and "clamp to page 1" are indistinguishable; mine use 120 rows and prove the clamp lands on the *actual* last page, e.g. page 3, not page 1) | **PASS** |

## Custom-habit rendering verdict (the dispatched top-suspected-gap)

**Confirmed real, but narrowly scoped — a display cosmetic gap, not a privacy or correctness bug.**

`_format_activity_value` (`core/portal/audit.py:211`) resolves a numeric value's unit via `HabitRegistry.from_config(deps.config)`, which — per `core/habits.py:HabitRegistry.for_user`'s own docstring (SPEC-v1.7.md R-G1) — does **not** include any user's per-user custom habits (those live only in a registry built with `.for_user(config, db, user_id)`). Verified with a real custom habit (`db.add_user_habit(MEMBER, {"id": "pushups", "unit_en": "reps", ...})`) logged and rendered on `/activity`:

- `test_activity_custom_habit_value_renders_without_unit_confirmed_gap` — the bare number (`20`) renders; `reps` does not, anywhere on the page.
- `test_activity_base_habit_value_renders_with_unit_control` — a base habit (`water`) renders `500 ml` correctly, proving the gap is specific to custom habits, not a general unit-lookup failure.
- `test_activity_habit_column_shows_raw_category_id_for_both_base_and_custom` — the "Habit" column is **not** registry-driven at all (it renders `row["category"]` verbatim, escaped) — so there is no mislabeling risk, only the missing unit suffix on the Value column.

**Severity: LOW.** AC24's literal wording ("category, value") does not require a unit. No data is wrong or leaked — only less pretty for exactly the users most likely to check the audit trail after adding a custom habit, and it breaks parity with the app's broader "registry-generic" custom-habit treatment elsewhere. Luna's own `IMPL-PORTAL-audit.md` already flagged this and judged it not worth a per-row `HabitRegistry.for_user` DB query; I concur that's a reasonable cost/benefit call for v1 — recommend a one-line note in `PROGRESS.md`/backlog rather than a blocking fix.

## Findings

### MAJOR FINDING — diary/text-habit content leaks into `/audit` via `undo`'s `old_value` (escalate to Archi)

**Not an AUDIT-module bug — inherited from a pre-existing, pre-portal capture-site bug that the portal makes materially more exposed.**

`SPEC-LINE-PORTAL.md` R-AUDIT-3 states its rationale for `/activity`'s privacy design as: *"matching the established posture that the owner's `/audit` never exposes another user's message content (AC24)."* **This claim is false, and demonstrably so.**

- **Root cause:** `core/undo_ui.py:161-172` (`send_undo_confirmation`) — `removed_value = row["value_num"] if row["value_num"] is not None else row["value_text"]`. For a text-type habit (e.g. `diary`), `value_num` is always `None`, so `removed_value` **is the full diary text**, passed verbatim into `audit.record(..., old_value=removed_value, ...)`.
- **Not hypothetical:** the base test suite already documents and accepts this — `tests/test_audit_capture.py::test_undo_diary_records_text_value` asserts `entry["old_value"] == "had a good day"` for exactly this path. This predates the portal (SPEC-v1.2.md/v1.3.md era).
- **Rendered verbatim on `/audit`:** `core/portal/audit.py:_render_audit_row` reuses `core/audit_view.py:_detail()` — the *same* formatter the chat `/audit` command uses — exactly as AC23 requires ("identical... to the chat `/audit`"). Escaping is correct (confirmed no XSS), but the diary content itself is fully visible, unredacted.
- **Proven live:** `tests/test_portal_audit_gaps.py::test_audit_detail_cell_leaks_diary_text_via_undo_old_value_MAJOR_FINDING` constructs a realistic diary-undo audit row (`old_value="I think I'm pregnant and haven't told my husband yet"`) and renders `/audit`. The actual output:
  ```html
  <td ...><span title="diary · I think I&#x27;m pregnant and haven&#x27;t told my husband yet → 1">
    diary · I think I&#x27;m pregnant and haven&#x27;t told my husband yet → 1</span></td>
  ```
  Fully readable diary content, in both the visible cell text and the `title=` attribute (so it also surfaces on hover/tooltip and copy-paste).

**Why this matters more now, even though the bug is old:** the portal turns a transient, owner-initiated chat query (`/audit N`, capped at 50 rows, requires the owner to actively type a command) into a **permanent, pageable, bookmarkable web table** spanning `db.audit_total()` rows across all time (AC22). Any user's private diary entry, once undone, sits there indefinitely and is one click/scroll away — a materially larger exposure surface than the chat command, even though the data source and the leak itself are unchanged.

**Disposition:** Does **not** fail AC22/AC23/AC25 as literally written — AC23 requires exact parity with the chat `/audit` command, and this module faithfully delivers that parity (the bug is upstream). AC24 is unaffected (`/activity` is structurally safe — see below). This **does** contradict R-AUDIT-3's own stated justification and is a real, user-facing privacy gap. **Recommend Archi route this to whoever owns `core/undo_ui.py`/`core/audit.py`** (not `core/portal/audit.py` — out of this module's file ownership per §11): the fix is at the recorder, not the renderer — never pass raw `value_text` as `old_value`/`new_value` for a text-type habit's `undo` action, mirroring the SQL-level protection `db.recent_logs_metadata` already applies for `/activity`. A one-line guard in `send_undo_confirmation` (e.g. `removed_value = row["value_num"]` only, or a redacted placeholder for text habits) would fix both the chat command and the portal simultaneously.

### AC24 privacy sweep — exhaustive, all PASS

Beyond Luna's own hostile-payload test, swept every plausible leak path for `/activity` specifically (where the spec's "never leaks" promise actually lives):

- Text-habit `value_text` with PII → em-dash, never the text (Luna's test + confirmed).
- **Unparsed row** (`category='unparsed'`, `habit_type=None`) with a hostile/PII `raw_message` → never rendered (`recent_logs_metadata` never selects `raw_message` for *any* row shape) — `test_activity_unparsed_row_never_leaks_raw_message`.
- **Malformed/adversarial data**: a `habit_type='numeric'` row that nonetheless has `value_text` populated (SQL only NULLs `value_text` for `habit_type='text'`) — confirmed `_format_activity_value` never reads `value_text` at all, so this can't leak even though the SQL doesn't null it for this shape — `test_activity_non_text_habit_with_stray_value_text_never_renders_it`. Flagging as defense-in-depth worth keeping in mind for any future refactor of that function.
- **Deleted (undone) log**: hidden entirely from `/activity` (not shown with a marker — simply absent), confirmed against a mixed result set — `test_activity_deleted_undo_log_is_hidden_not_shown`. Matches spec-silent-but-consistent behavior (`WHERE deleted_at IS NULL`, same convention as every other aggregation query).
- **Hostile display names** (a real vector — LINE profile `display_name` has no shape restriction, unlike routine names which are regex-restricted to `[a-z0-9_]+`) on both `/audit`'s "Who" and `/activity`'s "User" columns → correctly escaped.
- **Hostile audit `entity`/`new_value`** (the dispatch's own `<img src=x onerror=...>` scenario) → correctly escaped on `/audit`'s Detail column.
- Routine names specifically (`target_nl`/`/routine` old values) traced: `core/routines.py:_NAME_RE` restricts names to `[a-z0-9_]+` at the command layer, so no injection is possible through that path today; `target_set`'s old/new values are always numeric goals, never free text. Both verified safe by code trace, not just test.

### Identity gate composition — confirmed, via the real `PortalServer`

Neither pre-existing file proved this: `test_portal_audit.py` drives `audit.register(app, deps)` directly (no `identity_gate`, by its own docstring); `test_portal_security.py` drives `identity_gate` against synthetic handlers, not the real `/audit`/`/activity` routes. This pass built the actual `PortalServer(modules=[audit_module.register])` and confirmed:

- Header-less `GET /audit` → `403`, response body contains **none** of the underlying audit data (a marker value planted in the DB never appears) and no shell/stylesheet.
- Header-less `GET /activity` → `403`, same.
- Header-less `POST /users/approve` (a route AUDIT doesn't even own) through this composed app → still `403` — the gate is outermost regardless of which page module is mounted.
- Correct `Tailscale-User-Login` header → both routes return `200` with real data — proving the 403s above are a real gate, not a broken/missing route.

**Note for Archi/integration:** `core/portal/server.py:REGISTERED_MODULES` currently contains only `status.register` — USERS/AUDIT/QUOTA are not yet wired into the production `PortalServer` (confirmed by reading `server.py` directly; this matches Luna's own docstring that route registration is "the integration pass's job"). The tests above prove AUDIT's own composition works correctly *when* registered; they do not by themselves prove the integration step has happened yet (it hasn't, per `REGISTERED_MODULES`'s current contents) — that's `test_portal_integration.py`'s job once all four modules land.

### `source="portal"` vocabulary — confirmed composed

Luna flagged in `IMPL-PORTAL-audit.md` that `core/audit.py:SOURCES` didn't yet include `"portal"` at build time, with a concurrent USERS-track fix expected. Verified directly: `core/audit.py:137-138` now reads `Literal["command", "nl", "button", "admin", "system", "portal"]` / `SOURCES = (..., "portal")`. Composed end-to-end: a `source="portal"` audit row renders its tag verbatim (`>portal<`) on `/audit`, exactly as before the vocabulary fix (this module's render path never depended on the closed vocabulary — `source` is always shown verbatim, unlocalized).

## Pagination — off-by-one boundaries (the dispatch's specific ask)

Luna's own clamp/malformed-page tests only ever used ≤5-row datasets, where "clamp to page 1" and "clamp to the actual last page" are indistinguishable (both are page 1). Added, all PASS:

- `page=0`, `page=-1`, `page=abc` against a **120-row (3-page)** dataset → land on page 1 specifically, with correct content (not just `200 OK`).
- `page=99999` against the same 120-row dataset → clamps to **page 3** (the real last page, showing the oldest 20 rows), not page 1 — this is the off-by-one Archi's dispatch specifically flagged as worth checking and it was **not** actually proven by the existing suite until now.
- Exactly `PAGE_SIZE` (50) rows → single page, **both** pager controls absent, all 50 rows on the one page (no phantom empty page 2).
- Exactly `2 × PAGE_SIZE` (100) rows → exactly 2 pages, page 2 shows the oldest 50 with Newer present / Older suppressed.
- `page=99999` against a **totally empty** audit_log → the spec's empty state (`"No changes recorded yet."`), no pager, no table — not an error, not a stray pager row.
- Page-1 Newer suppression verified at the **label-text level**, not just the href (`"Newer"` string absent entirely, not merely un-linked) — matches Iris's UI.md §3.21 contract precisely.

## i18n — both languages, including column headers specifically

Luna's suite checks Thai/English page *heading*/chrome; this pass checked the `<th scope="col">` **column header text itself** in Thai (`เวลา`/`ใคร`/`อะไร`/`รายละเอียด`/`แหล่งที่มา` for `/audit`; `เวลา`/`ผู้ใช้`/`กิจกรรม`/`ค่า`/`แหล่งที่มา` for `/activity`), the Thai empty state + privacy note on `/activity`, and the Thai pager label (`เก่ากว่า` present / `ใหม่กว่า` absent on page 1). All PASS.

## Regressions detected

None. Full targeted regression run (`test_portal_audit.py`, `test_portal_audit_gaps.py`, `test_portal_db/deploy/integration/layout/security/server/stats.py`, `test_i18n.py`, `test_audit.py`, `test_audit_capture.py`, `test_audit_capture_gaps.py`, `test_access.py`, `test_config.py`, `test_undo_ui.py` — 393 tests total): **392 passed, 1 failed** (the intentional MAJOR FINDING proof above; not a regression, a discovered pre-existing issue).

Not in scope / not mine: the QUOTA-track's `channel.send` call-count regression (2 failures in `test_riders.py`/`test_refactor_s2_verify.py` per Luna's own IMPL note) is a different parallel module's concern, untouched by this run.

## Recommendation

**Escalate to Archi — spec gap discovered** (the diary-content-via-undo leak, R-AUDIT-3's rationale is factually contradicted by an existing, tested, out-of-module-scope capture-site behavior). This is not a "hand back to Luna" situation — the fix belongs to `core/undo_ui.py`/`core/audit.py` (a different, pre-existing shared module, not `core/portal/audit.py`), and is a judgment call on disposition (fix the recorder now vs. accept-and-document) that only Archi/the user should make given it also affects the pre-existing chat `/audit` command.

**Everything within AUDIT's own file ownership (AC22, AC23, AC24, AC25) is otherwise READY TO SHIP** — 58/59 new+existing tests pass, zero regressions, the custom-habit gap is a documented low-severity cosmetic note (not blocking), and identity-gate composition is verified end-to-end.

## Files

- `C:\Users\Demo\OneDrive - Ngow Hock Agency Co,Ltd\Claude-Cowork\Messaging-line\tests\test_portal_audit_gaps.py` (new, 27 tests)
- `C:\Users\Demo\OneDrive - Ngow Hock Agency Co,Ltd\Claude-Cowork\Messaging-line\tests\test_portal_audit.py` (Luna's, unmodified, 32 tests, still green)
- `C:\Users\Demo\OneDrive - Ngow Hock Agency Co,Ltd\Claude-Cowork\Messaging-line\src\habit_assistant\core\portal\audit.py` (under test, not modified)
- `C:\Users\Demo\OneDrive - Ngow Hock Agency Co,Ltd\Claude-Cowork\Messaging-line\src\habit_assistant\core\undo_ui.py:161-172` (finding's root cause, out of scope to fix here)
- `C:\Users\Demo\OneDrive - Ngow Hock Agency Co,Ltd\Claude-Cowork\Messaging-line\src\habit_assistant\core\audit_view.py:185-197` (`_detail()`, the shared renderer that surfaces the leak — also out of scope to fix here)
