"""Tests for SkillLoader (Phase 18 polling watcher, 2026-05-07).

Locks down:
  * start() spawns a single background task; idempotent
  * stop() awaits the loop's exit
  * polling cycle calls registry.reload_if_changed at interval
  * exception in reload doesn't crash the loop
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.skills.skill_loader import SkillLoader


class _FakeRegistry:
    def __init__(self, *, fail: bool = False):
        self.calls = 0
        self.fail = fail
        self._items: list[Any] = []

    def reload_if_changed(self) -> bool:
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic reload failure")
        return False

    def all(self) -> list[Any]:
        return list(self._items)


@pytest.mark.asyncio
async def test_start_is_idempotent():
    reg = _FakeRegistry()
    loader = SkillLoader(reg, interval_s=1)
    await loader.start()
    first_task = loader._task
    await loader.start()                         # second start — no-op
    assert loader._task is first_task
    await loader.stop()


@pytest.mark.asyncio
async def test_polling_calls_reload_at_interval():
    reg = _FakeRegistry()
    loader = SkillLoader(reg, interval_s=1)
    await loader.start()
    # Yield a few times so the loop ticks at least once.
    await asyncio.sleep(0.05)
    await loader.stop()
    assert reg.calls >= 1


@pytest.mark.asyncio
async def test_exception_does_not_crash_loop():
    reg = _FakeRegistry(fail=True)
    loader = SkillLoader(reg, interval_s=1)
    await loader.start()
    await asyncio.sleep(0.05)
    # Loop must still be alive — task not done yet.
    assert loader._task is not None
    assert not loader._task.done()
    await loader.stop()
    # And it actually called reload despite exceptions.
    assert reg.calls >= 1


@pytest.mark.asyncio
async def test_stop_without_start_is_safe():
    reg = _FakeRegistry()
    loader = SkillLoader(reg, interval_s=5)
    # No start() called — stop() should not raise.
    await loader.stop()
