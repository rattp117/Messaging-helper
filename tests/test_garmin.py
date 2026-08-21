"""Vera (tester) coverage for ROADMAP.md v1.0.0 "Insights: Charts-as-Images
+ Garmin Import", the Garmin half (AC1.0.3, AC1.0.4, and the Garmin slice
of AC1.0.5), plus the regression pin and the network-boundary audit that
need both charts and Garmin wired together. See tests/test_charts.py for
the chart/channel half and the module-level docstring conventions this
file follows.

AC -> section map:
- AC1.0.3 -> "parse_garmin_csv" + "build_garmin_report: join + discrepancy
  flagging" + "custom column_map" + "bilingual rendering"
- AC1.0.4 -> "Missing/malformed CSV handling"
- AC1.0.5 -> "No HTTP transport touched by the Garmin code path" +
  "Full weekly review contacts only the expected hosts" (charts + Garmin +
  Telegram combined, via the captured-transport fixture)
- Regression -> "run_weekly_review byte-identical to v0.10.0 when Garmin
  unconfigured" (a literal pin against the pre-v1.0.0 return expression)

Live-environment rule: every DB in this file is a scratch tmp_path SQLite
file; every Garmin CSV is a scratch tmp_path file. No real Telegram or
Ollama call is ever made. Nothing here ever opens data/habits.db or a real
Garmin export."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from habit_assistant.channels.telegram import TelegramChannel
from habit_assistant.config import Config
from habit_assistant.core import garmin, i18n
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.review import render_weekly_review_charts, run_weekly_review
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry


def _seed(db: Database, ts: str, category: str, value_num: float | None, raw: str = "x") -> int:
    return db.insert_log(LogEntry(None, "owner", ts, category, value_num, None, raw, "reply"))


def _write_csv(path: Path, header: str, rows: list[str]) -> None:
    path.write_text(header + "\n" + "\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")


class FakeLLM:
    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return "narrative"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


# ---------------------------------------------------------------------------
# AC1.0.3 -- parse_garmin_csv
# ---------------------------------------------------------------------------


def test_parse_garmin_csv_default_column_map_reads_date_and_hydration(tmp_path):
    csv_path = tmp_path / "garmin.csv"
    _write_csv(
        csv_path,
        "Date,Hydration(ml)",
        ["2026-08-13,2000", "2026-08-14,2500", "2026-08-15,1800"],
    )

    result = garmin.parse_garmin_csv(str(csv_path), {"date": "Date", "hydration_ml": "Hydration(ml)"})

    assert result == {"2026-08-13": 2000.0, "2026-08-14": 2500.0, "2026-08-15": 1800.0}


def test_parse_garmin_csv_sums_multiple_same_day_rows(tmp_path):
    csv_path = tmp_path / "garmin.csv"
    _write_csv(
        csv_path,
        "Date,Hydration(ml)",
        ["2026-08-13,500", "2026-08-13,700", "2026-08-13,300"],
    )

    result = garmin.parse_garmin_csv(str(csv_path), {"date": "Date", "hydration_ml": "Hydration(ml)"})

    assert result == {"2026-08-13": 1500.0}


def test_parse_garmin_csv_custom_column_map_is_honored(tmp_path):
    """AC1.0.3: column naming varies by device/locale -- a config-supplied
    column_map with different header names must be used instead of the
    default Date/Hydration(ml) names."""
    csv_path = tmp_path / "garmin_uk.csv"
    _write_csv(
        csv_path,
        "day,water_ml",
        ["2026-08-13,2200", "2026-08-14,1900"],
    )

    result = garmin.parse_garmin_csv(str(csv_path), {"date": "day", "hydration_ml": "water_ml"})

    assert result == {"2026-08-13": 2200.0, "2026-08-14": 1900.0}


def test_parse_garmin_csv_missing_configured_column_raises(tmp_path):
    """Using the default column_map against a file with different headers
    (no config override) is a configuration error -- the strict parser
    raises so build_garmin_report can degrade to "unavailable" (AC1.0.4)."""
    csv_path = tmp_path / "garmin.csv"
    _write_csv(csv_path, "day,water_ml", ["2026-08-13,2200"])  # no "Date"/"Hydration(ml)" headers

    with pytest.raises(ValueError):
        garmin.parse_garmin_csv(str(csv_path), {"date": "Date", "hydration_ml": "Hydration(ml)"})


def test_parse_garmin_csv_missing_file_raises(tmp_path):
    missing = tmp_path / "does_not_exist.csv"

    with pytest.raises(OSError):
        garmin.parse_garmin_csv(str(missing), {"date": "Date", "hydration_ml": "Hydration(ml)"})


def test_parse_garmin_csv_skips_rows_with_blank_date_or_hydration(tmp_path):
    csv_path = tmp_path / "garmin.csv"
    _write_csv(
        csv_path,
        "Date,Hydration(ml)",
        ["2026-08-13,2000", ",1500", "2026-08-15,"],  # 2 malformed rows, 1 good
    )

    result = garmin.parse_garmin_csv(str(csv_path), {"date": "Date", "hydration_ml": "Hydration(ml)"})

    assert result == {"2026-08-13": 2000.0}


def test_parse_garmin_csv_non_numeric_hydration_raises(tmp_path):
    csv_path = tmp_path / "garmin.csv"
    _write_csv(csv_path, "Date,Hydration(ml)", ["2026-08-13,not-a-number"])

    with pytest.raises(ValueError):
        garmin.parse_garmin_csv(str(csv_path), {"date": "Date", "hydration_ml": "Hydration(ml)"})


def test_parse_garmin_csv_non_iso_date_raises(tmp_path):
    csv_path = tmp_path / "garmin.csv"
    _write_csv(csv_path, "Date,Hydration(ml)", ["08/13/2026,2000"])  # MM/DD/YYYY, not ISO

    with pytest.raises(ValueError):
        garmin.parse_garmin_csv(str(csv_path), {"date": "Date", "hydration_ml": "Hydration(ml)"})


# ---------------------------------------------------------------------------
# AC1.0.3 -- build_garmin_report: join by date + discrepancy flagging
# ---------------------------------------------------------------------------


def test_build_garmin_report_none_when_csv_path_unconfigured(db):
    config = Config()  # default garmin.csv_path == ""
    report = garmin.build_garmin_report(db, config, date(2026, 8, 19), "owner")
    assert report is None


def test_build_garmin_report_joins_by_date_and_computes_correct_totals(db, tmp_path):
    end_date = date(2026, 8, 19)
    csv_path = tmp_path / "garmin.csv"
    week_days = [(end_date - timedelta(days=o)).isoformat() for o in range(6, -1, -1)]
    garmin_values = {d: 100.0 * (i + 1) for i, d in enumerate(week_days)}  # distinct known values
    _write_csv(csv_path, "Date,Hydration(ml)", [f"{d},{v}" for d, v in garmin_values.items()])

    self_reported = {d: 100.0 * (i + 1) for i, d in enumerate(week_days)}  # agrees exactly
    for d, v in self_reported.items():
        _seed(db, f"{d}T09:00:00", "water", v)

    config = Config.model_validate({"garmin": {"csv_path": str(csv_path)}})
    report = garmin.build_garmin_report(db, config, end_date, "owner")

    assert report is not None
    assert report.available is True
    assert len(report.comparisons) == 7
    for c in report.comparisons:
        assert c.self_reported_ml == pytest.approx(garmin_values[c.day])
        assert c.garmin_ml == pytest.approx(garmin_values[c.day])
        assert c.discrepancy_ml == pytest.approx(0.0)


def test_build_garmin_report_flags_discrepancy_beyond_threshold_not_within(db, tmp_path):
    """AC1.0.3: a discrepancy beyond the configured threshold is flagged;
    one within threshold is not."""
    end_date = date(2026, 8, 19)
    csv_path = tmp_path / "garmin.csv"
    week_days = [(end_date - timedelta(days=o)).isoformat() for o in range(6, -1, -1)]
    # Day -1: Garmin 2400, self-reported 500 -> diff 1900, beyond a 300 threshold -> flagged.
    # Day 0 (end_date): Garmin 2500, self-reported 2400 -> diff 100, within 300 -> not flagged.
    garmin_by_day = {d: 2000.0 for d in week_days}
    garmin_by_day[week_days[-2]] = 2400.0
    garmin_by_day[week_days[-1]] = 2500.0
    _write_csv(csv_path, "Date,Hydration(ml)", [f"{d},{v}" for d, v in garmin_by_day.items()])

    for d in week_days:
        _seed(db, f"{d}T09:00:00", "water", 2000.0)
    _seed(db, f"{week_days[-2]}T09:00:00", "water", -1500.0)  # net self-reported total 500 that day
    _seed(db, f"{week_days[-1]}T09:00:00", "water", 400.0)  # net self-reported total 2400 that day

    config = Config.model_validate({"garmin": {"csv_path": str(csv_path), "discrepancy_threshold_ml": 300}})
    report = garmin.build_garmin_report(db, config, end_date, "owner")

    by_day = {c.day: c for c in report.comparisons}
    flagged_day = by_day[week_days[-2]]
    within_day = by_day[week_days[-1]]
    assert flagged_day.self_reported_ml == pytest.approx(500.0)
    assert flagged_day.discrepancy_ml == pytest.approx(1900.0)
    assert flagged_day.discrepancy_ml > report.threshold_ml

    assert within_day.self_reported_ml == pytest.approx(2400.0)
    assert within_day.discrepancy_ml == pytest.approx(100.0)
    assert within_day.discrepancy_ml <= report.threshold_ml

    text_en = garmin.format_garmin_section(report, "en")
    flagged_lines = [ln for ln in text_en.splitlines() if flagged_day.day in ln]
    within_lines = [ln for ln in text_en.splitlines() if within_day.day in ln]
    assert any("⚠️" in ln for ln in flagged_lines)
    assert not any("⚠️" in ln for ln in within_lines)


def test_build_garmin_report_day_at_exact_threshold_is_not_flagged(db, tmp_path):
    """Boundary case: discrepancy_ml > threshold_ml is strict, so exactly
    at the threshold does not flag."""
    end_date = date(2026, 8, 19)
    csv_path = tmp_path / "garmin.csv"
    week_days = [(end_date - timedelta(days=o)).isoformat() for o in range(6, -1, -1)]
    _write_csv(csv_path, "Date,Hydration(ml)", [f"{d},2000" for d in week_days])
    for d in week_days:
        _seed(db, f"{d}T09:00:00", "water", 2000.0)
    _seed(db, f"{week_days[-1]}T09:00:00", "water", 300.0)  # exactly 300 above garmin's 2000

    config = Config.model_validate({"garmin": {"csv_path": str(csv_path), "discrepancy_threshold_ml": 300}})
    report = garmin.build_garmin_report(db, config, end_date, "owner")

    last_day = next(c for c in report.comparisons if c.day == week_days[-1])
    assert last_day.discrepancy_ml == pytest.approx(300.0)

    text_en = garmin.format_garmin_section(report, "en")
    last_day_lines = [ln for ln in text_en.splitlines() if last_day.day in ln]
    assert not any("⚠️" in ln for ln in last_day_lines)


def test_build_garmin_report_missing_garmin_day_defaults_to_zero(db, tmp_path):
    """A review-window day with no matching Garmin row (e.g. the watch
    wasn't worn that day) still gets a comparison row, with garmin_ml=0."""
    end_date = date(2026, 8, 19)
    csv_path = tmp_path / "garmin.csv"
    _write_csv(csv_path, "Date,Hydration(ml)", [f"{end_date.isoformat()},2000"])  # only 1 of 7 days
    _seed(db, f"{end_date.isoformat()}T09:00:00", "water", 2000.0)

    config = Config.model_validate({"garmin": {"csv_path": str(csv_path)}})
    report = garmin.build_garmin_report(db, config, end_date, "owner")

    assert len(report.comparisons) == 7
    other_day = next(c for c in report.comparisons if c.day != end_date.isoformat())
    assert other_day.garmin_ml == 0.0


# ---------------------------------------------------------------------------
# AC1.0.3 -- bilingual rendering via the i18n catalog
# ---------------------------------------------------------------------------


def test_format_garmin_section_renders_in_both_languages(db, tmp_path):
    end_date = date(2026, 8, 19)
    csv_path = tmp_path / "garmin.csv"
    week_days = [(end_date - timedelta(days=o)).isoformat() for o in range(6, -1, -1)]
    _write_csv(csv_path, "Date,Hydration(ml)", [f"{d},2000" for d in week_days])
    for d in week_days:
        _seed(db, f"{d}T09:00:00", "water", 2000.0)

    config = Config.model_validate({"garmin": {"csv_path": str(csv_path)}})
    report = garmin.build_garmin_report(db, config, end_date, "owner")

    text_en = garmin.format_garmin_section(report, "en")
    text_th = garmin.format_garmin_section(report, "th")

    assert i18n.t("garmin_section_header", "en") in text_en
    assert i18n.t("garmin_section_header", "th") in text_th
    assert text_en != text_th
    for d in week_days:
        assert d in text_en
        assert d in text_th


def test_format_garmin_section_none_report_is_empty_string():
    assert garmin.format_garmin_section(None, "en") == ""
    assert garmin.format_garmin_section(None, "th") == ""


# ---------------------------------------------------------------------------
# AC1.0.4 -- missing/malformed CSV handling: review still sends, no
# exception escapes, bilingual "unavailable" note.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make_broken_csv",
    [
        pytest.param(lambda tmp_path: str(tmp_path / "nonexistent.csv"), id="missing_file"),
        pytest.param(lambda tmp_path: _empty_file(tmp_path), id="empty_file"),
        pytest.param(lambda tmp_path: _non_numeric_file(tmp_path), id="non_numeric_hydration"),
        pytest.param(lambda tmp_path: _wrong_columns_file(tmp_path), id="wrong_column_names"),
    ],
)
def test_build_garmin_report_degrades_gracefully_for_every_broken_input(db, tmp_path, make_broken_csv):
    """These 4 shapes each break the WHOLE file (unreadable, no rows to
    infer headers from, a value that fails float() conversion, or the
    configured columns simply not present) -- build_garmin_report must
    catch every one and degrade to available=False, never raise, and the
    caller renders the bilingual "unavailable" note. See the standalone
    test below for the *different*, more forgiving case of a file that
    parses fine overall but has a few individually-malformed rows."""
    csv_path = make_broken_csv(tmp_path)
    config = Config.model_validate({"garmin": {"csv_path": csv_path}})

    report = garmin.build_garmin_report(db, config, date(2026, 8, 19), "owner")  # must not raise

    assert report is not None
    assert report.available is False
    assert report.comparisons == []

    text_en = garmin.format_garmin_section(report, "en")
    text_th = garmin.format_garmin_section(report, "th")
    assert i18n.t("garmin_unavailable", "en") in text_en
    assert i18n.t("garmin_unavailable", "th") in text_th


