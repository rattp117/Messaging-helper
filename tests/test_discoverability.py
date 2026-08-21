"""SPEC-v1.1.md §4 Feature 3 "discoverability" (`core/discoverability.py`,
`core/commands.py`'s `"help"`/`"habits"` kinds, `main.py`'s routing +
`set_my_commands` extension) -- module tests for the ACs this sequential
follow-on owns (SPEC-v1.1.md §11): AC35, AC36, AC37, AC38, AC39, AC40.

This module lands after the v1.1 shared surface + integration are both
green (SPEC-v1.1.md §11's own note: it edits `core/commands.py`, which the
`targets` module already touched, so it is NOT parallel-safe with that
work and must land sequentially, after). Conventions: real on-disk SQLite
(`tmp_path`) everywhere; no mocks for the DB. Ollama is represented by a
`FakeLLM` that raises `AssertionError` if either of its methods is ever
called -- both `/help` and `/habits` must be fully LLM-free (AC35/AC37),
so this is a hard proof, not just an absence of a real network call.
"""

from __future__ import annotations

from datetime import datetime
from typing import Awaitable, Callable
from types import SimpleNamespace

import pytest

from habit_assistant.channels.base import Button, Channel
from habit_assistant.config import Config
from habit_assistant.core import commands, discoverability, i18n
from habit_assistant.core.commands import Command
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.main import handle_inbound_message
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


