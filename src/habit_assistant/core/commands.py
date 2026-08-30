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
    from collections.abc import Callable

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
    # SPEC-v1.8.md §5/§6/§11 (shared surface): two bare Literal entries for
    # the two parallel modules that build on this pass, same skeleton
    # convention as the v1.6.0 four-kind block above -- `dispatch()` itself
    # does not yet recognize either; each module adds only its own
    # `_match_*` matcher function (see the `# module quicklog` / `# module
    # routines` stub comments in SPEC-v1.8.md §5) + one `dispatch()`
    # branch, disjoint from one another. "log" (module `quicklog`, R-Q1):
    # `/log`/`บันทึก`'s own kind -- the quick-log inline keyboard has no
    # further parsed fields (the keyboard itself is built from the
    # registry, not from anything in the command's tail), so no new
    # `Command` field is needed. "routine" (module `routines`, R-R1-R-R5):
    # `/routine ...`'s own kind, covering create/list/run/delete -- module
    # `routines`' own later edit adds whatever `Command` fields its parser
    # needs.
    "log",
    "routine",
    # SPEC-v1.9.md §5/§6/§11 (shared surface): four bare Literal entries
    # for the four parallel modules that build on this pass, same skeleton
    # convention as the v1.6.0/v1.8.0 blocks above -- `dispatch()` itself
    # does not yet recognize any of them; each module adds only its own
    # `_match_*` matcher function + one `dispatch()` branch, disjoint from
    # the other three. `reserved_trigger_words()` below already reserves
    # every literal these future matchers will anchor on (SPEC-v1.9.md §5's
    # own "reserve stems" comment), so a custom habit named after any of
    # them is rejected today, and the reservation needs no edit once the
    # matchers themselves land.
    #
    # "cadence" (module `cadence`, R18): `/cadence <habit> <N>|off`, Thai
    # `ต่อสัปดาห์`/`กี่ครั้งต่อสัปดาห์`. Reuses `Command.category` (habit),
    # `value_num` (N), and `pref_value` (raw "off" tail) -- no new fields.
    "cadence",
    # "pause"/"resume" (module `pause`, R12-R13): `/pause [<habit>]
    # <Nd|until DATE>`, Thai `พัก`/`หยุดพัก`; `/resume [<habit>]`, Thai
    # `กลับมา`/`ต่อ`. Reuses `Command.category` (habit, or None = all) and
    # `pref_value` (the raw duration/until-token) -- no new fields.
    "pause",
    "resume",
    # "wrapped" (module `wrapped`, R21): `/wrapped [month]`, alias
    # `/recap [month]`, Thai `สรุปเดือน`/`การ์ดสรุป`. Reuses `Command.
    # pref_value` (the raw "month" token, or None = default last-4-weeks
    # window) -- no new fields.
    "wrapped",
    # SPEC-v1.10.md §4 R-SS8 (shared surface, functional 5 "/guide"): a
    # compact getting-started card, bilingual, no parsed payload of its own
    # (`Command(kind="guide")` is the whole of it, same shape as "help"/
    # "habits" above) -- `core/discoverability.py:build_guide_text` (module
    # `M2`) is where the actual card content lives, dispatched by
    # `core/routing.py`'s later integration pass.
    "guide",
    # SPEC-LINE.md §4 R-C4/§9 OQ4 (module C, branch `line-version`): the
    # `/digest on|off` opt-out toggle, Thai alias `สรุปรายวัน`. Reuses
    # `Command.pref_value` (the lowercased tail -- "on"/"off", or `None`
    # for a bare "/digest"/"สรุปรายวัน" -- same "empty = show" grammar as
    # "checkin"/"dashboard" above), no new field needed.
    "digest",
    # SPEC-LINE.md §4 R-C5 (module C's own flagged gap, built at
    # Integration -- IMPL-LINE-C.md "Known limitations"/AC25): a new
    # `/review` that renders the weekly review as a free REPLY (text +
    # up to a couple of chart images via module A's media-URL path) on
    # demand -- the on-demand substitute for the auto-pushed weekly
    # review R-C2 suppresses on LINE. English slash form only (mirrors
    # `/guide`'s own "no bare-word alias" posture); `digest_review_
    # ready_line`'s own copy already points users at the literal text
    # "/review" in both languages, so no Thai alias is introduced here.
    # No parsed payload of its own (`Command(kind="review")` is the
    # whole of it, same shape as "help"/"habits"/"guide" above).
    "review",
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
    # SPEC-v1.8.md §5/§6 (module `routines`, R-R1-R-R5): the parsed SHAPE
    # of a "routine" command -- which operation (`routine_action`), the
    # raw (stripped-but-NOT-yet-lowercased/length-checked) routine name
    # token (`routine_name` -- full R-R1 normalization is `core/
    # routines.py`'s own job, mirroring `_parse_addhabit_fields`'s own
    # "stripped-but-verbatim, caller normalizes" convention for `id=`),
    # and, for "create" only, the raw `[(habit_token, value_str), ...]`
    # item list (`routine_items`). `routine_action`/`routine_name` are
    # both `None` only for the bare "/routine" (list) form. `routine_items
    # = None` for a "create"-shaped command means the tail after "=" had
    # no comma/whitespace-SHAPED items at all -- `execute_routine` treats
    # that as a usage reply, not a dispatch failure, same convention
    # `fields=None` already establishes for "addhabit".
    routine_action: Literal["create", "list", "run", "delete"] | None = None
    routine_name: str | None = None
    routine_items: list[tuple[str, str]] | None = None


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


# SPEC-REFACTOR.md Stage 3 rule 12(d): the token-collection step common to
# every registry-anchored Thai-alias trigger builder in this file (target/
# remind/history/heatmap/records+trends/delhabit/cadence, 7 definitions
# below) -- every habit id + Thai label in `registry`, `re.escape`d and
# sorted longest-first (so a longer token can never be shadowed by a
# shorter alternation branch matching first). Extracted verbatim from what
# was previously 7 byte-identical inline copies; each builder's own
# trailing regex construction (trigger literal(s), group shape, whether a
# `None` registry falls through or is checked explicitly) is untouched.
def _registry_th_tokens(registry: "HabitRegistry") -> list[str]:
    tokens: set[str] = set()
    for habit in registry:
        tokens.add(habit.id)
        if habit.label_th:
            tokens.add(habit.label_th)
    return sorted((re.escape(t) for t in tokens if t.strip()), key=len, reverse=True)


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
    escaped = _registry_th_tokens(registry)
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
    escaped = _registry_th_tokens(registry)
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
# guide -- SPEC-v1.10.md §4 R-SS8 (shared surface, functional 5). Anchored
# to the WHOLE stripped message, exactly the two literal strings R-SS8
# names (`/guide`/`คู่มือ`) -- same zero-false-positive conservatism as
# `_HELP_RE`/`_HABITS_RE` just above, so this can never fire on a real
# habit log or on ordinary Thai prose.
# ---------------------------------------------------------------------------

