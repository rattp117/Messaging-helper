"""Deliverable-presence tests (AC10): .env.example, .gitignore excludes
.env and data/, README covers macOS/Windows setup, and the launchd plist
exists with the expected keep-alive shape."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_env_example_exists_and_documents_required_vars():
    path = REPO_ROOT / ".env.example"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN" in text
    assert "TELEGRAM_CHAT_ID" in text


def test_env_example_does_not_contain_the_real_env_file_itself():
    """.env.example must never be a copy of a real, filled-in .env."""
    path = REPO_ROOT / ".env.example"
    text = path.read_text(encoding="utf-8")
    # Real Telegram bot tokens look like digits:35-char-string; the example
    # should read as an obvious placeholder, not something plausible.
    assert "your-bot-token-here" in text or "fake" in text.lower() or "example" in text.lower() or "123456" in text


def test_gitignore_excludes_env_and_data():
    path = REPO_ROOT / ".gitignore"
    assert path.exists()
    lines = {line.strip() for line in path.read_text(encoding="utf-8").splitlines()}
    assert ".env" in lines
    assert "data/" in lines


def test_readme_exists_and_covers_setup_topics():
    path = REPO_ROOT / "README.md"
    assert path.exists()
    text = path.read_text(encoding="utf-8").lower()
    for topic in ("venv", "ollama pull", ".env", "launchd"):
        assert topic in text, f"README.md should mention {topic!r}"


def test_launchd_plist_exists_with_keepalive_and_run_at_load():
    path = REPO_ROOT / "com.ratt.habit-assistant.plist"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "KeepAlive" in text
    assert "RunAtLoad" in text


def test_pyproject_declares_pytest_config_and_dev_deps():
    path = REPO_ROOT / "pyproject.toml"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "pytest" in text
    assert "testpaths" in text


def test_config_toml_exists_at_repo_root():
    assert (REPO_ROOT / "config.toml").exists()


def test_line_channel_is_implemented_via_the_webhook_server():
    """SPEC-LINE.md Module A replaced the pre-LINE-branch documented stub
    (which just asserted "webhook" in the docstring + a NotImplementedError
    placeholder) with a real implementation -- channels/line.py now wires
    an actual webhook server (channels/line_webhook.py) instead of raising.
    tests/test_line_channel.py and tests/test_line_webhook.py cover the
    real behavior in full; this deliverable-presence check just confirms
    the file exists and is no longer a stub."""
    path = REPO_ROOT / "src" / "habit_assistant" / "channels" / "line.py"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "webhook" in text.lower()
    assert "class LineChannel" in text
    assert "NotImplementedError" not in text
