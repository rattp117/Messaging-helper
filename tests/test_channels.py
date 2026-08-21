"""Channel abstraction tests (AC3):
- Channel ABC shape.
- TelegramChannel.send / .run against a mocked transport (request
  construction only -- never a real Telegram call).
- The `core/` and `storage/` seam: no concrete channel import anywhere in
  those packages.
"""

from __future__ import annotations

import ast
import json
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


# ---------------------------------------------------------------------------
# SPEC-v1.1.md R-U7 (AC10): send_actionable/set_my_commands/answer_callback_
# query are concrete defaults on the Channel ABC -- a minimal subclass that
# overrides only send()/run() (mirroring every one of the ~15 existing test
# fakes and channels/line.py's stub) must keep working unmodified.
# ---------------------------------------------------------------------------


class _BareChannel(Channel):
    """The minimal legal Channel subclass: only the two abstractmethods
    implemented, exactly like every pre-v1.1 fake/stub."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message):
        raise NotImplementedError("not exercised in these tests")


async def test_send_actionable_default_degrades_to_plain_send():
    channel = _BareChannel()
    await channel.send_actionable("hello", [("↩️ Undo", "undo:1")])
    assert channel.sent == ["hello"]


async def test_set_my_commands_default_is_a_silent_noop():
    channel = _BareChannel()
    result = await channel.set_my_commands({"en": [("undo", "Undo the last entry")]})
    assert result is None


async def test_answer_callback_query_default_is_a_silent_noop():
    channel = _BareChannel()
    result = await channel.answer_callback_query("cb-123")
    assert result is None


async def test_line_channel_run_stub_accepts_on_callback_kwarg():
    """channels/line.py's stub widened its `run` signature to match the
    Channel ABC (SPEC-v1.1.md §6) without becoming implemented -- it must
    still raise NotImplementedError, now also when called with on_callback.
    `__init__` itself already raises NotImplementedError (documented stub),
    so the instance is built via `object.__new__` to reach `run` at all."""
    from habit_assistant.channels.line import LineChannel

    instance = object.__new__(LineChannel)

    async def on_message(text: str) -> None:
        pass

    async def on_callback(data: str, source_text: str, cb_id: str) -> None:
        pass

    with pytest.raises(NotImplementedError):
        await instance.run(on_message, on_callback=on_callback)


# ---------------------------------------------------------------------------
# TelegramChannel.send_actionable / set_my_commands / answer_callback_query
# -- request construction (mirrors build_send_request's own test shape).
# ---------------------------------------------------------------------------


def test_build_send_actionable_request_shape():
    channel = TelegramChannel("123456:ABC-fake", "999")

    url, payload = channel.build_send_actionable_request("500ml logged", [("↩️ Undo", "undo:42")])

    assert url == "https://api.telegram.org/bot123456:ABC-fake/sendMessage"
    assert payload["chat_id"] == "999"
    assert payload["text"] == "500ml logged"
    assert payload["reply_markup"] == {
        "inline_keyboard": [[{"text": "↩️ Undo", "callback_data": "undo:42"}]]
    }


async def test_send_actionable_posts_with_reply_markup():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("123456:ABC-fake", "999", client=client)

    await channel.send_actionable("500ml logged", [("↩️ Undo", "undo:42")])

    assert len(captured) == 1
    body = json.loads(captured[0].content)
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "undo:42"
    await channel.aclose()


def test_build_set_my_commands_requests_one_per_language():
    channel = TelegramChannel("123456:ABC-fake", "999")

    requests = channel.build_set_my_commands_requests(
        {
            "en": [("undo", "Undo the last entry"), ("target", "View or set a daily goal")],
            "th": [("undo", "ยกเลิกรายการล่าสุด"), ("target", "ดูหรือตั้งเป้าหมายรายวัน")],
        }
    )

    assert len(requests) == 2
    urls = {url for url, _ in requests}
    assert urls == {"https://api.telegram.org/bot123456:ABC-fake/setMyCommands"}

    payloads_by_lang = {}
    for _, payload in requests:
        payloads_by_lang["th" if "language_code" in payload else "en"] = payload

    assert "language_code" not in payloads_by_lang["en"]
    assert payloads_by_lang["th"]["language_code"] == "th"
    assert {c["command"] for c in payloads_by_lang["en"]["commands"]} == {"undo", "target"}
    assert {c["command"] for c in payloads_by_lang["th"]["commands"]} == {"undo", "target"}


async def test_set_my_commands_posts_one_request_per_language():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("123456:ABC-fake", "999", client=client)

    await channel.set_my_commands(
        {"en": [("undo", "Undo the last entry")], "th": [("undo", "ยกเลิกรายการล่าสุด")]}
    )

    assert len(captured) == 2
    assert all(r.url.path == "/bot123456:ABC-fake/setMyCommands" for r in captured)
    await channel.aclose()


async def test_answer_callback_query_posts_callback_id():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("123456:ABC-fake", "999", client=client)

    await channel.answer_callback_query("cb-abc123")

    assert len(captured) == 1
    assert captured[0].url.path == "/bot123456:ABC-fake/answerCallbackQuery"
    body = json.loads(captured[0].content)
    assert body == {"callback_query_id": "cb-abc123"}
    await channel.aclose()


# ---------------------------------------------------------------------------
# TelegramChannel.run -- callback_query handling (SPEC-v1.1.md R-U4, AC6/AC9)
# ---------------------------------------------------------------------------


async def test_run_routes_callback_query_to_on_callback_and_answers_it():
    responses = [
        {
            "ok": True,
            "result": [
                {
                    "update_id": 10,
                    "callback_query": {
                        "id": "cbid-1",
                        "data": "undo:1234",
                        "message": {"text": "✅ 500 ml logged", "message_id": 42},
                        "from": {"id": 1},
                    },
                }
            ],
        },
    ]

    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            if not responses:
                raise StopPolling()
            return httpx.Response(200, json=responses.pop(0))
        posts.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("token", "chat", client=client)

    callback_calls: list[tuple[str, str, str]] = []

    async def on_callback(data: str, source_text: str, cb_id: str) -> None:
        callback_calls.append((data, source_text, cb_id))

    async def on_message(text: str) -> None:
        raise AssertionError("on_message must not be called for a callback_query update")

    with pytest.raises(StopPolling):
        await channel.run(on_message, on_callback=on_callback)

    assert callback_calls == [("undo:1234", "✅ 500 ml logged", "cbid-1")]
    assert channel._offset == 11
    assert len(posts) == 1  # exactly one answerCallbackQuery call
    assert posts[0].url.path.endswith("/answerCallbackQuery")
    body = json.loads(posts[0].content)
    assert body["callback_query_id"] == "cbid-1"
    await channel.aclose()


async def test_run_answers_callback_query_even_when_on_callback_raises():
    """R-U4: answerCallbackQuery must fire even on a handler error, so the
    client's spinner still clears."""
    responses = [
        {
            "ok": True,
            "result": [
                {
                    "update_id": 20,
                    "callback_query": {"id": "cbid-2", "data": "foo", "message": {}, "from": {"id": 1}},
                }
            ],
        },
    ]

    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            if not responses:
                raise StopPolling()
            return httpx.Response(200, json=responses.pop(0))
        posts.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("token", "chat", client=client)

    async def on_callback(data: str, source_text: str, cb_id: str) -> None:
        raise RuntimeError("boom")

    async def on_message(text: str) -> None:
        pass

    with pytest.raises(StopPolling):
        await channel.run(on_message, on_callback=on_callback)

    assert len(posts) == 1
    assert posts[0].url.path.endswith("/answerCallbackQuery")
    await channel.aclose()


