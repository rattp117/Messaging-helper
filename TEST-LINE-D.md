# Test Report — LINE edition, Module D (Linux deployment kit)

Scope: SPEC-LINE.md §4 R-D1–R-D4, §8 AC26–AC27 only. Verified against
IMPL-LINE-D.md. Files under test: `deploy/*`, `config.toml.line`,
`.env.line.example`, `.env.example`, `assets/richmenu/*`,
`docs/DEPLOY-LINE.md`, `.gitattributes`, `tests/test_deploy_line.py`.
No production/deploy file was modified by this pass — findings only.
Modules A/B/C were still editing this same worktree throughout this
review (concurrent, disjoint file ownership per §11); see "Tree state"
at the end.

## Summary

- Total: 42 tests (29 existing + 13 new gap-probe tests), plus 55
  regression tests re-run from the shared/config surface for safety.
- Passed: 93 (38 Module D + 47 `test_config.py` + 8 `test_deliverables.py`)
- Failed: 1 (new gap-probe test — non-blocking hygiene finding, see below)
- Skipped: 3 (`systemd-analyze` unavailable on this Windows dev box — expected, matches IMPL-LINE-D.md)
- Status: **PASS** (with one flagged non-blocking finding and one documentation correction for Luna)

```
tests/test_deploy_line.py tests/test_line_d_gaps.py tests/test_config.py tests/test_deliverables.py
-> 93 passed, 3 skipped, 1 failed
```

## Test files

