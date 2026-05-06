"""Sub-agent delegation — Phase 10 (parallel @handle dispatch).

Phase 11 (2026-05-06): opencode CLI → Claude CLI (Max OAuth) swap.
``ClaudeAgentDelegator`` 가 master 의 동일 ClaudeCodeAdapter 를 재사용해
각 ``@handle`` 별 독립 호출을 ``asyncio.gather`` 로 동시 진행.

Phase 9 가 IntentRouter 가 추출한 ``@coder`` 같은 mention 들을 모아 단일
master prompt 에 모든 SKILL.md snippet 을 inject 한다. Phase 10 은
**진짜 병렬 실행** — 각 mention 에 대해 독립 claude 호출을 띄우고
``asyncio.gather`` 로 동시에 진행한 뒤 결과를 집계.

기본 동작은 여전히 단일 master 호출 (``settings.master_parallel_agents``
가 False). 사용자가 명시 opt-in 하면, 여러 ``@handle`` 멘션이 발견될 때
fan-out:

    @bot @coder fizzbuzz 짜고 @reviewer 검토해줘
        ↓ IntentRouter
    agent_handles = ["@coder", "@reviewer"]
        ↓ HermesMaster (master_parallel_agents=True 일 때)
    delegate_many([
        SubAgentRequest("@coder",   "fizzbuzz 짜고 @reviewer 검토해줘"),
        SubAgentRequest("@reviewer", "fizzbuzz 짜고 @reviewer 검토해줘"),
    ])
        ↓ asyncio.gather  (semaphore 로 max_concurrency 제한)
    [SubAgentResult, SubAgentResult]
        ↓ aggregate_responses
    "### @coder\n...\n\n### @reviewer\n..."

Why behind a flag:
  * 각 호출이 별도 claude subprocess — 비용/지연이 N 배.
  * Max OAuth 시간당 한도 도달 위험.
  * 사용자가 의도적으로 켜야 의미 있음 (대부분 단일 master snippet inject
    로 충분).
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    # ``src.agents`` imports from ``src.core`` (SkillEntry) — defer to
    # run-time inside methods to dodge the circular import.
    from src.agents import AgentEntry, AgentRegistry


class SubAgentRequest(BaseModel):
    """One sub-task hand-off to a specific sub-agent."""

    agent_handle: str                 # "@coder" / "@reviewer" / ...
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
    implementations can do real I/O (claude subprocess, MCP client, ...)."""

    async def delegate(self, request: SubAgentRequest) -> SubAgentResult:
        ...

    async def delegate_many(
        self, requests: list[SubAgentRequest]
    ) -> list[SubAgentResult]:
        ...