def _seed(db: Database, ts: str, category: str, value_num: float | None) -> int:
    return db.insert_log(LogEntry(None, ts, category, value_num, None, "x", "reply"))


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.actionable: list[tuple[str, list[Button]]] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def send_actionable(self, text: str, buttons: list[Button]) -> None:
        self.actionable.append((text, buttons))
        self.sent.append(text)

    async def run(self, on_message: Callable[[str], Awaitable[None]], on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


class _NeverCalledLLM:
    """AC35/AC37: `/help`/`/habits` must be fully LLM-free -- any call to
    either method here fails the test loudly, rather than the test just
    happening not to exercise the LLM path."""

    async def chat_text(self, *args, **kwargs):
        raise AssertionError("build_help_text must never call the LLM")

    async def chat_json(self, *args, **kwargs):
        raise AssertionError("build_habits_overview must never call the LLM")


class _OllamaDownHealthMonitor:
    """Minimal `health_monitor` stand-in exposing only `.ollama_up`, the
    one attribute `handle_inbound_message` reads (mirrors
    tests/test_resilience.py's own `_FrozenHealthMonitor`)."""

    ollama_up = False


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


@pytest.fixture
def fixed_clock():
    return lambda: datetime(2026, 8, 19, 9, 0, 0)


# ---------------------------------------------------------------------------
# AC35: `/help` (also `ช่วยเหลือ` / `วิธีใช้`) -> command.kind == "help",
# LLM-free, reply language via resolve_reply_language, succeeds with Ollama
# down.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trigger", ["/help", "ช่วยเหลือ", "วิธีใช้"])
def test_ac35_dispatch_recognizes_help_triggers(trigger):
    assert commands.dispatch(trigger, DEFAULT_REGISTRY) == Command(kind="help")


async def test_ac35_help_reply_matches_build_help_text_and_works_with_ollama_down(db):
    config = Config()
    channel = FakeChannel()

    await handle_inbound_message(
        "/help",
        db=db,
        llm=_NeverCalledLLM(),
        channel=channel,
        config=config,
        registry=DEFAULT_REGISTRY,
        health_monitor=_OllamaDownHealthMonitor(),
    )

    assert channel.sent == [discoverability.build_help_text(config, "en")]
    assert channel.actionable == []  # R-U2 scope: not a log confirmation, no button


async def test_ac35_help_reply_language_follows_resolve_reply_language(db):
    # Thai trigger -> Thai reply (resolve_reply_language auto-detects Thai).
    # A DB is still required by handle_inbound_message's signature even
    # though build_help_text itself never touches it.
    config = Config()
    channel_th = FakeChannel()

    await handle_inbound_message(
        "ช่วยเหลือ",
        db=db,
        llm=_NeverCalledLLM(),
        channel=channel_th,
        config=config,
        registry=DEFAULT_REGISTRY,
        health_monitor=_OllamaDownHealthMonitor(),
    )

    assert channel_th.sent == [discoverability.build_help_text(config, "th")]


# ---------------------------------------------------------------------------
# AC36: the help reply covers every required capability section, and its
# time/number values are read live from `config`.
# ---------------------------------------------------------------------------


def test_ac36_help_text_covers_every_required_section():
    config = Config.model_validate(
        {
            "gamification": {"milestones": [3, 7, 30], "daily_summary": True, "daily_summary_time": "21:45"},
            "weekly_review": {"day_of_week": "sun", "time": "20:00"},
            "snooze": {"default_minutes": 30},
            "quiet_hours": {"windows": [["23:00", "07:00"]]},
        }
    )
    text = discoverability.build_help_text(config, "en")

    # Log with EN/TH examples -- both scripts present regardless of reply language.
    assert "500ml" in text
    assert "มล." in text

    # Undo: button + /undo.
    assert "Undo" in text
    assert "/undo" in text

    # Targets: /target + NL phrasing.
    assert "/target" in text
    assert "from now on" in text.lower()

    # NL queries.
    assert "how much water" in text.lower()

    # Streaks/milestones.
    assert "3, 7, 30" in text

    # Daily-summary + weekly-review times.
    assert "21:45" in text
    assert "20:00" in text
    assert "sun" in text

    # Snooze + quiet hours.
    assert "30" in text
    assert "23:00" in text and "07:00" in text


def test_ac36_help_text_values_are_read_live_from_config_not_hardcoded():
    config_a = Config.model_validate({"weekly_review": {"time": "20:00"}})
    config_b = Config.model_validate({"weekly_review": {"time": "18:15"}})

    text_a = discoverability.build_help_text(config_a, "en")
    text_b = discoverability.build_help_text(config_b, "en")

    assert "20:00" in text_a and "18:15" not in text_a
    assert "18:15" in text_b and "20:00" not in text_b


def test_ac36_help_text_daily_summary_off_omits_a_time_but_still_has_a_section():
    config = Config.model_validate({"gamification": {"daily_summary": False}})
    text = discoverability.build_help_text(config, "en")
    assert "daily summary" in text.lower()
    assert "21:45" not in text  # the default daily_summary_time is not shown when off


def test_ac36_help_text_quiet_hours_empty_still_has_a_section():
    config = Config()  # default: no quiet-hours windows configured
    text = discoverability.build_help_text(config, "en")
    assert "quiet hours" in text.lower()


def test_ac36_help_text_snooze_minutes_change_with_config():
    config = Config.model_validate({"snooze": {"default_minutes": 45}})
    text = discoverability.build_help_text(config, "en")
    assert "45" in text


# ---------------------------------------------------------------------------
# AC37: `/habits` (also `นิสัย`) -> command.kind == "habits", LLM-free,
# lists every registered habit in registry order; succeeds with Ollama
# down.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trigger", ["/habits", "นิสัย"])
def test_ac37_dispatch_recognizes_habits_triggers(trigger):
    assert commands.dispatch(trigger, DEFAULT_REGISTRY) == Command(kind="habits")


async def test_ac37_habits_reply_matches_build_habits_overview_and_works_with_ollama_down(db, fixed_clock):
    config = Config()
    channel = FakeChannel()

    await handle_inbound_message(
        "/habits",
        db=db,
        llm=_NeverCalledLLM(),
        channel=channel,
        config=config,
        registry=DEFAULT_REGISTRY,
        clock=fixed_clock,
        health_monitor=_OllamaDownHealthMonitor(),
    )

    expected = discoverability.build_habits_overview(db, config, DEFAULT_REGISTRY, fixed_clock, "en")
    assert channel.sent == [expected]
    assert channel.actionable == []  # not a log confirmation, no button


def test_ac37_every_registered_habit_appears_in_registry_order(db, fixed_clock):
    config = Config.model_validate(
        {
            "habits": [
                {
                    "id": "water",
                    "type": "numeric",
                    "goal": 2500,
                    "label": {"en": "water", "th": "น้ำ"},
                    "unit": {"en": "ml", "th": "มล."},
                },
                {
                    "id": "yoga",
                    "type": "duration",
                    "label": {"en": "yoga", "th": "โยคะ"},
                    "unit": {"en": "min", "th": "นาที"},
                },
                {
                    "id": "meds",
                    "type": "boolean",
                    "label": {"en": "meds", "th": "ยา"},
                },
                {
                    "id": "journal",
                    "type": "text",
                    "label": {"en": "journal", "th": "บันทึก"},
                },
            ]
        }
    )
    registry = HabitRegistry.from_config(config)

    overview = discoverability.build_habits_overview(db, config, registry, fixed_clock, "en")
    lines = [line for line in overview.split("\n") if line.startswith("•")]

    assert len(lines) == 4
    assert [line.split(" (")[0].lstrip("• ") for line in lines] == ["water", "yoga", "meds", "journal"]


# ---------------------------------------------------------------------------
# AC38: water has a DB target override, stretch doesn't -- the goal shown
# comes from targets.effective_goal, the mark from db.get_target.
# ---------------------------------------------------------------------------


def test_ac38_override_marked_as_your_target_vs_default_vs_no_goal(db, fixed_clock):
    config = Config()  # water config default 2500, stretch has none
    db.set_target("water", 2000.0)

    overview = discoverability.build_habits_overview(db, config, DEFAULT_REGISTRY, fixed_clock, "en")

    water_line = next(line for line in overview.split("\n") if line.startswith("• water"))
    stretch_line = next(line for line in overview.split("\n") if line.startswith("• stretch"))
    diary_line = next(line for line in overview.split("\n") if line.startswith("• diary"))

    assert "2000" in water_line
    assert "your target" in water_line
    assert "default" not in water_line  # not marked default once overridden

    assert "no goal" in stretch_line  # stretch has neither an override nor a config default
    assert "no goal" in diary_line  # text habits are never goal-able


def test_ac38_config_default_marked_default_not_your_target(db, fixed_clock):
    config = Config()  # water's config default (2500) applies, no override
    overview = discoverability.build_habits_overview(db, config, DEFAULT_REGISTRY, fixed_clock, "en")
    water_line = next(line for line in overview.split("\n") if line.startswith("• water"))
    assert "2500" in water_line
    assert "default" in water_line
    assert "your target" not in water_line


def test_ac38_clearing_the_override_reverts_the_mark_to_default(db, fixed_clock):
    config = Config()
    db.set_target("water", 2000.0)
    db.clear_target("water")
    overview = discoverability.build_habits_overview(db, config, DEFAULT_REGISTRY, fixed_clock, "en")
    water_line = next(line for line in overview.split("\n") if line.startswith("• water"))
    assert "2500" in water_line
    assert "default" in water_line


def test_ac38_target_on_a_previously_goalless_habit_is_marked_your_target(db, fixed_clock):
    """R-T5b (from the earlier features) generalized to /habits: a target
    set on a habit with no config default at all (stretch) still shows up
    correctly marked, not as "no goal"."""
    config = Config()
    db.set_target("stretch", 20.0)
    overview = discoverability.build_habits_overview(db, config, DEFAULT_REGISTRY, fixed_clock, "en")
    stretch_line = next(line for line in overview.split("\n") if line.startswith("• stretch"))
    assert "20" in stretch_line
    assert "your target" in stretch_line


# ---------------------------------------------------------------------------
# AC39: today's total via db.sum_value, under config.app.timezone rules
# (as elsewhere: driven by the injected `clock`).
# ---------------------------------------------------------------------------


def test_ac39_todays_water_total_shown_correctly(db, fixed_clock):
    config = Config()
    _seed(db, "2026-08-19T08:00:00", "water", 500.0)

    overview = discoverability.build_habits_overview(db, config, DEFAULT_REGISTRY, fixed_clock, "en")
    water_line = next(line for line in overview.split("\n") if line.startswith("• water"))
    assert "today 500 ml" in water_line


def test_ac39_only_todays_logs_count_not_other_days(db, fixed_clock):
    config = Config()
    _seed(db, "2026-08-18T08:00:00", "water", 9999.0)  # yesterday -- must not count
    _seed(db, "2026-08-19T08:00:00", "water", 300.0)  # today

    overview = discoverability.build_habits_overview(db, config, DEFAULT_REGISTRY, fixed_clock, "en")
    water_line = next(line for line in overview.split("\n") if line.startswith("• water"))
    assert "today 300 ml" in water_line
    assert "9999" not in water_line


def test_ac39_boolean_and_text_totals_use_count_not_sum(db, fixed_clock):
    config = Config.model_validate(
        {
            "habits": [
                {"id": "meds", "type": "boolean", "label": {"en": "meds", "th": "ยา"}},
                {"id": "journal", "type": "text", "label": {"en": "journal", "th": "บันทึก"}},
            ]
        }
    )
    registry = HabitRegistry.from_config(config)
    _seed(db, "2026-08-19T08:00:00", "meds", 1.0)
    _seed(db, "2026-08-19T20:00:00", "meds", 0.0)  # falsy -- count_true must not count this
    _seed(db, "2026-08-19T09:00:00", "journal", None)
    _seed(db, "2026-08-19T21:00:00", "journal", None)

    overview = discoverability.build_habits_overview(db, config, registry, fixed_clock, "en")
    meds_line = next(line for line in overview.split("\n") if line.startswith("• meds"))
    journal_line = next(line for line in overview.split("\n") if line.startswith("• journal"))

    assert "today 1" in meds_line  # count_true: only the truthy entry
    assert "today 2" in journal_line  # count: both entries


# ---------------------------------------------------------------------------
# AC40: `/help`/`/habits` appear in the startup command menu alongside
# `/undo`/`/target`, both languages; the adversarial corpus never
# dispatches as "help"/"habits".
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

    def get_job(self, job_id):
        return self.jobs.get(job_id)


class _FakeOllamaClient:
    def __init__(self, *args, **kwargs):
        pass

    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return "noted"

    async def chat_json(self, *args, **kwargs):
        return None

    async def probe_schema_support(self, *args, **kwargs) -> dict:
        return {}

    async def aclose(self) -> None:
        pass


class _AsyncMainFakeChannel(Channel):
    last_instance: "_AsyncMainFakeChannel | None" = None

    def __init__(self, *args, **kwargs) -> None:
        self.sent: list[str] = []
        self.set_my_commands_calls: list[dict] = []
        _AsyncMainFakeChannel.last_instance = self

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def send_actionable(self, text: str, buttons: list[Button]) -> None:
        self.sent.append(text)

    async def set_my_commands(self, commands) -> None:
        self.set_my_commands_calls.append(commands)

    async def run(self, on_message, on_callback=None) -> None:
        raise _StopAfterSchedulerStart()

    async def aclose(self) -> None:
        pass


def _run_async_main(monkeypatch, config):
    from habit_assistant import main as main_module

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module, "load_secrets", lambda: SimpleNamespace(telegram_bot_token="fake", telegram_chat_id="fake")
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", _AsyncMainFakeChannel)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClient)
    _FakeScheduler.last_instance = None
    _AsyncMainFakeChannel.last_instance = None
    return main_module


async def test_ac40_startup_registers_help_and_habits_alongside_undo_and_target(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    main_module = _run_async_main(monkeypatch, config)
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)

    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    channel = _AsyncMainFakeChannel.last_instance
    assert channel is not None
    assert len(channel.set_my_commands_calls) == 1
    registered = channel.set_my_commands_calls[0]
    assert set(registered.keys()) == {"en", "th"}
    for lang, entries in registered.items():
        names = [name for name, _desc in entries]
        assert {"undo", "target", "help", "habits"} <= set(names), f"{lang} set missing an expected command: {names}"

    # Actually localized, not copy-pasted between languages.
    en_help_desc = dict(registered["en"])["help"]
    th_help_desc = dict(registered["th"])["help"]
    assert en_help_desc != th_help_desc
    en_habits_desc = dict(registered["en"])["habits"]
    th_habits_desc = dict(registered["th"])["habits"]
    assert en_habits_desc != th_habits_desc


ADVERSARIAL_MESSAGES = [
    "ดื่มน้ำ 2 แก้ว",
    "500ml",
    "did 10 min stretch",
    "today I had to undo a mistake at work",
    "เลิกงานแล้ว เหนื่อยมาก",
    "made 3 bottles of juice",
    "I need to delete some old photos later",
    "ยกเลิกการนัดหมายพรุ่งนี้",
    "change it to feeling better today",
    "I finally decided to cancel my gym membership",
    # discoverability-specific: "help"/"habits"/their Thai equivalents
    # appearing mid-sentence must never fire (R-D1's whole-message anchor).
    "I need some help with my stretching form today",
    "my habits have really improved this month",
    "ขอความช่วยเหลือหน่อยได้ไหม",  # "help" phrased naturally, not the bare trigger
    "นิสัยการดื่มน้ำของฉันดีขึ้นมาก",  # "นิสัย" (habit) mid-sentence, not the bare trigger
    "let me tell you about my morning habits routine",
    "วิธีใช้ยานี้คือกินหลังอาหาร",  # "วิธีใช้" used literally (medication instructions), but with trailing words
]


@pytest.mark.parametrize("message", ADVERSARIAL_MESSAGES)
def test_ac40_adversarial_corpus_never_dispatches_as_help_or_habits(message):
    command = commands.dispatch(message, DEFAULT_REGISTRY)
    assert command is None or command.kind not in ("help", "habits")


# ===========================================================================
# Vera adversarial additions (beyond Luna's own 38 tests) -- per the
# coordinator's dispatch brief for this final gate pass.
# ===========================================================================

# ---------------------------------------------------------------------------
# Routing precedence: /help and /habits (and their Thai aliases) vs the
# target triggers, the query matcher, and plain logs that merely mention
# "help"/"habits"/"ช่วยเหลือ"/"นิสัย" mid-sentence.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "I need help drinking more water",  # coordinator's literal example
        "logging my habits: 500ml water today",  # a real log that happens to contain the word "habits"
    ],
)
def test_routing_ordinary_sentences_containing_help_or_habits_words_never_dispatch(message):
    command = commands.dispatch(message, DEFAULT_REGISTRY)
    assert command is None