| Path | Tests added | Covers which ACs |
|---|---|---|
| `tests/test_deploy_line.py` (Luna's, pre-existing) | 29 (26 pass, 3 skip) | AC26, AC27 |
| `tests/test_line_d_gaps.py` (new, this pass) | 13 (12 pass, 1 fail) | AC26, AC27, R-D4 guard-effectiveness |

## Byte-check results (independent, not trusting IMPL.md's claim)

Ran a raw `od`-equivalent byte scan (Python, `rb` mode) over every
`deploy/*.sh|.service|.timer|.cron` file plus `config.toml.line`,
`.env.line.example`, `.env.example`:

| File | CR bytes | BOM | Shebang |
|---|---|---|---|
| `deploy/setup.sh` | 0 | none | `#!/usr/bin/env bash` ✓ |
| `deploy/run.sh` | 0 | none | `#!/usr/bin/env bash` ✓ |
| `deploy/habit-assistant-line.service` | 0 | none | — |
| `deploy/habit-assistant-line-backup.service` | 0 | none | — |
| `deploy/habit-assistant-line-backup.timer` | 0 | none | — |
| `deploy/backup.cron` | 0 | none | — |
| `config.toml.line` | 0 | none | — |
| `.env.line.example` | 0 | none | — |
| **`.env.example`** | **28 (all CRLF)** | none | — |

**Finding:** `.env.example` (a pre-existing, `M`odified file — not new)
is entirely CRLF on disk. IMPL-LINE-D.md's smoke-test claim #3 ("every
`deploy/*.sh/.service/.timer/.cron` file verified... zero CR bytes")
never covered this file, and `tests/test_deploy_line.py`'s own
`test_deploy_files_are_lf_only_no_bom` only parametrizes over `deploy/*`
— so nothing caught it.

**But it doesn't break the deploy.** `.gitattributes` declares
`.env.example text eol=lf`. Verified empirically (not just by reading the
attribute) what git actually does:

```
$ git add .env.example
warning: in the working copy of '.env.example', CRLF will be replaced by LF the next time Git touches it
$ git show :.env.example | <byte-count script>
size 1527  CR 0  CRLF 0  LF 28        # <- normalized to pure LF in the index
$ git restore --staged .env.example   # left working tree/index exactly as found
```

So `git add`/commit normalizes this file to LF regardless of the
Windows working-tree state, and `eol=lf` forces LF on checkout on any
platform (including Linux) — **the artifact that actually reaches a
VPS is correct.** The gap is purely cosmetic (the on-disk copy in this
session's working tree wasn't resaved with LF), inherited from the
file predating this branch's `.gitattributes` addition
(`core.autocrlf=true` on this box would have checked it out as CRLF
before Module D added the LF-forcing rule). Non-blocking. Recommend
Luna resave `.env.example` with LF endings for hygiene/consistency
with the other two templates, which are already clean. New regression
test added: `test_line_templates_are_lf_only_on_disk` (fails today on
this one file only — see Failures below) plus a BOM-only variant that
passes, since BOM is the one thing git's `eol` filter does **not** fix
automatically.

## `setup.sh` adversarial review

Read the script end-to-end as an adversary, per dispatch instructions:

| Question | Finding |
|---|---|
| Idempotent? | Yes — every mutating step is gated: venv (`[ ! -x .venv/bin/python ]`), user creation (`! id -u`), `.env`/`config.toml` copy (`[ ! -f ... ]`). `pip install -e` and `chown -R`/`systemctl enable` run unconditionally every re-run but are themselves idempotent operations (pip no-ops on unchanged tree; chown/enable are naturally repeatable). No double-install or clobber risk found. |
| `set -e`? | `set -euo pipefail` at the top — confirmed via `bash -n` parse and direct read. |
| Paths consistent with the systemd unit? | Only if the operator clones to exactly `/opt/habit-assistant`, as `docs/DEPLOY-LINE.md` §2 instructs. `setup.sh` itself is relocatable (`$REPO_ROOT` computed from its own location), but the checked-in unit files hardcode `/opt/habit-assistant` literally (not templated at install time) — new test `test_systemd_units_and_backup_cron_agree_on_a_single_install_root` pins that the main service, backup service, backup cron, and the doc's clone instructions all agree on the same literal root. **Currently consistent** (all four say `/opt/habit-assistant`), but fragile: a future edit to any one of the 4 files won't be caught unless this new test stays in the suite. |
| Funnel command matches doc? | Yes — `setup.sh` prints `sudo tailscale funnel --bg $BIND_PORT` (parsed dynamically from `config.toml`'s `bind_port`, default 8080), matching `docs/DEPLOY-LINE.md` §4's `sudo tailscale funnel --bg 8080` example exactly. Confirmed neither `tailscale funnel` nor `tailscale up` nor `systemctl start habit-assistant-line.service` is ever *executed* by the script — only echoed via `log`. New test: `test_setup_sh_never_starts_the_main_bot_or_runs_funnel`. |
| Avoids running as root where it shouldn't? | The bootstrap script itself needs `sudo` (OS packages, `useradd`, unit install — expected of a provisioning script), but the **service** it installs runs as `habitbot`, not root, on both units (`User=habitbot`/`Group=habitbot`, confirmed the literal isn't `root` via new test `test_systemd_units_run_the_service_as_a_non_root_user`). |
| `HABIT_ASSISTANT_USER` override vs. static units | **Real gap, low severity.** `setup.sh` supports `SERVICE_USER="${HABIT_ASSISTANT_USER:-habitbot}"` for `useradd`/`chown`, but `deploy/habit-assistant-line*.service` are static files that hardcode `User=habitbot`/`Group=habitbot` — they don't pick up an override. If an operator ever sets `HABIT_ASSISTANT_USER` to anything else, the unit still tries to run as the never-created (or non-owning) `habitbot` user and fails to start. Not an AC violation (spec never asks for a configurable service user; the default, undocumented-override-unused path is fully self-consistent) — pinned as a documented finding via `test_service_user_override_is_consistent_with_the_static_unit_files` so it's visible rather than silently "fixed" in only one place later. |

## systemd unit sanity

- `After=network-online.target tailscaled.service`, `Wants=network-online.target` — sensible soft ordering (unit's own comment explains why it's not a hard `Requires=`: the bot binds locally regardless of Tailscale's state).
- `Restart=on-failure`, `RestartSec=5`, `StartLimitIntervalSec=60`/`StartLimitBurst=5` — present and reasonable crash-loop protection.
- `EnvironmentFile=/opt/habit-assistant/.env` matches where `setup.sh` places `.env` (given the `/opt/habit-assistant` clone-path assumption verified above).
- `ExecStart=/opt/habit-assistant/.venv/bin/python -m habit_assistant.main` — matches the venv `setup.sh` creates at `$REPO_ROOT/.venv` (same root-path caveat).
- Hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict` + `ReadWritePaths=/opt/habit-assistant/data`, `ProtectHome`, `UMask=0027` — all present, all reasonable for an unprivileged long-lived bot.
- Backup unit/timer: `Type=oneshot`, `ExecStart=... --backup`, `OnCalendar=*-*-* 03:30:00`, `Persistent=true` (catches a missed run) — correct and matches `backup.cron`'s `30 3 * * *` schedule and `--backup` flag exactly (existing test + confirmed by new `test_systemd_units_and_backup_cron_agree_on_a_single_install_root`).
- `systemd-analyze verify` itself: **not run** — genuinely unavailable on this Windows box (confirmed: `which systemd-analyze` finds nothing). Matches IMPL-LINE-D.md's stated limitation; `tests/test_deploy_line.py::test_systemd_analyze_when_available` is present and will run for real on Linux/the target VPS rather than silently skipping forever. Both unit files were instead validated against a directive-presence parser covering every R-D1/R-D3 field.

## `config.toml.line` + `.env.line.example` completeness

- Loaded `config.toml.line` through the **real** `Config.model_validate` (not a mock): `channel.type == "line"` ✓, `ollama.enabled is False` ✓, every `[line]`/`[digest]` field present and typed correctly ✓.
- Loaded `.env.line.example`'s exact three keys through the **real** `load_secrets(channel_type="line")`: round-trips clean; a deliberately-incomplete copy raises `ConfigError` naming the missing field. Confirmed by reading `config.py`'s `_REQUIRED_SECRETS_BY_CHANNEL` directly — `"line"` requires exactly `line_channel_access_token`/`line_channel_secret`/`line_owner_user_id`; `telegram_bot_token`/`telegram_chat_id` are not in that tuple, so **no Telegram token is required in LINE mode** — verified at the source, not just via the existing test.
- `.env.example` (the shared/general template) documents both channel blocks; `_TELEGRAM_ENV_KEYS` and `_LINE_ENV_KEYS` both present.

## `docs/DEPLOY-LINE.md` accuracy (cross-checked against the actual files, not just read)

- **Webhook path**: doc says `.../callback`; `src/habit_assistant/channels/line_webhook.py:110` registers `app.router.add_post("/callback", ...)` — matches exactly.
- **Funnel port vs. config port**: doc's example `sudo tailscale funnel --bg 8080` matches `config.toml.line`'s `[line].bind_port = 8080` default, and `setup.sh` derives the real value dynamically from `config.toml` rather than hardcoding it — consistent.
- **Service names**: doc references `habit-assistant-line.service`/`habit-assistant-line-backup.timer` — match the actual filenames in `deploy/`.
- **Install root**: doc's `git clone ... /opt/habit-assistant` is the one thing that makes the hardcoded systemd unit paths correct (see adversarial review above) — confirmed present via new `test_systemd_units_and_backup_cron_agree_on_a_single_install_root`.
- **Verification checklist** (§7, 7 items: webhook Verify, first message, quick log + undo, image reply, rich menu, digest test, quota-ledger check): all 7 are concrete and actionable (specific `journalctl`/`sqlite3` commands given, not vague "check it works").
- **Rich-menu asset**: doc §6 correctly describes it as a placeholder, auto-registered at startup, fail-open on failure — matches `assets/richmenu/README.md` and the registration behavior described in IMPL-LINE-A.md.

## Rich-menu image verification (independent, via PIL — not trusting the IMPL.md claim)

```
size(px): (2500, 1686)   mode: RGB   format: PNG
filesize: 70,717 bytes (69.1 KB)  -- well under LINE's 1 MB cap
valid LINE size (2500x1686 or 2500x843): True
```

Cross-checked the 6 button labels in `assets/richmenu/README.md`
(`/log`, `/habits`, `/heatmap`, `/wrapped`, `/help`, `/guide`) against
`core/routing.py`'s actual `command.kind == "..."` dispatch branches —
all 6 exist as real, dispatchable command kinds (new test
`test_richmenu_button_commands_are_real_dispatchable_commands`).

## Windows-ism regression-guard effectiveness (the specific ask: "would it catch `os.system('cls')` or a hardcoded `C:\` path?")

Answer: **No, today's marker list would miss both.**
`tests/test_deploy_line.py::test_src_has_no_windows_only_apis`'s marker
list is `["winreg", "msvcrt", "ctypes.windll", "os.startfile",
"PureWindowsPath", "WindowsPath("]`. Proved this against a scratch
in-memory snippet (never touching real `src/`) containing
`os.system('cls')` and a hardcoded `C:\Users\...` path — neither marker
matches. New test `test_windows_ism_guard_marker_list_would_miss_os_system_and_hardcoded_drive_paths`
documents this as a permanent, falsifiable pin (it will start failing,
and needs deletion/update, the day the marker list is strengthened).

**This is not a finding that `src/` currently has such an issue** — the
targeted `grep` in IMPL-LINE-D.md's own audit (and a spot-check of
`core/fonts.py`/`config.py`) found nothing, and I did not find anything
either. It's a finding about the *guard's* blind spot: a future PR could
reintroduce `os.system(...)` or a drive-letter path and this specific
regression test would stay green. Recommend Luna/Archi decide whether
to broaden the marker list (e.g. add `os.system`, a regex for
`[A-Za-z]:\\`, `sys.platform == "win32"`) in a follow-up — not blocking
this release since it's a guard-quality gap, not a live defect.

## AC coverage

- **AC26** *(systemd unit launches under venv Python, `Restart=on-failure`, `EnvironmentFile`, writable `data/`, no `.ps1`/Task-Scheduler logic)* → `test_main_service_unit_has_required_directives`, `test_backup_service_unit_uses_the_backup_flag`, `test_backup_timer_unit_fires_daily_and_is_installable`, `test_systemd_units_run_the_service_as_a_non_root_user` (new), `test_systemd_units_and_backup_cron_agree_on_a_single_install_root` (new), `test_deploy_files_are_lf_only_no_bom[*.service/*.timer]` — **PASS**. `.ps1`/`.plist` confirmed absent from every Module D deliverable and from `src/`'s own audit. Live `systemctl enable/start` on real systemd untestable on this Windows box — `test_systemd_analyze_when_available` skips with an explicit reason (3 skips), matching IMPL-LINE-D.md's own flagged limitation, not a silent gap.
- **AC27** *(fresh VPS reaches a running bot via docs/config/env/run.sh: venv → install → Funnel → webhook → backup cron)* → `test_config_toml_line_loads_clean`, `test_config_toml_line_public_base_url_is_a_placeholder_to_edit`, `test_config_toml_line_rich_menu_image_path_exists_in_repo`, `test_env_line_example_*` (3 tests), `test_env_example_documents_both_channels`, `test_backup_cron_has_matching_schedule_and_flag`, `test_deploy_line_doc_covers_the_required_runbook_sections`, `test_richmenu_placeholder_*` (2 tests), `test_richmenu_readme_exists_and_flags_the_placeholder_status`, plus new: `test_richmenu_button_commands_are_real_dispatchable_commands`, `test_systemd_units_and_backup_cron_agree_on_a_single_install_root`, `test_setup_sh_never_starts_the_main_bot_or_runs_funnel`, `test_line_templates_have_no_bom` (3), `test_gitattributes_declares_eol_lf_for_every_deploy_and_template_pattern` — **PASS** overall, with one non-blocking finding: `test_line_templates_are_lf_only_on_disk[.env.example]` **FAILS** (working-tree hygiene only, see byte-check section — proven the committed/checked-out artifact is unaffected). Live end-to-end "fresh VPS → running bot" walk itself needs a real Linux VPS + LINE channel + Tailscale tailnet, none of which exist in this environment — verified at file/content-correctness level only, same limitation IMPL-LINE-D.md itself flags. **Recommend a real VPS dry-run before first production deploy** (Archi/user decision, not a test gap).
- **R-D4** *(Linux-clean runtime, no Windows-isms)* → `test_src_has_no_windows_only_apis` (existing) — **PASS**. Guard-effectiveness gap noted separately above (informational, not a live defect).

## Failures

### `test_line_templates_are_lf_only_on_disk[.env.example]`
- **What was tested:** `.env.example`'s working-tree bytes contain no CR (i.e., it's already LF-only at rest, not merely "LF after git normalizes it").
- **AC violated:** None directly — `.gitattributes`' `eol=lf` rule (which Luna herself wrote for this exact file) is proven to normalize it correctly on `git add`/checkout (see byte-check section for the empirical `git add` + `git show :path` + `git restore --staged` proof). AC27's functional requirement (a fresh VPS reaching a running bot) is unaffected.
- **Input:** `.env.example` as currently saved in the worktree (a pre-existing, `M`odified file, not authored fresh by Module D).
- **Expected:** 0 CR bytes on disk.
- **Actual:** 28 CR bytes, all part of CRLF pairs (every line ending is CRLF).
- **Suspected cause:** The file predates this branch's `.gitattributes` addition and was originally checked out under this box's `core.autocrlf=true` (confirmed: `git config core.autocrlf` → `true`), so it carried CRLF from before Module D's edit; the edit tool that added the three `LINE_*` lines preserved the file's existing line-ending style rather than normalizing it.
- **Recommended fix:** Resave `.env.example` with LF line endings (e.g. re-write via a tool that doesn't apply Windows text-mode translation, matching how `.env.line.example`/`config.toml.line` were already saved cleanly). Trivial, non-blocking, cosmetic-at-rest only.

## Regressions detected

None. `tests/test_config.py` (47 passed) and `tests/test_deliverables.py`
(8 passed) — both touched adjacently by Module D's `.env.example` edit —
re-run clean, no regressions from Module D's changes.

## Documentation correction (for Luna, informational only)

IMPL-LINE-D.md's smoke-test item 8 states "73 passed, 3 skipped ...
(**69** from `test_config.py`'s config/secrets round-trips ... plus this
module's own **26** passed/3 skipped from `test_deploy_line.py`)" — 69 + 26
= 95, not 73. Independently re-ran both files together and got the correct
total (**73 passed, 3 skipped**, confirmed twice); `test_config.py` alone
is **47** passed, not 69 (47 + 26 = 73, which reconciles). The grand total
IMPL-LINE-D.md reports is correct — only the internal attribution sentence
has an arithmetic slip. Not functionally significant, flagging for
accuracy.

## Tree state (parallel-worktree note)

Modules A/B/C were actively editing this same worktree throughout this
review (git status showed churn between the start and end of this pass —
e.g. `IMPL-LINE-B.md` appeared mid-session). This review touched only:
read-only inspection of shared/other-module files (`config.py`,
`core/routing.py`, `channels/line_webhook.py`) for cross-file consistency
checks, plus one `git add .env.example` / `git restore --staged
.env.example` round-trip (used solely to empirically verify the
`.gitattributes` normalization claim — confirmed the working tree and
index were left exactly as found afterward). The only file written by
this pass is `tests/test_line_d_gaps.py` (new) and this report. Per
Archi's directed scope, a full-repo gate run was not attempted — this
report's numbers are the Module-D-relevant subset (`test_deploy_line.py`
+ `test_line_d_gaps.py` + `test_config.py` + `test_deliverables.py`),
consistent with IMPL-LINE-D.md's own stated deferral of the full LINE
gate to the integration pass (§11 step 5, after A+B+C+D all land). The
`test_v110_m3_gaps.py` flake mentioned in the dispatch was not
encountered/exercised — out of this pass's scope, per instructions.

## Recommendation

**Ready to ship** — both Module D acceptance criteria (AC26, AC27) pass
at the file/content-correctness level, which is the only level testable
in this Windows dev environment (no Linux VPS/systemd/LINE
channel/Tailscale tailnet available here — same limitation IMPL-LINE-D.md
itself flags for AC27's live end-to-end walk). One trivial, non-blocking
hygiene item (`.env.example` CRLF-at-rest, functionally inert per the
empirical git-normalization proof) and one guard-effectiveness gap
(Windows-ism marker list blind spot) are flagged for Luna/Archi's
judgment call, not release blockers. Recommend a real VPS dry-run before
the first production LINE deploy, as IMPL-LINE-D.md itself already
recommends.
