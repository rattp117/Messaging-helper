"""Access control & onboarding (SPEC-v1.2.md §4 "Access control &
onboarding (module `access`)", R-A1-R-A5): the gate every inbound update
passes through before any logging/LLM/command work, plus the owner-only
admin commands (`/approve`, `/block`, `/users`, `/invite`) and `/start`
that this module owns per SPEC-v1.2.md §11's module table (AC-A1-AC-A7).

Three public entry points (SPEC-v1.2.md §5):
- `classify(db, chat_id) -> Access` -- a pure read, fail-safe on any DB
  error (R-A1: "a lookup error classifies as not active (deny)").
- `handle_gate(...)` -- the seam `main.py`'s integration step calls FIRST,
  before `handle_inbound_message`/`commands.dispatch` (R-A1). Returns
  `True` only for `owner`/`active` (proceed to the normal pipeline);
  otherwise it has already sent the onboarding/refusal reply itself and
  returns `False` -- the caller must not log the message or call the LLM
  for a `False` result (R-A2's "neither logged nor sent to the LLM").
- `execute_admin(...)` -- the seam for `command.kind in ("start",
  "approve", "block", "users", "invite")`, called only AFTER `handle_gate`
  has already returned `True` (so the acting chat is already known
  owner/active) -- see this function's own docstring for why it still
  re-checks role itself.

No channel import beyond the `Channel` ABC (mirrors `core/undo_ui.py`'s
own import shape); no `main.py` changes here -- the exact wiring calls are
documented in IMPL-v1.2-access.md for the integration step to make.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Literal

from habit_assistant import __version__
from habit_assistant.channels.base import Channel
from habit_assistant.core import audit, i18n, user_prefs

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.commands import Command
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

Access = Literal["owner", "active", "pending", "blocked", "unknown"]

# SPEC-LINE.md §4 (release-gate Finding 1, branch `line-version`): a
# STRICT whitelist of exactly the two chat-id shapes this app ever
# actually hands to `execute_admin` -- not a general loosening.
# `commands.py:_match_access` hands `target_chat` through raw/unvalidated
# (shape-only layer); this is where R-A4/§3.5's "malformed ... chat id ->
# a friendly usage message" is actually enforced, for BOTH channels this
# core/ module is now reached from:
#   - Telegram: optional leading "-" (group/channel ids are negative),
#     digits only (unchanged, pre-LINE behavior).
#   - LINE: a literal "U" prefix followed by an opaque alphanumeric
#     token (SPEC-LINE.md §2.1's own example: `"U4af4980629..."`). Real
#     LINE userIds are `U` + 32 lowercase-hex characters, but this check
#     is deliberately NOT hex-restricted -- hex-strictness is an
#     implementation detail of LINE's own id generator, not a security
#     boundary this app can usefully enforce, and it would reject
#     perfectly legitimate non-hex ids too (this app's own LINE test
#     fixtures use readable placeholders like "Uowner..."/"Umember...").
#     The "U" prefix + a plausible length bound is the real, meaningful
#     shape check here.
_CHAT_ID_RE = re.compile(r"^(?:-?\d+|U[0-9A-Za-z]{16,40})$")


# ---------------------------------------------------------------------------
# R-A1: classify -- pure read, fail-safe (AC-A7).
# ---------------------------------------------------------------------------


def classify(db: "Database", chat_id: str) -> Access:
    """owner ⊂ active (R-A1): a `role="owner"` row classifies as `"owner"`
    outright, regardless of its `status` (attribute_legacy_to_owner always
    stamps it `active`, but this doesn't re-derive that -- the role alone
    is authoritative for the owner). Otherwise `status` decides:
    `"active"`/`"blocked"` map directly, anything else (`"pending"`, or a
    defensive fallback for an unexpected value) -> `"pending"`. No row at
    all -> `"unknown"`.

    AC-A7 (fail-safe): a `db.get_user` exception is caught here and
    classified as `"blocked"` -- not active, and no further DB write is
    attempted (unlike the `"unknown"` path, which would try to insert a
    pending row) while the DB is in a state that just failed to read.
    Never raises."""
    try:
        row = db.get_user(chat_id)
    except Exception:
        logger.exception("users lookup failed for chat_id=%r; failing closed (not active)", chat_id)
        return "blocked"

    if row is None:
        return "unknown"
    if row["role"] == "owner":
        return "owner"
    if row["status"] == "active":
        return "active"
    if row["status"] == "blocked":
        return "blocked"
    return "pending"


def _resolve_unprompted_language_for(db: "Database", config: "Config", chat_id: str) -> i18n.Language:
    """The language to use for an UNPROMPTED send addressed to `chat_id`
    (i.e. not a reply to something `chat_id` just said) -- the
    `access_request` notification to the owner (triggered by someone
    else's message) and the `access_granted` notification to a
    newly-approved user (triggered by the owner's `/approve`). Reads
    `chat_id`'s own stored `language_pref` via the shared `core/user_prefs.
    stored_language_pref` (SPEC-v1.8.md integration step's consolidation of
    what used to be four independent per-file copies of this lookup),
    defaulting to `"auto"` (the column's own default) if the row can't be
    read, mirroring `i18n.resolve_unprompted_language`'s existing fail-open
    shape for a missing preference."""
    pref = user_prefs.stored_language_pref(db, chat_id)
    return i18n.resolve_unprompted_language(config, user_pref=pref)


# ---------------------------------------------------------------------------
# R-A1/R-A2/R-A3: the gate itself.
# ---------------------------------------------------------------------------


async def handle_gate(
    db: "Database",
    channel: Channel,
    config: "Config",
    owner_chat_id: str,
    chat_id: str,
    display_name: str | None,
    text: str,
    *,
    lang: i18n.Language,
) -> bool:
    """SPEC-v1.2.md R-A1: called before any logging/LLM/command work.
    Returns `True` only for `owner`/`active` (the caller proceeds to the
    normal pipeline unchanged); otherwise sends the onboarding/refusal
    reply itself and returns `False` -- the caller must not log `text` or
    call the LLM for a `False` result (R-A2).

    `text` is accepted but not used to decide anything here (SPEC-v1.2.md
    §2.3: `/start` is not special-cased inside the gate itself -- see
    `execute_admin`'s docstring for why the R-A5 "active user runs
    `/start` -> welcome" branch lives there instead); it exists in the
    signature per SPEC-v1.2.md §5's own interface listing, kept for a
    future gate rule that DOES need to inspect the message (e.g. a
    future one-tap approve reply, §10) without another signature change.

    `lang` is `chat_id`'s OWN resolved reply language (a response to the
    message `chat_id` just sent) -- already resolved by the caller.
    `access_request` (sent to a DIFFERENT chat, the owner) and is instead
    resolved via `_resolve_unprompted_language_for` (unprompted send,
    R-P1's resolution rules)."""
    access = classify(db, chat_id)
    if access in ("owner", "active"):
        return True

    if access == "unknown":
        # R-A2: create the pending row (capturing display_name when given),
        # reply to the asker, and notify the owner -- best-effort on the
        # write (a DB hiccup here must not crash the inbound loop; the
        # asker still gets a reply either way).
        #
        # SPEC-v1.3.md R-C3/R-C4/AC-P1 (privacy): the audit row records
        # ONLY the state transition (new_value="pending") -- never `text`,
        # which is this chat's actual message content. `audit.record` is
        # called only when `upsert_user` itself succeeded (inside the same
        # try) -- no phantom "became pending" row for a write that never
        # landed. `actor` and `target_user_id` are both `chat_id` (nobody
        # else "acted" -- the chat's own first contact caused its own
        # transition, per core/audit.py's own docstring).
        try:
            db.upsert_user(chat_id, status="pending", display_name=display_name)
            audit.record(
                db,
                actor=chat_id,
                action="user_pending",
                source="admin",
                target_user_id=chat_id,
                old_value=None,
                new_value="pending",
            )
        except Exception:
            logger.exception("Failed to create pending user row for chat_id=%r", chat_id)
        await channel.send(chat_id, i18n.t("access_pending", lang))
        owner_lang = _resolve_unprompted_language_for(db, config, owner_chat_id)
        await channel.send(
            owner_chat_id,
            i18n.t("access_request", owner_lang, name=display_name or chat_id, chat_id=chat_id),
        )
        return False

    if access == "pending":
        await channel.send(chat_id, i18n.t("access_pending", lang))
        return False

    # blocked, or classify()'s AC-A7 fail-safe fallback.
    await channel.send(chat_id, i18n.t("access_denied", lang))
    return False


# ---------------------------------------------------------------------------
# R-A4/R-A5: `/start` + the owner-only admin commands.
# ---------------------------------------------------------------------------


def _render_users_list(db: "Database", lang: i18n.Language) -> str:
    """SPEC-v1.2.md §3.3: one line per user, in `list_users`'s own order
    (first-contacted-first). `role`/`status` render as their raw stored
    values (not localized -- this is an owner-only technical/admin view,
    and the spec's own §3.3 example never shows a Thai role/status word);
    the `· lang {pref}` suffix is shown only for an `active` row, matching
    the example (a `pending` row shows no lang suffix)."""
    lines = [i18n.t("users_list_header", lang)]
    for row in db.list_users():
        lang_suffix = f" · lang {row['language_pref']}" if row["status"] == "active" else ""
        lines.append(
            i18n.t(
                "users_list_line",
                lang,
                chat_id=row["chat_id"],
                role=row["role"],
                status=row["status"],
                lang_suffix=lang_suffix,
            )
        )
    return "\n".join(lines)


async def execute_admin(
    command: "Command",
    *,
    db: "Database",
    channel: Channel,
    config: "Config",
    owner_chat_id: str,
    chat_id: str,
    lang: i18n.Language,
) -> None:
    """The seam for `command.kind in ("start", "approve", "block", "users",
    "invite")`, called by the integration wiring only AFTER `handle_gate`
    has already returned `True` for `chat_id` (i.e. `chat_id` is already
    known `owner`/`active`) -- which is why `"start"` here only needs the
    R-A5 "active user" branch (welcome/intro): the unknown/pending/blocked
    `/start` branches are already covered by `handle_gate` itself, since
    `/start` from a non-active chat never reaches command dispatch at all
    (R-A1 gates before any command work).

    R-A4/AC-A4 (owner-only, invisible to non-owners): `approve`/`block`/
    `users`/`invite` re-check `classify(db, chat_id) == "owner"` HERE too,
    even though the caller could only have reached this function for an
    already-active chat -- active does not imply owner (a member is
    active too), and this is the one check that must never be skippable
    by a future caller that forgets it. A non-owner gets no reply at all
    (a silent no-op) -- `execute_admin` is declared `-> None` (SPEC-v1.2.md
    §5), so there is no "handled" signal back to the caller to fall
    through to the parser; a plain no-op is what "reveals nothing" (§3.5)
    means here (see IMPL-v1.2-access.md's "Known limitations" for this
    call).

    `target_chat` (for `approve`/`block`/`invite`) is validated here
    (`_CHAT_ID_RE`) -- missing or malformed -> `admin_usage` (§3.5). A DB
    write failure is caught and reported via `admin_save_failed`, never a
    traceback (mirrors `core/targets_command.py`'s own try/except-around-
    the-write convention). `/approve`/`/invite` are the same action (R-A4:
    "`/invite <chat_id>` -- alias of `/approve`")."""
    if command.kind == "start":
        # Integration-step hardening (TEST-v1.2-access.md's own low-
        # severity, non-blocking finding): every OTHER kind below
        # redundantly re-checks its own precondition even though the
        # caller is supposed to already be gated -- "start" had no
        # equivalent check at all, relying entirely on `handle_gate`
        # having already returned `True` for `chat_id`. Unlike
        # approve/block/users/invite (owner-ONLY, R-A4), `/start` is
        # available to any active user (R-A5), so the re-check here is
        # "owner or active" -- the exact set `handle_gate` itself treats
        # as "proceed" -- not "owner" alone, which would incorrectly
        # break `/start` for an ordinary member.
        if classify(db, chat_id) not in ("owner", "active"):
            return
        await channel.send(chat_id, i18n.t("start_welcome", lang))
        return

    if classify(db, chat_id) != "owner":
        return

    if command.kind == "users":
        await channel.send(chat_id, _render_users_list(db, lang))
        return

    target_chat = command.target_chat
    if target_chat is None or _CHAT_ID_RE.match(target_chat) is None:
        await channel.send(chat_id, i18n.t("admin_usage", lang))
        return

    # SPEC-v1.3.md R-C3: read the prior status BEFORE the write (best-effort
    # -- a failed pre-read degrades to old_value=None rather than blocking
    # the approve/block action itself, same "capture must never gate the
    # write" posture as every other capture site here).
    if command.kind in ("approve", "invite"):
        try:
            previous = db.get_user(target_chat)
        except Exception:
            previous = None
        previous_status = previous["status"] if previous is not None else None
        try:
            db.upsert_user(target_chat, status="active")
        except Exception:
            logger.exception("Failed to approve chat_id=%r", target_chat)
            await channel.send(chat_id, i18n.t("admin_save_failed", lang))
            return
        # SPEC-v1.5.md R-N5/AC-23: catch a newly-approved user up to the
        # CURRENT running version, right after the approve write succeeds --
        # so they never receive a release announcement for a version that
        # shipped before they were let in (only FUTURE version bumps
        # announce to them). Best-effort: a failure here must not undo or
        # block the approve itself (the user is already active); a missed
        # catch-up here just means `announce.announce_release`'s own
        # idempotent per-user check will (harmlessly) send them the current
        # version's note next startup instead.
        try:
            db.set_last_announced_version(target_chat, __version__)
        except Exception:
            logger.exception("Failed to catch up last_announced_version for newly-approved chat_id=%r", target_chat)
        audit.record(
            db,
            actor=chat_id,
            action="user_approve",
            source="admin",
            target_user_id=target_chat,
            old_value=previous_status,
            new_value="active",
        )
        await channel.send(chat_id, i18n.t("admin_approved_ack", lang, chat_id=target_chat))
        target_lang = _resolve_unprompted_language_for(db, config, target_chat)
        await channel.send(target_chat, i18n.t("access_granted", target_lang))
        return

    if command.kind == "block":
        try:
            previous = db.get_user(target_chat)
        except Exception:
            previous = None
        previous_status = previous["status"] if previous is not None else None
        try:
            db.upsert_user(target_chat, status="blocked")
        except Exception:
            logger.exception("Failed to block chat_id=%r", target_chat)
            await channel.send(chat_id, i18n.t("admin_save_failed", lang))
            return
        audit.record(
            db,
            actor=chat_id,
            action="user_block",
            source="admin",
            target_user_id=target_chat,
            old_value=previous_status,
            new_value="blocked",
        )
        await channel.send(chat_id, i18n.t("admin_blocked_ack", lang, chat_id=target_chat))
        return
