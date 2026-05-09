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
        # Phase 21 — keep A/B disabled by default in legacy tests so the
        # arm assignment doesn't randomly skip memory inject. Tests that
        # exercise A/B explicitly opt in via overrides.
        "ab_experiment_enabled": False,
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


# ---- Phase 16: permission-denied circuit breaker ---------------------


@pytest.mark.asyncio
async def test_permission_denied_response_replaced_with_notice(tmp_path):
    """Claude 가 권한 거부 안내문을 응답으로 만들어 보내면 봇이 그 응답을
    그대로 송출하는 대신 settings 점검 안내문으로 교체한다 (회로 차단기)."""
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)
    # Phase 17 게이트: bypassPermissions 모드에선 회로 차단기 비활성화.
    # 회로 차단기 동작 자체를 검증하므로 default 모드로 강제.
    master.adapter.permission_mode = "default"

    # 실제 스크린샷에서 본 거부 안내문 형태
    raw_denied = (
        "파일 쓰기 권한이 다시 거부되었습니다. 사용자 측에서 권한 프롬프트를 "
        "한번 더 승인해주셔야 진행 가능합니다.\n"
        "profiles/mail_ops/accounts.yaml 작성 권한을 허용해주세요."
    )
    master.adapter.run = AsyncMock(return_value=_ok_result(raw_denied))

    result = await master.handle("파일 만들어줘", user_id="u1")
    assert result.handled_by == "master:permission_denied"
    assert "권한 거부가 발생했습니다" in result.response
    assert ".claude/settings.json" in result.response
    # 원본 거부 안내문이 사용자에게 그대로 노출되지 않아야 함
    assert "한번 더 승인해주셔야" not in result.response
    assert result.task.degraded is True
    assert result.task.status == "failed"


@pytest.mark.asyncio
async def test_permission_denied_english_phrase_also_caught(tmp_path):
    """영문 'permission denied' 패턴도 동일하게 잡힌다."""
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)
    master.adapter.permission_mode = "default"  # Phase 17 게이트 우회
    master.adapter.run = AsyncMock(
        return_value=_ok_result(
            "I tried to write the file but got permission denied. "
            "Please approve the prompt."
        )
    )
    result = await master.handle("write something", user_id="u1")
    assert result.handled_by == "master:permission_denied"


@pytest.mark.asyncio
async def test_normal_response_passes_through_circuit_breaker(tmp_path):
    """일반 응답은 detector 영향 받으면 안 됨 — false positive 회귀 방지."""
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)
    master.adapter.run = AsyncMock(
        return_value=_ok_result("여기 코드입니다:\n```python\nprint('hi')\n```")
    )
    result = await master.handle("hello world 짜줘", user_id="u1")
    assert result.handled_by == "master:claude"
    assert "권한 거부" not in result.response


def test_permission_denied_regex_matches_known_phrases():
    """detector 패턴이 실제 관찰된 거부 표현들을 모두 잡는지 직접 검증."""
    from src.orchestrator.hermes_master import _looks_like_permission_denied

    matches = [
        "파일 쓰기 권한이 다시 거부되었습니다",
        "권한 프롬프트를 한번 더 승인해주셔야",
        "작성 권한을 허용해주세요",
        "permission denied",
        "Permission Denied: cannot write",
        "approval is required to continue",
    ]
    for phrase in matches:
        assert _looks_like_permission_denied(phrase), f"missed: {phrase!r}"

    non_matches = [
        "여기 코드입니다",
        "hello world",
        "파일을 작성했습니다",
        "",
    ]
    for phrase in non_matches:
        assert not _looks_like_permission_denied(phrase), (
            f"false positive on: {phrase!r}"
        )


# ---- Phase 21 (2026-05-07): A/B experiment arm stamping ---------------


@pytest.mark.asyncio
async def test_ab_disabled_yields_no_arm_stamp(tmp_path):
    """ab_experiment_enabled=False → ExperienceLog 의 experiment_arm 은 None."""
    log_dir = tmp_path / "exp_log"
    settings = _settings(
        tmp_path,
        experience_log_root=log_dir,
        ab_experiment_enabled=False,
    )
    logger = ExperienceLogger(log_dir, enabled=True)
    master = HermesMasterOrchestrator(settings, experience_logger=logger)
    master.adapter.run = AsyncMock(return_value=_ok_result("done"))

    result = await master.handle("자유 텍스트", user_id="u1")
    assert result.task.experiment_arm is None
    assert result.task.experiment_name is None


