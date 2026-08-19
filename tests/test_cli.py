"""CLI flag tests (AC9): --seed, --dry-run, --test-reminder.

The formal suite here is fully offline (subprocess for --seed since it
touches no network at all; in-process + monkeypatching for --dry-run and
--test-reminder since those would otherwise hit the real Ollama host /
api.telegram.org). Vera separately ran live spot-checks of --dry-run
against the reachable mac-mini Ollama server and --test-reminder against
the real api.telegram.org with a fake token -- see TEST.md for that
evidence; it is not a pytest dependency.
"""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from habit_assistant.main import build_arg_parser


# ---------------------------------------------------------------------------
# argparse shape
# ---------------------------------------------------------------------------


def test_build_arg_parser_defaults():
    args = build_arg_parser().parse_args([])
    assert args.test_reminder is None
    assert args.seed is False
    assert args.dry_run is None


def test_build_arg_parser_rejects_unknown_test_reminder_category():
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--test-reminder", "not-a-category"])


def test_build_arg_parser_accepts_known_categories():
    parser = build_arg_parser()
    for category in ("water", "stretch", "diary"):
        args = parser.parse_args(["--test-reminder", category])
        assert args.test_reminder == category


def test_build_arg_parser_dry_run_takes_a_message():
    args = build_arg_parser().parse_args(["--dry-run", "500ml"])
    assert args.dry_run == "500ml"


# ---------------------------------------------------------------------------
# --seed: fully offline, runs the real CLI entry point as a subprocess with
# cwd set to a scratch tmp_path so the seeded DB never touches the real
# repo's data/habits.db (config.toml's db_path is relative and resolves
# against the process's cwd).
# ---------------------------------------------------------------------------


def test_seed_flag_runs_cleanly_and_inserts_rows(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "habit_assistant.main", "--seed"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

    db_path = tmp_path / "data" / "habits.db"
    assert db_path.exists()

    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        assert count > 0
        categories = {row[0] for row in conn.execute("SELECT DISTINCT category FROM logs").fetchall()}
        assert categories <= {"water", "stretch", "diary"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# --dry-run: in-process via async_main with a monkeypatched OllamaClient
# class (no real network) -- confirms the wiring: parse -> print, no DB
# write, no Telegram involved.
# ---------------------------------------------------------------------------


class _FakeOllamaClientForDryRun:
    def __init__(self, *args, **kwargs):
        pass

    async def chat_json(self, system_prompt, user_prompt, json_schema):
        return json.dumps(
            {"category": "water", "water_ml": 500, "stretch_min": None, "diary_text": None, "confidence": 0.9}
        )

    async def chat_text(self, system_prompt, user_prompt):
        return "reflection"

    async def aclose(self):
        pass


async def test_dry_run_flag_via_async_main_prints_structured_result_offline(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace as NS

    from habit_assistant import main as main_module

    config = main_module.Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClientForDryRun)

    args = NS(seed=False, dry_run="500ml", test_reminder=None)
    await main_module.async_main(args)

    captured = capsys.readouterr()
    assert "'category': 'water'" in captured.out
    assert "'water_ml': 500" in captured.out

    # No DB row should be written for a dry-run.
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "habits.db"))
    try:
        count = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        assert count == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# --test-reminder: in-process via async_main with a monkeypatched
# TelegramChannel (no real network) -- confirms the CLI branch actually
# calls send_reminder with the right category and then exits.
# ---------------------------------------------------------------------------


class _FakeTelegramChannelForReminder:
    sent: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def send(self, text: str) -> None:
        _FakeTelegramChannelForReminder.sent.append(text)

    async def run(self, on_message):
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


async def test_test_reminder_flag_sends_correct_reminder_text_offline(tmp_path, monkeypatch):
    """ROADMAP.md v0.6.0 AC6.3: --test-reminder is an unprompted send, so
    it goes through `i18n.resolve_unprompted_language(config)` just like a
    real scheduled reminder -- with a default `Config()` (i18n.language=
    "auto", i18n.primary_language="th"), that resolves to Thai, not the
    English `REMINDER_TEXTS["stretch"]` this test asserted pre-v0.6.0.
    CHANGED (v0.6.0): expected text was `REMINDER_TEXTS["stretch"]`
    (English); now `i18n.t("reminder_stretch", "th")` (Thai) -- same
    catalog entry, resolved for the language this CLI path actually uses
    in production."""
    from habit_assistant import main as main_module
    from habit_assistant.core import i18n

    _FakeTelegramChannelForReminder.sent = []
    config = main_module.Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(main_module, "load_secrets", lambda: SimpleNamespace(telegram_bot_token="t", telegram_chat_id="c"))
    monkeypatch.setattr(main_module, "TelegramChannel", _FakeTelegramChannelForReminder)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClientForDryRun)

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder="stretch")
    await main_module.async_main(args)

    assert _FakeTelegramChannelForReminder.sent == [i18n.t("reminder_stretch", "th")]


class _FakeTelegramChannelUnauthorized:
    """Reproduces the real 401 path: TelegramChannel.send() raises
    httpx.HTTPStatusError when Telegram rejects a bad/fake token, exactly
    as confirmed by a live round-trip to api.telegram.org during this test
    session (see TEST.md)."""

    def __init__(self, *args, **kwargs):
        pass

    async def send(self, text: str) -> None:
        request = httpx.Request("POST", "https://api.telegram.org/botfake/sendMessage")
        response = httpx.Response(401, request=request, json={"ok": False, "description": "Unauthorized"})
        raise httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)

    async def run(self, on_message):
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


async def test_test_reminder_flag_fails_cleanly_on_401_not_crash(tmp_path, monkeypatch):
    """SPEC.md §10's --test-reminder is a dev affordance; a bad/expired
    token should surface as a clean, actionable failure (matching the
    ConfigError pattern already used for missing secrets), not an
    unhandled httpx.HTTPStatusError traceback out of async_main.

    KNOWN FAILURE as of this test run: main.py's `--test-reminder` branch
    calls `send_reminder(channel, ...)` -> `channel.send()` with no
    try/except around the network call, so a live round-trip against
    api.telegram.org with a fake token (verified separately, see TEST.md)
    produces a raw traceback and only incidentally exits 1 (Python's
    default unhandled-exception behavior, not a deliberate clean exit).
    """
    from habit_assistant import main as main_module

    config = main_module.Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module, "load_secrets", lambda: SimpleNamespace(telegram_bot_token="fake", telegram_chat_id="fake")
    )
    monkeypatch.setattr(main_module, "TelegramChannel", _FakeTelegramChannelUnauthorized)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClientForDryRun)

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder="water")

    with pytest.raises(SystemExit) as excinfo:
        await main_module.async_main(args)
    assert excinfo.value.code == 1


async def test_missing_secrets_exits_with_code_1(tmp_path, monkeypatch, capsys):
    """--test-reminder (or a real run) with no .env / secrets available
    must exit(1) with a clear message, not raise an unhandled exception."""
    from habit_assistant import main as main_module
    from habit_assistant.config import ConfigError

    config = main_module.Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    monkeypatch.setattr(main_module, "load_config", lambda: config)

    def raise_config_error():
        raise ConfigError("Could not load Telegram credentials from .env (missing: telegram_bot_token)...")

    monkeypatch.setattr(main_module, "load_secrets", raise_config_error)

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder="water")
    with pytest.raises(SystemExit) as excinfo:
        await main_module.async_main(args)

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "telegram_bot_token" in captured.err.lower()
