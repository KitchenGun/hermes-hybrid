"""BenchScheduler — automatic background bench trigger.

Two trigger paths:

  1. **Startup**: when the bot boots, compare installed Ollama models
     against ScoreMatrix cells. Any model that's installed but has zero
     observations for any job_type is "unbenched" → schedule a bench.

  2. **Watch loop**: poll Ollama's ``/api/tags`` every N seconds. New
     models that appear since the last poll → schedule a bench for
     just those.

The scheduler runs all bench work as background ``asyncio.Task`` and
holds at most one in-flight bench at a time (``self._bench_lock``).
This keeps GPU contention with live traffic predictable and lets the
runner's own ``gpu_concurrency`` cap stay at 1 (its default).

Lifecycle:
  - ``await scheduler.start()`` — fires the startup check + spawns the
    watch loop. Non-blocking; returns immediately.
  - ``await scheduler.stop()`` — cancels the watch loop and any in-flight
    bench. Safe to call multiple times.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any

from src.job_factory.score_matrix import ScoreMatrix

log = logging.getLogger(__name__)

# Default poll interval — 5 minutes. Overridable in __init__.
DEFAULT_POLL_INTERVAL_S = 300

# Time to wait between back-to-back bench triggers (e.g., user pulls
# multiple models in quick succession). Avoids thrashing the GPU.
DEFAULT_DEBOUNCE_S = 30


class BenchScheduler:
    """Background bench trigger.

    Args:
        score_matrix: The live ScoreMatrix; used to detect "unbenched"
            models (no cells with this model on any job_type).
        ollama_base_url: Where to call ``/api/tags``.
        bench_callback: Async callable that takes ``list[str]`` (model
            names to bench) and runs the bench. Typically a thin wrapper
            around BenchRunner that constructs adapters per model.
        poll_interval_s: How often the watch loop checks Ollama.
        debounce_s: Minimum gap between consecutive bench triggers.
    """

    def __init__(
        self,
        *,
        score_matrix: ScoreMatrix,
        ollama_base_url: str,
        bench_callback: Callable[[list[str]], Awaitable[Any]],
        poll_interval_s: int = DEFAULT_POLL_INTERVAL_S,
        debounce_s: int = DEFAULT_DEBOUNCE_S,
    ):
        self._matrix = score_matrix
        self._ollama_url = ollama_base_url.rstrip("/")
        self._bench = bench_callback
        self._poll_interval = poll_interval_s
        self._debounce = debounce_s

        self._known_models: set[str] = set()
        self._watch_task: asyncio.Task | None = None
        self._bench_task: asyncio.Task | None = None
        self._bench_lock = asyncio.Lock()
        self._stopped = False
        self._last_bench_at: float = 0.0  # event-loop time

    # ---- public lifecycle -------------------------------------------------

    async def start(self) -> None:
        """Fire the startup unbenched check + start the watch loop.

        Both run in the background — this method returns immediately.
        Call exactly once per scheduler instance.
        """
        if self._watch_task is not None:
            log.warning("scheduler.start.already_running")
            return

        # Startup: detect models needing bench, schedule them.
        installed = await self._discover_installed_models()
        self._known_models = set(installed)
        unbenched = self._find_unbenched(installed)
        if unbenched:
            log.info(
                "scheduler.startup.unbenched",
                extra={"models": list(unbenched), "count": len(unbenched)},
            )
            self._spawn_bench(list(unbenched))

        # Watch loop in background.
        self._watch_task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        """Cancel the watch loop and any in-flight bench. Idempotent."""
        if self._stopped:
            return
        self._stopped = True

        if self._watch_task is not None:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._watch_task = None

        if self._bench_task is not None and not self._bench_task.done():
            self._bench_task.cancel()
            try:
                await self._bench_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._bench_task = None

    # ---- internals --------------------------------------------------------

    async def _watch_loop(self) -> None:
        """Polls Ollama; schedules a bench for newly-installed models."""
        while not self._stopped:
            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                return
            if self._stopped:
                return

            try:
                installed = await self._discover_installed_models()
            except Exception as e:  # noqa: BLE001
                log.warning("scheduler.discover_failed", extra={"err": str(e)})
                continue

            new = set(installed) - self._known_models
            removed = self._known_models - set(installed)
            self._known_models = set(installed)

            if removed:
                log.info("scheduler.models_removed", extra={"models": list(removed)})
            if new:
                log.info("scheduler.models_new", extra={"models": list(new)})
                self._spawn_bench(list(new))

    def _find_unbenched(self, installed: list[str]) -> set[str]:
        """A model is 'unbenched' if it has zero ScoreMatrix observations
        across all job_types. Empty matrix → all installed are unbenched."""
        seen = self._matrix.all_models()
        return {m for m in installed if m not in seen}

    def _spawn_bench(self, models: list[str]) -> None:
        """Schedule a bench task. Coalesces if one is in flight."""
        if self._stopped:
            return
        loop = asyncio.get_event_loop()
        # Debounce: if we just kicked one off, defer for a few seconds.
        delay = max(0.0, self._debounce - (loop.time() - self._last_bench_at))
        if self._bench_task is not None and not self._bench_task.done():
            log.info(
                "scheduler.bench_in_flight_queued",
                extra={"models": list(models)},
            )
            # Queue: the next bench will fold these in — for v1 simplicity
            # we just spawn another that waits for the lock.
        self._last_bench_at = loop.time()
        self._bench_task = asyncio.create_task(
            self._run_bench(models, delay),
            name=f"bench-{','.join(sorted(models))[:50]}",
        )

    async def _run_bench(self, models: list[str], delay: float) -> None:
        """Lock-and-run wrapper around the user-supplied bench callback."""
        if delay > 0:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
        async with self._bench_lock:
            if self._stopped:
                return
            try:
                log.info("scheduler.bench_starting", extra={"models": models})
                await self._bench(models)
                log.info("scheduler.bench_done", extra={"models": models})
            except asyncio.CancelledError:
                log.info("scheduler.bench_cancelled", extra={"models": models})
                raise
            except Exception as e:  # noqa: BLE001
                log.exception(
                    "scheduler.bench_failed",
                    extra={"models": models, "err": str(e)},
                )

    async def _discover_installed_models(self) -> list[str]:
        """Hit Ollama's ``/api/tags`` and return model names.

        Runs the (blocking) urllib call in a thread to keep the event
        loop responsive — Ollama can be slow to answer if it's loading
        a model into VRAM concurrently.
        """
        url = f"{self._ollama_url}/api/tags"

        def _fetch() -> list[str]:
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, json.JSONDecodeError, OSError):
                return []
            return [
                m["name"]
                for m in data.get("models", [])
                if "name" in m
            ]

        return await asyncio.to_thread(_fetch)
