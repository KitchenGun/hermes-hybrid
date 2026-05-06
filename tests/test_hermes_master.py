"""Tests for HermesMasterOrchestrator (src/orchestrator/hermes_master.py).

The master path is the diagram's central component. Locks down:
  * RuleLayer match → master.handle returns the rule response without
    calling claude
  * Slash skill match → skill is invoked, claude skipped
  * deny_allowlist / deny_budget → policy gate rejects, claude skipped
  * Master LLM dispatch → claude called once, response stamped on task,
    Critic runs, ExperienceLog appended
  * claude auth/timeout/error → graceful degraded response, no exception
  * memory_inject → history_window is prepended with system memo bullet
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from src.config import Settings
from src.core import ExperienceLogger
from src.memory import InMemoryMemory
from src.claude_adapter import (
    ClaudeCodeAdapterError,
    ClaudeCodeAuthError,
    ClaudeCodeResult,
    ClaudeCodeTimeout,
)
from src.orchestrator.hermes_master import HermesMasterOrchestrator


def _settings(tmp_path, **overrides) -> Settings:
    """Build a Settings for master orchestrator tests. Phase 8 후 profiles_dir
    필드는 제거됐으므로 master 가 agents/ 만 본다."""
    base = {
        "_env_file": None,
        "discord_bot_token": "",
        "discord_allowed_user_ids": "",
        "require_allowlist": False,
        "ollama_enabled": False,
        "experience_log_enabled": False,
        "experience_log_root": tmp_path / "experience",
        "master_enabled": True,
        "memory_inject_enabled": False,
        "state_db_path": tmp_path / "test.db",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _ok_result(text: str = "응답") -> ClaudeCodeResult:
    return ClaudeCodeResult(
        text=text,
        model_name="opus",
        session_id="sess-1",
        duration_ms=10,
        input_tokens=20,
        output_tokens=15,
    )


@pytest.mark.asyncio
async def test_rule_layer_match_skips_claude(tmp_path):
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)
    master.adapter.run = AsyncMock(return_value=_ok_result())

    result = await master.handle("/ping", user_id="u1")
    assert result.handled_by == "rule"
    assert isinstance(result.response, str)
    master.adapter.run.assert_not_called()


@pytest.mark.asyncio
async def test_slash_skill_match_invokes_skill_not_claude(tmp_path):
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(
        settings, memory=InMemoryMemory()
    )
    master.adapter.run = AsyncMock(return_value=_ok_result())

    # /memo list — HybridMemoSkill is in default_registry
    result = await master.handle("/memo list", user_id="u1")
    assert result.handled_by == "skill:hybrid-memo"
    master.adapter.run.assert_not_called()


@pytest.mark.asyncio
async def test_master_dispatch_calls_claude_and_logs(tmp_path):
    log_dir = tmp_path / "exp_log"
    settings = _settings(tmp_path, experience_log_root=log_dir)
    logger = ExperienceLogger(log_dir, enabled=True)
    master = HermesMasterOrchestrator(
        settings, experience_logger=logger
    )
    master.adapter.run = AsyncMock(return_value=_ok_result("master 응답"))

    result = await master.handle("자유 텍스트 질문", user_id="u1")
    assert result.handled_by == "master:claude"
    assert result.response == "master 응답"
    master.adapter.run.assert_called_once()

    # ExperienceLog should have one row stamped with model_provider/master
    files = list(log_dir.glob("*.jsonl"))
    assert len(files) == 1
    line = files[0].read_text(encoding="utf-8").strip()
    import json
    rec = json.loads(line)
    assert rec["model_provider"] == "claude_cli"
    assert rec["model_name"] == "opus"
    assert rec["handled_by"] == "master:claude"


@pytest.mark.asyncio
async def test_claude_auth_error_yields_graceful_response(tmp_path):
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)
    master.adapter.run = AsyncMock(
        side_effect=ClaudeCodeAuthError("401")
    )

    result = await master.handle("hi", user_id="u1")
    assert result.handled_by == "master:auth_error"
    assert "Claude CLI" in result.response
    assert result.task.status == "failed"
    assert result.task.degraded is True


@pytest.mark.asyncio
async def test_claude_timeout_returns_timeout_message(tmp_path):
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)
    master.adapter.run = AsyncMock(
        side_effect=ClaudeCodeTimeout("timed out")
    )

    result = await master.handle("hi", user_id="u1")
    assert result.handled_by == "master:timeout"
    assert "시간 초과" in result.response


@pytest.mark.asyncio
async def test_claude_generic_error_degrades_gracefully(tmp_path):
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)
    master.adapter.run = AsyncMock(
        side_effect=ClaudeCodeAdapterError("non-JSON")
    )
    result = await master.handle("hi", user_id="u1")
    assert result.handled_by == "master:error"
    assert result.task.degraded is True


@pytest.mark.asyncio
async def test_deny_budget_rejects_without_calling_claude(tmp_path):
    settings = _settings(tmp_path, cloud_token_budget_daily=100)

    class _OverBudgetRepo:
        async def used_tokens_today(self, user_id):
            return 999

    master = HermesMasterOrchestrator(
        settings, repo=_OverBudgetRepo()  # type: ignore[arg-type]
    )
    master.adapter.run = AsyncMock(return_value=_ok_result())

    result = await master.handle("hi", user_id="u1")
    assert result.handled_by == "deny:budget"
    master.adapter.run.assert_not_called()


@pytest.mark.asyncio
async def test_memory_inject_prepends_system_memo(tmp_path):
    settings = _settings(
        tmp_path,
        memory_inject_enabled=True,
        memory_inject_top_k=2,
    )
    memory = InMemoryMemory()
    await memory.save("u1", "내일 회의 9시")
    await memory.save("u1", "전혀 무관한 메모")

    master = HermesMasterOrchestrator(settings, memory=memory)

    captured: dict = {}

    async def _capture_run(*, prompt, history, model=None, timeout_ms=None):
        captured["history"] = history
        captured["prompt"] = prompt
        return _ok_result("응답")

    master.adapter.run = _capture_run  # type: ignore[assignment]

    await master.handle("회의 일정 알려줘", user_id="u1")
    history = captured["history"]
    assert history, "history_window should be prepended"
    first = history[0]
    assert first["role"] == "system"
    assert "내일 회의 9시" in first["content"]


@pytest.mark.asyncio
async def test_orchestrator_delegates_to_master_when_enabled(tmp_path):
    """Orchestrator.handle must hand off to master when master_enabled=True."""
    from src.orchestrator.orchestrator import Orchestrator

    settings = _settings(tmp_path)
    o = Orchestrator(settings)
    # patch the lazy-built master
    fake_master = HermesMasterOrchestrator(settings)
    fake_master.adapter.run = AsyncMock(return_value=_ok_result("delegate ok"))
    o._hermes_master = fake_master

    result = await o.handle("아무거나", user_id="u1")
    assert result.response == "delegate ok"
    assert result.handled_by == "master:claude"


@pytest.mark.asyncio
async def test_orchestrator_default_off_keeps_legacy_path(tmp_path):
    """master_enabled=False → Orchestrator must NOT route via master."""
    from src.orchestrator.orchestrator import Orchestrator

    settings = _settings(tmp_path, master_enabled=False)
    o = Orchestrator(settings)
    # If Orchestrator tried to instantiate master it would fail because
    # claude CLI isn't installed in CI — confirm that path isn't taken
    # via the lazy master attribute.
    fake_master = HermesMasterOrchestrator(settings)
    fake_master.adapter.run = AsyncMock(return_value=_ok_result("nope"))
    o._hermes_master = fake_master

    # Use a RuleLayer hit so the legacy path returns immediately and
    # we don't depend on ollama / Claude CLI being available.
    result = await o.handle("/ping", user_id="u1")
    assert result.handled_by == "rule"
    fake_master.adapter.run.assert_not_called()


# ---- Phase 9: agent SKILL.md inject ----------------------------------


@pytest.mark.asyncio
async def test_agent_handle_mention_injects_skill_md_into_prompt(tmp_path):
    """`@coder` 멘션 → master prompt 에 coder SKILL.md frontmatter 가 inject."""
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)

    captured: dict = {}

    async def _capture_run(*, prompt, history, model=None, timeout_ms=None):
        captured["prompt"] = prompt
        return _ok_result("done")

    master.adapter.run = _capture_run  # type: ignore[assignment]

    await master.handle("@coder fizzbuzz 짜줘", user_id="u1")
    prompt = captured["prompt"]
    assert "## Active sub-agent: @coder" in prompt
    assert "role: write_new_code" in prompt
    # description / when_to_use snippets must reach the prompt
    assert "when_to_use:" in prompt
    # User message가 그대로 보존
    assert "fizzbuzz 짜줘" in prompt


@pytest.mark.asyncio
async def test_no_handle_means_no_agent_snippet(tmp_path):
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)

    captured: dict = {}

    async def _capture_run(*, prompt, history, model=None, timeout_ms=None):
        captured["prompt"] = prompt
        return _ok_result("done")

    master.adapter.run = _capture_run  # type: ignore[assignment]

    await master.handle("hello", user_id="u1")
    prompt = captured["prompt"]
    assert "Active sub-agent:" not in prompt


@pytest.mark.asyncio
async def test_multiple_handles_inject_each_snippet(tmp_path):
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)

    captured: dict = {}

    async def _capture_run(*, prompt, history, model=None, timeout_ms=None):
        captured["prompt"] = prompt
        return _ok_result("done")

    master.adapter.run = _capture_run  # type: ignore[assignment]

    await master.handle(
        "@coder 짜고 @reviewer 가 검토해줘", user_id="u1"
    )
    prompt = captured["prompt"]
    assert "## Active sub-agent: @coder" in prompt
    assert "## Active sub-agent: @reviewer" in prompt


@pytest.mark.asyncio
async def test_unknown_handle_does_not_break_prompt(tmp_path):
    """`@nobody` 는 IntentRouter 가 이미 필터링 — prompt 에 영향 없음."""
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)

    captured: dict = {}

    async def _capture_run(*, prompt, history, model=None, timeout_ms=None):
        captured["prompt"] = prompt
        return _ok_result("done")

    master.adapter.run = _capture_run  # type: ignore[assignment]

    await master.handle("@nobody 뭐해", user_id="u1")
    prompt = captured["prompt"]
    assert "Active sub-agent:" not in prompt
    assert "@nobody" in prompt   # user_message 자체에는 그대로 잔존


@pytest.mark.asyncio
async def test_agent_handles_stamped_on_task_and_logged(tmp_path):
    """ExperienceLog 에 agent_handles 가 기록돼야 한다."""
    log_dir = tmp_path / "exp_log"
    settings = _settings(tmp_path, experience_log_root=log_dir)
    logger = ExperienceLogger(log_dir, enabled=True)
    master = HermesMasterOrchestrator(settings, experience_logger=logger)
    master.adapter.run = AsyncMock(return_value=_ok_result("done"))

    result = await master.handle(
        "@coder 짜고 @reviewer 검토", user_id="u1"
    )
    assert result.task.agent_handles == ["@coder", "@reviewer"]

    # ExperienceLog 에도 동일하게
    files = list(log_dir.glob("*.jsonl"))
    assert len(files) == 1
    import json
    line = files[0].read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["agent_handles"] == ["@coder", "@reviewer"]


# ---- Phase 10: parallel @handle dispatch -----------------------------


@pytest.mark.asyncio
async def test_parallel_off_default_uses_single_master_call(tmp_path):
    """Default master_parallel_agents=False — 2+ handles 도 단일 호출."""
    settings = _settings(tmp_path)
    assert settings.master_parallel_agents is False

    master = HermesMasterOrchestrator(settings)
    master.adapter.run = AsyncMock(return_value=_ok_result("once"))

    result = await master.handle(
        "@coder + @reviewer 작업해", user_id="u1"
    )
    # 단일 master 호출만 — fan-out 안 함
    assert master.adapter.run.call_count == 1
    assert result.handled_by == "master:claude"
    assert result.response == "once"


@pytest.mark.asyncio
async def test_parallel_on_with_two_handles_fans_out(tmp_path):
    """master_parallel_agents=True + 2 handles → 각 agent 별 호출."""
    settings = _settings(tmp_path, master_parallel_agents=True)
    master = HermesMasterOrchestrator(settings)
    master.adapter.run = AsyncMock(return_value=_ok_result("agent ok"))

    result = await master.handle(
        "@coder 짜고 @reviewer 검토", user_id="u1"
    )
    # 2 handles → claude 2번 호출
    assert master.adapter.run.call_count == 2
    assert result.handled_by == "master:parallel"
    # aggregate_responses 형식 확인
    assert "### @coder" in result.response
    assert "### @reviewer" in result.response


@pytest.mark.asyncio
async def test_parallel_on_with_single_handle_uses_single_call(tmp_path):
    """단일 handle 은 fan-out 안 함 — Phase 9 inject 만으로 충분."""
    settings = _settings(tmp_path, master_parallel_agents=True)
    master = HermesMasterOrchestrator(settings)
    master.adapter.run = AsyncMock(return_value=_ok_result("solo"))

    result = await master.handle("@coder 짜줘", user_id="u1")
    assert master.adapter.run.call_count == 1
    assert result.handled_by == "master:claude"


@pytest.mark.asyncio
async def test_parallel_partial_failure_marks_degraded(tmp_path):
    """일부 agent 실패 → handled_by=master:parallel_partial, degraded=True."""
    settings = _settings(tmp_path, master_parallel_agents=True)
    master = HermesMasterOrchestrator(settings)

    call_count = {"n": 0}

    async def _flaky_run(*, prompt, history, model=None, timeout_ms=None):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise ClaudeCodeAdapterError("simulated failure")
        return _ok_result("ok")

    master.adapter.run = _flaky_run  # type: ignore[assignment]

    result = await master.handle(
        "@coder 짜고 @reviewer 검토", user_id="u1"
    )
    assert result.handled_by == "master:parallel_partial"
    assert result.task.degraded is True
    assert result.task.status == "succeeded"
    # 응답에 실패한 agent 표시
    assert "(failed)" in result.response


@pytest.mark.asyncio
async def test_parallel_full_failure_marks_failed(tmp_path):
    """모든 agent 실패 → handled_by=master:parallel_failed."""
    settings = _settings(tmp_path, master_parallel_agents=True)
    master = HermesMasterOrchestrator(settings)
    master.adapter.run = AsyncMock(
        side_effect=ClaudeCodeAdapterError("all fail")
    )

    result = await master.handle(
        "@coder + @reviewer", user_id="u1"
    )
    assert result.handled_by == "master:parallel_failed"
    assert result.task.status == "failed"
    assert result.task.degraded is True


@pytest.mark.asyncio
async def test_parallel_records_each_agent_token_usage(tmp_path):
    """각 sub-call 의 prompt/completion tokens 가 model_outputs 에 기록."""
    settings = _settings(tmp_path, master_parallel_agents=True)
    master = HermesMasterOrchestrator(settings)
    master.adapter.run = AsyncMock(return_value=_ok_result("response"))

    result = await master.handle(
        "@coder 짜고 @reviewer 검토", user_id="u1"
    )
    # 2개 모델 출력이 기록 — substage 가 parallel:@coder / @reviewer
    substages = [m.substage for m in result.task.model_outputs]
    assert "parallel:@coder" in substages
    assert "parallel:@reviewer" in substages