def test_routing_help_and_habits_are_case_insensitive_and_whitespace_tolerant():
    assert commands.dispatch("/HELP", DEFAULT_REGISTRY) == Command(kind="help")
    assert commands.dispatch("  /help  ", DEFAULT_REGISTRY) == Command(kind="help")
    assert commands.dispatch("/Habits", DEFAULT_REGISTRY) == Command(kind="habits")


def test_routing_target_slash_form_naming_habit_help_is_not_intercepted_by_help_matcher():
    """`/target help` is a TARGET command (show the goal for an
    unrecognized habit literally named "help") -- R-D1's `_HELP_RE`
    requires the WHOLE stripped message to be exactly "/help" (etc.), so
    it must never fire inside a longer `/target ...` command. `target`
    is checked before `help`/`habits` in `dispatch`'s own precedence
    order (R-T7 before R-D1), so this is also a precedence regression
    guard."""
    command = commands.dispatch("/target help", DEFAULT_REGISTRY)
    assert command.kind == "target"
    assert command.target_action == "show"
    assert command.category == "help"  # execute_target will report target_invalid_habit for it


def test_routing_help_with_trailing_question_mark_falls_to_query_not_help():
    """Documented, spec-compliant edge case (not a defect): `_HELP_RE` is
    anchored to EXACTLY "/help" (R-D1's literal grammar) -- "/help?" does
    not match it, and the trailing "?" instead satisfies the pre-existing
    v0.8 query anchor (`_TRAILING_QUESTION_MARK_RE`). This is benign: the
    message still gets a helpful, safe response (routed to the query
    answerer, never misfiled as a log) -- but it is NOT the `/help`
    formatter. Recorded here so a future change to either matcher's
    anchoring doesn't silently "fix" this by accident without a spec
    citation."""
    command = commands.dispatch("/help?", DEFAULT_REGISTRY)
    assert command == Command(kind="query")


