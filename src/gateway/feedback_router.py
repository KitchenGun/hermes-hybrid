"""Feedback Router — Phase 20 (2026-05-07).

In-memory message_id ↔ task_id LRU cache. Discord 봇이 응답 메시지를
보낸 후 ``register(message_id, task_id)`` 호출. 사용자 reaction 시
``lookup(message_id)`` 로 task_id 회수 → ExperienceLogger.append_feedback.

설계:
  * stdlib 만 (collections.OrderedDict 기반 LRU).
  * TTL 24h — 그 이전 reaction 만 처리. 영구 매핑 원하면 v2 (SQLite).
  * 재시작 시 휘발 OK — feedback 은 best-effort.
  * thread-safe 가 아니라 asyncio single-event-loop 가정. discord.py
    이벤트 핸들러는 단일 루프에서 직렬 실행되므로 충분.
"""
from __future__ import annotations

from collections import OrderedDict
from time import monotonic


class FeedbackRouter:
    """Bounded LRU mapping from bot-message-id to task-id."""

    def __init__(self, *, max_entries: int = 1000, ttl_seconds: int = 86_400):
        self._max = max(1, int(max_entries))
        self._ttl = max(1, int(ttl_seconds))
        # OrderedDict: insertion order = LRU order. We move-to-end on hits.
        self._store: OrderedDict[int, tuple[str, float]] = OrderedDict()

    def register(self, message_id: int, task_id: str) -> None:
        now = monotonic()
        self._store[message_id] = (task_id, now)
        self._store.move_to_end(message_id)
        self._evict()

    def lookup(self, message_id: int) -> str | None:
        entry = self._store.get(message_id)
        if entry is None:
            return None
        task_id, ts = entry
        if monotonic() - ts > self._ttl:
            self._store.pop(message_id, None)
            return None
        # Refresh LRU — hot messages stay longer.
        self._store.move_to_end(message_id)
        return task_id

    def _evict(self) -> None:
        # Drop expired (cheap because OrderedDict iteration is ordered by
        # insert + we only check the head until first non-expired).
        cutoff = monotonic() - self._ttl
        while self._store:
            oldest_key = next(iter(self._store))
            _, ts = self._store[oldest_key]
            if ts < cutoff:
                self._store.popitem(last=False)
            else:
                break
        # Cap by size.
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    def __len__(self) -> int:
        return len(self._store)


__all__ = ["FeedbackRouter"]
