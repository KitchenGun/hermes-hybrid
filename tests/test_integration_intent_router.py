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


# ---- Phase 24 (2026-05-08): context anchoring for follow-up messages -----


_LONG_INSTA_TURN = (
    "인스타 자동화 프로젝트 — 현재 의견 정리. 권장 MVP 순서는 "
    "(1) 계정 연결 OAuth, (2) 게시 스케줄러, (3) 분석 대시보드. "
    "각 단계마다 관찰 가능한 메트릭 정의가 필요해."
)


@pytest.mark.asyncio
async def test_followup_with_long_prior_user_turn_stamps_anchor():
    """짧은 follow-up + history 안의 장문 user turn → anchor_message stamp."""
    router = IntentRouter(_settings())
    history = [
        {"role": "user", "content": _LONG_INSTA_TURN},
        {"role": "assistant", "content": "확인했어, 진행 계획 정리해볼게."},
    ]
    result = await router.route(
        user_message="좋아 진행해보자. 골격 정하고 보고해라",
        user_id="42",
        session_id="s1",
        history=history,
    )
    assert result.is_followup is True
    assert result.anchor_message is not None
    assert "인스타 자동화" in result.anchor_message


@pytest.mark.asyncio
async def test_deictic_followup_resolves_through_assistant_turn():
    """직전 turn 이 assistant 라도 가장 최근 *장문 user* turn 을 anchor 로 잡는다."""
    router = IntentRouter(_settings())
    history = [
        {"role": "user", "content": _LONG_INSTA_TURN},
        {"role": "assistant", "content": "라우팅 디버깅 중입니다 — metadata 빈 row 발견."},
    ]
    result = await router.route(
        user_message="이 내용에 대한 골격을 정하고 보고해줘라",
        user_id="42",
        session_id="s1",
        history=history,
    )
    assert result.is_followup is True
    assert result.anchor_message is not None
    assert "인스타 자동화" in result.anchor_message


@pytest.mark.asyncio
async def test_long_message_is_not_treated_as_followup():
    """장문 user 입력은 우연히 follow-up 토큰을 품고 있어도 anchor 치환 X."""
    router = IntentRouter(_settings())
    long_msg = (
        "이 내용은 별개 주제야. " + "x" * 200
    )
    result = await router.route(
        user_message=long_msg,
        user_id="42",
        session_id="s1",
        history=[{"role": "user", "content": _LONG_INSTA_TURN}],
    )
    assert result.is_followup is False
    assert result.anchor_message is None


@pytest.mark.asyncio
async def test_followup_without_history_yields_no_anchor():
    """history 가 비었으면 anchor 는 None — false positive 방지."""
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="진행해",
        user_id="42",
        session_id="s1",
        history=[],
    )
    assert result.is_followup is True
    assert result.anchor_message is None


@pytest.mark.asyncio
async def test_followup_with_only_short_prior_user_turn_yields_no_anchor():
    """직전 user turn 이 짧으면 (인사 등) anchor 로 신뢰하지 않는다."""
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="진행해",
        user_id="42",
        session_id="s1",
        history=[
            {"role": "user", "content": "안녕"},
            {"role": "assistant", "content": "네 무엇을 도와드릴까요"},
        ],
    )
    assert result.is_followup is True
    assert result.anchor_message is None


@pytest.mark.asyncio
async def test_non_followup_short_message_does_not_anchor():
    """short message 라도 follow-up 토큰이 없으면 anchor 치환 X."""
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="안녕 오늘은?",
        user_id="42",
        session_id="s1",
        history=[{"role": "user", "content": _LONG_INSTA_TURN}],
    )
    assert result.is_followup is False
    assert result.anchor_message is None


@pytest.mark.xfail(
    reason=(
        "Phase 24 reviewer MEDIUM: _FOLLOWUP_RE 의 continuation 군 "
        "(보고/정리/진행 등) 은 *새* 짧은 자연어 메시지에서도 자연스럽게 "
        "등장한다. 현재 구현은 길이만 게이팅하고 lexical overlap / "
        "deictic vs continuation 분리 게이트가 없어 무관한 장문 prior "
        "turn 이 잘못된 anchor 로 stamp 된다. @coder hand-off 대상."
    ),
    strict=True,
)
@pytest.mark.asyncio
async def test_continuation_token_in_new_topic_does_not_falsely_anchor():
    """false-positive 회귀 잠금: continuation 토큰 ('보고', '정리') 만
    들어간 *새 주제* 짧은 메시지는 무관한 장문 prior turn 을 anchor 로
    stamp 하면 안 된다.

    reproduction: 직전 turn 은 인스타 자동화, 현재 turn 은 회의 보고서.
    deictic 토큰 ('이 내용', '그거' 등) 이 없으므로 anchor 는 None 이어야
    한다. 현재 구현은 단순 정규식 매치라 잘못된 anchor 를 stamp — 이 케이스를
    잠가 두면 토큰 분류 / lexical overlap 게이트 추가 시 자동으로 통과한다.
    """
    router = IntentRouter(_settings())
    result = await router.route(
        user_message="내일 회의 보고서 정리해줘",
        user_id="42",
        session_id="s1",
        history=[{"role": "user", "content": _LONG_INSTA_TURN}],
    )
    assert result.anchor_message is None
