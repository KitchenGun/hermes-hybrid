"""Phase 2b feature-flag tests: USE_HERMES_FOR_HEAVY.

Verifies:
  - Flag off (default) → heavy (!heavy) uses the legacy ClaudeCodeAdapter
    path. Hermes is not called.
  - Flag on → heavy routes through HermesAdapter with
    provider='claude-code' pinned and handled_by='claude-max-hermes'.
  - Session reuse (FIX#4) still works: prior session_id is passed to
    Hermes via resume_session; new session_id is recorded after success.
  - Resume failure fallback: if Hermes errors on a resume call, the
    registry is invalidated and a fresh Hermes call is retried once.
  - Hermes-side exceptions (auth/timeout/other) produce user-readable
    degraded responses with the right handled_by tags.
"""
from __future__ import annotations

from typing import Any

import pytest

from src.config import Settings
from src.hermes_adapter import (
    HermesAdapterError,
    HermesAuthError,
    HermesResult,
    HermesTimeout,
)
from src.orchestrator import Orchestrator


# ---- fakes ------------------------------------------------------------------


class _RecordingHermes:
    """Scripted Hermes stand-in that records each call and pops a scripted
    result (or raises a scripted exception) per call."""

    def __init__(self, scripts: list[HermesResult | Exception]):
        self._scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []

    async def run(self, query: str, *, model: str, provider: str, **kwargs: Any) -> HermesResult:
        self.calls.append({"query": query, "model": model, "provider": provider, **kwargs})
        if not self._scripts:
            raise AssertionError("Hermes called more times than scripted")
        r = self._scripts.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _hermes_heavy_ok(text: str, session_id: str = "sid-heavy") -> HermesResult:
    return HermesResult(
        text=text, session_id=session_id, tier_used="C2",
        model_name="sonnet", provider="claude-code",
        duration_ms=50, stdout_raw="", stderr_raw="",
        prompt_tokens=100, completion_tokens=200,
        provider_requested="claude-code", provider_actual="claude-code",
        models_used=["sonnet"], primary_model="sonnet", turns_used=2,
    )


class _FakeClaudeCode:
    """Legacy ClaudeCodeAdapter stand-in — used when the flag is off."""

    def __init__(self, text: str = "legacy heavy reply"):
        self._text = text
        self.calls: list[dict] = []

    async def run(self, *, prompt, history, resume_session_id=None, persist_session=False):
        from src.claude_adapter.adapter import ClaudeCodeResult
        self.calls.append({
            "prompt": prompt, "resume_session_id": resume_session_id,
            "persist_session": persist_session,
        })
        return ClaudeCodeResult(
            text=self._text, session_id="sid-legacy",
            model_name="claude-code-max",
            input_tokens=80, output_tokens=120,
            duration_ms=10,
        )


# ---- tests ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_uses_legacy_claude_code(settings: Settings):
    """Default behavior: heavy goes through ClaudeCodeAdapter; Hermes idle."""
    settings.use_hermes_for_heavy = False
    o = Orchestrator(settings)

    fake_cc = _FakeClaudeCode("legacy heavy reply")
    o.claude_code = fake_cc  # type: ignore[assignment]
    hermes_spy = _RecordingHermes([])
    o.hermes = hermes_spy  # type: ignore[assignment]

    result = await o.handle("plan the distributed system", user_id="u1", heavy=True)
    assert result.handled_by == "claude-max"
    assert result.response == "legacy heavy reply"
    assert len(fake_cc.calls) == 1
    assert hermes_spy.calls == []


@pytest.mark.asyncio
async def test_flag_on_routes_heavy_through_hermes(settings: Settings):
    """Flag on → HermesAdapter with provider='claude-code' pinned."""
    settings.use_hermes_for_heavy = True
    o = Orchestrator(settings)

    hermes = _RecordingHermes([_hermes_heavy_ok("hermes heavy reply")])
    o.hermes = hermes  # type: ignore[assignment]

    result = await o.handle("plan the thing", user_id="u1", heavy=True)
    assert result.handled_by == "claude-max-hermes"
    assert result.response == "hermes heavy reply"
    assert len(hermes.calls) == 1
    assert hermes.calls[0]["provider"] == "claude-code"
    assert hermes.calls[0]["model"] == settings.claude_code_model
    assert hermes.calls[0]["resume_session"] is None  # first turn → no prior sid


