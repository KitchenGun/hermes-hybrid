"""Tests for IntentRouter (src/integration/intent_router.py).

The router is a deterministic short-circuit layer in front of the
master LLM. We lock down:
  * RuleLayer match → handled_by=rule, response populated, master skipped
  * Slash skill match → handled_by=skill:<name>, slash_skill stamped
  * (Phase 11) heavy 분기 폐기 — master = single lane
  * fallthrough → trigger_type=discord_message, profile_id=None

Phase 8 (2026-05-06) 후 forced_profile 분기는 폐기 — 시그니처는 호환을
위해 남기지만 항상 무시.
"""
from __future__ import annotations

import pytest

from src.config import Settings
from src.integration import IntentRouter


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


@pytest.mark.asyncio
async def test_rule_match_short_circuits():
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="/ping",
        user_id="42",
        session_id="s1",
    )
    assert result.short_circuited
    assert result.handled_by == "rule"
    assert isinstance(result.response, str)
    assert result.rule_match is not None


@pytest.mark.asyncio
async def test_slash_skill_match_short_circuits_with_skill_match():
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="/memo list",
        user_id="42",
        session_id="s1",
    )
    assert result.short_circuited
    assert result.handled_by == "skill:hybrid-memo"
    assert result.slash_skill == "hybrid-memo"
    assert result.job_id == "hybrid-memo"
    assert result.job_category == "chat"
    assert result.skill_match is not None


@pytest.mark.asyncio
async def test_forced_profile_arg_is_ignored_phase8():
    """Phase 8 후 forced_profile 인자는 무시되고 일반 fallthrough 와 동일."""
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="오늘 운동 30분 했어",
        user_id="42",
        session_id="s1",
        forced_profile="journal_ops",
    )
    assert not result.short_circuited
    assert result.trigger_type == "discord_message"
    assert result.trigger_source == "user:42"
    assert result.profile_id is None
    assert result.forced_profile is None


# Phase 11 (2026-05-06): heavy 분기 폐기. test_heavy_flag_marks_trigger_source 제거.


@pytest.mark.asyncio
async def test_fallthrough_no_short_circuit():
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="안녕 오늘 뭐할까",
        user_id="42",
        session_id="s1",
    )
    assert not result.short_circuited
    assert result.handled_by is None
    assert result.trigger_type == "discord_message"
    assert result.trigger_source == "user:42"
    assert result.profile_id is None


@pytest.mark.asyncio
async def test_rule_takes_precedence_over_slash_skill():
    """If both match, RuleLayer wins (instant deterministic reply)."""
    router = IntentRouter(_settings())
    # /ping is a RuleLayer match. Make sure no slash skill steals it.
    result = await router.route(
        user_message="/ping",
        user_id="42",
        session_id="s1",
    )
    assert result.handled_by == "rule"
    assert result.slash_skill is None


# ---- Phase 9: @handle mention parsing --------------------------------


@pytest.mark.asyncio
async def test_known_agent_mention_is_extracted():
    """`@coder write fizzbuzz` → agent_handles=['@coder']."""
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="@coder write fizzbuzz",
        user_id="42",
        session_id="s1",
    )
    assert result.agent_handles == ["@coder"]
    assert not result.short_circuited
    assert result.trigger_type == "discord_message"


@pytest.mark.asyncio
async def test_multiple_distinct_handles_preserved_in_order():
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="@coder 짜고 @reviewer 가 검토해줘",
        user_id="42",
        session_id="s1",
    )
    assert result.agent_handles == ["@coder", "@reviewer"]


@pytest.mark.asyncio
async def test_duplicate_handles_deduped():
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="@coder 짜고 다시 @coder 가 마무리",
        user_id="42",
        session_id="s1",
    )
    assert result.agent_handles == ["@coder"]


@pytest.mark.asyncio
async def test_unknown_handle_filtered():
    """`@nobody` is not in AgentRegistry → silently dropped."""
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="@nobody hello",
        user_id="42",
        session_id="s1",
    )
    assert result.agent_handles == []


@pytest.mark.asyncio
async def test_email_like_string_does_not_match():
    """`user@example.com` should NOT trigger a mention — preceded by \\w."""
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="kang@coder.dev 보내줘",   # `@coder` 앞에 . — 차단됨
        user_id="42",
        session_id="s1",
    )
    assert result.agent_handles == []


@pytest.mark.asyncio
async def test_mention_handle_is_case_insensitive():
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="@CODER 알았지?",
        user_id="42",
        session_id="s1",
    )
    # canonical form ('@coder') 로 정규화됨
    assert result.agent_handles == ["@coder"]


@pytest.mark.asyncio
async def test_mention_stamped_even_on_slash_skill_match():
    """슬래시 skill 단락이라도 mention 은 stamp — 하류에서 무시할 수 있도록."""
    router = IntentRouter(_settings())
    # `/memo save @coder ...` — /memo 는 slash skill 매치, @coder 는 valid handle
    result = await router.route(
        user_message="/memo save @coder hello",
        user_id="42",
        session_id="s1",
    )
    assert result.handled_by == "skill:hybrid-memo"
    # slash skill 자체가 처리하므로 master 는 안 부르지만 stamp 는 보존
    assert result.agent_handles == ["@coder"]


@pytest.mark.asyncio
async def test_no_message_yields_empty_handles():
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="",
        user_id="42",
        session_id="s1",
    )
    assert result.agent_handles == []
