"""Tests for PolicyGate (src/integration/policy_gate.py).

PolicyGate is the orchestrator-facing single contract for safety /
budget / tier policy. Phase 8 (2026-05-06) 후 책임 축소:
  * allow when nothing's wrong
  * deny_allowlist when require_allowlist=True and user not in list
  * deny_budget when repo says daily token usage >= cap
  * post_validate delegates to Validator (decision is unchanged)

폐기:
  * needs_confirmation (profile yaml 의존)
  * profile_loader 인자 (ProfileLoader 자체 폐기)
"""
from __future__ import annotations

import pytest

from src.config import Settings
from src.integration import PolicyGate
from src.state import TaskState


def _settings(**overrides) -> Settings:
    base = {
        "_env_file": None,
        "discord_bot_token": "",
        "discord_allowed_user_ids": "",
        "require_allowlist": False,
        "ollama_enabled": False,
        "experience_log_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _task(*, user_id: str = "100") -> TaskState:
    return TaskState(
        session_id="s1", user_id=user_id, user_message="m"
    )


class _StubRepo:
    def __init__(self, used: int = 0):
        self._used = used

    async def used_tokens_today(self, user_id: str) -> int:
        return self._used


@pytest.mark.asyncio
async def test_allow_when_nothing_blocks():
    gate = PolicyGate(_settings(), repo=_StubRepo(used=0))
    decision = await gate.pre_dispatch(_task())
    assert decision.action == "allow"


@pytest.mark.asyncio
async def test_deny_allowlist_when_user_not_in_list():
    gate = PolicyGate(
        _settings(
            require_allowlist=True,
            discord_allowed_user_ids="999",
        ),
        repo=_StubRepo(),
    )
    decision = await gate.pre_dispatch(_task(user_id="100"))
    assert decision.action == "deny_allowlist"


@pytest.mark.asyncio
async def test_allowlist_admits_listed_user():
    gate = PolicyGate(
        _settings(
            require_allowlist=True,
            discord_allowed_user_ids="100,200",
        ),
        repo=_StubRepo(),
    )
    decision = await gate.pre_dispatch(_task(user_id="100"))
    assert decision.action == "allow"


@pytest.mark.asyncio
async def test_deny_budget_when_daily_cap_reached():
    gate = PolicyGate(
        _settings(cloud_token_budget_daily=1000),
        repo=_StubRepo(used=1500),
    )
    decision = await gate.pre_dispatch(_task())
    assert decision.action == "deny_budget"
    assert "1500" in decision.reason


@pytest.mark.asyncio
async def test_no_repo_skips_budget_check():
    gate = PolicyGate(_settings(), repo=None)
    decision = await gate.pre_dispatch(_task())
    assert decision.action == "allow"


def test_post_validate_delegates_to_validator():
    """Post-validate must return the same ValidationResult Validator
    would return — PolicyGate is a thin wrapper here."""
    from src.validator import Validator

    settings = _settings()
    direct = Validator(settings).validate(
        _task(), output_text="안녕"
    )
    via_gate = PolicyGate(settings, repo=None).post_validate(
        _task(), output_text="안녕"
    )
    assert via_gate.decision == direct.decision
    assert via_gate.next_tier == direct.next_tier
