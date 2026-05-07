"""Tests for FeedbackRouter (Phase 20 in-memory LRU).

Locks down:
  * register / lookup roundtrip
  * unknown id → None
  * LRU eviction at capacity
  * TTL expiry
  * lookup refreshes LRU position
"""
from __future__ import annotations

import time
from unittest.mock import patch

from src.gateway.feedback_router import FeedbackRouter


def test_register_then_lookup_returns_task_id():
    r = FeedbackRouter(max_entries=10, ttl_seconds=60)
    r.register(101, "task-A")
    assert r.lookup(101) == "task-A"


def test_unknown_message_returns_none():
    r = FeedbackRouter()
    assert r.lookup(999) is None


def test_eviction_drops_oldest_at_capacity():
    r = FeedbackRouter(max_entries=2, ttl_seconds=60)
    r.register(1, "a")
    r.register(2, "b")
    r.register(3, "c")                           # forces eviction of 1
    assert r.lookup(1) is None
    assert r.lookup(2) == "b"
    assert r.lookup(3) == "c"


def test_ttl_expires_old_entries():
    r = FeedbackRouter(max_entries=10, ttl_seconds=1)
    fake_now = [1000.0]

    def _now():
        return fake_now[0]

    with patch("src.gateway.feedback_router.monotonic", side_effect=_now):
        r.register(1, "a")
        fake_now[0] = 1000.5
        assert r.lookup(1) == "a"                # within TTL
        fake_now[0] = 1002.0
        assert r.lookup(1) is None               # expired


def test_lookup_refreshes_lru_position():
    """LRU 갱신: 최근 lookup된 항목은 capacity-evict의 head 후보가 아니어야."""
    r = FeedbackRouter(max_entries=2, ttl_seconds=60)
    r.register(1, "a")
    r.register(2, "b")
    # Access 1 — should now be most recently used.
    assert r.lookup(1) == "a"
    r.register(3, "c")                           # evict LRU = 2
    assert r.lookup(2) is None
    assert r.lookup(1) == "a"
    assert r.lookup(3) == "c"