_GUIDE_RE = re.compile(r"^(?:/guide|คู่มือ)$", re.IGNORECASE)


def _match_guide(stripped: str) -> bool:
    return _GUIDE_RE.match(stripped) is not None


# ---------------------------------------------------------------------------
# review -- SPEC-LINE.md §4 R-C5 (module C's own flagged gap, built at
# Integration). Anchored to the WHOLE stripped message, exactly the one
# literal string `/review` -- same zero-false-positive conservatism as
# `_GUIDE_RE`/`_HELP_RE`/`_HABITS_RE` just above. English slash form only;
# see the `CommandKind` skeleton entry above for why no Thai alias exists.
# ---------------------------------------------------------------------------

_REVIEW_RE = re.compile(r"^/review$", re.IGNORECASE)


def _match_review(stripped: str) -> bool:
    return _REVIEW_RE.match(stripped) is not None


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
    escaped = _registry_th_tokens(registry)
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
    escaped = _registry_th_tokens(registry)
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
# digest -- SPEC-LINE.md §4 R-C4/§9 OQ4 (module C, branch `line-version`).
# `/digest on|off`, bare `/digest` = show (same "empty = show" grammar as
# `/checkin`/`/dashboard` above), Thai alias `สรุปรายวัน`. Strictly binary
# (`on`/`off` only, no `default`/window shape -- there's nothing else to
# configure, unlike `/checkin`) -- `Command.pref_value` carries the
# lowercased tail, no new `Command` field needed.
#
# `สรุปรายวัน` ("daily summary") is an ordinary Thai compound that COULD open
# real prose (e.g. a report title), the same false-positive risk class
# `เช็คอิน`/`แดชบอร์ด` before it were hardened against -- so it gets the
# identical treatment: anchored to the WHOLE stripped message (a glued
# continuation never matches), and when a tail IS present after a space it
# must be exactly `on`/`off` or the match is rejected, falling through to
# `None`. A bare "สรุปรายวัน" (nothing else) DOES match, same "show" meaning
# as the slash form, mirroring `เช็คอิน`/`แดชบอร์ด`'s own precedent.
# ---------------------------------------------------------------------------

