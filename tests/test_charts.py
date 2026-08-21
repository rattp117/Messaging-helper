"""Vera (tester) coverage for ROADMAP.md v1.0.0 "Insights: Charts-as-Images
+ Garmin Import", the chart/channel half (AC1.0.1, AC1.0.2, and the
chart-rendering slice of AC1.0.5). Luna wrote NO formal tests this round
(smoke script only, documented in IMPL.md) -- this file plus
tests/test_garmin.py is the entire v1.0.0 AC test suite.

AC -> section map:
- AC1.0.2 -> "TelegramChannel.send_image / Channel ABC default degrade"
- AC1.0.1 -> "core/charts.py rendering" + "core/review.py chart wiring" +
  "main.py weekly_review_job wiring (text-then-images, fail-open)"
- AC1.0.5 -> "No new outbound host" (charts + Garmin combined, since the
  captured-transport fixture needs both channel and DB wired up)
- Audit -> "Version consistency" + "matplotlib import guard" (the same
  MATPLOTLIB_AVAILABLE=False simulation covers AC1.0.1's matplotlib-absent
  case and the README/pyproject install-guard claim -- one implementation,
  cited for both per the task brief)
- Regression -> "run_weekly_review byte-identical to v0.10.0 when Garmin
  unconfigured" (charts never touch run_weekly_review's text output at all
  -- only render_weekly_review_charts, a separate function -- so this pin
  is really a Garmin-off pin; see tests/test_garmin.py for the fuller
  Garmin-specific regression note)

Conventions borrowed from the existing suite: `_synthetic_habit` mirrors
tests/test_review.py's copy; `FakeChannel` mirrors tests/test_streaks.py's
copy; `_FakeScheduler`/`_FakeTelegramChannel`/`_StopAfterSchedulerStart`
mirror tests/test_reminders.py and tests/test_streaks.py's own extended
copy (adds send_image recording + a way to invoke the weekly_review job
from inside run()); the mocked-transport request-shape pattern mirrors
tests/test_channels.py.

Live-environment rule: every DB in this file is a scratch tmp_path SQLite
file. No real Telegram or Ollama call is ever made -- Telegram is always
either a bare Channel subclass, a MockTransport-backed TelegramChannel, or
the _FakeTelegramChannel test double; Ollama is always a FakeLLM or a
monkeypatched main_module.OllamaClient. Nothing here ever opens
data/habits.db or reads the real .env."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Awaitable, Callable

import httpx
import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.channels.line import LineChannel
from habit_assistant.channels.telegram import TelegramChannel
from habit_assistant.config import Config
from habit_assistant.core import charts
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.review import compute_weekly_stats, render_weekly_review_charts, run_weekly_review
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

REPO_ROOT = Path(__file__).resolve().parents[1]

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _synthetic_habit(id_: str, type_: str, **kw) -> Habit:
    defaults = dict(
        label_en=id_,
        label_th=id_,
        unit_en="ml" if type_ in ("numeric", "duration") else None,
        unit_th="มล." if type_ in ("numeric", "duration") else None,
        goal=None,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )
    defaults.update(kw)
    return Habit(id=id_, type=type_, **defaults)


def _default_registry() -> HabitRegistry:
    return HabitRegistry.from_config(Config())


def _seed(db: Database, ts: str, category: str, value_num: float | None, raw: str = "x") -> int:
    return db.insert_log(LogEntry(None, ts, category, value_num, None, raw, "reply"))


def _seed_known_week(db: Database, end_date: date) -> None:
    """Same shape as test_review.py's fixture: water 500ml every day,
    stretch on the last 3 days only."""
    for offset in range(6, -1, -1):
        d = (end_date - timedelta(days=offset)).isoformat()
        _seed(db, f"{d}T09:00:00", "water", 500.0)
        if offset <= 2:
            _seed(db, f"{d}T11:00:00", "stretch", 10.0)


class FakeLLM:
    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return "noted"


class FakeChannel(Channel):
    """Records both plain sends and image sends, in call order, so a test
    can assert the text-then-images ordering main.py's weekly_review_job
    depends on."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.images: list[tuple[bytes, str]] = []
        self.calls: list[str] = []  # "send" / "send_image" in call order

    async def send(self, text: str) -> None:
        self.sent.append(text)
        self.calls.append("send")

    async def send_image(self, image: bytes, caption: str) -> None:
        self.images.append((image, caption))
        self.calls.append("send_image")

    async def run(self, on_message: Callable[[str], Awaitable[None]]) -> None:
        raise NotImplementedError("not exercised in these tests")


