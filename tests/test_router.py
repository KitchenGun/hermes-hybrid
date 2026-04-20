"""Router heuristic + 7B refinement tests (R11).

FIX#1 tests live at the bottom: Router must *never* emit ``claude-code``
as a provider — it's excluded at the type level and further checked at
runtime so a future code change can't regress Max protection.
"""
from __future__ import annotations

import typing

import pytest

from src.config import Settings
from src.llm.base import LLMResponse, LLMTimeoutError
from src.router.router import Provider, Router, _parse_refined, _route_to_provider


@pytest.mark.asyncio
async def test_short_conversational(settings: Settings):
    r = Router(settings)
    d = await r.decide("hi there")
    assert d.route == "local"
    assert d.requires_planning is False


@pytest.mark.asyncio
async def test_code_signal(settings: Settings):
    r = Router(settings)
    d = await r.decide("def foo(): return 42")
    assert d.route == "worker"


@pytest.mark.asyncio
async def test_multistep_cloud(settings: Settings):
    r = Router(settings)
    msg = "이 URL을 분석하고 보고서를 작성해: https://example.com"
    d = await r.decide(msg)
    assert d.route == "cloud"
    assert d.requires_planning is True


@pytest.mark.asyncio
async def test_structured_output_request(settings: Settings):
    r = Router(settings)
    d = await r.decide("Return the result as JSON with a 'name' field and an array of items.")
    assert d.route in ("worker", "cloud")


@pytest.mark.asyncio
async def test_file_ref_triggers_planning(settings: Settings):
    r = Router(settings)
    d = await r.decide("Read config.yaml and summarize.")
    assert d.requires_planning is True


# ---- 7B refinement parser -----------------------------------------------


def test_parse_refined_strict_json():
    d = _parse_refined('{"route":"cloud","confidence":0.9,"requires_planning":true,"reason":"multi-step"}')
    assert d is not None
    assert d["route"] == "cloud"
    assert d["confidence"] == 0.9
    assert d["requires_planning"] is True


def test_parse_refined_json_with_prose_prefix():
    raw = 'Sure, here is the JSON:\n{"route":"local","confidence":0.8,"requires_planning":false,"reason":"chat"}'
    d = _parse_refined(raw)
    assert d is not None
    assert d["route"] == "local"


def test_parse_refined_unparseable():
    assert _parse_refined("not json at all") is None
    assert _parse_refined("") is None


# ---- 7B refinement integration with mocked Ollama ------------------------


class _FakeOllama:
    def __init__(self, model: str, text: str | Exception):
        self.model = model
        self.name = "fake"
        self._text = text
        self.calls: list[dict] = []

    async def generate(self, messages, *, max_tokens=2048, temperature=0.2):
        self.calls.append({"messages": messages, "max_tokens": max_tokens,
                           "temperature": temperature})
        if isinstance(self._text, Exception):
            raise self._text
        return LLMResponse(text=self._text, model=self.model,
                           prompt_tokens=10, completion_tokens=20)


@pytest.mark.asyncio
async def test_refine_overrides_heuristic_when_high_conf(settings: Settings):
    """7B says cloud with high confidence → override local heuristic."""
    settings.ollama_enabled = True
    r = Router(settings)
    # Message is short → heuristic says local. 7B disagrees at conf=0.90.
    r._ollama_router = _FakeOllama(  # type: ignore[assignment]
        settings.ollama_router_model,
        '{"route":"cloud","confidence":0.9,"requires_planning":true,"reason":"tricky plan"}',
    )
    d = await r.decide("schedule follow-up")
    assert d.route == "cloud"
    assert d.requires_planning is True
    assert d.reason.startswith("7b:")


