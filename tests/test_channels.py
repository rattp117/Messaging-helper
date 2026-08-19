"""Channel abstraction tests (AC3):
- Channel ABC shape.
- TelegramChannel.send / .run against a mocked transport (request
  construction only -- never a real Telegram call).
- The `core/` and `storage/` seam: no concrete channel import anywhere in
  those packages.
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.channels.telegram import TelegramChannel

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "habit_assistant"


# ---------------------------------------------------------------------------
# Channel ABC shape
# ---------------------------------------------------------------------------


def test_channel_is_abstract_and_cannot_be_instantiated():
    with pytest.raises(TypeError):
        Channel()  # type: ignore[abstract]


def test_telegram_channel_is_a_channel():
    channel = TelegramChannel("fake-token", "12345")
    assert isinstance(channel, Channel)


# ---------------------------------------------------------------------------
# TelegramChannel.send -- request construction + mocked transport, no real call
# ---------------------------------------------------------------------------


def test_build_send_request_shape():
    channel = TelegramChannel("123456:ABC-fake", "999")

    url, payload = channel.build_send_request("hello world")

    assert url == "https://api.telegram.org/bot123456:ABC-fake/sendMessage"
    assert payload == {"chat_id": "999", "text": "hello world"}


async def test_send_posts_to_send_message_endpoint_with_mocked_transport():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("123456:ABC-fake", "999", client=client)

    await channel.send("✅ 500 ml logged")

    assert len(captured) == 1
    assert captured[0].url.path == "/bot123456:ABC-fake/sendMessage"
    await channel.aclose()


async def test_send_raises_on_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("bad-token", "999", client=client)

    with pytest.raises(httpx.HTTPStatusError):
        await channel.send("hi")
    await channel.aclose()


# ---------------------------------------------------------------------------
# TelegramChannel.run -- long-poll loop against a mocked transport
#
# `TelegramChannel.run` is `while True` and swallows on_message exceptions
# (that's the behavior under test in the third case below), so we can't stop
# the loop by raising from on_message. Instead each test's transport handler
# serves a fixed queue of poll responses and then raises `StopPolling` --
# an exception type NOT caught by `run`'s `except httpx.HTTPError`, so it
# propagates out of `await channel.run(...)` and ends the test.
# ---------------------------------------------------------------------------


class StopPolling(Exception):
    """Sentinel raised by a test transport once its canned responses are
    exhausted, to end TelegramChannel.run's `while True` loop."""


def _queued_response_handler(responses: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        if not responses:
            raise StopPolling()
        return httpx.Response(200, json=responses.pop(0))

    return handler


async def test_run_calls_on_message_for_each_inbound_text_and_advances_offset():
    calls: list[str] = []
    responses = [
        {
            "ok": True,
            "result": [
                {"update_id": 1, "message": {"text": "500ml"}},
                {"update_id": 2, "message": {"text": "10 min stretch"}},
            ],
        },
    ]

    transport = httpx.MockTransport(_queued_response_handler(responses))
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("token", "chat", client=client)

    async def on_message(text: str) -> None:
        calls.append(text)

    with pytest.raises(StopPolling):
        await channel.run(on_message)

    assert calls == ["500ml", "10 min stretch"]
    assert channel._offset == 3  # last update_id (2) + 1
    await channel.aclose()


async def test_run_skips_updates_without_message_text():
    responses = [
        {
            "ok": True,
            "result": [
                {"update_id": 5, "edited_message": {"text": "no text field on message"}},
                {"update_id": 6, "message": {"sticker": {}}},  # message with no "text"
                {"update_id": 7, "message": {"text": "500ml"}},
            ],
        },
    ]

    transport = httpx.MockTransport(_queued_response_handler(responses))
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("token", "chat", client=client)

    calls: list[str] = []

    async def on_message(text: str) -> None:
        calls.append(text)

    with pytest.raises(StopPolling):
        await channel.run(on_message)

    assert calls == ["500ml"]
    await channel.aclose()


async def test_run_on_message_exception_does_not_crash_the_loop():
    """A handler exception must be swallowed so the inbound loop keeps
    running (per channels/telegram.py's try/except around on_message):
    both updates in the same poll batch must still reach on_message even
    though the first one raises."""
    responses = [
        {
            "ok": True,
            "result": [
                {"update_id": 1, "message": {"text": "boom"}},
                {"update_id": 2, "message": {"text": "next"}},
            ],
        },
    ]

    transport = httpx.MockTransport(_queued_response_handler(responses))
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("token", "chat", client=client)

    calls: list[str] = []

    async def on_message(text: str) -> None:
        calls.append(text)
        if text == "boom":
            raise RuntimeError("handler blew up")

    with pytest.raises(StopPolling):
        await channel.run(on_message)

    assert calls == ["boom", "next"]
    await channel.aclose()


# ---------------------------------------------------------------------------
# Channel seam: no concrete channel import in core/ or storage/ (SPEC.md §8)
# ---------------------------------------------------------------------------


def _imported_module_names(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("package", ["core", "storage"])
def test_core_and_storage_do_not_import_concrete_channels(package):
    forbidden_substrings = ("channels.telegram", "channels.line")
    offenders = []
    for py_file in (SRC_ROOT / package).glob("*.py"):
        for module_name in _imported_module_names(py_file):
            if any(sub in module_name for sub in forbidden_substrings):
                offenders.append((py_file.name, module_name))

    assert offenders == [], f"core/storage modules must only depend on channels.base, found: {offenders}"


@pytest.mark.parametrize("package", ["core", "storage"])
def test_core_and_storage_may_still_reference_channel_abc(package):
    """Sanity check the AST scan itself isn't vacuously passing -- at least
    one module in core/ should legitimately import the Channel ABC."""
    if package != "core":
        pytest.skip("only core/ wires the Channel ABC directly")
    found_base_import = any(
        any("channels.base" in module_name for module_name in _imported_module_names(py_file))
        for py_file in (SRC_ROOT / package).glob("*.py")
    )
    assert found_base_import
