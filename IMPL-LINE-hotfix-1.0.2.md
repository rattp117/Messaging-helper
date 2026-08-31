# Implementation — LINE edition hotfix round (v1.0.2)

Three deployment errata found on the real VPS on 2026-08-31, fixed on branch
`line-version` in the dedicated worktree
`C:\Users\Demo\OneDrive - Ngow Hock Agency Co,Ltd\Claude-Cowork\Messaging-line`.
No feature/behavior change — all three are correctness fixes for the
deployment path.

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/channels/line.py` | modified | Added `LINE_API_DATA_ROOT = "https://api-data.line.me"`; `register_rich_menu`'s content-upload POST now targets it instead of `LINE_API_ROOT` (`api.line.me`), which 404s for binary uploads in production. |
| `tests/test_line_channel.py` | modified | `test_register_rich_menu_creates_uploads_and_sets_default` now asserts the **host** of each of the 3 registration calls (`api.line.me`, `api-data.line.me`, `api.line.me`), not just the path — the previous version only checked paths, so it would have passed even with the wrong host. |
| `deploy/setup.sh` | modified | Step 6's config.toml guard: since `config.toml` is a tracked (Telegram-flavored) file, `[ ! -f config.toml ]` never fires on a fresh clone. Added an `elif` branch that detects a still-Telegram-flavored `config.toml` (no `type = "line"` in it) and, when `config.toml.line` exists, backs it up to `config.toml.telegram.bak` and installs the real LINE config — logging what happened. A `config.toml` that already declares `type = "line"` (installed or hand-edited) is left untouched. |
| `tests/test_deploy_line.py` | modified | Added 4 new tests that extract step 6's logic verbatim from `setup.sh` (between its own `# --- 6.` / `# --- 7.` markers, pure file I/O, safe to actually execute) and run it against a throwaway `tmp_path` via real bash: fresh-repo install, fresh-clone Telegram-config replacement (the actual bug), hand-edited-LINE-config-never-clobbered, and idempotent re-run. |
| `pyproject.toml` | modified | `[project.optional-dependencies].charts` now pins `numpy>=1.26,<2` alongside `matplotlib>=3.8`, with a comment naming the x86-64-v2 wheel-baseline crash this prevents. |
| `docs/DEPLOY-LINE.md` | modified | Added a "NumPy baseline" entry to §9 Troubleshooting: symptom (`RuntimeError`/crash-loop right after `[charts]` install on an older CPU), root cause (numpy 2.x wheels require x86-64-v2/SSE4.2+), and fix (the new pin, or `pip install 'numpy<2'` on an already-built venv). |

## How it works

1. **Rich-menu blob endpoint.** LINE splits its API across two hosts: JSON
   management calls stay on `api.line.me`; binary-content calls (the
   rich-menu image upload, and message-content download if this app ever
   adds that) live on `api-data.line.me`. `register_rich_menu` made all
   three of its calls (create, upload, set-default) against `LINE_API_ROOT`;
   only the middle one (upload) needed to move to the new
   `LINE_API_DATA_ROOT` constant. I audited the rest of `line.py` for any
   other binary-content endpoint — the only one is this rich-menu upload;
   there's no message-content download call anywhere in this codebase.
2. **setup.sh config guard.** `config.toml` at the repo root is tracked
   git history (pre-dates the LINE branch, still Telegram-flavored — no
   `[channel]` section at all, so `ChannelConfig.type` defaults to
   `"telegram"`). `config.toml.line` is the only place `type = "line"`
   is ever written. The new `elif` greps for that exact string's absence
   as the "still Telegram" signal, which is robust to both the legacy
   no-`[channel]`-section shape and a hypothetical explicit
   `type = "telegram"`, while never touching a config that already says
   `type = "line"` (installed by this same guard, or hand-edited by an
   operator) — satisfying both "fix the fresh-clone bug" and "never
   clobber a real LINE config" from the same single condition.
3. **numpy pin.** Purely a dependency constraint in the `[charts]` extra;
   no code changes. `deploy/setup.sh` step 4 already runs
   `pip install -e ".[charts]"`, so the pin takes effect on any fresh
   install or re-run with no further action.

