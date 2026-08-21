# Implementation — v1.2.0 Multi-user support (module `preferences`)

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/core/preferences.py` | Created | `execute_lang`/`execute_quiet` — validate a `Command`, write `users.language_pref`/`users.quiet_hours_json`, return a bilingual reply. Never raises. |
| `src/habit_assistant/core/commands.py` | Modified | Added `"lang"`/`"quiet"` shape recognition: `_LANG_SLASH_RE`/`_LANG_TH_RE`, `_QUIET_SLASH_RE`/`_QUIET_TH_RE`, `_match_lang`/`_match_quiet`, wired into `dispatch()` (after `access`'s admin block, before `help`/`habits`). Updated `Command.pref_value`'s field comment and the module/`dispatch()` docstrings to document the new kinds. Did not touch `access`'s or `schedules`'s own code (both had already landed in this file when I started; verified via re-read before each edit). **Iteration (Vera round 1):** hardened `_LANG_TH_RE`/`_QUIET_TH_RE` with a single-token capture + blacklist(`ภาษา`)/shape-whitelist(`เงียบ`) guard. **Iteration (Vera round 2):** replaced the `ภาษา` blacklist with a small curated whitelist (`_LANG_TH_VALID_VALUES`), removed the now-unused `_looks_like_th_prose`/`_TH_PROSE_MARKERS`. See "Iteration log" below for both rounds. |
| `src/habit_assistant/core/i18n.py` | Modified | Filled the reserved "Module `preferences`" catalog section with 7 keys: `lang_set`, `lang_usage`, `quiet_set`, `quiet_cleared`, `quiet_usage`, `quiet_invalid_window`, `preferences_save_failed`. |
| `tests/test_preferences.py` | Created + extended by Vera (2 rounds) | 94 tests total: my original 45 (`dispatch()` shape, `execute_lang`/`execute_quiet` unit behavior, AC-P1/AC-P2 composition) + Vera round 1's 36 (isolation across 3 users, boundary/encoding validation, persistence across a fresh `Database` open, non-`sqlite3` exception fallback, cross-track dispatch precedence, the Thai-alias misfire corpus) + Vera round 2's 13 (the `ภาษา`-blacklist residual-misfire corpus that exposed round 1's remaining gap, plus a `เงียบ` positive control proving its shape-whitelist had no equivalent gap). I did not modify any of Vera's tests in either round — see "Iteration log". |

`main.py` was **not** touched, per my dispatch instructions — see "Maps to acceptance criteria" and "Known limitations" below for the exact wiring integration needs to add.

## How it works

`core/commands.dispatch()` recognizes `/lang [en|th|auto]` (Thai alias `ภาษา <value>`) and `/quiet [HH:MM-HH:MM[,...]|off]` (Thai alias `เงียบ <value>`) purely by shape — anchored regexes, same slash-form/Thai-alias split `/remind` already uses (bare slash form permitted with no value). The slash forms stay fully permissive (any non-empty tail — near-zero false-positive surface, an explicit "/" prefix no normal sentence starts with). The Thai aliases additionally require the value to be a single whitespace-free token AND pass a per-command plausibility check before matching at all: `เงียบ` uses a loose "off"/HH:MM-HH:MM shape whitelist (`_QUIET_TH_VALUE_RE`, unchanged since round 1 — a quiet-hours value has a mechanical shape a language name doesn't); `ภาษา` uses a small curated value whitelist (`_LANG_TH_VALID_VALUES = {"en", "th", "auto", "ไทย", "english"}`, as of round 2 — see below). The raw, lowercased trigger tail that DOES pass shape is carried through unvalidated as `Command.pref_value`. `core/preferences.execute_lang`/`execute_quiet` are where the actual semantics live: `execute_lang` validates the value is one of `en`/`th`/`auto` and calls `Database.set_user_language` (shared surface); `execute_quiet` recognizes `"off"` (writes an explicit empty JSON list `"[]"`, distinct from the NULL "never set/inherit config" state) or parses one-or-more comma-separated `HH:MM-HH:MM` windows (reusing `config._HHMM_RE`, the exact same pattern `QuietHoursConfig` itself validates with) and calls `Database.set_user_quiet_hours` with the JSON-encoded `[[start, end], ...]` list — the exact shape `core/reminders.effective_quiet_windows` (shared surface) already reads. Both functions never raise: a DB write failure is caught, logged, and reported via `preferences_save_failed`; an invalid/missing value never writes anything and returns a usage/invalid-input reply. Because the shared surface already built the *read* side of per-user language (`i18n.resolve_reply_language`/`resolve_unprompted_language`'s `user_pref` parameter) and quiet hours (`core/reminders.effective_quiet_windows`), my module only needed to build the *write* side — the two compose correctly, proven directly in `tests/test_preferences.py` without needing `main.py` wired up at all.

## Smoke test done

Full test file (post Vera-round-2 fix): `.venv\Scripts\python.exe -m pytest -q tests/test_preferences.py` → **94 passed** (my original 45 + Vera round 1's 36 + Vera round 2's 13, including all 4 round-1 and all 6 round-2 previously-failing Thai-alias-misfire cases).

Full suite (never against `data\habits.db`, always `tmp_path`/`tempfile`-based SQLite): `.venv\Scripts\python.exe -m pytest -q` → **1288 passed, 0 failed, 1 skipped** (initial hand-off: 1114/1; Vera round 1 baseline: 1258 passed/4 failed/1 skipped, fixed to 1275/0/1; Vera round 2 baseline: 1282 passed/6 failed/1 skipped, fixed to 1288/0/1 — exactly matching the round-2 target).

Standalone smoke script (not committed, deleted after use, run via `.venv\Scripts\python.exe` against a scratch `tempfile.mkdtemp()`-adjacent temp dir, never `data/habits.db`):
```
[OK] 1. /lang th sets pref + confirms in Thai: ✅ ได้เลย ต่อไปนี้จะตอบเป็น "th" นะ
[OK] 2. owner still auto/English on plain English text
[OK] 3. Thai alias ภาษา en works: ✅ Got it — I'll reply in "en" from now on.
[OK] 4. /quiet sets two windows, owner unaffected: 🌙 Quiet hours set: 22:00-07:00, 12:00-13:00. No reminders will be sent to you during that time.
[OK] 5. /quiet off clears to explicit empty: 🌙 Quiet hours cleared for you — reminders can be sent at any time now.
[OK] 6. malformed window rejected cleanly: 🤔 Each quiet-hours window must look like "22:00-07:00" (24-hour HH:MM). Separate multiple windows with commas, or use "off" to clear.
[OK] 7. adversarial corpus falls through to parser (dispatch -> None)