@pytest.mark.asyncio
async def test_ab_treatment_arm_runs_memory_inject(tmp_path):
    """ratio=1.0 + memory_inject_enabled → arm='treatment' + memory inject 호출."""
    settings = _settings(
        tmp_path,
        memory_inject_enabled=True,
        memory_inject_top_k=2,
        ab_experiment_enabled=True,
        ab_treatment_ratio=1.0,            # all-treatment
    )
    memory = InMemoryMemory()
    await memory.save("u1", "내일 회의 9시")

    master = HermesMasterOrchestrator(settings, memory=memory)

    captured: dict = {}

    async def _capture_run(*, prompt, history, model=None, timeout_ms=None):
        captured["history"] = history
        return _ok_result("응답")

    master.adapter.run = _capture_run  # type: ignore[assignment]

    result = await master.handle("회의 일정", user_id="u1")
    # treatment arm — inject ran, arm stamped
    assert result.task.experiment_arm == "treatment"
    assert result.task.experiment_name == "memory_inject"
    history = captured["history"]
    assert history and history[0]["role"] == "system"
    assert "내일 회의 9시" in history[0]["content"]


@pytest.mark.asyncio
async def test_ab_control_arm_skips_memory_inject(tmp_path):
    """ratio=0.0 → arm='control' + history_window 비어 있어야."""
    settings = _settings(
        tmp_path,
        memory_inject_enabled=True,
        ab_experiment_enabled=True,
        ab_treatment_ratio=0.0,            # all-control
    )
    memory = InMemoryMemory()
    await memory.save("u1", "내일 회의 9시")

    master = HermesMasterOrchestrator(settings, memory=memory)

    captured: dict = {}

    async def _capture_run(*, prompt, history, model=None, timeout_ms=None):
        captured["history"] = history
        return _ok_result("응답")

    master.adapter.run = _capture_run  # type: ignore[assignment]

    result = await master.handle("회의 일정", user_id="u1")
    assert result.task.experiment_arm == "control"
    # history must NOT contain the system memo prefix
    assert not captured["history"], (
        "control arm must skip memory_inject — history should be empty"
    )


@pytest.mark.asyncio
async def test_ab_treatment_no_hits_sub_label(tmp_path):
    """treatment arm 인데 search miss 면 arm='treatment_no_hits' sub-label."""
    settings = _settings(
        tmp_path,
        memory_inject_enabled=True,
        ab_experiment_enabled=True,
        ab_treatment_ratio=1.0,            # all-treatment
    )
    memory = InMemoryMemory()  # empty memory → search returns []

    master = HermesMasterOrchestrator(settings, memory=memory)
    master.adapter.run = AsyncMock(return_value=_ok_result("응답"))

    result = await master.handle("아무거나", user_id="u1")
    assert result.task.experiment_arm == "treatment_no_hits"


# ---- Phase 24 (2026-05-08): context anchoring fix ---------------------


_LONG_INSTA_TURN = (
    "인스타 자동화 프로젝트 — 현재 의견 정리. 권장 MVP 순서는 "
    "(1) 계정 연결 OAuth, (2) 게시 스케줄러, (3) 분석 대시보드. "
    "각 단계마다 관찰 가능한 메트릭 정의가 필요해."
)


@pytest.mark.asyncio
async def test_followup_after_long_user_turn_injects_topic_anchor(tmp_path):
    """짧은 follow-up + history 의 장문 user turn → prompt 에 anchor 블록.

    재현 시나리오 (input 2): "좋아 진행해보자. 골격 정하고 나에게 보고해라" —
    직전 인스타 자동화 장문 user turn 이 anchor 로 들어가서 master 가
    routing/디버깅 cluster 로 anchor 가 옮겨가지 않게.
    """
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)

    captured: dict = {}

    async def _capture_run(*, prompt, history, model=None, timeout_ms=None):
        captured["prompt"] = prompt
        captured["history"] = history
        return _ok_result("응답")

    master.adapter.run = _capture_run  # type: ignore[assignment]

    history = [
        {"role": "user", "content": _LONG_INSTA_TURN},
        {"role": "assistant", "content": "라우팅 디버깅 진행 — metadata 빈 row 검토 중."},
    ]
    await master.handle(
        "좋아 진행해보자. 골격 정하고 보고해라",
        user_id="u1",
        history=history,
    )
    prompt = captured["prompt"]
    assert "## Recent topic anchor (referenced by follow-up)" in prompt
    assert "인스타 자동화 프로젝트" in prompt
    # anchor 블록이 user message *앞* 에 위치해야 함 (anchor 우선)
    anchor_pos = prompt.index("Recent topic anchor")
    user_pos = prompt.index("## User\n")
    assert anchor_pos < user_pos


