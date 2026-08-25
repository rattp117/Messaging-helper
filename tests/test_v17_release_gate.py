"""SPEC-v1.7.md release-gate pass (Vera, final verification before v1.7.0
ships): probes explicitly requested by Archi that are NOT already covered
by `tests/test_v17_integration.py` (the integration track's own file),
`tests/test_habitdef.py` / `tests/test_v17_habitdef_gaps.py` (`habitdef`
track), or `tests/test_v17_isolation_sweep.py` (`sweep` track):

1. The provider FALLBACK path inside `handle_inbound_message` (called with
   no `provider`, e.g. the `--dry-run` CLI shape at `main.py:1305-1323`) --
   does `/addhabit`/`/delhabit` through that path still write correctly and
   invalidate safely with no shared cache to invalidate?
2. Menu regression at full release-gate precision: exactly 16 public
   commands in both languages, `/addhabit`/`/delhabit` LAST (matching this
   codebase's own established "each release appends its own commands at
   the end of the chain" convention), and `/help` lists both bilingually.
3. A single continuous end-to-end lifecycle through REAL dispatch:
   addhabit -> preparse log (zero LLM) -> `/target` on the custom habit ->
   dashboard/heatmap/records/trends/habits/history all pick it up ->
   `/delhabit` archives it (has history) -> active surfaces stop showing
   it -> plain `/history` still lists the past entry (SPEC-v1.7.md R-C2's
   own "historical entries remain visible in /history") -> id stays
   reserved -> a second, log-free habit's `/delhabit` hard-deletes and
   frees its id for immediate reuse.
4. `RELEASE_NOTES["1.7.0"]` readiness -- NOT previously covered by any
   committed test (only by Luna's own manual smoke script per
   IMPL-v1.7-shared.md); `announce_release` is exercised directly against
   the literal string `"1.7.0"` (mirrors `tests/test_announce.py`'s own
   `KNOWN_VERSION` convention) so this is proven WITHOUT bumping
   `__init__.py:__version__`.
5. AC-5 (byte-identical) and AC-6 (Thai-numeral/full-width-digit preparse
   lock) re-checked one final time on the finished, fully-integrated tree.

Live-environment rule (unchanged from every other integration-adjacent
file in this suite): every DB here is a scratch `tmp_path` SQLite file.
Nothing in this file ever opens `data/habits.db`, and no real Telegram/
Ollama call is made."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import announce, i18n, release_notes
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.main import handle_inbound_message
from habit_assistant.storage.db import Database

OWNER = "1001"
MEMBER = "2002"


# ===========================================================================
# Shared async_main harness -- own copy, mirrors tests/test_v16_integration.
# py + tests/test_v17_integration.py's own identical "Section B" convention
# (each integration-adjacent file keeps its own rather than importing
# another test file's fixtures).
# ===========================================================================


class _StopAfterSchedulerStart(Exception):
    pass


class _FakeScheduler:
    def __init__(self, *args, **kwargs):
        self.jobs: dict[str, object] = {}

    def add_job(self, func, trigger=None, args=None, id=None, replace_existing=True, **kwargs):
        self.jobs[id] = SimpleNamespace(func=func, trigger=trigger, args=args, id=id)

    def start(self):
        pass

    def shutdown(self, wait=False):
        pass


class _FakeOllamaClient:
    responses: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def chat_text(self, system_prompt, user_prompt):
        return "noted"

    async def chat_json(self, system_prompt, user_prompt, json_schema, valid_categories):
        if _FakeOllamaClient.responses:
            return _FakeOllamaClient.responses.pop(0)
        return json.dumps({"category": "unknown", "value": None, "confidence": 0.1})

    async def probe_schema_support(self, *args, **kwargs) -> dict:
        return {}

    async def aclose(self) -> None:
        pass


class _ScriptedChannel(Channel):
    last_instance: "_ScriptedChannel | None" = None
    script: list[tuple] = []

    def __init__(self, *args, **kwargs) -> None:
        self.sent: list[tuple[str, str]] = []
        self.images: list[tuple[str, bytes, str]] = []
        self.set_my_commands_calls: list[dict] = []
        self.pinned: dict[str, str] = {}
        self.edits: list[tuple[str, str, str]] = []
        self._next_msg_id = 8000
        _ScriptedChannel.last_instance = self

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    async def send_actionable(self, chat_id: str, text: str, buttons) -> None:
        self.sent.append((chat_id, text))

    async def send_and_pin(self, chat_id: str, text: str) -> str | None:
        self._next_msg_id += 1
        msg_id = str(self._next_msg_id)
        self.pinned[chat_id] = msg_id
        self.sent.append((chat_id, text))
        return msg_id

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> bool:
        self.edits.append((chat_id, message_id, text))
        return self.pinned.get(chat_id) == message_id

    async def unpin(self, chat_id: str, message_id: str) -> None:
        if self.pinned.get(chat_id) == message_id:
            del self.pinned[chat_id]

    async def set_my_commands(self, commands, *, scope_chat_id=None) -> None:
        # SPEC-v1.8.md R-D2: only records the default (global) menu
        # registration -- see test_discoverability.py's identical fake for
        # the full rationale.
        if scope_chat_id is None:
            self.set_my_commands_calls.append(commands)

    def sent_to(self, chat_id: str) -> list[str]:
        return [text for cid, text in self.sent if cid == chat_id]

    async def run(self, on_message, on_callback=None) -> None:
        for step in _ScriptedChannel.script:
            _, chat_id, text, display_name = step
            await on_message(chat_id, text, display_name)
        raise _StopAfterSchedulerStart()

    async def aclose(self) -> None:
        pass


async def _run(monkeypatch, config, script, owner_chat_id=OWNER, responses=None):
    from habit_assistant import main as main_module
    from habit_assistant.core import access as access_module

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module, "load_secrets",
        lambda: SimpleNamespace(telegram_bot_token="fake-token", telegram_chat_id=owner_chat_id),
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", _ScriptedChannel)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClient)
    monkeypatch.setattr(main_module, "__version__", "0.0.0-test")
    monkeypatch.setattr(access_module, "__version__", "0.0.0-test")
    _ScriptedChannel.last_instance = None
    _ScriptedChannel.script = script
    _FakeOllamaClient.responses = list(responses or [])

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)
    return _ScriptedChannel.last_instance


def _seed_users(tmp_path, *, member: bool = False) -> None:
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    if member:
        seed_db.upsert_user(MEMBER, role="member", status="active")
    seed_db.close()


# ===========================================================================
# 1. Provider FALLBACK path (`handle_inbound_message` called with NO
#    `provider` -- the exact shape the `--dry-run` CLI branch uses at
#    main.py:1305-1323): addhabit/delhabit still write correctly and
#    invalidate safely, with zero shared cache across the calls.
# ===========================================================================


async def test_addhabit_through_the_no_provider_fallback_path_writes_and_confirms(tmp_path):
    """Mirrors the `--dry-run` CLI shape exactly: no `provider` kwarg at
    all. `main.py:801`'s `active_provider = provider if provider is not
    None else RegistryProvider(config, db)` must build a fresh one-off
    provider, and `execute_addhabit` must still validate + write + confirm
    correctly through it."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    db = Database(tmp_path / "habits.db")
    db.upsert_user(OWNER, role="owner", status="active")
    try:
        await handle_inbound_message(
            "/addhabit id=pages|type=numeric|en=pages|unit=pages|goal=20",
            db=db,
            llm=_FakeOllamaClient(),
            channel=None,
            config=config,
            dry_run=True,
            user_id=OWNER,
            # deliberately NO `provider=` kwarg, NO `registry=` kwarg.
        )
        row = db.get_user_habit(OWNER, "pages")
        assert row is not None and row["archived_at"] is None
        assert db.count_active_user_habits(OWNER) == 1
    finally:
        db.close()


async def test_two_sequential_no_provider_calls_still_see_each_others_writes(tmp_path, capsys):
    """Two SEPARATE `handle_inbound_message` calls, each with its own
    fresh one-off `RegistryProvider` (no shared cache between them, exactly
    like two separate `--dry-run` process invocations). Since
    `execute_addhabit`'s validation always reads `provider.for_user(user_
    id)` -> `HabitRegistry.for_user(config, db, user_id)` -> a REAL DB
    read, the second call must still see the first call's write (a
    duplicate-id rejection) even though nothing was cached across the two
    calls -- proving correctness does not depend on the cache, only the
    AC-3 "no restart" PERFORMANCE benefit does."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    db = Database(tmp_path / "habits.db")
    db.upsert_user(OWNER, role="owner", status="active")
    try:
        await handle_inbound_message(
            "/addhabit id=pages|type=numeric|en=pages|unit=pages|goal=20",
            db=db, llm=_FakeOllamaClient(), channel=None, config=config,
            dry_run=True, user_id=OWNER,
        )
        capsys.readouterr()  # discard the first confirmation
        await handle_inbound_message(
            "/addhabit id=pages|type=numeric|en=pages again|unit=pages|goal=20",
            db=db, llm=_FakeOllamaClient(), channel=None, config=config,
            dry_run=True, user_id=OWNER,
        )
        reply = capsys.readouterr().out
        assert "🤔" in reply  # duplicate-id rejection, no crash
        assert db.count_active_user_habits(OWNER) == 1  # still exactly one row
    finally:
        db.close()


async def test_delhabit_through_the_no_provider_fallback_path_archives_and_hard_deletes(tmp_path, capsys):
    """Both smart-delete branches (R-C2), each through a fresh one-off
    provider with nothing cached beforehand -- `provider.invalidate(user_
    id)` on a cache that never held an entry for that user must be a safe
    no-op (`dict.pop(user_id, None)`), never a KeyError.

    NOTE on `dry_run` semantics discovered while writing this test (pre-
    existing, NOT a v1.7 regression): for a COMMAND (`/addhabit`/
    `/delhabit`/etc.), `dry_run=True` still performs the real write and
    `print(reply)`s the same confirmation a live channel would have been
    sent (main.py's per-kind `if dry_run: print(reply); return` branches).
    But for a PLAIN LOG message (free text, no recognized command),
    `dry_run=True` prints the raw parsed `asdict(result)` and returns
    BEFORE the insert (main.py:1025-1027) -- it never writes a `logs` row
    at all. So the log step below (needed to give "pages" history, to
    exercise the archive branch rather than hard-delete) uses
    `dry_run=False` with a minimal fake channel instead."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    db = Database(tmp_path / "habits.db")
    db.upsert_user(OWNER, role="owner", status="active")
    try:
        # Hard-delete branch: no logs.
        await handle_inbound_message(
            "/addhabit id=temp|type=numeric|en=temp|unit=u",
            db=db, llm=_FakeOllamaClient(), channel=None, config=config,
            dry_run=True, user_id=OWNER,
        )
        capsys.readouterr()
        await handle_inbound_message(
            "/delhabit temp", db=db, llm=_FakeOllamaClient(), channel=None, config=config,
            dry_run=True, user_id=OWNER,
        )
        reply = capsys.readouterr().out
        assert reply.startswith("🗑️")
        assert db.get_user_habit(OWNER, "temp") is None  # id freed, row gone entirely

        # Archive branch: give a second custom habit a log first (via a
        # real registry built for this write, since the no-provider/no-
        # registry `handle_inbound_message` call for a PLAIN log message
        # needs an explicit per-user registry to recognize "pages" at
        # all -- unlike addhabit/delhabit, which always resolve their own
        # registry internally regardless of the `registry=` kwarg).
        await handle_inbound_message(
            "/addhabit id=pages|type=numeric|en=pages|unit=pages|goal=20",
            db=db, llm=_FakeOllamaClient(), channel=None, config=config,
            dry_run=True, user_id=OWNER,
        )
        capsys.readouterr()
        live_registry = HabitRegistry.for_user(config, db, OWNER)
        log_channel = _AnnounceFakeChannel()
        await handle_inbound_message(
            "20 pages", db=db, llm=_FakeOllamaClient(), channel=log_channel, config=config,
            dry_run=False, user_id=OWNER, registry=live_registry,
        )
        log_reply = log_channel.sent_to(OWNER)[0]
        assert "20 pages logged" in log_reply
        assert db.count_logs_for(OWNER, "pages") == 1

        capsys.readouterr()
        await handle_inbound_message(
            "/delhabit pages", db=db, llm=_FakeOllamaClient(), channel=None, config=config,
            dry_run=True, user_id=OWNER,
        )
        archive_reply = capsys.readouterr().out
        assert archive_reply.startswith("🗄️")
        row = db.get_user_habit(OWNER, "pages")
        assert row is not None and row["archived_at"] is not None  # archived, not deleted
    finally:
        db.close()


# ===========================================================================
# 2. Menu regression at full precision + bilingual /help (R-A2).
# ===========================================================================


async def test_public_menu_is_exactly_18_commands_log_routine_last_both_languages(tmp_path, monkeypatch):
    # RENAMED (SPEC-v1.8.md's own integration step, mirrors this file's
    # own established "each release renames + extends this test" pattern):
    # `log`/`routine` (modules `quicklog`/`routines`, R-D2) now append
    # after `addhabit`/`delhabit` (16 -> 18 total) -- the owner-only
    # commands (including `audit`) are registered on a SEPARATE,
    # owner-chat-scoped menu instead (R-D2), which `set_my_commands_
    # calls[0]` (the default/global registration) never captures.
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _seed_users(tmp_path)
    channel = await _run(monkeypatch, config, script=[])

    registered = channel.set_my_commands_calls[0]
    assert set(registered.keys()) == {"en", "th"}
    expected_tail = ["log", "routine"]
    for lang, entries in registered.items():
        names = [name for name, _desc in entries]
        assert len(names) == 18, f"{lang}: {names}"
        assert len(set(names)) == 18, f"{lang}: duplicate command name(s)"
        # Established convention: each release appends its OWN new
        # commands at the end of the chain (see main.py's own
        # command_menu comment) -- v1.8.0's log/routine must be the last
        # two entries, in that order, after v1.7.0's addhabit/delhabit.
        assert names[-2:] == expected_tail, f"{lang}: {names}"
        assert names[-4:-2] == ["addhabit", "delhabit"], f"{lang}: {names}"
        assert names[-5] == "trends", f"{lang}: {names}"
        assert not (set(names) & {"approve", "block", "users", "invite", "audit"})


