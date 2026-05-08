"""Conflict policy helpers (P1).

The writer ([writer.py][src/memory/ingestion/writer.py]) already
detects same-(type, slug) divergence at write time and quarantines
both candidates. ``conflict.py`` exposes the *reading* side of that
contract:

- ``select_runtime_winner`` — when retrieval (P2) needs a single
  active item per (type, slug), it picks the most recent ``updated_at``
  among ``status=active`` candidates. ``status=needs_review`` items
  are NEVER returned.

- ``detect_pairs`` — given a flat candidate stream (as produced by
  the extractor), returns the list of same-(type, slug) pairs that
  should NOT be auto-merged. Callers route the loser to
  needs_review.md instead of issuing two writer.write() calls that
  would conflict at the file level.

Auto-merge is deliberately not exposed: the writer's user_correction
branch is the only path that flips between two active items, and it
requires the caller to explicitly mark ``source="user_correction"``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .extractor import Candidate
from .normalizer import normalize_body_for_compare
from .writer import MemoryItem, slugify


@dataclass(frozen=True, slots=True)
class ConflictPair:
    """Two candidates competing for the same (type, slug) slot."""

    type: str
    slug: str
    earlier: Candidate
    later: Candidate
    bodies_equivalent: bool


def detect_pairs(candidates: Sequence[Candidate]) -> list[ConflictPair]:
    """Return same-(type, slug) candidate pairs whose bodies differ.

    Pairs whose bodies normalise to the same string are NOT returned —
    the writer's idempotent-merge branch handles those cleanly. Only
    genuine divergence shows up here.
    """
    by_key: dict[tuple[str, str], list[Candidate]] = {}
    for c in candidates:
        key = (c.type, slugify(c.title))
        by_key.setdefault(key, []).append(c)

    pairs: list[ConflictPair] = []
    for (type_, slug), group in by_key.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                a_norm = normalize_body_for_compare(a.body)
                b_norm = normalize_body_for_compare(b.body)
                bodies_eq = a_norm == b_norm
                if bodies_eq:
                    continue
                pairs.append(ConflictPair(
                    type=type_,
                    slug=slug,
                    earlier=a,
                    later=b,
                    bodies_equivalent=bodies_eq,
                ))
    return pairs


def select_runtime_winner(items: Iterable[MemoryItem]) -> MemoryItem | None:
    """Return the freshest ``status=active`` item or ``None``.

    Items with ``status=needs_review`` are filtered out — the agent
    must not pick a quarantined candidate at runtime even if it's the
    most recent. ``superseded`` items are also dropped (the writer's
    user_correction branch already promoted the replacement).
    """
    candidates = [it for it in items if it.status == "active"]
    if not candidates:
        return None
    return max(candidates, key=lambda it: (it.updated_at, it.created_at))


__all__ = ["ConflictPair", "detect_pairs", "select_runtime_winner"]
