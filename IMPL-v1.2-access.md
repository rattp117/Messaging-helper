# Implementation — v1.2.0 Multi-user support (module `access`)

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/core/access.py` | Created | `classify`, `handle_gate`, `execute_admin` — the access-control gate, onboarding flow, and owner-only admin commands (R-A1-R-A5) |
| `src/habit_assistant/core/commands.py` | Modified | Added `_match_access` (recognizes `/start`, `/users`, `/approve`, `/block`, `/invite`) and wired it into `dispatch()`, right after the `remind` check and before `help`/`habits`. Module docstring extended with the same "v1.2.0 module `access`" note the other two parallel modules added for their own kinds. |
| `src/habit_assistant/core/i18n.py` | Modified | Filled the `access` key-block skeleton with 10 catalog entries: `access_pending`, `access_denied`, `access_request`, `access_granted`, `start_welcome`, `admin_usage`, `admin_save_failed`, `admin_approved_ack`, `admin_blocked_ack`, `users_list_header`, `users_list_line` |
| `tests/test_access.py` | Created | 37 tests covering `commands.dispatch`'s five new kinds, `classify`, `handle_gate`, `execute_admin`, and two end-to-end composition tests |

`core/commands.py` and `core/i18n.py` are the two shared files all three parallel modules (`access`, `preferences`, `schedules`) touch, per SPEC-v1.2.md §11. I only added to the sections/kinds this module owns — I never touched the `preferences`/`schedules` key-block markers or their kinds, and vice versa (confirmed by re-reading both files after each edit; both modules' work is visible alongside mine and all three compose cleanly — full suite is green, see below). I did **not** touch `main.py`, `channels/base.py`, or `channels/telegram.py` — out of scope per the dispatch instructions.

## How it works

Every inbound update is meant to pass through `access.handle_gate(...)` **before** any logging/LLM/command work (R-A1) — that wiring itself lands at the later integration step (see "Integration wiring" below), not in this pass. `handle_gate` calls the pure, fail-safe `classify(db, chat_id)` (owner/active/pending/blocked/unknown, catching any DB read error and returning `"blocked"` rather than ever risking a granted access — AC-A7). `owner`/`active` return `True` immediately (proceed, gate is a no-op) with zero sends. `unknown` creates a `pending` `users` row, replies `access_pending` to the asker, and notifies the owner with `access_request` (naming the chat id and the exact `/approve` command) — resolved in the owner's *own* stored language via `i18n.resolve_unprompted_language`, since that notification is unprompted from the owner's point of view even though it's a reply-shaped send to the asker. `pending`/`blocked` reply `access_pending`/`access_denied` respectively and return `False` either way — the caller must not log the message or call the LLM for a `False` result, which `handle_gate` naturally guarantees simply by never touching `logs` or the LLM client itself.

`execute_admin` is the seam for `command.kind in ("start", "approve", "block", "users", "invite")`, called only *after* `handle_gate` has already returned `True` for the acting chat (so it's known active/owner already) — that's why `"start"` only needs the "active user → welcome" branch here; the unknown/pending/blocked `/start` branches are already fully covered by `handle_gate` itself, since `/start` from a non-active chat never reaches command dispatch at all. The four true admin kinds (`approve`/`block`/`users`/`invite`) re-check `classify(db, chat_id) == "owner"` independently inside `execute_admin` (belt-and-suspenders — active does not imply owner), and no-op silently for anyone else (AC-A4). `/approve` and `/invite` are literally the same code path (R-A4's own "alias of `/approve`"). A `target_chat` that fails `^-?\d+$` (missing or malformed) gets the `admin_usage` reply instead of being acted on.

## Smoke test done

Ran `pytest tests/test_access.py -q` in isolation first (37/37 passed), then `pytest tests/test_commands.py tests/test_i18n.py tests/test_i18n_literals.py -q` to confirm the shared-file edits didn't regress the existing command-dispatch/catalog-integrity suites (133/133 passed), then the full suite twice as the `preferences`/`schedules` tracks landed concurrently — both runs green (final: **1114 passed, 1 skipped, 0 failed**, ~99s; baseline before any v1.2 module work was 976 passed / 1 skipped).

Also ran a standalone, not-committed smoke script (`.venv\Scripts\python.exe`, scratch `tempfile.TemporaryDirectory()` SQLite path — never `data/habits.db`) exercising the full onboarding → block → approve → non-owner-invisible → `/users` → `/start` sequence end to end through `access.classify`/`handle_gate`/`execute_admin` and `commands.dispatch`, with real bilingual output observed:
```
[OK] 1. unknown -> pending row + gated off
     stranger heard: 👋 Hi! This is a private habit bot. I've asked the owner to approve you — you'll hear back soon.
     owner heard: 🔔 Dana (แชท 999888) ขอสิทธิ์เข้าใช้งาน อนุมัติด้วย: /approve 999888