# ---------------------------------------------------------------------------
# Ollama-down / LLM-raises through the REAL wiring (handle_inbound_message),
# mirroring tests/test_v11_integration.py's own pattern -- confirms both
# commands truly sit before the deferral check and never touch the LLM
# client at all, independent of whatever health_monitor reports.
# ---------------------------------------------------------------------------


class _OllamaUpHealthMonitor:
    ollama_up = True


class _AlwaysRaisingLLM:
    """Stronger proof than `_NeverCalledLLM` alone: even if something were
    to call this LLM, it would blow up loudly and immediately -- used here
    with `ollama_up=True` (the opposite of the existing DOWN-only coverage)
    to prove /help and /habits are unconditionally LLM-free, not merely
    "skipped because Ollama looked down"."""

    async def chat_text(self, *args, **kwargs):
        raise RuntimeError("LLM must never be reached by /help or /habits")

    async def chat_json(self, *args, **kwargs):
        raise RuntimeError("LLM must never be reached by /help or /habits")


@pytest.mark.parametrize("text", ["/help", "/habits"])
async def test_help_and_habits_never_touch_llm_even_when_ollama_reports_up(db, fixed_clock, text):
    config = Config()
    channel = FakeChannel()

    await handle_inbound_message(
        text,
        db=db,
        llm=_AlwaysRaisingLLM(),
        channel=channel,
        config=config,
        registry=DEFAULT_REGISTRY,
        clock=fixed_clock,
        health_monitor=_OllamaUpHealthMonitor(),
    )

    assert len(channel.sent) == 1  # got a real reply, not swallowed or crashed


