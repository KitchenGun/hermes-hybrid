"""TaskState invariants: cloud budget, retry gates, tier switching (R8)."""
from __future__ import annotations

from src.state import TaskState


def test_record_cloud_usage_decrements_budget():
    s = TaskState(session_id="s", user_id="u", user_message="hi", token_budget_remaining=1000)
    s.record_model_output(
        tier="C1", text="hello", model_name="gpt-4o",
        prompt_tokens=100, completion_tokens=50,
    )
    assert s.cloud_call_count == 1
    assert s.token_budget_remaining == 850
    assert "gpt-4o" in s.cloud_model_used


def test_local_tier_does_not_count_cloud():
    s = TaskState(session_id="s", user_id="u", user_message="hi")
    s.record_model_output(tier="L2", text="x", model_name="qwen", prompt_tokens=10, completion_tokens=5)
    assert s.cloud_call_count == 0
    assert s.cloud_model_used == []


def test_retry_gate_helpers():
    s = TaskState(session_id="s", user_id="u", user_message="hi", retry_budget=3)
    assert s.can_retry_same_tier(2)
    s.same_tier_retries = 2
    assert not s.can_retry_same_tier(2)


def test_switch_tier_resets_same_tier_counter():
    """R8: moving to a new tier must reset same_tier_retries, otherwise the
    new tier inherits an already-exhausted counter and cannot retry in place."""
    s = TaskState(session_id="s", user_id="u", user_message="hi", current_tier="L2")
    s.same_tier_retries = 2
    s.switch_tier("L3")
    assert s.current_tier == "L3"
    assert s.same_tier_retries == 0


def test_switch_tier_same_tier_is_noop():
    s = TaskState(session_id="s", user_id="u", user_message="hi", current_tier="L2")
    s.same_tier_retries = 2
    s.switch_tier("L2")
    assert s.same_tier_retries == 2  # unchanged


def test_claude_model_recorded_once_even_on_repeat():
    s = TaskState(session_id="s", user_id="u", user_message="hi", token_budget_remaining=1000)
    s.record_model_output(tier="C2", text="a", model_name="claude-opus-4-7",
                          prompt_tokens=10, completion_tokens=10)
    s.record_model_output(tier="C2", text="b", model_name="claude-opus-4-7",
                          prompt_tokens=10, completion_tokens=10)
    assert s.cloud_call_count == 2
    # dedupe in cloud_model_used means budget-check by `in` works correctly
    assert s.cloud_model_used.count("claude-opus-4-7") == 1
