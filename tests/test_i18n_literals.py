"""AC6.2: "Every user-facing string in main.py, reminders.py, and review.py
resolves through the catalog -- a test asserts no hard-coded user-facing
literal remains in those modules."

Static, AST-based: walks each module's source for every `<expr>.send(...)`
call (the Channel.send seam, SPEC.md §8 -- the one place a user-facing
string actually leaves the process) and fails if any argument passed to it
is a hard-coded string literal or an f-string built inline, instead of a
name/call/attribute expression that (in this codebase's convention) routes
through `i18n.t(...)`. This directly enforces AC6.2 without needing to
exercise every code path at runtime, and it re-runs automatically on every
future change to these three files -- catching regressions where a new
confirmation is added with an inline string instead of a catalog lookup.

Deliberately conservative in what it flags (a literal string, or an
f-string, passed straight to `.send(`) rather than trying to prove a
variable's *ultimate* provenance is the catalog -- Vera can hardened this
further (e.g. trace that the variable is assigned from `i18n.t(...)` via a
simple def-use check, or ban string concatenation feeding into `.send(`)
without changing the contract this test enforces today.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "habit_assistant"

# ROADMAP.md v0.6.0 AC6.2's explicit scope: main.py, core/reminders.py,
# core/review.py. (core/health.py's two alert strings were also moved
# into the catalog for consistency, but AC6.2 itself only names these
# three files -- health.py isn't scanned here.)
SCANNED_MODULES = [
    SRC / "main.py",
    SRC / "core" / "reminders.py",
    SRC / "core" / "review.py",
]


def _literal_send_offenders(path: Path) -> list[str]:
    """Return a description string per `.send(...)` call whose argument is
    a hard-coded literal (str/f-string) rather than a name/call/attribute
    expression."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "send":
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        offenders.append(f"line {node.lineno}: literal string passed to .send(): {arg.value!r}")
                    elif isinstance(arg, ast.JoinedStr):
                        offenders.append(f"line {node.lineno}: f-string built inline and passed to .send()")
            self.generic_visit(node)

    Visitor().visit(tree)
    return offenders


@pytest.mark.parametrize("path", SCANNED_MODULES, ids=lambda p: p.name)
def test_no_hardcoded_literal_passed_to_channel_send(path):
    offenders = _literal_send_offenders(path)
    assert offenders == [], f"{path.name} passes a hard-coded literal to .send() (bypasses the i18n catalog): {offenders}"


def test_scanner_itself_actually_detects_a_planted_literal(tmp_path):
    """Meta-test: prove the scanner isn't vacuously passing (e.g. because
    of a typo in the AST walk) by running it against a deliberately
    non-compliant snippet."""
    bad_module = tmp_path / "bad.py"
    bad_module.write_text(
        "async def handler(channel):\n"
        "    await channel.send('a hard-coded literal, not from the catalog')\n",
        encoding="utf-8",
    )
    offenders = _literal_send_offenders(bad_module)
    assert len(offenders) == 1
    assert "hard-coded literal" in offenders[0]


def test_scanner_does_not_flag_catalog_lookups_or_variables(tmp_path):
    """Meta-test: the scanner must not false-positive on the compliant
    pattern every production call site actually uses (`i18n.t(...)`, or a
    variable/expression built from it)."""
    good_module = tmp_path / "good.py"
    good_module.write_text(
        "async def handler(channel, lang, text):\n"
        "    await channel.send(i18n.t('some_id', lang))\n"
        "    await channel.send(text)\n",
        encoding="utf-8",
    )
    assert _literal_send_offenders(good_module) == []