@pytest.mark.parametrize("text", ["/help", "/habits"])
async def test_help_and_habits_work_with_no_health_monitor_wired_at_all(db, fixed_clock, text):
    """`health_monitor=None` (e.g. `--dry-run`/CLI paths that don't wire
    one) must not be required for /help or /habits to work -- they sit
    entirely above the `health_monitor is not None and not ...ollama_up`
    check in main.py."""
    config = Config()
    channel = FakeChannel()

    await handle_inbound_message(
        text,
        db=db,
        llm=_NeverCalledLLM(),
        channel=channel,
        config=config,
        registry=DEFAULT_REGISTRY,
        clock=fixed_clock,
        health_monitor=None,
    )

    assert len(channel.sent) == 1


async def test_help_reply_does_not_write_a_deferred_ack_log_row_while_ollama_down(db, fixed_clock):
    """Belt-and-suspenders on "routing truly sits before the deferral
    check": if /help were (incorrectly) falling through to the deferral
    branch instead of being handled by its own branch, it would insert an
    `unparsed` log row and send `deferred_ack` instead of the real help
    text. Assert neither happens."""
    config = Config()
    channel = FakeChannel()

    await handle_inbound_message(
        "/help",
        db=db,
        llm=_NeverCalledLLM(),
        channel=channel,
        config=config,
        registry=DEFAULT_REGISTRY,
        clock=fixed_clock,
        health_monitor=_OllamaDownHealthMonitor(),
    )

    assert db._conn.execute("SELECT COUNT(*) AS n FROM logs").fetchone()["n"] == 0
    assert channel.sent != [i18n.t("deferred_ack", "en")]


