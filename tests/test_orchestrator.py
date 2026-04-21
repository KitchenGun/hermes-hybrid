"""Orchestrator integration tests — verify risk fixes hold end-to-end.

These tests stub out:
  - HermesAdapter.run (avoid real WSL subprocess)
  - OpenAIClient / AnthropicClient / OllamaClient .generate

so we can exercise every policy branch deterministically.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from src.claude_adapter.adapter import ClaudeCodeAuthError, ClaudeCodeResult
from src.config import Settings
from src.hermes_adapter.adapter import HermesAdapterError, HermesResult
from src.llm.base import LLMResponse, LLMTimeoutError
from src.orchestrator import Orchestrator
from src.state import Repository


# ---- fakes -----------------------------------------------------------------


@dataclass
class _Call:
    messages: list[dict[str, str]]
    max_tokens: int


class _FakeLLM:
    """Stand-in for OpenAI/Anthropic/Ollama client. Scripted responses per call."""

    def __init__(self, model: str, scripts: list[LLMResponse | Exception]):
        self.model = model
        self.name = "fake"
        self._scripts = list(scripts)
        self.calls: list[_Call] = []

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse:
        self.calls.append(_Call(messages=messages, max_tokens=max_tokens))
        if not self._scripts:
            raise RuntimeError(f"{self.model}: no more scripted responses")
        r = self._scripts.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class _FakeHermes:
    """Stand-in for HermesAdapter — scripted run() results."""

    def __init__(self, scripts: list[HermesResult | Exception]):
        self._scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []

    async def run(self, query: str, *, model: str, provider: str, **_: Any) -> HermesResult:
        self.calls.append({"query": query, "model": model, "provider": provider})
        if not self._scripts:
            raise RuntimeError("hermes: no more scripted results")
        r = self._scripts.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class _FakeClaudeCode:
    """Stand-in for ClaudeCodeAdapter — scripted run() results."""

    def __init__(self, scripts: list[ClaudeCodeResult | Exception]):
        self._scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        *,
        prompt: str,
        history: list[dict[str, str]] | None = None,
        model: str | None = None,
        timeout_ms: int | None = None,
        resume_session_id: str | None = None,
        persist_session: bool = False,
    ) -> ClaudeCodeResult:
        self.calls.append({
            "prompt": prompt,
            "history": list(history or []),
            "model": model,
            "resume_session_id": resume_session_id,
            "persist_session": persist_session,
        })
        if not self._scripts:
            raise RuntimeError("claude_code: no more scripted results")
        r = self._scripts.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _resp(text: str, model: str, pt: int = 10, ct: int = 10) -> LLMResponse:
    return LLMResponse(text=text, model=model, prompt_tokens=pt, completion_tokens=ct)


def _hermes_result(text: str, model: str, tier: str = "C1") -> HermesResult:
    return HermesResult(
        text=text, session_id="sid-1", tier_used=tier,  # type: ignore[arg-type]
        model_name=model, provider="openrouter",
        duration_ms=1, stdout_raw="", stderr_raw="",
        prompt_tokens=20, completion_tokens=15,
    )


def _claude_result(text: str, model: str = "claude-sonnet-4-6") -> ClaudeCodeResult:
    return ClaudeCodeResult(
        text=text, model_name=model, session_id="s-1",
        duration_ms=1, input_tokens=10, output_tokens=20,
        total_cost_usd=0.01,
    )


# ---- builder ---------------------------------------------------------------


def _build_orch(settings: Settings, repo: Repository | None = None, *,
                local_scripts=None, worker_scripts=None, main_scripts=None,
                anth_scripts=None, ollama_local_scripts=None, ollama_worker_scripts=None,
                hermes_scripts=None, claude_scripts=None,
                claude_c1_scripts=None) -> Orchestrator:
    o = Orchestrator(settings, repo=repo)
    o._openai_surrogate_local = _FakeLLM(  # type: ignore[assignment]
        settings.openai_model_local_surrogate, local_scripts or [])
    o._openai_surrogate_worker = _FakeLLM(  # type: ignore[assignment]
        settings.openai_model_worker_surrogate, worker_scripts or [])
    o._openai_main = _FakeLLM(settings.openai_model, main_scripts or [])  # type: ignore[assignment]
    o._anthropic = _FakeLLM(settings.anthropic_model, anth_scripts or [])  # type: ignore[assignment]
    o._ollama_local = _FakeLLM(settings.ollama_work_model, ollama_local_scripts or [])  # type: ignore[assignment]
    o._ollama_worker = _FakeLLM(settings.ollama_worker_model, ollama_worker_scripts or [])  # type: ignore[assignment]
    o.hermes = _FakeHermes(hermes_scripts or [])  # type: ignore[assignment]
    o.claude_code = _FakeClaudeCode(claude_scripts or [])  # type: ignore[assignment]
    # Separate C1-Haiku adapter (path A). Fakes are only exercised when
    # settings.c1_backend == "claude_cli".
    o.claude_code_c1 = _FakeClaudeCode(claude_c1_scripts or [])  # type: ignore[assignment]
    return o


# ---- tests -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule_layer_ping_bypasses_llm(settings: Settings):
    o = _build_orch(settings)
    result = await o.handle("/ping", user_id="u1")
    assert result.handled_by == "rule"
    assert result.response == "pong"
    # No LLM was called.
    assert o._openai_surrogate_local.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_local_tier_uses_surrogate_when_ollama_disabled(settings: Settings):
    """R3: Ollama off → local traffic goes to gpt-4o-mini surrogate, NOT Claude."""
    o = _build_orch(
        settings,
        local_scripts=[_resp("local reply", "gpt-4o-mini")],
    )
    result = await o.handle("hi there", user_id="u1")
    assert result.handled_by == "local-surrogate"
    assert result.response == "local reply"
    # Crucially: Anthropic / Hermes never touched.
    assert o._anthropic.calls == []  # type: ignore[attr-defined]
    assert o.hermes.calls == []  # type: ignore[attr-defined]
    # Surrogate token cap was applied.
    assert o._openai_surrogate_local.calls[0].max_tokens == settings.surrogate_max_tokens_local  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_surrogate_cap_at_worker_tier(settings: Settings):
    o = _build_orch(
        settings,
        worker_scripts=[_resp("def foo(): return 42", "gpt-4o")],
    )
    result = await o.handle("def foo(): return 42", user_id="u1")
    assert result.handled_by == "worker-surrogate"
    assert o._openai_surrogate_worker.calls[0].max_tokens == settings.surrogate_max_tokens_worker  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_tier_up_resets_same_tier_counter(settings: Settings):
    """R8: after local tier fails hard, we tier-up, and the fresh tier
    can retry in-place (its same_tier counter is reset)."""
    # local fails with empty → low_quality → tier_up to L3
    # L3 returns valid reply → success
    o = _build_orch(
        settings,
        local_scripts=[_resp("", "gpt-4o-mini")],  # empty triggers low_quality
        worker_scripts=[_resp("ok on worker", "gpt-4o")],
    )
    result = await o.handle("hello", user_id="u1")
    assert result.response == "ok on worker"
    assert result.task.current_tier == "L3"
    # switch_tier reset same_tier_retries to 0 before the successful L3 call.
    assert result.task.same_tier_retries == 0
    assert result.task.tier_up_retries >= 1


@pytest.mark.asyncio
async def test_claude_never_reached_from_local_when_ollama_off(settings: Settings):
    """R2+R3: If local and worker both produce empty, we escalate to C1 GPT-4o,
    NOT Claude. Claude is reached only via the opt-in heavy path."""
    o = _build_orch(
        settings,
        local_scripts=[_resp("", "gpt-4o-mini")],    # low_quality → tier_up
        worker_scripts=[_resp("", "gpt-4o")],        # low_quality → tier_up
        main_scripts=[_resp("gpt-4o answer", "gpt-4o")],
        claude_scripts=[_claude_result("should never be called")],
    )
    result = await o.handle("hello", user_id="u1")
    assert result.response == "gpt-4o answer"
    assert result.task.current_tier == "C1"
    # Claude (Anthropic / Hermes / ClaudeCode) must NOT have been called.
    assert o._anthropic.calls == []  # type: ignore[attr-defined]
    assert o.hermes.calls == []  # type: ignore[attr-defined]
    assert o.claude_code.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_c1_is_auto_escalation_ceiling(settings: Settings):
    """Post heavy-path refactor: validator never emits escalate_claude.
    A C1 failure becomes final_failure, never an auto-burn of a Max session.
    Claude can only be invoked via the explicit !heavy prefix."""
    from src.state import TaskState
    from src.validator import Validator
    v = Validator(settings)
    s = TaskState(session_id="s", user_id="u", user_message="hi",
                  current_tier="C1", retry_budget=4)
    verdict = v.validate(s, output_text="", timed_out=True)
    assert verdict.decision == "final_failure"
    # Either "top" or current tier name must appear in the reason
    assert "C1" in verdict.reason or "top" in verdict.reason.lower()
    # And definitively NOT an escalate_claude decision
    assert verdict.decision != "escalate_claude"


@pytest.mark.asyncio
async def test_non_heavy_never_invokes_claude_even_if_all_tiers_fail(settings: Settings):
    """Full-flow counterpart to the validator unit test above:
    with every auto tier failing, Claude Code adapter must stay untouched."""
    o = _build_orch(
        settings,
        local_scripts=[_resp("", "gpt-4o-mini")],
        worker_scripts=[_resp("", "gpt-4o")],
        main_scripts=[_resp("", "gpt-4o")],  # C1 also empty → final_failure
        claude_scripts=[_claude_result("should never be called")],
    )
    result = await o.handle("hi", user_id="u1")
    assert o.claude_code.calls == []  # type: ignore[attr-defined]
    assert result.task.degraded is True


@pytest.mark.asyncio
async def test_daily_budget_blocks_before_dispatch(settings: Settings, tmp_path):
    """R4: If the user's daily cloud-token usage has already hit the cap,
    the orchestrator refuses before any LLM call."""
    repo = Repository(tmp_path / "r.db")
    await repo.init()
    await repo.add_tokens("u1", settings.cloud_token_budget_daily + 1)

    o = _build_orch(settings, repo=repo, local_scripts=[_resp("should not run", "gpt-4o-mini")])
    result = await o.handle("hi there", user_id="u1")
    assert "budget" in result.response.lower()
    assert result.task.degraded is True
    # LLM never called
    assert o._openai_surrogate_local.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_task_is_persisted(settings: Settings, tmp_path):
    """R4: success path should persist TaskState so /status can find it later."""
    repo = Repository(tmp_path / "r.db")
    await repo.init()
    o = _build_orch(settings, repo=repo, local_scripts=[_resp("done", "gpt-4o-mini")])
    result = await o.handle("hi", user_id="u1")
    got = await repo.get_task(result.task.task_id)
    assert got is not None
    assert got.status == "succeeded"
    assert got.final_response == "done"


@pytest.mark.asyncio
async def test_daily_token_ledger_updated_on_cloud_use(settings: Settings, tmp_path):
    repo = Repository(tmp_path / "r.db")
    await repo.init()
    # force cloud route by triggering prompt that escalates; simplest: make local empty
    # then worker empty then C1 succeed with known token counts
    o = _build_orch(
        settings, repo=repo,
        local_scripts=[_resp("", "gpt-4o-mini")],
        worker_scripts=[_resp("", "gpt-4o")],
        main_scripts=[_resp("cloud reply", "gpt-4o", pt=40, ct=60)],
    )
    result = await o.handle("hi", user_id="u1")
    assert result.task.current_tier == "C1"
    assert await repo.used_tokens_today("u1") == 100  # 40 + 60


@pytest.mark.asyncio
async def test_heavy_path_claude_auth_error_degrades(settings: Settings):
    """Heavy-path Claude Code auth failure (expired OAuth or Max quota out):
    degrade immediately with a clear message — no retries to avoid quota burn."""
    o = _build_orch(
        settings,
        claude_scripts=[ClaudeCodeAuthError("401 unauthorized")],
    )
    result = await o.handle("deep analysis please", user_id="u1", heavy=True)
    assert result.handled_by == "claude-auth"
    assert result.task.degraded is True
    assert result.task.current_tier == "C2"


@pytest.mark.asyncio
async def test_heavy_path_bypasses_rule_and_router(settings: Settings):
    """Even a rule-match input (`/ping`) or a "hi" that would normally go to
    local tier gets routed directly to Claude Code when heavy=True."""
    o = _build_orch(
        settings,
        local_scripts=[_resp("should not run", "gpt-4o-mini")],
        claude_scripts=[_claude_result("heavy response")],
    )
    result = await o.handle("/ping", user_id="u1", heavy=True)
    assert result.handled_by == "claude-max"
    assert result.response == "heavy response"
    assert result.task.current_tier == "C2"
    assert result.task.route == "cloud"
    # Local surrogate was primed but never called — heavy skips the auto ladder.
    assert o._openai_surrogate_local.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_heavy_path_records_claude_tokens_in_ledger(settings: Settings, tmp_path):
    """Heavy path must still update the daily cloud-token ledger (so a user
    invoking !heavy in a loop doesn't bypass the safety budget)."""
    repo = Repository(tmp_path / "r.db")
    await repo.init()
    o = _build_orch(
        settings, repo=repo,
        claude_scripts=[_claude_result("heavy reply")],
    )
    await o.handle("go deep", user_id="u1", heavy=True)
    # ClaudeCodeResult helper returns input=10 output=20 → 30 total
    assert await repo.used_tokens_today("u1") == 30


@pytest.mark.asyncio
async def test_c1_planning_is_direct_openai_not_hermes(settings: Settings):
    """After the OpenRouter-skip decision: C1 always goes direct to OpenAI.
    Hermes CLI must NOT be invoked for requires_planning=True at C1.
    (Hermes is only used at C2 for Claude — tested separately.)"""
    msg = "이 URL을 분석하고 보고서를 작성해: https://example.com"
    o = _build_orch(
        settings,
        main_scripts=[_resp("direct gpt reply", "gpt-4o")],
    )
    result = await o.handle(msg, user_id="u1")
    assert result.handled_by == "cloud-gpt"
    assert result.response == "direct gpt reply"
    assert o.hermes.calls == []  # type: ignore[attr-defined]
    assert result.task.requires_planning is True  # router did flag it
    assert result.task.current_tier == "C1"


@pytest.mark.asyncio
async def test_llm_timeout_triggers_tier_up(settings: Settings):
    """Timeout at local → tier_up (validator maps timeout → escalate)."""
    o = _build_orch(
        settings,
        local_scripts=[LLMTimeoutError("deadline")],
        worker_scripts=[_resp("recovered on L3", "gpt-4o")],
    )
    result = await o.handle("hi", user_id="u1")
    assert result.response == "recovered on L3"
    assert result.task.current_tier == "L3"


@pytest.mark.asyncio
async def test_per_user_lock_serializes_same_user(settings: Settings):
    """R13: two concurrent requests from the same user must not run in parallel
    (per-user in-flight = 1)."""
    import asyncio

    started: list[int] = []
    finished: list[int] = []

    class _SlowLLM(_FakeLLM):
        async def generate(self, messages, *, max_tokens=2048, temperature=0.2):
            started.append(len(started))
            await asyncio.sleep(0.05)
            finished.append(len(finished))
            return _resp("ok", "gpt-4o-mini")

    o = Orchestrator(settings)
    o._openai_surrogate_local = _SlowLLM("gpt-4o-mini", [])  # type: ignore[assignment]

    async def _one() -> None:
        await o.handle("hi", user_id="same-user")

    await asyncio.gather(_one(), _one())
    # Each task's start must be followed by that same task's finish before
    # the next starts — i.e. never started[1] before finished[0].
    assert len(started) == 2 and len(finished) == 2


@pytest.mark.asyncio
async def test_retry_replay_creates_new_task(settings: Settings, tmp_path):
    repo = Repository(tmp_path / "r.db")
    await repo.init()
    o = _build_orch(
        settings, repo=repo,
        local_scripts=[_resp("first", "gpt-4o-mini"), _resp("second", "gpt-4o-mini")],
    )
    first = await o.handle("hi", user_id="u1")
    assert first.response == "first"

    replayed = await o.replay(first.task.task_id)
    assert replayed is not None
    assert replayed.task.task_id != first.task.task_id
    assert replayed.task.user_id == "u1"
    assert replayed.response == "second"


# ---- Path A: C1 via Claude CLI (Haiku) -------------------------------------


def _c1_claude_settings(settings: Settings) -> Settings:
    """Clone settings with c1_backend flipped to claude_cli."""
    return settings.model_copy(update={"c1_backend": "claude_cli"})


@pytest.mark.asyncio
async def test_c1_routes_through_claude_cli_when_backend_is_claude(settings: Settings):
    """Path A: with c1_backend=claude_cli, C1 traffic hits claude_code_c1
    (Haiku) instead of the OpenAI gpt-4o client. Phase 2 Hermes flag is off
    so the claude-cli branch wins over the legacy openai branch."""
    s = _c1_claude_settings(settings)
    # Force escalation to C1: local + worker return empty, then C1 answers.
    o = _build_orch(
        s,
        local_scripts=[_resp("", "gpt-4o-mini")],
        worker_scripts=[_resp("", "gpt-4o")],
        main_scripts=[_resp("SHOULD NOT BE CALLED", "gpt-4o")],
        claude_c1_scripts=[_claude_result("haiku reply", model="claude-haiku-4-5")],
    )
    result = await o.handle("hello", user_id="u1")
    assert result.handled_by == "cloud-claude-cli"
    assert result.response == "haiku reply"
    assert result.task.current_tier == "C1"
    # Legacy OpenAI gpt-4o client must NOT have been touched.
    assert o._openai_main.calls == []  # type: ignore[attr-defined]
    # Haiku adapter called exactly once, with the configured light model.
    assert len(o.claude_code_c1.calls) == 1  # type: ignore[attr-defined]
    call = o.claude_code_c1.calls[0]  # type: ignore[attr-defined]
    assert call["model"] == s.c1_claude_code_model
    assert call["persist_session"] is False  # C1 is stateless
    assert call["resume_session_id"] is None
    # Heavy-path Claude adapter stays untouched.
    assert o.claude_code.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_c1_claude_cli_auth_error_is_non_retryable(settings: Settings):
    """ClaudeCodeAuthError on C1 lane must degrade immediately — NOT burn
    retries attempting to hit the Max OAuth token that already rejected us."""
    from src.claude_adapter.adapter import ClaudeCodeAuthError

    s = _c1_claude_settings(settings)
    o = _build_orch(
        s,
        local_scripts=[_resp("", "gpt-4o-mini")],
        worker_scripts=[_resp("", "gpt-4o")],
        claude_c1_scripts=[ClaudeCodeAuthError("Max quota exhausted")],
    )
    result = await o.handle("hello", user_id="u1")
    assert result.handled_by == "claude-auth"
    assert result.task.degraded is True
    # Only one attempt on the Haiku adapter — no retry storm.
    assert len(o.claude_code_c1.calls) == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_c1_backend_openai_is_default_legacy_path(settings: Settings):
    """Default (c1_backend='openai'): C1 still uses gpt-4o — Path A must
    stay off unless explicitly opted in."""
    o = _build_orch(
        settings,
        local_scripts=[_resp("", "gpt-4o-mini")],
        worker_scripts=[_resp("", "gpt-4o")],
        main_scripts=[_resp("gpt-4o reply", "gpt-4o")],
        claude_c1_scripts=[_claude_result("SHOULD NOT BE CALLED")],
    )
    result = await o.handle("hello", user_id="u1")
    assert result.handled_by == "cloud-gpt"
    assert result.response == "gpt-4o reply"
    # Haiku adapter must NOT have been called in the default config.
    assert o.claude_code_c1.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_hermes_c1_flag_overrides_claude_cli_backend(settings: Settings):
    """Precedence: use_hermes_for_c1 beats c1_backend=claude_cli.
    The Phase 2 Hermes C1 path is the most explicit knob and wins."""
    s = settings.model_copy(update={
        "c1_backend": "claude_cli",
        "use_hermes_for_c1": True,
    })
    o = _build_orch(
        s,
        local_scripts=[_resp("", "gpt-4o-mini")],
        worker_scripts=[_resp("", "gpt-4o")],
        hermes_scripts=[_hermes_result("hermes reply", "gpt-4o", tier="C1")],
        claude_c1_scripts=[_claude_result("SHOULD NOT BE CALLED")],
    )
    result = await o.handle("hello", user_id="u1")
    assert result.handled_by == "cloud-gpt-hermes"
    assert o.claude_code_c1.calls == []  # type: ignore[attr-defined]
    assert len(o.hermes.calls) == 1  # type: ignore[attr-defined]
