# Implementation — Rich menu rewire: two direct-log postback cells

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/channels/line.py` | Modified | `_default_rich_menu_payload()` rebuilt for the new 3x2 layout: top row leads with two POSTBACK direct-log cells (`log:water:250`, `log:stretch:10`), `/habits` moves into the top row's third slot, `/heatmap`/`/wrapped`/`/help` fill the bottom row. `/log` and `/guide` no longer have a cell. Docstring rewritten to document the new grammar, the `displayText` choice, and the Iris hand-off. |
| `tests/test_line_channel.py` | Modified | Added `test_default_rich_menu_payload_pins_the_rewired_cells_actions_and_order` — pins exact per-cell bounds/action/order (the existing `create_body == _default_rich_menu_payload()` check is self-referential and would pass on a wrong redesign). |
| `tests/test_line_d_gaps.py` | Modified | Rewrote `test_richmenu_button_commands_are_real_dispatchable_commands` to split by action type: message-action cells keep the original dispatch+README cross-check; the two postback cells get their own real check — `data` parsed against `quicklog._LOG_CALLBACK_RE`, then resolved against the base `HabitRegistry` exactly like `handle_log_callback` does before writing. |
| `tests/test_deploy_line.py` | Modified | `test_richmenu_readme_documents_the_current_design_tokens_cells_and_regeneration` was doing `area["action"]["text"]` unconditionally, which now `KeyError`s on the two postback cells (no `text` key). Fixed to skip non-message actions — the README's own "six cells" table is Iris's next pass, not re-derived here. |
| `tests/test_line_integration.py` | Modified | Added 5 new end-to-end tests (section "9.") through the REAL webhook: happy-path water tap (non-owner/MEMBER), happy-path stretch tap (OWNER), undo-quickreply parity, pending-user gate pin, first-contact-stranger gate pin. |

No changes to `assets/richmenu/*` (README, `generate_richmenu.py`, `richmenu.png`) — that is explicitly Iris's next pass; see the intentional break and hand-off table below.

## How it works

`_default_rich_menu_payload()` still builds one flat `POST /v2/bot/richmenu` JSON body: a 2500x1686 canvas, 3x2 grid, same cell math (`w // 3`, `h // 2`) as before. The only change is per-cell `action`: cells 0/1 are now `{"type": "postback", "data": "log:<habit>:<value>", "displayText": "..."}` instead of `{"type": "message", "text": "/log"}`; cells 2-5 are unchanged message actions, just reordered. A tap on either new cell arrives at LINE's webhook as an ordinary `postback` event, which `channels/line_webhook.py:LineWebhookServer.process_event` already routes to `on_callback(user_id, data, "", pseudo_id)` (unchanged — the routing plumbing needed zero code changes; the `data.startswith("log:")` grammar already existed for the `/log` keyboard). `core/routing.py:on_callback` gates on `access.classify(...) in ("owner", "active")`, then dispatches to `quicklog.handle_log_callback`, which re-validates `data` against `_LOG_CALLBACK_RE`, resolves the habit against the tapping user's own registry, writes the log row, and sends the same confirmation+undo+dashboard-refresh reply a typed log or a `/log` keyboard tap gets. Nothing downstream of the webhook needed to change — the rich-menu rewire is entirely a payload-shape change plus the tests that pin it.

## Smoke test done

- `pytest tests/test_line_channel.py tests/test_line_d_gaps.py tests/test_deploy_line.py -q` → **74 passed, 3 skipped** (the 3 skips are pre-existing/unrelated, e.g. the systemd-analyze test that needs a tool this Windows box doesn't have).
- `pytest tests/test_line_integration.py -q` → **21 passed** (includes the 5 new rich-menu postback tests, run through the REAL webhook: signed HTTP POST → `LineWebhookServer` → FIFO worker → `core/routing.py:on_callback` → `quicklog.handle_log_callback` → real SQLite `Database` → one reply call recorded via `httpx.MockTransport`).
- `pytest tests/test_line_webhook.py tests/test_line_a_gaps.py tests/test_line_v12_gaps.py tests/test_quicklog.py tests/test_v18_quicklog_gaps.py -q` → **229 passed** (unaffected-but-adjacent suites, confirming no collateral breakage).
- **Full suite**: `pytest -q` → **5683 passed, 4 skipped, 1 xfailed, 0 failed** (275.6s).
- Ran `python assets/richmenu/generate_richmenu.py -o <tmp>` directly (not part of pytest — confirmed via grep that no test imports/calls `generate_richmenu.verify_against_code`): fails exactly where expected, **exit code 1**, `"Cell 0: code sends None, artwork is labelled '/log'"`, before any PNG is written or the contrast check runs. This is the one intentional break — see "Known limitations" and the hand-off table below.
- `git status --short` confirms only the 5 files above changed; `assets/richmenu/*` untouched.

## Maps to acceptance criteria (from the dispatch)

- **Remove `/log`/`/guide` cells; add two DIRECT-LOG cells on the top row; rearrange for thumb reach** → `src/habit_assistant/channels/line.py:_default_rich_menu_payload`. Pinned in `tests/test_line_channel.py:test_default_rich_menu_payload_pins_the_rewired_cells_actions_and_order`.
- **Postback data `log:water:250` / `log:stretch:10`, verbatim quick-log grammar, bilingual-sensible Thai-primary `displayText`** → same function; `displayText` is `"💧 250ml"` / `"💪 10 นาที"` per the dispatch's own suggestion. Regex/registry-validity pinned in `tests/test_line_d_gaps.py:test_richmenu_button_commands_are_real_dispatchable_commands`.
- **Four remaining cells stay message actions with existing texts** → same function (`/habits`, `/heatmap`, `/wrapped`, `/help`, byte-identical text, just reordered). Pinned in the same `test_line_channel.py` test.
- **Verify the postback path end-to-end through the REAL webhook**:
  - Logs 250 water / 10 stretch for the tapping user, honest confirmation → `test_rich_menu_water_direct_log_postback_logs_and_confirms_for_a_non_owner_active_user`, `test_rich_menu_stretch_direct_log_postback_logs_and_confirms_for_the_owner` (`tests/test_line_integration.py`).
  - Works for a non-owner active user too → the water test above uses `MEMBER` (non-owner); the stretch test uses `OWNER` — both classes covered.
  - Undo/dashboard parity with a typed log → `test_rich_menu_direct_log_postback_carries_the_same_undo_quickreply_as_a_typed_log`.
  - Unknown/pending-user postback → access gate handles it, pinned (not crash, not a log write) → `test_rich_menu_postback_from_a_pending_user_pins_the_actual_gate_behavior`, `test_rich_menu_postback_from_a_first_contact_stranger_is_also_a_silent_no_op`. **See "Known limitations" — the actual pinned behavior is a silent no-op, not the "polite gate reply" the dispatch's parenthetical named as an expectation.**
- **Do NOT touch `assets/richmenu/*`; end report with Iris's exact cell-spec table** → done, table below. `git status --short` confirms zero changes under `assets/richmenu/`.
- **Exit bar: LINE-relevant subset green except the generator/PNG assertions intentionally broken** → confirmed above; the one break is `generate_richmenu.py`'s own `verify_against_code()`, which is **not** wired into pytest at all (grepped — nothing imports or calls it), so it does not affect the automated gate. `tests/test_deploy_line.py`'s README cross-check was the one pytest-level test that would have hard-crashed on the payload change (`KeyError`, not a clean assertion) — fixed to skip non-message cells rather than left broken, since it lives in `tests/` (mine to own), not `assets/richmenu/` (Iris's).

## Known limitations / flagged for Archi

**The pending/unknown-user postback gate does NOT send a polite reply — it is a pre-existing, documented silent no-op, now reachable through 2 more cells than before.**

`core/routing.py:on_callback`'s own docstring says: *"A non-active chat's tap is a silent no-op (no onboarding reply — a tap isn't itself a message to onboard from)."* This is shared code for every postback prefix (`undo:`/`routine:`/`clarify:`/`log:`) and predates this rich-menu rewire — I did not change it, and changing it is a bigger decision than this task owns (it would affect the undo button, routine buttons, and clarify buttons app-wide, not just the two new rich-menu cells).

Before this rewire, ALL SIX rich-menu cells were message actions, so a pending/blocked/unknown user tapping ANY cell went through `access.handle_gate` (the text-message gate), which DOES reply politely (`access_pending`/`access_denied`) and, for a first-contact user, creates a pending row and alerts the owner. After this rewire, the two new direct-log cells are postback actions, routed through `on_callback`'s gate instead — which does none of that: no reply, no pending-row creation, no owner alert. The one hard guarantee that still holds (and is what I pinned in tests) is that it's inert, not a crash and not an unauthorized log write.

I flagged rather than silently "fixed" this because: (1) the dispatch's own parenthetical assumed a "polite gate reply" that the real code doesn't produce — a genuine spec-vs-code mismatch worth a decision, not a guess; (2) the fix, if wanted, belongs at the shared `on_callback` gate, not scoped to these two cells, so it's a separate piece of work with its own blast radius (undo/routine/clarify buttons too).

**Recommendation for Archi**: decide whether `on_callback`'s gate should grow a polite reply for a non-active tap (mirroring `handle_gate`'s), as a follow-up ticket — not bundled into this rich-menu rewire.

## For Iris — exact cell-spec table (next pass)

Nothing under `assets/richmenu/` was touched. Running `generate_richmenu.py` today fails immediately at `verify_against_code()`, cell 0 (`"code sends None, artwork is labelled '/log'"`) — it doesn't understand a postback cell (`area["action"].get("text")` is `None`) and its own `CELLS` tuple/icon set/README table all still describe the OLD 6 message-action cells. Everything below is what her pass needs to update; the runtime payload (`_default_rich_menu_payload()`) is now the source of truth she should verify against, same as before.

| Cell (L→R, T→B) | Bounds (x, y, w, h) | Action type | Payload content | Suggested icon intent | Notes |
|---|---|---|---|---|---|
| 1 | 0, 0, 833, 843 | **postback** | `data: "log:water:250"`, `displayText: "💧 250ml"` | Water droplet / glass, filled (this is now the primary CTA slot — matches the old `/log` solid-icon treatment) | No `text` field — `verify_against_code()` needs a new branch for postback cells (compare `data`, not `text`) |
| 2 | 833, 0, 833, 843 | **postback** | `data: "log:stretch:10"`, `displayText: "💪 10 นาที"` | Stretch figure or a "+10" badge variant of the old stretch motif | Same `verify_against_code()` note as cell 1 |
| 3 | 1666, 0, 833, 843 | message | `text: "/habits"` | Checklist, top item done (unchanged from the old design) | Same icon/label as before — just moved from slot 2 to slot 3 |
| 4 | 0, 843, 833, 843 | message | `text: "/heatmap"` | 4x4 heat grid (unchanged) | Moved from slot 3 to slot 4 |
| 5 | 833, 843, 833, 843 | message | `text: "/wrapped"` | Rising bar chart (unchanged) | Same slot as before (4) — no visual change needed here beyond the neighbors shifting |
| 6 | 1666, 843, 833, 843 | message | `text: "/help"` | Circled question mark (unchanged) | Moved from slot 5 to slot 6 |

Also needs updating in her pass:
- `assets/richmenu/generate_richmenu.py`'s `CELLS` tuple (currently 6 `(command, thai_label, icon)` triples assuming every cell has a `command` string) and `verify_against_code()` (currently reads `area["action"].get("text")` unconditionally — needs an `action["type"] == "postback"` branch that checks `data`/`displayText` instead, and probably a new Thai on-menu label per new cell since `displayText` is chat-only text, not what's drawn on the button image itself).
- Two new icons for the direct-log cells (a plain "/log" plus-in-a-disc no longer fits two distinct habit-specific actions).
- `assets/richmenu/README.md`'s "The six cells" table (still lists the old `/log`/`/guide` cells and doesn't mention the two new postback cells at all) and its "Each area's action is... not a postback" line under "Constraints for any future redesign" (now inaccurate — two cells ARE postback actions).
- `richmenu.png` regenerated once the above lands; `tests/test_deploy_line.py::test_richmenu_readme_documents_the_current_design_tokens_cells_and_regeneration` and `tests/test_line_d_gaps.py::test_richmenu_button_commands_are_real_dispatchable_commands` are both written to automatically pick up whatever the README documents once she updates it (format-tolerant substring checks, not hardcoded).