## Smoke test done

- Manually extracted and ran `deploy/setup.sh`'s step-6 block against
  four synthetic `REPO_ROOT` directories via real Git Bash, before
  encoding the same scenarios as pytest tests: (1) no `config.toml` at
  all → installs the LINE template; (2) fresh-clone shape (`config.toml`
  present, Telegram-flavored) → backs up to `config.toml.telegram.bak`
  and installs the LINE template; (3) hand-edited real LINE config
  already in place → left byte-identical, no backup created; (4) re-run
  after the LINE config is already installed → no-op, idempotent. All
  four matched the intended behavior before I wrote a single test
  assertion.
- Verified `pyproject.toml` still parses as valid TOML and the `charts`
  extra resolves to `['matplotlib>=3.8', 'numpy>=1.26,<2']` via
  `tomllib.load`.
- Confirmed `deploy/setup.sh` has no BOM and no CR bytes after editing
  (`.gitattributes` forces `deploy/*.sh` to LF on commit regardless, but
  checked directly since a stray `\r` would silently break the new
  `elif`/`grep` logic on Linux).
- Ran the full exit-bar command (see below) — this exercises
  `register_rich_menu`'s corrected host through the real `LineChannel`
  code path (not just the extracted-snippet smoke test) via
  `tests/test_line_channel.py`'s `httpx.MockTransport`-backed suite.

**Command + result (targeted files):**
```
tests/test_line_channel.py tests/test_deploy_line.py tests/test_line_a_gaps.py -q
→ 103 passed, 3 skipped in 7.50s
```
The 3 skips are the pre-existing `systemd-analyze`/real-bash-availability
skip conditions already documented in the test file's own skip reasons —
none are new.

**Command + result (full LINE gate):**
```
pytest -m "not telegram_only and not llm_only" -n auto -q
→ 1 failed, 5053 passed, 4 skipped, 1 xfailed in 83.30s
```
The 1 failure is
`tests/test_v19_release_gate.py::test_ac17_habits_line_transitions_from_available_to_used_after_a_real_grace_bridge`
— the known pre-existing Monday ISO-week date-drift flake, exactly as
flagged going in. Not touched by any of these three fixes; not fixed
here per instruction.

## Maps to acceptance criteria

This is a hotfix round, not a spec-driven feature — the three fixes map
to the three errata items directly:

- **Item 1 (rich-menu blob endpoint)** → `src/habit_assistant/channels/line.py:47` (`LINE_API_DATA_ROOT`) and `line.py:284` (the upload call). Verified by `tests/test_line_channel.py::test_register_rich_menu_creates_uploads_and_sets_default`'s new host assertions.
- **Item 2 (setup.sh tracked-config guard)** → `deploy/setup.sh` step 6. Verified by 4 new tests in `tests/test_deploy_line.py` (`test_setup_step6_*`), each actually executing the extracted guard logic via bash, not just asserting on script text.
- **Item 3 (numpy CPU-baseline pin)** → `pyproject.toml`'s `[project.optional-dependencies].charts` and `docs/DEPLOY-LINE.md` §9. No runtime test possible for "does the pin prevent a crash on a pre-SSE4.2 CPU" from this dev box; verified instead that the pin is syntactically correct and present (`tomllib` parse) and that the doc entry doesn't break `test_deploy_line_doc_covers_the_required_runbook_sections`'s existing coverage assertions.

## Known limitations

- The numpy fix is a dependency-pin + doc change only — there's no
  automated test that would catch a *future* re-introduction of an
  unpinned/incompatible numpy version being pulled in transitively by
  some other dependency bump; it relies on the pin staying in
  `pyproject.toml`.
- `tests/test_line_integration.py`'s own `_LineApiRecorder` (a separate,
  broader end-to-end LINE API mock used by the integration suite) still
  matches outbound calls by path only, not host — it wasn't in scope
  (not named in the exit bar, and host-blindness there doesn't cause a
  false pass/fail for this fix since `httpx.MockTransport` intercepts by
  path regardless of host). Only `tests/test_line_channel.py`'s
  rich-menu test, which the task explicitly named, was extended with
  host pinning.