_DIGEST_SLASH_RE = re.compile(r"^/digest(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)
_DIGEST_TH_RE = re.compile(r"^สรุปรายวัน(?:\s+(?P<rest>\S.*))?$")
_DIGEST_TAIL_WORDS = {"on", "off"}


def _match_digest(stripped: str) -> "Command | None":
    slash_match = _DIGEST_SLASH_RE.match(stripped)
    if slash_match is not None:
        rest = slash_match.group("rest")
        return Command(kind="digest", pref_value=rest.strip().lower() if rest else None)

    th_match = _DIGEST_TH_RE.match(stripped)
    if th_match is None:
        return None
    rest = th_match.group("rest")
    if rest is None:
        return Command(kind="digest", pref_value=None)  # bare -> show
    tail = rest.strip().lower()
    if tail not in _DIGEST_TAIL_WORDS:
        return None
    return Command(kind="digest", pref_value=tail)


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
    escaped = _registry_th_tokens(registry)
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
# wrapped -- SPEC-v1.9.md §4 Rule 21 / §5 (module `wrapped`). `/wrapped
# [month]`, alias `/recap [month]` (both slash-anchored, fully permissive
# tail -- same "an explicit '/' prefix is a near-zero false-positive
# surface, so the view layer alone validates the tail" posture `/heatmap`/
# `/records`/`/trends` above already established), and two disjoint Thai
# triggers: `สรุปเดือน` ("month recap") ALWAYS means the calendar-month
# window -- its own name already says "month", so no tail is needed or
# read; `การ์ดสรุป` ("recap card") is the generic trigger, defaulting to
# the bare "last 4 weeks" window unless followed by an explicit "เดือน"/
# "month" tail token (mirrors the English `/wrapped month` shape). No
# habit token in this grammar at all (Rule 21: always the user's WHOLE
# registry) -- unlike `/heatmap`/`/records`/`/trends`, so neither Thai
# trigger needs the registry-anchored habit-token construction those use;
# both are whole-message-anchored instead (`^...$`), same discipline
# SPEC-v1.9.md §5 calls for on every new v1.9 Thai alias.
# ---------------------------------------------------------------------------

_WRAPPED_SLASH_RE = re.compile(r"^/(?:wrapped|recap)(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)
_WRAPPED_TH_RE = re.compile(r"^(?P<trigger>สรุปเดือน|การ์ดสรุป)(?:\s+(?P<rest>\S.*))?$")
_WRAPPED_MONTH_WORDS = {"month", "เดือน"}


def _parse_wrapped_tail(rest: str | None) -> str | None:
    """`None`/anything-not-"month" -> `None` (the default 4-week window);
    a first token of "month" (English, case-insensitive via the caller's
    `.lower()`) or "เดือน" (Thai) -> `"month"`. Trailing content beyond the
    first token is ignored, mirroring `_parse_history_tail`'s own
    documented leniency toward a tail this shape-only layer doesn't fully
    validate."""
    if rest is None:
        return None
    parts = rest.strip().split()
    if not parts:
        return None
    return "month" if parts[0].lower() in _WRAPPED_MONTH_WORDS else None


def _match_wrapped(stripped: str) -> "Command | None":
    slash_match = _WRAPPED_SLASH_RE.match(stripped)
    if slash_match is not None:
        return Command(kind="wrapped", pref_value=_parse_wrapped_tail(slash_match.group("rest")))

    th_match = _WRAPPED_TH_RE.match(stripped)
    if th_match is not None:
        if th_match.group("trigger") == "สรุปเดือน":
            return Command(kind="wrapped", pref_value="month")
        return Command(kind="wrapped", pref_value=_parse_wrapped_tail(th_match.group("rest")))

    return None


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
    escaped = _registry_th_tokens(registry)
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
# log -- SPEC-v1.8.md §4 R-Q1 (module `quicklog`). `/log`, Thai alias
# `บันทึก` -- pops the quick-log inline keyboard (built by `core/quicklog.
# build_keyboard`, this module's own job is only to recognize the trigger
# SHAPE). No further parsed fields (SPEC-v1.8.md §5's own skeleton comment
# on `CommandKind`'s "log" entry: "the keyboard itself is built from the
# registry, not from anything in the command's tail") -- `Command(kind=
# "log")` is the whole of it, mirroring `/help`'s/`/habits`' own bare-kind
# shape (SPEC-v1.1.md R-D1) rather than any of the settings-style commands'
# tail-parsing.
#
# CRITICAL false-positive hazard flagged by the coordinator: `บันทึก`
# ("log"/"save"/"record") is a common Thai word that legitimately opens
# ordinary diary-log prose -- e.g. "บันทึกไดอารี่ วันนี้เหนื่อย" ("[diary]
# entry: today I'm tired") MUST still be classified as a diary log, NOT
# this command. Unlike `เตือน`/`ภาษา`/`เงียบ`/`ย้อนหลัง`/`เช็คอิน`/
# `แดชบอร์ด`/`ปฏิทิน`/`สถิติ`/`แนวโน้ม`/`เพิ่มนิสัย`/`ลบนิสัย` above --
# every one of which was hardened via a REGISTRY-anchored habit-token
# alternation or a value-shape whitelist for whatever tail follows the
# trigger -- `/log`'s own grammar has no tail to anchor against at all
# (R-Q1: bare command only). So the mitigation here is the strictest one
# in this file: `_LOG_RE` matches ONLY the two EXACT, WHOLE stripped
# messages "/log" and "บันทึก" -- zero tail tolerance, not even a single
# trailing space's worth of extra text (unlike `/help`'s/`/habits`' own
# already-conservative whole-message-only patterns, which at least don't
# have this specific real-word collision risk to defend against). Any
# continuation at all -- "บันทึกไดอารี่...", "บันทึก 500 น้ำ", "บันทึกด้วยนะ"
# -- leaves characters unconsumed after the trigger, so the match fails
# and falls through to the normal parser exactly like an ordinary diary
# log always has (verified against an adversarial corpus in
# tests/test_quicklog.py, mirroring `tests/test_commands.py`'s own
# discipline for every other Thai alias above).
# ---------------------------------------------------------------------------

_LOG_RE = re.compile(r"^(?:/log|บันทึก)$", re.IGNORECASE)


def _match_log(stripped: str) -> "Command | None":
    if _LOG_RE.match(stripped) is not None:
        return Command(kind="log")
    return None


# ---------------------------------------------------------------------------
# routine -- SPEC-v1.8.md §4 R-R1-R-R5 (module `routines`). `/routine <name>
# = <habit> <val>[, ...]` (create), bare `/routine` (list), `/routine <name>`
# (run), `/routine delete <name>` (delete) -- plus the Thai alias `กิจวัตร`
# for create/run/delete (SPEC-v1.8.md §2.3's own "(Thai: กิจวัตร)"/"(Thai
# tail: ลบ)" annotations; notably NOT annotated on the bare-list line, so
# `กิจวัตร` alone carries no "list" meaning -- see below).
#
# The slash form stays fully permissive for delete/create/run (mirrors
# every other settings-style slash command above -- an explicit "/" prefix
# is a near-zero false-positive surface no normal sentence starts with),
# INCLUDING a malformed create tail (`routine_items=None`, letting `core/
# routines.py:execute_routine` reply with a usage message rather than this
# shape-only layer silently swallowing it) -- same "recognized shape here,
# semantic/validation there" split as `/addhabit`'s own `fields=None`
# convention. `routine_name` here is raw/stripped, NOT yet lowercased or
# length/charset-checked (R-R1's own `^[a-z0-9_]+$`/`<=32` normalization is
# `execute_routine`'s job, mirroring `_parse_addhabit_fields`'s own
# "caller normalizes `id=`" split) -- an invalid name still reaches
# `execute_routine`, which replies with the friendly `routine_invalid_name`
# error, never a silent non-match.
#
# The Thai alias `กิจวัตร` ("routine"/"daily practice") is, like `เพิ่มนิสัย`
# before it, an ordinary Thai compound that could plausibly open real prose
# ("กิจวัตรประจำวันของฉันคือ..." -- "my daily routine is..."). Unlike
# `ลบนิสัย`/`เตือน`/etc., there is no existing registry to anchor a routine
# NAME against (routine names are per-user, DB-only state this dispatch
# layer -- which only ever sees a `HabitRegistry` -- has no access to).
# Instead, this reuses R-R1's OWN id-shape constraint as the anchor: a
# routine name is BY DEFINITION restricted to `^[a-z0-9_]+$` (ASCII lower/
# digits/underscore), a character class ordinary Thai prose can never
# produce a token in -- so `กิจวัตร` followed by whitespace then an
# ASCII-id-shaped token (optionally followed by `=<items>` or a trailing
# `ลบ`) structurally cannot occur inside a genuine Thai sentence, which
# never actually contains such tokens (verified against the adversarial
# corpus in tests/test_commands.py). This is at least as tight a
# false-positive guard as the registry-anchoring strategy `_build_delhabit_
# th_pattern`/`_build_remind_th_pattern` use elsewhere, just keyed on the
# argument's OWN shape instead of a live habit list. A BARE "กิจวัตร" (the
# whole message, nothing after it) does NOT match at all -- unlike
# `เช็คอิน`/`แดชบอร์ด`'s own "bare word = show" convention, SPEC-v1.8.md
# §2.3 never annotates the bare-list line with a Thai alias, so `กิจวัตร`
# alone falls through to `None` (verified by the existing shared-surface
# skeleton test `test_reserved_trigger_words_covers_every_real_command_
# stem`, which already asserts the bare word dispatches to `None`).
# ---------------------------------------------------------------------------

_ROUTINE_SLASH_BARE_RE = re.compile(r"^/routine$", re.IGNORECASE)
_ROUTINE_SLASH_DELETE_RE = re.compile(r"^/routine\s+delete\s+(?P<name>\S+)$", re.IGNORECASE)
# TEST-v1.8-routines.md Finding 1 (Archi-directed follow-up fix): `items`
# was `.+` (>=1 char), so a fully bare "/routine <name> = " -- nothing at
# all after "=" once `dispatch()`'s own `text.strip()` has removed any
# trailing whitespace -- failed to match this regex AT ALL, meaning
# `commands.dispatch` returned `None` and the message silently fell through
# to the general log/LLM path instead of reaching `execute_routine`'s
# friendly `routine_create_usage` error. `.*` (>=0 chars) lets a truly
# empty tail still dispatch, with `routine_items=None` (`_parse_routine_
# items("")` already returns `None` for an all-empty items string, the
# same "no non-empty segments" branch a single bare habit-token-with-no-
# value tail already hits) -- `_create`'s existing `if command.routine_
# items is None: return routine_create_usage` then fires exactly like the
# "/routine morning = water" (habit token, no value) case already does.
_ROUTINE_SLASH_CREATE_RE = re.compile(r"^/routine\s+(?P<name>\S+?)\s*=\s*(?P<items>.*)$", re.IGNORECASE)
_ROUTINE_SLASH_RUN_RE = re.compile(r"^/routine\s+(?P<name>\S+)$", re.IGNORECASE)

# The Thai alias's own name token is anchored to R-R1's id shape itself
# (see the module comment above) -- not the live registry.
_ROUTINE_TH_DELETE_RE = re.compile(r"^กิจวัตร\s+(?P<name>[a-z0-9_]+)\s*ลบ$", re.IGNORECASE)
# Same `.+` -> `.*` fix as `_ROUTINE_SLASH_CREATE_RE` above, applied
# symmetrically to the Thai alias for the identical reason (a bare
# "กิจวัตร morning =" would otherwise silently fail to dispatch too).
_ROUTINE_TH_CREATE_RE = re.compile(r"^กิจวัตร\s+(?P<name>[a-z0-9_]+?)\s*=\s*(?P<items>.*)$", re.IGNORECASE)
_ROUTINE_TH_RUN_RE = re.compile(r"^กิจวัตร\s+(?P<name>[a-z0-9_]+)$", re.IGNORECASE)


def _parse_routine_items(items_str: str) -> list[tuple[str, str]] | None:
    """SPEC-v1.8.md §2.3's create grammar: comma-separated `"<habit>
    <val>"` items after `"="` -- SHAPE-only parsing (mirrors
    `_parse_addhabit_fields`'s own recognize-shape/validate-elsewhere
    split): each non-empty comma-segment must split into at least two
    whitespace-separated tokens (a habit token + a value tail, the value
    tail kept verbatim -- it may itself contain a space, e.g. an amount +
    unit like "20 min", mirroring `_parse_target_value`'s own tail
    shape). Returns `None` if ANY non-empty segment has fewer than two
    tokens, or there are no non-empty segments at all -- `core/
    routines.py:execute_routine`'s caller replies with a usage message for
    a `None` result, same convention `_parse_addhabit_fields`'s `None`
    already establishes for a malformed "/addhabit" tail."""
    items: list[tuple[str, str]] = []
    for segment in items_str.split(","):
        segment = segment.strip()
        if not segment:
            continue
        tokens = segment.split(None, 1)
        if len(tokens) < 2:
            return None
        items.append((tokens[0], tokens[1].strip()))
    return items if items else None


def _match_routine(stripped: str, registry: "HabitRegistry") -> "Command | None":
    """SPEC-v1.8.md §5: `registry` is accepted only to match every other
    `_match_*` function's own calling convention (`dispatch()` threads the
    same `registry` positionally to all of them) -- unused here, since
    routine names/items are per-user DB state this dispatch layer has no
    access to (see the module comment above for why the Thai alias is
    anchored on the name's own id SHAPE instead of a registry lookup)."""
    del registry

    if _ROUTINE_SLASH_BARE_RE.match(stripped):
        return Command(kind="routine", routine_action="list")

    match = _ROUTINE_SLASH_DELETE_RE.match(stripped)
    if match is not None:
        return Command(kind="routine", routine_action="delete", routine_name=match.group("name").strip())

    match = _ROUTINE_SLASH_CREATE_RE.match(stripped)
    if match is not None:
        return Command(
            kind="routine",
            routine_action="create",
            routine_name=match.group("name").strip(),
            routine_items=_parse_routine_items(match.group("items")),
        )

    match = _ROUTINE_SLASH_RUN_RE.match(stripped)
    if match is not None:
        return Command(kind="routine", routine_action="run", routine_name=match.group("name").strip())

    match = _ROUTINE_TH_DELETE_RE.match(stripped)
    if match is not None:
        return Command(kind="routine", routine_action="delete", routine_name=match.group("name"))

    match = _ROUTINE_TH_CREATE_RE.match(stripped)
    if match is not None:
        return Command(
            kind="routine",
            routine_action="create",
            routine_name=match.group("name"),
            routine_items=_parse_routine_items(match.group("items")),
        )

    match = _ROUTINE_TH_RUN_RE.match(stripped)
    if match is not None:
        return Command(kind="routine", routine_action="run", routine_name=match.group("name"))

    return None


# ---------------------------------------------------------------------------
# cadence -- SPEC-v1.9.md §4 R18 (module `cadence`). Slash form `/cadence
# <habit> <N>|off` stays fully permissive (mirrors `/target`'s/`/remind`'s
# own posture -- nobody types "/cadence" by accident, so any first token
# becomes the raw/resolved habit, any tail is offered to
# `_parse_cadence_tail`; a malformed tail still produces a Command with
# `value_num=None, pref_value=None` -- `core/cadence.py:execute_cadence`
# reports the friendly `cadence_usage` reply for that shape, never a
# dispatch failure, same recognize-shape/execute split as every settings
# command above).
#
# CRITICAL (flagged by the shared-surface Luna, IMPL-v1.9-shared.md's own
# "Known limitations"): the Thai alias trigger `กี่ครั้งต่อสัปดาห์` CONTAINS
# `กี่` -- one of `_QUERY_PATTERNS`' own substring anchors (query intent,
# below in this file) -- so a cadence phrase that also matches THIS
# module's own trigger must be recognized before `_match_query` ever gets
# a look, or it would be silently swallowed as an ordinary "how many"
# query forever. `dispatch()`'s own call order (this matcher is wired in
# well before the `_match_query` check at the very end of that function)
# is what guarantees that -- see `tests/test_cadence.py`'s own adversarial
# corpus for the two-way proof (a genuine cadence phrase routes to
# "cadence"; an ordinary "กี่..." query with no resolvable habit+value tail
# still routes to "query", unaffected by this matcher's addition).
#
# Both Thai aliases (`กี่ครั้งต่อสัปดาห์`/`ต่อสัปดาห์`) are, like `เตือน`/
# `ย้อนหลัง`/`เป้า` before them, ordinary Thai words/compounds that can open
# real prose ("ต่อสัปดาห์นี้ฉันยุ่งมาก" -- "this week I'm very busy") --
# same false-positive risk class, same mitigation: the habit token is
# built from the LIVE registry's ids/Thai labels (mirrors
# `_build_target_th_set_pattern`/`_build_remind_th_pattern` exactly), AND
# (stricter than `/remind`'s Thai alias, matching `/target`'s Thai
# alias's own posture) the trailing value must ALREADY have the shape of
# a valid cadence value (digits, or an off word) -- an unrecognized tail
# shape falls through to `None` (ordinary prose), never a usage reply,
# unlike the slash form's permissiveness above. Reuses `Command.category`
# (habit), `value_num` (N), and `pref_value` (raw "off" tail) -- no new
# fields (SPEC-v1.9.md §5's own explicit note).
# ---------------------------------------------------------------------------

_CADENCE_SLASH_RE = re.compile(r"^/cadence(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)
_CADENCE_OFF_WORDS = {"off", "ปิด", "ค่าเริ่มต้น"}


def _parse_cadence_tail(tail: str) -> tuple[float | None, str | None]:
    """`tail` -> `(value_num, pref_value)`. Only the FIRST whitespace-
    delimited token is consulted (mirrors `/history`'s own "a token beyond
    the first is ignored" tolerance) -- an off word (English "off", Thai
    "ปิด"/"ค่าเริ่มต้น", the same clear-word vocabulary `_TARGET_CLEAR_WORDS`
    already uses for `ค่าเริ่มต้น`) -> `(None, "off")`; a plain non-negative
    digit string -> `(float(N), None)` (bounds/range validation, e.g.
    N > 7, is `execute_cadence`'s own job, not this shape-only layer,
    R18's "N∉[1,7] returns the friendly error"); anything else (empty,
    non-numeric, negative) -> `(None, None)`, the shared "malformed shape"
    sentinel both the slash form's usage-reply and the Thai alias's
    fall-through-to-None each key off of."""
    token = tail.strip().split(None, 1)[0] if tail.strip() else ""
    if not token:
        return None, None
    if token.lower() in _CADENCE_OFF_WORDS:
        return None, "off"
    if token.isdigit():
        return float(token), None
    return None, None


def _match_cadence_slash(stripped: str, registry: "HabitRegistry") -> "Command | None":
    match = _CADENCE_SLASH_RE.match(stripped)
    if match is None:
        return None
    rest = match.group("rest")
    if rest is None:
        return Command(kind="cadence")  # bare "/cadence" -> execute_cadence's own usage reply

    parts = rest.strip().split(None, 1)
    habit_token = parts[0]
    tail = parts[1].strip() if len(parts) > 1 else None
    # `_resolve_target_category` is a generic habit-token resolver (id /
    # en label / th label -> id, else the raw lowercased token) shared
    # verbatim with `/target`/`/remind`/`/history` rather than duplicated.
    category = _resolve_target_category(habit_token, registry)
    if tail is None:
        return Command(kind="cadence", category=category)  # habit only, no value -> usage reply

    value_num, pref_value = _parse_cadence_tail(tail)
    return Command(kind="cadence", category=category, value_num=value_num, pref_value=pref_value)


def _build_cadence_th_pattern(registry: "HabitRegistry") -> re.Pattern[str] | None:
    """Thai "(กี่ครั้ง)ต่อสัปดาห์<habit><value>" -- habit token built from the
    LIVE registry's ids/Thai labels, same false-positive mitigation as
    `_build_target_th_set_pattern`/`_build_remind_th_pattern`/
    `_build_history_th_pattern` (only a message naming a habit this bot
    actually tracks for this user can ever match at all). The two trigger
    literals don't need length-sorting against each other (unlike the
    habit-token alternation below, which DOES sort longest-first) -- they
    start with different characters (`ก` vs `ต`), so alternation order
    between them can never cause one to shadow the other. Returns `None`
    if the registry has no matchable Thai/id tokens (defensive; every
    shipped config has at least water's "น้ำ")."""
    escaped = _registry_th_tokens(registry)
    if not escaped:
        return None
    habit_alt = "|".join(escaped)
    return re.compile(rf"^(?:กี่ครั้งต่อสัปดาห์|ต่อสัปดาห์)\s*(?P<habit>{habit_alt})\s*(?P<value>\S+)$")


def _match_cadence_nl(stripped: str, registry: "HabitRegistry") -> "Command | None":
    th_pattern = _build_cadence_th_pattern(registry)
    if th_pattern is None:
        return None
    match = th_pattern.match(stripped)
    if match is None:
        return None

    value_num, pref_value = _parse_cadence_tail(match.group("value"))
    if value_num is None and pref_value is None:
        # Trailing text doesn't have a valid cadence-value SHAPE -- ordinary
        # Thai prose that merely happens to open with the trigger word and
        # name a real habit (e.g. "ต่อสัปดาห์น้ำท่วมหนักมาก"). Fall through to
        # `None` (unlike the slash form's permissiveness above) -- the same
        # conservative posture `_match_remind`'s Thai alias hardening
        # established (see this file's own v1.2.0 audit-fix docstring
        # section) for a real Thai word that can open ordinary sentences.
        return None

    # The habit token came straight out of the registry-built alternation
    # above, so it is always resolvable -- the `or` fallback is defensive
    # only, never actually hit (mirrors `_match_remind`'s own precedent).
    category = _resolve_habit_token(match.group("habit"), registry) or match.group("habit").lower()
    return Command(kind="cadence", category=category, value_num=value_num, pref_value=pref_value)


def _match_cadence(stripped: str, registry: "HabitRegistry") -> "Command | None":
    return _match_cadence_slash(stripped, registry) or _match_cadence_nl(stripped, registry)


# ---------------------------------------------------------------------------
# pause / resume -- SPEC-v1.9.md §4 R12/R13 (module `pause`). Slash forms
# `/pause [<habit>] <Nd|until DATE|until WEEKDAY>` and `/resume [<habit>]`
# stay fully permissive (mirrors `/target`'s/`/remind`'s/`/cadence`'s own
# posture -- nobody types "/pause"/"/resume" by accident, so any tail
# still produces a Command; `core/pause.py:execute_pause`/`execute_resume`
# are where an unresolved habit, a malformed/past duration, or an
# over-cap span each become a friendly reply, never a dispatch failure).
#
# `_split_pause_tail` below is this module's own shape-only "does the
# first token look like the START of a duration, or a habit name?"
# split -- R12's duration grammar always starts with either a digit
# ("5d") or the literal word "until", a shape no habit id/label in this
# app can ever collide with (habit tokens are alphabetic ids or labels;
# "until" is not itself a reserved trigger word, but the grammar's own
# fixed vocabulary makes the split unambiguous regardless). A bare
# `/pause`/`/resume` (no tail at all) and a habit-only tail with NO
# duration (`/pause water`) both carry `pref_value=None` -- `execute_
# pause` treats that as "show current status" (a deliberate UX addition,
# not an AC-mandated behavior; see IMPL-v1.9-pause.md), never an error.
#
# Both Thai aliases (`พัก`/`หยุดพัก` for pause, `กลับมา`/`ต่อ` for resume)
# are, like `เตือน`/`ย้อนหลัง`/`ต่อสัปดาห์` before them, ordinary Thai
# words that can open real prose ("ต่อ" above all -- an extremely common
# two-character word meaning "continue/next/versus", the single riskiest
# trigger stem in this file). Two deliberate hardening choices, STRICTER
# than `เตือน`'s own established precedent:
#   1. The Thai trigger's tail is MANDATORY (`\s+\S.*`, no optional
#      group) -- a completely bare "พัก"/"หยุดพัก"/"กลับมา"/"ต่อ" NEVER
#      dispatches at all, unlike `เช็คอิน`'s/`แดชบอร์ด`'s own "bare word
#      = show" precedent. R13's own "no token resumes all" meaning is
#      still fully reachable -- just via `/resume` (the English slash
#      form, which nobody types by accident), never via the bare Thai
#      word alone. This is a deliberate, documented narrowing of the
#      Thai alias's reach (see IMPL-v1.9-pause.md's "Known limitations")
#      in exchange for eliminating the false-positive risk a two-
#      character common word carries when bare-matchable.
#   2. When a tail IS present, any habit-shaped token in it must resolve
#      via the LIVE registry (mirrors `_build_remind_th_pattern`'s own
#      alternation), and any duration-shaped token must match the same
#      `<N>d`/`until ...` SHAPE `_split_pause_tail` recognizes for the
#      slash form (not yet full semantic validation -- that's still
#      `execute_pause`'s job) -- so ordinary prose after any of the four
#      trigger words ("พัก ก่อนนะ", "ต่อไปเลย", "กลับมาแล้วนะ") still
#      falls through to `None` (verified against the adversarial corpus
#      in tests/test_pause.py).
# ---------------------------------------------------------------------------

_PAUSE_SLASH_RE = re.compile(r"^/pause(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)
_RESUME_SLASH_RE = re.compile(r"^/resume(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)
_PAUSE_TH_RE = re.compile(r"^(?:หยุดพัก|พัก)\s+(?P<rest>\S.*)$")
_RESUME_TH_RE = re.compile(r"^(?:กลับมา|ต่อ)\s+(?P<rest>\S.*)$")
_PAUSE_DURATION_SHAPE_RE = re.compile(r"^(?:\d+d|until\s+\S+)$", re.IGNORECASE)


def _split_pause_tail(rest: str) -> tuple[str | None, str | None]:
    """`rest` (already stripped, non-empty) -> `(habit_token, duration_raw)`.
    The first whitespace token starting with a digit or equal to "until"
    (case-insensitive) means NO habit was given -- the whole tail is the
    duration; otherwise the first token is the habit and everything after
    it (joined back with single spaces) is the duration, or `None` if
    nothing follows (habit given, no duration -> `execute_pause`'s own
    "show status for this habit" branch)."""
    tokens = rest.split()
    first = tokens[0]
    if first[0].isdigit() or first.lower() == "until":
        return None, " ".join(tokens)
    remainder = " ".join(tokens[1:])
    return first, remainder or None


def _match_pause_slash(stripped: str, registry: "HabitRegistry") -> "Command | None":
    match = _PAUSE_SLASH_RE.match(stripped)
    if match is None:
        return None
    rest = match.group("rest")
    if rest is None:
        return Command(kind="pause", category=None, pref_value=None)
    habit_token, duration_raw = _split_pause_tail(rest.strip())
    category = _resolve_target_category(habit_token, registry) if habit_token is not None else None
    return Command(kind="pause", category=category, pref_value=duration_raw)


def _match_pause_th(stripped: str, registry: "HabitRegistry") -> "Command | None":
    match = _PAUSE_TH_RE.match(stripped)
    if match is None:
        return None
    habit_token, duration_raw = _split_pause_tail(match.group("rest").strip())
    if habit_token is not None:
        resolved = _resolve_habit_token(habit_token, registry)
        if resolved is None:
            return None
        habit_token = resolved
    if duration_raw is not None and not _PAUSE_DURATION_SHAPE_RE.match(duration_raw):
        return None
    return Command(kind="pause", category=habit_token, pref_value=duration_raw)


def _match_pause(stripped: str, registry: "HabitRegistry") -> "Command | None":
    return _match_pause_slash(stripped, registry) or _match_pause_th(stripped, registry)


def _match_resume_slash(stripped: str, registry: "HabitRegistry") -> "Command | None":
    match = _RESUME_SLASH_RE.match(stripped)
    if match is None:
        return None
    rest = match.group("rest")
    if rest is None:
        return Command(kind="resume", category=None)
    habit_token = rest.strip().split(None, 1)[0]
    return Command(kind="resume", category=_resolve_target_category(habit_token, registry))


def _match_resume_th(stripped: str, registry: "HabitRegistry") -> "Command | None":
    match = _RESUME_TH_RE.match(stripped)
    if match is None:
        return None
    tail = match.group("rest").strip()
    # Unlike pause, resume has no duration -- a valid Thai-alias argument
    # is exactly ONE token (a habit name); anything with a second token is
    # ordinary prose ("ต่อ ไปอีกหน่อย"), not a habit name, and falls
    # through to None.
    if " " in tail:
        return None
    resolved = _resolve_habit_token(tail, registry)
    if resolved is None:
        return None
    return Command(kind="resume", category=resolved)


def _match_resume(stripped: str, registry: "HabitRegistry") -> "Command | None":
    return _match_resume_slash(stripped, registry) or _match_resume_th(stripped, registry)


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


def _edit_triggered(stripped: str) -> bool:
    return _EDIT_TRIGGER.match(stripped) is not None


def _resolve_edit(stripped: str, registry: "HabitRegistry") -> "Command | None":
    """`edit`'s full resolution -- called only once `_edit_triggered` has
    already confirmed the trigger shape matched (rule 14 invariant (ii)).
    `None` here is a TERMINAL rejection (SPEC-v0.7.md §4 R14's own "garbled
    tail -> None" contract): the caller (`dispatch`) returns it immediately
    rather than treating it as "no match, keep walking the table"."""
    trigger_match = _EDIT_TRIGGER.match(stripped)
    if trigger_match is None:
        return None
    parsed = _parse_edit_value(trigger_match.group("value"), registry)
    if parsed is None:
        return None
    category, value_num = parsed
    return Command(kind="edit", category=category, value_num=value_num)


def _resolve_snooze(stripped: str, registry: "HabitRegistry") -> "Command | None":
    del registry
    snoozed, minutes = _match_snooze(stripped)
    return Command(kind="snooze", minutes=minutes) if snoozed else None


def _ignore_registry(
    match: "Callable[[str], Command | None]",
) -> "Callable[[str, HabitRegistry], Command | None]":
    """Adapts a `_match_*` function that only takes `stripped` (no
    `registry`) to the uniform `(stripped, registry) -> Command | None`
    row shape `_MATCHERS` needs -- these matchers have no registry
    dependency of their own; `registry` is threaded through unused."""

    def _adapted(stripped: str, registry: "HabitRegistry") -> "Command | None":
        del registry
        return match(stripped)

    return _adapted


def _bool_matcher(
    match: "Callable[[str], bool]", kind: CommandKind
) -> "Callable[[str, HabitRegistry], Command | None]":
    """Adapts a bare `bool` `_match_*` predicate (undo/help/habits/query --
    none of the four carry any payload) to the uniform row shape, producing
    the payload-less `Command(kind=...)` on a hit."""

    def _adapted(stripped: str, registry: "HabitRegistry") -> "Command | None":
        del registry
        return Command(kind=kind) if match(stripped) else None

    return _adapted


@dataclass(frozen=True, slots=True)
class _MatcherEntry:
    """One row of the dispatch table (SPEC-REFACTOR.md Stage 3, rule 14).
    `match(stripped, registry)` returns the `Command` this row recognizes,
    or `None`. For an ordinary row (`triggered=None`), `None` means "this
    row doesn't recognize `stripped` -- keep walking the table" -- every
    matcher here is pure fall-through (rule 14's own "disjoint-trigger"
    property, documented per-matcher above). `edit` is the sole exception
    (rule 14 invariant (ii)): once `triggered(stripped)` is True, this row
    COMMITS -- `match`'s return value (a `Command`, or a terminal `None`)
    is what `dispatch` returns, without offering `stripped` to any later
    row."""

    kind: str
    match: "Callable[[str, HabitRegistry], Command | None]"
    triggered: "Callable[[str], bool] | None" = None


# The table itself: SAME 27 rows PLUS `guide` (SPEC-v1.10.md §4 R-SS8) PLUS
# `digest` (SPEC-LINE.md §4 R-C4, branch `line-version`) PLUS `review`
# (SPEC-LINE.md §4 R-C5, Integration pass), SAME order as the if-chain the
# original 27 replaced (ROADMAP.md v0.9.0 -> SPEC-v1.9.md's own accreted
# routing brief, each insertion point documented per-matcher above) --
# undo -> edit -> snooze -> target -> remind -> access -> audit -> lang ->
# quiet -> checkin -> dnd -> dashboard -> digest -> history -> heatmap ->
# records -> trends -> wrapped -> review -> addhabit -> delhabit -> log ->
# routine -> cadence -> pause -> resume -> help -> habits -> guide -> query.
# `guide` is placed immediately before `query` (R-SS8's own stated
# placement) -- it has no substring/interrogative-anchor overlap with any
# query pattern, so its exact position among the rest (other than staying
# ahead of the final `query` row) doesn't change behavior; grouped next to
# `help`/`habits` for readability, same disjoint-trigger-text reasoning.
# `digest` is grouped next to `dashboard` for the same reason -- disjoint
# trigger text (`/digest`/`สรุปรายวัน` vs every other row) means its exact
# position among the rest doesn't change behavior either. `review` is
# grouped right after `wrapped` -- both are on-demand, reply-only "show me
# X" commands with disjoint trigger text (`/review` vs every other row), so
# its exact position among the rest doesn't change behavior either.
# `_assert_dispatch_invariants` below proves the three rule-14 invariants
# hold structurally; `tests/test_refactor_s3.py`'s golden precedence corpus
# proves the pre-v1.10 27-row table reproduces the pre-conversion if-chain's
# exact output (unaffected by these additive rows).
_MATCHERS: list[_MatcherEntry] = [
    _MatcherEntry("undo", _bool_matcher(_match_undo, "undo")),
    _MatcherEntry("edit", _resolve_edit, triggered=_edit_triggered),
    _MatcherEntry("snooze", _resolve_snooze),
    _MatcherEntry("target", _match_target),
    _MatcherEntry("remind", _match_remind),
    _MatcherEntry("access", _ignore_registry(_match_access)),
    _MatcherEntry("audit", _ignore_registry(_match_audit)),
    _MatcherEntry("lang", _ignore_registry(_match_lang)),
    _MatcherEntry("quiet", _ignore_registry(_match_quiet)),
    _MatcherEntry("checkin", _ignore_registry(_match_checkin)),
    _MatcherEntry("dnd", _ignore_registry(_match_dnd)),
    _MatcherEntry("dashboard", _ignore_registry(_match_dashboard)),
    _MatcherEntry("digest", _ignore_registry(_match_digest)),
    _MatcherEntry("history", _match_history),
    _MatcherEntry("heatmap", _match_heatmap),
    _MatcherEntry("records", _match_records),
    _MatcherEntry("trends", _match_trends),
    _MatcherEntry("wrapped", _ignore_registry(_match_wrapped)),
    _MatcherEntry("review", _bool_matcher(_match_review, "review")),
    _MatcherEntry("addhabit", _ignore_registry(_match_addhabit)),
    _MatcherEntry("delhabit", _match_delhabit),
    _MatcherEntry("log", _ignore_registry(_match_log)),
    _MatcherEntry("routine", _match_routine),
    _MatcherEntry("cadence", _match_cadence),
    _MatcherEntry("pause", _match_pause),
    _MatcherEntry("resume", _match_resume),
    _MatcherEntry("help", _bool_matcher(_match_help, "help")),
    _MatcherEntry("habits", _bool_matcher(_match_habits, "habits")),
    _MatcherEntry("guide", _bool_matcher(_match_guide, "guide")),
    _MatcherEntry("query", _bool_matcher(_match_query, "query")),
]


def _assert_dispatch_invariants(matchers: "list[_MatcherEntry]") -> None:
    """SPEC-REFACTOR.md Stage 3 rule 14: a runtime, import-time structural
    guard proving `_MATCHERS` still encodes the three precedence invariants
    a future table edit could otherwise silently break. Runs once, at
    import time, right below -- `tests/test_refactor_s3.py` covers the same
    three invariants behaviorally on top of this."""
    kinds = [m.kind for m in matchers]
    assert len(kinds) == len(set(kinds)), "duplicate matcher kind in _MATCHERS"
    assert kinds[-1] == "query", (
        "'query' must be the LAST row -- it is the only substring/.search matcher (rule 14 invariant iii)"
    )
    assert kinds.index("cadence") < kinds.index("query"), (
        "'cadence' must precede 'query' -- กี่ครั้งต่อสัปดาห์ contains the query anchor กี่ (rule 14 invariant i)"
    )
    commit_rows = [m.kind for m in matchers if m.triggered is not None]
    assert commit_rows == ["edit"], f"'edit' must be the sole commit-on-trigger row, got {commit_rows!r} (invariant ii)"


_assert_dispatch_invariants(_MATCHERS)


def dispatch(text: str, registry: "HabitRegistry") -> Command | None:
    """Classify `text` as an explicit command, or return None to fall
    through to the LLM parser (AC5.5: normal habit messages like "500ml"
    or "ดื่มน้ำ 2 แก้ว" must route unchanged -- zero false positives).

    SPEC-REFACTOR.md Stage 3: walks `_MATCHERS`, the table-driven form of
    what was (through v1.9.2) an ordered if-chain -- same 27 rows, same
    order, same three precedence invariants (rule 14; `_assert_dispatch_
    invariants` above proves them structurally, `tests/test_refactor_s3.py`
    proves the table reproduces the pre-conversion if-chain's exact output
    over a golden corpus). An ordinary row's `None` means "doesn't
    recognize `stripped`, keep walking"; `edit` is the sole exception
    (`triggered`, invariant (ii)) -- once its trigger SHAPE matches, this
    function returns whatever `edit`'s own resolution gives (a `Command`,
    or a TERMINAL `None`) without offering `stripped` to any later row,
    exactly reproducing the pre-v0.8 "a garbled edit tail falls through to
    the extractor, not to snooze/target/.../query" contract. `query` stays
    the final row because `_match_query` is the only substring/`.search`
    matcher here (invariant (iii)) -- every other row is anchored to the
    whole stripped message with its own disjoint trigger text (documented
    per-matcher above), so its exact position among the rest is
    behavior-preserving by construction."""
    stripped = text.strip()
    if not stripped:
        return None

    for entry in _MATCHERS:
        if entry.triggered is not None:
            if not entry.triggered(stripped):
                continue
            return entry.match(stripped, registry)
        result = entry.match(stripped, registry)
        if result is not None:
            return result

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
            # SPEC-v1.8.md R-S5 (shared surface): "log"/"routine" reserved
            # ahead of the two parallel modules that will actually add
            # `_match_log`/`_match_routine` (module `quicklog`/`routines`) --
            # same "skeleton reserves the word before the matcher exists"
            # posture as the `CommandKind` skeleton entries just above in
            # this file. These are the exact literals SPEC-v1.8.md §2.1/§2.3
            # documents as the trigger words those two future matchers will
            # anchor on ("/log"/"บันทึก", "/routine"/"กิจวัตร") -- so a custom
            # habit named after any of them is rejected by `habitdef` today
            # (AC-8), and the reservation needs no edit once the matchers
            # themselves land.
            "log",
            "บันทึก",
            "routine",
            "กิจวัตร",
            # SPEC-v1.9.md §5 (shared surface): reserved ahead of the four
            # parallel modules that will actually add `_match_cadence`/
            # `_match_pause`/`_match_resume`/`_match_wrapped` (module
            # `cadence`/`pause`/`wrapped`) -- same "skeleton reserves the
            # word before the matcher exists" posture as the `CommandKind`
            # skeleton entries just above in this file. These are the
            # exact literals SPEC-v1.9.md §5's own "reserve stems" comment
            # names, so a custom habit named after any of them is rejected
            # by `habitdef` today (mirrors the v1.8 log/routine
            # reservation), and the reservation needs no edit once the
            # matchers themselves land.
            "cadence",
            "ต่อสัปดาห์",
            "กี่ครั้งต่อสัปดาห์",
            "pause",
            "พัก",
            "หยุดพัก",
            "resume",
            "กลับมา",
            "ต่อ",
            "wrapped",
            "recap",
            "สรุปเดือน",
            "การ์ดสรุป",
            # SPEC-v1.10.md §4 R-SS8 (shared surface, functional 5 "/guide"):
            # `_GUIDE_RE`, line ~831 above.
            "guide",
            "คู่มือ",
            # SPEC-LINE.md §4 R-C4/§9 OQ4 (module C, branch `line-version`):
            # `_DIGEST_SLASH_RE`/`_DIGEST_TH_RE` above.
            "digest",
            "สรุปรายวัน",
            # SPEC-LINE.md §4 R-C5 (Integration pass, branch `line-version`):
            # `_REVIEW_RE` above.
            "review",
        }
    )
