"""Validator tests — retry budget, tier-up, error classification (R10)."""
from __future__ import annotations

from src.config import Settings
from src.state import TaskState
from src.validator import Validator


def _state(tier="L2", retry_budget=4, user_message="hi") -> TaskState:
    return TaskState(
        session_id="s1",
        user_id="u1",
        user_message=user_message,
        current_tier=tier,
        retry_budget=retry_budget,
    )


def test_pass_on_good_output(settings: Settings):
    v = Validator(settings)
    s = _state()
    assert v.validate(s, output_text="hello world").decision == "pass"


def test_malformed_same_tier_then_tier_up(settings: Settings):
    v = Validator(settings)
    s = _state()
    # first malformed → retry same tier
    r1 = v.validate(s, output_text="not json", expected_schema="json")
    assert r1.decision == "retry_same_tier"
    s.same_tier_retries = 2  # budget exhausted
    r2 = v.validate(s, output_text="still not json", expected_schema="json")
    assert r2.decision in ("tier_up", "escalate_cloud")


def test_low_quality_tier_ups(settings: Settings):
    v = Validator(settings)
    s = _state(tier="L2")
    r = v.validate(s, output_text="   ")
    assert r.decision in ("tier_up", "escalate_cloud")


def test_c1_timeout_is_final_failure_not_claude_escalation(settings: Settings):
    """Post heavy-path refactor: C1 is the auto-escalation ceiling.
    A C1 timeout becomes final_failure instead of burning a Max session.
    Claude is reachable only via the explicit `!heavy` prefix."""
    v = Validator(settings)
    s = _state(tier="C1")
    r = v.validate(s, output_text="", timed_out=True)
    assert r.decision == "final_failure"
    assert r.decision != "escalate_claude"


def test_final_failure_when_budget_exhausted(settings: Settings):
    v = Validator(settings)
    s = _state(retry_budget=0)
    r = v.validate(s, output_text="")
    assert r.decision == "final_failure"


def test_refusal_at_l2_is_low_quality(settings: Settings):
    """R10: bottom-tier refusals should escalate, not stall."""
    v = Validator(settings)
    s = _state(tier="L2")
    r = v.validate(s, output_text="죄송합니다, 저는 답할 수 없습니다.")
    assert r.decision in ("tier_up", "escalate_cloud")


def test_refusal_at_c1_is_not_auto_lowquality(settings: Settings):
    """Higher tiers may refuse legitimately (policy) — don't flag as low_quality."""
    v = Validator(settings)
    s = _state(tier="C1")
    r = v.validate(s, output_text="I cannot help with that request.")
    # Refusal regex only applies at L2; at C1 this is just a short reply.
    # s.user_message = "hi" (len < 30), so the ultra-short rule also doesn't fire.
    assert r.decision == "pass"


def test_ultrashort_to_long_prompt_is_low_quality(settings: Settings):
    v = Validator(settings)
    s = _state(user_message="a" * 80)
    r = v.validate(s, output_text="ok")
    assert r.decision in ("tier_up", "escalate_cloud")


def test_c1_failure_is_final_regardless_of_claude_budget(settings: Settings):
    """Post heavy-path refactor: the validator doesn't even consider Claude
    escalation from C1 — the claude_call_budget is enforced in the heavy
    path, not here. C1 failure is always final_failure for the auto ladder."""
    v = Validator(settings)
    s = _state(tier="C1")
    # Even with no prior Claude call, C1 still returns final_failure (not
    # escalate_claude) — the old budget-gate path is gone.
    r = v.validate(s, output_text="", timed_out=True)
    assert r.decision == "final_failure"
    assert r.decision != "escalate_claude"


def test_tier_up_to_c1_blocked_by_zero_token_budget(settings: Settings):
    v = Validator(settings)
    s = _state(tier="L3")
    s.token_budget_remaining = 0
    r = v.validate(s, output_text="", timed_out=True)
    assert r.decision == "final_failure"
    assert "token" in r.reason.lower()