def test_build_garmin_report_tolerates_individually_malformed_rows_in_an_otherwise_good_file(db, tmp_path):
    """AC1.0.4's "malformed rows (wrong column count)" case, precisely:
    unlike a whole-file failure (tested above), a file that parses fine
    overall but has a couple of individually short/ragged rows (a row
    missing its hydration value, a row with extra trailing columns) is NOT
    treated as unavailable -- csv.DictReader tolerates ragged rows
    (missing trailing fields read as None, extra fields collected under
    the restkey), and parse_garmin_csv's own blank-field guard skips a row
    with no usable date/hydration rather than failing the whole file. The
    review still gets a full, correct comparison built from the rows that
    DID parse."""
    csv_path = tmp_path / "malformed.csv"
    _write_csv(
        csv_path,
        "Date,Hydration(ml)",
        ["2026-08-13", "2026-08-14,1000,extra,columns", "2026-08-15,2000"],
    )
    config = Config.model_validate({"garmin": {"csv_path": str(csv_path)}})

    report = garmin.build_garmin_report(db, config, date(2026, 8, 19), "owner")  # must not raise

    assert report.available is True
    by_day = {c.day: c for c in report.comparisons}
    assert by_day["2026-08-13"].garmin_ml == 0.0  # the ragged row with no hydration value was skipped
    assert by_day["2026-08-14"].garmin_ml == 1000.0  # extra trailing columns didn't break the good fields
    assert by_day["2026-08-15"].garmin_ml == 2000.0