class ImagelessChannel(Channel):
    """Deliberately does NOT override send_image -- exercises the ABC's
    default degrade-to-text-send behavior (AC1.0.2)."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message: Callable[[str], Awaitable[None]]) -> None:
        raise NotImplementedError("not exercised in these tests")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


# ---------------------------------------------------------------------------
# AC1.0.2 -- TelegramChannel.send_image / Channel ABC default degrade
# ---------------------------------------------------------------------------


def test_build_send_image_request_shape():
    channel = TelegramChannel("123456:ABC-fake", "999")

    url, data, files = channel.build_send_image_request(b"fake-png-bytes", "a caption")

    assert url == "https://api.telegram.org/bot123456:ABC-fake/sendPhoto"
    assert data == {"chat_id": "999", "caption": "a caption"}
    assert files["photo"][0] == "chart.png"
    assert files["photo"][1] == b"fake-png-bytes"
    assert files["photo"][2] == "image/png"


async def test_send_image_posts_multipart_to_send_photo_endpoint_with_mocked_transport():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("123456:ABC-fake", "999", client=client)

    await channel.send_image(PNG_MAGIC + b"...rest-of-png...", "Water: 16400 ml this week")

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/bot123456:ABC-fake/sendPhoto"
    assert req.url.host == "api.telegram.org"
    body = req.content
    assert b"chart.png" in body
    assert b"Water: 16400 ml this week" in body
    assert PNG_MAGIC in body
    await channel.aclose()


async def test_send_image_raises_on_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("bad-token", "999", client=client)

    with pytest.raises(httpx.HTTPStatusError):
        await channel.send_image(b"x", "caption")
    await channel.aclose()


async def test_channel_abc_default_send_image_degrades_to_plain_send_without_touching_core():
    """AC1.0.2: "a channel without an image implementation degrades to a
    text send -- verified without touching core/". This test imports only
    channels.base.Channel and a local subclass -- no core/ import
    anywhere in this test."""
    channel = ImagelessChannel()

    await channel.send_image(b"some-png-bytes", "caption text")

    assert channel.sent == ["caption text"]


def test_channel_send_image_default_is_defined_on_the_abc_itself():
    """Sanity check the default lives on Channel, not something each
    subclass must reimplement -- ImagelessChannel above never defined
    send_image at all."""
    assert "send_image" in Channel.__dict__
    assert "send_image" not in ImagelessChannel.__dict__


def test_line_channel_stub_still_imports_and_is_a_valid_channel_subclass():
    """Regression check on the ABC extension: adding a concrete (not
    @abstractmethod) send_image to Channel must not change whether
    channels/line.py's documented stub is a valid, instantiable-in-principle
    subclass. LineChannel only overrides send/run (not send_image) and its
    __init__ deliberately raises NotImplementedError itself (documented
    stub) -- so instantiating it must fail with THAT specific, intentional
    error, not a TypeError from abc.ABCMeta complaining about an unimplemented
    abstract method (which is what would happen if send_image had been added
    as @abstractmethod instead of a default)."""
    assert issubclass(LineChannel, Channel)
    with pytest.raises(NotImplementedError):
        LineChannel()


# ---------------------------------------------------------------------------
# AC1.0.1 -- core/charts.py rendering
# ---------------------------------------------------------------------------


def test_matplotlib_is_actually_installed_in_this_venv():
    """Precondition sanity check -- IMPL.md claims matplotlib was installed
    specifically so rendering could be exercised for real, not just
    import-guarded. If this fails, every other "real PNG bytes" assertion
    below is meaningless (they'd all be silently hitting the
    MATPLOTLIB_AVAILABLE=False branch instead)."""
    assert charts.MATPLOTLIB_AVAILABLE is True


def test_render_habit_chart_numeric_produces_real_png_bytes(db):
    habit = _synthetic_habit("water", "numeric", goal=2500.0)
    end_date = date(2026, 8, 19)
    _seed(db, "2026-08-19T09:00:00", "water", 2500.0)

    image = charts.render_habit_chart(db, Config(), habit, end_date, "en")

    assert image is not None
    assert image[:8] == PNG_MAGIC
    assert len(image) > 500  # not a stub/placeholder-sized blob


def test_render_habit_chart_duration_produces_real_png_bytes(db):
    habit = _synthetic_habit("stretch", "duration")
    end_date = date(2026, 8, 19)
    _seed(db, "2026-08-19T09:00:00", "stretch", 10.0)

    image = charts.render_habit_chart(db, Config(), habit, end_date, "en")

    assert image is not None
    assert image[:8] == PNG_MAGIC


def test_render_habit_chart_boolean_produces_real_png_bytes(db):
    habit = _synthetic_habit("meds", "boolean", unit_en=None, unit_th=None)
    end_date = date(2026, 8, 19)
    _seed(db, "2026-08-19T09:00:00", "meds", 1.0)

    image = charts.render_habit_chart(db, Config(), habit, end_date, "en")

    assert image is not None
    assert image[:8] == PNG_MAGIC


def test_render_habit_chart_text_habit_returns_none(db):
    """Text habits (e.g. diary) have nothing numeric to plot."""
    habit = _synthetic_habit("diary", "text", unit_en=None, unit_th=None)
    end_date = date(2026, 8, 19)

    image = charts.render_habit_chart(db, Config(), habit, end_date, "en")

    assert image is None


def test_render_weekly_charts_skips_text_habits_but_charts_the_rest(db):
    registry = _default_registry()  # water(numeric), stretch(duration), diary(text)
    end_date = date(2026, 8, 19)
    _seed_known_week(db, end_date)

    pairs = charts.render_weekly_charts(db, Config(), registry, end_date, "en")

    habit_ids = [h.id for h, _ in pairs]
    assert habit_ids == ["water", "stretch"]  # diary (text) skipped, registry order preserved
    for _, image in pairs:
        assert image[:8] == PNG_MAGIC


def test_render_habit_chart_render_failure_is_caught_and_returns_none(db, monkeypatch):
    """AC1.0.1's "rendering fails -> falls back, no crash" case: a
    matplotlib-layer exception must be swallowed by render_habit_chart's
    own try/except, not propagate."""
    habit = _synthetic_habit("water", "numeric", goal=2500.0)
    end_date = date(2026, 8, 19)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated matplotlib failure")

    monkeypatch.setattr(charts, "_render_bar_chart", boom)

    image = charts.render_habit_chart(db, Config(), habit, end_date, "en")

    assert image is None


def test_render_habit_chart_matplotlib_absent_returns_none_and_warns_once(db, monkeypatch, caplog):
    """AC1.0.1's matplotlib-absent fallback + the README/pyproject install
    guard claim (Audit item 7): simulate an environment without the
    optional [charts] extra by flipping MATPLOTLIB_AVAILABLE, then confirm
    (a) rendering degrades to None without raising and (b) the "missing
    matplotlib" warning is logged exactly once across multiple render
    calls, not once per call (no per-call log spam)."""
    monkeypatch.setattr(charts, "MATPLOTLIB_AVAILABLE", False)
    monkeypatch.setattr(charts, "_warned_missing", False)
    habit = _synthetic_habit("water", "numeric", goal=2500.0)
    end_date = date(2026, 8, 19)

    import logging

    with caplog.at_level(logging.WARNING, logger="habit_assistant.core.charts"):
        image1 = charts.render_habit_chart(db, Config(), habit, end_date, "en")
        image2 = charts.render_habit_chart(db, Config(), habit, end_date, "en")
        image3 = charts.render_habit_chart(db, Config(), habit, end_date, "en")

    assert image1 is None
    assert image2 is None
    assert image3 is None
    warning_records = [r for r in caplog.records if "matplotlib is not installed" in r.message]
    assert len(warning_records) == 1  # not per-call spam


def test_charts_module_import_guard_never_raises_when_matplotlib_hidden(monkeypatch):
    """AC1.0.1/Audit item 7's "actually fires gracefully" claim, at the
    import-guard level itself (not just the render-call level covered
    above): reload core/charts.py with `matplotlib` hidden from
    sys.modules and confirm the module-level try/except sets
    MATPLOTLIB_AVAILABLE = False instead of raising ImportError at import
    time."""
    import importlib

    monkeypatch.setitem(sys.modules, "matplotlib", None)
    monkeypatch.setitem(sys.modules, "matplotlib.pyplot", None)
    try:
        reloaded = importlib.reload(charts)
        assert reloaded.MATPLOTLIB_AVAILABLE is False
    finally:
        # Restore the real module for every subsequent test in this process.
        monkeypatch.undo()
        importlib.reload(charts)
        assert charts.MATPLOTLIB_AVAILABLE is True


# ---------------------------------------------------------------------------
# AC1.0.1 -- core/review.py chart wiring (render_weekly_review_charts)
# ---------------------------------------------------------------------------


def test_render_weekly_review_charts_disabled_returns_empty_list(db):
    registry = _default_registry()
    _seed_known_week(db, date(2026, 8, 19))
    config = Config.model_validate({"charts": {"enabled": False}})

    pairs = render_weekly_review_charts(db, config, registry, today=date(2026, 8, 19))

    assert pairs == []


def test_render_weekly_review_charts_enabled_returns_captioned_real_pngs(db):
    registry = _default_registry()
    _seed_known_week(db, date(2026, 8, 19))
    config = Config()  # charts.enabled=True by default

    pairs = render_weekly_review_charts(db, config, registry, today=date(2026, 8, 19))

    assert len(pairs) == 2  # water + stretch; diary (text) excluded
    for image, caption in pairs:
        assert image[:8] == PNG_MAGIC
        assert isinstance(caption, str) and caption  # non-empty caption


def test_render_weekly_review_charts_caption_numbers_agree_with_stats_block(db):
    """A chart's caption must never drift from the text review's own
    stats block -- both are built from the same HabitStats."""
    registry = _default_registry()
    end_date = date(2026, 8, 19)
    _seed_known_week(db, end_date)
    config = Config()

    stats = compute_weekly_stats(db, config, registry, end_date)
    pairs = render_weekly_review_charts(db, config, registry, today=end_date)
    captions_by_habit = dict(zip(["water", "stretch"], [c for _, c in pairs]))

    water = stats.get("water")
    assert str(int(water.total)) in captions_by_habit["water"]
    stretch = stats.get("stretch")
    assert str(stretch.streak) in captions_by_habit["stretch"]


def test_render_weekly_review_charts_matplotlib_absent_falls_back_to_empty_list(db, monkeypatch):
    """Same matplotlib-absent simulation as the charts-module-level test
    above, exercised through the review-layer entry point main.py actually
    calls."""
    from habit_assistant.core import charts as charts_module

    monkeypatch.setattr(charts_module, "MATPLOTLIB_AVAILABLE", False)
    monkeypatch.setattr(charts_module, "_warned_missing", False)
    registry = _default_registry()
    _seed_known_week(db, date(2026, 8, 19))
    config = Config()

    pairs = render_weekly_review_charts(db, config, registry, today=date(2026, 8, 19))

    assert pairs == []


# ---------------------------------------------------------------------------
# AC1.0.1 -- main.py weekly_review_job wiring: text THEN images, fail-open
# ---------------------------------------------------------------------------


async def test_weekly_review_job_sequence_sends_text_then_chart_images(db):
    """Mirrors main.py's weekly_review_job closure exactly (same call
    order, same functions: run_weekly_review then render_weekly_review_charts,
    channel.send then channel.send_image per pair) -- the same technique
    tests/test_review.py's own
    test_weekly_review_job_sends_result_over_channel uses for the v0.10.0
    text-only job."""
    registry = _default_registry()
    end_date = date(2026, 8, 19)
    _seed_known_week(db, end_date)
    config = Config()
    channel = FakeChannel()
    llm = FakeLLM()

    text = await run_weekly_review(db, config, registry, llm, today=end_date)
    await channel.send(text)
    for image, caption in render_weekly_review_charts(db, config, registry, today=end_date):
        await channel.send_image(image, caption)

    assert channel.calls[0] == "send"
    assert channel.calls.count("send_image") == 2  # water + stretch
    assert all(c == "send_image" for c in channel.calls[1:])
    assert channel.sent == [text]
    assert len(channel.images) == 2
    for image, caption in channel.images:
        assert image[:8] == PNG_MAGIC
        assert isinstance(caption, str) and caption


async def test_weekly_review_job_sequence_charts_disabled_is_text_only(db):
    registry = _default_registry()
    end_date = date(2026, 8, 19)
    _seed_known_week(db, end_date)
    config = Config.model_validate({"charts": {"enabled": False}})
    channel = FakeChannel()
    llm = FakeLLM()

    text = await run_weekly_review(db, config, registry, llm, today=end_date)
    await channel.send(text)
    for image, caption in render_weekly_review_charts(db, config, registry, today=end_date):
        await channel.send_image(image, caption)

    assert channel.calls == ["send"]
    assert channel.images == []


# ---------------------------------------------------------------------------
# AC1.0.1 -- the SAME wiring, exercised through the REAL async_main/job.func()
# path (per IMPL.md's own request: "a channel.send_image call-count
# assertion on the main.py wiring"), with a monkeypatched OllamaClient so
# zero real network I/O of any kind happens (per the live-environment rules
# -- not even a failed connection attempt to localhost:11434).
# ---------------------------------------------------------------------------


class _StopAfterSchedulerStart(Exception):
    pass


class _FakeScheduler:
    last_instance: "_FakeScheduler | None" = None

    def __init__(self, *args, **kwargs):
        self.jobs: dict[str, object] = {}
        _FakeScheduler.last_instance = self

    def add_job(self, func, trigger=None, args=None, id=None, replace_existing=True):
        self.jobs[id] = SimpleNamespace(func=func, trigger=trigger, args=args, id=id)

    def start(self):
        pass

    def shutdown(self, wait=False):
        pass

    def get_jobs(self):
        return list(self.jobs.values())

    def get_job(self, job_id):
        return self.jobs.get(job_id)


class _FakeTelegramChannel:
    """Records send/send_image calls; invokes the registered weekly_review
    job from inside run() (the only safe point, per test_streaks.py's own
    documented reasoning -- async_main's try/finally closes db/llm/channel
    as soon as run() returns/raises)."""

    last_instance: "_FakeTelegramChannel | None" = None
    invoke_weekly_review_job_on_run: bool = False

    def __init__(self, *args, **kwargs):
        self.sent: list[str] = []
        self.images: list[tuple[bytes, str]] = []
        self.calls: list[str] = []
        _FakeTelegramChannel.last_instance = self

    async def send(self, text: str) -> None:
        self.sent.append(text)
        self.calls.append("send")

    async def send_image(self, image: bytes, caption: str) -> None:
        self.images.append((image, caption))
        self.calls.append("send_image")

    async def send_actionable(self, text: str, buttons) -> None:
        # SPEC-v1.1.md R-U7: mirrors the Channel ABC's own default
        # (send_image-style degradation) -- drop the buttons, record text.
        self.sent.append(text)
        self.calls.append("send_actionable")

    async def set_my_commands(self, commands) -> None:
        # Deliberately NOT appended to `self.calls` -- that list is this
        # fake's pre-existing "send vs. send_image ordering" contract
        # (see class docstring), which this test's own assertions index
        # into; the one-time startup command-menu registration is a
        # separate concern, tracked here only for tests that care.
        self.set_my_commands_calls = getattr(self, "set_my_commands_calls", 0) + 1

    async def run(self, on_message, on_callback=None):
        if _FakeTelegramChannel.invoke_weekly_review_job_on_run:
            job = _FakeScheduler.last_instance.get_job("weekly_review")
            if job is not None:
                await job.func()
        raise _StopAfterSchedulerStart()

    async def aclose(self) -> None:
        pass


class _FakeOllamaClient:
    """No real network I/O whatsoever -- not even a failed connection
    attempt. Stands in for main.py's real OllamaClient(config.ollama...)
    construction so probe_schema_support/reparse_pending_unparsed/
    run_weekly_review's chat_text call all resolve locally."""

    def __init__(self, *args, **kwargs):
        self.available = True

    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return "A solid week."

    async def chat_json(self, *args, **kwargs):
        return None

    async def probe_schema_support(self, *args, **kwargs) -> dict:
        return {}

    async def aclose(self) -> None:
        pass