[OK] 2. blocked -> denied, not processed
[OK] 3. approve -> active, subsequently gates through
[OK] 4. non-owner admin command -- invisible no-op
[OK] 5. /users listing:
👥 Users:
• 1574572064 — owner · active · lang auto
• 999888 — member · active · lang auto
[OK] 6. /start welcome: 👋 Welcome back! Send /help to see everything I can do.

ALL SMOKE CHECKS PASSED
```
This confirms the owner's `access_request` notification correctly resolves to *their own* stored language (Thai, the default `config.i18n.primary_language`) independent of the language the stranger's own reply used (English, as passed by the caller) — the two-different-recipients-two-different-languages case R-P1/R-A2 imply but no unit test exercises verbatim.

## Maps to acceptance criteria

- **AC-A1** (unknown → pending, `access_pending` + owner `access_request`, message neither logged nor LLM'd) → `core/access.py:handle_gate` (the `access == "unknown"` branch); `tests/test_access.py::test_handle_gate_unknown_creates_pending_and_notifies_owner`, `::test_handle_gate_unknown_no_display_name_falls_back_to_chat_id`. "Neither logged nor LLM'd" is structural: `handle_gate` never imports/calls the LLM client or `db.insert_log` — a `False` return is the caller's (future integration) signal to skip `handle_inbound_message` entirely.
- **AC-A2** (`/approve` → active, `access_granted`, can log normally) → `core/access.py:execute_admin` (`approve`/`invite` branch); `tests/test_access.py::test_execute_admin_approve_grants_access_and_notifies_target`, `::test_end_to_end_owner_approves_a_stranger_who_can_then_proceed`.
- **AC-A3** (`/block` → blocked, next message `access_denied`, not processed) → `execute_admin`'s `block` branch + `handle_gate`'s `blocked` branch; `tests/test_access.py::test_execute_admin_block_revokes_access`, `::test_handle_gate_blocked_chat_denied`.
- **AC-A4** (non-owner admin command → not executed, invisible) → `execute_admin`'s `classify(db, chat_id) != "owner"` early return (no reply, no write); `tests/test_access.py::test_execute_admin_admin_commands_invisible_to_non_owner`.
- **AC-A5** (`/users` → role + status listing) → `core/access.py:_render_users_list`; `tests/test_access.py::test_execute_admin_users_lists_everyone` (matches SPEC-v1.2.md §3.3's exact shape, incl. the pending row's missing `· lang` suffix).
- **AC-A6** (`/start`: active → welcome, unknown → pending flow) → `execute_admin`'s `start` branch (active) + `handle_gate` (unknown, since `/start` never reaches dispatch for a non-active chat); `tests/test_access.py::test_execute_admin_start_active_user_gets_welcome`, `::test_end_to_end_start_from_unknown_runs_pending_flow`.
- **AC-A7** (fail-safe: lookup error → not active, never granted) → `core/access.py:classify`'s `except Exception: return "blocked"`; `tests/test_access.py::test_classify_fails_safe_on_lookup_error`, `::test_handle_gate_fails_safe_on_lookup_error`.

All 7 owned ACs pass.

## Integration wiring (for the integration step — I did not touch `main.py`)

Per SPEC-v1.2.md §11 step 1, `main.py`'s `on_message` closure (around line 1164) needs, in order:

```python
async def on_message(chat_id: str, text: str) -> None:
    lang = i18n.resolve_reply_language(text, config)
    display_name = None  # see "Known limitations" #1 below
    proceed = await access.handle_gate(
        db, channel, config, secrets.telegram_chat_id, chat_id, display_name, text, lang=lang
    )
    if not proceed:
        return

    command = commands.dispatch(text, registry)
    if command is not None and command.kind in ("start", "approve", "block", "users", "invite"):
        await access.execute_admin(
            command, db=db, channel=channel, config=config,
            owner_chat_id=secrets.telegram_chat_id, chat_id=chat_id, lang=lang,
        )
        return

    await handle_inbound_message(
        text, db=db, llm=llm, channel=channel, config=config, user_id=chat_id,
        health_monitor=health_monitor, registry=registry, scheduler=scheduler, reminder_state=reminder_state,
    )
