"""Garmin hydration CSV import (ROADMAP.md v1.0.0 "...Garmin Import"),
closing SPEC.md §12's TODO.

Parses a local Garmin hydration export (stdlib `csv` -- no third-party
dependency) and joins it by date against this app's own `water` logs, for a
per-day self-reported-vs-device comparison appended to the weekly review.

`[garmin] csv_path = ""` (config.py's default) means the feature is off.
`column_map` maps this app's field names to the CSV's header names, since
export column naming varies by locale/device; the default assumes a
`Date, Hydration(ml)`-style export. The file is read locally only -- it is
never uploaded anywhere (AC1.0.5)."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date

from habit_assistant.config import Config
from habit_assistant.core import i18n, timeutil
from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GarminDayComparison:
    day: str
    self_reported_ml: float
    garmin_ml: float

    @property
    def discrepancy_ml(self) -> float:
        return abs(self.self_reported_ml - self.garmin_ml)


@dataclass(slots=True)
class GarminReport:
    """`available=False` means Garmin import is configured but the file
    was missing/malformed at review time -- the review notes this instead
    of failing (AC1.0.4). `comparisons` is empty in that case."""

    available: bool
    comparisons: list[GarminDayComparison]
    threshold_ml: float


def _normalize_date(raw: str) -> str:
    """Garmin hydration exports are commonly already ISO-dated
    (ROADMAP's own default assumption); anything that doesn't parse as
    ISO raises ValueError, which the caller treats as a malformed file
    (AC1.0.4)."""
    return date.fromisoformat(raw.strip()).isoformat()


def parse_garmin_csv(csv_path: str, column_map: dict[str, str]) -> dict[str, float]:
    """Return `{date_iso: hydration_ml}` parsed from the Garmin export,
    summing multiple same-day rows. Raises on a missing file, an
    unreadable/malformed CSV, or a missing/unparsable configured column
    -- callers (`build_garmin_report`) catch broadly and degrade to
    "unavailable" (AC1.0.4); this function stays strict so each failure
    mode is easy to test independently."""
    date_col = column_map.get("date", "Date")
    hydration_col = column_map.get("hydration_ml", "Hydration(ml)")

    result: dict[str, float] = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if date_col not in fieldnames or hydration_col not in fieldnames:
            raise ValueError(
                f"Garmin CSV {csv_path!r} missing expected column(s): {date_col!r}/{hydration_col!r}"
            )
        for row in reader:
            raw_date = (row.get(date_col) or "").strip()
            raw_ml = (row.get(hydration_col) or "").strip()
            if not raw_date or not raw_ml:
                continue
            day = _normalize_date(raw_date)
            result[day] = result.get(day, 0.0) + float(raw_ml)
    return result


def build_garmin_report(db: Database, config: Config, end_date: date, user_id: str) -> GarminReport | None:
    """None when Garmin import isn't configured (`csv_path` empty) --
    the review appends nothing in that case. A configured-but-broken file
    (missing, unreadable, malformed) returns `available=False` instead, so
    the caller can render the bilingual "unavailable" note without the
    weekly review itself failing (AC1.0.4). SPEC-v1.2.md R-D3: the
    self-reported side is scoped to `user_id` -- each user's Garmin
    cross-check compares only their own logged water."""
    csv_path = config.garmin.csv_path
    if not csv_path:
        return None

    try:
        garmin_by_day = parse_garmin_csv(csv_path, config.garmin.column_map)
    except Exception:
        logger.warning("Garmin CSV %r unavailable/malformed; noting it in the weekly review", csv_path, exc_info=True)
        return GarminReport(available=False, comparisons=[], threshold_ml=config.garmin.discrepancy_threshold_ml)

    comparisons = [
        GarminDayComparison(day=d, self_reported_ml=db.water_total_ml(user_id, d), garmin_ml=garmin_by_day.get(d, 0.0))
        for d in timeutil.week_days(end_date)
    ]
    return GarminReport(available=True, comparisons=comparisons, threshold_ml=config.garmin.discrepancy_threshold_ml)


def format_garmin_section(report: GarminReport | None, lang: i18n.Language) -> str:
    """`""` when `report` is None (feature off -- nothing appended to the
    review). Otherwise a bilingual section: the "unavailable" note
    (AC1.0.4), or a per-day self-reported-vs-Garmin comparison with
    discrepancies beyond `threshold_ml` flagged (AC1.0.3)."""
    if report is None:
        return ""

    lines = [i18n.t("garmin_section_header", lang)]
    if not report.available:
        lines.append(i18n.t("garmin_unavailable", lang))
        return "\n".join(lines)

    for c in report.comparisons:
        flagged = c.discrepancy_ml > report.threshold_ml
        msg_id = "garmin_day_line_flagged" if flagged else "garmin_day_line"
        lines.append(
            i18n.t(
                msg_id,
                lang,
                day=c.day,
                self_reported=int(c.self_reported_ml),
                garmin=int(c.garmin_ml),
                diff=int(c.discrepancy_ml),
            )
        )
    return "\n".join(lines)
