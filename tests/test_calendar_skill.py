"""CalendarSkill tests.

Covers:
  - Intent match: Korean (일정/캘린더/미팅/...) and English (calendar/meeting/...)
    keywords + natural-language queries like "오늘 일정 뭐 있어".
  - Non-match: unrelated chat ("hello there"), avoid false positives on
    words that merely contain calendar substrings ("escalate" has no
    "calendar" token — but we test this anyway).
  - Flag gating: default_registry() omits CalendarSkill unless
    settings.calendar_skill_enabled is True.
  - Invocation wiring: skill calls orchestrator.hermes.run with the
    configured profile + preload_skills + provider + model.
  - Error rendering: a hermes.run failure surfaces as a user-readable
    warning with the underlying exception type, not a traceback.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from src.config import Settings
from src.hermes_adapter.adapter import (
    HermesAdapterError,
    HermesAuthError,
    HermesResult,
    HermesTimeout,
)
from src.orchestrator import Orchestrator
from src.skills import CalendarSkill, SkillRegistry, default_registry
from src.skills.base import SkillContext


# ---- match / non-match -----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "이번주 일정 알려줘",
        "오늘 캘린더 뭐 있어?",
        "내일 미팅 있나?",
        "다음주 약속 확인해줘",
        "what's on my calendar today?",
        "show me this week's schedule",
        "any meetings tomorrow?",
        "list my agenda for next week",
        "이번 주 스케줄 정리해줘",
    ],
)
def test_calendar_skill_matches_calendar_intent(text: str):
    s = CalendarSkill()
    match = s.match(text)
    assert match is not None, f"should match: {text!r}"
    assert match.args["query"] == text


@pytest.mark.parametrize(
    "text",
    [
        "hello there",
        "파이썬 코드 짜줘",
        "weather tomorrow",           # weather, not schedule
        "escalate this to the team",  # no calendar token despite substring overlap
        "",
        "   ",
    ],
)
def test_calendar_skill_does_not_match_unrelated(text: str):
    assert CalendarSkill().match(text) is None


# ---- registry gating -------------------------------------------------------


def test_default_registry_omits_calendar_when_flag_off(settings: Settings):
    """Default flag value (False) → CalendarSkill is NOT registered, so the
    registry contents match the pre-skill baseline (test_skills.py line 75).
    """
    assert settings.calendar_skill_enabled is False
    reg = default_registry(settings)
    assert "calendar" not in reg.names()
    assert reg.names() == ["hybrid-status", "hybrid-budget", "hybrid-memo", "kanban"]


def test_default_registry_includes_calendar_when_flag_on(settings: Settings):
    settings.calendar_skill_enabled = True
    reg = default_registry(settings)
    # CalendarSkill is first so it wins over any future keyword-based skill.
    assert reg.names()[0] == "calendar"


def test_default_registry_without_settings_omits_calendar():
    """Callers that don't pass settings (existing tests, legacy code) still
    get the pre-CalendarSkill registry shape."""
    reg = default_registry()
    assert "calendar" not in reg.names()


# ---- invocation wiring -----------------------------------------------------


class _FakeHermes:
    def __init__(self, result: HermesResult | Exception):
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def run(self, query: str, **kwargs: Any) -> HermesResult:
        self.calls.append({"query": query, **kwargs})
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _hermes_ok(text: str = "오늘 일정 3건") -> HermesResult:
    return HermesResult(
        text=text,
        session_id="sid-1",
        tier_used="C1",  # type: ignore[arg-type]
        model_name="qwen2.5-coder:32b-instruct",
        provider="ollama-local",
        duration_ms=1,
        stdout_raw="",
        stderr_raw="",
    )


@pytest.mark.asyncio
async def test_calendar_skill_invokes_hermes_with_profile_and_skill(
    settings: Settings,
):
    """Happy path: match → hermes.run called with the profile name, the
    google-workspace skill preloaded, and model/provider passed through
    from settings."""
    settings.calendar_skill_enabled = True
    # Pin model/provider explicitly so we can assert pass-through. With
    # the defaults (empty strings), both would be None — that's covered
    # by ``test_calendar_skill_empty_model_provider_passes_none``.
    settings.calendar_skill_model = "qwen2.5-coder:32b-instruct"
    settings.calendar_skill_provider = "auto"
    settings.calendar_skill_preload = "productivity/google-workspace"
    o = Orchestrator(settings)
    fake = _FakeHermes(_hermes_ok("이번 주 일정: 월 10시 회의, 수 14시 리뷰"))
    o.hermes = fake  # type: ignore[assignment]

    r = await o.handle("이번주 일정 알려줘", user_id="u1")

    assert r.handled_by == "skill:calendar"
    assert "회의" in r.response

    # Verify the hermes.run invocation shape.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["query"].startswith("[현재 날짜: ")
    assert call["query"].endswith("\n\n이번주 일정 알려줘")
    assert call["profile"] == settings.calendar_skill_profile  # "calendar_ops"
    assert call["preload_skills"] == [settings.calendar_skill_preload]
    assert call["provider"] == "auto"
    assert call["model"] == "qwen2.5-coder:32b-instruct"
    assert call["max_turns"] == settings.calendar_skill_read_max_turns
    assert call["timeout_ms"] == settings.calendar_skill_timeout_ms


@pytest.mark.asyncio
async def test_calendar_skill_empty_model_provider_passes_none(settings: Settings):
    """Default (empty-string) model/provider → None to the adapter → the
    ``-m``/``--provider`` flags are omitted and the profile's own
    ``config.yaml`` drives selection. This is the supported path for
    custom providers like ``ollama-local`` that aren't valid CLI
    ``--provider`` argparse choices."""
    settings.calendar_skill_enabled = True
    assert settings.calendar_skill_model == ""
    assert settings.calendar_skill_provider == ""
    o = Orchestrator(settings)
    fake = _FakeHermes(_hermes_ok())
    o.hermes = fake  # type: ignore[assignment]

    await o.handle("오늘 일정 뭐 있어?", user_id="u1")

    call = fake.calls[0]
    assert call["model"] is None
    assert call["provider"] is None
    # Profile is still passed; preload is omitted by default.
    assert call["profile"] == "calendar_ops"
    assert call["preload_skills"] == []


@pytest.mark.asyncio
async def test_calendar_skill_short_circuits_master_dispatch(settings: Settings):
    """A calendar match must NOT touch the master LLM. We rely on
    master_enabled=False (fixture default) so the only way the skill
    reaches the slash-skill handler is via the IntentRouter short-circuit
    — exactly the contract under test."""
    settings.calendar_skill_enabled = True
    o = Orchestrator(settings)
    o.hermes = _FakeHermes(_hermes_ok())  # type: ignore[assignment]

    r = await o.handle("what's on my calendar today?", user_id="u1")
    assert r.handled_by == "skill:calendar"
    assert "master:disabled" not in r.response


@pytest.mark.asyncio
async def test_calendar_skill_renders_hermes_error_as_warning(settings: Settings):
    """If hermes.run raises a generic ``HermesAdapterError`` (NOT a known-cause
    subclass like Timeout/Auth), the skill renders a friendly warning with
    the exception type — not a traceback, not the raw stderr.

    2026-05-04: only the bare ``HermesAdapterError`` takes this in-skill path;
    ``HermesTimeout`` / ``HermesAuthError`` trigger the claude_cli fallback
    instead (covered in the tests below).
    """
    settings.calendar_skill_enabled = True
    o = Orchestrator(settings)
    o.hermes = _FakeHermes(  # type: ignore[assignment]
        HermesAdapterError("profile calendar_ops is not configured")
    )

    r = await o.handle("오늘 일정 알려줘", user_id="u1")
    assert r.handled_by == "skill:calendar"
    # Skill returned a string rather than raising — task succeeded from the
    # orchestrator's perspective (degraded=False), and the response carries
    # the warning payload.
    assert r.task.status == "succeeded"
    assert "Calendar lookup failed" in r.response
    assert "HermesAdapterError" in r.response
    # Hint points at manual hermes invocation and the OAuth check.
    assert "hermes -p calendar_ops chat" in r.response
    assert "Google OAuth" in r.response


# 2026-05-06: legacy test_orchestrator-based fakes removed. The
# Hermes/Claude CLI fallback paths and the "skill disabled →
# legacy pipeline" check no longer have a meaningful counterpart
# under the all-via-master architecture (master handles forced/heavy/
# free text uniformly). If a calendar-specific master regression
# surfaces it should land in tests/test_hermes_master.py instead.
