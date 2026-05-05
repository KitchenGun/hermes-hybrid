"""Critic — Validator wrap that stamps a numeric self_score on the task.

The Validator decides retry / tier-up policy (R10). The Critic adds two
diagnostic signals on top, *without* changing what the Validator returns:

  1. ``task.internal_confidence`` ← float in [0, 1], a soft quality score
     derived from the validator decision and the output shape.
  2. one entry in ``task.reflection_notes`` with the verdict + score, so
     Reflection / Curator jobs can join score and rationale without doing
     their own validator replay.

Why a wrapper instead of editing Validator directly: keeping retry policy
separate from quality scoring means a future score model (LLM judge,
embedding similarity, etc.) can replace ``compute_self_score`` without
risking a regression in escalation behavior.

The score is intentionally a small lookup table — fancy heuristics drift,
numbers in a table can be reasoned about and tuned from real data.
"""
from __future__ import annotations

from src.state import TaskState
from src.validator import Validator, ValidationResult


# Base score per Validator decision. The validator's authoritative
# decisions map to a 0-1 confidence prior; output-shape factors then
# scale this base down. Numbers are intentionally round so an operator
# can tune them from real reflection data without re-deriving math.
_DECISION_BASE: dict[str, float] = {
    "pass": 0.90,
    "retry_same_tier": 0.40,
    "tier_up": 0.30,
    "escalate_cloud": 0.25,
    "escalate_claude": 0.20,
    "final_failure": 0.05,
}

# Hard caps when something went structurally wrong — a polite-looking
# response after a tool error or timeout shouldn't earn high confidence
# even if the validator decided to "pass" the text.
_TIMEOUT_CAP = 0.10
_TOOL_ERROR_CAP = 0.15


def _length_factor(text: str) -> float:
    """Soft penalty for ultra-short outputs.

    Empty / one-word responses to a non-trivial prompt are usually a
    failure even when the validator passes them, so they should not
    inherit the full base score. Calibrated from the Validator's own
    ``_MIN_USEFUL_LEN = 4`` plus typical Korean reply lengths.
    """
    n = len(text or "")
    if n == 0:
        return 0.0
    if n < 20:
        return 0.55
    if n < 100:
        return 0.85
    return 1.0


def compute_self_score(
    verdict: ValidationResult,
    *,
    output_text: str,
    timed_out: bool,
    tool_error: bool,
) -> float:
    """Project a verdict + output shape into a 0-1 confidence score.

    The score is diagnostic only — never feeds back into retry policy.
    """
    base = _DECISION_BASE.get(verdict.decision, 0.30)
    score = base * _length_factor(output_text)
    if timed_out:
        score = min(score, _TIMEOUT_CAP)
    if tool_error:
        score = min(score, _TOOL_ERROR_CAP)
    # Round to 3 decimals — JSONL stays readable, no false precision.
    return round(score, 3)


class Critic:
    """Validator wrapper that also records a self_score on the task.

    Drop-in replacement for direct Validator usage in the orchestrator:
    same return type, same arguments, plus a stamp on the task.
    """

    def __init__(self, validator: Validator):
        self.validator = validator

    def evaluate(
        self,
        task: TaskState,
        *,
        output_text: str,
        expected_schema: str | None = None,
        timed_out: bool = False,
        tool_error: bool = False,
        self_score_override: float | None = None,
        hermes_turns_used: int = 0,
    ) -> ValidationResult:
        verdict = self.validator.validate(
            task,
            output_text=output_text,
            expected_schema=expected_schema,
            timed_out=timed_out,
            tool_error=tool_error,
            hermes_turns_used=hermes_turns_used,
        )
        score = (
            self_score_override
            if self_score_override is not None
            else compute_self_score(
                verdict,
                output_text=output_text,
                timed_out=timed_out,
                tool_error=tool_error,
            )
        )
        task.internal_confidence = score
        task.reflection_notes.append(
            f"[critic] {verdict.decision} ({score:.2f}): {verdict.reason}"
        )
        return verdict