def _run_async_main_and_capture_scheduler(monkeypatch, config, invoke_weekly_review_job=False):
    from habit_assistant import main as main_module

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module, "load_secrets", lambda: SimpleNamespace(telegram_bot_token="fake", telegram_chat_id="fake")
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", _FakeTelegramChannel)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClient)
    _FakeScheduler.last_instance = None
    _FakeTelegramChannel.last_instance = None
    _FakeTelegramChannel.invoke_weekly_review_job_on_run = invoke_weekly_review_job
    return main_module


async def test_async_main_weekly_review_job_sends_text_then_images_no_real_network(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    main_module = _run_async_main_and_capture_scheduler(monkeypatch, config, invoke_weekly_review_job=True)
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)

    # Seed through the real db path async_main opens, before invoking.
    db = Database(tmp_path / "habits.db")
    _seed_known_week(db, date.today())
    db.close()

    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    channel = _FakeTelegramChannel.last_instance
    assert channel is not None
    assert channel.calls[0] == "send"
    assert "send_image" in channel.calls
    assert len(channel.images) >= 1
    for image, caption in channel.images:
        assert image[:8] == PNG_MAGIC
        assert isinstance(caption, str) and caption


async def test_async_main_weekly_review_job_chart_render_exception_still_sends_text(tmp_path, monkeypatch):
    """AC1.0.1's belt-and-suspenders catch in main.py itself: a render-layer
    exception raised from render_weekly_review_charts must be caught by
    weekly_review_job's own try/except (not just charts.py's internal one),
    leaving the review text-only rather than crashing the job."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    main_module = _run_async_main_and_capture_scheduler(monkeypatch, config, invoke_weekly_review_job=True)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated total chart-layer failure")

    # main.py does `from habit_assistant.core.review import
    # render_weekly_review_charts` -- that binds the name directly into
    # main's own module globals, so weekly_review_job resolves it via
    # main_module.render_weekly_review_charts, not review_module's copy.
    monkeypatch.setattr(main_module, "render_weekly_review_charts", boom)

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    db = Database(tmp_path / "habits.db")
    _seed_known_week(db, date.today())
    db.close()

    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    channel = _FakeTelegramChannel.last_instance
    assert channel is not None
    assert channel.calls == ["send"]  # text still sent, no crash, no images
    assert channel.images == []


# ---------------------------------------------------------------------------
# Audit -- version consistency (VERSION / pyproject.toml / __init__.py)
# ---------------------------------------------------------------------------


def test_version_is_consistent_across_version_file_pyproject_and_init():
    """The `VERSION` file (PROGRESS.md's playbook: "single source of
    truth") is read dynamically here and used to cross-check pyproject.toml
    and `__init__.py`, rather than pinning a hardcoded literal -- a
    hardcoded expectation went stale at the very first release after this
    test was written (v1.0.0 -> v1.0.1 bumped `VERSION`/pyproject.toml but
    the test still asserted "1.0.0", failing on every release from then on
    regardless of whether the three files actually agreed with each
    other). This version reads whatever `VERSION` currently says and
    requires the other two to match it -- it fails only on a genuine
    cross-file drift, not on every version bump."""
    version_file = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()

    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    import re

    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject_text, re.MULTILINE)
    assert m is not None, "pyproject.toml has no [project] version field"
    pyproject_version = m.group(1)

    import habit_assistant

    init_version = habit_assistant.__version__

    assert pyproject_version == version_file, (
        f"pyproject.toml version is {pyproject_version!r}, VERSION file says {version_file!r}"
    )
    assert init_version == version_file, (
        f"habit_assistant.__version__ is {init_version!r}, VERSION file says {version_file!r} -- "
        f"src/habit_assistant/__init__.py was not bumped this release"
    )


def test_readme_charts_install_line_is_a_real_installable_extra():
    """README.md claims `uv pip install -e ".[charts]"` / equivalently
    `pip install -e ".[charts]"` -- confirm pyproject.toml actually
    declares that optional-dependency group (not just prose drift)."""
    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    readme_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "[charts]" in readme_text
    assert "matplotlib" in pyproject_text
    assert "charts = [" in pyproject_text or 'charts=["matplotlib' in pyproject_text.replace(" ", "")
