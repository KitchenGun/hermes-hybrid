"""Memory injection service for the master orchestrator (P2).

Wraps four concerns into a single call so :func:`hermes_master.
HermesMasterOrchestrator._maybe_inject_memory` can stay a thin
adapter:

1. Read the AUTO-GENERATED ``data/memory/USER.md`` and
   ``data/memory/MEMORY.md`` artifacts produced by P0-B's
   :meth:`MemoryCurator.compile_split_memory`.
2. Optionally enrich with retriever top-k items relevant to the
   current user query. Default OFF — the new retrieval path is
   isolated under its own A/B key (``memory_retrieval_v1``) so the
   legacy Phase-21 ``memory_inject`` experiment stays clean.
3. Drop any candidate marked ``needs_review``, ``pii_candidate``,
   ``security_severity in (medium, high)``, or ``superseded``. The
   compile artifacts already exclude these, but the retriever may
   return items that became quarantined after the last compile.
4. Apply ``memory_inject_token_budget`` greedily — compiled context
   first, retriever hits second, lower-priority items dropped.

Hermes_master integration (plan-only in this commit): the real
``_maybe_inject_memory`` will call::

    svc = MemoryInjectionService(...)
    text = svc.compose(query=user_message, settings=settings)
    if text:
        history = [{"role": "system", "content": text}, *history]

Until that wrapper lands, this service is exercised through tests
only — making it safe to ship now without touching the live request
path.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

from .ingestion.retriever import KeywordRetriever, MemoryRetriever, RetrievalHit
from .ingestion.writer import MemoryItem

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InjectionResult:
    text: str
    used_compiled_user: bool
    used_compiled_memory: bool
    retrieval_hits: int
    excluded_quarantined: int
    over_budget_dropped: int
    ab_arm: str


class MemoryInjectionService:
    """Compose the system-prompt prefix from compiled + retrieved memory."""

    def __init__(
        self,
        *,
        compiled_memory_root: Path | str,
        processed_memory_root: Path | str,
        retriever: Optional[MemoryRetriever] = None,
        token_budget: int = 2000,
        retriever_k: int = 5,
        retrieval_enabled: bool = False,
        retrieval_ab_key: str = "memory_retrieval_v1",
    ) -> None:
        self.compiled_memory_root = Path(compiled_memory_root)
        self.processed_memory_root = Path(processed_memory_root)
        self.token_budget = max(0, token_budget)
        self.retriever_k = max(0, retriever_k)
        self.retrieval_enabled = retrieval_enabled
        self.retrieval_ab_key = retrieval_ab_key
        self._retriever = retriever or KeywordRetriever(self.processed_memory_root)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def compose(
        self,
        *,
        query: str,
        ab_arm: str = "control",
        include_compiled: bool = True,
    ) -> InjectionResult:
        """Return the assembled system-prompt prefix.

        ``ab_arm`` reflects the *retrieval* experiment, not the legacy
        Phase-21 ``memory_inject`` arm. The two experiments must remain
        independent — compiled USER.md / MEMORY.md is shown to every
        arm (when ``include_compiled=True``) so the legacy
        experiment's variance is unaffected.

        ``include_compiled`` defaults to True for the canonical caller
        (a future thin :func:`_maybe_inject_memory` wrapper). Set to
        False when the caller already injects compiled USER.md /
        MEMORY.md by another path (today the orchestrator's
        ``_compose_prompt`` already prepends them via
        :meth:`MemoryCurator.read_prompt_prepend`); in that case the
        result text contains only the retrieval supplement and avoids
        double-prepending the same compiled context.
        """
        parts: list[tuple[str, int]] = []  # (text_chunk, est_tokens)

        used_user = False
        used_memory = False
        if include_compiled:
            user_md = self._read(self.compiled_memory_root / "USER.md")
            memory_md = self._read(self.compiled_memory_root / "MEMORY.md")
            used_user = bool(user_md)
            used_memory = bool(memory_md)
            if user_md:
                parts.append((f"## USER (compiled)\n{user_md.strip()}", _est_tokens(user_md)))
            if memory_md:
                parts.append((f"## MEMORY (compiled)\n{memory_md.strip()}", _est_tokens(memory_md)))

        retrieval_hits: list[RetrievalHit] = []
        if (
            self.retrieval_enabled
            and ab_arm == "treatment"
            and self.retriever_k > 0
            and query.strip()
        ):
            try:
                retrieval_hits = self._retriever.search(query, k=self.retriever_k)
            except Exception as exc:  # noqa: BLE001
                _log.warning("memory.retrieval_failed", extra={"err": str(exc)})
                retrieval_hits = []

        excluded = 0
        for hit in retrieval_hits:
            if _should_exclude(hit.item):
                excluded += 1
                continue
            chunk = (
                f"### relevant: {hit.item.title}\n"
                f"{hit.item.body.strip()}"
            )
            parts.append((chunk, _est_tokens(chunk)))

        # Greedy budget application.
        running = 0
        kept: list[str] = []
        dropped = 0
        for text, tokens in parts:
            if running + tokens > self.token_budget and kept:
                dropped += 1
                continue
            kept.append(text)
            running += tokens

        composed = "\n\n".join(kept)
        return InjectionResult(
            text=composed,
            used_compiled_user=used_user,
            used_compiled_memory=used_memory,
            retrieval_hits=len(retrieval_hits) - excluded,
            excluded_quarantined=excluded,
            over_budget_dropped=dropped,
            ab_arm=ab_arm,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _read(path: Path) -> str:
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""


def _should_exclude(item: MemoryItem) -> bool:
    if item.status != "active":
        return True
    if item.pii_candidate:
        return True
    if item.security_severity in ("medium", "high"):
        return True
    return False


def _est_tokens(text: str) -> int:
    """Cheap token estimate (chars / 4). Matches the curator's heuristic."""
    return max(1, len(text) // 4)


__all__ = ["InjectionResult", "MemoryInjectionService"]