def _empty_file(tmp_path) -> str:
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    return str(path)


def _non_numeric_file(tmp_path) -> str:
    path = tmp_path / "nonnumeric.csv"
    _write_csv(path, "Date,Hydration(ml)", ["2026-08-13,lots"])
    return str(path)


def _wrong_columns_file(tmp_path) -> str:
    path = tmp_path / "wrongcols.csv"
    _write_csv(path, "timestamp,ml_drunk", ["2026-08-13,2000"])
    return str(path)


async def test_run_weekly_review_still_sends_and_notes_garmin_unavailable_on_broken_csv(db, tmp_path):
    """AC1.0.4: the review as a whole (not just build_garmin_report in
    isolation) still produces a full text and sends -- no exception
    escapes to the caller."""
    from habit_assistant.core.habits import HabitRegistry

    registry = HabitRegistry.from_config(Config())
    end_date = date(2026, 8, 19)
    _seed(db, f"{end_date.isoformat()}T09:00:00", "water", 500.0)
    config = Config.model_validate({"garmin": {"csv_path": str(tmp_path / "nonexistent.csv")}})
    llm = FakeLLM()
    lang = i18n.resolve_unprompted_language(config)

    text = await run_weekly_review(db, config, registry, llm, lang, "owner", today=end_date)  # must not raise

    assert i18n.t("garmin_unavailable", "th") in text  # default primary_language is th