@pytest.mark.asyncio
async def test_deictic_followup_after_assistant_debug_turn_uses_long_user_anchor(tmp_path):
    """input 3 재현: '이 내용에 대한 골격을 정하고...' → 직전 assistant 가
    디버깅 응답이어도 가장 가까운 장문 user turn 이 anchor 로 우선."""
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)

    captured: dict = {}

    async def _capture_run(*, prompt, history, model=None, timeout_ms=None):
        captured["prompt"] = prompt
        return _ok_result("응답")

    master.adapter.run = _capture_run  # type: ignore[assignment]

    history = [
        {"role": "user", "content": _LONG_INSTA_TURN},
        {
            "role": "assistant",
            "content": "라우팅 디버그: metadata 빈 row, ExperienceLog 회복 진행.",
        },
    ]
    await master.handle(
        "이 내용에 대한 골격을 정하고 보고해줘라",
        user_id="u1",
        history=history,
    )
    prompt = captured["prompt"]
    assert "Recent topic anchor" in prompt
    assert "인스타 자동화" in prompt


@pytest.mark.asyncio
async def test_no_anchor_block_for_normal_message(tmp_path):
    """일반 (장문 + non-followup) 메시지는 anchor 블록 없음 — false positive 방지."""
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)

    captured: dict = {}

    async def _capture_run(*, prompt, history, model=None, timeout_ms=None):
        captured["prompt"] = prompt
        return _ok_result("응답")

    master.adapter.run = _capture_run  # type: ignore[assignment]

    await master.handle(
        "안녕 오늘 회의 일정 알려줘",
        user_id="u1",
        history=[{"role": "user", "content": _LONG_INSTA_TURN}],
    )
    assert "Recent topic anchor" not in captured["prompt"]


@pytest.mark.asyncio
async def test_curator_block_moved_below_user_message(tmp_path):
    """R3 — auto-curated MEMORY.md 블록이 user message *앞* 에 prepend 되지
    않고 *뒤* 에 'Background context (lower priority)' 라벨로 붙는다.
    짧은 user message 에서 큐레이터 cluster 가 attention 을 압도하던
    회귀 방지."""
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)
    # MemoryCurator 가 비어있지 않은 prepend 를 반환하도록 stub
    master.memory_curator.read_prompt_prepend = lambda: (  # type: ignore[assignment]
        "## User profile (auto-learned)\n자주 쓰는: @coder, @reviewer\n\n"
        "## Recent agent notes (auto-curated)\n- routing debug: metadata 빈 row"
    )

    captured: dict = {}

    async def _capture_run(*, prompt, history, model=None, timeout_ms=None):
        captured["prompt"] = prompt
        return _ok_result("응답")

    master.adapter.run = _capture_run  # type: ignore[assignment]

    await master.handle("hello", user_id="u1")
    prompt = captured["prompt"]
    assert "## User\n" in prompt
    assert "Background context (auto-curated, lower priority)" in prompt
    user_pos = prompt.index("## User\n")
    bg_pos = prompt.index("Background context (auto-curated, lower priority)")
    assert user_pos < bg_pos, (
        "auto-curated 블록은 user message 보다 뒤에 배치돼야 한다"
    )


@pytest.mark.asyncio
async def test_memory_recall_uses_anchor_query_not_short_followup(tmp_path):
    """R2 — follow-up 1턴 ('진행해') 으로 memory.search 하면 무관 cluster 가
    hit 한다. anchor 가 잡히면 그 anchor 를 query 로 사용해야 함."""
    settings = _settings(
        tmp_path,
        memory_inject_enabled=True,
        memory_inject_top_k=2,
    )

    captured_queries: list[str] = []

    class _RecordingMemory:
        async def search(self, user_id, query, k):
            captured_queries.append(query)
            return []

    master = HermesMasterOrchestrator(settings, memory=_RecordingMemory())  # type: ignore[arg-type]
    master.adapter.run = AsyncMock(return_value=_ok_result("응답"))

    await master.handle(
        "좋아 진행해보자. 골격 정하고 보고해라",
        user_id="u1",
        history=[{"role": "user", "content": _LONG_INSTA_TURN}],
    )
    assert captured_queries, "memory.search must be called when inject enabled"
    # query 가 follow-up 1턴이 아닌 장문 anchor 여야 한다
    assert any("인스타 자동화" in q for q in captured_queries)
    assert "진행해보자" not in captured_queries[0]


