# Implementation — v1.7.0 `habitdef` track (per-user custom habits: `/addhabit`/`/delhabit`)

## Files changed

| Path | Status | Description |
|---|---|---|
| `src/habit_assistant/core/habitdef.py` | new | `validate_and_normalize` (pure, DB-free, R-V1–R-V5) + `execute_addhabit`/`execute_delhabit` (R-C1/R-C2) |
| `src/habit_assistant/core/commands.py` | modified (disjoint keys) | `Command.fields: dict[str,str] \| None` (new field); `_parse_addhabit_fields`, `_match_addhabit`, `_build_delhabit_th_pattern`, `_match_delhabit`; wired into `dispatch()`; three stale "not yet matched"-style comments updated for accuracy |
| `src/habit_assistant/core/i18n.py` | modified (disjoint keys) | 22 new `addhabit_*`/`delhabit_*` catalog entries (EN+TH) |
| `tests/test_habitdef.py` | new | 101 tests: dispatch adversarial corpus, `validate_and_normalize` unit tests, `execute_addhabit`/`execute_delhabit` integration tests, AC-H4/AC-H6 |

I did not touch `main.py`, `storage/*`, `core/registry_provider.py`, `core/habits.py`, `core/audit.py`/`audit_view.py`, or `config.py`/`config.toml` — all shared surface, already in place per `IMPL-v1.7-shared.md`. I also did not touch `tests/test_v17_isolation_sweep.py` or `TEST-v1.7-sweep.md` — the `sweep` track's own files, which I observed appear mid-session (parallel dispatch) and left alone.

## How it works

`core/commands.py:dispatch()` recognizes `/addhabit <pipe key=value>` (Thai alias `เพิ่มนิสัย`) and `/delhabit <id>` (Thai alias `ลบนิสัย`), producing a `Command(kind="addhabit", fields={...})` or `Command(kind="delhabit", category=<id>)` — pure shape recognition, no validation. `core/habitdef.py:validate_and_normalize` is a pure function (registries in, normalized row or `(msg_id, kwargs)` error out) that enforces R-V1–R-V5 against the caller's *current* per-user registry; `execute_addhabit`/`execute_delhabit` are the DB-touching wrappers around it — validate (or look up), write via the shared-surface `db.add_user_habit`/`archive_user_habit`/`delete_user_habit`, call `provider.invalidate(user_id)` so the very next `provider.for_user()` call rebuilds that user's registry with no restart, record one fail-open `core/audit.py` row, and return a bilingual confirmation. `/delhabit`'s smart-delete branch is decided by `db.count_logs_for` (archive if any history exists, else hard-delete, freeing the id).

