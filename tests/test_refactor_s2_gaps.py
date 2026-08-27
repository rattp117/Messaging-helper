"""SPEC-REFACTOR.md Stage 2 ("main.py decomposition") own exit-bar checks
that don't fit naturally into any pre-existing test file: no import cycle
(AC6), `main.py` stays a thin entry (< 150 lines, AC6), and
`commands.dispatch` runs exactly once per routed message (AC7).

Every other Stage 2 behavior (byte-identical routing/confirmations, the
monkeypatch-preserving re-export wrappers on `habit_assistant.main`, the
`core/confirmation.py` leaf killing the quicklog mirror) is already
exercised by the pre-existing suite running UNMODIFIED against the new
module layout -- these are the structural checks the spec calls out that
had no prior test to piggyback on.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from habit_assistant.channels.base import Button, Channel
from habit_assistant.config import Config
from habit_assistant.core import commands
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.registry_provider import RegistryProvider
from habit_assistant.core.routing import on_message
from habit_assistant.storage.db import Database

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "habit_assistant"


# ===========================================================================
# AC6 -- no import cycle. The pre-Stage-2 audit's own selling point ("no
# module in src/ imports main ... the split is cycle-safe by construction",
# SPEC-REFACTOR.md rule 9) must still hold now that main.py's logic moved
# into core/app.py, core/jobs.py, core/routing.py, core/confirmation.py.
# ===========================================================================


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC_ROOT.parent)
    parts = rel.with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _internal_imports(path: Path) -> set[str]:
    """Every `habit_assistant.*` name this file imports at module level OR
    inside a function/method body -- a cycle hidden behind a lazy/deferred
    import is still a real dependency-graph cycle for this codebase's own
    "cycle-safe by construction" invariant, even though it wouldn't raise
    `ImportError` at process start."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "habit_assistant" or alias.name.startswith("habit_assistant."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and (node.module == "habit_assistant" or node.module.startswith("habit_assistant.")):
                found.add(node.module)
    return found


def _build_graph() -> dict[str, set[str]]:
    return {_module_name(path): _internal_imports(path) for path in SRC_ROOT.rglob("*.py")}


def _resolve(name: str, graph: dict[str, set[str]]) -> str | None:
    """A dependency may name a package whose `__init__` re-exports symbols
    (e.g. `habit_assistant.core` for `from habit_assistant.core import
    dashboard`) rather than a leaf module file -- walk up to the closest
    ancestor that IS a node in the graph."""
    target = name
    while target not in graph:
        if "." not in target:
            return None
        target = target.rsplit(".", 1)[0]
    return target


def _find_cycle(graph: dict[str, set[str]]) -> list[str] | None:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {m: WHITE for m in graph}
    stack: list[str] = []

    def visit(m: str) -> list[str] | None:
        color[m] = GRAY
        stack.append(m)
        for dep in sorted(graph.get(m, ())):
            target = _resolve(dep, graph)
            if target is None or target == m:
                continue
            if color[target] == GRAY:
                return stack[stack.index(target):] + [target]
            if color[target] == WHITE:
                result = visit(target)
                if result is not None:
                    return result
        stack.pop()
        color[m] = BLACK
        return None

    for m in sorted(graph):
        if color[m] == WHITE:
            result = visit(m)
            if result is not None:
                return result
    return None


def test_no_import_cycle_anywhere_in_src():
    graph = _build_graph()
    cycle = _find_cycle(graph)
    assert cycle is None, f"import cycle detected: {' -> '.join(cycle or [])}"


def test_no_module_in_src_imports_main_except_main_itself():
    """The specific audit finding rule 9 leans on: every module that used
    to be part of `main.py` (`core/app.py`/`core/jobs.py`/
    `core/routing.py`/`core/confirmation.py`) imports only DOWNWARD, never
    back into `main` -- `main.py` is the only file allowed to import
    itself (trivially, it doesn't)."""
    offenders = []
    for path in SRC_ROOT.rglob("*.py"):
        if path == SRC_ROOT / "main.py":
            continue
        imports = _internal_imports(path)
        if any(name == "habit_assistant.main" for name in imports):
            offenders.append(str(path.relative_to(SRC_ROOT.parent)))
    assert offenders == [], f"modules importing habit_assistant.main: {offenders}"


# ===========================================================================
# AC6 -- main.py stays a thin entry (< 150 lines).
# ===========================================================================


def test_main_py_is_a_thin_entry_under_150_lines():
    tree = ast.parse((SRC_ROOT / "main.py").read_text(encoding="utf-8"))
    # AST-verified count (per SPEC-REFACTOR.md §2's own methodology): the
    # highest line number touched by any parsed node, not a raw newline
    # count (which undercounts/overcounts on blank lines or line-ending
    # quirks).
    max_line = max((getattr(node, "end_lineno", 0) or 0) for node in ast.walk(tree))
    assert max_line < 150, f"main.py is {max_line} lines (AST-verified), expected < 150"


# ===========================================================================
# AC7 -- commands.dispatch runs exactly once per routed message (baseline
# 2: on_message's own pre-check dispatch + handle_inbound_message's second,
# redundant one).
# ===========================================================================


class _RecordingChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.actionable: list[tuple[str, str, list[Button]]] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append((chat_id, text))

    async def send_actionable(self, chat_id: str, text: str, buttons: list[Button]) -> None:
        self.actionable.append((chat_id, text, buttons))
        self.sent.append((chat_id, text))

    async def set_my_commands(self, commands, *, scope_chat_id=None) -> None:
        pass

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised -- this test drives on_message directly")


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "ac7.db")


async def test_dispatch_called_exactly_once_per_message_via_on_message(db, monkeypatch):
    owner = "owner-1"
    db.attribute_legacy_to_owner(owner)  # so access.classify(db, owner) == "owner" (passes the gate)

    config = Config.model_validate({})
    channel = _RecordingChannel()
    provider = RegistryProvider(config, db)
    calls = []
    real_dispatch = commands.dispatch

    def counting_dispatch(text, registry):
        calls.append(text)
        return real_dispatch(text, registry)

    monkeypatch.setattr(commands, "dispatch", counting_dispatch)

    from habit_assistant.core.reminders import ReminderState

    class _FakeScheduler:
        def add_job(self, *a, **k):
            pass

    class _FakeHealthMonitor:
        ollama_up = True

    await on_message(
        owner,
        "500ml",
        db=db,
        llm=None,
        channel=channel,
        config=config,
        owner_chat_id=owner,
        provider=provider,
        scheduler=_FakeScheduler(),
        reminder_state=ReminderState(),
        health_monitor=_FakeHealthMonitor(),
    )

    assert calls == ["500ml"], f"commands.dispatch must run exactly once per message (rule 5/AC7), got {calls}"
    assert channel.sent, "the water log should still have been confirmed"
