"""Phase 12 — pipeline workflow tests.

Locks down:
  * PipelineCatalog YAML 로드 + match by trigger_keyword
  * PipelineRunner sequential 실행 — N 단계, 각 단계마다 adapter.run 1회
  * 단계 사이 결과 hand-off (prior 응답이 다음 prompt 에 prepend)
  * 한 단계 실패해도 다음 단계 진행 (graceful)
  * unknown handle → 그 단계만 실패, 나머지 OK
  * IntentRouter 가 trigger_keyword 매치 시 pipeline_id stamp
  * @handle 명시 mention 이 있으면 pipeline_id stamp 안 함 (mention 우선)
  * HermesMaster 가 pipeline_id 있을 때 _dispatch_pipeline 호출
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.agents import AgentRegistry
from src.config import Settings
from src.integration import IntentRouter
from src.orchestrator.pipeline_runner import PipelineRunner
from src.orchestrator.pipelines import Pipeline, PipelineCatalog


_REPO_ROOT = Path(__file__).resolve().parent.parent


def _settings(**overrides) -> Settings:
    base = {
        "_env_file": None,
        "discord_bot_token": "",
        "discord_allowed_user_ids": "",
        "require_allowlist": False,
        "ollama_enabled": False,
        "experience_log_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _registry() -> AgentRegistry:
    return AgentRegistry(repo_root=_REPO_ROOT)


# ---- PipelineCatalog -----------------------------------------------------


def test_catalog_loads_yaml_and_finds_pipelines():
    cat = PipelineCatalog()
    pipelines = cat.all()
    # Plan 의 4 pipelines.
    assert "feature_dev" in pipelines
    assert "bug_fix" in pipelines
    assert "security_review" in pipelines
    assert "refactor" in pipelines


def test_feature_dev_has_8_stages_in_order():
    cat = PipelineCatalog()
    fd = cat.get("feature_dev")
    assert fd is not None
    assert fd.sequence == (
        "@finder", "@analyst", "@architect", "@planner",
        "@coder", "@reviewer", "@tester", "@documenter",
    )


def test_match_by_korean_keyword():
    cat = PipelineCatalog()
    p = cat.match("이 모듈에 새 기능 추가해줘")
    assert p is not None
    assert p.pipeline_id == "feature_dev"


def test_match_by_english_keyword():
    cat = PipelineCatalog()
    p = cat.match("Please implement a feature for X")
    assert p is not None
    assert p.pipeline_id == "feature_dev"


def test_no_match_returns_none():
    cat = PipelineCatalog()
    assert cat.match("안녕 오늘 날씨") is None
    assert cat.match("") is None


def test_bug_fix_match():
    cat = PipelineCatalog()
    p = cat.match("이 함수에 버그 있어")
    assert p is not None
    assert p.pipeline_id == "bug_fix"
    assert p.sequence[0] == "@finder"
    assert "@debugger" in p.sequence
    assert "@fixer" in p.sequence


# ---- PipelineRunner ------------------------------------------------------


@dataclass
class _StubAdapterResult:
    text: str = "stage response"
    model_name: str = "opus"
    input_tokens: int = 30
    output_tokens: int = 20
    duration_ms: int = 100
    session_id: str = "s1"
    total_cost_usd: float = 0.0


class _StubAdapter:
    def __init__(self, *, raises: Exception | None = None):
        self.calls: list[dict[str, Any]] = []
        self._raises = raises

    async def run(self, *, prompt: str, history=None, model=None, timeout_ms=None):
        self.calls.append({"prompt": prompt, "model": model, "timeout_ms": timeout_ms})
        if self._raises is not None:
            raise self._raises
        return _StubAdapterResult(text=f"resp[{len(self.calls)}]")


def _make_pipeline(*handles: str, checkpoints: tuple[str, ...] = ()) -> Pipeline:
    return Pipeline(
        pipeline_id="test_pipeline",
        description="test",
        trigger_keywords=("test",),
        sequence=tuple(handles),
        checkpoint_after=checkpoints,
    )


@pytest.mark.asyncio
async def test_runner_executes_each_stage_once():
    adapter = _StubAdapter()
    runner = PipelineRunner(adapter, _registry())
    pipeline = _make_pipeline("@finder", "@analyst", "@coder")
    result = await runner.run(pipeline=pipeline, user_message="X 분석")

    assert result.completed
    assert len(result.stages) == 3
    assert len(adapter.calls) == 3
    assert all(s.success for s in result.stages)
    assert [s.handle for s in result.stages] == ["@finder", "@analyst", "@coder"]


@pytest.mark.asyncio
async def test_prior_stage_response_prepended_to_next_prompt():
    adapter = _StubAdapter()
    runner = PipelineRunner(adapter, _registry())
    pipeline = _make_pipeline("@finder", "@analyst")
    await runner.run(pipeline=pipeline, user_message="x")

    # 두 번째 호출 prompt 안에 첫 단계의 ``[prior:@finder]`` transcript 포함
    second_prompt = adapter.calls[1]["prompt"]
    assert "[prior:@finder]" in second_prompt
    assert "resp[1]" in second_prompt   # first stage's response


@pytest.mark.asyncio
async def test_unknown_handle_fails_only_that_stage():
    adapter = _StubAdapter()
    runner = PipelineRunner(adapter, _registry())
    pipeline = _make_pipeline("@finder", "@nobody", "@analyst")
    result = await runner.run(pipeline=pipeline, user_message="x")

    assert len(result.stages) == 3
    # adapter 호출은 2번 (unknown handle 은 호출 X)
    assert len(adapter.calls) == 2
    assert result.stages[0].success is True
    assert result.stages[1].success is False
    assert "unknown agent handle" in result.stages[1].error
    assert result.stages[2].success is True
    assert result.failed_count == 1
    assert result.succeeded_count == 2


@pytest.mark.asyncio
async def test_adapter_exception_does_not_abort_pipeline():
    """One stage 가 raises 해도 다음 단계 진행."""

    call_count = {"n": 0}

    class _Flaky:
        async def run(self, *, prompt, history=None, model=None, timeout_ms=None):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("simulated failure")
            return _StubAdapterResult()

    runner = PipelineRunner(_Flaky(), _registry())
    pipeline = _make_pipeline("@finder", "@analyst", "@coder")
    result = await runner.run(pipeline=pipeline, user_message="x")

    assert len(result.stages) == 3
    assert result.stages[0].success is True
    assert result.stages[1].success is False
    assert "RuntimeError" in result.stages[1].error
    assert result.stages[2].success is True


@pytest.mark.asyncio
async def test_progress_callback_fires_per_stage():
    adapter = _StubAdapter()
    runner = PipelineRunner(adapter, _registry())
    pipeline = _make_pipeline(
        "@finder", "@analyst", checkpoints=("@analyst",)
    )

    events: list[tuple[int, int, str, str]] = []

    async def progress(idx, total, handle, status):
        events.append((idx, total, handle, status))

    await runner.run(pipeline=pipeline, user_message="x", progress=progress)

    # 각 단계 start + done/checkpoint = 2 이벤트 × 2 단계 = 4
    assert len(events) == 4
    assert events[0] == (0, 2, "@finder", "start")
    assert events[1] == (0, 2, "@finder", "done")
    assert events[2] == (1, 2, "@analyst", "start")
    assert events[3] == (1, 2, "@analyst", "checkpoint")  # checkpoint_after


def test_aggregate_text_format():
    adapter = _StubAdapter()
    runner = PipelineRunner(adapter, _registry())
    pipeline = _make_pipeline("@finder", "@analyst")
    result = asyncio.run(runner.run(pipeline=pipeline, user_message="x"))
    out = result.aggregate_text()
    assert "### @finder (1/2)" in out
    assert "### @analyst (2/2)" in out


# ---- IntentRouter pipeline_id stamp -------------------------------------


@pytest.mark.asyncio
async def test_intent_router_stamps_pipeline_id_on_keyword_match():
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="이 모듈에 새 기능 추가해줘",
        user_id="u1",
        session_id="s1",
    )
    assert result.pipeline_id == "feature_dev"
    assert not result.short_circuited


@pytest.mark.asyncio
async def test_explicit_handle_mention_skips_pipeline_match():
    """@handle 명시 mention 이 있으면 pipeline_id stamp 안 함."""
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="@coder 새 기능 추가",   # has both @handle and pipeline keyword
        user_id="u1",
        session_id="s1",
    )
    assert result.agent_handles == ["@coder"]
    assert result.pipeline_id is None        # mention 우선


@pytest.mark.asyncio
async def test_no_keyword_no_pipeline():
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="안녕",
        user_id="u1",
        session_id="s1",
    )
    assert result.pipeline_id is None
    assert result.agent_handles == []


@pytest.mark.asyncio
async def test_rule_layer_short_circuit_does_not_run_pipeline():
    """RuleLayer 단락 시 pipeline_id 도 stamp 되지만 master 는 안 호출됨.
    (pipeline 실행 자체는 master 분기에서만)."""
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="/ping",
        user_id="u1",
        session_id="s1",
    )
    assert result.handled_by == "rule"
    # /ping 자체에 pipeline keyword 가 없으니 pipeline_id 도 None
    assert result.pipeline_id is None


# ---- HermesMaster pipeline dispatch -------------------------------------


@pytest.mark.asyncio
async def test_master_dispatches_pipeline_when_pipeline_id_set(tmp_path):
    """master.handle 이 pipeline_id 가 있으면 _dispatch_pipeline 호출."""
    from src.orchestrator.hermes_master import HermesMasterOrchestrator

    settings = _settings(
        experience_log_root=tmp_path / "exp",
        state_db_path=tmp_path / "test.db",
    )
    master = HermesMasterOrchestrator(settings)

    # adapter mock — 모든 stage 가 같은 응답 반환
    master.adapter.run = AsyncMock(
        return_value=_StubAdapterResult(text="stage out")
    )

    result = await master.handle(
        "이 함수에 버그 있어",  # bug_fix pipeline trigger
        user_id="u1",
    )

    # bug_fix = 5 stages: @finder @debugger @fixer @reviewer @tester
    assert result.handled_by.startswith("master:pipeline:bug_fix") or \
           result.handled_by.startswith("master:pipeline_")
    assert master.adapter.run.call_count == 5
    assert "### @finder" in result.response
    assert "### @debugger" in result.response


@pytest.mark.asyncio
async def test_master_pipeline_records_each_stage_in_experience_log(tmp_path):
    """ExperienceLog 의 model_outputs[] 에 단계별 substage='pipeline:@<handle>'."""
    from src.core import ExperienceLogger
    from src.orchestrator.hermes_master import HermesMasterOrchestrator

    log_dir = tmp_path / "exp"
    settings = _settings(
        experience_log_root=log_dir,
        state_db_path=tmp_path / "test.db",
        experience_log_enabled=True,
    )
    logger = ExperienceLogger(log_dir, enabled=True)
    master = HermesMasterOrchestrator(settings, experience_logger=logger)
    master.adapter.run = AsyncMock(return_value=_StubAdapterResult(text="ok"))

    result = await master.handle("보안 감사해줘", user_id="u1")

    # security_review = 4 stages
    assert master.adapter.run.call_count == 4
    substages = [m.substage for m in result.task.model_outputs]
    assert any(s.startswith("pipeline:@") for s in substages)

    # ExperienceLog row
    files = list(log_dir.glob("*.jsonl"))
    assert len(files) == 1
    import json
    rec = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert rec["pipeline_id"] == "security_review"
    assert rec["pipeline_stage_count"] == 4
