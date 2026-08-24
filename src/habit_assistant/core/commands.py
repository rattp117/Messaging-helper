"""Command-dispatch seam (ROADMAP.md v0.5.0 "Command Layer & Edit/Undo").

Runs BEFORE the LLM parser. Matches a small, conservative set of explicit
bilingual commands -- a leading `/command` and explicit undo/delete/edit
phrases -- and returns a structured `Command` describing the action to
take. Anything that doesn't match returns `None`: the caller falls through
to the normal `parse_message` LLM path unchanged (AC5.5) -- this module
never mutates or misclassifies a message it doesn't recognize.

Pure functions only, no channel import, no DB import, no LLM call for the
patterns matched here -- mirrors core/parser.py's "no channel imports"
rule and keeps the seam callable from main.py, tests, and later versions
(v0.8 queries, v0.9 snooze) alike. Conservative by design: every pattern
below is anchored to the *whole* stripped message (not a substring match),
because a false positive here would silently swallow a real habit log --
the worst failure mode for this router (ROADMAP.md's own risk note).

v0.7.0 (ROADMAP.md "Multi-Habit Extensibility", SPEC-v0.7.md §4 R14, module
M1): `dispatch`'s edit-value parsing is now driven by the live
`HabitRegistry` instead of hardcoded water/stretch units -- any configured
habit's `unit`/`unit_aliases` can be an edit target, not just water/stretch.
Ambiguous units (two habits sharing the same unit token) resolve first-match
in registry order (SPEC-v0.7.md §9 risk 6).

v0.8.0 (ROADMAP.md "Natural-Language Queries", AC8.1-AC8.5): a third,
LLM-free `"query"` kind, detected purely by anchored bilingual interrogative
patterns -- "how much/many", "did I"/"have I", Thai "กี่"/"เท่าไหร่"/
"เท่าไร"/"ไหม"/"หรือยัง", or a trailing "?"/"？". This module still never
calls the LLM itself (the actual `{habit_id, metric, timeframe}`
classification is `core/query.py`'s job, which `main.py` invokes only after
`dispatch` has flagged the message as query-shaped) -- keeping the
conservative "no false positives on a real log" contract for THIS layer:
none of the anchors above can appear in a plain log like "500ml" or "10 min
stretch" (see `tests/test_commands.py`'s adversarial corpus, unchanged and
still green). Checked after undo/edit and after snooze, so an edit-trigger
phrase that fails to parse as NUMBER [+ UNIT] still falls through to the
parser exactly as it did pre-v0.8 (it never reaches the query check) --
only a message that matched *neither* undo nor an edit trigger gets a
chance to match query.

v0.9.0 (ROADMAP.md "Adaptive Reminders, Snooze & Quiet Hours", AC9.3): a
fourth, LLM-free `"snooze"` kind -- an explicit, conservative bilingual
trigger ("snooze"/"snooze 30"/"/snooze 30", "เลื่อน"/"เลื่อนก่อน"/"เลื่อน 30
นาที") optionally carrying an explicit minute count in `Command.minutes`
(`None` means "use the configured default", `core/reminders.py`/`main.py`'s
job, not this module's -- this module never reads `Config.snooze`). Checked
*between* edit and query (SPEC-v0.7.md's own routing brief: "undo/edit ->
snooze -> query -> extractor") because "snooze" and "เลื่อน" don't overlap
either the undo/edit triggers or any query anchor, so ordering relative to
undo/edit doesn't matter in practice -- it's placed there to match the
brief's exact stated precedence. Resolving *which* habit to snooze is not
this module's job either (it has no DB/registry-state access beyond the
static `registry` argument) -- `main.py` resolves that from
`core/reminders.ReminderState.last_habit_id` at dispatch time.

v1.1.0 (SPEC-v1.1.md §4 R-T7-R-T9, module `targets`): a fifth, LLM-free
`"target"` kind -- deterministic per-habit daily-goal set/show/clear,
recognized by (a) the slash form `/target [<habit> [<value>|default]]`
and (b) conservative anchored bilingual NL "set" triggers ("set <habit>
goal to <value>", "change <habit>('s) goal to <value>", Thai "ตั้งเป้า
<habit> <value>" / "เป้า <habit> <value>"). Checked *after* snooze and
*before* query (R-T7's stated precedence: "undo/edit -> snooze -> target
-> query -> extractor"). Unlike edit's "garbled tail -> None" contract, a
`/target ...`/NL trigger whose value tail doesn't cleanly parse as
NUMBER [+ UNIT] still returns a `Command(kind="target",
target_action="usage")` -- never a silent `None` fall-through -- so the
caller (`core/targets_command.execute_target`) can reply with concrete
usage help instead of the message being misfiled as an unrecognized log
(R-T7's own explicit carve-out). This module never resolves whether a
named habit id actually exists/is goal-able -- an unresolved habit token
is carried through verbatim as `Command.category` (lowercased) so
`execute_target`'s own registry lookup is what reports
`target_invalid_habit`/`target_not_goalable` (R-T10); this module only
recognizes the *shape* of a target command and, for a value tail that DID
parse, converts any recognized unit alias to the habit's base unit
(mirrors `_parse_edit_value`'s registry-driven unit resolution). The full
free-form NL target-setting path (R-T13-R-T16, "from now on I want to
drink 2.5L a day") is NOT here -- that is `core/target_nl.py`'s
LLM-classification job, gated and routed by `main.py`, not this
anchored/deterministic layer.

v1.1.0 (SPEC-v1.1.md §4 R-D1, module `discoverability`, sequential
follow-on landed after the v1.1 shared surface + integration -- SPEC-v1.1.md
§11's own note that this module edits the same file the `targets` module
already touched, hence NOT parallel-safe with it): two more LLM-free kinds,
`"help"` (`^/help$`, `^ช่วยเหลือ$`, `^วิธีใช้$`) and `"habits"`
(`^/habits$`, `^นิสัย$`) -- each anchored to the *whole* stripped message,
same conservatism as every pattern above (R-C5: a plain log can never
accidentally equal one of these five exact strings). Checked alongside the
other anchored commands, after target and before query (R-D1's own stated
precedence: "... -> target -> help/habits -> query -> extractor") -- neither
kind carries any payload (`Command(kind="help")`/`Command(kind="habits")`
is the whole of it); the actual reply text is `core/discoverability.py`'s
job, dispatched by `main.py`.

v1.2.0 (SPEC-v1.2.md §4 R-S5, module `schedules`, parallel track landed
after the v1.2 shared surface): one more LLM-free kind, `"remind"` --
slash form `/remind <habit> [<HH:MM>...|off|default|reset|clear]` plus
the Thai alias `เตือน`. `Command.times` carries the parsed-but-UNVALIDATED
shape (`[]`=show, `["off"]`=off, `["default"]`=reset, else the raw HH:MM
token list for "set") -- `core/schedules.execute_remind` is where HH:MM
validation, de-dupe, and the <=24 cap actually happen (R-S5), mirroring
`target`'s own recognize-shape/execute split.

v1.2.0 audit fix (post-landing, prompted by sibling module `preferences`'s
own Vera-caught false-positive class on `ภาษา`/`เงียบ`): the Thai alias
`เตือน` originally used the same "mandatory space, then anything" gate as
the slash form -- too permissive, since `เตือน` (unlike "/remind") is a
real word that opens ordinary Thai prose ("เตือน ๆ หน่อยนะ", "เตือน
ฉันด้วยนะ" all mis-dispatched as `kind="remind"`). Fixed by requiring, for
the Thai form ONLY, (1) the habit token resolve to a real registry habit
via an alternation built from live `Habit.id`/`label_th` (mirrors
`_build_target_th_set_pattern`'s own precedent) and (2) any tail have the
SHAPE of a valid remind argument (clear/off word, or HH:MM-shaped
tokens) -- so a real habit word followed by ordinary prose ("เตือน น้ำ
ท่วมด้วย") also falls through. The slash form is untouched -- it was
never at risk (nobody types "/remind" by accident). See
`_match_remind`'s own docstring-comment block for the full analysis.

v1.2.0 (SPEC-v1.2.md §4 R-A1-R-A5, module `access`, parallel track landed
after the v1.2 shared surface): five more LLM-free kinds, English-only
slash forms -- `"start"` (`/start`), `"users"` (`/users`), and
`"approve"`/`"block"`/`"invite"` (`/approve|/block|/invite [<chat_id>]`,
`Command.target_chat` carrying the raw, unvalidated first token). No Thai
aliases (SPEC-v1.2.md §2.3 only gives Thai aliases for `/lang`/`/quiet`/
`/remind`, not these five). This layer only recognizes *shape* -- whether
the acting chat is actually the owner (R-A4), and whether `target_chat`
is a well-formed chat id, are `core/access.py:execute_admin`'s job, same
recognize-shape/execute split as `target`/`remind`.

v1.2.0 (SPEC-v1.2.md §4 R-P1/R-P2, module `preferences`, parallel track
landed after the v1.2 shared surface): two more LLM-free kinds, `"lang"`
(`/lang en|th|auto`, Thai alias `ภาษา en|th|auto`) and `"quiet"`
(`/quiet HH:MM-HH:MM[,...]|off`, Thai alias `เงียบ HH:MM-HH:MM[,...]|off`).
Both reuse `/remind`'s slash-form/Thai-alias split: the slash form
permits a bare `/lang`/`/quiet` with no value (so `core/preferences.py`'s
`execute_lang`/`execute_quiet` can reply with a usage message instead of
the message silently falling through) and stays fully permissive for any
non-empty tail otherwise (near-zero false-positive surface -- an
explicit "/" prefix no normal sentence starts with).

The Thai-alias form went through two hardening rounds (TEST-v1.2-
preferences.md) because `ภาษา`/`เงียบ` are ordinary Thai WORDS
("language"/"quiet") that legitimately open real sentences, unlike a
"/"-prefixed slash command. Round 1: the original mitigation (a
mandatory single whitespace before a non-empty value, mirroring
`/remind`'s Thai alias `เตือน`) only protected against the trigger glued
to more text with NO space -- a legitimate space-separated continuation
(the standard mai-yamok reduplication "เงียบ ๆ หน่อยนะ", or a natural
clause "ภาษา นี้ยากมาก") still misfired. Fixed by requiring the
Thai-alias value to be a single whitespace-free token, PLUS (at the
time) a curated Thai-prose-marker blacklist for `ภาษา` and a shape
whitelist for `เงียบ`. Round 2: Vera's follow-up audit found the `ภาษา`
blacklist structurally could not cover every ordinary Thai word -- 6
more realistic single-word messages ("ภาษา อังกฤษ"/"จีน"/"ใหม่"/"ดี"/
"สวย"/"อะไร", e.g. "which language?" or "[my] language [is] good") still
misfired. `ภาษา` now uses the same WHITELIST strategy `เงียบ` already
did: only `en`/`th`/`auto` (the valid value set, R-P1) plus the two
reviewed near-miss names (`ไทย`, `english`) dispatch at all --
`_LANG_TH_VALID_VALUES`, replacing the removed `_looks_like_th_prose`
blacklist. Everything else, including any other Thai language name in
prose, falls through to `None` -- SPEC-v1.2.md §2.3's zero-false-
positive discipline prioritizes precision here (the deterministic
`/lang en` always works, and free-form NL language-switching was
consciously deferred with the rest of NL phrasing, §10), so a Thai word
for a language name in ordinary prose must not be treated as a command.
See the comment block directly above `_match_lang`/`_match_quiet` for
the full current rationale. The slash forms remain untouched by both
rounds: an unrecognized language code or a malformed HH:MM window there
is still carried through verbatim (lowercased) as `Command.pref_value`
-- `core/preferences.py` is where the semantic validation, the `users`
table write, and the bilingual reply happen (same recognize-shape/
execute split as `target`/`remind`). Grouped with `remind`/`access` for
readability -- disjoint trigger text from every other kind means exact
placement doesn't change behavior.

v1.3.0 (SPEC-v1.3.md §4 R-V1, module `audit-view`): one more LLM-free
kind, `"audit"` -- the owner-only `/audit [N]` recent-activity viewer,
plus the optional Thai alias `ประวัติ [N]` (SPEC-v1.3.md §10: "Natural-
language phrasing for /audit" is explicitly out of scope -- deterministic
command + this one Thai alias only). `Command.limit` carries the parsed
N (an `int`), or `None` for a missing/non-numeric N -- `core/audit_view.
render_recent` treats `None` as "use the default limit" (R-V2/§3.3's own
"`/audit abc` (non-numeric N) falls back to the default limit"
contract), same recognize-shape/execute split as every settings-style
command above. The slash form stays fully permissive (any tail, parsed
best-effort) -- an explicit "/" prefix is the same near-zero
false-positive surface `/target`/`/remind`/`/lang`/`/quiet` already rely
on. The Thai alias, by contrast, is anchored to the WHOLE stripped
message with an optional PURELY-NUMERIC tail only (whole-message match,
optionally followed by whitespace + digits, nothing else) -- unlike a
registry habit token, "ประวัติ" ("history") is an
ordinary Thai word that can open real prose (e.g. "ประวัติศาสตร์...",
"เขาเขียนประวัติส่วนตัว..."), so this mirrors `/help`'s `_HELP_RE`/
`/habits`' `_HABITS_RE` own "whole-message, no partial/prefix match"
conservatism (never `เตือน`'s permissive-tail shape) rather than
`preferences`'s heavier whitelist machinery -- a glued continuation like
"ประวัติศาสตร์" can never match at all (§ can't reach `$` with trailing
non-digit, non-whitespace text after "ประวัติ"). Grouped with `access`'s
other owner-only admin commands for readability (disjoint trigger text,
so exact placement doesn't change behavior); whether the acting chat
actually IS the owner is enforced downstream (`main.py`'s integration-
step `access.classify(...) == "owner"` re-check, R-V3) -- this layer
only recognizes shape, same split as every command above."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from habit_assistant.core import units

if TYPE_CHECKING:
    from habit_assistant.core.habits import HabitRegistry

# SPEC-v1.2.md §5/§11: the eight new kinds below are a SKELETON only --
# added here (and the matching `Command` fields just below) so the three
# parallel modules (`access`, `preferences`, `schedules`) never collide on
# this file's shared enum/dataclass. `dispatch()` itself does not yet
# recognize/produce any of them -- each module adds its own anchored
# matching for its own disjoint kinds, exactly like `target`/`help`/
# `habits` were added in earlier releases.
CommandKind = Literal[
    "undo",
    "edit",
    "query",
    "snooze",
    "target",
    "help",
    "habits",
    # module `access` (R-A1-R-A5): onboarding + owner-only admin ops.
    "start",
    "approve",
    "block",
    "users",
    "invite",
    # module `preferences` (R-P1/R-P2): /lang, /quiet.
    "lang",
    "quiet",
    # module `schedules` (R-S5): /remind.
    "remind",
    # module `audit-view` (SPEC-v1.3.md R-V1): /audit.
    "audit",
    # module `history` (SPEC-v1.4.md R-D2): /history. Reuses Command's
    # existing `category` (habit filter, or an unresolved token flagged
    # for `history_view.render_history`'s own `history_invalid_habit`
    # reply) and `limit` fields (the parsed N) -- no new fields needed.
    "history",
    # SPEC-v1.5.md §4 R-K8 (module `checkins`): /checkin's own kind, plus
    # its Thai alias เช็คอิน. `/dnd` (Thai งดรบกวน, R-D5) is a PURE alias of
    # "quiet" -- it produces `Command(kind="quiet", ...)`, the exact same
    # shape `_match_quiet` already does, so no new kind is needed for it.
    "checkin",
    # SPEC-v1.6.md §5/§6 skeleton (shared surface): four new kinds for
    # the four parallel modules that build on this pass, pre-added here
    # so those modules' own `commands.py` edits never touch this Literal
    # declaration -- each module adds only its own `_match_*` matcher
    # function + one `dispatch()` branch, disjoint from the other three.
    # "dashboard" (module `dashboard`, R-D1): /dashboard's own kind, plus
    # its Thai alias แดชบอร์ด. Mirrors "checkin"'s own field reuse --
    # on/off tail (or a bare "/dashboard"/"แดชบอร์ด" for "show", same
    # non-error "empty = show" grammar as R-K8) populates `pref_value`,
    # not a new field.
    "dashboard",
    # "heatmap"/"records"/"trends" (modules `heatmap`/`insights`, R-H1/
    # R-R3/R-T2): all three share /history's own tail grammar (SPEC-
    # v1.6.md §2.1: "an optional registry habit id then an optional
    # integer, whole-message-anchored, registry/numeric-gated") --
    # `category` is the optional habit filter (or an unresolved token
    # flagged invalid, same "history_invalid_habit"-style convention) and
    # `limit` is the optional trailing integer. Only "heatmap" actually
    # uses `limit` (the weeks count, "/heatmap water 8"); "records" and
    # "trends" take a habit filter only and leave `limit` unused (always
    # `None`) -- no new fields needed for any of the three.
    "heatmap",
    "records",
    "trends",
    # SPEC-v1.7.md §5/§6/§11 (module `habitdef`): /addhabit and /delhabit's
    # own kinds, plus their Thai aliases เพิ่มนิสัย/ลบนิสัย. The bare Literal
    # entries landed here first (shared-surface seam); the pipe `key=value`
    # matcher/parsing and `Command.fields` (SPEC-v1.7.md §6: "commands.py --
    # addhabit/delhabit parsing (shared file; disjoint keys)") are
    # `habitdef`'s own later, disjoint edit to this same file -- see
    # `_match_addhabit`/`_match_delhabit` below. `reserved_trigger_words()`
    # below reserves "addhabit"/"delhabit"/เพิ่มนิสัย/ลบนิสัย against these
    # exact literals so the matcher below has no freedom to drift from
    # what's reserved.
    "addhabit",
    "delhabit",
]


@dataclass(slots=True)
class Command:
    kind: CommandKind
    category: str | None = None  # a configured habit id -- "edit", "target" (set/show/clear), "history" (filter), and (skeleton) "heatmap"/"records"/"trends" (filter)
    value_num: float | None = None  # new value -- "edit", and "target" (the new goal, in base unit) for "set"
    minutes: int | None = None  # explicit snooze minutes -- only set for "snooze"; None = use the configured default
    # SPEC-v1.1.md §5: which target operation -- only set for kind="target".
    target_action: Literal["set", "clear", "show", "show_all", "usage"] | None = None
    # SPEC-v1.2.md §5 skeleton (module `access`): the chat id an admin op
    # acts on -- "approve"/"block"/"invite". Not yet populated by dispatch().
    target_chat: str | None = None
    # SPEC-v1.2.md §5 (module `preferences`, R-P1/R-P2): the raw
    # (lowercased) trigger tail for "lang" ("en"/"th"/"auto", or an
    # unrecognized token -- `core/preferences.py:execute_lang` reports
    # `lang_usage`) or "quiet" ("22:00-07:00[,...]"/"off", or a malformed
    # token -- `execute_quiet` reports `quiet_invalid_window`); `None` for
    # a bare "/lang"/"/quiet" (or Thai-alias-with-no-value, which doesn't
    # match at all) with no value -- also `lang_usage`/`quiet_usage`.
    # SPEC-v1.5.md §5 (module `checkins`): also reused for "checkin"'s own
    # tail ("on"/"off"/"default"/"HH:MM-HH:MM", or `None` for a bare
    # "/checkin"/"เช็คอิน" -- R-K8's own "empty = show" grammar, NOT a usage
    # error unlike bare "/lang"/"/quiet"). "/dnd"/"งดรบกวน" (R-D5) is a pure
    # alias of "quiet" -- it populates THIS SAME field with the same
    # "22:00-07:00[,...]"/"off" shape `_match_quiet` already produces, no
    # dedicated field of its own.
    # SPEC-v1.6.md §5 skeleton (module `dashboard`): also reused for
    # "dashboard"'s own tail ("on"/"off", or `None` for a bare
    # "/dashboard"/"แดชบอร์ด" -- R-D1's own "empty = show" grammar, same
    # non-error convention as "checkin"). Not yet populated by dispatch().
    pref_value: str | None = None
    # SPEC-v1.2.md §5 skeleton (module `schedules`): "remind"'s parsed
    # times -- [] = show, ["off"] = off, ["default"] = reset, else an
    # HH:MM list to set. Not yet populated by dispatch().
    times: list[str] | None = None
    # SPEC-v1.3.md §5 (module `audit-view`, R-V1): the parsed N for
    # "audit" (e.g. "/audit 5" -> 5). `None` for a bare "/audit"/"ประวัติ",
    # a non-numeric tail ("/audit abc"), or any other unparsed tail --
    # `core/audit_view.render_recent` treats `None` as "use the default
    # limit" (R-V2). SPEC-v1.4.md §5 (module `history`, R-D2): reused
    # verbatim for "history"'s own parsed N -- same "None = default limit"
    # contract, this time honored by `core/history_view.render_history`.
    # SPEC-v1.6.md §5 skeleton (module `heatmap`): reused again for
    # "heatmap"'s own parsed weeks count ("/heatmap water 8" -> 8); `None`
    # = the module's own default (12 weeks, R-H1). Not used by "records"/
    # "trends" (always `None` for those two -- habit filter only, see
    # `category` below). Not yet populated by dispatch().
    limit: int | None = None
    # SPEC-v1.7.md §5/§6 (module `habitdef`): the raw, UNVALIDATED
    # pipe `key=value` field set for "addhabit" -- `_parse_addhabit_fields`
    # below builds it (lowercased keys, stripped-but-otherwise-verbatim
    # string values; a Thai `th=`/English `en=` value keeps its own
    # case/script). `None` means the tail didn't have the key=value pipe
    # SHAPE at all (a bare "/addhabit" with no tail, or a Thai-alias tail
    # that isn't key=value-shaped) -- `core/habitdef.py:execute_addhabit`
    # treats that as a usage reply, no write, same "recognized shape here,
    # semantic validation there" split as every command above. `category`
    # (existing field, reused rather than adding a redundant one) carries
    # "delhabit"'s own raw (lowercased) habit-id token -- resolved-or-not
    # exactly like `_resolve_target_category`'s established convention,
    # since a habit slated for deletion may not even be in the live
    # registry (archived id).
    fields: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# undo / delete last entry -- English "undo"/"delete", Thai "ยกเลิก"/"ลบ",
# and the literal "/undo" / "/delete" slash-commands.
# ---------------------------------------------------------------------------

_UNDO_PATTERNS = [
    re.compile(r"^/(undo|delete)$", re.IGNORECASE),
    re.compile(r"^(undo|delete)(\s+(the\s+)?(last|that))?(\s+(entry|log|message))?$", re.IGNORECASE),
    re.compile(r"^(ยกเลิก|ลบ)(อันล่าสุด|ล่าสุด|อันนั้น)?$"),
]

# ---------------------------------------------------------------------------
# edit-value -- an explicit trigger phrase followed by a new value. The
# trigger must lead the message; whatever follows must parse cleanly as
# NUMBER [+ UNIT] or the whole message is rejected (falls through to the
# parser) rather than guessed at.
# ---------------------------------------------------------------------------

_EDIT_TRIGGER = re.compile(
    r"^(?:/edit\s+|make that\s+|change (?:it|that)\s+to\s+|edit (?:it|that|last)\s+to\s+|"
    r"แก้(?:ไข)?(?:ล่าสุด)?เป็น\s*)"
    r"(?P<value>.+)$",
    re.IGNORECASE,
)

# SPEC-v1.5.md §4 R-L5: the unit-lookup/-resolution machinery (below,
# `_build_unit_lookup`/`_resolve_unit`) and this value regex moved to
# `core/units.py` so `core/preparse.py` (v1.5.0's deterministic
# pre-parser) can reuse the EXACT same logic instead of a second copy --
# imported here under their original private names so every call site in
# THIS file needed zero further edits (pure extract-and-delegate, byte-
# identical behavior, AC-2's own regression guard is the existing command
# test suite passing unmodified).
_VALUE_RE = units.VALUE_RE

# ---------------------------------------------------------------------------
# snooze -- ROADMAP.md v0.9.0 (AC9.3). English "snooze"/"snooze 30"/
# "/snooze 30" (an optional trailing minute count, with or without a
# "min(s)"/"minutes" unit word); Thai "เลื่อน"/"เลื่อนก่อน" (bare postpone)
# or "เลื่อน 30 นาที" (an explicit minute count). Anchored to the whole
# stripped message, same conservative "explicit trigger only" philosophy
# as undo/edit -- a message that merely mentions "snooze"/"เลื่อน" mid-
# sentence must not be swallowed (verified against the adversarial corpus
# in tests/test_commands.py).
# ---------------------------------------------------------------------------

_SNOOZE_EN_RE = re.compile(
    r"^/?snooze(?:\s+(?:for\s+)?(?P<minutes>\d+)\s*(?:min(?:ute)?s?)?)?$", re.IGNORECASE
)
_SNOOZE_TH_RE = re.compile(r"^เลื่อน(?:ก่อน)?(?:\s*(?P<minutes>\d+)\s*นาที)?$")


def _match_snooze(stripped: str) -> tuple[bool, int | None]:
    """Returns `(matched, minutes)`. `minutes` is the explicit count parsed
    out of the phrase (e.g. "snooze 30" -> 30), or `None` when the phrase
    carried no number (e.g. bare "snooze"/"เลื่อนก่อน") -- the caller falls
    back to `Config.snooze.default_minutes` for `None`."""
    for pattern in (_SNOOZE_EN_RE, _SNOOZE_TH_RE):
        match = pattern.match(stripped)
        if match is not None:
            minutes_str = match.group("minutes")
            return True, (int(minutes_str) if minutes_str else None)
    return False, None


# ---------------------------------------------------------------------------
# target set/show/clear -- SPEC-v1.1.md §4 R-T7-R-T9 (module `targets`).
# Slash form `/target [<habit> [<value>|default]]`, plus conservative
# anchored bilingual NL "set" triggers. Both forms resolve a habit TOKEN
# (an id or a configured label) via `_build_habit_token_lookup`, and a
# value tail via `_parse_target_value`, which reuses the same
# `_build_unit_lookup`/`_resolve_unit` machinery `_parse_edit_value`
# already uses for edit-value unit resolution (R-T9).
# ---------------------------------------------------------------------------

_TARGET_SLASH_RE = re.compile(r"^/target(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)
_TARGET_CLEAR_WORDS = {"default", "reset", "clear", "ค่าเริ่มต้น"}

# English NL "set"-only triggers (R-T7b). `\S+?` (non-greedy) on the habit
# group so the "change <habit>('s) ..." form correctly splits a possessive
# ("water's" -> habit "water", the "'s" consumed by its own optional
# group) instead of swallowing the apostrophe-s into the habit token.
_TARGET_EN_SET_PATTERNS = [
    re.compile(r"^set\s+(?P<habit>\S+?)\s+(?:goal|target)\s+to\s+(?P<value>.+)$", re.IGNORECASE),
    re.compile(r"^change\s+(?P<habit>\S+?)(?:'s)?\s+(?:goal|target)\s+to\s+(?P<value>.+)$", re.IGNORECASE),
]


def _build_habit_token_lookup(registry: "HabitRegistry") -> dict[str, str]:
    """Maps a lowercased habit id / English label / Thai label -> habit id,
    so a target command's habit token can be given as the raw id (slash
    form, English NL triggers: "water") or its configured label (Thai NL
    trigger: "น้ำ"). Iterated in registry order with `setdefault` --
    first-match-wins on a shared token, same convention as
    `_build_unit_lookup`. Unlike that lookup, this one is NOT filtered to
    numeric/duration habits -- a target command must also be able to name
    a non-goalable habit (e.g. "/target diary 5") so `execute_target` can
    report `target_not_goalable` rather than a bare `target_invalid_habit`."""
    lookup: dict[str, str] = {}
    for habit in registry:
        lookup.setdefault(habit.id.strip().lower(), habit.id)
        if habit.label_en:
            lookup.setdefault(habit.label_en.strip().lower(), habit.id)
        if habit.label_th:
            lookup.setdefault(habit.label_th.strip().lower(), habit.id)
    return lookup


def _resolve_habit_token(token: str, registry: "HabitRegistry") -> str | None:
    return _build_habit_token_lookup(registry).get(token.strip().lower())


def _resolve_target_category(habit_token: str, registry: "HabitRegistry") -> str:
    """The habit id if `habit_token` resolves, else the raw (lowercased)
    token itself -- letting `execute_target`'s own registry lookup fail
    and report `target_invalid_habit` (R-T10/AC16) rather than this
    module silently rejecting an unrecognized habit name."""
    resolved = _resolve_habit_token(habit_token, registry)
    return resolved if resolved is not None else habit_token.strip().lower()


def _build_target_th_set_pattern(registry: "HabitRegistry") -> re.Pattern[str] | None:
    """Thai "ตั้งเป้า<habit><value>" / "เป้า<habit><value>" (R-T7b), habit
    token built from the LIVE registry's ids/Thai labels rather than a
    generic "any non-digit run" character class. Thai script is normally
    written with no spaces between words, so a generic habit-token class
    risks a false positive on an unrelated sentence that happens to start
    with "เป้า" (e.g. "เป้าหมายของฉันคือ 2000 บาท", a diary-style reflection
    about a personal goal in baht, not a habit target) -- anchoring the
    habit token to only the habits actually configured eliminates that
    false-positive class entirely (a message naming a habit this bot
    doesn't track can't accidentally look like this trigger). Returns None
    if the registry has no matchable Thai/id tokens (defensive; every
    shipped config has at least `water`'s "น้ำ")."""
    tokens: set[str] = set()
    for habit in registry:
        tokens.add(habit.id)
        if habit.label_th:
            tokens.add(habit.label_th)
    escaped = sorted((re.escape(t) for t in tokens if t.strip()), key=len, reverse=True)
    if not escaped:
        return None
    habit_alt = "|".join(escaped)
    return re.compile(rf"^(?:ตั้งเป้า|เป้า)\s*(?P<habit>{habit_alt})\s*(?P<value>\d.*)$")



# Unlike `_VALUE_RE` (edit values, always a physical quantity actually
# logged), a target's proposed value can legitimately be typed as a
# non-positive number ("/target water 0"/"-5", AC15) -- that must still
# reach `execute_target`'s own `target_invalid_value` reply, not silently
# become a `target_usage` reply. So target-value parsing accepts an
# optional leading "-" that `_VALUE_RE` deliberately does not.
_TARGET_VALUE_RE = re.compile(r"^(?P<num>-?\d+(?:\.\d+)?)\s*(?P<unit>\S+)?\s*$")


def _parse_target_value(habit_token: str, value_str: str, registry: "HabitRegistry") -> tuple[str, float] | None:
    """Parse a target-command value tail into (category, goal_in_base_unit),
    or None if the tail doesn't cleanly parse as NUMBER [+ UNIT], or an
    explicit unit resolves to a habit OTHER than the one named (R-T9's
    usage-error case). `category` is the resolved habit id when the named
    habit token matches a configured habit; otherwise it is the raw
    (lowercased) token, so an unrecognized habit ("/target coffee 2000")
    still produces a "set" command `execute_target` can reject with
    `target_invalid_habit` (AC16) -- this function never treats an
    unresolvable habit token as a parse failure by itself. A non-positive
    number ("/target water 0"/"-5") is likewise passed through unchanged
    (AC15) -- `execute_target`'s own "set" validation is what reports
    `target_invalid_value`, not this layer."""
    match = _TARGET_VALUE_RE.match(value_str.strip())
    if not match:
        return None
    num = float(match.group("num"))
    unit_raw = match.group("unit")
    resolved_habit_id = _resolve_habit_token(habit_token, registry)

    if resolved_habit_id is None:
        # The named habit itself isn't recognized -- a trailing unit token
        # can't be validated against an unknown habit's alias table, so
        # it's ignored; the raw token is what execute_target reports as
        # invalid, regardless of what the unit might otherwise suggest.
        return habit_token.strip().lower(), num

    if unit_raw is None:
        return resolved_habit_id, num

    unit_resolution = _resolve_unit(_build_unit_lookup(registry), unit_raw.lower())
    if unit_resolution is None:
        return None  # unrecognized unit token -> usage (can't tell what's meant)
    unit_habit_id, multiplier = unit_resolution
    if unit_habit_id != resolved_habit_id:
        return None  # unit belongs to a different habit than the one named -> usage (R-T9)
    return resolved_habit_id, num * multiplier


def _build_target_set_or_usage(habit_token: str, value_str: str, registry: "HabitRegistry") -> "Command":
    parsed = _parse_target_value(habit_token, value_str, registry)
    if parsed is None:
        return Command(kind="target", target_action="usage")
    category, value_num = parsed
    return Command(kind="target", category=category, value_num=value_num, target_action="set")


def _match_target_slash(stripped: str, registry: "HabitRegistry") -> "Command | None":
    match = _TARGET_SLASH_RE.match(stripped)
    if match is None:
        return None
    rest = match.group("rest")
    if rest is None:
        return Command(kind="target", target_action="show_all")

    parts = rest.strip().split(None, 1)
    habit_token = parts[0]
    tail = parts[1].strip() if len(parts) > 1 else None

    if tail is None:
        return Command(kind="target", category=_resolve_target_category(habit_token, registry), target_action="show")

    if tail.lower() in _TARGET_CLEAR_WORDS:
        return Command(kind="target", category=_resolve_target_category(habit_token, registry), target_action="clear")

    return _build_target_set_or_usage(habit_token, tail, registry)


def _match_target_nl(stripped: str, registry: "HabitRegistry") -> "Command | None":
    for pattern in _TARGET_EN_SET_PATTERNS:
        match = pattern.match(stripped)
        if match is not None:
            return _build_target_set_or_usage(match.group("habit"), match.group("value"), registry)

    th_pattern = _build_target_th_set_pattern(registry)
    if th_pattern is not None:
        match = th_pattern.match(stripped)
        if match is not None:
            return _build_target_set_or_usage(match.group("habit"), match.group("value"), registry)

    return None


def _match_target(stripped: str, registry: "HabitRegistry") -> "Command | None":
    return _match_target_slash(stripped, registry) or _match_target_nl(stripped, registry)


# ---------------------------------------------------------------------------
# remind -- SPEC-v1.2.md §4 R-S5 (module `schedules`). Slash form
# `/remind <habit> [<HH:MM> [<HH:MM> ...]|off|default|reset|clear]` stays
# fully permissive (an unresolved habit token or an arbitrary tail still
# produces a Command, letting `execute_remind` report a friendly
# `remind_invalid_habit`/`remind_invalid_time` -- mirrors `/target`'s own
# slash-form behavior, AC16). Nobody types "/remind" by accident in prose,
# so this form carries no false-positive risk.
#
# Unlike `/target`'s slash form, there is no bare `/remind` (no habit)
# "show all" shape -- SPEC-v1.2.md §2.3 lists only per-habit forms -- so a
# bare `/remind` with no habit token simply doesn't match (falls through
# to `None`), same conservative posture as every other anchored command
# here.
#
# The Thai alias `เตือน`, by contrast, IS a real Thai word that can open
# ordinary prose -- audit finding (post-landing, prompted by sibling
# module `preferences`'s own Vera-caught false-positive class on
# `ภาษา`/`เงียบ`): a bare mandatory-`\s+`-then-anything gate is NOT enough.
# Correctly-spelled Thai puts a space before particles like the
# mai-yamok "ๆ" and other trailing words ("เตือน ๆ หน่อยนะ", "เตือน
# ฉันด้วยนะ", "เตือน แล้วนะ" -- all ordinary sentences, none of them a
# remind command), so the earlier "any token after a space" habit-token
# lookup mis-dispatched every one of these as `kind="remind"` with an
# unresolved/garbage category. Fixed the same way `preferences` fixed its
# own two triggers: dispatch only on a VALID ARGUMENT SHAPE for this
# bare-word form, prose falls through. Two restrictions, mirroring
# `_build_target_th_set_pattern`'s own registry-anchored precedent:
#   1. The habit token must resolve to a REAL configured habit (id or
#      Thai label) via a registry-built alternation, not an arbitrary
#      token -- "เตือน ๆ หน่อยนะ"/"เตือน ฉันด้วยนะ"/"เตือน แล้วนะ" name no
#      configured habit at all, so none of them can match anymore.
#   2. Even when the habit token IS real, a trailing tail must itself
#      look like a valid remind argument (a clear/off word, or a
#      whitespace-separated list of HH:MM-SHAPED tokens -- not yet
#      validated as REAL times, that's still `execute_remind`'s job,
#      R-S5) -- otherwise the match is rejected. This catches
#      "เตือน น้ำ ท่วมด้วย" ("[a message about] water flooding" --
#      "น้ำ" IS a real habit label, but "ท่วมด้วย" isn't a time/off/
#      default shape, so this now falls through instead of misfiring).
# The slash form is untouched by either restriction (see above).
#
# `Command.times` carries the UNVALIDATED tail exactly as SPEC-v1.2.md §5
# describes it: `[]` = show, `["off"]` = off, `["default"]` = reset, else
# the raw whitespace-split tokens for "set". HH:MM validation, de-dupe,
# and the <=24 cap are `core/schedules.execute_remind`'s job (R-S5), not
# this layer's -- mirrors `/target`'s own recognize-shape/execute split.
# ---------------------------------------------------------------------------

_REMIND_SLASH_RE = re.compile(r"^/remind\s+(?P<rest>\S.*)$", re.IGNORECASE)

# A single valid remind-argument TOKEN shape (not yet a real time -- just
# digits-and-a-colon, so an intentionally-malformed time like "25:99" can
# still reach `execute_remind`'s own `remind_invalid_time` reply through
# the Thai form too, symmetric with the slash form) -- used only to keep
# ordinary Thai prose out of the Thai bare-word trigger's match, per the
# audit finding above.
_REMIND_TIME_TOKEN_SHAPE_RE = re.compile(r"^\d{1,2}:\d{2}$")


def _build_remind_command(category: str, tail: str | None) -> "Command":
    if tail is None:
        return Command(kind="remind", category=category, times=[])
    if tail.lower() in _TARGET_CLEAR_WORDS:
        return Command(kind="remind", category=category, times=["default"])
    if tail.lower() == "off":
        return Command(kind="remind", category=category, times=["off"])
    return Command(kind="remind", category=category, times=tail.split())


def _remind_tail_has_valid_shape(tail: str) -> bool:
    """Gate for the Thai bare-word trigger ONLY (see the audit note
    above) -- `tail` is a clear/off word, or every whitespace-separated
    token in it has the digits-and-colon SHAPE of an HH:MM time (real
    HH:MM validation is still `execute_remind`'s job, R-S5). Ordinary
    Thai prose following a real habit word (e.g. "ท่วมด้วย" in "เตือน น้ำ
    ท่วมด้วย") never has this shape."""
    lowered = tail.lower()
    if lowered in _TARGET_CLEAR_WORDS or lowered == "off":
        return True
    tokens = tail.split()
    return bool(tokens) and all(_REMIND_TIME_TOKEN_SHAPE_RE.match(tok) for tok in tokens)


def _build_remind_th_pattern(registry: "HabitRegistry") -> re.Pattern[str] | None:
    """Thai "เตือน<habit>[<tail>]" (SPEC-v1.2.md §2.3's "Thai alias:
    เตือน"), habit token built from the LIVE registry's ids/Thai labels --
    same false-positive mitigation as `_build_target_th_set_pattern`
    (only a message naming a habit this bot actually tracks can ever
    match at all). Returns None if the registry has no matchable Thai/id
    tokens (defensive; every shipped config has at least water's "น้ำ")."""
    tokens: set[str] = set()
    for habit in registry:
        tokens.add(habit.id)
        if habit.label_th:
            tokens.add(habit.label_th)
    escaped = sorted((re.escape(t) for t in tokens if t.strip()), key=len, reverse=True)
    if not escaped:
        return None
    habit_alt = "|".join(escaped)
    return re.compile(rf"^เตือน\s+(?P<habit>{habit_alt})(?:\s+(?P<tail>.+))?$")


def _match_remind(stripped: str, registry: "HabitRegistry") -> "Command | None":
    slash_match = _REMIND_SLASH_RE.match(stripped)
    if slash_match is not None:
        parts = slash_match.group("rest").strip().split(None, 1)
        habit_token = parts[0]
        tail = parts[1].strip() if len(parts) > 1 else None
        # `_resolve_target_category` is a generic habit-token resolver
        # (id / en label / th label -> id, else the raw lowercased token)
        # despite its name -- shared verbatim with `/target` rather than
        # duplicated here.
        return _build_remind_command(_resolve_target_category(habit_token, registry), tail)

    th_pattern = _build_remind_th_pattern(registry)
    if th_pattern is not None:
        th_match = th_pattern.match(stripped)
        if th_match is not None:
            tail_raw = th_match.group("tail")
            tail = tail_raw.strip() if tail_raw else None
            if tail is None or _remind_tail_has_valid_shape(tail):
                # The habit token came straight out of the registry-built
                # alternation above, so it is always resolvable -- the
                # `or` fallback is defensive only, never actually hit.
                category = _resolve_habit_token(th_match.group("habit"), registry) or th_match.group("habit").lower()
                return _build_remind_command(category, tail)

    return None


# ---------------------------------------------------------------------------
# help / habits -- SPEC-v1.1.md §4 R-D1 (module `discoverability`). Anchored
# to the whole stripped message, exactly the five literal strings R-D1
# names -- no partial/prefix matching, so neither can ever fire on a real
# habit log (AC40).
# ---------------------------------------------------------------------------

_HELP_RE = re.compile(r"^(?:/help|ช่วยเหลือ|วิธีใช้)$", re.IGNORECASE)
_HABITS_RE = re.compile(r"^(?:/habits|นิสัย)$", re.IGNORECASE)


def _match_help(stripped: str) -> bool:
    return _HELP_RE.match(stripped) is not None


def _match_habits(stripped: str) -> bool:
    return _HABITS_RE.match(stripped) is not None


# ---------------------------------------------------------------------------
# onboarding + owner-only admin -- SPEC-v1.2.md §4 R-A1-R-A5 (module
# `access`). English slash forms only -- SPEC-v1.2.md §2.3 gives Thai
# aliases for `/lang`/`/quiet`/`/remind` (modules `preferences`/`schedules`)
# but none for `/start`/`/approve`/`/block`/`/users`/`/invite`, so none are
# added here. Each is anchored to the whole stripped message, same
# conservatism as `/help`/`/habits` above -- none of these five literal
# words can appear as a real habit log. `/approve`/`/block`/`/invite`
# capture the FIRST whitespace-delimited token after the command word as
# `target_chat` (raw, unvalidated -- `core/access.py:execute_admin` is
# where a malformed/missing token becomes the R-A4/§3.5 usage reply, not
# this shape-only layer); a bare `/approve` with no token still produces a
# Command (target_chat=None), never a silent None fall-through, mirroring
# `_match_target_slash`'s own "recognized shape -> always a Command" rule.
# ---------------------------------------------------------------------------

_START_RE = re.compile(r"^/start$", re.IGNORECASE)
_USERS_RE = re.compile(r"^/users$", re.IGNORECASE)
_APPROVE_RE = re.compile(r"^/approve(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)
_BLOCK_RE = re.compile(r"^/block(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)
_INVITE_RE = re.compile(r"^/invite(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)


def _first_token(rest: str | None) -> str | None:
    if rest is None:
        return None
    parts = rest.strip().split(None, 1)
    return parts[0] if parts else None


def _match_access(stripped: str) -> "Command | None":
    if _START_RE.match(stripped):
        return Command(kind="start")
    if _USERS_RE.match(stripped):
        return Command(kind="users")
    match = _APPROVE_RE.match(stripped)
    if match is not None:
        return Command(kind="approve", target_chat=_first_token(match.group("rest")))
    match = _BLOCK_RE.match(stripped)
    if match is not None:
        return Command(kind="block", target_chat=_first_token(match.group("rest")))
    match = _INVITE_RE.match(stripped)
    if match is not None:
        return Command(kind="invite", target_chat=_first_token(match.group("rest")))
    return None


# ---------------------------------------------------------------------------
# audit -- SPEC-v1.3.md §4 R-V1 (module `audit-view`). The owner-only
# `/audit [N]` recent-activity viewer, plus the optional Thai alias
# `ประวัติ [N]`. See this module's own docstring (v1.3.0 section, above)
# for the full slash-form-permissive / Thai-alias-conservative rationale.
# ---------------------------------------------------------------------------

_AUDIT_SLASH_RE = re.compile(r"^/audit(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)
# Anchored to the WHOLE stripped message (mirrors `_HELP_RE`/`_HABITS_RE`,
# not `เตือน`'s permissive-tail shape) -- "ประวัติ" is an ordinary Thai
# word ("history") that opens real prose, so only a bare match or a
# purely-numeric tail is recognized; anything else falls through to None.
_AUDIT_TH_RE = re.compile(r"^ประวัติ(?:\s+(?P<n>\d+))?$")


def _parse_audit_limit(rest: str | None) -> int | None:
    """The first whitespace-delimited token after "/audit", parsed as a
    plain non-negative integer -- missing, non-numeric, or any other
    garbage tail -> `None` (R-V2/§3.3: "a non-numeric N uses the default
    limit"), never a rejected match -- `/audit <anything>` always
    recognizes as an "audit" command, mirroring `/target`'s/`/remind`'s
    own "recognized shape -> always a Command" permissiveness."""
    if rest is None:
        return None
    token = rest.strip().split(None, 1)[0]
    return int(token) if token.isdigit() else None


def _match_audit(stripped: str) -> "Command | None":
    match = _AUDIT_SLASH_RE.match(stripped)
    if match is not None:
        return Command(kind="audit", limit=_parse_audit_limit(match.group("rest")))

    th_match = _AUDIT_TH_RE.match(stripped)
    if th_match is not None:
        n = th_match.group("n")
        return Command(kind="audit", limit=int(n) if n else None)

    return None


# ---------------------------------------------------------------------------
# history -- SPEC-v1.4.md §4 R-D2 (module `history`). `/history [<habit>]
# [<N>]`, plus the Thai alias `ย้อนหลัง` ("retrospective/past" -- NOT
# `ประวัติ`, which `/audit` already owns, AC-5's own explicit "must not
# collide" requirement). Tail grammar (SPEC-v1.4.md §2.1), in order:
# [<habit>] [<N>] -- first token a registry habit id/label -> filter; else
# digits -> N; a second \d+ token after a resolved habit -> N.
#
# The slash form stays fully permissive (mirrors `/target`/`/remind`/
# `/audit`'s own posture -- nobody types "/history" by accident, so any
# tail, however malformed, still produces a Command; an unresolved first
# token is carried through raw via `_resolve_target_category`'s own
# "resolved id, else the raw lowercased token" fallback, letting
# `history_view.render_history`'s own `registry.get(category)` check
# report the friendly `history_invalid_habit` reply -- same recognize-
# shape-here/validate-there split as `/target`'s AC16 pattern).
#
# The Thai alias `ย้อนหลัง` is, by contrast, an ordinary Thai WORD that can
# open real prose ("ย้อนหลังไปสามปีที่แล้ว...", "looking back three years
# ago...") -- same false-positive risk class `เตือน`/`ภาษา`/`เงียบ` were
# each found to have (TEST-v1.2/1.3-*.md's own hardening rounds), so this
# alias is hardened FROM THE START rather than retrofitted later: the
# tail, when present, must be built entirely from a REGISTRY-anchored
# habit token and/or a purely-numeric token (mirrors `_build_remind_th_
# pattern`'s own registry-alternation precedent). The mandatory trailing
# `$` anchor is what actually does the work -- any unrecognized trailing
# text (ordinary prose after "ย้อนหลัง") leaves characters unconsumed by
# either optional group, so the whole match fails and falls through to
# `None`, never partially matching (AC-5's adversarial-corpus requirement).
# ---------------------------------------------------------------------------

_HISTORY_SLASH_RE = re.compile(r"^/history(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)


def _parse_history_tail(rest: str | None, registry: "HabitRegistry") -> tuple[str | None, int | None]:
    """SPEC-v1.4.md §2.1's tail grammar: `[<habit>] [<N>]`, in that order.
    First token: a registry habit id/label -> filter (`category`, via
    `_resolve_target_category`'s existing resolved-id-or-raw-token
    fallback); else digits -> N (`limit`), no filter. A second `\\d+`
    token after a resolved habit -> N. Any token beyond the first two is
    ignored (mirrors this file's other slash-form tails' own leniency
    toward trailing content -- `execute_target`/`schedules.execute_remind`
    apply the SAME "the view layer, not this shape-only layer, is what
    rejects a malformed request" posture)."""
    if rest is None:
        return None, None
    parts = rest.strip().split()
    if not parts:
        return None, None
    first = parts[0]
    if first.isdigit():
        return None, int(first)
    category = _resolve_target_category(first, registry)
    if len(parts) > 1 and parts[1].isdigit():
        return category, int(parts[1])
    return category, None


def _build_history_th_pattern(registry: "HabitRegistry") -> re.Pattern[str] | None:
    """Thai "ย้อนหลัง[<habit>][<N>]" -- habit token built from the LIVE
    registry's ids/Thai labels, same false-positive mitigation as
    `_build_remind_th_pattern`/`_build_target_th_set_pattern` (only a
    message naming a habit this bot actually tracks, and/or a bare
    number, can ever match at all). Returns `None` if the registry has no
    matchable Thai/id tokens (defensive; every shipped config has at
    least water's "น้ำ") -- in that case only the bare form or a
    purely-numeric tail can match."""
    tokens: set[str] = set()
    for habit in registry:
        tokens.add(habit.id)
        if habit.label_th:
            tokens.add(habit.label_th)
    escaped = sorted((re.escape(t) for t in tokens if t.strip()), key=len, reverse=True)
    habit_group = rf"(?:\s+(?P<habit>{'|'.join(escaped)}))?" if escaped else ""
    return re.compile(rf"^ย้อนหลัง{habit_group}(?:\s+(?P<n>\d+))?$")


def _match_history(stripped: str, registry: "HabitRegistry") -> "Command | None":
    slash_match = _HISTORY_SLASH_RE.match(stripped)
    if slash_match is not None:
        category, limit = _parse_history_tail(slash_match.group("rest"), registry)
        return Command(kind="history", category=category, limit=limit)

    th_pattern = _build_history_th_pattern(registry)
    if th_pattern is not None:
        th_match = th_pattern.match(stripped)
        if th_match is not None:
            group_dict = th_match.groupdict()
            habit_raw = group_dict.get("habit")
            n_raw = group_dict.get("n")
            # The habit token came straight out of the registry-built
            # alternation above, so it is always resolvable -- the `or`
            # fallback is defensive only, never actually hit (mirrors
            # `_match_remind`'s own identical comment for its Thai path).
            category = (_resolve_habit_token(habit_raw, registry) or habit_raw.lower()) if habit_raw else None
            return Command(kind="history", category=category, limit=int(n_raw) if n_raw else None)

    return None


# ---------------------------------------------------------------------------
# heatmap -- SPEC-v1.6.md §4 R-H1 (module `heatmap`). `/heatmap [<habit>]
# [<weeks>]`, plus the Thai alias `ปฏิทิน` ("calendar"). Tail grammar
# MIRRORS `/history`'s exactly (SPEC-v1.6.md §2.1: "an optional registry
# habit id then an optional integer, whole-message-anchored, registry/
# numeric-gated") -- reuses `_parse_history_tail`'s identical (category, N)
# parsing rather than a second copy of the same logic; only the *meaning*
# of the trailing integer differs (a weeks count, not a row limit), which
# is `core/heatmap.py`'s own concern (default 12 / cap 52), not this
# shape-only layer's -- `Command.limit` carries the raw parsed int either
# way, same field-reuse skeleton comment already documents (§5).
#
# The Thai alias `ปฏิทิน` ("calendar") is an ordinary Thai word that can
# open real prose (e.g. "ปฏิทินจีนปีนี้..." -- "this year's Chinese
# calendar..."), same false-positive risk class as `เตือน`/`ภาษา`/`เงียบ`/
# `ย้อนหลัง`/`เช็คอิน` -- hardened from the start rather than shipped loose:
# anchored to the WHOLE stripped message, the optional tail built entirely
# from a REGISTRY-anchored habit token and/or a purely-numeric token,
# mirroring `_build_history_th_pattern`'s own precedent exactly (only a
# message naming a habit this bot actually tracks, and/or a bare number,
# can ever match at all).
# ---------------------------------------------------------------------------

_HEATMAP_SLASH_RE = re.compile(r"^/heatmap(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)


def _build_heatmap_th_pattern(registry: "HabitRegistry") -> re.Pattern[str] | None:
    """Thai "ปฏิทิน[<habit>][<weeks>]" -- habit token built from the LIVE
    registry's ids/Thai labels, same false-positive mitigation as
    `_build_history_th_pattern`/`_build_remind_th_pattern`. Returns `None`
    if the registry has no matchable Thai/id tokens (defensive; every
    shipped config has at least water's "น้ำ") -- in that case only the
    bare form or a purely-numeric tail can match."""
    tokens: set[str] = set()
    for habit in registry:
        tokens.add(habit.id)
        if habit.label_th:
            tokens.add(habit.label_th)
    escaped = sorted((re.escape(t) for t in tokens if t.strip()), key=len, reverse=True)
    habit_group = rf"(?:\s+(?P<habit>{'|'.join(escaped)}))?" if escaped else ""
    return re.compile(rf"^ปฏิทิน{habit_group}(?:\s+(?P<n>\d+))?$")


def _match_heatmap(stripped: str, registry: "HabitRegistry") -> "Command | None":
    slash_match = _HEATMAP_SLASH_RE.match(stripped)
    if slash_match is not None:
        category, weeks = _parse_history_tail(slash_match.group("rest"), registry)
        return Command(kind="heatmap", category=category, limit=weeks)

    th_pattern = _build_heatmap_th_pattern(registry)
    if th_pattern is not None:
        th_match = th_pattern.match(stripped)
        if th_match is not None:
            group_dict = th_match.groupdict()
            habit_raw = group_dict.get("habit")
            n_raw = group_dict.get("n")
            # The habit token came straight out of the registry-built
            # alternation above, so it is always resolvable -- the `or`
            # fallback is defensive only, never actually hit (mirrors
            # `_match_history`'s own identical comment for its Thai path).
            category = (_resolve_habit_token(habit_raw, registry) or habit_raw.lower()) if habit_raw else None
            return Command(kind="heatmap", category=category, limit=int(n_raw) if n_raw else None)

    return None


# ---------------------------------------------------------------------------
# lang / quiet -- SPEC-v1.2.md §4 R-P1/R-P2 (module `preferences`). Slash
# forms `/lang [en|th|auto]` and `/quiet [HH:MM-HH:MM[,...]|off]` (bare, no
# value, is a valid shape too -- `core/preferences.py`'s `execute_lang`/
# `execute_quiet` reply with a usage message rather than this layer
# silently returning None). The slash forms stay fully permissive (any
# non-empty tail) -- an explicit "/" prefix is a near-zero false-positive
# surface no normal sentence starts with, so `execute_lang`/`execute_quiet`
# alone are trusted to validate the value.
#
# v1.2.0 hardening, round 1 (TEST-v1.2-preferences.md): the Thai aliases
# `ภาษา`/`เงียบ` are ordinary Thai WORDS ("language"/"quiet") that
# legitimately open plenty of real sentences, unlike a "/"-prefixed slash
# command -- a mandatory single whitespace alone (the original mitigation)
# only protects against the trigger glued to more text with NO space; it
# does NOT protect against a legitimate space-separated continuation, e.g.
# the standard mai-yamok reduplication "เงียบ ๆ หน่อยนะ" ("keep it down,
# please") or a natural clause break like "ภาษา นี้ยากมาก" ("this language
# is hard"). Fixed by requiring the Thai-alias value group to be a single
# whitespace-free token (`\S+`, not `\S.*`) -- a multi-word continuation
# never matches the Thai-alias regex at all -- PLUS, for the remaining
# single-token case, a per-command plausibility check on the value.
#
# v1.2.0 hardening, round 2 (TEST-v1.2-preferences.md, Vera's follow-up
# audit): round 1's plausibility check for `ภาษา` was a curated BLACKLIST
# of common Thai discourse markers (reject the value if it contains one).
# That structurally cannot cover every ordinary Thai word -- 6 more
# realistic single-word messages ("ภาษา อังกฤษ"/"จีน"/"ใหม่"/"ดี"/"สวย"/
# "อะไร", e.g. "which language?" or "[my] language [is] good") contained
# none of the curated markers and still misfired. `ภาษา` now uses a
# WHITELIST instead, `_LANG_TH_VALID_VALUES` -- exactly the tokens that
# should dispatch: the valid value set SPEC-v1.2.md R-P1 defines
# (`en`/`th`/`auto`) plus the two REVIEWED near-miss language-name
# attempts `test_execute_lang_rejects_unsupported_codes_writes_nothing`
# requires to still reach `execute_lang`'s `lang_usage` nudge rather than
# vanish silently (`ไทย` -- the native Thai word for "Thai" -- and
# `english`). Everything else, including any other Thai language name in
# ordinary prose, now falls through to `None`. This mirrors `เงียบ`'s own
# strategy below (`_QUIET_TH_VALUE_RE`, a shape whitelist rather than a
# word blacklist) -- SPEC-v1.2.md §2.3's own zero-false-positive
# discipline prioritizes precision here: the deterministic `/lang en`
# always works regardless, and free-form NL language-switching was
# consciously deferred along with the rest of NL phrasing (§10), so a
# Thai word for a language name IN PROSE must not be treated as a
# command. `เงียบ`'s check was never a blacklist (a quiet-hours value has
# an unambiguous mechanical SHAPE -- digits/colons/hyphens/commas or the
# literal word "off" -- a language name doesn't), so it was unaffected by
# round 2 and needed no change.
#
# A tail that fails its guard falls through to `None` (the normal
# parser), never partially committing to a `lang_usage`/`quiet_invalid_
# window` reply. Neither the language code nor the quiet-hours windows
# are semantically validated here (SLASH form or Thai alias alike) -- the
# raw (lowercased) tail that DOES pass shape is carried through verbatim
# as `Command.pref_value`; `core/preferences.py` is where semantic
# validation, the `users` table write, and the bilingual reply happen
# (R-T7's recognize-shape/execute split, reused verbatim by every v1.1/
# v1.2 settings-style command above).
# ---------------------------------------------------------------------------

_LANG_SLASH_RE = re.compile(r"^/lang(?:\s+(?P<value>\S.*))?$", re.IGNORECASE)
_LANG_TH_RE = re.compile(r"^ภาษา\s+(?P<value>\S+)$")
_QUIET_SLASH_RE = re.compile(r"^/quiet(?:\s+(?P<value>\S.*))?$", re.IGNORECASE)
_QUIET_TH_RE = re.compile(r"^เงียบ\s+(?P<value>\S+)$")

# The ONLY values the Thai alias `ภาษา` may dispatch on (round 2 above):
# the spec's own valid value set, plus the two reviewed near-miss names.
# Comparison is case-insensitive (the caller lowercases first) -- matches
# `_LANG_SLASH_RE`'s own `re.IGNORECASE`/`.lower()` posture.
_LANG_TH_VALID_VALUES = {"en", "th", "auto", "ไทย", "english"}

# A loose SHAPE check only (not full HH:MM range validation -- that stays
# `execute_quiet`'s job via `_HHMM_RE`) so a shape-plausible but
# out-of-range Thai-alias attempt (e.g. "เงียบ 25:99-07:00") still reaches
# `execute_quiet`'s own `quiet_invalid_window` reply rather than silently
# vanishing -- consistent with the slash form's own permissiveness.
_QUIET_TH_VALUE_RE = re.compile(r"^(off|\d{1,2}:\d{2}-\d{1,2}:\d{2}(,\d{1,2}:\d{2}-\d{1,2}:\d{2})*)$", re.IGNORECASE)


def _match_lang(stripped: str) -> "Command | None":
    slash_match = _LANG_SLASH_RE.match(stripped)
    if slash_match is not None:
        value = slash_match.group("value")
        return Command(kind="lang", pref_value=value.strip().lower() if value else None)

    th_match = _LANG_TH_RE.match(stripped)
    if th_match is None:
        return None
    value = th_match.group("value").lower()
    if value not in _LANG_TH_VALID_VALUES:
        return None
    return Command(kind="lang", pref_value=value)


def _match_quiet(stripped: str) -> "Command | None":
    slash_match = _QUIET_SLASH_RE.match(stripped)
    if slash_match is not None:
        value = slash_match.group("value")
        return Command(kind="quiet", pref_value=value.strip().lower() if value else None)

    th_match = _QUIET_TH_RE.match(stripped)
    if th_match is None:
        return None
    value = th_match.group("value")
    if _QUIET_TH_VALUE_RE.match(value) is None:
        return None
    return Command(kind="quiet", pref_value=value.lower())


# ---------------------------------------------------------------------------
# checkin / dnd -- SPEC-v1.5.md §4 R-K8/R-D5 (module `checkins`). `/checkin`
# is a new deterministic, LLM-free per-user setter (on/off/default/HH:MM-
# HH:MM window, bare = show, R-K8) plus its Thai alias `เช็คอิน`. `/dnd`
# (Thai `งดรบกวน`) is a PURE alias of `/quiet` -- it produces the exact same
# `Command(kind="quiet", pref_value=...)` shape `_match_quiet` above already
# does (R-D5: same storage, same `preferences.execute_quiet`), so no new
# `Command` field or `CommandKind` value is needed for it at all -- only one
# more recognizer feeding the SAME "quiet" kind.
#
# `/checkin`'s slash form stays fully permissive, mirroring every other
# settings-style slash command above (`/lang`/`/quiet`/`/remind`/`/target`):
# an explicit "/" prefix is a near-zero false-positive surface, so any tail,
# however malformed, still produces a Command -- `core/checkins.
# execute_checkin` is where a bad window becomes a friendly usage reply, not
# this shape-only layer. A bare `/checkin` (no tail) is deliberately NOT a
# usage error here -- R-K8's own grammar gives it a distinct meaning ("empty
# = show"), unlike `/lang`/`/quiet`'s bare form.
#
# The Thai alias `เช็คอิน` (a common transliterated loanword -- a hotel/
# flight/social-media "check-in") carries the same false-positive risk class
# already hardened for `เตือน`/`ภาษา`/`เงียบ`/`ย้อนหลัง` above: it can open
# ordinary prose. Anchored to the WHOLE stripped message (a glued
# continuation like "เช็คอินแล้ว" -- no space -- never matches at all, the
# anchor alone rules it out) PLUS, when a tail IS present after a space, it
# must have the exact valid-argument SHAPE (`on`/`off`/`default`, or an
# `HH:MM-HH:MM` window) -- an ordinary spaced continuation ("เช็คอิน
# ร้านอาหารอร่อยมาก") still falls through to `None`. A BARE "เช็คอิน" (the
# whole message, nothing else) DOES match, same "show" meaning as the slash
# form -- mirrors `ย้อนหลัง`'s own established precedent (`_build_history_
# th_pattern`, above) of a bare match on a common word being acceptable
# when the grammar itself defines a meaning for the empty-tail case, rather
# than `เตือน`'s stricter "always requires a real habit token" gate (checkin
# has no habit token to anchor on).
#
# `/dnd`'s Thai alias `งดรบกวน` mirrors `เงียบ`'s own mandatory-value shape
# (`_QUIET_TH_VALUE_RE`, reused verbatim here) -- no bare-match meaning
# exists for `/dnd` any more than it does for `/quiet` itself (both are
# usage-reply-on-bare, not show-on-bare), so the Thai alias requires a
# value just like `เงียบ` does.
# ---------------------------------------------------------------------------

_CHECKIN_SLASH_RE = re.compile(r"^/checkin(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)
_CHECKIN_TH_RE = re.compile(r"^เช็คอิน(?:\s+(?P<rest>\S.*))?$")
_CHECKIN_TAIL_WORDS = {"on", "off", "default"}
_CHECKIN_WINDOW_SHAPE_RE = re.compile(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$")

_DND_SLASH_RE = re.compile(r"^/dnd(?:\s+(?P<value>\S.*))?$", re.IGNORECASE)
_DND_TH_RE = re.compile(r"^งดรบกวน\s+(?P<value>\S+)$")


def _checkin_tail_has_valid_shape(tail: str) -> bool:
    """Gate for the Thai bare-word trigger's OWN tail (see the block
    comment above) -- `on`/`off`/`default`, or an `HH:MM-HH:MM`-SHAPED
    token (real HH:MM range validation is still `core/checkins.
    execute_checkin`'s job, mirrors `_remind_tail_has_valid_shape`'s
    identical recognize-shape/validate split)."""
    lowered = tail.strip().lower()
    if lowered in _CHECKIN_TAIL_WORDS:
        return True
    return _CHECKIN_WINDOW_SHAPE_RE.match(lowered) is not None


def _match_checkin(stripped: str) -> "Command | None":
    slash_match = _CHECKIN_SLASH_RE.match(stripped)
    if slash_match is not None:
        rest = slash_match.group("rest")
        return Command(kind="checkin", pref_value=rest.strip().lower() if rest else None)

    th_match = _CHECKIN_TH_RE.match(stripped)
    if th_match is None:
        return None
    rest = th_match.group("rest")
    if rest is None:
        return Command(kind="checkin", pref_value=None)  # bare -> show (R-K8)
    tail = rest.strip()
    if not _checkin_tail_has_valid_shape(tail):
        return None
    return Command(kind="checkin", pref_value=tail.lower())


def _match_dnd(stripped: str) -> "Command | None":
    """R-D5: a pure alias of `/quiet` -- produces the SAME `Command(kind=
    "quiet", ...)` shape `_match_quiet` does, so it routes to the exact
    same `preferences.execute_quiet`/`quiet_hours_json` storage with zero
    new plumbing."""
    slash_match = _DND_SLASH_RE.match(stripped)
    if slash_match is not None:
        value = slash_match.group("value")
        return Command(kind="quiet", pref_value=value.strip().lower() if value else None)

    th_match = _DND_TH_RE.match(stripped)
    if th_match is None:
        return None
    value = th_match.group("value")
    if _QUIET_TH_VALUE_RE.match(value) is None:
        return None
    return Command(kind="quiet", pref_value=value.lower())


# ---------------------------------------------------------------------------
# dashboard -- SPEC-v1.6.md §4 Feature 1 R-D1 (module `dashboard`). `/dashboard
# on|off`, bare `/dashboard` = show (R-D1's own "empty = show" grammar, NOT a
# usage error -- same convention as `/checkin`'s bare form), plus the Thai
# alias `แดชบอร์ด`. `Command.pref_value` carries the lowercased tail
# ("on"/"off", or `None` for bare) exactly like `/checkin` does -- no new
# `Command` field needed (see the CommandKind skeleton comment above).
#
# The Thai alias `แดชบอร์ด` is a transliterated loanword ("dashboard", as in a
# car dashboard or a data dashboard) that can open ordinary prose, the same
# false-positive risk class already hardened for `เช็คอิน`/`เตือน`/`ภาษา`/
# `เงียบ`/`ย้อนหลัง` above -- so it gets the identical treatment `เช็คอิน`
# already established: anchored to the WHOLE stripped message (a glued
# continuation never matches at all), and when a tail IS present after a
# space, it must be exactly `on`/`off` (dashboard's own grammar is strictly
# binary -- no `default`/window shape to accept, unlike `/checkin`) or the
# match is rejected and falls through to `None`. A bare "แดชบอร์ด" (the whole
# message, nothing else) DOES match, same "show" meaning as the slash form --
# mirrors `เช็คอิน`'s own precedent of a bare match on a common word being
# acceptable when the grammar itself defines a meaning for the empty-tail
# case.
# ---------------------------------------------------------------------------

_DASHBOARD_SLASH_RE = re.compile(r"^/dashboard(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)
_DASHBOARD_TH_RE = re.compile(r"^แดชบอร์ด(?:\s+(?P<rest>\S.*))?$")
_DASHBOARD_TAIL_WORDS = {"on", "off"}


def _match_dashboard(stripped: str) -> "Command | None":
    slash_match = _DASHBOARD_SLASH_RE.match(stripped)
    if slash_match is not None:
        rest = slash_match.group("rest")
        return Command(kind="dashboard", pref_value=rest.strip().lower() if rest else None)

    th_match = _DASHBOARD_TH_RE.match(stripped)
    if th_match is None:
        return None
    rest = th_match.group("rest")
    if rest is None:
        return Command(kind="dashboard", pref_value=None)  # bare -> show (R-D1)
    tail = rest.strip().lower()
    if tail not in _DASHBOARD_TAIL_WORDS:
        return None
    return Command(kind="dashboard", pref_value=tail)


# ---------------------------------------------------------------------------
# records / trends -- SPEC-v1.6.md §4 Feature 3/4 (module `insights`),
# R-R3/R-T2. `/records [<habit>]`/`สถิติ [<habit>]` and `/trends
# [<habit>]`/`แนวโน้ม [<habit>]` share the exact tail grammar `/history`
# already established (SPEC-v1.6.md §2.1: "an optional registry habit id
# then an optional integer, whole-message-anchored, registry/numeric-
# gated") -- reuses `_parse_history_tail` as-is for the slash form (habit-
# filter + trailing-int parsing is identical), but the parsed int is
# always discarded: SPEC-v1.6.md §5's own skeleton comment on `Command.
# limit` states only "heatmap" uses it -- a trailing digit ("/records
# water 8") is silently ignored rather than rejected, mirroring
# `/history`'s own "any token beyond the first two is ignored" leniency
# for the slash form.
#
# The Thai aliases สถิติ ("statistics"/records) and แนวโน้ม ("trend(s)") are,
# like ประวัติ/ย้อนหลัง/เช็คอิน/แดชบอร์ด before them, ordinary Thai words (or
# loanwords) that can open real prose -- hardened the SAME way
# `_build_history_th_pattern` was (see its own comment for the full
# false-positive rationale): the habit token, when present, must resolve
# to a REAL configured habit via a registry-built alternation, and any
# trailing tail is digits-only, with the whole match anchored end-to-end
# -- neither trigger word can ever partially match an unrelated sentence.
# `_build_insights_th_pattern` below is a small, parameterized
# generalization of `_build_history_th_pattern`'s own construction
# (shared here because `records`/`trends` are literally the SAME
# `insights` module, unlike `history`, which is a separate,
# independently-landed module -- not worth a third near-identical
# copy-paste for two kinds owned by the same file section).
# ---------------------------------------------------------------------------

_RECORDS_SLASH_RE = re.compile(r"^/records(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)
_TRENDS_SLASH_RE = re.compile(r"^/trends(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)


def _build_insights_th_pattern(trigger: str, registry: "HabitRegistry") -> re.Pattern[str] | None:
    """Registry-anchored Thai-alias builder shared by `_match_records`/
    `_match_trends` -- see the comment block above for the false-positive
    rationale (mirrors `_build_history_th_pattern`'s own construction)."""
    tokens: set[str] = set()
    for habit in registry:
        tokens.add(habit.id)
        if habit.label_th:
            tokens.add(habit.label_th)
    escaped = sorted((re.escape(t) for t in tokens if t.strip()), key=len, reverse=True)
    habit_group = rf"(?:\s+(?P<habit>{'|'.join(escaped)}))?" if escaped else ""
    return re.compile(rf"^{trigger}{habit_group}(?:\s+(?P<n>\d+))?$")


def _match_insights_kind(
    stripped: str, registry: "HabitRegistry", *, kind: "CommandKind", slash_re: re.Pattern[str], th_trigger: str
) -> "Command | None":
    slash_match = slash_re.match(stripped)
    if slash_match is not None:
        category, _limit = _parse_history_tail(slash_match.group("rest"), registry)
        return Command(kind=kind, category=category)

    th_pattern = _build_insights_th_pattern(th_trigger, registry)
    if th_pattern is not None:
        th_match = th_pattern.match(stripped)
        if th_match is not None:
            habit_raw = th_match.groupdict().get("habit")
            # The habit token came straight out of the registry-built
            # alternation above, so it is always resolvable -- the `or`
            # fallback is defensive only, never actually hit (mirrors
            # `_match_history`'s own identical comment for its Thai path).
            category = (_resolve_habit_token(habit_raw, registry) or habit_raw.lower()) if habit_raw else None
            return Command(kind=kind, category=category)

    return None


def _match_records(stripped: str, registry: "HabitRegistry") -> "Command | None":
    return _match_insights_kind(stripped, registry, kind="records", slash_re=_RECORDS_SLASH_RE, th_trigger="สถิติ")


def _match_trends(stripped: str, registry: "HabitRegistry") -> "Command | None":
    return _match_insights_kind(stripped, registry, kind="trends", slash_re=_TRENDS_SLASH_RE, th_trigger="แนวโน้ม")


# ---------------------------------------------------------------------------
# addhabit / delhabit -- SPEC-v1.7.md §4 (module `habitdef`). `/addhabit
# <pipe key=value grammar>` (Thai alias `เพิ่มนิสัย`) and `/delhabit <id>`
# (Thai alias `ลบนิสัย`).
#
# `/addhabit`'s slash form stays fully permissive, mirroring every other
# settings-style slash command above -- an explicit "/" prefix is a
# near-zero false-positive surface, so even a malformed/empty tail still
# produces a `Command(kind="addhabit", fields=...)`, letting `core/
# habitdef.py:execute_addhabit` reply with the usage/example message
# rather than this shape-only layer silently swallowing it.
#
# The Thai alias `เพิ่มนิสัย` (literally "add habit") IS an ordinary
# compound that could plausibly open real prose ("อยากเพิ่มนิสัยที่ดี" --
# "[I] want to add good habits"), the same false-positive risk class
# `เตือน`/`ภาษา`/`เงียบ`/`เช็คอิน`/`แดชบอร์ด` were each hardened against.
# Registry-anchoring (the usual mitigation for THOSE triggers) doesn't
# apply here -- there's no existing habit to anchor a CREATE command's
# argument against. Instead this uses the equivalent strategy `เงียบ`'s
# own `_QUIET_TH_VALUE_RE` established: a strict GRAMMAR-SHAPE whitelist
# (every `|`-separated segment must contain a bare `key=`) rather than a
# curated word blacklist -- ordinary Thai prose about habits has no `=`
# signs in it at all, so it can never satisfy this shape, while the
# spec's own pipe grammar always does. `_parse_addhabit_fields` is both
# the shape gate AND the actual field extraction (one pass, not two) --
# a `None` result means "not key=value-shaped", used here to reject the
# Thai-alias match entirely (falls through to `None`, ordinary prose);
# the SAME `None` result for the SLASH form instead becomes a usage-reply
# `Command` (permissive posture, see above) rather than a non-match.
#
# `/delhabit`'s slash form is likewise permissive (any first token becomes
# the raw, unresolved `category` -- mirrors `/target`'s/`/history`'s own
# "recognized shape -> always a Command" rule; an unresolved id is
# `execute_delhabit`'s own `delhabit_not_found` reply, not a dispatch
# failure). The Thai alias `ลบนิสัย` ("delete habit") IS registry-anchored
# (mirrors `_build_remind_th_pattern`/`_build_history_th_pattern` exactly)
# -- a habit slated for deletion, unlike one being created, DOES already
# exist in the registry the caller passes in (base id, or the user's own
# already-created custom habit), so only a message naming a habit this
# bot actually tracks for this user can ever match at all.
# ---------------------------------------------------------------------------

_ADDHABIT_SLASH_RE = re.compile(r"^/addhabit(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)
_ADDHABIT_TH_RE = re.compile(r"^เพิ่มนิสัย(?:\s+(?P<rest>\S.*))?$")

_DELHABIT_SLASH_RE = re.compile(r"^/delhabit(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)


def _parse_addhabit_fields(rest: str) -> dict[str, str] | None:
    """SPEC-v1.7.md §2.1's pipe `key=value` grammar -- SHAPE-only parsing
    (mirrors `_parse_edit_value`'s/`_parse_target_value`'s own recognize-
    shape/validate-elsewhere split): every `|`-separated segment must
    contain a `=`, the substring before it (stripped, lowercased) is the
    key, everything after (stripped, kept verbatim otherwise -- a label/
    unit value can be Thai text or mixed case, never lowercased here) is
    the raw value. An empty segment (e.g. a stray leading/trailing `|`) is
    skipped. Returns `None` if ANY non-empty segment lacks a `=`, or the
    tail has no segments at all -- `core/habitdef.py:validate_and_
    normalize`'s own caller replies with the usage message for a `None`
    result (SLASH form), or the Thai-alias trigger doesn't match at all
    (see the module comment above)."""
    fields: dict[str, str] = {}
    for segment in rest.split("|"):
        segment = segment.strip()
        if not segment:
            continue
        key, sep, value = segment.partition("=")
        if not sep:
            return None
        key = key.strip().lower()
        if not key:
            return None
        fields[key] = value.strip()
    return fields if fields else None


def _match_addhabit(stripped: str) -> "Command | None":
    slash_match = _ADDHABIT_SLASH_RE.match(stripped)
    if slash_match is not None:
        rest = slash_match.group("rest")
        return Command(kind="addhabit", fields=_parse_addhabit_fields(rest) if rest else None)

    th_match = _ADDHABIT_TH_RE.match(stripped)
    if th_match is None:
        return None
    rest = th_match.group("rest")
    if rest is None:
        return None  # bare "เพิ่มนิสัย" -- no argument shape to whitelist against, ordinary prose.
    fields = _parse_addhabit_fields(rest)
    if fields is None:
        return None  # tail isn't key=value-pipe-shaped -- ordinary prose (see module comment above).
    return Command(kind="addhabit", fields=fields)


def _build_delhabit_th_pattern(registry: "HabitRegistry") -> re.Pattern[str] | None:
    """Thai "ลบนิสัย<habit>" -- habit token built from the LIVE registry's
    ids/Thai labels, same false-positive mitigation as `_build_remind_th_
    pattern`/`_build_history_th_pattern` (only a message naming a habit
    this bot actually tracks for this user -- a base id, or one of their
    own already-created custom habits, both present in `registry` by the
    time this dispatches -- can ever match at all). Returns `None` if the
    registry has no matchable Thai/id tokens (defensive; every shipped
    config has at least water's "น้ำ")."""
    tokens: set[str] = set()
    for habit in registry:
        tokens.add(habit.id)
        if habit.label_th:
            tokens.add(habit.label_th)
    escaped = sorted((re.escape(t) for t in tokens if t.strip()), key=len, reverse=True)
    if not escaped:
        return None
    habit_alt = "|".join(escaped)
    return re.compile(rf"^ลบนิสัย\s+(?P<habit>{habit_alt})$")


def _match_delhabit(stripped: str, registry: "HabitRegistry") -> "Command | None":
    slash_match = _DELHABIT_SLASH_RE.match(stripped)
    if slash_match is not None:
        habit_token = _first_token(slash_match.group("rest"))
        category = habit_token.strip().lower() if habit_token else None
        return Command(kind="delhabit", category=category)

    th_pattern = _build_delhabit_th_pattern(registry)
    if th_pattern is not None:
        th_match = th_pattern.match(stripped)
        if th_match is not None:
            # The habit token came straight out of the registry-built
            # alternation above, so it is always resolvable -- the `or`
            # fallback is defensive only, never actually hit (mirrors
            # `_match_history`'s/`_match_remind`'s own identical comment).
            category = _resolve_habit_token(th_match.group("habit"), registry) or th_match.group("habit").lower()
            return Command(kind="delhabit", category=category)

    return None


# ---------------------------------------------------------------------------
# query intent -- ROADMAP.md v0.8.0 (AC8.1-AC8.5). Anchored, conservative
# interrogative markers only: none of these substrings/endings can occur in
# a normal habit log (verified against the full adversarial corpus in
# tests/test_commands.py). The actual {habit_id, metric, timeframe}
# classification happens in core/query.py via the LLM -- this function only
# decides "does this look like a question about past data at all".
# ---------------------------------------------------------------------------

_QUERY_PATTERNS = [
    re.compile(r"\bhow\s+(much|many)\b", re.IGNORECASE),
    re.compile(r"\b(did|have|has)\s+i\b", re.IGNORECASE),
    re.compile("กี่"),
    re.compile("เท่าไหร่|เท่าไร"),
    re.compile("ไหม"),
    re.compile("หรือยัง"),
]
_TRAILING_QUESTION_MARK_RE = re.compile(r"[?？]\s*$")


def _match_query(stripped: str) -> bool:
    if _TRAILING_QUESTION_MARK_RE.search(stripped):
        return True
    return any(pattern.search(stripped) for pattern in _QUERY_PATTERNS)


def _match_undo(stripped: str) -> bool:
    return any(pattern.match(stripped) for pattern in _UNDO_PATTERNS)


# SPEC-v1.5.md §4 R-L5: aliases onto `core/units.py`'s own public
# functions (see this file's own `_VALUE_RE` comment above for the full
# extraction rationale) -- every call site below (`_parse_target_value`,
# `_parse_edit_value`) is unchanged.
_build_unit_lookup = units.build_unit_lookup
_resolve_unit = units.resolve_unit


def _default_numeric_habit(registry: "HabitRegistry") -> str | None:
    """A bare number with no unit at all defaults to the first
    numeric/duration habit in registry order (generalizes v0.6.0's
    hardcoded "no unit -> water" default; water is first in the shipped
    registry order, so this reproduces that behavior exactly for the
    default config)."""
    for habit in registry:
        if habit.type in ("numeric", "duration"):
            return habit.id
    return None


def _parse_edit_value(value_str: str, registry: "HabitRegistry") -> tuple[str, float] | None:
    """Parse the text after an edit trigger into (habit_id, new_value).
    Returns None if it doesn't cleanly parse as a positive NUMBER [+ UNIT]
    resolvable to a configured habit -- the caller treats that as "not
    actually a command" (AC5.5's conservatism applies to edit targets
    too)."""
    match = _VALUE_RE.match(value_str.strip())
    if not match:
        return None
    num = float(match.group("num"))
    if num <= 0:
        return None
    unit_raw = match.group("unit")

    if unit_raw is None:
        habit_id = _default_numeric_habit(registry)
        if habit_id is None:
            return None
        return habit_id, num

    resolved = _resolve_unit(_build_unit_lookup(registry), unit_raw.lower())
    if resolved is None:
        return None
    habit_id, multiplier = resolved
    return habit_id, num * multiplier


def dispatch(text: str, registry: "HabitRegistry") -> Command | None:
    """Classify `text` as an explicit command, or return None to fall
    through to the LLM parser (AC5.5: normal habit messages like "500ml"
    or "ดื่มน้ำ 2 แก้ว" must route unchanged -- zero false positives).

    SPEC-v0.7.md §4 R14 / AC12: edit values resolve to a habit id via
    `registry` (its configured `unit`/`unit_aliases`), not a hardcoded
    water/stretch check.

    ROADMAP.md v0.9.0 / SPEC-v1.1.md §4 R-T7/R-D1: checked in order undo ->
    edit -> snooze -> target -> help -> habits -> query -> (fall through to
    the parser), matching this version's own required routing ("undo/edit ->
    snooze -> target -> help/habits -> query -> extractor"). SPEC-v1.2.md §4
    R-S5 (module `schedules`) inserts one more anchored, LLM-free check --
    `remind` -- right after `target` and before `help`/`habits`: both are
    deterministic "settings"-style slash commands with disjoint trigger
    text (`/remind`/`เตือน` never overlaps `/target`'s own triggers or any
    help/habits/query anchor), so exact placement relative to them doesn't
    change behavior -- it's grouped with `target` for readability. SPEC-v1.2.md
    §4 R-P1/R-P2 (module `preferences`) likewise inserts `lang`/`quiet`
    (`/lang`/`ภาษา`, `/quiet`/`เงียบ`) right after `access`'s admin block and
    before `help`/`habits` -- same disjoint-trigger reasoning, so exact
    placement doesn't change behavior either. SPEC-v1.3.md §4 R-V1 (module
    `audit-view`) inserts one more, `audit` (`/audit`/`ประวัติ`), right
    after `access`'s admin block too -- same disjoint-trigger reasoning.
    An edit-trigger
    phrase whose tail doesn't parse as NUMBER [+ UNIT] returns None
    immediately (pre-v0.8 behavior, unchanged) rather than also being
    offered to the snooze/target/help/habits/query matchers -- it already
    committed to "edit" shape, not any of those."""
    stripped = text.strip()
    if not stripped:
        return None

    if _match_undo(stripped):
        return Command(kind="undo")

    trigger_match = _EDIT_TRIGGER.match(stripped)
    if trigger_match is not None:
        parsed = _parse_edit_value(trigger_match.group("value"), registry)
        if parsed is None:
            return None
        category, value_num = parsed
        return Command(kind="edit", category=category, value_num=value_num)

    snoozed, minutes = _match_snooze(stripped)
    if snoozed:
        return Command(kind="snooze", minutes=minutes)

    target_command = _match_target(stripped, registry)
    if target_command is not None:
        return target_command

    remind_command = _match_remind(stripped, registry)
    if remind_command is not None:
        return remind_command

    # SPEC-v1.2.md §4 R-A1-R-A5 (module `access`): `/start`/`/approve`/
    # `/block`/`/users`/`/invite` -- disjoint literal trigger words from
    # every pattern above/below, so exact placement doesn't change
    # behavior; grouped here with the other v1.2 settings-style slash
    # commands for readability.
    access_command = _match_access(stripped)
    if access_command is not None:
        return access_command

    # SPEC-v1.3.md §4 R-V1 (module `audit-view`): `/audit`/`ประวัติ` --
    # disjoint trigger text from every pattern above/below, grouped here
    # with `access`'s other owner-only admin commands for readability
    # (same rationale as `remind`/`access`/`lang`/`quiet` above).
    audit_command = _match_audit(stripped)
    if audit_command is not None:
        return audit_command

    # SPEC-v1.2.md §4 R-P1/R-P2 (module `preferences`): `/lang`/`ภาษา` and
    # `/quiet`/`เงียบ` -- disjoint trigger text from every pattern above/
    # below, grouped here with the other v1.2 settings-style slash commands
    # for readability (same rationale as `remind`/`access` above).
    lang_command = _match_lang(stripped)
    if lang_command is not None:
        return lang_command

    quiet_command = _match_quiet(stripped)
    if quiet_command is not None:
        return quiet_command

    # SPEC-v1.5.md §4 R-K8/R-D5 (module `checkins`): `/checkin`/`เช็คอิน`
    # and `/dnd`/`งดรบกวน` (a pure "quiet"-kind alias) -- disjoint trigger
    # text from every pattern above/below, grouped here with the other
    # settings-style slash commands for readability (same rationale as
    # `remind`/`access`/`lang`/`quiet` above).
    checkin_command = _match_checkin(stripped)
    if checkin_command is not None:
        return checkin_command

    dnd_command = _match_dnd(stripped)
    if dnd_command is not None:
        return dnd_command

    # SPEC-v1.6.md §4 Feature 1 R-D1 (module `dashboard`): `/dashboard`/
    # `แดชบอร์ด` -- disjoint trigger text from every pattern above/below,
    # grouped here with the other settings-style slash commands for
    # readability (same rationale as `remind`/`access`/`lang`/`quiet`/
    # `checkin` above).
    dashboard_command = _match_dashboard(stripped)
    if dashboard_command is not None:
        return dashboard_command

    # SPEC-v1.4.md §4 R-D2 (module `history`): `/history`/`ย้อนหลัง` --
    # disjoint trigger text from every pattern above/below (and explicitly
    # does NOT collide with `/audit`'s own `ประวัติ`, AC-5), grouped here
    # with the other settings/view-style slash commands for readability.
    history_command = _match_history(stripped, registry)
    if history_command is not None:
        return history_command

    # SPEC-v1.6.md §4 R-H1 (module `heatmap`): `/heatmap`/`ปฏิทิน` --
    # disjoint trigger text from every pattern above/below, grouped here
    # with `history`'s own tail-grammar precedent for readability (exact
    # placement doesn't change behavior).
    heatmap_command = _match_heatmap(stripped, registry)
    if heatmap_command is not None:
        return heatmap_command

    # SPEC-v1.6.md §4 Feature 3/4 (module `insights`): `/records`/`สถิติ`
    # and `/trends`/`แนวโน้ม` -- disjoint trigger text from every pattern
    # above/below, grouped here with `/history`'s/`/heatmap`'s own
    # view-style commands for readability (exact placement doesn't change
    # behavior).
    records_command = _match_records(stripped, registry)
    if records_command is not None:
        return records_command

    trends_command = _match_trends(stripped, registry)
    if trends_command is not None:
        return trends_command

    # SPEC-v1.7.md §4 (module `habitdef`): `/addhabit`/`เพิ่มนิสัย` and
    # `/delhabit`/`ลบนิสัย` -- disjoint trigger text from every pattern
    # above/below, grouped here with `/history`'s/`/heatmap`'s/`/records`'s/
    # `/trends`'s own view/definition-style commands for readability (exact
    # placement doesn't change behavior).
    addhabit_command = _match_addhabit(stripped)
    if addhabit_command is not None:
        return addhabit_command

    delhabit_command = _match_delhabit(stripped, registry)
    if delhabit_command is not None:
        return delhabit_command

    if _match_help(stripped):
        return Command(kind="help")

    if _match_habits(stripped):
        return Command(kind="habits")

    if _match_query(stripped):
        return Command(kind="query")

    return None


# ---------------------------------------------------------------------------
# SPEC-v1.7.md §4 R-V3 (shared surface, consumed by module `habitdef`):
# the single authoritative source of "every deterministic command trigger
# word, both languages" -- a custom habit's id/label (en or th) equal
# (case-insensitively, stripped -- `habitdef`'s own comparison, this
# function just returns the raw lowercase/Thai literals) to any word in
# this set is rejected (AC-H3), so a habit named "help"/"เตือน" can never
# exist and shadow real dispatch.
#
# Every entry below is copied verbatim from the literal already embedded
# in that trigger's own compiled pattern above in this file (cited per
# group) -- not re-derived from i18n copy or guessed -- satisfying R-V3's
# "same literals the command matchers use" requirement. `tests/test_
# commands.py`'s own cross-check (added alongside this function) verifies
# every word below actually round-trips through `dispatch()` to its
# expected kind, so a future edit to any trigger regex that silently
# drops/renames a word breaks that test rather than silently opening a
# collision hole.
#
# Deliberately EXCLUDED:
#   - `_QUERY_PATTERNS` (query intent, line ~1321): these are substring-
#     matched interrogative PARTICLES ("กี่"/"เท่าไหร่"/"ไหม"/"หรือยัง", "how
#     much/many", "did/have/has i") plus a trailing "?"/"？" -- not a
#     single fixed trigger STEM a habit id could equal one-for-one the way
#     "help" collides with `/help`. R-V3's own enumeration (SPEC-v1.7.md
#     §4) does not list "query" among the stems either.
#   - Argument/tail VALUES, not trigger words: `_TARGET_CLEAR_WORDS`
#     ("default"/"reset"/"clear"/"ค่าเริ่มต้น") and the on/off/HH:MM-shaped
#     tails `_match_quiet`/`_match_checkin`/`_match_dashboard`/`_match_lang`
#     accept -- these are values typed AFTER a trigger, not the trigger
#     itself.
#   - `/start`/`/users`/`/approve`/`/block`/`/invite`'s own bare English
#     words ARE included below even though R-V3's illustrative parenthetical
#     doesn't name them -- they are still real, slash-anchored command
#     trigger stems (module `access`, lines ~740-744), and the surrounding
#     "…" in R-V3's own wording reads as "and so on", not an exhaustive
#     exclusion list. Excluding real command words here would leave a
#     collision hole the function's own purpose exists to close.
# ---------------------------------------------------------------------------


def reserved_trigger_words() -> frozenset[str]:
    """SPEC-v1.7.md R-V3: every deterministic command trigger stem, both
    languages -- see the module comment directly above for the full
    inclusion/exclusion rationale and the literal-source discipline."""
    return frozenset(
        {
            # undo/delete (_UNDO_PATTERNS, line ~336-340)
            "undo",
            "delete",
            "ยกเลิก",
            "ลบ",
            # edit-heads (_EDIT_TRIGGER, line ~349-354)
            "edit",
            "make",
            "change",
            "แก้",
            "แก้ไข",
            # snooze (_SNOOZE_EN_RE/_SNOOZE_TH_RE, line ~377-380)
            "snooze",
            "เลื่อน",
            # target (_TARGET_SLASH_RE/_TARGET_EN_SET_PATTERNS/
            # _build_target_th_set_pattern, line ~406-474)
            "target",
            "goal",
            "ตั้งเป้า",
            "เป้า",
            # remind (_REMIND_SLASH_RE/_build_remind_th_pattern, line ~623-674)
            "remind",
            "เตือน",
            # help / habits (_HELP_RE/_HABITS_RE, line ~712-713)
            "help",
            "habits",
            "ช่วยเหลือ",
            "วิธีใช้",
            "นิสัย",
            # access -- start/users/approve/block/invite (line ~740-744),
            # English slash-only, no Thai alias in this app
            "start",
            "users",
            "approve",
            "block",
            "invite",
            # audit (_AUDIT_SLASH_RE/_AUDIT_TH_RE, line ~778-783)
            "audit",
            "ประวัติ",
            # history (_HISTORY_SLASH_RE, line ~843; Thai trigger ย้อนหลัง)
            "history",
            "ย้อนหลัง",
            # heatmap (_HEATMAP_SLASH_RE, line ~935; Thai trigger ปฏิทิน)
            "heatmap",
            "ปฏิทิน",
            # lang (_LANG_SLASH_RE/_LANG_TH_RE, line ~1038-1039)
            "lang",
            "ภาษา",
            # quiet (_QUIET_SLASH_RE/_QUIET_TH_RE, line ~1040-1041)
            "quiet",
            "เงียบ",
            # checkin (_CHECKIN_SLASH_RE/_CHECKIN_TH_RE, line ~1129-1130)
            "checkin",
            "เช็คอิน",
            # dnd (_DND_SLASH_RE/_DND_TH_RE, line ~1134-1135)
            "dnd",
            "งดรบกวน",
            # dashboard (_DASHBOARD_SLASH_RE/_DASHBOARD_TH_RE, line ~1210-1211)
            "dashboard",
            "แดชบอร์ด",
            # records (_RECORDS_SLASH_RE + _match_records's own th_trigger,
            # line ~1263/1304-1305)
            "records",
            "สถิติ",
            # trends (_TRENDS_SLASH_RE + _match_trends's own th_trigger,
            # line ~1264/1308-1309)
            "trends",
            "แนวโน้ม",
            # addhabit/delhabit (module `habitdef`, matched by
            # `_match_addhabit`/`_match_delhabit` above -- SPEC-v1.7.md
            # §2.1's own literal example text; reserved here too so
            # `core/habitdef.py:validate_and_normalize`'s own id/label
            # check can't pick a word already claimed as a habit name)
            "addhabit",
            "delhabit",
            "เพิ่มนิสัย",
            "ลบนิสัย",
        }
    )
