"""Deployment-file tests for the LINE edition (SPEC-LINE.md §4 Module D,
AC26/AC27; §11 owns `deploy/*`, `config.toml.line`, `.env.example`,
`docs/DEPLOY-LINE.md`, `assets/richmenu/*`). These are lint-level checks
doable in pytest -- `config.toml.line` loads clean through the real
`load_config`, the `.env` templates round-trip through the real
`load_secrets(channel_type="line")`, and every deploy/* file is
Linux-clean (LF-only, no BOM) and syntactically valid. `systemd-analyze`
is not available on this Windows dev box, so the systemd-unit checks below
are our own directive-presence parser; `test_systemd_analyze_when_available`
runs the real tool when the suite executes on a box that has it (CI on
Linux, or the target VPS itself) instead of silently skipping forever.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import struct
from pathlib import Path

import pytest

from habit_assistant.config import load_config, load_secrets

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "deploy"

_LINE_ENV_KEYS = {"LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET", "LINE_OWNER_USER_ID"}
_TELEGRAM_ENV_KEYS = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}


def _env_keys(path: Path) -> set[str]:
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


def _assert_lf_only_no_bom(path: Path) -> None:
    data = path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), f"{path.name} has a UTF-8 BOM (breaks a Linux shebang/systemd parse)"
    assert b"\r" not in data, f"{path.name} contains CR bytes (CRLF) -- must be LF-only to run on Linux"


def _parse_ini_directives(path: Path) -> dict[str, list[str]]:
    """Minimal systemd-unit-shaped `key=value` parser (no section nesting
    needed for the presence/value assertions below)."""
    directives: dict[str, list[str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("[") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        directives.setdefault(key.strip(), []).append(value.strip())
    return directives


# --- config.toml.line --------------------------------------------------------

def test_config_toml_line_loads_clean():
    config = load_config(REPO_ROOT / "config.toml.line")
    assert config.channel.type == "line"
    assert config.ollama.enabled is False
    assert config.line.bind_host == "127.0.0.1"
    assert config.line.bind_port == 8080
    assert config.line.media_dir == "data/media"
    assert config.line.rich_menu_image == "assets/richmenu/richmenu.png"
    assert config.digest.enabled is True
    assert config.digest.time == "20:00"
    assert config.digest.warn_cap == 280
    assert config.digest.include_weekly_review_day is True
    assert [h.id for h in config.habits] == ["water", "stretch", "diary"]


def test_config_toml_line_public_base_url_is_a_placeholder_to_edit():
    config = load_config(REPO_ROOT / "config.toml.line")
    # deploy/setup.sh never edits config.toml -- an operator must, per
    # docs/DEPLOY-LINE.md §5. A literal, unmistakable placeholder here
    # (rather than a real-looking URL) makes "did you forget this step"
    # obvious rather than silently pointing media URLs at nothing.
    assert "CHANGE-ME" in config.line.public_base_url


def test_config_toml_line_rich_menu_image_path_exists_in_repo():
    config = load_config(REPO_ROOT / "config.toml.line")
    assert (REPO_ROOT / config.line.rich_menu_image).is_file()


# --- .env templates vs the real Secrets shape --------------------------------

def test_env_line_example_has_all_three_line_secrets():
    keys = _env_keys(REPO_ROOT / ".env.line.example")
    assert _LINE_ENV_KEYS <= keys


def test_env_line_example_loads_via_load_secrets(tmp_path):
    """The exact key names in .env.line.example must round-trip through
    load_secrets(channel_type="line") with no missing-var error -- the
    real completeness check (R-D2), not just a key-name diff."""
    keys = _env_keys(REPO_ROOT / ".env.line.example")
    lines = []
    for key in sorted(keys):
        value = "U" + "a" * 32 if key == "LINE_OWNER_USER_ID" else "dummy-value"
        lines.append(f"{key}={value}")
    env_path = tmp_path / ".env"
    env_path.write_text("\n".join(lines), encoding="utf-8")

    secrets = load_secrets(env_path, channel_type="line")

    assert secrets.line_channel_access_token == "dummy-value"
    assert secrets.line_channel_secret == "dummy-value"
    assert secrets.line_owner_user_id == "U" + "a" * 32


def test_env_line_example_missing_a_var_still_raises_actionable_error(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("LINE_CHANNEL_SECRET=x\nLINE_CHANNEL_ACCESS_TOKEN=y\n", encoding="utf-8")

    with pytest.raises(Exception) as exc_info:
        load_secrets(env_path, channel_type="line")
    assert "line_owner_user_id" in str(exc_info.value)


def test_env_example_documents_both_channels():
    # .env.example stays the shared/general template (both channels this
    # branch can select via [channel].type); .env.line.example is the
    # LINE-only deployment-ready copy setup.sh actually installs.
    keys = _env_keys(REPO_ROOT / ".env.example")
    assert _TELEGRAM_ENV_KEYS <= keys
    assert _LINE_ENV_KEYS <= keys


# --- systemd units ------------------------------------------------------------

def test_main_service_unit_has_required_directives():
    directives = _parse_ini_directives(DEPLOY_DIR / "habit-assistant-line.service")
    assert directives["ExecStart"][0].endswith(".venv/bin/python -m habit_assistant.main")
    assert directives["WorkingDirectory"] == ["/opt/habit-assistant"]
    assert directives["EnvironmentFile"] == ["/opt/habit-assistant/.env"]
    assert directives["Restart"] == ["on-failure"]
    assert "RestartSec" in directives
    after_targets = " ".join(directives["After"])
    assert "network-online.target" in after_targets
    assert "tailscaled.service" in after_targets
    assert directives["WantedBy"] == ["multi-user.target"]


def test_backup_service_unit_uses_the_backup_flag():
    directives = _parse_ini_directives(DEPLOY_DIR / "habit-assistant-line-backup.service")
    assert directives["ExecStart"][0].endswith("-m habit_assistant.main --backup")
    assert directives["Type"] == ["oneshot"]


def test_backup_timer_unit_fires_daily_and_is_installable():
    directives = _parse_ini_directives(DEPLOY_DIR / "habit-assistant-line-backup.timer")
    assert "OnCalendar" in directives
    assert directives["Persistent"] == ["true"]
    assert directives["WantedBy"] == ["timers.target"]


def test_backup_cron_has_matching_schedule_and_flag():
    text = (DEPLOY_DIR / "backup.cron").read_text(encoding="utf-8")
    cron_lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert len(cron_lines) == 1
    assert cron_lines[0].startswith("30 3 * * *")  # 03:30, matching the timer's OnCalendar
    assert "--backup" in cron_lines[0]


@pytest.mark.parametrize(
    "unit_file",
    [
        "habit-assistant-line.service",
        "habit-assistant-line-backup.service",
        "habit-assistant-line-backup.timer",
        "backup.cron",
        "setup.sh",
        "run.sh",
    ],
)
def test_deploy_files_are_lf_only_no_bom(unit_file):
    _assert_lf_only_no_bom(DEPLOY_DIR / unit_file)


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not available on this box (e.g. Windows dev machine) -- run this suite on Linux/the target VPS for the real check")
@pytest.mark.parametrize(
    "unit_file",
    ["habit-assistant-line.service", "habit-assistant-line-backup.service", "habit-assistant-line-backup.timer"],
)
def test_systemd_analyze_when_available(unit_file):
    result = subprocess.run(
        ["systemd-analyze", "verify", str(DEPLOY_DIR / unit_file)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


# --- shell scripts -------------------------------------------------------------

def _find_real_bash() -> str | None:
    """`shutil.which("bash")` can resolve to Windows' own WSL launcher
    stub (System32\\bash.exe) ahead of Git Bash on PATH, which isn't a
    real shell at all -- it just prints "Windows Subsystem for Linux has
    no installed distributions" and exits 1, which would otherwise read
    as a bogus syntax-check failure. Try known-good candidates first,
    falling back to PATH, and confirm whichever one we pick actually
    identifies as GNU bash before trusting its `-n` result."""
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        shutil.which("bash"),
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            result = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=10)
        except OSError:
            continue
        if result.returncode == 0 and "bash" in result.stdout.lower():
            return candidate
    return None


_REAL_BASH = _find_real_bash()


@pytest.mark.skipif(_REAL_BASH is None, reason="no functional bash found (only a non-functional WSL launcher stub, or none at all) -- e.g. a Windows box without Git Bash/WSL installed")
@pytest.mark.parametrize("script", ["setup.sh", "run.sh"])
def test_shell_scripts_pass_bash_syntax_check(script):
    result = subprocess.run([_REAL_BASH, "-n", str(DEPLOY_DIR / script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", ["setup.sh", "run.sh"])
def test_shell_scripts_start_with_a_shebang_and_set_euo_pipefail(script):
    text = (DEPLOY_DIR / script).read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text


# --- setup.sh step 6: config.toml guard (hotfix v1.0.2) ------------------------
#
# config.toml is a TRACKED file in git (the repo's Telegram-flavored
# default -- `channel.type` defaults to "telegram" and it has no
# `[channel]` section at all), so on a fresh clone the OLD `if [ ! -f
# config.toml ]` guard never fired: `-f` was always true, and a LINE
# deploy silently kept running the Telegram config until someone manually
# `cp`'d config.toml.line over it. Step 6 alone is pure file I/O (no
# sudo/apt/systemd, unlike the rest of setup.sh), so these tests actually
# EXECUTE the real guard logic -- extracted verbatim from deploy/setup.sh
# between its own "# --- 6." / "# --- 7." markers -- against a throwaway
# REPO_ROOT, rather than just asserting on the script's text.


def _extract_step_6() -> str:
    text = (DEPLOY_DIR / "setup.sh").read_text(encoding="utf-8")
    start = text.index("# --- 6.")
    end = text.index("# --- 7.")
    return text[start:end]


def _run_step_6(repo_root: Path) -> str:
    script_path = repo_root / "_step6.sh"
    script = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'REPO_ROOT="$1"\n'
        'log() { echo "[setup.sh] $*"; }\n'
        + _extract_step_6()
    )
    # newline="\n" is deliberate (not the platform default): a CRLF-mangled
    # script here would fail with spurious "command not found: fi\r"-style
    # errors that have nothing to do with the guard logic under test.
    script_path.write_text(script, encoding="utf-8", newline="\n")
    result = subprocess.run([_REAL_BASH, str(script_path), str(repo_root)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


_LINE_CONFIG_STUB = '[channel]\ntype = "line"\n'


@pytest.mark.skipif(_REAL_BASH is None, reason="no functional bash found (only a non-functional WSL launcher stub, or none at all) -- e.g. a Windows box without Git Bash/WSL installed")
def test_setup_step6_fresh_repo_with_no_config_toml_installs_line_config(tmp_path):
    (tmp_path / "config.toml.line").write_text(_LINE_CONFIG_STUB, encoding="utf-8", newline="\n")

    stdout = _run_step_6(tmp_path)

    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == _LINE_CONFIG_STUB
    assert not (tmp_path / "config.toml.telegram.bak").exists()
    assert "Copying config.toml.line" in stdout


@pytest.mark.skipif(_REAL_BASH is None, reason="no functional bash found (only a non-functional WSL launcher stub, or none at all) -- e.g. a Windows box without Git Bash/WSL installed")
def test_setup_step6_fresh_clone_telegram_flavored_config_toml_gets_replaced(tmp_path):
    """The actual production bug: a fresh `git clone` always has
    config.toml present (it's tracked) and Telegram-flavored (no
    `type = "line"` anywhere). The old guard left it in place; the fix
    must back it up and install the real LINE config."""
    (tmp_path / "config.toml").write_text('[telegram]\npoll_timeout = 30\n', encoding="utf-8", newline="\n")
    (tmp_path / "config.toml.line").write_text(_LINE_CONFIG_STUB, encoding="utf-8", newline="\n")

    stdout = _run_step_6(tmp_path)

    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == _LINE_CONFIG_STUB
    assert (tmp_path / "config.toml.telegram.bak").read_text(encoding="utf-8") == "[telegram]\npoll_timeout = 30\n"
    assert "Telegram-flavored" in stdout
    assert "backing it up" in stdout


@pytest.mark.skipif(_REAL_BASH is None, reason="no functional bash found (only a non-functional WSL launcher stub, or none at all) -- e.g. a Windows box without Git Bash/WSL installed")
def test_setup_step6_hand_edited_line_config_is_never_clobbered(tmp_path):
    hand_edited = '[channel]\ntype = "line"\n\n[line]\npublic_base_url = "https://my-real-host.ts.net"\n'
    (tmp_path / "config.toml").write_text(hand_edited, encoding="utf-8", newline="\n")
    (tmp_path / "config.toml.line").write_text(_LINE_CONFIG_STUB, encoding="utf-8", newline="\n")

    stdout = _run_step_6(tmp_path)

    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == hand_edited
    assert not (tmp_path / "config.toml.telegram.bak").exists()
    assert "leaving it untouched" in stdout


@pytest.mark.skipif(_REAL_BASH is None, reason="no functional bash found (only a non-functional WSL launcher stub, or none at all) -- e.g. a Windows box without Git Bash/WSL installed")
def test_setup_step6_is_idempotent_once_the_line_config_is_installed(tmp_path):
    """docs/DEPLOY-LINE.md §10 "Updating" re-runs setup.sh after every
    `git pull` -- once config.toml is already the real LINE config, a
    second run must not re-trigger the backup path."""
    (tmp_path / "config.toml.line").write_text(_LINE_CONFIG_STUB, encoding="utf-8", newline="\n")
    _run_step_6(tmp_path)
    assert not (tmp_path / "config.toml.telegram.bak").exists()

    stdout = _run_step_6(tmp_path)

    assert not (tmp_path / "config.toml.telegram.bak").exists()
    assert "leaving it untouched" in stdout


# --- rich menu placeholder asset ----------------------------------------------

_LINE_RICH_MENU_VALID_SIZES = {(2500, 1686), (2500, 843)}


def _png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a valid PNG (bad signature)"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_richmenu_placeholder_is_a_valid_png_at_a_line_supported_size():
    png_path = REPO_ROOT / "assets" / "richmenu" / "richmenu.png"
    assert png_path.is_file()
    assert _png_dimensions(png_path) in _LINE_RICH_MENU_VALID_SIZES


def test_richmenu_placeholder_is_under_lines_1mb_limit():
    png_path = REPO_ROOT / "assets" / "richmenu" / "richmenu.png"
    assert png_path.stat().st_size < 1_000_000


def test_richmenu_readme_exists_and_flags_the_placeholder_status():
    readme = (REPO_ROOT / "assets" / "richmenu" / "README.md").read_text(encoding="utf-8")
    assert "placeholder" in readme.lower()
    assert "OQ3" in readme


# --- docs/DEPLOY-LINE.md ------------------------------------------------------

def test_deploy_line_doc_covers_the_required_runbook_sections():
    text = (REPO_ROOT / "docs" / "DEPLOY-LINE.md").read_text(encoding="utf-8")
    for must_contain in [
        "/callback",                       # webhook path
        "tailscale funnel",                # funnel setup
        "developers.line.biz",             # LINE console
        "Channel secret",                  # console step
        "Channel access token",            # console step
        "Auto-reply messages",             # disable per R-D3 dispatch note
        "Greeting messages",               # disable per R-D3 dispatch note
        "deploy/setup.sh",                 # setup script
        "backup",                          # backup cron/timer
        "Verify",                          # webhook verify button
        "Troubleshooting" if "Troubleshooting" in text else "troubleshooting",
    ]:
        assert must_contain in text, f"docs/DEPLOY-LINE.md missing expected coverage of: {must_contain!r}"


# --- R-D4: Linux-clean runtime audit (regression guard) -----------------------

# Integration pass (Archi-sanctioned extra, per TEST-LINE-D.md's own Gap 4
# finding / tests/test_line_d_gaps.py::test_windows_ism_guard_marker_list_
# would_miss_os_system_and_hardcoded_drive_paths): the original 6-marker
# list missed two of the most realistic Windows-isms -- `os.system('cls')`-
# class shell-out calls, and a hardcoded `C:\` (or any single-letter drive)
# path literal. `os.system(` is added as a plain substring marker, same
# mechanism as the other 6; a hardcoded drive-letter path can't be a plain
# substring (the drive letter itself varies), so it gets its own regex
# check in `test_src_has_no_windows_only_apis` below instead of a 7th
# literal entry here.
_WINDOWS_ONLY_MARKERS = [
    "winreg",
    "msvcrt",
    "ctypes.windll",
    "os.startfile",
    "PureWindowsPath",
    "WindowsPath(",
    "os.system(",
]

# Matches a quoted Windows drive-letter path root as it actually appears in
# REAL Python source text -- either an escaped-backslash literal
# (`"C:\\Users\\..."`, two source characters per separator, the common
# form since a lone `\U`/`\N`/etc. in a non-raw string is itself a
# SyntaxError) or a raw-string literal (`r"C:\Users\..."`, one character).
# `\\+` (one-or-more literal backslashes) catches both. The `[A-Za-z]:\`
# shape no Linux path can ever have (a POSIX path never has a `:`
# immediately followed by a backslash after a single letter) --
# deliberately does NOT match a bare URL scheme like `https://...` (no
# backslash there) or a type-annotated assignment (`x: str = ...`, no
# backslash either).
_DRIVE_LETTER_PATH_RE = re.compile(r"[\"'][A-Za-z]:\\+")


def test_src_has_no_windows_only_apis():
    """SPEC-LINE.md §4 R-D4: the runtime path (src/) must have no
    Windows-isms. Verified once by hand (grep across src/, clean) --
    this is the regression guard so a future change can't reintroduce
    one silently. The .ps1 launchers / .plist at the repo root are
    Windows/macOS-only by design and out of scope here (R-D1/D2 replace
    them for Linux, they aren't expected to be Linux-clean themselves)."""
    src_dir = REPO_ROOT / "src" / "habit_assistant"
    offenders = []
    for path in src_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for marker in _WINDOWS_ONLY_MARKERS:
            if marker in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {marker!r}")
        drive_letter_hit = _DRIVE_LETTER_PATH_RE.search(text)
        if drive_letter_hit:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: hardcoded drive-letter path {drive_letter_hit.group()!r}")
    assert not offenders, f"Windows-only APIs found in src/ (R-D4): {offenders}"
