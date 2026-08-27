"""Release announcements (SPEC-v1.5.md §4 "Feature 4 -- Release
announcements (module `announce`)", R-N1-R-N6): a deterministic, LLM-free,
once-per-version-per-user "what's new" broadcast, run once at startup.

One public entry point (SPEC-v1.5.md §5):
- `announce_release(db, channel, config, version) -> None` -- the startup
  step `main.py`'s integration wiring calls once, AFTER migrations +
  `attribute_legacy_to_owner` (R-N2 -- same placement rationale as
  attribution: the active-user set must already be correct). See
  IMPL-v1.5-announce.md's "Wiring instructions" for the exact call site.

No channel import beyond the `Channel` ABC (mirrors `core/access.py`'s own
import shape); this module never consults `reminders.in_dnd_now` -- R-N4's
own explicit "send anyway" decision (a rare one-shot, not a recurring habit
nudge -- SPEC-v1.5.md §2.3's suppression matrix)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from habit_assistant.channels.base import Channel
from habit_assistant.core import i18n, user_prefs
from habit_assistant.core.release_notes import RELEASE_NOTES, get_release_note

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)


async def announce_release(db: "Database", channel: Channel, config: "Config", version: str) -> None:
    """SPEC-v1.5.md R-N2: for each `db.active_user_ids()` (pending/blocked
    users are never in that set, R-N2/AC-24), skip a user already at
    `version` (idempotent per user per version, R-N3/AC-21); otherwise
    resolve their language and send -- ONLY a successful send marks them
    caught up (`db.set_last_announced_version`), so a send/DB failure
    leaves that user unmarked and they are retried the next startup
    (fail-open, AC-21). R-N3: latest-version-only -- a user several
    versions behind still receives just `version`'s own note, never a
    backfill/rollup of intermediate versions (there is nothing to
    iterate -- this function only ever sends ONE note, the one for
    `version`). R-N4: DND is never consulted here (send-anyway, a rare
    one-shot). Sequential sends (fine at this scale, R-N2). Never raises.

    R-N1: `version` with no catalog entry at all announces nothing --
    returns immediately, no reads, no writes (AC-22)."""
    if version not in RELEASE_NOTES:
        return

    for user_id in db.active_user_ids():
        try:
            already_announced = db.get_last_announced_version(user_id) == version
        except Exception:
            logger.exception(
                "Reading last_announced_version failed for %s while announcing v%s; "
                "treating as not-yet-announced (fail-open)",
                user_id,
                version,
            )
            already_announced = False
        if already_announced:
            continue

        lang = i18n.resolve_unprompted_language(config, user_pref=user_prefs.stored_language_pref(db, user_id))
        note = get_release_note(version, lang)
        if note is None:
            # Defensive only (R-N1's own authoring convention guarantees
            # every catalog entry carries both languages) -- never crash
            # on a hypothetically partial entry; simply skip this user for
            # this run, they'll be retried (and correctly resolved) next
            # startup exactly like a send failure below.
            continue

        try:
            await channel.send(user_id, note)
        except Exception:
            logger.exception(
                "Failed to send the v%s release announcement to %s; left unmarked for retry next startup",
                version,
                user_id,
            )
            continue

        try:
            db.set_last_announced_version(user_id, version)
        except Exception:
            logger.exception(
                "Marking %s as announced for v%s failed after a successful send; will resend next startup",
                user_id,
                version,
            )
