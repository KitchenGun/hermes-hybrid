"""Sub-agent delegation interface — Phase 5 stub.

The end goal: an Orchestrator action can spin up a *sub-agent* against
a different profile to handle a sub-task, run several in parallel, and
aggregate the results back into the parent task. Hermes already does
some delegation internally (its plan→act→reflect loop), but at a finer
grain than profile-level work hand-off.

This module ships the **interface** + a **single sequential delegator**
backed by HermesAdapter. Parallel execution and structured result
aggregation are deferred to Phase 5b — locking the interface now
prevents a breaking-change later when those land.

Why no real parallel execution yet:
  * hermes CLI semantics around concurrent sessions per profile aren't
    fully validated for our setup
  * a botched parallel rollout corrupts shared profile state
    (sessions/, jobs.json, memory)
  * sequential first → we add parallelism after measuring sub-agent
    success rates from the experience log
"""
from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class SubAgentRequest(BaseModel):
    """One sub-task hand-off."""

    profile_id: str
    user_message: str
    parent_task_id: str
    parent_session_id: str
    # Optional metadata the parent wants the sub-agent to see — e.g.
    # which step of a multi-step plan this is. Free-form to keep this
    # interface stable across iterations.
    context: dict[str, Any] = Field(default_factory=dict)


class SubAgentResult(BaseModel):
    """Sub-task outcome."""

    request: SubAgentRequest
    success: bool
    response: str = ""
    error: str = ""
    # Structured fields the parent might aggregate — usage / cost /
    # tools called. Filled by the concrete delegator.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: int = 0


class Delegator(Protocol):
    """Anything that can run a SubAgentRequest. Async so concrete
    implementations can do real I/O (hermes subprocess, MCP client, ...)."""

    async def delegate(self, request: SubAgentRequest) -> SubAgentResult:
        ...

    async def delegate_many(
        self, requests: list[SubAgentRequest]
    ) -> list[SubAgentResult]:
        ...


class SequentialHermesDelegator:
    """Phase 5 stub — runs one sub-agent at a time via HermesAdapter.

    The ``hermes_adapter`` is duck-typed (``HermesAdapter``-like with a
    ``call(query, profile, ...)`` coroutine) so tests can inject a stub
    without importing the real adapter.

    Why this lives next to ``Critic`` / ``ExperienceLogger`` (in core):
    delegation is a growth-loop primitive — when Curator promotes a
    skill, the natural next step is having the parent agent delegate
    to that skill's profile. Keeping the interface in core/ makes that
    path obvious.
    """

    def __init__(self, hermes_adapter: Any):
        self.hermes = hermes_adapter

    async def delegate(self, request: SubAgentRequest) -> SubAgentResult:
        try:
            # HermesAdapter.call signature varies between versions; pass
            # only what we know is universal and let the adapter fill
            # the rest from settings / defaults.
            result = await self.hermes.call(
                request.user_message,
                profile=request.profile_id,
            )
            return SubAgentResult(
                request=request,
                success=True,
                response=getattr(result, "text", "") or "",
                prompt_tokens=getattr(result, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(result, "completion_tokens", 0) or 0,
                cost_usd=float(getattr(result, "cost_usd", 0.0) or 0.0),
                duration_ms=int(getattr(result, "duration_ms", 0) or 0),
            )
        except Exception as e:  # noqa: BLE001
            return SubAgentResult(
                request=request,
                success=False,
                error=f"{type(e).__name__}: {e}",
            )

    async def delegate_many(
        self, requests: list[SubAgentRequest]
    ) -> list[SubAgentResult]:
        # Sequential by design. Phase 5b swaps this for asyncio.gather()
        # once sub-agent contention is measured.
        return [await self.delegate(r) for r in requests]


__all__ = [
    "Delegator",
    "SequentialHermesDelegator",
    "SubAgentRequest",
    "SubAgentResult",
]
