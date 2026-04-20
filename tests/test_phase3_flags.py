"""Phase 3: master flag + trust-hermes-reflection behavior.

Covers:
  - ``USE_HERMES_EVERYWHERE=true`` implies all three per-phase flags
    via the ``effective_*`` properties (L2/L3 + C1 + heavy all route
    through Hermes).
  - Per-phase flags still work as overrides when the master is off.
  - ``trust_hermes_reflection`` short-circuits the validator on multi-turn
    Hermes outputs — but only when turns_used >= 2 AND no timeout /
    tool_error / empty / malformed-JSON.
"""
from __future__ import annotations

from typing import Any

import pytest

from src.config import Settings
from src.hermes_adapter import HermesResult
from src.orchestrator import Orchestrator
from src.state.task_state import TaskState
from src.validator import Validator


# ---- effective_* properties ------------------------------------------------


def test_everywhere_implies_all_per_phase_flags(settings: Settings):
    settings.use_hermes_everywhere = True
    # All per-phase flags stay off explicitly — effective_* must still return True
    settings.use_hermes_for_local = False
    settings.use_hermes_for_c1 = False
    settings.use_hermes_for_heavy = False
    assert settings.effective_use_hermes_for_local is True
    assert settings.effective_use_hermes_for_c1 is True
    assert settings.effective_use_hermes_for_heavy is True


def test_per_phase_flags_still_win_when_master_off(settings: Settings):
    settings.use_hermes_everywhere = False
    settings.use_hermes_for_local = True
    settings.use_hermes_for_c1 = False
    settings.use_hermes_for_heavy = True
    assert settings.effective_use_hermes_for_local is True
    assert settings.effective_use_hermes_for_c1 is False
    assert settings.effective_use_hermes_for_heavy is True


# ---- orchestrator honors effective flags -----------------------------------


class _RecordingHermes:
    def __init__(self, result: HermesResult):
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def run(self, query: str, *, model: str, provider: str, **kwargs: Any) -> HermesResult:
        self.calls.append({"query": query, "model": model, "provider": provider, **kwargs})
        return self.result


def _hermes_ok(model: str, provider: str, turns: int = 1) -> HermesResult:
    return HermesResult(
        text="hermes reply", session_id="sid-3", tier_used="C1",
        model_name=model, provider=provider,
        duration_ms=5, stdout_raw="", stderr_raw="",
        prompt_tokens=10, completion_tokens=20,
        provider_requested=provider, provider_actual=provider,
        models_used=[model], primary_model=model, turns_used=turns,
    )


@pytest.mark.asyncio
async def test_master_flag_routes_local_through_hermes(settings: Settings):
    """With the master on and all per-phase flags off, L2 still goes to Hermes."""
    settings.use_hermes_everywhere = True
    settings.use_hermes_for_local = False
    settings.ollama_enabled = True

    o = Orchestrator(settings)
    hermes = _RecordingHermes(_hermes_ok(settings.ollama_work_model, "ollama"))
    o.hermes = hermes  # type: ignore[assignment]

    result = await o.handle("hi", user_id="u1")
    assert result.handled_by == "local-hermes"
    assert hermes.calls[0]["provider"] == "ollama"


# ---- Validator: trust_hermes_reflection ------------------------------------


def _make_task(user_msg: str = "test") -> TaskState:
    return TaskState(
        session_id="sid", user_id="u1", user_message=user_msg,
    )


def test_reflection_trusted_when_flag_on_and_turns_ge_2(settings: Settings):
    settings.trust_hermes_reflection = True
    v = Validator(settings)
    task = _make_task("write me a fibonacci function")

    # Suspiciously short reply that would normally be flagged low_quality
    verdict = v.validate(
        task,
        output_text="ok",
        timed_out=False,
        tool_error=False,
        hermes_turns_used=3,
    )
    assert verdict.decision == "pass"
    assert "hermes reflection trusted" in verdict.reason


def test_reflection_not_trusted_with_single_turn(settings: Settings):
    """turns_used < 2 → fall back to normal validator path."""
    settings.trust_hermes_reflection = True
    v = Validator(settings)
    task = _make_task("write me a fibonacci function")
    verdict = v.validate(
        task,
        output_text="",  # empty → low_quality
        hermes_turns_used=1,
    )
    assert verdict.decision != "pass"


def test_reflection_not_trusted_when_flag_off(settings: Settings):
    settings.trust_hermes_reflection = False
    v = Validator(settings)
    task = _make_task("write me a fibonacci function")
    verdict = v.validate(
        task,
        output_text="",
        hermes_turns_used=5,
    )
    # Flag off → empty text still fails validator
    assert verdict.decision != "pass"


def test_reflection_does_not_override_timeout(settings: Settings):
    """Timeouts always fail — reflection doesn't excuse them."""
    settings.trust_hermes_reflection = True
    v = Validator(settings)
    task = _make_task()
    verdict = v.validate(
        task,
        output_text="maybe something",
        timed_out=True,
        hermes_turns_used=5,
    )
    assert verdict.decision != "pass"


def test_reflection_does_not_override_tool_error(settings: Settings):
    settings.trust_hermes_reflection = True
    v = Validator(settings)
    task = _make_task()
    verdict = v.validate(
        task,
        output_text="maybe",
        tool_error=True,
        hermes_turns_used=5,
    )
    assert verdict.decision != "pass"


def test_reflection_does_not_override_empty_output(settings: Settings):
    settings.trust_hermes_reflection = True
    v = Validator(settings)
    task = _make_task()
    verdict = v.validate(
        task,
        output_text="   ",  # whitespace only
        hermes_turns_used=5,
    )
    assert verdict.decision != "pass"


def test_reflection_respects_json_schema(settings: Settings):
    """When the caller asked for JSON, reflection must not override a
    schema violation — Hermes' reflection doesn't fix malformed JSON."""
    settings.trust_hermes_reflection = True
    v = Validator(settings)
    task = _make_task()
    verdict = v.validate(
        task,
        output_text="this is not json",
        expected_schema="json",
        hermes_turns_used=5,
    )
    assert verdict.decision != "pass"


def test_reflection_passes_valid_json_with_high_turns(settings: Settings):
    settings.trust_hermes_reflection = True
    v = Validator(settings)
    task = _make_task()
    verdict = v.validate(
        task,
        output_text='{"ok": true}',
        expected_schema="json",
        hermes_turns_used=3,
    )
    assert verdict.decision == "pass"
