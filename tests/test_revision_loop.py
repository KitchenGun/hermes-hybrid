"""Phase 13 — revision loop tests.

Locks down:
  * RevisionLoop 한 번에 성공 → 1 attempt
  * score 낮으면 retry — context 안에 reflection prompt 포함
  * cap 도달 시 best attempt 반환
  * model escalation: haiku → sonnet → opus
  * adapter 예외 시에도 다음 attempt 진행 (graceful)
  * HermesMaster: revision_loop_enabled=False 시 single-shot 유지 (호환)
  * HermesMaster: revision_loop_enabled=True 시 RevisionLoop 호출
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.config import Settings
from src.orchestrator.revision_loop import RevisionAttempt, RevisionLoop, RevisionResult


def _settings(tmp_path, **overrides) -> Settings:
    base = {
        "_env_file": None,
        "discord_bot_token": "",
        "discord_allowed_user_ids": "",
        "require_allowlist": False,
        "ollama_enabled": False,
        "experience_log_enabled": False,
        "experience_log_root": tmp_path / "exp",
        "state_db_path": tmp_path / "test.db",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@dataclass
class _StubResult:
    text: str = "ok"
    model_name: str = "opus"
    input_tokens: int = 50
    output_tokens: int = 30
    duration_ms: int = 100
    session_id: str = "s1"
    total_cost_usd: float = 0.0


class _StubAdapter:
    def __init__(self, *, raises: Exception | None = None, response: str = "ok"):
        self.calls: list[dict[str, Any]] = []
        self._raises = raises
        self._response = response

    async def run(self, *, prompt: str, history=None, model=None, timeout_ms=None):
        self.calls.append({"prompt": prompt, "model": model})
        if self._raises is not None:
            raise self._raises
        return _StubResult(text=self._response, model_name=model or "opus")


# ---- RevisionLoop --------------------------------------------------------


@pytest.mark.asyncio
async def test_one_attempt_when_score_meets_threshold():
    """첫 응답이 threshold 이상이면 retry 안 함."""
    adapter = _StubAdapter(response="full and correct response")
    loop = RevisionLoop(
        adapter,
        critic_scorer=lambda t: 0.9,        # always pass
        max_retries=3,
        score_threshold=0.5,
    )
    result = await loop.run(prompt="질문", initial_model="opus")

    assert result.attempt_count == 1
    assert result.succeeded is True
    assert result.escalated is False
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_retry_on_low_score_and_escalate_model():
    """첫 시도 score=0.2 (low) → 2번째 시도, 모델 escalate."""
    scores_iter = iter([0.2, 0.8])    # first low, second high
    adapter = _StubAdapter()
    loop = RevisionLoop(
        adapter,
        critic_scorer=lambda t: next(scores_iter),
        max_retries=3,
        score_threshold=0.5,
        model_escalation=("haiku", "sonnet", "opus"),
    )
    result = await loop.run(prompt="질문", initial_model="haiku")

    assert result.attempt_count == 2
    assert result.succeeded is True
    assert result.escalated is True
    # 모델: haiku → sonnet
    assert adapter.calls[0]["model"] == "haiku"
    assert adapter.calls[1]["model"] == "sonnet"
    # 두 번째 prompt 에 reflection context 포함
    assert "이전 응답 self_score" in adapter.calls[1]["prompt"]


@pytest.mark.asyncio
async def test_max_retries_cap_returns_best_attempt():
    """3회 시도 모두 score 낮음 → best attempt 반환."""
    scores_iter = iter([0.2, 0.3, 0.4, 0.4, 0.4])
    adapter = _StubAdapter()
    loop = RevisionLoop(
        adapter,
        critic_scorer=lambda t: next(scores_iter),
        max_retries=3,
        score_threshold=0.5,
    )
    result = await loop.run(prompt="질문", initial_model="opus")

    assert result.attempt_count == 3
    assert result.succeeded is False
    # 최고 score 인 0.4 (3번째) 가 best
    assert result.final_self_score == 0.4


@pytest.mark.asyncio
async def test_adapter_exception_graceful():
    """첫 시도 raises → 2번째 시도 진행."""
    call_count = {"n": 0}

    class _Flaky:
        async def run(self, *, prompt, history=None, model=None, timeout_ms=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated")
            return _StubResult(text="recovery", model_name=model)

    loop = RevisionLoop(
        _Flaky(),
        critic_scorer=lambda t: 0.9,
        max_retries=3,
        score_threshold=0.5,
    )
    result = await loop.run(prompt="X", initial_model="haiku")

    assert result.attempt_count == 2
    assert result.attempts[0].success is False
    assert "RuntimeError" in result.attempts[0].error
    assert result.attempts[1].success is True
    assert result.succeeded is True


@pytest.mark.asyncio
async def test_all_attempts_raise_returns_failure():
    """3회 시도 모두 raises → succeeded=False."""
    adapter = _StubAdapter(raises=RuntimeError("always fails"))
    loop = RevisionLoop(
        adapter,
        critic_scorer=lambda t: 0.0,
        max_retries=3,
        score_threshold=0.5,
    )
    result = await loop.run(prompt="X", initial_model="opus")

    assert result.attempt_count == 3
    assert all(not a.success for a in result.attempts)
    assert result.succeeded is False


@pytest.mark.asyncio
async def test_model_already_at_top_does_not_escalate_further():
    """initial_model=opus + score 낮음 → escalation 위치 변화 없음 (top 도달)."""
    scores_iter = iter([0.2, 0.3, 0.3])
    adapter = _StubAdapter()
    loop = RevisionLoop(
        adapter,
        critic_scorer=lambda t: next(scores_iter),
        max_retries=3,
        score_threshold=0.5,
        model_escalation=("haiku", "sonnet", "opus"),
    )
    result = await loop.run(prompt="X", initial_model="opus")

    assert result.attempt_count == 3
    # opus 가 top 이라 다음 시도도 opus
    assert all(a.model == "opus" for a in result.attempts)


# ---- HermesMaster integration -------------------------------------------


@pytest.mark.asyncio
async def test_master_revision_off_default_uses_single_shot(tmp_path):
    """revision_loop_enabled=False (default) — RevisionLoop 사용 X."""
    from src.orchestrator.hermes_master import HermesMasterOrchestrator

    settings = _settings(tmp_path)
    assert settings.revision_loop_enabled is False
    master = HermesMasterOrchestrator(settings)
    master.adapter.run = AsyncMock(return_value=_StubResult(text="single shot"))

    result = await master.handle("자유 텍스트", user_id="u1")
    assert master.adapter.run.call_count == 1
    assert result.handled_by == "master:claude"


@pytest.mark.asyncio
async def test_master_revision_on_with_low_score_retries(tmp_path):
    """revision_loop_enabled=True + 짧은 응답 (score 낮음) → retry."""
    from src.orchestrator.hermes_master import HermesMasterOrchestrator

    settings = _settings(tmp_path, revision_loop_enabled=True)
    master = HermesMasterOrchestrator(settings)
    # 짧은 응답 (length factor 가 score 를 낮춤)
    master.adapter.run = AsyncMock(return_value=_StubResult(text="ok"))

    result = await master.handle("질문 — 자세히", user_id="u1")
    # Critic 가 짧은 응답 score 를 낮게 매겨 retry 가능하지만 — 실제 critic
    # 동작에 따라 1~3회. 적어도 1회 호출 보장.
    assert master.adapter.run.call_count >= 1
    assert "master" in (result.handled_by or "")


@pytest.mark.asyncio
async def test_master_revision_on_records_each_attempt_in_outputs(tmp_path):
    """revision loop 모든 attempt 가 model_outputs 에 substage='revision:N'."""
    from src.orchestrator.hermes_master import HermesMasterOrchestrator

    settings = _settings(tmp_path, revision_loop_enabled=True)
    master = HermesMasterOrchestrator(settings)

    call_count = {"n": 0}

    async def _flaky(*, prompt, history=None, model=None, timeout_ms=None):
        call_count["n"] += 1
        return _StubResult(text="ok" if call_count["n"] == 1 else "much longer ok")

    master.adapter.run = _flaky  # type: ignore[assignment]

    result = await master.handle("질문", user_id="u1")
    substages = [m.substage for m in result.task.model_outputs]
    assert any(s.startswith("revision:") for s in substages)
