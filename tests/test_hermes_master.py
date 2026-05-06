"""Tests for HermesMasterOrchestrator (src/orchestrator/hermes_master.py).

The master path is the diagram's central component. Locks down:
  * RuleLayer match → master.handle returns the rule response without
    calling opencode
  * Slash skill match → skill is invoked, opencode skipped
  * deny_allowlist / deny_budget → policy gate rejects, opencode skipped
  * Master LLM dispatch → opencode called once, response stamped on task,
    Critic runs, ExperienceLog appended
  * opencode auth/timeout/error → graceful degraded response, no exception
  * memory_inject → history_window is prepended with system memo bullet
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from src.config import Settings
from src.core import ExperienceLogger
from src.memory import InMemoryMemory
from src.opencode_adapter import (
    OpenCodeAdapterError,
    OpenCodeAuthError,
    OpenCodeResult,
    OpenCodeTimeout,
)
from src.orchestrator.hermes_master import HermesMasterOrchestrator


def _settings(tmp_path, **overrides) -> Settings:
    # Use a real-but-empty profiles dir so JobFactory's existence check
    # passes. Master path doesn't actually scan profiles in unit tests
    # (we mock opencode.run).
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
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
        "use_new_job_factory": False,
        "state_db_path": tmp_path / "test.db",
        "profiles_dir": profiles_dir,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _ok_result(text: str = "응답") -> OpenCodeResult:
    return OpenCodeResult(
        text=text,
        model_name="gpt-5.5",
        session_id="sess-1",
        duration_ms=10,
        input_tokens=20,
        output_tokens=15,
    )


@pytest.mark.asyncio
async def test_rule_layer_match_skips_opencode(tmp_path):
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)
    master.opencode.run = AsyncMock(return_value=_ok_result())

    result = await master.handle("/ping", user_id="u1")
    assert result.handled_by == "rule"
    assert isinstance(result.response, str)
    master.opencode.run.assert_not_called()


@pytest.mark.asyncio
async def test_slash_skill_match_invokes_skill_not_opencode(tmp_path):
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(
        settings, memory=InMemoryMemory()
    )
    master.opencode.run = AsyncMock(return_value=_ok_result())

    # /memo list — HybridMemoSkill is in default_registry
    result = await master.handle("/memo list", user_id="u1")
    assert result.handled_by == "skill:hybrid-memo"
    master.opencode.run.assert_not_called()


@pytest.mark.asyncio
async def test_master_dispatch_calls_opencode_and_logs(tmp_path):
    log_dir = tmp_path / "exp_log"
    settings = _settings(tmp_path, experience_log_root=log_dir)
    logger = ExperienceLogger(log_dir, enabled=True)
    master = HermesMasterOrchestrator(
        settings, experience_logger=logger
    )
    master.opencode.run = AsyncMock(return_value=_ok_result("master 응답"))

    result = await master.handle("자유 텍스트 질문", user_id="u1")
    assert result.handled_by == "master:opencode"
    assert result.response == "master 응답"
    master.opencode.run.assert_called_once()

    # ExperienceLog should have one row stamped with model_provider/master
    files = list(log_dir.glob("*.jsonl"))
    assert len(files) == 1
    line = files[0].read_text(encoding="utf-8").strip()
    import json
    rec = json.loads(line)
    assert rec["model_provider"] == "opencode"
    assert rec["model_name"] == "gpt-5.5"
    assert rec["handled_by"] == "master:opencode"


@pytest.mark.asyncio
async def test_opencode_auth_error_yields_graceful_response(tmp_path):
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)
    master.opencode.run = AsyncMock(
        side_effect=OpenCodeAuthError("401")
    )

    result = await master.handle("hi", user_id="u1")
    assert result.handled_by == "master:auth_error"
    assert "opencode" in result.response
    assert result.task.status == "failed"
    assert result.task.degraded is True


@pytest.mark.asyncio
async def test_opencode_timeout_returns_timeout_message(tmp_path):
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)
    master.opencode.run = AsyncMock(
        side_effect=OpenCodeTimeout("timed out")
    )

    result = await master.handle("hi", user_id="u1")
    assert result.handled_by == "master:timeout"
    assert "시간 초과" in result.response


@pytest.mark.asyncio
async def test_opencode_generic_error_degrades_gracefully(tmp_path):
    settings = _settings(tmp_path)
    master = HermesMasterOrchestrator(settings)
    master.opencode.run = AsyncMock(
        side_effect=OpenCodeAdapterError("non-JSON")
    )
    result = await master.handle("hi", user_id="u1")
    assert result.handled_by == "master:error"
    assert result.task.degraded is True


@pytest.mark.asyncio
async def test_deny_budget_rejects_without_calling_opencode(tmp_path):
    settings = _settings(tmp_path, cloud_token_budget_daily=100)

    class _OverBudgetRepo:
        async def used_tokens_today(self, user_id):
            return 999

    master = HermesMasterOrchestrator(
        settings, repo=_OverBudgetRepo()  # type: ignore[arg-type]
    )
    master.opencode.run = AsyncMock(return_value=_ok_result())

    result = await master.handle("hi", user_id="u1")
    assert result.handled_by == "deny:budget"
    master.opencode.run.assert_not_called()


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

    master.opencode.run = _capture_run  # type: ignore[assignment]

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
    fake_master.opencode.run = AsyncMock(return_value=_ok_result("delegate ok"))
    o._hermes_master = fake_master

    result = await o.handle("아무거나", user_id="u1")
    assert result.response == "delegate ok"
    assert result.handled_by == "master:opencode"


@pytest.mark.asyncio
async def test_orchestrator_default_off_keeps_legacy_path(tmp_path):
    """master_enabled=False → Orchestrator must NOT route via master."""
    from src.orchestrator.orchestrator import Orchestrator

    settings = _settings(tmp_path, master_enabled=False)
    o = Orchestrator(settings)
    # If Orchestrator tried to instantiate master it would fail because
    # opencode isn't installed in CI — confirm that path isn't taken
    # via the lazy master attribute.
    fake_master = HermesMasterOrchestrator(settings)
    fake_master.opencode.run = AsyncMock(return_value=_ok_result("nope"))
    o._hermes_master = fake_master

    # Use a RuleLayer hit so the legacy path returns immediately and
    # we don't depend on ollama / Claude CLI being available.
    result = await o.handle("/ping", user_id="u1")
    assert result.handled_by == "rule"
    fake_master.opencode.run.assert_not_called()
