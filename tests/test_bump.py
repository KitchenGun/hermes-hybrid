"""FIX#2: bump compression tests.

Key invariants to lock in:
  - First attempt (no prior model output) → pass-through, empty summary.
  - Subsequent attempts → compressed summary ≤ bounded length, referencing
    *only* the last attempt (non-cumulative).
  - Preview is hard-capped at 200 chars regardless of previous reply size.
  - Length of the bumped prompt stays within the same bound across N
    retries — repeated calls don't stack summaries.
"""
from __future__ import annotations

from src.orchestrator.bump import BumpPayload, compress_for_bump
from src.state import TaskState
from src.validator import ValidationResult


def _mk_task(msg: str = "Summarize the report in 3 bullets.") -> TaskState:
    return TaskState(
        session_id="s1",
        user_id="u1",
        user_message=msg,
        current_tier="L2",
    )


def _mk_verdict(reason: str = "low quality output") -> ValidationResult:
    return ValidationResult(decision="retry_same_tier", reason=reason)


def test_first_attempt_pass_through():
    """No prior model output → payload is the original prompt unchanged."""
    task = _mk_task()
    payload = compress_for_bump(task, _mk_verdict())
    assert isinstance(payload, BumpPayload)
    assert payload.had_previous is False
    assert payload.summary_line == ""
    assert payload.prompt == task.user_message
    assert payload.preview_len == 0


def test_second_attempt_injects_bounded_summary():
    """After one failed attempt, the summary cites the prev tier/model/reason
    and embeds a preview of the previous reply."""
    task = _mk_task()
    task.record_model_output(
        tier="L2",
        text="I don't know, sorry.",
        model_name="qwen2.5:7b-instruct",
    )
    payload = compress_for_bump(task, _mk_verdict("refusal at L2 is low_quality"))
    assert payload.had_previous is True
    assert payload.summary_line  # non-empty
    assert "L2:qwen2.5:7b-instruct" in payload.summary_line
    assert "refusal" in payload.summary_line
    assert "I don't know" in payload.summary_line
    # Bumped prompt = summary + \n\n + original
    assert payload.prompt.endswith(task.user_message)
    assert payload.summary_line in payload.prompt


def test_preview_is_capped_at_200_chars():
    """A 5000-char previous reply must not blow up the bump prompt."""
    task = _mk_task()
    huge = "A" * 5000
    task.record_model_output(tier="L3", text=huge, model_name="qwen2.5:14b-instruct")
    payload = compress_for_bump(task, _mk_verdict())
    assert payload.had_previous is True
    assert payload.preview_len <= 203  # 200 + "..." (3 chars)
    # Summary size is bounded — no matter how big the reply was:
    assert len(payload.summary_line) < 600


def test_non_cumulative_across_retries():
    """Critical invariant: 5 rounds of failure must not stack 5 summaries.

    Each call to ``compress_for_bump`` looks at only the *last* model output,
    so repeated calls produce summaries of roughly the same size. This is
    what makes the bump strategy safe for long retry chains.
    """
    task = _mk_task()
    sizes: list[int] = []
    for i in range(5):
        task.record_model_output(
            tier="L2",
            text=f"attempt {i} reply — some text that could grow",
            model_name="qwen2.5:7b-instruct",
        )
        payload = compress_for_bump(task, _mk_verdict(f"try {i}"))
        sizes.append(len(payload.prompt))

    # All bumped prompts fall in a tight band: not monotonically growing.
    # Allow a tiny drift (index int → str length) but not cumulative growth.
    assert max(sizes) - min(sizes) < 50, f"prompt lengths drifted: {sizes}"


def test_newlines_are_flattened_in_preview():
    """Multi-line previous replies get flattened so the preview stays single-line
    (keeps the bump breadcrumb compact and easy to inline)."""
    task = _mk_task()
    task.record_model_output(
        tier="L2", text="line1\nline2\nline3", model_name="qwen2.5:7b-instruct",
    )
    payload = compress_for_bump(task, _mk_verdict())
    # The summary quotes the preview via repr(), so literal "\n" escape is fine
    # but raw newline characters must not survive in the preview chunk.
    # Check the summary itself only contains one logical line (the injected
    # breadcrumb), not the original newlines.
    assert "line1 line2 line3" in payload.summary_line


def test_missing_reason_does_not_crash():
    """Validator could hand us an empty/None-ish reason; compressor must cope."""
    task = _mk_task()
    task.record_model_output(tier="L2", text="reply", model_name="qwen2.5:7b-instruct")
    payload = compress_for_bump(task, ValidationResult(decision="retry_same_tier", reason=""))
    assert payload.had_previous is True
    assert payload.summary_line  # still produced