async def test_run_answers_callback_query_even_when_on_callback_is_none():
    """A caller that doesn't pass on_callback (back-compat) must still get
    its callback_query updates answered, not silently dropped."""
    responses = [
        {
            "ok": True,
            "result": [{"update_id": 30, "callback_query": {"id": "cbid-3", "data": "undo:1", "message": {}}}],
        },
    ]

    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            if not responses:
                raise StopPolling()
            return httpx.Response(200, json=responses.pop(0))
        posts.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("token", "chat", client=client)

    async def on_message(text: str) -> None:
        pass

    with pytest.raises(StopPolling):
        await channel.run(on_message)  # no on_callback given

    assert len(posts) == 1
    assert posts[0].url.path.endswith("/answerCallbackQuery")
    await channel.aclose()


async def test_run_offset_advances_past_a_mix_of_messages_and_callbacks():
    responses = [
        {
            "ok": True,
            "result": [
                {"update_id": 40, "message": {"text": "500ml"}},
                {"update_id": 41, "callback_query": {"id": "cbid-4", "data": "undo:9", "message": {}}},
                {"update_id": 42, "message": {"text": "10 min stretch"}},
            ],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            if not responses:
                raise StopPolling()
            return httpx.Response(200, json=responses.pop(0))
        return httpx.Response(200, json={"ok": True, "result": True})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("token", "chat", client=client)

    messages: list[str] = []

    async def on_message(text: str) -> None:
        messages.append(text)

    async def on_callback(data: str, source_text: str, cb_id: str) -> None:
        pass

    with pytest.raises(StopPolling):
        await channel.run(on_message, on_callback=on_callback)

    assert messages == ["500ml", "10 min stretch"]
    assert channel._offset == 43  # last update_id (42) + 1 -- no update skipped or reprocessed
    await channel.aclose()
