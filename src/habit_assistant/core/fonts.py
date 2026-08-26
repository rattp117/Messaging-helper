"""Thai-capable chart/heatmap font registration (SPEC-v1.9.md §4 Rule 23,
the "font shared surface" -- the prerequisite that unblocks Thai text in
every matplotlib-rendered image: `core/charts.py`'s weekly PNGs, `core/
heatmap.py`'s consistency calendar, and `core/wrapped.py`'s recap card,
which land later as module M4).

**The v1.0.0 known issue this closes**: matplotlib's default font
(DejaVu Sans) has no Thai glyphs, so any Thai text drawn on a chart
rendered as tofu boxes (see PROGRESS.md's own "Known issue" note). Noto
Sans Thai (bundled below, SIL OFL 1.1 -- `assets/fonts/OFL.txt` ships
alongside the TTF per the license's own redistribution requirement) fixes
that.

**Registration is ADDITIVE, not a replacement** (AC6's own load-bearing
requirement): `rcParams["font.family"]` is set to `["DejaVu Sans", "Noto
Sans Thai"]` -- DejaVu Sans stays the PRIMARY (first) family, so every
existing chart's non-Thai content (titles, axis labels, digits, English
month abbreviations) resolves to the exact same font file it always has;
matplotlib's per-glyph fallback (stable since >=3.6, this repo runs 3.11)
only reaches into Noto Sans Thai for a codepoint DejaVu Sans itself lacks
-- i.e. Thai script. `tests/test_v19_shared_surface.py` proves this
empirically: the SAME chart rendered before and after calling
`register_thai_font()` produces byte-identical PNG output when the text
is non-Thai.

**Fail-open, exactly like every other render path in this codebase**
(`core/charts.py`/`core/heatmap.py`'s own "matplotlib missing -> log once,
return None, never raise" contract): a missing bundled font file, or any
other registration failure, is logged once and leaves `rcParams`
untouched -- Thai text then falls back to the pre-v1.9 tofu-box behavior,
never a crash. Callers (`charts.py`/`heatmap.py`, and later `wrapped.py`)
invoke this INSIDE their own `MATPLOTLIB_AVAILABLE` try/except guard, so
this module never has to decide on its own whether matplotlib is
importable at all -- `register_thai_font()` is only ever called once
matplotlib is already known-importable.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Repo layout: src/habit_assistant/core/fonts.py -> repo root is 3 parents
# up (mirrors config.py's own REPO_ROOT derivation).
_REPO_ROOT = Path(__file__).resolve().parents[3]
FONT_PATH = _REPO_ROOT / "assets" / "fonts" / "NotoSansThai-Regular.ttf"

_THAI_FAMILY_NAME = "Noto Sans Thai"
_PRIMARY_FAMILY_NAME = "DejaVu Sans"

_registered = False
_warned_missing = False


def _warn_missing_once() -> None:
    global _warned_missing
    if not _warned_missing:
        logger.warning(
            "Noto Sans Thai font not found at %s; Thai text in charts/heatmap/wrapped-card "
            "images will render as tofu boxes until it is restored",
            FONT_PATH,
        )
        _warned_missing = True


def register_thai_font() -> None:
    """Idempotent: a second (or Nth) call from `charts.py`/`heatmap.py`/
    (later) `wrapped.py` is a cheap no-op once registration has already
    succeeded once in this process. `addfont`s the bundled TTF into
    matplotlib's font manager, then sets `rcParams["font.family"] =
    ["DejaVu Sans", "Noto Sans Thai"]` (SPEC-v1.9.md Rule 23, verbatim --
    DejaVu Sans first/primary). No-op if already registered, if the
    bundled font file is missing (logged once), or if matplotlib itself
    is unavailable (defensive only -- every real caller in this codebase
    already gates this call behind its own `MATPLOTLIB_AVAILABLE` check)."""
    global _registered
    if _registered:
        return

    try:
        import matplotlib
        from matplotlib import font_manager
    except ImportError:
        return  # matplotlib unavailable -- nothing to register against.

    if not FONT_PATH.exists():
        _warn_missing_once()
        return

    try:
        font_manager.fontManager.addfont(str(FONT_PATH))
        matplotlib.rcParams["font.family"] = [_PRIMARY_FAMILY_NAME, _THAI_FAMILY_NAME]
        _registered = True
    except Exception:
        logger.exception(
            "Failed to register Noto Sans Thai font; Thai text in charts/heatmap/wrapped-card "
            "images will render as tofu boxes"
        )