# ---------------------------------------------------------------------------
# /habits correctness: DB-override round trip through the real /target
# command (not raw db.set_target/clear_target calls), goal-less habit, the
# Asia/Bangkok-style day boundary (a log just before midnight vs the query
# clock just after), and all four aggregation kinds in one overview.
# ---------------------------------------------------------------------------


async def test_habits_override_round_trip_through_real_target_command_updates_the_overview(db, fixed_clock):
    """Exercises the override via the actual `/target` command path (not a
    raw `db.set_target` call) end to end, then clears it the same way, and
    confirms /habits reflects each state correctly -- this is the
    "after /target <habit> default -> back to default" case the
    coordinator called out specifically."""
    config = Config()
    channel = FakeChannel()

    await handle_inbound_message(
        "/target water 1800", db=db, llm=_NeverCalledLLM(), channel=channel, config=config,
        registry=DEFAULT_REGISTRY, clock=fixed_clock,
    )
    overview_after_set = await _habits_text(db, config, channel, fixed_clock)
    water_line = _line_for(overview_after_set, "water")
    assert "1800" in water_line
    assert "your target" in water_line

    await handle_inbound_message(
        "/target water default", db=db, llm=_NeverCalledLLM(), channel=channel, config=config,
        registry=DEFAULT_REGISTRY, clock=fixed_clock,
    )
    overview_after_clear = await _habits_text(db, config, channel, fixed_clock)
    water_line_2 = _line_for(overview_after_clear, "water")
    assert "2500" in water_line_2
    assert "default" in water_line_2
    assert "your target" not in water_line_2


async def _habits_text(db, config, channel, clock) -> str:
    channel.sent.clear()
    await handle_inbound_message(
        "/habits", db=db, llm=_NeverCalledLLM(), channel=channel, config=config,
        registry=DEFAULT_REGISTRY, clock=clock,
    )
    return channel.sent[-1]


def _line_for(overview: str, prefix: str) -> str:
    return next(line for line in overview.split("\n") if line.startswith(f"• {prefix}"))


def test_habits_goalless_habit_with_no_override_shows_no_goal_standalone(db, fixed_clock):
    """A dedicated, standalone check (not mixed with the override case) for
    R-D3's "no goal" branch on a goal-able habit that has neither a config
    default nor a DB override."""
    config = Config()
    overview = discoverability.build_habits_overview(db, config, DEFAULT_REGISTRY, fixed_clock, "en")
    stretch_line = _line_for(overview, "stretch")
    assert i18n.t("habits_overview_goal_none", "en") in stretch_line
    assert "your target" not in stretch_line
    assert "default" not in stretch_line


