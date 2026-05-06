"""KanbanDispatcher — gateway-internal cron loop (Phase 2-A).

Each ``tick()`` runs in this exact order:

  1. reclaim TTL-expired or PID-dead runs → status=ready
  2. promote todo → ready when all parents are done
  3. promote triage/todo → ready when scheduled_at <= now
  4. atomic claim + spawn (concurrency-gated, default 1)
  5. notify callback (optional opt-in)

Worker spawn is injected as ``spawn_runner`` (Step 4 wires the master
CLI subprocess wrapper). Tests pass mock spawners + a fake ``now``/``pid_alive``
so we can verify TTL and crash recovery without real processes or sleeps.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Awaitable, Callable

from src.core.kanban.db import KanbanDB
from src.core.kanban.models import KanbanTask
from src.obs import get_logger


log = get_logger(__name__)


SpawnRunner = Callable[[KanbanTask], Awaitable[int]]
NotifyCallback = Callable[[str, str], Awaitable[None]]  # (kind, task_id)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class KanbanDispatcher:
    def __init__(
        self,
        db: KanbanDB,
        *,
        spawn_runner: SpawnRunner,
        poll_seconds: int = 60,
        claim_ttl_seconds: int = 300,
        spawn_failure_limit: int = 5,
        max_inflight: int = 1,
        now: Callable[[], datetime] | None = None,
        notify: NotifyCallback | None = None,
        enabled: bool = True,
        pid_alive: Callable[[int], bool] | None = None,
    ):
        self.db = db
        self.spawn_runner = spawn_runner
        self.poll_seconds = poll_seconds
        self.claim_ttl_seconds = claim_ttl_seconds
        self.spawn_failure_limit = spawn_failure_limit
        self.max_inflight = max_inflight
        self.now = now or _utc_now
        self.notify = notify
        self.enabled = enabled
        self.pid_alive = pid_alive or _pid_alive
        self._stop = asyncio.Event()

    async def tick(self, *, dry_run: bool = False, max_claims: int | None = None) -> dict:
        """Run one pass.

        ``dry_run`` skips all mutating writes — only inspects state and
        returns what *would* change. ``max_claims`` caps step 4 (atomic
        claim + spawn) so an operator can drain a queue piecemeal.
        """
        if not self.enabled:
            return {}
        report: dict[str, list[str]] = {
            "reclaimed": [],
            "promoted": [],
            "claimed": [],
            "blocked": [],
            "spawn_failed": [],
        }
        now_dt = self.now()

        # 1. reclaim TTL-expired or PID-dead runs
        active_runs = await self.db.list_active_runs()
        inflight = len(active_runs)
        # Tasks the dispatcher touched this tick — don't re-claim them in step 4.
        excluded: set[str] = set()
        for run in active_runs:
            try:
                expires = datetime.fromisoformat(run.claim_expires_at)
            except ValueError:
                continue
            outcome: str | None = None
            if now_dt >= expires:
                outcome = "timed_out"
            elif run.pid is not None and not self.pid_alive(run.pid):
                outcome = "crashed"
            if outcome is None:
                continue
            if not dry_run:
                await self.db.end_run(
                    run.id, outcome=outcome,
                    error="reclaimed by dispatcher",
                )
                await self.db.set_status(
                    run.task_id, "ready",
                    actor="dispatcher", reason=f"reclaimed: {outcome}",
                )
            report["reclaimed"].append(run.task_id)
            excluded.add(run.task_id)
            inflight -= 1

        # 2. promote todo → ready when parents all done
        for task in await self.db.list_tasks(status="todo"):
            parents = await self.db.parents_of(task.id)
            if not parents:
                continue
            if all(p.status == "done" for p in parents):
                if not dry_run:
                    await self.db.set_status(
                        task.id, "ready", actor="dispatcher"
                    )
                report["promoted"].append(task.id)

        # 3. promote triage/todo → ready when scheduled_at <= now
        now_iso = now_dt.isoformat(timespec="seconds")
        for task in await self.db.list_tasks_due(now_iso):
            if not dry_run:
                await self.db.set_status(
                    task.id, "ready", actor="dispatcher"
                )
            report["promoted"].append(task.id)

        # 4. atomic claim + spawn (concurrency-gated)
        claims_done = 0
        while inflight < self.max_inflight:
            if max_claims is not None and claims_done >= max_claims:
                break
            if dry_run:
                # Don't mutate; just peek at next ready task that we
                # haven't already touched this tick.
                ready_tasks = await self.db.list_tasks(status="ready")
                next_ready = next(
                    (t for t in ready_tasks if t.id not in excluded), None
                )
                if next_ready is None:
                    break
                report["claimed"].append(next_ready.id)
                excluded.add(next_ready.id)
                inflight += 1
                claims_done += 1
                continue
            claimed = await self.db.atomic_claim_one_ready(
                claim_ttl_seconds=self.claim_ttl_seconds,
                exclude_ids=tuple(excluded),
            )
            if claimed is None:
                break
            inflight += 1
            claims_done += 1
            try:
                pid = await self.spawn_runner(claimed)
            except Exception as e:
                log.warning(
                    "kanban.spawn_failed",
                    task=claimed.id, err=str(e),
                )
                await self.db.end_run(
                    claimed.current_run_id,
                    outcome="spawn_failed",
                    error=str(e),
                )
                await self.db.set_status(
                    claimed.id, "ready", actor="dispatcher"
                )
                excluded.add(claimed.id)
                inflight -= 1
                report["spawn_failed"].append(claimed.id)
                count = await self.db.bump_spawn_failure(claimed.id)
                if count >= self.spawn_failure_limit:
                    await self.db.set_status(
                        claimed.id, "blocked",
                        actor="dispatcher",
                        reason=f"auto-blocked: {count} spawn failures",
                    )
                    report["blocked"].append(claimed.id)
                continue
            await self.db.attach_pid(claimed.current_run_id, pid)
            await self.db.reset_spawn_failure(claimed.id)
            report["claimed"].append(claimed.id)

        # 5. notify (opt-in, skipped on dry_run)
        if not dry_run and self.notify is not None:
            for kind, ids in report.items():
                for tid in ids:
                    try:
                        await self.notify(kind, tid)
                    except Exception as e:
                        log.warning(
                            "kanban.notify_failed",
                            kind=kind, task=tid, err=str(e),
                        )
        return report

    async def run(self) -> None:
        log.info("kanban.dispatcher_started", poll=self.poll_seconds)
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as e:
                log.warning("kanban.tick_failed", err=str(e))
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.poll_seconds
                )
            except asyncio.TimeoutError:
                pass
        log.info("kanban.dispatcher_stopped")

    async def stop(self) -> None:
        self._stop.set()


__all__ = ["KanbanDispatcher", "NotifyCallback", "SpawnRunner"]
