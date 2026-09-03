"""Vera's gap-probe suite for Module D (Linux deployment kit), SPEC-LINE.md
§4 R-D1..R-D4 / §8 AC26-AC27. `tests/test_deploy_line.py` (Luna's own 29
tests) already covers the happy-path claims; this file targets what that
suite does NOT check, found by independent byte-level/adversarial review:

1. `.gitattributes` claims `eol=lf` for `config.toml.line` / `.env.example`
   / `.env.line.example` too, but `test_deploy_files_are_lf_only_no_bom`
   only parametrizes over `deploy/*` -- these three files have zero
   automated LF/BOM coverage.
2. Cross-file consistency: the systemd units hardcode `/opt/habit-assistant`
   independently of `setup.sh`'s `$REPO_ROOT`-relative logic, and
   `docs/DEPLOY-LINE.md` is the only thing tying the two together (the
   `git clone ... /opt/habit-assistant` step). Nothing tests that the
   three actually agree.
3. The rich-menu's 6 button commands vs. commands the app actually
   dispatches.
4. Whether `test_src_has_no_windows_only_apis`'s marker list would catch a
   realistic Windows-ism (`os.system('cls')`, a hardcoded `C:` drive-letter
   path) -- this is a review of the guard's own effectiveness, not of `src/`.
5. Service-user / root-avoidance checks not asserted elsewhere.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "deploy"


def _parse_ini_directives(path: Path) -> dict[str, list[str]]:
    directives: dict[str, list[str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("[") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        directives.setdefault(key.strip(), []).append(value.strip())
    return directives


# --- Gap 1: BOM coverage for the 3 template files .gitattributes also claims ---

@pytest.mark.parametrize(
    "name",
    ["config.toml.line", ".env.line.example", ".env.example"],
)
def test_line_templates_have_no_bom(name):
    """.gitattributes lists these 3 files under `eol=lf` right alongside
    deploy/*, but test_deploy_line.py's LF/BOM check only parametrizes
    over deploy/*. Git's `eol=lf` normalizes CRLF->LF on add/checkout
    automatically (verified manually below in the test report), but it
    does NOT strip a byte-order mark -- a BOM here would silently survive
    to the Linux checkout and break tomllib/dotenv parsing. This is the
    one property .gitattributes can't fix for us, so it needs its own
    permanent guard."""
    data = (REPO_ROOT / name).read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), f"{name} has a UTF-8 BOM"
    assert not data.startswith(b"\xff\xfe"), f"{name} has a UTF-16 LE BOM"
    assert not data.startswith(b"\xfe\xff"), f"{name} has a UTF-16 BE BOM"


@pytest.mark.parametrize(
    "name",
    ["config.toml.line", ".env.line.example", ".env.example"],
)
def test_line_templates_are_lf_only_on_disk(name):
    """Companion to the BOM check above: the *working-tree* bytes should
    already be LF-only, not merely "LF after git normalizes it on add".
    Relying on the git filter to clean up an author's CRLF is fragile
    (e.g. a file copied in by a script that doesn't go through `git add`,
    or read directly by a tool that doesn't apply clean filters) and this
    property is cheap to guarantee at rest. NOTE (see TEST-LINE-D.md): as
    of this review .env.example currently FAILS this -- its working-tree
    copy has CRLF throughout. Verified separately (git add + git show
    :.env.example) that `.gitattributes`' `eol=lf` correctly normalizes
    it to pure LF once staged, so what actually gets committed/checked
    out on Linux is fine -- this failure flags a hygiene gap in the raw
    file, not a functional break."""
    data = (REPO_ROOT / name).read_bytes()
    assert b"\r" not in data, f"{name} contains CR bytes on disk (CRLF) -- resave with LF endings"


def test_gitattributes_declares_eol_lf_for_every_deploy_and_template_pattern():
    """Regression guard on .gitattributes itself: if a future edit drops
    one of these lines, the LF-on-checkout guarantee silently stops
    applying to that file/pattern."""
    text = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    expected_patterns = [
        "deploy/*.sh",
        "deploy/*.service",
        "deploy/*.timer",
        "deploy/*.cron",
        "config.toml.line",
        ".env.example",
        ".env.line.example",
    ]
    for pattern in expected_patterns:
        line_match = [ln for ln in text.splitlines() if ln.strip().startswith(pattern)]
        assert line_match, f".gitattributes has no rule for {pattern!r}"
        assert "eol=lf" in line_match[0], f".gitattributes rule for {pattern!r} doesn't force eol=lf: {line_match[0]!r}"


# --- Gap 2: cross-file path consistency (setup.sh <-> systemd units <-> docs) ---

def test_systemd_units_and_backup_cron_agree_on_a_single_install_root():
    """R-D1 (`WorkingDirectory=<repo>`) is satisfied only if every path
    fragment across the 4 deploy artifacts that hardcode an absolute
    install path actually agrees on the SAME root. setup.sh itself is
    relocatable (`$REPO_ROOT` computed from its own location), but the
    checked-in unit/cron files are not templated -- they hardcode a
    literal. If that literal ever drifts between files, a copy-paste
    fix in one place silently breaks the others."""
    main = _parse_ini_directives(DEPLOY_DIR / "habit-assistant-line.service")
    backup = _parse_ini_directives(DEPLOY_DIR / "habit-assistant-line-backup.service")
    cron_line = [
        ln for ln in (DEPLOY_DIR / "backup.cron").read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ][0]

    main_root = main["WorkingDirectory"][0]
    assert main["EnvironmentFile"][0] == f"{main_root}/.env"
    assert main["ExecStart"][0] == f"{main_root}/.venv/bin/python -m habit_assistant.main"

    backup_root = backup["WorkingDirectory"][0]
    assert backup["EnvironmentFile"][0] == f"{backup_root}/.env"
    assert backup["ExecStart"][0] == f"{backup_root}/.venv/bin/python -m habit_assistant.main --backup"

    assert main_root == backup_root, (
        f"main service root {main_root!r} != backup service root {backup_root!r}"
    )
    assert main_root in cron_line, (
        f"backup.cron doesn't reference the same install root ({main_root!r}): {cron_line!r}"
    )

    # And the runbook must actually instruct cloning to that exact path --
    # otherwise a reader who follows docs/DEPLOY-LINE.md literally ends up
    # with a repo root that doesn't match what the units hardcode.
    doc = (REPO_ROOT / "docs" / "DEPLOY-LINE.md").read_text(encoding="utf-8")
    assert main_root in doc, (
        f"docs/DEPLOY-LINE.md never mentions the install root the systemd units hardcode ({main_root!r}) "
        "-- a reader who clones anywhere else silently breaks ExecStart/EnvironmentFile/WorkingDirectory"
    )


def test_systemd_units_run_the_service_as_a_non_root_user():
    for unit_file in ["habit-assistant-line.service", "habit-assistant-line-backup.service"]:
        directives = _parse_ini_directives(DEPLOY_DIR / unit_file)
        assert directives["User"] != ["root"], f"{unit_file} runs as root"
        assert directives.get("User"), f"{unit_file} has no explicit User= (would default to root)"


def test_setup_sh_never_starts_the_main_bot_or_runs_funnel():
    """R-D3: Funnel is documentation, not code; the runbook (docs/DEPLOY-
    LINE.md §2) explicitly says setup.sh must not start the bot before
    secrets are filled in. setup.sh should only ever *print* the funnel
    command and *enable* (not unconditionally start-and-leave-running via
    an interactive `tailscale up`) the main service."""
    text = (DEPLOY_DIR / "setup.sh").read_text(encoding="utf-8")
    # No line should invoke `tailscale funnel` or `tailscale up` for real --
    # only echo/log it.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("log ", "#")):
            continue
        assert "tailscale funnel" not in stripped, f"setup.sh executes tailscale funnel directly: {line!r}"
        assert "tailscale up" not in stripped, f"setup.sh executes tailscale up directly: {line!r}"
    assert "systemctl start habit-assistant-line.service" not in text.replace("sudo systemctl start habit-assistant-line.service", "")
    assert re.search(r"^\s*sudo systemctl start habit-assistant-line\.service\s*$", text, re.MULTILINE) is None


def test_service_user_override_is_consistent_with_the_static_unit_files():
    """Adversarial finding: setup.sh lets an operator override the service
    user via $HABIT_ASSISTANT_USER (`SERVICE_USER="${HABIT_ASSISTANT_USER:-habitbot}"`),
    and uses $SERVICE_USER for useradd/chown -- but the checked-in unit
    files are not templated and hardcode `User=habitbot`/`Group=habitbot`
    literally. If an operator ever sets HABIT_ASSISTANT_USER to anything
    other than "habitbot", the units still try to run as a user that was
    never created (or that doesn't own the chown'd files), and the
    service fails to start. This test documents/pins that coupling so it
    doesn't get "fixed" invisibly in only one place; it is not itself an
    AC violation since the spec never asks for a configurable service
    user and the default path (no override) is fully self-consistent."""
    setup_text = (DEPLOY_DIR / "setup.sh").read_text(encoding="utf-8")
    assert 'SERVICE_USER="${HABIT_ASSISTANT_USER:-habitbot}"' in setup_text, (
        "setup.sh's override mechanism changed shape -- re-check whether the unit files "
        "still silently hardcode the default and update this pinning test / flag to Luna"
    )
    for unit_file in ["habit-assistant-line.service", "habit-assistant-line-backup.service"]:
        directives = _parse_ini_directives(DEPLOY_DIR / unit_file)
        assert directives["User"] == ["habitbot"], (
            f"{unit_file} User= no longer hardcoded to 'habitbot' -- if it's now templated, "
            "this override-consistency gap may be resolved; re-verify against setup.sh"
        )


# --- Gap 3: rich-menu buttons vs. real dispatchable commands --------------------


def test_richmenu_button_commands_are_real_dispatchable_commands():
    """Repaired (line/v1.1.0 hardening pass, Archi-sanctioned mechanical
    fix): this test's own README-table regex broke when Iris's rich-menu
    artwork commit (`0f4e310`, `assets/richmenu/*` only -- disjoint from
    every file this dispatch touches, confirmed via `git show --stat`)
    rewrote "The six cells" table to add a "Tap area" column and drop
    the backtick-wrapped command shape the old
    `r"\\|\\s*\\d\\s*\\|\\s*`(/\\w+)`"` regex depended on -- 0 rows matched,
    not a real regression in the rich menu itself.

    Rather than re-deriving a new regex for the new table shape (fragile
    again the next time Iris/Luna reformat the doc), this now asserts
    against `channels/line.py:_default_rich_menu_payload()` directly --
    the actual, authoritative runtime source LINE calls at startup
    (`register_rich_menu`), not a markdown transcription of it that can
    drift out of sync with the code. This is strictly stronger (it would
    catch a code-side command typo the README never would) and
    format-proof (no README table shape to keep in sync with a regex).

    The README is still spot-checked, format-tolerantly (a plain
    substring search, not a table-shape regex), so a genuine doc/code
    drift -- the README describing a button the code no longer sends --
    is still caught without depending on the table's exact markdown
    shape.

    Rich-menu rewire (branch `line-version`, 2026-09-03 request): the menu
    is no longer ALL message actions -- the two new top-row cells are
    POSTBACK actions (`log:<habit>:<value>`), which have no `action
    ["text"]` at all (that key is a message action's own field). This test
    now splits its assertions by action type: every MESSAGE-action cell
    still gets the exact same dispatchable-command + README cross-check
    as before; every POSTBACK cell gets its own, equally real, cross-check
    -- its `data` must actually parse against `core/quicklog.py`'s own
    `_LOG_CALLBACK_RE` (the same regex `on_callback`'s `data.startswith
    ("log:")` branch hands it at runtime) AND resolve against the BASE
    habit registry (`Config()`'s own `_default_habits()` -- the one every
    user's per-user registry is built on top of, `HabitRegistry.
    from_config`), the same two checks `quicklog.handle_log_callback`
    itself performs before ever writing a row -- so a typo'd habit id or
    a malformed payload in the rich-menu cell itself would fail HERE,
    not silently no-op for every real user at runtime."""
    from habit_assistant.channels.line import _default_rich_menu_payload
    from habit_assistant.config import Config
    from habit_assistant.core.habits import HabitRegistry
    from habit_assistant.core.quicklog import _LOG_CALLBACK_RE

    payload = _default_rich_menu_payload()
    areas = payload["areas"]
    assert len(areas) == 6, f"expected 6 rich-menu areas, found {len(areas)}"

    message_actions = [area["action"] for area in areas if area["action"]["type"] == "message"]
    postback_actions = [area["action"] for area in areas if area["action"]["type"] == "postback"]
    assert len(postback_actions) == 2, f"expected exactly 2 direct-log postback cells, found {len(postback_actions)}"
    assert len(message_actions) == 4, f"expected exactly 4 message-action cells, found {len(message_actions)}"

    # -- message-action cells: unchanged cross-check against real dispatch --
    button_commands = [action["text"] for action in message_actions]
    routing_text = (REPO_ROOT / "src" / "habit_assistant" / "core" / "routing.py").read_text(encoding="utf-8")
    dispatched_kinds = set(re.findall(r'command\.kind\s*==\s*"(\w+)"', routing_text))

    for cmd in button_commands:
        assert cmd.startswith("/"), f"rich-menu button action {cmd!r} is not a slash command"
        kind = cmd.lstrip("/")
        assert kind in dispatched_kinds, (
            f"rich-menu button {cmd!r} (from _default_rich_menu_payload()) has no matching "
            f"`command.kind == {kind!r}` dispatch in core/routing.py (found kinds: {sorted(dispatched_kinds)})"
        )

    readme = (REPO_ROOT / "assets" / "richmenu" / "README.md").read_text(encoding="utf-8")
    for cmd in button_commands:
        assert f"`{cmd}`" in readme, (
            f"README no longer documents rich-menu button {cmd!r} anywhere -- doc/code drift "
            f"(format-tolerant substring check, not a table-shape regex)"
        )

    # -- postback direct-log cells: parse against the real callback regex,
    # then resolve against the real base registry, exactly like
    # `quicklog.handle_log_callback` does before it ever writes a row --
    base_registry = HabitRegistry.from_config(Config())
    for action in postback_actions:
        data = action["data"]
        match = _LOG_CALLBACK_RE.match(data)
        assert match is not None, f"rich-menu direct-log postback data {data!r} does not match _LOG_CALLBACK_RE"
        habit_id = match.group("habit")
        value = float(match.group("value"))
        habit = base_registry.get(habit_id)
        assert habit is not None, f"rich-menu direct-log postback names habit {habit_id!r}, not in the base registry"
        assert habit.type != "text", f"rich-menu direct-log postback names a text habit {habit_id!r} (never quick-loggable)"
        if habit.type == "boolean":
            assert value == 1, f"rich-menu direct-log postback for boolean habit {habit_id!r} must carry value 1, got {value}"
        else:
            assert value > 0, f"rich-menu direct-log postback for {habit_id!r} must carry a positive value, got {value}"
        assert action.get("displayText"), f"rich-menu direct-log postback for {habit_id!r} has no displayText"


# --- Gap 4: is the Windows-ism regression guard actually effective? -------------

def test_windows_ism_guard_marker_list_now_catches_os_system_and_hardcoded_drive_paths():
    """UPDATED per this test's own original instruction ("if this now
    fails, the guard has been strengthened; update/retire this test") --
    Integration (Archi-sanctioned extra, item 7) broadened `tests/test_
    deploy_line.py`'s guard: `os.system(` is now a plain marker, and a new
    `_DRIVE_LETTER_PATH_RE` catches a quoted `[A-Za-z]:\\`-shaped path
    (escaped-backslash `"C:\\\\Users\\\\..."` or raw-string `r"C:\\Users\\..."`
    form, either one). This test now proves the STRENGTHENED guard
    actually catches the exact offending snippet the original version of
    this test used to demonstrate the blind spot with -- a real regression
    guard for the guard itself, not just documentation that a gap exists."""
    from test_deploy_line import _DRIVE_LETTER_PATH_RE, _WINDOWS_ONLY_MARKERS  # the real, current guard

    offending_snippet = textwrap.dedent(
        """
        import os

        def clear_screen():
            os.system('cls')

        LOG_DIR = "C:\\\\Users\\\\habitbot\\\\AppData\\\\Local\\\\habit-assistant"
        """
    )

    marker_hits = [marker for marker in _WINDOWS_ONLY_MARKERS if marker in offending_snippet]
    assert "os.system(" in marker_hits, "the broadened marker list must catch os.system('cls')-class calls"
    assert _DRIVE_LETTER_PATH_RE.search(offending_snippet) is not None, (
        "the new drive-letter regex must catch the hardcoded C:\\ path"
    )

    # A raw-string form (single backslash) must also be caught -- the
    # original gap-report's own snippet only exercised the escaped-double-
    # backslash form.
    raw_string_snippet = 'LOG_DIR = r"C:\\Users\\habitbot\\AppData\\Local\\habit-assistant"'
    assert _DRIVE_LETTER_PATH_RE.search(raw_string_snippet) is not None, (
        "the drive-letter regex must also catch a raw-string-literal hardcoded path"
    )

    # Negative controls: a bare URL scheme and a type-annotated assignment
    # must NOT be flagged (no backslash in either).
    assert _DRIVE_LETTER_PATH_RE.search('BASE_URL = "https://api.line.me"') is None
    assert _DRIVE_LETTER_PATH_RE.search("def f(x: str) -> None: ...") is None