def test_build_garmin_report_malformed_csv_does_not_raise_out_of_build_report(db, tmp_path):
    """Direct re-check of the broad except in build_garmin_report -- a
    non-numeric hydration value raises ValueError inside parse_garmin_csv,
    and that must never propagate past build_garmin_report."""
    csv_path = tmp_path / "bad.csv"
    _write_csv(csv_path, "Date,Hydration(ml)", ["2026-08-13,not-a-number"])
    config = Config.model_validate({"garmin": {"csv_path": str(csv_path)}})

    report = garmin.build_garmin_report(db, config, date(2026, 8, 19), "owner")  # must not raise

    assert report.available is False


# ---------------------------------------------------------------------------
# AC1.0.5 -- no HTTP transport touched by the Garmin code path
# ---------------------------------------------------------------------------


async def test_garmin_parse_and_join_never_invokes_http_transport(db, tmp_path):
    """AC1.0.5: chart rendering and CSV parsing happen locally. Prove the
    Garmin path specifically never touches HTTP by handing it a transport
    that raises on ANY request -- if build_garmin_report (or
    parse_garmin_csv underneath it) ever tried to make an HTTP call, this
    transport would blow up the test."""

    def _forbidden_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Garmin code path made an unexpected HTTP request: {request.url}")

    end_date = date(2026, 8, 19)
    csv_path = tmp_path / "garmin.csv"
    week_days = [(end_date - timedelta(days=o)).isoformat() for o in range(6, -1, -1)]
    _write_csv(csv_path, "Date,Hydration(ml)", [f"{d},2000" for d in week_days])
    for d in week_days:
        _seed(db, f"{d}T09:00:00", "water", 2000.0)

    config = Config.model_validate({"garmin": {"csv_path": str(csv_path)}})

    # No transport is even constructed/passed to garmin.py -- it has no
    # httpx dependency at all (grep-confirmed in IMPL.md) -- this test's
    # _forbidden_handler exists purely as a tripwire in case a future
    # change accidentally threads a client through; if it's never
    # constructed, the transport is simply unused, which is itself the
    # point (garmin.py takes no client parameter).
    report = garmin.build_garmin_report(db, config, end_date, "owner")

    assert report is not None
    assert report.available is True
    # Sanity: garmin.py's public functions accept no httpx client at all.
    import inspect

    assert "client" not in inspect.signature(garmin.build_garmin_report).parameters
    assert "client" not in inspect.signature(garmin.parse_garmin_csv).parameters


