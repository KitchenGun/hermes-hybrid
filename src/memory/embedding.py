"""Embedding-based memory search — Phase 4.

Wraps a base ``MemoryBackend`` and replaces ``search`` with semantic
matching via ollama's ``/api/embeddings`` endpoint. Falls back to the
underlying backend's LIKE search when the embed call fails (model
missing, ollama down, network error) so a backend swap can never
silently break memory inject.

Embedding cache: per-text in-process LRU. Cheap for typical corpora
(< 1k memos per user) and survives across one bot lifetime. Restart
re-embeds on first use.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections import OrderedDict
from typing import Callable, Iterable

from .base import Memo, MemoryBackend


def embed_text(
    text: str,
    *,
    model: str,
    base_url: str,
    timeout_s: int = 10,
) -> list[float]:
    """Single-shot ollama embed call. Raises on any failure.

    Returns the raw embedding vector — no normalization (cosine handles
    that). Empty input returns ``[]`` without making a request.
    """
    text = (text or "").strip()
    if not text:
        return []
    body = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    url = f"{base_url.rstrip('/')}/api/embeddings"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    vec = data.get("embedding") or []
    if not isinstance(vec, list):
        return []
    return [float(x) for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Empty vectors → 0.0 (no false matches)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


class _LRU:
    """Tiny LRU cache for embedding vectors (OrderedDict-backed)."""

    def __init__(self, capacity: int = 2048):
        self.capacity = capacity
        self._d: OrderedDict[str, list[float]] = OrderedDict()

    def get(self, key: str) -> list[float] | None:
        if key in self._d:
            self._d.move_to_end(key)
            return self._d[key]
        return None

    def put(self, key: str, value: list[float]) -> None:
        self._d[key] = value
        self._d.move_to_end(key)
        while len(self._d) > self.capacity:
            self._d.popitem(last=False)


class EmbeddingMemoryBackend(MemoryBackend):
    """Decorator: delegates save/list/clear to the inner backend, replaces
    ``search`` with embedding similarity. Inner backend's own search is
    used as the LIKE fallback when embeddings can't be obtained."""

    def __init__(
        self,
        inner: MemoryBackend,
        *,
        model: str,
        base_url: str,
        timeout_s: int = 10,
        cache_capacity: int = 2048,
        # Injection point for tests — replace the real HTTP call with a
        # stub that returns a vector synchronously.
        embedder: Callable[[str], list[float]] | None = None,
    ):
        self.inner = inner
        self.model = model
        self.base_url = base_url
        self.timeout_s = timeout_s
        self._cache = _LRU(capacity=cache_capacity)
        self._embedder = embedder

    # ---- pass-through ------------------------------------------------

    async def save(self, user_id: str, text: str) -> Memo:
        return await self.inner.save(user_id, text)

    async def list_memos(self, user_id: str, limit: int = 20) -> list[Memo]:
        return await self.inner.list_memos(user_id, limit=limit)

    async def clear(self, user_id: str) -> int:
        return await self.inner.clear(user_id)

    # ---- search via embeddings --------------------------------------

    def _embed(self, text: str) -> list[float]:
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        if self._embedder is not None:
            vec = self._embedder(text)
        else:
            vec = embed_text(
                text,
                model=self.model,
                base_url=self.base_url,
                timeout_s=self.timeout_s,
            )
        if vec:
            self._cache.put(text, vec)
        return vec

    async def search(
        self, user_id: str, query: str, k: int = 5
    ) -> list[Memo]:
        q = (query or "").strip()
        if not q:
            return []
        # Try query embedding first. If it fails, fall back to inner's
        # LIKE search — memory should never be silently empty.
        try:
            q_vec = self._embed(q)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return await self.inner.search(user_id, query, k=k)
        if not q_vec:
            return await self.inner.search(user_id, query, k=k)

        # Pull a wider window than k so embedding ranking has material to
        # re-rank. 5x is a small constant — for typical /memo corpora
        # (< 100 per user) this still fits in one list_memos call.
        candidates = await self.inner.list_memos(user_id, limit=max(k * 5, 20))
        scored: list[tuple[float, Memo]] = []
        for memo in candidates:
            try:
                vec = self._embed(memo.text)
            except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                continue
            if not vec:
                continue
            scored.append((cosine(q_vec, vec), memo))

        if not scored:
            # Embedding pipeline yielded nothing usable — fall back.
            return await self.inner.search(user_id, query, k=k)

        scored.sort(key=lambda t: -t[0])
        return [m for _, m in scored[:k]]


def maybe_wrap_with_embedding(
    inner: MemoryBackend,
    *,
    backend: str,
    model: str,
    base_url: str,
    timeout_s: int = 10,
) -> MemoryBackend:
    """Factory used by run_bot wiring — picks LIKE vs embedding."""
    if backend == "embedding":
        return EmbeddingMemoryBackend(
            inner, model=model, base_url=base_url, timeout_s=timeout_s
        )
    return inner


__all__ = [
    "EmbeddingMemoryBackend",
    "cosine",
    "embed_text",
    "maybe_wrap_with_embedding",
]
