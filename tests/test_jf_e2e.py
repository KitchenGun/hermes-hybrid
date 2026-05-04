"""End-to-end tests: Orchestrator routes a message through Job Factory v2.

The orchestrator's `_handle_via_job_factory_v2` is the integration
point. We stub the dispatcher so the test stays fast (no Ollama / no
OpenAI / no subprocess), then verify the orchestrator translates each
DispatchOutcome correctly to its own OrchestratorResult shape and
TaskState fields.

Coverage:
  * Flag off → legacy router fires (sanity).
  * Flag on + ok outcome → succeeded, response wired through.
  * Flag on + needs_approval → degraded with approval message.
  * Flag on + denied_cloud → degraded with cap message.
  * Flag on + exhausted → degraded with best-step text.
  * Flag on + dispatcher build error → degraded with init-error message.
  * Heavy / forced_profile / rule still take precedence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest

from src.config import Settings
from src.job_factory.classifier import JobClassification
from src.job_factory.dispatcher import (
    ApprovalRequest,
    DispatchResult,
    StepRecord,
)
from src.orchestrator import Orchestrator
from src.state import Repository

# Reuse the test_orchestrator fakes for non-v2 paths (rule, heavy, etc.).
from tests.test_orchestrator import (  # type: ignore[import-not-found]
    _build_orch,
    _claude_result,
    _resp,
)


# ---- Stub dispatcher ------------------------------------------------------


@dataclass
class _StubDispatcher:
    """Returns a canned DispatchResult on dispatch().

    Used in place of the real JobFactoryDispatcher via monkeypatching
    the orchestrator's lazy builder.
    """

    canned: DispatchResult
    last_message: str = ""

    async def dispatch(self, message: str) -> DispatchResult:
        self.last_message = message
        return self.canned


def _classification(job_type: str = "simple_chat") -> JobClassification:
    return JobClassification(
        job_type=job_type, confidence=0.9, method="keyword",
    )


def _ok_result(text: str = "✅ done") -> DispatchResult:
    return DispatchResult(
        outcome="ok",
        job_type="simple_chat",
        classification=_classification(),
        steps=[StepRecord(
            provider="ollama", model="qwen2.5:7b",
            matrix_key="ollama/qwen2.5:7b",
            selection_reason="exploitation",
            score=85.0, passed=True,
            response_text=text,
        )],
        final_text=text,
    )


def _needs_approval_result() -> DispatchResult:
    return DispatchResult(
        outcome="needs_approval",
        job_type="heavy_project_task",
        classification=_classification("heavy_project_task"),
        approval_request=ApprovalRequest(
            matrix_key="claude_cli/sonnet",
            provider="claude_cli",
            model="sonnet",
            reason="job_type 'heavy_project_task' requires_user_approval=True",
            estimated_cost_usd=0.0,
            triggered_rule="job_requires_approval",
        ),
    )


def _denied_cloud_result() -> DispatchResult:
    return DispatchResult(
        outcome="denied_cloud",
        job_type="summarize",
        classification=_classification("summarize"),
        steps=[StepRecord(
            provider="ollama", model="weak",
            matrix_key="ollama/weak",
            selection_reason="exploitation",
            score=30.0, passed=False,
        )],
    )


def _exhausted_result() -> DispatchResult:
    return DispatchResult(
        outcome="exhausted",
        job_type="simple_chat",
        classification=_classification(),
        steps=[StepRecord(
            provider="ollama", model="m1",
            matrix_key="ollama/m1",
            selection_reason="exploitation",
            score=40.0, passed=False,
            response_text="meh response",
        )],
        final_text="meh response",
    )


# ---- Helpers --------------------------------------------------------------


def _settings_v2_on(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        discord_bot_token="",
        require_allowlist=False,
        ollama_enabled=False,
        state_db_path=tmp_path / "test.db",
        use_new_job_factory=True,    # Phase 7 flag ON
    )


def _settings_v2_off(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        discord_bot_token="",
        require_allowlist=False,
        ollama_enabled=False,
        state_db_path=tmp_path / "test.db",
        use_new_job_factory=False,   # default
    )


def _patch_dispatcher(orch, dispatcher_stub) -> None:
    """Pre-populate the orch's lazy v2 dispatcher with a stub."""
    orch._job_factory_v2 = dispatcher_stub


# ---- Phase 7 wiring tests -------------------------------------------------


@pytest.mark.asyncio
async def test_v2_flag_off_uses_legacy_path(tmp_path):
    """Sanity: with ``use_new_job_factory=False`` the orchestrator never
    builds the v2 dispatcher."""
    settings = _settings_v2_off(tmp_path)
    o = _build_orch(
        settings,
        local_scripts=[_resp("legacy reply", "gpt-4o-mini")],
    )
    # Pre-build a dispatcher stub that would crash if called.
    crash = _StubDispatcher(canned=_ok_result())
    o._job_factory_v2 = crash

    result = await o.handle("hello", user_id="u1")
    # Stub never invoked.
    assert crash.last_message == ""
    # Legacy path took the request.
    assert result.handled_by != "v2:ok:ollama/qwen2.5:7b:exploitation"


