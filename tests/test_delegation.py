"""Tests for the sub-agent delegation interface (Phase 5 stub).

The interface is a contract for Phase 5b parallel execution. We lock
down:
  * SequentialHermesDelegator forwards to hermes.call with profile_id
  * adapter exception → SubAgentResult(success=False, error=...)
    (delegation must NEVER raise — caller should always get a result)
  * delegate_many runs in order and returns same-length list
  * ``response`` falls back to empty string when adapter result has no
    text attribute (defensive coding for adapter version skew)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from src.core import (
    SequentialHermesDelegator,
    SubAgentRequest,
    SubAgentResult,
)


@dataclass
class _StubResult:
    text: str = "stub response"
    prompt_tokens: int = 100
    completion_tokens: int = 50
    cost_usd: float = 0.001
    duration_ms: int = 250


class _StubAdapter:
    def __init__(self, raises: Exception | None = None):
        self.calls: list[dict[str, Any]] = []
        self._raises = raises

    async def call(self, query: str, *, profile: str, **kw: Any) -> Any:
        self.calls.append({"query": query, "profile": profile, **kw})
        if self._raises is not None:
            raise self._raises
        return _StubResult()


@pytest.mark.asyncio
async def test_delegate_forwards_profile_and_query():
    adapter = _StubAdapter()
    deleg = SequentialHermesDelegator(adapter)
    req = SubAgentRequest(
        profile_id="kk_job",
        user_message="이 회사 분석해줘",
        parent_task_id="t1",
        parent_session_id="s1",
    )
    result = await deleg.delegate(req)
    assert result.success is True
    assert result.response == "stub response"
    assert adapter.calls == [
        {"query": "이 회사 분석해줘", "profile": "kk_job"}
    ]


@pytest.mark.asyncio
async def test_delegate_swallows_adapter_exception():
    """Delegation must never throw — caller gets success=False instead."""
    adapter = _StubAdapter(raises=RuntimeError("hermes timeout"))
    deleg = SequentialHermesDelegator(adapter)
    req = SubAgentRequest(
        profile_id="kk_job",
        user_message="X",
        parent_task_id="t1",
        parent_session_id="s1",
    )
    result = await deleg.delegate(req)
    assert isinstance(result, SubAgentResult)
    assert result.success is False
    assert "RuntimeError" in result.error
    assert "hermes timeout" in result.error


@pytest.mark.asyncio
async def test_delegate_many_preserves_order_and_length():
    adapter = _StubAdapter()
    deleg = SequentialHermesDelegator(adapter)
    requests = [
        SubAgentRequest(
            profile_id=p, user_message=f"msg-{p}",
            parent_task_id="t", parent_session_id="s",
        )
        for p in ("kk_job", "calendar_ops", "advisor_ops")
    ]
    results = await deleg.delegate_many(requests)
    assert len(results) == 3
    assert [r.request.profile_id for r in results] == [
        "kk_job", "calendar_ops", "advisor_ops"
    ]
    assert all(r.success for r in results)


@pytest.mark.asyncio
async def test_delegate_handles_adapter_without_text_attribute():
    """If adapter returns a bare object missing .text, response defaults
    to '' rather than crashing."""
    class _BareResult:
        pass

    class _BareAdapter:
        async def call(self, query: str, *, profile: str, **kw: Any) -> Any:
            return _BareResult()

    deleg = SequentialHermesDelegator(_BareAdapter())
    req = SubAgentRequest(
        profile_id="x", user_message="hi",
        parent_task_id="t", parent_session_id="s",
    )
    result = await deleg.delegate(req)
    assert result.success is True
    assert result.response == ""
    assert result.prompt_tokens == 0