async def test_help_text_lists_addhabit_and_delhabit_bilingually(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [
        ("message", OWNER, "/help", None),
        ("message", OWNER, "/lang th", None),
        ("message", OWNER, "/help", None),
    ]
    channel = await _run(monkeypatch, config, script)

    help_en = channel.sent_to(OWNER)[0]
    help_th = channel.sent_to(OWNER)[-1]
    for cmd in ("/addhabit", "/delhabit"):
        assert cmd in help_en
        assert cmd in help_th


# ===========================================================================
# 3. Full continuous lifecycle through REAL dispatch: create -> preparse
#    log -> /target -> dashboard/heatmap/records/trends/habits/history all
#    pick it up -> archive (has history) -> active surfaces drop it ->
#    plain /history still lists the past entry -> id reserved -> a
#    SEPARATE log-free habit's /delhabit hard-deletes + frees its id.
# ===========================================================================


async def test_full_custom_habit_lifecycle_through_real_dispatch(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _seed_users(tmp_path)

    script = [
        ("message", OWNER, "/dashboard on", None),
        # No Thai field here deliberately: `en=`/`th=` text is echoed into
        # the command message itself, and `i18n.resolve_reply_language`'s
        # "auto" mode detects the reply language from the INBOUND text --
        # a Thai `th=` value in the /addhabit line would flip every reply
        # in this script to Thai (confirmed while drafting this test), an
        # unrelated language-detection interaction this lifecycle test
        # isn't trying to probe (that's `test_help_text_lists_addhabit_
        # and_delhabit_bilingually`'s own job).
        ("message", OWNER, "/addhabit id=pages|type=numeric|en=pages|unit=pages|goal=10", None),
        ("message", OWNER, "20 pages", None),  # zero-LLM preparse instant log
        ("message", OWNER, "/target pages 25", None),
        ("message", OWNER, "/habits", None),
        ("message", OWNER, "/records", None),
        ("message", OWNER, "/trends", None),
        ("message", OWNER, "/heatmap pages", None),
        ("message", OWNER, "/history pages", None),
        ("message", OWNER, "/history", None),
        # Now archive it (it has history -- the "20 pages" log above).
        ("message", OWNER, "/delhabit pages", None),
        ("message", OWNER, "/habits", None),
        ("message", OWNER, "/records", None),
        ("message", OWNER, "/history pages", None),  # filtered-by-archived-id
        ("message", OWNER, "/history", None),  # plain, unfiltered
        ("message", OWNER, "/addhabit id=pages|type=numeric|en=pages again|unit=pages", None),  # id reserved
        # A SEPARATE, log-free habit: /delhabit hard-deletes, id freed.
        ("message", OWNER, "/addhabit id=scratch|type=numeric|en=scratch|unit=u", None),
        ("message", OWNER, "/delhabit scratch", None),
        ("message", OWNER, "/addhabit id=scratch|type=numeric|en=scratch reused|unit=u", None),
    ]
    channel = await _run(monkeypatch, config, script)
    sent = channel.sent_to(OWNER)

    # sent[0] is the live board's own initial render (send_and_pin fires
    # before the "Live dashboard turned on" confirmation) -- everything
    # else below is offset by that one extra send.
    dashboard_on_reply = sent[1]
    assert "Live dashboard turned on" in dashboard_on_reply

    addhabit_reply = sent[2]
    assert 'Added "pages"' in addhabit_reply

    log_reply = sent[3]
    assert "20 pages logged" in log_reply
    assert "clarifying" not in log_reply.lower()

    target_reply = sent[4]
    assert "pages" in target_reply.lower() and "25" in target_reply

    habits_reply_1 = sent[5]
    assert "pages" in habits_reply_1

    records_reply_1 = sent[6]
    assert "pages" in records_reply_1

    trends_reply_1 = sent[7]
    assert "pages" in trends_reply_1

    heatmap_reply_1 = sent[8]  # matplotlib installed -> PNG send_image ->
    # base Channel.send_image default sends the caption as plain text.
    assert "pages" in heatmap_reply_1

    history_filtered_1 = sent[9]
    assert "pages" in history_filtered_1
    assert "20" in history_filtered_1

    history_plain_1 = sent[10]
    assert "pages" in history_plain_1
    assert "20 pages" in history_plain_1  # type-specific description, pre-archive

    archive_reply = sent[11]
    assert archive_reply.startswith("🗄️")
    assert "Archived" in archive_reply

    habits_reply_2 = sent[12]
    assert "pages" not in habits_reply_2  # active surface: gone

    records_reply_2 = sent[13]
    assert "pages" not in records_reply_2  # active surface: gone

    # Filtered /history <archived-id>: registry.get("pages") is now None
    # (archived rows are excluded from the active registry, R-G1/AC-2),
    # so render_history's OWN unresolved-category guard fires -- the
    # SAME `history_invalid_habit` reply an id that was NEVER created
    # would produce. This is a real, previously-untested consequence of
    # R-C2's "gone from active surfaces" wording extending to /history's
    # OWN filter argument, even though the plain (unfiltered) /history
    # right below still lists the row. Not a spec violation (R-C2 only
    # promises the entries stay visible in /history, not that the FILTER
    # keeps working) -- documented, not failed, but worth Archi/Luna
    # knowing about explicitly.
    history_filtered_2 = sent[14]
    assert history_filtered_2 == i18n.t(
        "history_invalid_habit", "en", habit_id="pages", habit_list="water, stretch, diary"
    )

    # Plain (unfiltered) /history: the row IS still present (R-C2's own
    # "historical entries remain visible in /history") -- but its
    # description DEGRADES from the pre-archive "20 pages" phrasing to
    # the generic `describe_log_generic` fallback ("pages entry"),
    # because `undo_ui.describe_log` also resolves the habit through the
    # (now-archived-excluding) registry. A second, previously-untested
    # finding: the raw quoted original message ("20 pages") still shows,
    # so the value is not TRULY lost to the user, just no longer
    # formatted with its unit/label.
    history_plain_2 = sent[15]
    assert "pages" in history_plain_2  # still listed -- row survives archiving
    assert "pages entry" in history_plain_2  # degraded generic description
    assert "20 pages logged" not in history_plain_2  # NOT the pre-archive phrasing
    assert '"20 pages"' in history_plain_2  # raw quoted message still carries the value

    readd_rejected = sent[16]
    assert readd_rejected.startswith("🤔")
    assert "reserved" in readd_rejected

    # Hard-delete + immediate id reuse (separate, log-free habit).
    scratch_add = sent[17]
    assert scratch_add.startswith("✅")
    scratch_del = sent[18]
    assert scratch_del.startswith("🗑️")
    scratch_readd = sent[19]
    assert scratch_readd.startswith("✅")
    assert "scratch reused" in scratch_readd

    db = Database(tmp_path / "habits.db")
    try:
        row = db.get_user_habit(OWNER, "pages")
        assert row is not None and row["archived_at"] is not None
        assert db.count_active_user_habits(OWNER) == 1  # only "scratch" (reused) is active
        assert db.get_user_habit(OWNER, "scratch") is not None
    finally:
        db.close()


# ===========================================================================
# 4. RELEASE_NOTES["1.7.0"] readiness -- not covered by any existing
#    committed test (AC-8 was only manually smoke-tested per IMPL-v1.7-
#    shared.md). Exercises the REAL `announce_release` against the literal
#    "1.7.0", never touching __init__.py:__version__.
# ===========================================================================

KNOWN_V17 = "1.7.0"


class _AnnounceFakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised here")

    def sent_to(self, chat_id: str) -> list[str]:
        return [text for cid, text in self.sent if cid == chat_id]


def test_v170_ships_as_a_release_notes_catalog_entry():
    assert KNOWN_V17 in release_notes.RELEASE_NOTES


def test_get_release_note_returns_both_languages_for_v170():
    en = release_notes.get_release_note(KNOWN_V17, "en")
    th = release_notes.get_release_note(KNOWN_V17, "th")
    assert en is not None and "1.7.0" in en and "/addhabit" in en
    assert th is not None and "1.7.0" in th and "/addhabit" in th
    assert en != th


async def test_announce_release_picks_up_v170_for_an_active_user_and_is_idempotent(tmp_path):
    """Directly exercises `core/announce.py:announce_release` (the REAL
    startup-wired function, `main.py:1488`'s own call site) with the
    literal string "1.7.0" -- proves the announce machinery WOULD pick up
    the v1.7.0 note the instant `__version__` is bumped at Phase 6.5,
    without this test ever touching that bump itself."""
    config = Config()
    db = Database(tmp_path / "habits.db")
    db.upsert_user(OWNER, role="owner", status="active")
    channel = _AnnounceFakeChannel()
    try:
        await announce.announce_release(db, channel, config, KNOWN_V17)
        sent = channel.sent_to(OWNER)
        assert len(sent) == 1
        assert "1.7.0" in sent[0]
        assert db.get_last_announced_version(OWNER) == KNOWN_V17

        # Idempotent: a second announce_release call for the SAME version
        # sends nothing more (R-N3, already-caught-up).
        await announce.announce_release(db, channel, config, KNOWN_V17)
        assert len(channel.sent_to(OWNER)) == 1
    finally:
        db.close()


# ===========================================================================
# 5. AC-5 (byte-identical) + AC-6 (Thai-numeral/full-width lock), one
#    final re-check on the finished, fully-integrated tree.
# ===========================================================================


async def test_ac5_owner_water_confirmation_byte_identical_via_real_dispatch(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _seed_users(tmp_path)
    channel = await _run(monkeypatch, config, script=[("message", OWNER, "500ml", None)])
    reply = channel.sent_to(OWNER)[0]
    assert reply == "✅ 500 ml logged — today 500 / 2500 ml (20%)"


async def test_ac5_owner_water_confirmation_byte_identical_via_the_no_provider_fallback_path(tmp_path):
    """Same AC-5 gate, re-checked at the OTHER real call shape: a direct
    `handle_inbound_message` call with no `provider` at all, `registry`
    built exactly the way the `--dry-run` CLI branch builds its own
    (`HabitRegistry.from_config(config)`, main.py:1285).

    Uses `dry_run=False` with a minimal fake channel (not the literal
    `--dry-run=True` CLI flag) because `dry_run=True` short-circuits a
    PLAIN LOG message to `print(asdict(result))` BEFORE the confirmation
    is even built or the row inserted (main.py:1025-1027, pre-existing,
    unrelated to v1.7) -- that path was never going to render a
    confirmation string to compare byte-for-byte in the first place. This
    still exercises the exact `registry=HabitRegistry.from_config(config)`
    shape the CLI path uses; only the send-vs-print tail differs."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    db = Database(tmp_path / "habits.db")
    db.upsert_user(OWNER, role="owner", status="active")
    channel = _AnnounceFakeChannel()
    try:
        registry = HabitRegistry.from_config(config)
        await handle_inbound_message(
            "500ml", db=db, llm=_FakeOllamaClient(), channel=channel, config=config,
            dry_run=False, registry=registry, user_id=OWNER,
        )
        reply = channel.sent_to(OWNER)[0]
        assert reply == "✅ 500 ml logged — today 500 / 2500 ml (20%)"
    finally:
        db.close()


async def test_ac6_thai_numeral_and_full_width_digit_lock_final_recheck(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _seed_users(tmp_path)
    script = [
        ("message", OWNER, "๕๐๐ มล.", None),  # Thai numerals
        ("message", OWNER, "５００ml", None),  # full-width digits
    ]
    channel = await _run(monkeypatch, config, script)
    sent = channel.sent_to(OWNER)
    assert len(sent) == 2
    for reply in sent:
        assert "500" in reply
        assert "clarifying" not in reply.lower()

    db = Database(tmp_path / "habits.db")
    try:
        rows = db.logs_between(OWNER, "2000-01-01T00:00:00", "2100-01-01T00:00:00")
        assert len(rows) == 2
        assert all(r["category"] == "water" and r["value_num"] == 500.0 for r in rows)
    finally:
        db.close()
