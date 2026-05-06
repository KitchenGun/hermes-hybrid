"""Tests for the Phase 10 sub-agent delegation interface.

Phase 8 (2026-05-06) deleted the profile system, so the prior
``SequentialHermesDelegator`` (profile_id-based) is gone. Phase 10
introduces ``OpenCodeAgentDelegator`` — one ``opencode`` subprocess per
@handle, parallelized via ``asyncio.gather`` with a semaphore.

We lock down:
  * delegate forwards to opencode.run with the right prompt (agent
    SKILL.md frontmatter snippet + user message)
  * unknown @handle → SubAgentResult(success=False, error=...) without
    calling opencode
  * adapter exception → SubAgentResult(success=False, error=...)
    (delegation must NEVER raise)
  * delegate_many preserves request order
  * delegate_many actually runs in parallel (concurrent timing)
  * max_concurrency caps simultaneous in-flight calls
  * aggregate_responses formats per-agent sections
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from src.agents import AgentEntry, AgentRegistry
from src.core import (
    OpenCodeAgentDelegator,
    SubAgentRequest,
    SubAgentResult,
    aggregate_responses,
)
from src.core.delegation import _compose_agent_prompt


_REPO_ROOT = Path(__file__).resolve().parent.parent


def _registry() -> AgentRegistry:
    return AgentRegistry(repo_root=_REPO_ROOT)


@dataclass
class _StubResult:
    text: str = "stub response"
    model_name: str = "gpt-5.5"
    session_id: str = "sess-1"
    duration_ms: int = 250
    input_tokens: int = 100
    output_tokens: int = 50
    total_cost_usd: float = 0.001


class _StubOpenCode:
    def __init__(self, *, raises: Exception | None = None, delay: float = 0.0):
        self.calls: list[dict[str, Any]] = []
        self._raises = raises
        self._delay = delay
        self._concurrent_now = 0
        self.peak_concurrent = 0
        self._lock = asyncio.Lock()

    async def run(self, *, prompt: str, history=None, model=None, timeout_ms=None):
        async with self._lock:
            self._concurrent_now += 1
            self.peak_concurrent = max(
                self.peak_concurrent, self._concurrent_now
            )
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            self.calls.append({
                "prompt": prompt,
                "history": list(history or []),
                "model": model,
            })
            if self._raises is not None:
                raise self._raises
            return _StubResult()
        finally:
            async with self._lock:
                self._concurrent_now -= 1


# ---- delegate -------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_resolves_handle_and_calls_opencode():
    adapter = _StubOpenCode()
    deleg = OpenCodeAgentDelegator(adapter, _registry())
    req = SubAgentRequest(
        agent_handle="@coder",
        user_message="fizzbuzz 짜줘",
        parent_task_id="t1",
        parent_session_id="s1",
    )
    result = await deleg.delegate(req)
    assert result.success is True
    assert result.response == "stub response"
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 50
    # opencode 호출 한 번 — prompt 안에 SKILL.md frontmatter snippet 포함
    assert len(adapter.calls) == 1
    prompt = adapter.calls[0]["prompt"]
    assert "## Active sub-agent: @coder" in prompt
    assert "fizzbuzz 짜줘" in prompt


@pytest.mark.asyncio
async def test_delegate_unknown_handle_returns_failure_without_opencode_call():
    adapter = _StubOpenCode()
    deleg = OpenCodeAgentDelegator(adapter, _registry())
    req = SubAgentRequest(
        agent_handle="@nobody",
        user_message="hi",
        parent_task_id="t1",
        parent_session_id="s1",
    )
    result = await deleg.delegate(req)
    assert result.success is False
    assert "@nobody" in result.error
    assert adapter.calls == []   # 등록되지 않은 핸들은 opencode 안 부름


@pytest.mark.asyncio
async def test_delegate_swallows_opencode_exception():
    adapter = _StubOpenCode(raises=RuntimeError("opencode timeout"))
    deleg = OpenCodeAgentDelegator(adapter, _registry())
    req = SubAgentRequest(
        agent_handle="@coder",
        user_message="X",
        parent_task_id="t1",
        parent_session_id="s1",
    )
    result = await deleg.delegate(req)
    assert isinstance(result, SubAgentResult)
    assert result.success is False
    assert "RuntimeError" in result.error
    assert "opencode timeout" in result.error


# ---- delegate_many -------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_many_preserves_order_and_length():
    adapter = _StubOpenCode()
    deleg = OpenCodeAgentDelegator(adapter, _registry())
    handles = ["@coder", "@reviewer", "@tester"]
    requests = [
        SubAgentRequest(
            agent_handle=h, user_message=f"msg-{h}",
            parent_task_id="t", parent_session_id="s",
        )
        for h in handles
    ]
    results = await deleg.delegate_many(requests)
    assert len(results) == 3
    assert [r.request.agent_handle for r in results] == handles
    assert all(r.success for r in results)


@pytest.mark.asyncio
async def test_delegate_many_runs_in_parallel():
    """3 calls × 100ms each should finish in <250ms (parallel), not 300+ ms (sequential)."""
    adapter = _StubOpenCode(delay=0.10)
    deleg = OpenCodeAgentDelegator(adapter, _registry(), max_concurrency=3)
    requests = [
        SubAgentRequest(
            agent_handle=h, user_message="x",
            parent_task_id="t", parent_session_id="s",
        )
        for h in ("@coder", "@reviewer", "@tester")
    ]
    start = time.perf_counter()
    results = await deleg.delegate_many(requests)
    elapsed = time.perf_counter() - start

    assert len(results) == 3
    assert all(r.success for r in results)
    # parallel: ~0.10s. sequential would be ~0.30s. Be lenient for CI noise.
    assert elapsed < 0.25, f"expected parallel (<0.25s), got {elapsed:.3f}s"
    # peak concurrency reached the cap
    assert adapter.peak_concurrent == 3


@pytest.mark.asyncio
async def test_max_concurrency_caps_simultaneous_calls():
    """5 calls with max_concurrency=2 → peak_concurrent should be 2."""
    adapter = _StubOpenCode(delay=0.05)
    deleg = OpenCodeAgentDelegator(adapter, _registry(), max_concurrency=2)
    handles = ["@coder", "@reviewer", "@tester", "@debugger", "@security"]
    requests = [
        SubAgentRequest(
            agent_handle=h, user_message="x",
            parent_task_id="t", parent_session_id="s",
        )
        for h in handles
    ]
    await deleg.delegate_many(requests)
    assert adapter.peak_concurrent == 2


@pytest.mark.asyncio
async def test_delegate_many_empty_list_returns_empty():
    adapter = _StubOpenCode()
    deleg = OpenCodeAgentDelegator(adapter, _registry())
    assert await deleg.delegate_many([]) == []


@pytest.mark.asyncio
async def test_delegate_many_single_failure_does_not_abort_batch():
    """One unknown handle in the middle should fail just that one;
    the other agents still get their results."""
    adapter = _StubOpenCode()
    deleg = OpenCodeAgentDelegator(adapter, _registry())
    requests = [
        SubAgentRequest(
            agent_handle=h, user_message="x",
            parent_task_id="t", parent_session_id="s",
        )
        for h in ("@coder", "@nobody", "@reviewer")
    ]
    results = await deleg.delegate_many(requests)
    assert [r.request.agent_handle for r in results] == [
        "@coder", "@nobody", "@reviewer"
    ]
    assert results[0].success is True
    assert results[1].success is False
    assert results[2].success is True


# ---- prompt composition --------------------------------------------------


def test_compose_agent_prompt_includes_skill_md_fields():
    reg = _registry()
    coder = reg.by_handle("@coder")
    assert coder is not None
    prompt = _compose_agent_prompt(coder, "fizzbuzz 만들어줘")
    assert "## Active sub-agent: @coder" in prompt
    assert "role: write_new_code" in prompt
    assert "## User" in prompt
    assert "fizzbuzz 만들어줘" in prompt


# ---- aggregate_responses -------------------------------------------------


def test_aggregate_responses_concatenates_with_handle_headers():
    req1 = SubAgentRequest(
        agent_handle="@coder", user_message="x",
        parent_task_id="t", parent_session_id="s",
    )
    req2 = SubAgentRequest(
        agent_handle="@reviewer", user_message="x",
        parent_task_id="t", parent_session_id="s",
    )
    out = aggregate_responses([
        SubAgentResult(request=req1, success=True, response="ok 1"),
        SubAgentResult(request=req2, success=True, response="ok 2"),
    ])
    assert "### @coder" in out
    assert "### @reviewer" in out
    assert "ok 1" in out
    assert "ok 2" in out


def test_aggregate_responses_marks_failures():
    req = SubAgentRequest(
        agent_handle="@nobody", user_message="x",
        parent_task_id="t", parent_session_id="s",
    )
    out = aggregate_responses([
        SubAgentResult(request=req, success=False, error="not found"),
    ])
    assert "### @nobody (failed)" in out
    assert "not found" in out


def test_aggregate_responses_empty():
    assert aggregate_responses([]) == ""
