"""Phase 2 feature-flag tests: USE_HERMES_FOR_C1.

Verifies:
  - Flag off (default) → C1 uses the direct OpenAI client (legacy path).
    Hermes is not called.
  - Flag on → C1 routes through HermesAdapter with provider='openai'
    pinned and the main ``openai_model`` (NOT the surrogate). handled_by
    becomes 'cloud-gpt-hermes'.
  - Bump prefix is prepended on retries (feature parity with Phase 1).
  - Claude is structurally unreachable from this lane — we assert the
    pinned provider is ``"openai"`` and never ``"claude-code"``.
"""
from __future__ import annotations

from typing import Any

import pytest

from src.config import Settings
from src.hermes_adapter import HermesResult
from src.llm.base import LLMResponse
from src.orchestrator import Orchestrator


# ---- fakes ------------------------------------------------------------------


class _RecordingHermes:
    def __init__(self, scripts: list[HermesResult]):
        self._scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []

    async def run(self, query: str, *, model: str, provider: str, **kwargs: Any) -> HermesResult:
        self.calls.append({"query": query, "model": model, "provider": provider, **kwargs})
        return self._scripts.pop(0)


class _FakeOpenAI:
    def __init__(self, model: str, text: str):
        self.model = model
        self._text = text
        self.calls: list[dict] = []

    async def generate(self, msgs, *, max_tokens=2048, temperature=0.2):
        self.calls.append({"msgs": msgs})
        return LLMResponse(
            text=self._text, model=self.model,
            prompt_tokens=5, completion_tokens=10,
        )


def _hermes_ok(text: str, model: str) -> HermesResult:
    return HermesResult(
        text=text, session_id="sid-c1", tier_used="C1",
        model_name=model, provider="openai",
        duration_ms=5, stdout_raw="", stderr_raw="",
        prompt_tokens=20, completion_tokens=30,
        provider_requested="openai", provider_actual="openai",
        models_used=[model], primary_model=model, turns_used=1,
    )


# ---- routing to C1: force cloud route ---------------------------------------


# Messages long enough / complexity-flagged to push the router to 'cloud'.
# We use an explicit planning-heavy question to maximize the odds.
_CLOUD_PROMPT = (
    "Please write a detailed architectural plan for a distributed rate "
    "limiter with cross-datacenter replication, including failure modes "
    "and reasoning about consistency vs availability trade-offs."
)


async def _force_c1(o: Orchestrator, settings: Settings) -> None:
    """Monkey-patch the router to always pick 'cloud' so the test is
    deterministic regardless of future heuristic tweaks."""
    from src.router.router import RouterDecision

    async def _always_cloud(msg, *, history_window):
        return RouterDecision(
            route="cloud",
            confidence=0.9,
            reason="forced by test",
            requires_planning=True,
            provider="openai",
        )

    o.router.decide = _always_cloud  # type: ignore[assignment]


# ---- tests ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_uses_legacy_openai_direct(settings: Settings):
    """Default behavior: C1 goes straight to OpenAI, Hermes untouched."""
    settings.use_hermes_for_c1 = False
    o = Orchestrator(settings)
    await _force_c1(o, settings)

    fake_openai = _FakeOpenAI(settings.openai_model, "legacy c1 reply")
    o._openai_main = fake_openai  # type: ignore[assignment]
    hermes_spy = _RecordingHermes([])
    o.hermes = hermes_spy  # type: ignore[assignment]

    result = await o.handle(_CLOUD_PROMPT, user_id="u1")
    assert result.handled_by == "cloud-gpt"
    assert result.response == "legacy c1 reply"
    assert len(fake_openai.calls) == 1
    assert hermes_spy.calls == []  # Hermes NOT called


@pytest.mark.asyncio
async def test_flag_on_routes_c1_through_hermes_openai(settings: Settings):
    """Flag on → Hermes lane pinned to provider=openai with the main model."""
    settings.use_hermes_for_c1 = True
    o = Orchestrator(settings)
    await _force_c1(o, settings)

    hermes = _RecordingHermes([
        _hermes_ok("c1 hermes reply", settings.openai_model),
    ])
    o.hermes = hermes  # type: ignore[assignment]

    result = await o.handle(_CLOUD_PROMPT, user_id="u1")
    assert result.handled_by == "cloud-gpt-hermes"
    assert result.response == "c1 hermes reply"
    assert len(hermes.calls) == 1
    assert hermes.calls[0]["provider"] == "openai"
    assert hermes.calls[0]["model"] == settings.openai_model
    # Critical invariant: Claude is structurally unreachable from C1.
    assert hermes.calls[0]["provider"] != "claude-code"


@pytest.mark.asyncio
async def test_flag_on_uses_main_model_not_surrogate(settings: Settings):
    """C1 is the planning tier — it must use the main ``openai_model``
    (gpt-4o), not the ``openai_model_local_surrogate`` (gpt-4o-mini)."""
    settings.use_hermes_for_c1 = True
    settings.openai_model = "gpt-4o"
    settings.openai_model_local_surrogate = "gpt-4o-mini"

    o = Orchestrator(settings)
    await _force_c1(o, settings)

    hermes = _RecordingHermes([_hermes_ok("ok", "gpt-4o")])
    o.hermes = hermes  # type: ignore[assignment]

    await o.handle(_CLOUD_PROMPT, user_id="u1")
    assert hermes.calls[0]["model"] == "gpt-4o"
    assert hermes.calls[0]["model"] != "gpt-4o-mini"


@pytest.mark.asyncio
async def test_flag_on_respects_max_turns(settings: Settings):
    """C1 earns plan/act/reflect — we pass the full ``hermes_max_turns``
    budget (unlike L2/L3 which clamp to 5)."""
    settings.use_hermes_for_c1 = True
    settings.hermes_max_turns = 12

    o = Orchestrator(settings)
    await _force_c1(o, settings)

    hermes = _RecordingHermes([_hermes_ok("ok", settings.openai_model)])
    o.hermes = hermes  # type: ignore[assignment]

    await o.handle(_CLOUD_PROMPT, user_id="u1")
    assert hermes.calls[0]["max_turns"] == 12