class ClaudeAgentDelegator:
    """Phase 10/11 — real parallel delegator using Claude CLI per agent.

    Each :class:`SubAgentRequest` becomes one claude subprocess call.
    The agent's SKILL.md frontmatter (lookup via :class:`AgentRegistry`)
    is composed into the system prompt so the LLM follows that agent's
    role/inputs/outputs/constraints.

    Concurrency is bounded by ``max_concurrency`` (default 3) via an
    :class:`asyncio.Semaphore`; this caps simultaneous claude subprocesses
    so the host doesn't burst-load WSL or the Max OAuth quota.

    The constructor takes the ClaudeCodeAdapter (already concurrency-aware
    via its own pool) — we install our own semaphore on top so this
    delegator's own per-call budget is independent of the adapter's
    default.
    """

    def __init__(
        self,
        adapter: Any,                          # ClaudeCodeAdapter-like
        agents: "AgentRegistry | None" = None,
        *,
        max_concurrency: int = 3,
    ):
        if agents is None:
            from src.agents import AgentRegistry as _AR
            agents = _AR()
        self.adapter = adapter
        self.agents = agents
        self.max_concurrency = max(1, max_concurrency)

    async def delegate(self, request: SubAgentRequest) -> SubAgentResult:
        entry = self.agents.by_handle(request.agent_handle)
        if entry is None:
            return SubAgentResult(
                request=request,
                success=False,
                error=(
                    f"unknown agent handle: {request.agent_handle} "
                    "(not registered in AgentRegistry)"
                ),
            )

        prompt = _compose_agent_prompt(entry, request.user_message)
        try:
            result = await self.adapter.run(
                prompt=prompt,
                history=[],
            )
        except Exception as e:  # noqa: BLE001
            return SubAgentResult(
                request=request,
                success=False,
                error=f"{type(e).__name__}: {e}",
            )

        return SubAgentResult(
            request=request,
            success=True,
            response=getattr(result, "text", "") or "",
            prompt_tokens=int(getattr(result, "input_tokens", 0) or 0),
            completion_tokens=int(getattr(result, "output_tokens", 0) or 0),
            cost_usd=float(getattr(result, "total_cost_usd", 0.0) or 0.0),
            duration_ms=int(getattr(result, "duration_ms", 0) or 0),
        )

    async def delegate_many(
        self,
        requests: list[SubAgentRequest],
        *,
        max_concurrency: int | None = None,
    ) -> list[SubAgentResult]:
        """Fan out via ``asyncio.gather`` with a semaphore.

        Order is preserved: ``results[i]`` corresponds to ``requests[i]``.
        Failures inside :meth:`delegate` are caught and returned as
        ``success=False`` results — a single sub-agent crash does not
        abort the batch.
        """
        if not requests:
            return []
        cap = max(1, max_concurrency or self.max_concurrency)
        sem = asyncio.Semaphore(cap)

        async def _run(req: SubAgentRequest) -> SubAgentResult:
            async with sem:
                return await self.delegate(req)

        return await asyncio.gather(*[_run(r) for r in requests])


# ---- prompt + aggregation helpers --------------------------------------


_SUB_AGENT_SYSTEM_PROMPT = (
    "You are acting as a Hermes sub-agent. The frontmatter below scopes "
    "your role / inputs / outputs / constraints — stay strictly inside "
    "those bounds. Be concise. Use Korean when the user does. Output only "
    "what your role calls for; do not duplicate work that other sub-agents "
    "would handle."
)


def _compose_agent_prompt(entry: "AgentEntry", user_message: str) -> str:
    """Compose the system+snippet+user prompt for a sub-agent call.

    Uses the same SKILL.md projection as :meth:`HermesMasterOrchestrator.
    _agent_snippet` so the two paths produce identical agent context.
    """
    lines: list[str] = [_SUB_AGENT_SYSTEM_PROMPT, ""]
    lines.append(
        f"## Active sub-agent: {entry.handle} (role: {entry.role or '—'})"
    )
    if entry.description:
        lines.append(f"description: {entry.description}")
    if entry.when_to_use:
        lines.append("when_to_use:")
        lines.extend(f"  - {item}" for item in entry.when_to_use)
    if entry.not_for:
        lines.append("not_for:")
        lines.extend(f"  - {item}" for item in entry.not_for)
    if entry.inputs:
        lines.append(f"inputs: {', '.join(entry.inputs)}")
    if entry.outputs:
        lines.append(f"outputs: {', '.join(entry.outputs)}")
    if entry.primary_tools:
        lines.append(f"primary_tools: {', '.join(entry.primary_tools)}")
    lines.append("")
    lines.append("## User")
    lines.append(user_message)
    return "\n".join(lines)


def aggregate_responses(results: list[SubAgentResult]) -> str:
    """Concatenate per-agent responses with handle headers.

    Phase 10 의 단순 집계 — 후속 Phase 에서 master 가 결과들을 다시
    한 번 종합 (LLM round-trip) 하는 패턴으로 확장 가능.

    Format:
        ### @coder
        <coder response>

        ### @reviewer
        <reviewer response>

        ### @debugger (failed)
        <error message>
    """
    if not results:
        return ""
    parts: list[str] = []
    for r in results:
        header = f"### {r.request.agent_handle}"
        if not r.success:
            header += " (failed)"
            body = r.error or "(no error message)"
        else:
            body = r.response or "(empty response)"
        parts.append(f"{header}\n{body}")
    return "\n\n".join(parts)


__all__ = [
    "ClaudeAgentDelegator",
    "Delegator",
    "SubAgentRequest",
    "SubAgentResult",
    "aggregate_responses",
]