```

**This ordering is load-bearing, not stylistic**: `handle_inbound_message` calls `commands.dispatch` a second time internally, but it only recognizes `query`/`snooze`/`target`/`help`/`habits` explicitly — every *other* non-`None` `Command.kind` (including all eight v1.2 kinds: `start`/`approve`/`block`/`users`/`invite`/`lang`/`quiet`/`remind`) falls through its final `if command.kind == "undo": ... else: _execute_edit(...)` branch and would be silently mis-handled as an **edit** command. The integrator must intercept `start`/`approve`/`block`/`users`/`invite` (and, by the `preferences`/`schedules` modules' own IMPL notes, `lang`/`quiet`/`remind`) *before* calling `handle_inbound_message`, exactly as above — never let those eight kinds reach it.

`secrets.telegram_chat_id` is the owner id, already in scope in `async_main` (used identically at line 934's `attribute_legacy_to_owner` call and the `HealthMonitor` construction) — no new plumbing needed to obtain it.

## Known limitations

1. **`display_name` is unavailable at the call site as delivered by the shared surface.** SPEC-v1.2.md §2.1 lists `update.message.from.first_name` as available inbound data, and `handle_gate`'s signature (§5) takes `display_name: str | None` to capture it (R-A2). But the shared surface's `Channel.run` ABC (`channels/base.py`) and `TelegramChannel.run()` (`channels/telegram.py`) only thread `(chat_id, text)` into `on_message` — `TelegramChannel.run` reads the full Telegram `message` dict (which *does* contain `message["from"]["first_name"]`) but discards everything except `chat_id`/`text` before calling the handler. This does not block any of my 7 owned ACs — every `access.py` function handles `display_name=None` correctly (R-A2's own wording: "capturing `display_name` **when the update provides one**" — explicitly optional), and my smoke test above passes a real name only because I called `handle_gate` directly, not through the channel. It does mean the shipped `/users`/`access_request` output will show the bare chat id instead of a friendly name until a follow-up widens `on_message` to 3-arg `(chat_id, text, display_name)` (a small, additive change to `channels/base.py`/`channels/telegram.py`/`channels/line.py` — outside this module's file ownership, so I flagged it here rather than making it myself). Recommend Archi route this to a fast follow-up rather than blocking the current integration.
2. **`AC-A4`'s "falls through as an unknown message" is implemented as a silent no-op, not a literal fall-through to the LLM parser.** `execute_admin`'s declared return type is `-> None` (SPEC-v1.2.md §5's own interface) — there is no boolean "handled" signal back to the integration wiring to let it re-route to `parse_message` on a non-owner attempt. I judged "no reply, no state change" to satisfy §3.5's "reveals nothing" requirement (arguably *more* silent than a `clarifying_question` reply from the parser, which is also fine since that reply never mentions admin commands either) rather than re-plumbing a signal the spec's own interface doesn't provide. Confirmed by `tests/test_access.py::test_execute_admin_admin_commands_invisible_to_non_owner`. Flagging this interpretation explicitly — happy to add a return value if Archi/Vera want literal parser fall-through instead.
3. **Copy-mapping judgment call for `access_pending`/`access_denied`.** SPEC-v1.2.md §3.2 shows two *different* illustrative texts — one captioned "unknown user, first contact" and one captioned "blocked / not-yet-approved user messaging again" — but R-A2 and R-A3 both literally say "reply `access_pending`" for the unknown-first-contact case *and* the pending-repeat case (one catalog id ⇒ one string via `i18n.t()`, by construction). I resolved this by using the "first contact" text verbatim for `access_pending` (reused for both unknown-first-contact and pending-repeat, since it reads fine either way) and the "not-yet-approved" text verbatim for `access_denied` (blocked). This satisfies R-A3's requirement of two *distinct* ids/replies for pending vs. blocked while staying faithful to both example texts given. Not a spec contradiction requiring escalation — just recording the reasoning.
4. **Added an owner-facing acknowledgment for `/approve`/`/block`/`/invite`** (`admin_approved_ack`/`admin_blocked_ack`) beyond what SPEC-v1.2.md §3.2/R-A4 literally describe (which only mention the *target* chat's `access_granted`, not any reply to the owner). Every other command in this codebase (undo, edit, target, snooze) confirms back to its caller, so a silent owner experience felt like a UX gap, not a deliberate spec choice. Purely additive — doesn't change any AC's pass/fail — but noting it since it's beyond the letter of R-A4.
5. **No Thai aliases for `/start`/`/approve`/`/block`/`/users`/`/invite`.** The task dispatch mentioned "+ Thai aliases per spec" for these commands, but SPEC-v1.2.md §2.3 only assigns Thai aliases to `/lang` (`ภาษา`), `/quiet` (`เงียบ`), and `/remind` (`เตือน`) — all owned by the other two parallel modules. I followed the spec (source of truth per my own instructions) rather than the paraphrased dispatch note; no Thai alias was added for any of my five commands.

## Iteration log

None yet — first hand-off, all 37 own tests plus the full suite passed on the first run.