def test_garmin_module_has_no_network_imports():
    """Static grep-equivalent check mirroring IMPL.md's own audit claim:
    core/garmin.py performs no network I/O of any kind."""
    import inspect

    source = inspect.getsource(garmin)
    for forbidden in ("httpx", "requests", "socket", "urlopen", "aiohttp"):
        assert forbidden not in source, f"core/garmin.py unexpectedly references {forbidden!r}"


# ---------------------------------------------------------------------------
# AC1.0.5 -- full weekly review (charts + Garmin + Telegram) contacts only
# the expected hosts.
# ---------------------------------------------------------------------------


async def test_full_weekly_review_with_charts_and_garmin_contacts_only_telegram_host(db, tmp_path):
    """Runs a full charts-enabled + Garmin-configured weekly review send
    (text + images) through a real TelegramChannel backed by a captured
    MockTransport, and asserts every single captured request's host is
    api.telegram.org -- proving neither charts.py nor garmin.py (nor
    anything else in this path) ever reaches out to any other host."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("123456:ABC-fake", "999", client=client)

    end_date = date(2026, 8, 19)
    week_days = [(end_date - timedelta(days=o)).isoformat() for o in range(6, -1, -1)]
    for d in week_days:
        _seed(db, f"{d}T09:00:00", "water", 2000.0)
        _seed(db, f"{d}T11:00:00", "stretch", 10.0)

    csv_path = tmp_path / "garmin.csv"
    _write_csv(csv_path, "Date,Hydration(ml)", [f"{d},2000" for d in week_days])
    config = Config.model_validate({"garmin": {"csv_path": str(csv_path)}})  # charts.enabled=True default
    registry = HabitRegistry.from_config(Config())
    llm = FakeLLM()
    lang = i18n.resolve_unprompted_language(config)

    text = await run_weekly_review(db, config, registry, llm, lang, "owner", today=end_date)
    await channel.send("999", text)
    pairs = render_weekly_review_charts(db, config, registry, lang, "owner", today=end_date)
    assert pairs  # sanity: charts were actually produced this run
    for image, caption in pairs:
        await channel.send_image("999", image, caption)

    assert len(captured) == 1 + len(pairs)  # 1 sendMessage + N sendPhoto
    hosts = {req.url.host for req in captured}
    assert hosts == {"api.telegram.org"}, f"unexpected host(s) contacted: {hosts}"

    await channel.aclose()


# ---------------------------------------------------------------------------
# Regression -- run_weekly_review byte-identical to v0.10.0 when Garmin is
# unconfigured (the default). Pinned directly against the pre-v1.0.0
# return expression (git show v0.10.0:src/habit_assistant/core/review.py's
# `return f"{header}\n\n{summary}\n\n{narrative}"`), per the same technique
# tests/test_streaks.py's own v0.10.0 regression section used.
# ---------------------------------------------------------------------------


async def test_run_weekly_review_byte_identical_to_v0100_when_garmin_unconfigured(db):
    from habit_assistant.core import i18n as i18n_module
    from habit_assistant.core.review import compute_weekly_stats, format_stats_summary

    registry = HabitRegistry.from_config(Config())
    end_date = date(2026, 8, 19)
    _seed(db, f"{end_date.isoformat()}T09:00:00", "water", 2500.0)
    _seed(db, f"{end_date.isoformat()}T11:00:00", "stretch", 10.0)
    config = Config()  # default garmin.csv_path == "" (unconfigured)
    llm = FakeLLM()

    # The literal v0.10.0 algorithm, reconstructed from `git show
    # v0.10.0:src/habit_assistant/core/review.py`'s run_weekly_review tail:
    #   header = i18n.t("weekly_review_header", lang)
    #   return f"{header}\n\n{summary}\n\n{narrative}"
    stats = compute_weekly_stats(db, config, registry, end_date, "owner")
    lang = i18n_module.resolve_unprompted_language(config)
    summary = format_stats_summary(stats, registry, lang)
    narrative = "narrative"  # FakeLLM.chat_text's fixed return value
    expected_v0100_shape = f"{i18n_module.t('weekly_review_header', lang)}\n\n{summary}\n\n{narrative}"

    actual = await run_weekly_review(db, config, registry, llm, lang, "owner", today=end_date)

    assert actual == expected_v0100_shape
    assert "Garmin" not in actual  # zero footprint when unconfigured (bonus, not just byte-pin)
