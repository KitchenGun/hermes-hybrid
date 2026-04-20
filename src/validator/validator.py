"""Validator: classify failure, manage retry budget, decide escalation (R10).

Per design doc §7. Validator does NOT re-execute anything; it only emits a
Decision that the Orchestrator obeys.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from src.config import Settings
from src.state import TaskState, Tier

Decision = Literal[
    "pass",
    "retry_same_tier",
    "tier_up",
    "escalate_cloud",
    "escalate_claude",
    "final_failure",
]


# --- low-quality patterns (R10) ----------------------------------------------
_EMPTY_RE = re.compile(r"^\s*$")
# repeat: same non-trivial chunk ≥ 3 times (common stuck-loop symptom)
_REPEAT_RE = re.compile(r"(.{8,})\1{2,}")
# generic refusal / hallucination red flags
_REFUSAL_RE = re.compile(
    r"^\s*(죄송합니다|잘\s?모르겠|알\s?수\s?없습니다|I\s+(cannot|can't|don't know)|"
    r"I'm\s+sorry,?\s+I\s+can'?t)",
    re.IGNORECASE,
)
_MIN_USEFUL_LEN = 4  # chars; ultra-short replies to non-trivial prompts are suspect


@dataclass
class ValidationResult:
    decision: Decision
    reason: str
    next_tier: Tier | None = None


# Claude (C2) is NEVER reached via automatic escalation — it is a user-opt-in
# heavy path only. C1 is the ceiling for validator-driven tier-ups; a failure
# at C1 becomes final_failure (degraded) instead of burning a Max session.
_TIER_UP_MAP: dict[Tier, Tier] = {
    "L2": "L3",
    "L3": "C1",
    "C1": "C1",  # no auto-escalation to Claude (heavy path only)
    "C2": "C2",  # reached only via heavy path; no further tier-up
}


class Validator:
    def __init__(self, settings: Settings):
        self.settings = settings

    def validate(
        self,
        state: TaskState,
        *,
        output_text: str,
        expected_schema: str | None = None,
        timed_out: bool = False,
        tool_error: bool = False,
        self_score: float | None = None,
        hermes_turns_used: int = 0,
    ) -> ValidationResult:
        # Phase 3: if Hermes ran its own plan/act/reflect loop for ≥ 2 turns
        # and the operator flipped `trust_hermes_reflection`, skip the local
        # low-quality pattern checks and trust Hermes' reflection. Timeouts,
        # tool errors, and empty/malformed outputs still fail the validator —
        # this is a "stop second-guessing on subjective quality" knob, not
        # a blanket pass.
        if (
            getattr(self.settings, "trust_hermes_reflection", False)
            and hermes_turns_used >= 2
            and not timed_out
            and not tool_error
            and bool((output_text or "").strip())
        ):
            if expected_schema == "json":
                import json as _json
                try:
                    _json.loads(output_text)
                except (ValueError, _json.JSONDecodeError):
                    # Schema violations still fail — reflection doesn't fix JSON.
                    pass
                else:
                    return ValidationResult(
                        decision="pass",
                        reason=f"hermes reflection trusted (turns={hermes_turns_used})",
                    )
            else:
                return ValidationResult(
                    decision="pass",
                    reason=f"hermes reflection trusted (turns={hermes_turns_used})",
                )

        err = self._classify(
            state=state,
            output_text=output_text,
            expected_schema=expected_schema,
            timed_out=timed_out,
            tool_error=tool_error,
            self_score=self_score,
        )
        if err is None:
            return ValidationResult(decision="pass", reason="output ok")

        state.record_error(err, f"validator classified: {err}", tier=state.current_tier)

        if state.retry_count >= state.retry_budget:
            return ValidationResult(decision="final_failure", reason="retry budget exhausted")

        if err == "malformed_output":
            if state.can_retry_same_tier(self.settings.same_tier_retry_max):
                return ValidationResult(
                    decision="retry_same_tier",
                    reason="malformed output, try same tier with repair prompt",
                )
            return self._tier_up(state, "malformed, same-tier exhausted")

        if err == "low_quality":
            return self._tier_up(state, "low quality output")

        if err == "timeout":
            return self._tier_up(state, "timeout, escalate")

        if err == "tool_error":
            if state.can_retry_same_tier(self.settings.same_tier_retry_max):
                return ValidationResult(
                    decision="retry_same_tier",
                    reason="tool error, retry with tool substitution",
                )
            return self._tier_up(state, "tool error, same-tier exhausted")

        return ValidationResult(decision="final_failure", reason="unknown error class")

    # ---- private ----

    def _classify(
        self,
        *,
        state: TaskState,
        output_text: str,
        expected_schema: str | None,
        timed_out: bool,
        tool_error: bool,
        self_score: float | None,
    ):
        if timed_out:
            return "timeout"
        if tool_error:
            return "tool_error"

        text = output_text or ""

        if expected_schema == "json":
            try:
                json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return "malformed_output"

        if _EMPTY_RE.match(text) or _REPEAT_RE.search(text):
            return "low_quality"

        # Ultra-short response to a non-trivial prompt → low quality
        if len(text.strip()) < _MIN_USEFUL_LEN and len(state.user_message) > 30:
            return "low_quality"

        # Explicit refusal/hallucination flag (bottom tier only — higher tiers
        # sometimes refuse legitimately, e.g. policy-violating prompts)
        if state.current_tier == "L2" and _REFUSAL_RE.search(text):
            return "low_quality"

        if self_score is not None and self_score < 0.6:
            return "low_quality"

        return None

    def _tier_up(self, state: TaskState, reason: str) -> ValidationResult:
        if not state.can_tier_up(self.settings.tier_up_retry_max):
            return ValidationResult(decision="final_failure", reason=f"{reason}; tier-up exhausted")

        next_tier = _TIER_UP_MAP[state.current_tier]

        # Top of the auto-escalation ladder: further tier-up would be a no-op
        # (we removed C1→C2 auto-escalation; Claude is heavy-path only).
        if next_tier == state.current_tier:
            return ValidationResult(
                decision="final_failure",
                reason=f"{reason}; top auto-tier reached ({next_tier})",
            )

        if next_tier == "C1":
            if state.token_budget_remaining <= 0:
                return ValidationResult(decision="final_failure", reason="cloud token budget 0")
            return ValidationResult(decision="escalate_cloud", reason=reason, next_tier="C1")
        return ValidationResult(decision="tier_up", reason=reason, next_tier=next_tier)
