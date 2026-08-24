"""Per-user custom habits -- `/addhabit`/`/delhabit` (SPEC-v1.7.md §4
"Habit definition, validation & lifecycle", module `habitdef`, R-C1/R-C2/
R-V1-R-V5).

Two halves, mirroring every other settings-style command in this codebase
(`core/checkins.py`'s `/checkin`, `core/schedules.py`'s `/remind`):

- **`validate_and_normalize`** (R-V1-R-V5): a pure, DB-free function --
  fields in, either a normalized row ready for `db.add_user_habit` or a
  localized-error-id-and-kwargs out. Testable without a database, exactly
  matching SPEC-v1.7.md §5's own signature (`base_registry`/`user_registry`
  in, no `db`).
- **`execute_addhabit`/`execute_delhabit`** (R-C1/R-C2): the DB-touching
  half `core/commands.dispatch`'s `"addhabit"`/`"delhabit"` kinds feed --
  validate (or, for delhabit, look up), write, invalidate the acting
  user's cached registry (`core/registry_provider.py`, so the very next
  message/fan-out sees the change with no restart, AC-3), record one
  fail-open `core/audit.py` row, and confirm bilingually. Never raises,
  same "structured op in, formatted string out" contract every
  `execute_*` function in this codebase already follows.

Per-user isolation (R-C1/R-C2, mirrors every scoped module in this
codebase): every DB read/write below is scoped to a single `user_id`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from habit_assistant.config import _HABIT_ID_RE, _RESERVED_HABIT_IDS
from habit_assistant.core import audit, i18n
from habit_assistant.core.commands import reserved_trigger_words

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.commands import Command
    from habit_assistant.core.habits import HabitRegistry
    from habit_assistant.core.registry_provider import RegistryProvider
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

# R-V1's own grammar's required keys -- "id=", "type=", "en=" (R-V2's `th`/
# `unit`/`goal`/`alias` are each optional or conditionally required, dealt
# with further down).
_REQUIRED_KEYS = ("id", "type", "en")
_HABIT_TYPES = frozenset({"numeric", "duration", "text", "boolean"})


# ===========================================================================
# validate_and_normalize -- R-V1-R-V5, pure and DB-free.
# ===========================================================================


def _normalize_id(raw: str) -> str:
    """R-V1: trim, lowercase, spaces -> `_` -- BEFORE the `^[a-z0-9_]+$`
    shape check below, so "morning walk" normalizes to "morning_walk"
    (a valid id) rather than being rejected outright for containing a
    space."""
    return re.sub(r"\s+", "_", raw.strip().lower())


def _parse_unit(raw: str) -> tuple[str, str]:
    """"<en>[/<th>]" -> (unit_en, unit_th). A missing/empty Thai half
    defaults to the English one -- the spec's own grammar marks it
    optional the same bracket-notation way `th=` (the label) is, and
    R-V's own "Labels: en required, th defaults to en" recorded decision
    (SPEC-v1.7.md §9) is extended here for consistency."""
    unit_en, _, unit_th = raw.partition("/")
    unit_en = unit_en.strip()
    unit_th = unit_th.strip() or unit_en
    return unit_en, unit_th


def _parse_alias(raw: str) -> dict[str, float] | None:
    """"tok1:mult1,tok2:mult2,..." -> {tok: mult}. Returns `None` on ANY
    malformed segment (missing `:`, non-numeric or non-positive
    multiplier, empty token) -- `validate_and_normalize`'s caller treats
    that as `addhabit_invalid_alias` (usage-style, no write), same
    "malformed shape -> friendly error" posture as every other field
    here."""
    aliases: dict[str, float] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        tok, sep, mult_str = part.partition(":")
        if not sep:
            return None
        tok = tok.strip().lower()
        try:
            mult = float(mult_str.strip())
        except ValueError:
            return None
        if not tok or mult <= 0:
            return None
        aliases[tok] = mult
    return aliases


def _word_reserved(word: str, reserved: frozenset[str]) -> bool:
    """R-V3: case-insensitive, stripped comparison against `core/commands.
    py:reserved_trigger_words()` -- the single authoritative source, so
    this can never drift from what `dispatch()` actually treats as a
    command trigger."""
    return word.strip().lower() in reserved


def _label_exists(registry: "HabitRegistry", label: str, lang: str) -> bool:
    """R-V3's own "duplicating another of the user's active habit labels
    (in the same language)" check -- scanned across the FULL per-user
    registry (base + the user's own active custom habits), not just the
    custom rows: a duplicate label against a BASE habit's own label is
    just as much a genuine dispatch/matcher ambiguity (`_resolve_habit_
    token`'s `setdefault` is first-match-wins) as a duplicate against
    another custom habit would be."""
    target = label.strip().lower()
    for habit in registry:
        existing = habit.label_th if lang == "th" else habit.label_en
        if existing and existing.strip().lower() == target:
            return True
    return False


def validate_and_normalize(
    fields: dict[str, str],
    base_registry: "HabitRegistry",
    user_registry: "HabitRegistry",
    reserved: frozenset[str],
    cap: int,
) -> tuple[dict[str, object] | None, str | None, dict[str, object]]:
    """SPEC-v1.7.md §5/R-V1-R-V5: pure, DB-free validation of one
    `/addhabit` field set (as `core/commands.py:_parse_addhabit_fields`
    shaped it -- lowercased keys, stripped-but-verbatim string values).

    Returns `(row, None, {})` on success -- `row` is ready for `db.
    add_user_habit` (keys: id/type/label_en/label_th/unit_en/unit_th/goal/
    unit_aliases, the last a JSON string or `None`). Returns `(None,
    msg_id, kwargs)` on failure -- `msg_id` is a `core/i18n.py` catalog
    id, `kwargs` its `.format()` arguments; the CALLER (`execute_addhabit`)
    is the one that actually calls `i18n.t`, so this function stays
    lang-agnostic (mirrors `core/commands.py:_parse_target_value`'s own
    "structured result out, caller formats" split).

    Never raises and never writes -- purely a function of its arguments,
    so it's testable with plain in-memory `HabitRegistry` objects and no
    database at all, exactly as SPEC-v1.7.md §5's own signature (no `db`
    parameter) implies.

    Checked in R-V's own listed order: R-V5 (cap) first -- a well-formed
    request still can't land at the limit, so that's reported up front
    rather than after validating fields for a habit that can't be added
    regardless; then R-V1 (id); then R-V2 (type/unit/goal); then R-V3
    (label collision, including the reserved-word check for id AND both
    labels). R-V4 (unit collision) is deliberately NOT a rejection here --
    per its own spec text, creation is always ALLOWED; a colliding unit
    token simply falls out of the per-user preparse lookup once the habit
    joins the registry (`core/units.py:build_unit_lookup`'s existing
    cross-habit-collision rule, unchanged, now operating over the
    per-user registry)."""
    missing = [key for key in _REQUIRED_KEYS if not fields.get(key, "").strip()]
    if missing:
        return None, "addhabit_usage", {}

    active_custom_count = len(user_registry) - len(base_registry)
    if active_custom_count >= cap:
        return None, "addhabit_cap_reached", {"cap": cap}

    norm_id = _normalize_id(fields["id"])
    if not norm_id or not _HABIT_ID_RE.match(norm_id) or len(norm_id) > 32:
        return None, "addhabit_invalid_id", {"id": fields["id"].strip()}
    if norm_id in _RESERVED_HABIT_IDS:
        return None, "addhabit_invalid_id", {"id": norm_id}
    if _word_reserved(norm_id, reserved):
        return None, "addhabit_reserved_word", {"word": norm_id}
    if base_registry.get(norm_id) is not None:
        return None, "addhabit_shadow_base", {"id": norm_id}
    if user_registry.get(norm_id) is not None:
        # base ids were already excluded above -- a registry hit here can
        # only be one of THIS user's own already-active custom habits.
        return None, "addhabit_duplicate_id", {"id": norm_id}

    habit_type = fields["type"].strip().lower()
    if habit_type not in _HABIT_TYPES:
        return None, "addhabit_invalid_type", {"type": fields["type"].strip()}
    numeric_or_duration = habit_type in ("numeric", "duration")

    label_en = fields["en"].strip()
    label_th = fields.get("th", "").strip() or label_en
    for label in (label_en, label_th):
        if _word_reserved(label, reserved):
            return None, "addhabit_reserved_word", {"word": label}

    unit_raw = fields.get("unit", "").strip()
    goal_raw = fields.get("goal", "").strip()

    if numeric_or_duration and not unit_raw:
        return None, "addhabit_missing_unit", {}
    if not numeric_or_duration and unit_raw:
        return None, "addhabit_unexpected_unit", {}
    if not numeric_or_duration and goal_raw:
        return None, "addhabit_invalid_goal", {}

    unit_en: str | None = None
    unit_th: str | None = None
    if unit_raw:
        unit_en, unit_th = _parse_unit(unit_raw)
        if not unit_en:
            return None, "addhabit_missing_unit", {}

    goal: float | None = None
    if goal_raw:
        try:
            goal = float(goal_raw)
        except ValueError:
            return None, "addhabit_invalid_goal", {}
        if goal <= 0:
            return None, "addhabit_invalid_goal", {}

    aliases: dict[str, float] | None = None
    alias_raw = fields.get("alias", "").strip()
    if alias_raw:
        aliases = _parse_alias(alias_raw)
        if aliases is None:
            return None, "addhabit_invalid_alias", {}

    if _label_exists(user_registry, label_en, "en"):
        return None, "addhabit_duplicate_label", {"label": label_en}
    if _label_exists(user_registry, label_th, "th"):
        return None, "addhabit_duplicate_label", {"label": label_th}

    row: dict[str, object] = {
        "id": norm_id,
        "type": habit_type,
        "label_en": label_en,
        "label_th": label_th,
        "unit_en": unit_en,
        "unit_th": unit_th,
        "goal": goal,
        "unit_aliases": json.dumps(aliases) if aliases else None,
    }
    return row, None, {}


# ===========================================================================
# execute_addhabit -- R-C1.
# ===========================================================================

_KIND_LABEL_MSG_IDS: dict[str, str] = {
    "numeric": "habit_kind_numeric",
    "duration": "habit_kind_duration",
    "text": "habit_kind_text",
    "boolean": "habit_kind_boolean",
}


def _detail_phrase(row: dict[str, object], lang: i18n.Language) -> str:
    kind_label = i18n.t(_KIND_LABEL_MSG_IDS[row["type"]], lang)
    if row["type"] in ("numeric", "duration"):
        unit = row["unit_th"] if lang == "th" else row["unit_en"]
        if row["goal"] is not None:
            return i18n.t("addhabit_detail_goal", lang, kind=kind_label, unit=unit, goal=row["goal"])
        return i18n.t("addhabit_detail_no_goal", lang, kind=kind_label, unit=unit)
    return i18n.t("addhabit_detail_bare", lang, kind=kind_label)


def _build_addhabit_confirmation(row: dict[str, object], lang: i18n.Language) -> str:
    """SPEC-v1.7.md §3.1's own illustrative example: for
    id=reading|type=duration|en=reading|th=อ่านหนังสือ|unit=min/นาที|goal=30,
    lang="en" renders byte-identical to `'✅ Added "reading" (อ่านหนังสือ) —
    duration in min, goal 30/day. Log it like "20 min" or use /remind
    reading.'` -- `label`/`other_label` show BOTH languages' labels
    (own-language first, quoted; the other in parens) regardless of which
    language the reply itself is in, so the confirmation always
    disambiguates what got created even for a user who mixed scripts
    across `en=`/`th=`."""
    own_label = row["label_th"] if lang == "th" else row["label_en"]
    other_label = row["label_en"] if lang == "th" else row["label_th"]
    detail = _detail_phrase(row, lang)
    if row["type"] in ("numeric", "duration"):
        unit = row["unit_th"] if lang == "th" else row["unit_en"]
        example = f"20 {unit}"
        return i18n.t(
            "addhabit_success",
            lang,
            label=own_label,
            other_label=other_label,
            detail=detail,
            example=example,
            id=row["id"],
        )
    return i18n.t(
        "addhabit_success_bare", lang, label=own_label, other_label=other_label, detail=detail, id=row["id"]
    )


async def execute_addhabit(
    command: "Command",
    *,
    db: "Database",
    provider: "RegistryProvider",
    config: "Config",
    base_registry: "HabitRegistry",
    lang: i18n.Language,
    user_id: str,
) -> str:
    """SPEC-v1.7.md R-C1: `command.fields` is the raw pipe `key=value`
    dict `core/commands.py:_match_addhabit` captured -- `None` (a bare
    "/addhabit"/malformed Thai-alias tail) -> the usage reply, no write.
    Otherwise: validates (R-V1-R-V5, `validate_and_normalize`, against
    THIS user's current registry via `provider.for_user`), checks the one
    thing that function can't (an ARCHIVED id staying reserved, R-V1 --
    needs a DB lookup `validate_and_normalize`'s DB-free signature
    doesn't have), inserts the row, invalidates `user_id`'s cached
    registry (R-G2, so the very next message/fan-out sees it with no
    restart -- AC-3), records one fail-open `habit_create` audit row
    (mirrors `core/checkins.py:execute_checkin`'s own post-write-audit
    pattern), and confirms bilingually. Never raises."""
    if command.fields is None:
        return i18n.t("addhabit_usage", lang)

    user_registry = provider.for_user(user_id)
    row, msg_id, kwargs = validate_and_normalize(
        command.fields, base_registry, user_registry, reserved_trigger_words(), config.custom_habits.max_per_user
    )
    if row is None:
        return i18n.t(msg_id, lang, **kwargs)  # type: ignore[arg-type]

    # R-V1's own "not already used, active OR archived" rule: the active
    # half is already covered by validate_and_normalize's user_registry
    # check above (the registry excludes archived rows by construction,
    # R-G1); the archived half needs a DB lookup this DB-free function
    # can't make itself, so it's checked here, right before the write.
    try:
        existing = db.get_user_habit(user_id, row["id"])
    except Exception:
        logger.exception("Archived-id lookup failed for %r/%r; failing safe (no write)", user_id, row["id"])
        return i18n.t("addhabit_save_failed", lang)
    if existing is not None:
        return i18n.t("addhabit_archived_id", lang, id=row["id"])

    try:
        db.add_user_habit(user_id, row)
    except Exception:
        logger.exception("Failed to add custom habit %r for user %r", row["id"], user_id)
        return i18n.t("addhabit_save_failed", lang)

    provider.invalidate(user_id)
    audit.record(db, actor=user_id, action="habit_create", source="command", entity=row["id"], new_value=row["type"])

    return _build_addhabit_confirmation(row, lang)


# ===========================================================================
# execute_delhabit -- R-C2 (OQ2 resolved: smart delete).
# ===========================================================================


async def execute_delhabit(
    command: "Command",
    *,
    db: "Database",
    provider: "RegistryProvider",
    lang: i18n.Language,
    user_id: str,
) -> str:
    """SPEC-v1.7.md R-C2: `command.category` is the raw (lowercased)
    habit-id token `core/commands.py:_match_delhabit` captured -- `None`
    (a bare "/delhabit"/"ลบนิสัย") -> usage, no write. A token that isn't
    one of THIS user's own habits (never created, already hard-deleted,
    or already archived -- unarchive is deferred, §10) -> `delhabit_
    not_found`, no write; a base habit id (owner never has a `user_habits`
    row for it) resolves the same way, since `db.get_user_habit` only
    ever returns a row this user's OWN `/addhabit` created.

    Otherwise: **smart delete** (OQ2) -- soft-archives (`archived_at`
    stamped, id stays reserved, historical `logs` survive in `/history`)
    when `db.count_logs_for` is non-zero; hard-deletes (id freed) when
    it's zero. Either way, invalidates `user_id`'s cached registry (R-G2/
    AC-3) and records one fail-open `habit_archive`/`habit_delete` audit
    row. Never raises."""
    habit_id = (command.category or "").strip().lower()
    if not habit_id:
        return i18n.t("delhabit_usage", lang)

    try:
        row = db.get_user_habit(user_id, habit_id)
    except Exception:
        logger.exception("Habit lookup failed for %r/%r; failing safe (no write)", user_id, habit_id)
        return i18n.t("delhabit_save_failed", lang)
    if row is None or row["archived_at"] is not None:
        return i18n.t("delhabit_not_found", lang, id=habit_id)

    try:
        has_history = db.count_logs_for(user_id, habit_id) > 0
    except Exception:
        logger.exception("Log-count lookup failed for %r/%r; failing safe (no write)", user_id, habit_id)
        return i18n.t("delhabit_save_failed", lang)

    try:
        if has_history:
            db.archive_user_habit(user_id, habit_id)
        else:
            db.delete_user_habit(user_id, habit_id)
    except Exception:
        logger.exception("Failed to remove custom habit %r for user %r", habit_id, user_id)
        return i18n.t("delhabit_save_failed", lang)

    provider.invalidate(user_id)
    action = "habit_archive" if has_history else "habit_delete"
    audit.record(db, actor=user_id, action=action, source="command", entity=habit_id)

    if has_history:
        return i18n.t("delhabit_archived", lang, id=habit_id)
    return i18n.t("delhabit_deleted", lang, id=habit_id)
