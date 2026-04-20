"""Per-user memory backend — Phase 2 stub.

The ``hybrid-memo`` skill uses this to let a user jot short notes that
survive across Discord turns (but not necessarily across bot restarts,
depending on backend). Phase 3 is expected to wire the same interface
to Hermes' native memory so the model sees notes in-context.

Scope:
  - **Short-text only**. Notes should be ≤ 2k chars; larger payloads are
    rejected. We're not building a general-purpose key-value store here.
  - **Per-user keyspace**. Backends must never leak notes across users
    even if the underlying storage is shared.
  - **No ordering guarantees**. ``list_memos`` returns notes in the order
    they were inserted (best-effort); order isn't load-bearing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol


_MAX_NOTE_CHARS = 2000


class MemoryTooLarge(ValueError):
    pass


@dataclass(frozen=True)
class Memo:
    user_id: str
    text: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class MemoryBackend(Protocol):
    """Minimum surface a memory backend must implement."""

    async def save(self, user_id: str, text: str) -> Memo: ...
    async def list_memos(self, user_id: str, limit: int = 20) -> list[Memo]: ...
    async def clear(self, user_id: str) -> int: ...


def _validate(text: str) -> str:
    t = (text or "").strip()
    if not t:
        raise ValueError("memo text cannot be empty")
    if len(t) > _MAX_NOTE_CHARS:
        raise MemoryTooLarge(f"memo text exceeds {_MAX_NOTE_CHARS} chars")
    return t
