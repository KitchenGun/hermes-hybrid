"""Tests for Critic — Validator wrap with self_score stamping.

Contract:
  * Critic.evaluate returns the same ValidationResult that the wrapped
    Validator would return — retry/tier policy is unchanged.
  * task.internal_confidence is stamped with a 0-1 score derived from
    verdict + output shape.
  * task.reflection_notes gains one entry per evaluate() call.
  * Score caps apply: timed_out caps at 0.10, tool_error caps at 0.15
    even if the verdict says ``pass`` (we don't reward false-positive
    successes after a structural failure).
"""
from __future__ import annotations

from src.config import Settings
from src.core import Critic, compute_self_score
from src.state import TaskState
from src.validator import Validator
from src.validator.validator import ValidationResult


def _settings() -> Settings:
    # Use a constructed Settings (env file might be present at the repo
    # root, that's fine — these tests only read fields the validator
    # already touched in its own test suite).
    return Settings()


def _task(message: str = "테스트 메시지", tier: str = "L2") -> TaskState:
    return TaskState(
        session_id="s1",
        user_id="u1",
        user_message=message,
        current_tier=tier,  # type: ignore[arg-type]
    )


# ---- compute_self_score: pure function -------------------------------------


def test_score_pass_long_output_is_high():
    verdict = ValidationResult(decision="pass", reason="ok")
    score = compute_self_score(
        verdict, output_text="x" * 200, timed_out=False, tool_error=False
    )
    assert score == 0.9  # 0.90 base * 1.0 length factor


def test_score_pass_short_output_is_penalized():
    verdict = ValidationResult(decision="pass", reason="ok")
    score = compute_self_score(
        verdict, output_text="ok", timed_out=False, tool_error=False
    )
    # 0.90 * 0.55 (n<20) = 0.495
    assert score == 0.495


def test_score_pass_empty_output_is_zero():
    verdict = ValidationResult(decision="pass", reason="ok")
    score = compute_self_score(
        verdict, output_text="", timed_out=False, tool_error=False
    )
    assert score == 0.0


def test_score_retry_decisions_are_low():
    verdict = ValidationResult(decision="retry_same_tier", reason="malformed")
    # 30 chars — falls in the 20<=n<100 band
    score = compute_self_score(
        verdict, output_text="x" * 30, timed_out=False, tool_error=False
    )
    # 0.40 * 0.85 (20<=n<100) = 0.34
    assert score == 0.34


def test_score_final_failure_is_floor():
    verdict = ValidationResult(decision="final_failure", reason="budget")
    score = compute_self_score(
        verdict, output_text="x" * 200, timed_out=False, tool_error=False
    )
    assert score == 0.05  # base 0.05 * 1.0


def test_score_timeout_caps_even_on_pass():
    """A timeout that the validator decided to 'pass' should not earn
    high confidence — the structural failure still happened."""
    verdict = ValidationResult(decision="pass", reason="text trusted")
    score = compute_self_score(
        verdict, output_text="x" * 200, timed_out=True, tool_error=False
    )
    assert score == 0.10


def test_score_tool_error_caps_even_on_pass():
    verdict = ValidationResult(decision="pass", reason="text trusted")
    score = compute_self_score(
        verdict, output_text="x" * 200, timed_out=False, tool_error=True
    )
    assert score == 0.15


# ---- Critic.evaluate: integration with TaskState ---------------------------


def test_critic_returns_unchanged_validation_result():
    """Drop-in compat: the verdict the orchestrator obeys must be exactly
    what Validator alone would have returned."""
    settings = _settings()
    validator = Validator(settings)
    critic = Critic(validator)
    task = _task()

    # Use a prompt + output combination that we know triggers a clear
    # validator decision: empty output → low_quality / retry_same_tier
    # (path depends on validator config, but the 'verdict equality' check
    # below is what we actually care about).
    direct = validator.validate(_task(), output_text="", timed_out=False, tool_error=False)
    via_critic = critic.evaluate(task, output_text="", timed_out=False, tool_error=False)
    assert via_critic.decision == direct.decision
    assert via_critic.next_tier == direct.next_tier


def test_critic_stamps_internal_confidence_on_task():
    settings = _settings()
    critic = Critic(Validator(settings))
    task = _task()
    assert task.internal_confidence == 0.0
    critic.evaluate(task, output_text="x" * 100, timed_out=False, tool_error=False)
    assert task.internal_confidence > 0.0


def test_critic_appends_one_reflection_note_per_call():
    settings = _settings()
    critic = Critic(Validator(settings))
    task = _task()
    critic.evaluate(task, output_text="x" * 100)
    critic.evaluate(task, output_text="x" * 50)
    assert len(task.reflection_notes) == 2
    assert all(n.startswith("[critic]") for n in task.reflection_notes)


def test_critic_self_score_override_bypasses_compute():
    """Operator-injected score (e.g., LLM judge result) wins over the
    heuristic. Useful for staging a richer score model later without
    touching Critic.evaluate's call sites."""
    settings = _settings()
    critic = Critic(Validator(settings))
    task = _task()
    critic.evaluate(
        task,
        output_text="x" * 200,
        self_score_override=0.42,
    )
    assert task.internal_confidence == 0.42