@pytest.mark.asyncio
async def test_v2_flag_on_routes_to_dispatcher_ok(tmp_path):
    settings = _settings_v2_on(tmp_path)
    o = _build_orch(settings)
    stub = _StubDispatcher(canned=_ok_result("hello from v2"))
    _patch_dispatcher(o, stub)

    result = await o.handle("hi", user_id="u1")
    assert stub.last_message == "hi"
    assert result.task.status == "succeeded"
    assert result.response == "hello from v2"
    assert result.handled_by.startswith("v2:ok:")
    assert result.task.job_profile_id == "simple_chat"


@pytest.mark.asyncio
async def test_v2_needs_approval_degrades_with_message(tmp_path):
    settings = _settings_v2_on(tmp_path)
    o = _build_orch(settings)
    stub = _StubDispatcher(canned=_needs_approval_result())
    _patch_dispatcher(o, stub)

    result = await o.handle("refactor everything", user_id="u1")
    assert result.task.status == "failed"
    assert result.task.degraded is True
    assert "승인" in result.response
    assert result.handled_by.startswith("v2:needs_approval")


@pytest.mark.asyncio
async def test_v2_denied_cloud_explains_cap(tmp_path):
    settings = _settings_v2_on(tmp_path)
    o = _build_orch(settings)
    stub = _StubDispatcher(canned=_denied_cloud_result())
    _patch_dispatcher(o, stub)

    result = await o.handle("summarize this", user_id="u1")
    assert result.task.status == "failed"
    assert "한도" in result.response
    assert result.handled_by.startswith("v2:denied_cloud")


@pytest.mark.asyncio
async def test_v2_exhausted_returns_best_step_text(tmp_path):
    settings = _settings_v2_on(tmp_path)
    o = _build_orch(settings)
    stub = _StubDispatcher(canned=_exhausted_result())
    _patch_dispatcher(o, stub)

    result = await o.handle("question", user_id="u1")
    assert result.task.status == "failed"
    assert result.task.degraded is True
    # Best (= last failed) step's response_text surfaces.
    assert "meh response" in result.response


@pytest.mark.asyncio
async def test_v2_no_local_models_message(tmp_path):
    settings = _settings_v2_on(tmp_path)
    o = _build_orch(settings)
    stub = _StubDispatcher(canned=DispatchResult(
        outcome="no_local_models",
        job_type="simple_chat",
        classification=_classification(),
    ))
    _patch_dispatcher(o, stub)

    result = await o.handle("hi", user_id="u1")
    assert "Ollama" in result.response or "로컬" in result.response


@pytest.mark.asyncio
async def test_v2_dispatcher_build_failure_degrades(tmp_path):
    """If the lazy build crashes (e.g., missing config), orchestrator
    degrades cleanly rather than 500ing the bot."""
    settings = _settings_v2_on(tmp_path)
    o = _build_orch(settings)

    # Force build failure by patching the lazy builder to raise.
    def _crashy(_settings, **_kwargs):
        raise RuntimeError("config not found")

    with patch(
        "src.job_factory.builder.build_job_factory_dispatcher",
        side_effect=_crashy,
    ):
        result = await o.handle("hi", user_id="u1")

    assert result.task.degraded is True
    assert "init failed" in result.response or "v2" in result.response.lower()
    assert result.handled_by == "v2-init-error"


# ---- Gate precedence ------------------------------------------------------


@pytest.mark.asyncio
async def test_v2_yields_to_heavy_path(tmp_path):
    """`!heavy` always wins over v2."""
    settings = _settings_v2_on(tmp_path)
    o = _build_orch(
        settings,
        claude_scripts=[_claude_result("heavy reply")],
    )
    stub = _StubDispatcher(canned=_ok_result("v2 reply"))
    _patch_dispatcher(o, stub)

    result = await o.handle("anything", user_id="u1", heavy=True)
    # Heavy path → claude-max.
    assert result.handled_by == "claude-max"
    assert "heavy reply" in result.response
    # Stub never invoked.
    assert stub.last_message == ""


@pytest.mark.asyncio
async def test_v2_yields_to_forced_profile(tmp_path):
    """forced_profile (e.g., #일기 channel) always wins over v2."""
    settings = _settings_v2_on(tmp_path)
    o = _build_orch(settings)
    # Hermes adapter scripted to return a profile-driven response.
    from src.hermes_adapter.adapter import HermesResult
    o.hermes._scripts = [HermesResult(  # type: ignore[attr-defined]
        text="profile reply", session_id="s",
        tier_used="L2", model_name="x",
        provider="custom",
        duration_ms=10,
        stdout_raw="", stderr_raw="",
        prompt_tokens=10, completion_tokens=5,
    )]
    stub = _StubDispatcher(canned=_ok_result("v2 reply"))
    _patch_dispatcher(o, stub)

    result = await o.handle(
        "log this", user_id="u1", forced_profile="journal_ops",
    )
    # forced_profile bypasses v2.
    assert "profile reply" in result.response
    assert stub.last_message == ""


@pytest.mark.asyncio
async def test_v2_yields_to_rule_layer(tmp_path):
    """`/ping` always wins over v2 (rule layer is upstream)."""
    settings = _settings_v2_on(tmp_path)
    o = _build_orch(settings)
    stub = _StubDispatcher(canned=_ok_result("v2 reply"))
    _patch_dispatcher(o, stub)

    result = await o.handle("/ping", user_id="u1")
    assert result.handled_by == "rule"
    assert result.response == "pong"
    assert stub.last_message == ""
