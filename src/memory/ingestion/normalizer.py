"""Item normalisation utilities used by writer / extractor / curator.

Three responsibilities:

1. ``normalize_body_for_compare`` — collapse whitespace and strip trailing
   punctuation so two bodies that differ only in formatting hash to the
   same string. Used by :class:`ProcessedMemoryWriter` to detect
   idempotent merges.

2. ``claude_to_hermes`` — rewrite Claude-specific syntax into the form
   Hermes processed_memory standardises on:
     - ``@coder`` mentions stay verbatim (Hermes already supports the
       same ``@handle`` syntax via Phase 9), but a leading ``@@`` is
       collapsed to ``@``.
     - Slash skill invocations (``/foo arg``) become ``[skill: foo]``
       so the literal slash doesn't trip Discord/Slack autocompletion
       when the body is later echoed back.
     - File path links of the shape ``[label](path:42)`` are preserved
       (the format is already Hermes-canonical).
     - Triple-backtick fences are kept verbatim.

3. ``dedupe_items`` — drop later items that share an ``item_id`` with an
   earlier item. Used after batch extraction or after merging two
   processed_memory snapshots.
"""
from __future__ import annotations

import re
from typing import Iterable

from .writer import MemoryItem

# ---------------------------------------------------------------------------
# Body normalisation
# ---------------------------------------------------------------------------
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_body_for_compare(text: str) -> str:
    """Lowercase, strip all punctuation, collapse whitespace.

    Used for equality comparison only — never as the persisted body.
    Mirrors the inline ``_normalize_for_compare`` in
    :mod:`src.memory.ingestion.writer` so writer and normalizer agree
    on what counts as the same body content. Two bodies that normalise
    to the same string are treated as substantively identical by the
    writer's idempotent-merge branch.
    """
    if not text:
        return ""
    base = text.casefold()
    base = _PUNCT_RE.sub(" ", base)
    base = _WS_RE.sub(" ", base).strip()
    return base


# ---------------------------------------------------------------------------
# Claude → Hermes syntax
# ---------------------------------------------------------------------------
_DOUBLE_AT_RE = re.compile(r"@@(?=\w)")
_SLASH_SKILL_RE = re.compile(
    r"(?<!\S)/([a-z][a-z0-9_-]{1,40})(?=\s|$)",
    re.IGNORECASE,
)


def claude_to_hermes(text: str) -> str:
    """Translate Claude-specific syntax into Hermes processed_memory form."""
    if not text:
        return text
    result = _DOUBLE_AT_RE.sub("@", text)
    # Skip transformation inside fenced code blocks. Naïve: split on ```
    # and only rewrite even-indexed segments (outside fences).
    out_segments: list[str] = []
    parts = result.split("```")
    for i, segment in enumerate(parts):
        if i % 2 == 0:
            segment = _SLASH_SKILL_RE.sub(r"[skill: \1]", segment)
        out_segments.append(segment)
    return "```".join(out_segments)


# ---------------------------------------------------------------------------
# Item-level dedup
# ---------------------------------------------------------------------------
def dedupe_items(items: Iterable[MemoryItem]) -> list[MemoryItem]:
    """Return items in input order, dropping later duplicates by item_id.

    "Earlier" in the input list wins. Callers that need a "latest wins"
    policy should reverse the input first or rely on the writer's
    update branch instead.
    """
    seen: set[str] = set()
    out: list[MemoryItem] = []
    for item in items:
        if item.item_id in seen:
            continue
        seen.add(item.item_id)
        out.append(item)
    return out


__all__ = [
    "normalize_body_for_compare",
    "claude_to_hermes",
    "dedupe_items",
]