@pytest.mark.asyncio
async def test_flag_on_session_reuse(settings: Settings):
    """Second heavy turn → Hermes gets resume_session=<prior sid>."""
    settings.use_hermes_for_heavy = True
    o = Orchestrator(settings)

    hermes = _RecordingHermes([
        _hermes_heavy_ok("turn 1", session_id="sid-A"),
        _hermes_heavy_ok("turn 2", session_id="sid-A"),
    ])
    o.hermes = hermes  # type: ignore[assignment]

    await o.handle("first", user_id="u1", heavy=True)
    await o.handle("second", user_id="u1", heavy=True)

    assert hermes.calls[0]["resume_session"] is None
    assert hermes.calls[1]["resume_session"] == "sid-A"  # reused within TTL


@pytest.mark.asyncio
async def test_flag_on_resume_failure_fallback_to_fresh(settings: Settings):
    """If Hermes errors on a resume call, invalidate + retry fresh once.
    Mirrors the legacy ClaudeCodeResumeFailed handling."""
    settings.use_hermes_for_heavy = True
    o = Orchestrator(settings)

    # Seed the registry with a prior sid so the next call attempts resume.
    o.heavy_sessions.record("u1", "stale-sid")

    hermes = _RecordingHermes([
        HermesAdapterError("session not found"),          # resume attempt fails
        _hermes_heavy_ok("fresh reply", session_id="sid-new"),  # fresh retry OK
    ])
    o.hermes = hermes  # type: ignore[assignment]

    result = await o.handle("retry me", user_id="u1", heavy=True)
    assert result.handled_by == "claude-max-hermes"
    assert result.response == "fresh reply"
    assert len(hermes.calls) == 2
    assert hermes.calls[0]["resume_session"] == "stale-sid"
    assert hermes.calls[1]["resume_session"] is None
    # Registry should now hold the NEW session id (sid-new), not stale-sid.
    assert o.heavy_sessions.peek("u1") == "sid-new"


@pytest.mark.asyncio
async def test_flag_on_hermes_auth_error_degrades_gracefully(settings: Settings):
    """Hermes-side auth failure on heavy → user-readable degraded response
    with handled_by='hermes-auth'. No exception bubbles up."""
    settings.use_hermes_for_heavy = True
    o = Orchestrator(settings)
    hermes = _RecordingHermes([HermesAuthError("oauth refresh failed")])
    o.hermes = hermes  # type: ignore[assignment]

    result = await o.handle("do thing", user_id="u1", heavy=True)
    assert result.handled_by == "hermes-auth"
    assert result.task.degraded is True
    assert "Hermes" in result.response or "claude" in result.response.lower()


@pytest.mark.asyncio
async def test_flag_on_hermes_timeout_degrades_gracefully(settings: Settings):
    settings.use_hermes_for_heavy = True
    o = Orchestrator(settings)
    hermes = _RecordingHermes([HermesTimeout("timed out after 300000ms")])
    o.hermes = hermes  # type: ignore[assignment]

    result = await o.handle("long task", user_id="u1", heavy=True)
    assert result.handled_by == "claude-timeout"
    assert result.task.degraded is True
    assert "timed out" in result.response


@pytest.mark.asyncio
async def test_flag_on_fresh_call_error_propagates_to_degraded(settings: Settings):
    """A first-turn (no prior sid) Hermes error is NOT wrapped by the
    resume-fallback branch — it should bubble up to _handle_heavy and
    render a degraded 'hermes error on heavy' response."""
    settings.use_hermes_for_heavy = True
    o = Orchestrator(settings)
    hermes = _RecordingHermes([HermesAdapterError("some non-resume failure")])
    o.hermes = hermes  # type: ignore[assignment]

    result = await o.handle("first turn", user_id="u1", heavy=True)
    assert result.handled_by == "claude-error"
    assert result.task.degraded is True
    # Only one call — no fallback retry, since there was no prior sid.
    assert len(hermes.calls) == 1
