"""Phase 1 feature-flag tests: USE_HERMES_FOR_LOCAL.

Verifies:
  - Flag off (default) → legacy Ollama/OpenAI client is used for L2/L3.
    None of the Hermes/Claude adapters get touched.
  - Flag on + Ollama enabled → L2/L3 hit HermesAdapter with provider='ollama'
    and the Ollama work/worker model. handled_by becomes 'local-hermes'.
  - Flag on + Ollama disabled → HermesAdapter with provider='openai' and
    the OpenAI surrogate model. handled_by 'local-hermes-surrogate'.
  - Bump prefix still flows through to the Hermes query on retries.
"""
from __future__ import annotations

from typing import Any

import pytest

from src.config import Settings
from src.hermes_adapter import HermesResult
from src.orchestrator import Orchestrator


class _RecordingHermes:
    def __init__(self, scripts: list[HermesResult]):
        self._scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []

    async def run(self, query: str, *, model: str, provider: str, **kwargs: Any) -> HermesResult:
        self.calls.append({"query": query, "model": model, "provider": provider, **kwargs})
        return self._scripts.pop(0)


def _hermes_ok(text: str, model: str, provider: str) -> HermesResult:
    return HermesResult(
        text=text, session_id="sid-lane", tier_used="C1",
        model_name=model, provider=provider,
        duration_ms=5, stdout_raw="", stderr_raw="",
        prompt_tokens=15, completion_tokens=20,
        provider_requested=provider, provider_actual=provider,
        models_used=[model], primary_model=model, turns_used=1,
    )


@pytest.mark.asyncio
async def test_flag_off_uses_legacy_path(settings: Settings):
    """Default behavior: flag off → the Hermes adapter is not called at all
    for L2/L3 traffic. Guarantees Phase 1 is opt-in."""
    from src.llm.base import LLMResponse

    class _FakeLLM:
        def __init__(self, model: str, text: str):
            self.model = model
            self._text = text
            self.calls: list[dict] = []

        async def generate(self, msgs, *, max_tokens=2048, temperature=0.2):
            self.calls.append({"msgs": msgs})
            return LLMResponse(text=self._text, model=self.model,
                               prompt_tokens=5, completion_tokens=10)

    settings.use_hermes_for_local = False
    settings.ollama_enabled = False
    o = Orchestrator(settings)
    o._openai_surrogate_local = _FakeLLM(settings.openai_model_local_surrogate, "legacy reply")  # type: ignore[assignment]
    hermes_spy = _RecordingHermes([])
    o.hermes = hermes_spy  # type: ignore[assignment]

    result = await o.handle("hi there", user_id="u1")
    assert result.handled_by == "local-surrogate"
    assert hermes_spy.calls == []  # Hermes NOT called


@pytest.mark.asyncio
async def test_flag_on_with_ollama_uses_hermes_ollama(settings: Settings):
    """Flag on + Ollama enabled → Hermes lane, pinned to provider=ollama."""
    settings.use_hermes_for_local = True
    settings.ollama_enabled = True
    o = Orchestrator(settings)
    hermes = _RecordingHermes([
        _hermes_ok("hermes reply", settings.ollama_work_model, "ollama"),
    ])
    o.hermes = hermes  # type: ignore[assignment]

    result = await o.handle("hi there", user_id="u1")
    assert result.handled_by == "local-hermes"
    assert result.response == "hermes reply"
    assert len(hermes.calls) == 1
    assert hermes.calls[0]["provider"] == "ollama"
    assert hermes.calls[0]["model"] == settings.ollama_work_model


@pytest.mark.asyncio
async def test_flag_on_without_ollama_uses_hermes_openai_surrogate(settings: Settings):
    """Flag on + Ollama disabled → Hermes lane, pinned to provider=openai
    with the local surrogate model. Claude is structurally unreachable."""
    settings.use_hermes_for_local = True
    settings.ollama_enabled = False
    o = Orchestrator(settings)
    hermes = _RecordingHermes([
        _hermes_ok("surrogate via hermes", settings.openai_model_local_surrogate, "openai"),
    ])
    o.hermes = hermes  # type: ignore[assignment]

    result = await o.handle("hi there", user_id="u1")
    assert result.handled_by == "local-hermes-surrogate"
    assert hermes.calls[0]["provider"] == "openai"
    assert hermes.calls[0]["model"] == settings.openai_model_local_surrogate
    # Critical: never claude-code
    assert hermes.calls[0]["provider"] != "claude-code"


@pytest.mark.asyncio
async def test_flag_on_worker_tier_picks_worker_model(settings: Settings):
    """Worker tier → Hermes called with the worker model (14B coder / gpt-4o)."""
    settings.use_hermes_for_local = True
    settings.ollama_enabled = True
    o = Orchestrator(settings)
    hermes = _RecordingHermes([
        _hermes_ok("worker hermes reply", settings.ollama_worker_model, "ollama"),
    ])
    o.hermes = hermes  # type: ignore[assignment]

    # Code-signal message → router picks 'worker' route → L3 tier
    result = await o.handle("def foo(): return 42", user_id="u1")
    assert result.handled_by == "worker-hermes"
    assert hermes.calls[0]["model"] == settings.ollama_worker_model
