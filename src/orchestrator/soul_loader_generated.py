"""W4 — SOUL injection helper.

Reads profiles/SOUL.generated.md (mtime-cached) and returns a system-prompt
fragment. Called from src/orchestrator/hermes_master.py:_compose_prompt()
inside the W4 marker block.

Honors HERMES_DISABLE_GROWTH_BLOCKS=true (caller must check; this module
itself is import-safe).
"""
from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SOUL_PATH = _REPO_ROOT / "profiles" / "SOUL.generated.md"

_cache: tuple[float, str] | None = None


def _load(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def compose_soul_block(path: Path | None = None) -> str:
    """Return the SOUL prose wrapped in a header. Empty when file missing.

    Cached by mtime so repeated calls within one process don't re-read.
    """
    if os.environ.get("HERMES_DISABLE_GROWTH_BLOCKS"):
        return ""

    p = path or _SOUL_PATH
    if not p.exists():
        return ""

    global _cache
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return ""

    if _cache is not None and _cache[0] == mtime:
        return _cache[1]

    body = _load(p).strip()
    if not body:
        _cache = (mtime, "")
        return ""

    block = "## Agent SOUL (voice / style)\n\n" + body
    _cache = (mtime, block)
    return block


__all__ = ["compose_soul_block"]
