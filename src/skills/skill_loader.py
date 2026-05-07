"""Skill Loader — Phase 18 (2026-05-07).

AgentRegistry hot-reload polling. master 가 재시작 안 해도 새 SKILL.md 가
SkillPromoter 의 auto_install 로 추가되면 자동 인식.

설계:
  * polling-only (watchdog 의존 X — Windows + WSL 호환성).
  * 30s 기본 폴링 — SkillPromoter 의 일요일 23:30 cadence 와 비교해
    충분히 빠르고 CPU 부담 X (single stat 호출 N개).
  * `start(registry, interval_s)` 가 ``asyncio.create_task`` 로 백그라운드
    루프를 띄움. ``stop()`` 으로 깔끔히 종료. 봇 lifecycle 에 묶여 있음.
  * registry.reload_if_changed() 가 mtime 비교 + 변경 시 atomic swap _scan.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from src.obs import get_logger

log = get_logger(__name__)


class SkillLoader:
    """Background polling watcher for an AgentRegistry."""

    def __init__(
        self,
        registry: Any,                          # AgentRegistry-like
        *,
        interval_s: int = 30,
    ):
        self.registry = registry
        self.interval_s = max(1, int(interval_s))
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Spawn the polling task. Idempotent — calling twice is a no-op."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._poll_forever())
        log.info("skill_loader.started", interval_s=self.interval_s)

    async def stop(self) -> None:
        """Signal the loop to exit and await its completion."""
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=self.interval_s + 5)
        except asyncio.TimeoutError:
            self._task.cancel()
        finally:
            self._task = None
            log.info("skill_loader.stopped")

    async def _poll_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                changed = self.registry.reload_if_changed()
                if changed:
                    log.info(
                        "skill_loader.reloaded",
                        agents=len(self.registry.all()),
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("skill_loader.poll_failed", err=str(e))

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.interval_s,
                )
            except asyncio.TimeoutError:
                continue


__all__ = ["SkillLoader"]
