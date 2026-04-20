"""In-memory ``MemoryBackend`` — default implementation.

Lives in process memory, cleared on bot restart. Fine for Phase 2 because
memo is a nice-to-have for quick "jot this" workflows; durable memory is
out of scope until Phase 3 wires Hermes' own memory surface.

Concurrency: all operations use a single ``asyncio.Lock`` so concurrent
Discord turns for the same user don't race. No IO, so the lock is
essentially free.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

from .base import Memo, MemoryBackend, _validate


class InMemoryMemory(MemoryBackend):
    def __init__(self) -> None:
        self._by_user: dict[str, list[Memo]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def save(self, user_id: str, text: str) -> Memo:
        clean = _validate(text)
        async with self._lock:
            memo = Memo(user_id=user_id, text=clean)
            self._by_user[user_id].append(memo)
            return memo

    async def list_memos(self, user_id: str, limit: int = 20) -> list[Memo]:
        async with self._lock:
            return list(self._by_user.get(user_id, []))[-limit:]

    async def clear(self, user_id: str) -> int:
        async with self._lock:
            prior = self._by_user.pop(user_id, [])
            return len(prior)