def test_habits_day_boundary_a_log_just_before_midnight_excluded_from_the_next_days_query():
    """Coordinator's own phrasing: "today's totals respect the day
    boundary (entry logged before midnight vs after)". `build_habits_
    overview` derives "today" purely from `clock().date().isoformat()`
    matched as an `ts LIKE '<day>%'` prefix against whatever local-time
    string the log was stored with (`db.sum_value`'s existing mechanism,
    shared by every other day-boundary consumer in this codebase) -- this
    test proves that boundary is exact to the second, not off-by-one in
    either direction."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "boundary.db")
        try:
            config = Config()
            _seed(db, "2026-08-19T23:59:59", "water", 500.0)  # last second of Aug 19

            # Query clock is one second later, now Aug 20 -- the Aug-19
            # entry must NOT count toward "today" (Aug 20).
            clock_next_day = lambda: datetime(2026, 8, 20, 0, 0, 1)
            overview_next_day = discoverability.build_habits_overview(
                db, config, DEFAULT_REGISTRY, clock_next_day, "en"
            )
            assert "today 0" in _line_for(overview_next_day, "water")

            # Query clock is still Aug 19 (same second the log landed) --
            # it MUST count.
            clock_same_day = lambda: datetime(2026, 8, 19, 23, 59, 59)
            overview_same_day = discoverability.build_habits_overview(
                db, config, DEFAULT_REGISTRY, clock_same_day, "en"
            )
            assert "today 500 ml" in _line_for(overview_same_day, "water")

            # And a log at the FIRST second of a day must count for that
            # same day (the symmetric edge).
            _seed(db, "2026-08-20T00:00:01", "water", 250.0)
            overview_new_day = discoverability.build_habits_overview(
                db, config, DEFAULT_REGISTRY, clock_next_day, "en"
            )
            assert "today 250 ml" in _line_for(overview_new_day, "water")
        finally:
            db.close()


def test_habits_all_four_aggregation_kinds_in_one_overview(fixed_clock):
    """Each habit kind's aggregation function, exercised together in a
    single `/habits` call: numeric+duration use `sum_value`, boolean uses
    `count_true` (a falsy 0.0 entry must NOT count), text uses `count`
    (every row counts regardless of value)."""
    import tempfile
    from pathlib import Path

    config = Config.model_validate(
        {
            "habits": [
                {"id": "water", "type": "numeric", "goal": 2500, "label": {"en": "water", "th": "น้ำ"}, "unit": {"en": "ml", "th": "มล."}},
                {"id": "stretch", "type": "duration", "label": {"en": "stretch", "th": "ยืดเส้น"}, "unit": {"en": "min", "th": "นาที"}},
                {"id": "meds", "type": "boolean", "label": {"en": "meds", "th": "ยา"}},
                {"id": "journal", "type": "text", "label": {"en": "journal", "th": "บันทึก"}},
            ]
        }
    )
    registry = HabitRegistry.from_config(config)

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "kinds.db")
        try:
            _seed(db, "2026-08-19T08:00:00", "water", 300.0)
            _seed(db, "2026-08-19T12:00:00", "water", 200.0)  # numeric sums: 500
            _seed(db, "2026-08-19T09:00:00", "stretch", 5.0)
            _seed(db, "2026-08-19T18:00:00", "stretch", 10.0)  # duration sums: 15
            _seed(db, "2026-08-19T07:00:00", "meds", 1.0)
            _seed(db, "2026-08-19T19:00:00", "meds", 0.0)  # boolean count_true: 1 (falsy excluded)
            _seed(db, "2026-08-19T10:00:00", "journal", None)
            _seed(db, "2026-08-19T20:00:00", "journal", None)  # text count: 2

            overview = discoverability.build_habits_overview(db, config, registry, fixed_clock, "en")
            assert "today 500 ml" in _line_for(overview, "water")
            assert "today 15 min" in _line_for(overview, "stretch")
            assert "today 1" in _line_for(overview, "meds")
            assert "today 2" in _line_for(overview, "journal")
        finally:
            db.close()


# ---------------------------------------------------------------------------
# /help values track config: milestones and a second time/value change, to
# rule out any hardcoding beyond the single weekly-review-time check Luna
# already wrote.
# ---------------------------------------------------------------------------


def test_help_milestones_change_with_config_not_hardcoded():
    config_a = Config.model_validate({"gamification": {"milestones": [3, 7, 30]}})
    config_b = Config.model_validate({"gamification": {"milestones": [5, 15]}})

    text_a = discoverability.build_help_text(config_a, "en")
    text_b = discoverability.build_help_text(config_b, "en")

    assert "3, 7, 30" in text_a and "5, 15" not in text_a
    assert "5, 15" in text_b and "3, 7, 30" not in text_b


def test_help_multiple_quiet_hour_windows_all_shown():
    config = Config.model_validate({"quiet_hours": {"windows": [["23:00", "07:00"], ["12:30", "13:00"]]}})
    text = discoverability.build_help_text(config, "en")
    assert "23:00-07:00" in text
    assert "12:30-13:00" in text


# ---------------------------------------------------------------------------
# i18n: no missing-key KeyErrors in either language, no mojibake, and an
# exact (not just subset) command-menu check.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "th"])
def test_help_text_renders_cleanly_in_both_languages_no_keyerror(lang):
    config = Config.model_validate(
        {
            "gamification": {"milestones": [3, 7, 30], "daily_summary": True, "daily_summary_time": "21:45"},
            "weekly_review": {"day_of_week": "sun", "time": "20:00"},
            "snooze": {"default_minutes": 30},
            "quiet_hours": {"windows": [["23:00", "07:00"]]},
        }
    )
    text = discoverability.build_help_text(config, lang)  # raises KeyError on any missing/mismatched template key
    assert text  # non-empty
    assert "�" not in text  # no mojibake replacement character


@pytest.mark.parametrize("lang", ["en", "th"])
def test_habits_overview_renders_cleanly_in_both_languages_no_keyerror(db, fixed_clock, lang):
    db.set_target("water", 2000.0)
    _seed(db, "2026-08-19T08:00:00", "water", 500.0)
    text = discoverability.build_habits_overview(db, Config(), DEFAULT_REGISTRY, fixed_clock, lang)
    assert text
    assert "�" not in text


def test_en_and_th_help_and_habits_text_are_genuinely_different_not_copy_pasted():
    config = Config()
    en_help = discoverability.build_help_text(config, "en")
    th_help = discoverability.build_help_text(config, "th")
    assert en_help != th_help

    db_fixture_config = Config()
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "lang.db")
        try:
            clock = lambda: datetime(2026, 8, 19, 9, 0, 0)
            en_overview = discoverability.build_habits_overview(db, db_fixture_config, DEFAULT_REGISTRY, clock, "en")
            th_overview = discoverability.build_habits_overview(db, db_fixture_config, DEFAULT_REGISTRY, clock, "th")
            assert en_overview != th_overview
        finally:
            db.close()


async def test_command_menu_registers_exactly_the_four_expected_commands_no_extras(tmp_path, monkeypatch):
    """Luna's own AC40 test uses a subset check (`<=`); this tightens it to
    an exact-set check per language, guarding against an accidental
    duplicate or stray entry creeping into either language's merge."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    main_module = _run_async_main(monkeypatch, config)
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)

    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    channel = _AsyncMainFakeChannel.last_instance
    registered = channel.set_my_commands_calls[0]
    for lang, entries in registered.items():
        names = [name for name, _desc in entries]
        assert set(names) == {"undo", "target", "help", "habits"}, f"{lang}: {names}"
        assert len(names) == len(set(names)), f"{lang}: duplicate command entries {names}"