ALL SMOKE CHECKS PASSED
```
This exercises the exact seam integration will wire up: `commands.dispatch` → `execute_lang`/`execute_quiet` → the shared surface's `effective_quiet_windows`/`resolve_reply_language` reading it straight back.

## Maps to acceptance criteria

- **AC-P1** (language): `commands.dispatch` (`_match_lang`) → `core/preferences.py:execute_lang` → `Database.set_user_language`. Proven end-to-end (write + shared-surface read composing correctly) by `tests/test_preferences.py::test_ac_p1_lang_th_makes_replies_thai_regardless_of_input_language` and `::test_ac_p1_owner_default_auto_is_unaffected_by_member_lang_change`. **Not yet wired into `main.py`** — see "Known limitations" for the exact integration step needed for this to take effect in the live bot.
- **AC-P2** (quiet hours): `commands.dispatch` (`_match_quiet`) → `core/preferences.py:execute_quiet` → `Database.set_user_quiet_hours` → read back via `core/reminders.py:effective_quiet_windows`. Proven by `::test_ac_p2_quiet_hours_scoped_to_the_setting_user_only` and `::test_ac_p2_quiet_off_clears_only_that_users_windows`. The actual reminder-suppression *consumer* of `effective_quiet_windows` (`send_reminder`, shared surface, already landed) needs no further change — it already calls `effective_quiet_windows(db, config, chat_id)` per firing reminder, so a stored override takes effect on the very next tick with no restart.

Both ACs are covered end-to-end at the unit/composition level. Full production behavior additionally requires the integration wiring below (explicitly deferred per my dispatch: "Do NOT touch main.py — routing lands at integration").

## Known limitations — exact integration wiring needed

Not done by me (explicitly out of scope — main.py and other already-landed shared-surface files are integration's job):

1. **Route `command.kind in ("lang", "quiet")` in `main.py:handle_inbound_message`.** Add, alongside the existing `"target"`/`"help"`/`"habits"` branches (around `src/habit_assistant/main.py:549-581`, after the `"target"` branch):
   ```python
   if command.kind == "lang":
       reply = await preferences.execute_lang(command, db=db, lang=lang, user_id=user_id)
       if dry_run:
           print(reply)
           return
       assert channel is not None, "channel is required outside dry-run"
       await channel.send(user_id, reply)
       return
   if command.kind == "quiet":
       reply = await preferences.execute_quiet(command, db=db, lang=lang, user_id=user_id)
       if dry_run:
           print(reply)
           return
       assert channel is not None, "channel is required outside dry-run"
       await channel.send(user_id, reply)
       return
   ```
   Requires `from habit_assistant.core import preferences` added to `main.py`'s imports.

2. **Thread the stored language preference into every `resolve_reply_language`/`resolve_unprompted_language` call site** — this is what actually makes AC-P1 take effect for a user's *other* messages, not just the `/lang` confirmation itself (my `execute_lang` already confirms in the newly-set language on its own, needing no wiring). The shared surface built the `user_pref` parameter but left every call site passing `"auto"` (a no-op) by design (IMPL-v1.2-shared.md's own "Known limitations" #3) — this module's job was the *write* side only. Four call sites need a `user_pref=` argument added, each via `db.get_user(user_id)` (falling back to `"auto"` if the row is somehow missing):
   - `main.py:527` — `handle_inbound_message`: `lang = i18n.resolve_reply_language(text, config, user_pref=(db.get_user(user_id) or {}).get("language_pref", "auto"))`. (`sqlite3.Row` doesn't support `.get`, so use `row["language_pref"] if row else "auto"` in practice.)
   - `main.py:758` — `reparse_pending_unparsed`: same pattern, `user_id` is already in scope from the loop (`row["user_id"]`).
   - `main.py:1083`/`main.py:1140` — `weekly_review_job`/`daily_summary_job`: move `lang = i18n.resolve_unprompted_language(config)` **inside** the `for user_id in db.active_user_ids():` loop and add `user_pref=`.
   - `core/reminders.py:297` — `run_due_reminders`: move `language = i18n.resolve_unprompted_language(config)` **inside** the `for user_id in db.active_user_ids():` loop and add `user_pref=` (this file is shared-surface-owned, not mine to edit, but it's the one remaining per-user fan-out that still resolves language once globally instead of per user).

   `main.py:981` (the `--test-reminder` manual CLI tool) intentionally stays as-is — it's addressed to the owner only (a manual ops tool, not a per-user fan-out), and the owner's default `auto` pref makes it byte-identical regardless.

3. **`/help` should mention `/lang` and `/quiet`** — `core/discoverability.py:build_help_text` currently has no line for either (it predates this module). Not an AC of mine (AC-P1/AC-P2 don't require `/help` coverage), but worth a follow-up so the commands are discoverable — flagging for whoever does the `access`+`preferences`+`schedules` integration pass, since `discoverability` is a separate, already-landed module.

None of the above blocks AC-P1/AC-P2 as I own them (both are proven at the module-composition level per my dispatch's explicit scope), but the bot will not actually honor `/lang`/`/quiet` for a live user until step 1 lands, and a user's *other* messages won't reflect their `/lang` choice until step 2 lands.

## Iteration log

### Round 1 (Vera, `TEST-v1.2-preferences.md`) — FAIL, 4 failures, hand-back

**Failure:** `test_thai_alias_does_not_misfire_on_common_space_separated_phrasing` (4 parametrized cases, all in the same cluster). Realistic, correctly-spelled Thai sentences that happen to *start* with the literal alias word "เงียบ"/"ภาษา" followed by a space and more text were dispatching as `Command(kind="quiet"/"lang", ...)` instead of falling through to the LLM parser — e.g. `dispatch("เงียบ ๆ หน่อยนะ", registry)` (standard *mai-yamok* reduplication, "keep it down, please") returned a `quiet` command instead of `None`.

**Root cause:** `_LANG_TH_RE`/`_QUIET_TH_RE`'s only mitigation was a mandatory `\s+` between the trigger word and the value (`^ภาษา\s+(?P<value>\S.*)$` / `^เงียบ\s+(?P<value>\S.*)$`). That protects only against the trigger glued to more text with *no* space (the threat model my original comment block explicitly claimed) — it does nothing against a legitimate space *followed by* ordinary Thai prose, which is common: "เงียบ"/"ภาษา" are real Thai words ("quiet"/"language") that open plenty of normal sentences, unlike the `/`-prefixed slash forms (near-zero false-positive surface by construction).

**Fix** (`core/commands.py`, my disjoint `_match_lang`/`_match_quiet`/regex section only — did not touch `_match_remind` or any `access`/`schedules` code, per the coordinator's instruction that `schedules`' own Luna was auditing `เตือน` for the identical weakness in parallel):
1. Tightened the Thai-alias value capture group from `\S.*` to `\S+` (one whitespace-free token, not "rest of the line") — this alone rejects any *multi-word* continuation (`"เงียบ ๆ หน่อยนะ"`, `"เงียบ ๆ หน่อย"`) since the regex simply doesn't match past the first space.
2. Added a per-command plausibility check for the remaining *single-token* continuations (`"เงียบ จังเลยวันนี้"`, `"ภาษา นี้ยากมาก"` — both one contiguous Thai-script run, so guard #1 alone doesn't catch them):
   - **`เงียบ`**: `_QUIET_TH_VALUE_RE`, a loose shape check (`off`, or `HH:MM-HH:MM[,...]`-shaped digits/colons/hyphens/commas only — not full 00-23/00-59 range validation, which stays `execute_quiet`'s job). A quiet-hours value has an unambiguous mechanical shape prose doesn't, so a strict whitelist works cleanly here.
   - **`ภาษา`**: `_looks_like_th_prose`, a curated Thai marker list (`"ๆ"`, `"นะ"`, `"จัง"`, `"เลย"`, `"หน่อย"`, `"มาก"`, `"นี้"`, `"ยาก"`, `"ไหม"` — the same curated-substring-list technique `_QUERY_PATTERNS` already uses elsewhere in this file for Thai interrogative markers, not a novel pattern). A language *name* has no mechanical shape a regex whitelist can check (`"ไทย"`/`"english"` are both legitimate single-word attempts worth a `lang_usage` reply, same as `"eng"`/`"th-TH"` already are via the slash form) — a whitelist-of-valid-codes approach (Vera's own suggested direction, restricting to exactly `en`/`th`/`auto`) was considered and rejected because it would have broken Vera's own already-passing `test_execute_lang_rejects_unsupported_codes_writes_nothing` (which requires `"ภาษา ไทย"`/`"ภาษา english"` to still dispatch as `kind="lang"` so `execute_lang` can reply `lang_usage`, not vanish silently). A blacklist of common discourse markers threads that needle: it rejects the two prose cases in the failing corpus while still admitting single-word attempts that aren't literally `en`/`th`/`auto`.

Also fixed a `SyntaxWarning: invalid escape sequence '\s'` I introduced in the module docstring update (a literal `` `\s+` `` inside a non-raw triple-quoted string) by rewording to prose ("a mandatory single whitespace") instead of regex notation.

**Verification:** `tests/test_preferences.py` — 81/81 passed (my original 45 + Vera's 36, including all 4 previously-failing cases; none of Vera's tests were modified). `tests/test_commands.py`/`test_i18n.py`/`test_i18n_literals.py`/`test_access.py` — 170/170 passed (no shadowing of `access`'s or `schedules`'s own kinds). Full repo suite — **1275 passed, 0 failed, 1 skipped** (target was 1262+/0/1; the extra count includes the `schedules` module's own parallel `เตือน` hardening, which landed in the same file during this round).

### Round 2 (Vera's follow-up audit) — FAIL, 6 failures, hand-back

**Failure:** `test_lang_th_alias_blacklist_residual_misfire_corpus` (6 new parametrized cases, all newly added this round). Six realistic, unremarkable two-word Thai messages — `"ภาษา อังกฤษ"` ("[studied] English"), `"ภาษา จีน"` ("Chinese"), `"ภาษา ใหม่"` ("[a] new language"), `"ภาษา ดี"` ("[my] language [is] good"), `"ภาษา สวย"` ("[a] beautiful language"), `"ภาษา อะไร"` ("which language?") — still dispatched as `Command(kind="lang", ...)` instead of falling through.

**Root cause:** Round 1's `_looks_like_th_prose` guard was a curated BLACKLIST of Thai discourse markers (ๆ/นะ/จัง/เลย/หน่อย/มาก/นี้/ยาก/ไหม). A blacklist can only reject words someone thought to curate — every one of these 6 messages is an ordinary single Thai word (a language name, "new", "good", "beautiful", "what") that contains none of the listed markers, so all 6 still passed the guard and dispatched. Structural weakness, not a curation gap I could patch by adding a few more words to the list — the space of ordinary single Thai words is unbounded.

**Fix ruling (coordinator, on Vera's recommendation):** switch `ภาษา`'s guard from blacklist to a small curated WHITELIST, mirroring `เงียบ`'s own (already-robust) shape-whitelist strategy, since a blacklist can never be complete but a whitelist of the small, closed set of values that *should* dispatch is exhaustive by construction:
- `core/commands.py`: replaced `_TH_PROSE_MARKERS`/`_looks_like_th_prose` (removed entirely — confirmed unused anywhere else in the codebase before deleting) with `_LANG_TH_VALID_VALUES = {"en", "th", "auto", "ไทย", "english"}` — exactly R-P1's valid value set (`en`/`th`/`auto`) plus the two REVIEWED near-miss names `test_execute_lang_rejects_unsupported_codes_writes_nothing` requires to keep dispatching (`ไทย`, the native Thai word for "Thai"; `english`) so `execute_lang` can still reply `lang_usage` on those two rather than the message vanishing. `_match_lang`'s Thai-alias branch now checks `value.lower() not in _LANG_TH_VALID_VALUES` instead of the removed prose check. `เงียบ`'s `_QUIET_TH_VALUE_RE` shape-whitelist was untouched (Vera's positive-control corpus, 7 cases including descriptive words never explicitly enumerated like "สงบ"/"แล้ว", confirmed it had no equivalent gap — a mechanical shape check can't accidentally admit an arbitrary Thai word the way a word-membership blacklist can).
- **Design rationale (recorded per the coordinator's request):** SPEC-v1.2.md §2.3 states the new commands follow "the same discipline as `/undo`/`/target`" — a zero-false-positive-first router. That discipline prioritizes precision over an NL-friendly "any single word gets a helpful nudge" property: the deterministic `/lang en` always works regardless of this guard, and full natural-language language-switching (e.g. "ภาษา อังกฤษ" someday meaning "switch to English") was consciously deferred along with the rest of free-form NL command phrasing (SPEC-v1.2.md §10). So an ordinary Thai word for a language name appearing in prose (with no `/lang`/`ภาษา <code>` intent) must not be treated as a command — closing the false-positive gap is the correct trade, even though it means a handful of near-miss command ATTEMPTS in Thai (any word other than `ไทย`/`english`) now silently fall through instead of getting a `lang_usage` nudge. That's an accepted, deliberate narrowing, not an oversight.

**Verification:** `tests/test_preferences.py` — 94/94 passed (81 from round 1 + Vera's 13 new this round: the 6-case residual corpus + the 7-case `เงียบ` positive control; none of Vera's tests were modified). `tests/test_commands.py`/`test_i18n.py`/`test_i18n_literals.py`/`test_access.py` — 170/170 passed. Full repo suite — **1288 passed, 0 failed, 1 skipped**, exactly matching the round-2 target (baseline was 1282 passed/6 failed/1 skipped).
