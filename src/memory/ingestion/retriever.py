"""Top-k retrieval over processed_memory (P2).

The injection service ([injection.py][src/memory/injection.py]) calls
this to enrich the compiled USER.md / MEMORY.md context with
on-demand items relevant to the current user request. The contract is
deliberately narrow:

- Input: a free-text query and a result count ``k``.
- Output: a list of :class:`MemoryItem` ordered by descending score.

Two implementations ship in v1:

- :class:`KeywordRetriever` reads processed_memory directly and ranks
  items by substring overlap with query tokens. No external
  dependency. Filters out non-active items (the runtime-winner
  contract from :mod:`conflict`).
- An embedding-backed retriever is left as plan-only — Ollama bge-m3
  is already wired in :mod:`src.memory.embedding`; the LikeBackend
  approach in :mod:`src.memory.sqlite` is the reference pattern.

The Protocol exists so callers can swap backends without depending
on a concrete class.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .conflict import select_runtime_winner
from .writer import MemoryItem, TYPE_TO_FILE, parse_processed_file


@runtime_checkable
class MemoryRetriever(Protocol):
    def search(self, query: str, k: int = 5) -> list["RetrievalHit"]: ...


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    item: MemoryItem
    score: float


# ---------------------------------------------------------------------------
# Keyword backend
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    if not text:
        return set()
    return {t.casefold() for t in _TOKEN_RE.findall(text) if len(t) > 1}


class KeywordRetriever:
    """Substring / token-overlap retriever over data/processed_memory/*.md.

    Items with status != "active" are filtered out (matches the
    runtime-winner policy in :func:`conflict.select_runtime_winner`).
    Quarantined / superseded items are NOT exposed to retrieval.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _load_active(self) -> list[MemoryItem]:
        items: list[MemoryItem] = []
        for fname in TYPE_TO_FILE.values():
            path = self.root / fname
            if not path.exists():
                continue
            for it in parse_processed_file(path.read_text(encoding="utf-8")):
                if it.status != "active":
                    continue
                if it.pii_candidate or it.security_severity in ("medium", "high"):
                    continue
                items.append(it)
        # If multiple active items collide on (type, slug), pick the freshest.
        from .writer import slugify
        by_key: dict[tuple[str, str], list[MemoryItem]] = {}
        for it in items:
            by_key.setdefault((it.type, slugify(it.title)), []).append(it)
        out: list[MemoryItem] = []
        for group in by_key.values():
            winner = select_runtime_winner(group)
            if winner is not None:
                out.append(winner)
        return out

    def search(self, query: str, k: int = 5) -> list[RetrievalHit]:
        if not query.strip() or k <= 0:
            return []
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        hits: list[RetrievalHit] = []
        for it in self._load_active():
            haystack_tokens = _tokens(it.title) | _tokens(it.body) | set(t.casefold() for t in it.tags)
            overlap = query_tokens & haystack_tokens
            if not overlap:
                continue
            score = len(overlap) / max(1, len(query_tokens))
            # small bias for failure_pattern + decision (more useful in injection)
            if it.type in ("failure_pattern", "decision"):
                score += 0.1
            hits.append(RetrievalHit(item=it, score=score))
        hits.sort(key=lambda h: (-h.score, -len(h.item.body)))
        return hits[:k]


__all__ = [
    "MemoryRetriever",
    "RetrievalHit",
    "KeywordRetriever",
]