# ---------------------------------------------------------------------------
# Bonus regression: the FULL combined adversarial corpus (undo/edit/query/
# snooze/target's own corpora from tests/test_commands.py + tests/
# test_targets.py, plus this module's own) through dispatch -- no genuine
# log message may dispatch as an ACTION-taking command kind (undo, edit,
# snooze, target, help, habits). "query" is the one benign exception: it is
# pre-existing v0.8 behavior (an interrogative-marker match, e.g. Thai
# "ไหม"), and answering a data question is always safe -- it never mislogs
# or silently swallows anything. This corpus specifically re-confirms that
# adding "help"/"habits" did not perturb ANY earlier module's own
# adversarial guarantees.
# ---------------------------------------------------------------------------

_ACTION_KINDS = {"undo", "edit", "snooze", "target", "help", "habits"}

_FULL_ADVERSARIAL_CORPUS = [
    # tests/test_commands.py's own AC5.5 corpus
    "ดื่มน้ำ 2 แก้ว",
    "500ml",
    "did 10 min stretch",
    "today I had to undo a mistake at work",
    "เลิกงานแล้ว เหนื่อยมาก",
    "made 3 bottles of juice",
    "I need to delete some old photos later",
    "ยกเลิกการนัดหมายพรุ่งนี้",
    "change it to feeling better today",
    "I finally decided to cancel my gym membership",
    # tests/test_discoverability.py's own additions (this file, above)
    "I need some help with my stretching form today",
    "my habits have really improved this month",
    "ขอความช่วยเหลือหน่อยได้ไหม",
    "นิสัยการดื่มน้ำของฉันดีขึ้นมาก",
    "let me tell you about my morning habits routine",
    "วิธีใช้ยานี้คือกินหลังอาหาร",
    "I need help drinking more water",
    "logging my habits: 500ml water today",
]


@pytest.mark.parametrize("message", _FULL_ADVERSARIAL_CORPUS)
def test_bonus_full_adversarial_corpus_never_dispatches_as_an_action_command(message):
    command = commands.dispatch(message, DEFAULT_REGISTRY)
    assert command is None or command.kind not in _ACTION_KINDS