Two design points worth flagging explicitly:
- **Thai-alias false-positive discipline (Archi's explicit instruction)**: `/delhabit`'s Thai alias `ลบนิสัย` is **registry-anchored** (built from the live registry's ids/Thai labels, exactly like `_build_remind_th_pattern`/`_build_history_th_pattern`) — this works because a habit being *deleted* already exists in the registry. `/addhabit`'s Thai alias `เพิ่มนิสัย` has no such anchor (nothing exists yet to create against), so it uses the equivalent established strategy for that case — a strict **grammar-shape whitelist** (every `|`-segment must contain a bare `key=`), the same kind of mitigation `_QUIET_TH_VALUE_RE` already uses for `เงียบ`. Both are exercised by an adversarial corpus in `test_habitdef.py` (glued prose, spaced prose with no `=`, bare triggers, "ลบ" vs "ลบนิสัย" non-collision with undo).
- **AC-H6 (`/habits` lists custom habits) needed zero new code.** `core/discoverability.py:build_habits_overview` already iterates whatever `HabitRegistry` it's handed — since the shared surface's `HabitRegistry.for_user` already merges base + active custom habits, passing that per-user registry through is the entire mechanism. I proved this with an integration test rather than modifying `discoverability.py` (out of my file scope; and modifying it wasn't necessary).

## Smoke test done

1. Direct production-code script (not mocked): built a temp DB, a real `RegistryProvider`, exercised the full `/addhabit id=reading|type=duration|en=reading|th=อ่านหนังสือ|unit=min/นาที|goal=30` example from SPEC-v1.7.md §3.1 end-to-end — confirmed the reply is **byte-identical** to the spec's own illustrative text; then duplicate-id rejection, reserved-word rejection, hard-delete (no logs), soft-archive (with logs), archived-id-stays-reserved re-add rejection, and the resulting `audit_log` action sequence (`habit_create`/`habit_archive`/`habit_delete`/`habit_create`). All assertions passed.
2. A second script proved AC-H4 (unit collision excluded from `units.build_unit_lookup` after creation, creation still allowed) and AC-H6 (`/habits` overview includes the custom habit) and R-V5 (21st habit rejected with the cap message, exactly 20 stay active).
3. A third script ran the full adversarial Thai-alias corpus directly against `commands.dispatch` (glued prose, bare triggers, spaced-but-not-key=value tails) — zero false positives, and confirmed "ลบนิสัย" never collides with undo's own "ลบ" trigger.
4. `python -m py_compile` on all three touched/new production files.
5. `PYTHONPATH=src .venv\Scripts\python.exe -m pytest tests/test_habitdef.py -q` → **101 passed**.
6. Full suite: `PYTHONPATH=src .venv\Scripts\python.exe -m pytest -q` → **3200 passed, 0 failed, 1 skipped, 1 xfailed** (baseline was 3078/0/1/1xf; delta is my 101 tests plus the `sweep` track's own tests that landed in parallel).
7. Did not touch `data/habits.db` — every check used a fresh temp-directory DB via `tmp_path`/`tempfile.mkdtemp()`.

## Maps to acceptance criteria

- **AC-H1** (create) → `core/habitdef.py:execute_addhabit` + `_build_addhabit_confirmation`; `tests/test_habitdef.py::test_execute_addhabit_creates_row_and_confirms_bilingually` (asserts byte-identical to the spec's own example reply) + `test_execute_addhabit_appears_in_the_users_registry_immediately_ac3`.
- **AC-H2** (validation) → `core/habitdef.py:validate_and_normalize`; ~30 parametrized tests covering id normalization/shape/reserved/shadow/duplicate, type/unit/goal rules, and the cap — each paired with a "no write" assertion at the `execute_addhabit` layer (`test_execute_addhabit_*_no_write` tests).
- **AC-H3** (label/id collision safety) → `_word_reserved` (id and both labels checked against `commands.reserved_trigger_words()`); `test_validate_rejects_id_equal_to_a_reserved_trigger_word`, `test_validate_rejects_label_equal_to_a_reserved_trigger_word_{en,th}`, `test_execute_addhabit_rejects_reserved_word_id_no_write_ac_h3`. Regex-metacharacter safety → `test_validate_a_label_with_regex_metacharacters_is_accepted_and_safe` (builds a live Thai-alias pattern from the accepted label and proves it doesn't raise and doesn't misfire).
- **AC-H4** (unit collision degrades) → `test_addhabit_colliding_unit_is_excluded_from_preparse_lookup_ac_h4` (creation allowed, `units.build_unit_lookup` excludes the colliding token) + `test_addhabit_non_colliding_unit_preparses_normally` (control case).
- **AC-H5** (delete semantics) → `core/habitdef.py:execute_delhabit`; `test_execute_delhabit_hard_deletes_when_no_logs_ac_h5`, `test_execute_delhabit_soft_archives_when_it_has_history_ac_h5`, `test_execute_delhabit_soft_archive_counts_a_previously_undone_entry_too` (an undone-but-logged entry still counts as history), `test_execute_delhabit_already_archived_is_not_found_not_re_archived`.
- **AC-H6** (`/habits`) → no new production code needed (see "How it works" above); `test_habits_overview_lists_custom_habit_for_owner_and_not_for_other_user` + `test_habits_overview_omits_an_archived_custom_habit`.

## Known limitations

- **`main.py` is not wired up.** `/addhabit`/`/delhabit` are fully implemented and tested at the `commands.dispatch()` → `execute_addhabit`/`execute_delhabit` layer, but nothing routes them from a live inbound Telegram message yet, and they aren't in the `set_my_commands` menu. Per SPEC-v1.7.md §11, this is the **integration step**, explicitly listed as happening *after* both the `habitdef` and `sweep` tracks complete — not part of either track's own file ownership. Whoever runs integration needs: route `Command.kind in ("addhabit", "delhabit")` to `habitdef.execute_addhabit`/`execute_delhabit` in `handle_inbound_message` (passing `provider`, `config`, the base registry, resolved `lang`, and the acting `user_id`), and add `/addhabit`/`/delhabit` to the bot command menu (the `help_addhabit_cmd`/`help_delhabit_cmd` i18n keys already exist from the shared surface but aren't yet referenced by `discoverability.build_help_text`).
- **`/edithabit` is out of scope**, per SPEC-v1.7.md §10 — not implemented, as instructed.
- **Alias grammar (`alias=tok:mult,...`) is implemented but has no dedicated AC** — SPEC-v1.7.md §2.1 lists it as part of the grammar; I parse and store it (tested: `test_validate_parses_alias_grammar`, malformed-alias rejection), but no acceptance criterion specifically exercises it beyond that.
- **Unit-collision note in the confirmation message**: SPEC-v1.7.md §4 R-V4 says the confirmation "may note" a unit collision — I did not add that note (spec says "may", not "must"; AC-H4 only requires the collision to degrade safely at the preparse layer, which it does). Flagging in case Vera or Archi wants it added.

No Luna↔Vera round has happened yet for this track — this is the first handoff.