@pytest.mark.asyncio
async def test_refine_ignored_when_low_conf(settings: Settings):
    """7B verdict at conf < router_conf_accept → keep heuristic."""
    settings.ollama_enabled = True
    r = Router(settings)
    r._ollama_router = _FakeOllama(  # type: ignore[assignment]
        settings.ollama_router_model,
        '{"route":"cloud","confidence":0.5,"requires_planning":true,"reason":"?"}',
    )
    d = await r.decide("hi there")
    # Low-conf 7B ignored → heuristic's "local, 0.80" survives → stays local.
    assert d.route == "local"


@pytest.mark.asyncio
async def test_refine_garbage_response_falls_back(settings: Settings):
    """7B emits non-JSON → fall back to heuristic, never crash."""
    settings.ollama_enabled = True
    r = Router(settings)
    r._ollama_router = _FakeOllama(  # type: ignore[assignment]
        settings.ollama_router_model, "I think you should use cloud",
    )
    d = await r.decide("hi there")
    assert d.route == "local"  # heuristic


@pytest.mark.asyncio
async def test_refine_timeout_falls_back(settings: Settings):
    """7B unreachable → heuristic still works (bot doesn't freeze)."""
    settings.ollama_enabled = True
    r = Router(settings)
    r._ollama_router = _FakeOllama(  # type: ignore[assignment]
        settings.ollama_router_model, LLMTimeoutError("deadline"),
    )
    d = await r.decide("hi there")
    assert d.route == "local"


@pytest.mark.asyncio
async def test_refine_rejects_bad_route_value(settings: Settings):
    settings.ollama_enabled = True
    r = Router(settings)
    r._ollama_router = _FakeOllama(  # type: ignore[assignment]
        settings.ollama_router_model,
        '{"route":"nuclear","confidence":0.95,"requires_planning":false,"reason":"lol"}',
    )
    d = await r.decide("hi there")
    assert d.route == "local"  # invalid route field → reject, keep heuristic


# ======================================================================
# FIX#1 — Router can never emit claude-code
# ======================================================================


def test_provider_literal_excludes_claude_code():
    """The ``Provider`` Literal alias must be exactly {ollama, openai}.

    If this ever admits ``claude-code`` or any other member we've just
    opened a structural bypass of Max protection. Changing this set is
    a deliberate architectural decision, not a casual refactor.
    """
    args = typing.get_args(Provider)
    assert set(args) == {"ollama", "openai"}
    assert "claude-code" not in args
    assert "anthropic" not in args


def test_route_to_provider_never_returns_claude_code():
    """Exhaustive mapping check: every (route, ollama_enabled) combination
    resolves to ollama or openai — never claude-code."""
    for route in ("local", "worker", "cloud"):
        for ollama in (True, False):
            p = _route_to_provider(route, ollama_enabled=ollama)  # type: ignore[arg-type]
            assert p in ("ollama", "openai"), f"{route}/{ollama} → {p}"


@pytest.mark.asyncio
async def test_decide_stamps_ollama_when_enabled(settings: Settings):
    """With Ollama enabled, local/worker routes carry provider=ollama."""
    settings.ollama_enabled = True
    r = Router(settings)
    d = await r.decide("hi there")  # → local route
    assert d.route == "local"
    assert d.provider == "ollama"


@pytest.mark.asyncio
async def test_decide_stamps_openai_when_ollama_disabled(settings: Settings):
    """Ollama off → local/worker routes fall back to the openai surrogate,
    not claude. This is the R3 surrogate path made explicit."""
    settings.ollama_enabled = False
    r = Router(settings)
    d = await r.decide("hi there")
    assert d.route == "local"
    assert d.provider == "openai"


@pytest.mark.asyncio
async def test_decide_cloud_route_always_openai(settings: Settings):
    """Cloud route maps to openai regardless of ollama_enabled. Claude is
    never reachable via the auto ladder."""
    for ollama in (True, False):
        settings.ollama_enabled = ollama
        r = Router(settings)
        d = await r.decide("이 URL을 분석하고 보고서를 작성해: https://example.com")
        assert d.route == "cloud"
        assert d.provider == "openai"