@pytest.mark.asyncio
async def test_permission_denied_caches_failed_task_context(tmp_path):
    """R4 — _handle_permission_denied 가 final_response 를 교체하기 *전*
    에 원 user_message 를 user 별 캐시 + task.failed_task_context 에 저장."""
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)
    master.adapter.permission_mode = "default"  # Phase 17 게이트 우회

    raw_denied = (
        "파일 쓰기 권한이 다시 거부되었습니다. 사용자 측에서 권한 프롬프트를 "
        "한번 더 승인해주셔야 진행 가능합니다."
    )
    master.adapter.run = AsyncMock(return_value=_ok_result(raw_denied))

    original_msg = "인스타 자동화 MVP 골격 짜서 파일로 저장해줘"
    result = await master.handle(original_msg, user_id="u1")
    assert result.handled_by == "master:permission_denied"

    # in-process 캐시 + task 필드 둘 다 stamp
    assert "u1" in master._failed_task_contexts
    assert master._failed_task_contexts["u1"]["user_message"] == original_msg
    assert result.task.failed_task_context is not None
    assert result.task.failed_task_context["user_message"] == original_msg


@pytest.mark.asyncio
async def test_followup_recovers_failed_task_context_when_history_lacks_anchor(tmp_path):
    """R4 후속 — 권한 거부 직후 follow-up 이 들어오고 history 가 충분치
    않으면 master 가 _failed_task_contexts 에서 원 의도를 복구해 anchor 로
    inject."""
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)
    master.adapter.permission_mode = "default"  # Phase 17 게이트 우회

    # 1턴: 권한 거부 시뮬레이션 — final_response 가 안내문으로 교체됨
    master.adapter.run = AsyncMock(
        return_value=_ok_result(
            "권한 프롬프트를 한번 더 승인해주셔야 진행 가능합니다."
        )
    )
    await master.handle(
        "인스타 자동화 MVP 골격 보고해줘",
        user_id="u1",
    )

    # 2턴: 사용자가 follow-up 으로 다시 요청 — history 에는 안내문만 남았음
    captured: dict = {}

    async def _capture_run(*, prompt, history, model=None, timeout_ms=None):
        captured["prompt"] = prompt
        return _ok_result("정상 응답")

    master.adapter.run = _capture_run  # type: ignore[assignment]

    # discord_bot 이 history 에 안내문 assistant turn 만 남기는 상황을 모사
    history = [
        {"role": "user", "content": "짧"},   # < ANCHOR_MIN_CHARS
        {
            "role": "assistant",
            "content": "⚠️ Claude 측에서 권한 거부가 발생했습니다 …",
        },
    ]
    await master.handle("이 내용 진행해", user_id="u1", history=history)
    prompt = captured["prompt"]
    assert "Recent topic anchor" in prompt
    assert "인스타 자동화" in prompt


@pytest.mark.asyncio
async def test_ab_arm_logged_to_experience_record(tmp_path):
    """ExperienceLog JSONL 에 experiment_arm / experiment_name 이 기록돼야."""
    log_dir = tmp_path / "exp_log"
    settings = _settings(
        tmp_path,
        experience_log_root=log_dir,
        memory_inject_enabled=True,
        ab_experiment_enabled=True,
        ab_treatment_ratio=1.0,
    )
    memory = InMemoryMemory()
    await memory.save("u1", "회의 일정 메모")

    logger = ExperienceLogger(log_dir, enabled=True)
    master = HermesMasterOrchestrator(
        settings, memory=memory, experience_logger=logger,
    )
    master.adapter.run = AsyncMock(return_value=_ok_result("응답"))

    await master.handle("회의 알려줘", user_id="u1")

    files = list(log_dir.glob("*.jsonl"))
    assert len(files) == 1
    import json
    rec = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert rec["experiment_arm"] == "treatment"
    assert rec["experiment_name"] == "memory_inject"
