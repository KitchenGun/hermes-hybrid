"""JobFactoryValidator — score LLM responses 0-100 for the dispatcher.

Replaces ``dispatcher._default_validator`` (which returned 70 for any
non-empty text) with a composite scorer that adapts to job_type:

  * **LengthValidator**     — penalize too-short (suspicious) and over-
                               long (likely off-track) responses.
  * **StructuralValidator** — for jobs that should produce JSON
                               (schedule_logging, document_transform).
  * **LLMJudgeValidator**   — optional GPT-4o-mini grade against a
                               job-specific rubric (code_review,
                               summarize, code_generation).
  * **CompositeValidator**  — combines the above with weights per
                               job_type, returns one (score, passed)
                               tuple.

The dispatcher only sees the composite. Each individual validator is
independent and unit-tested against fabricated AdapterResponse objects.

Why a separate file from bench/scorers.py? Bench scorers measure
benchmark prompts (with rubrics, expected labels, unit tests embedded
in YAML). Production validators measure live responses (no rubric
upfront, just heuristics + optional judge). Different concerns, similar
shape — kept apart so production-time changes don't ripple through
bench.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from src.job_factory.registry import JobType
from src.llm.adapters.base import (
    AdapterRequest,
    AdapterResponse,
    ChatMessage,
    LLMAdapter,
)

log = logging.getLogger(__name__)


# ---- Result type ----------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    """Score 0-100 + per-axis breakdown.

    Attributes:
        score: 0-100, the composite that the dispatcher compares against
            ``JobType.quality_threshold``.
        passed_baseline: True if the response cleared all "must-have"
            checks (non-empty, JSON-parseable when required, etc.).
            False ⇒ score is typically 0 regardless of other axes.
        breakdown: ``axis_name → axis_score`` for ledger / observability.
        reason: Short human-readable summary (logged on failure).
    """

    score: float
    passed_baseline: bool
    breakdown: dict[str, float] = field(default_factory=dict)
    reason: str = ""


# ---- Protocol -------------------------------------------------------------


@runtime_checkable
class ResponseValidator(Protocol):
    """Async-aware single-axis validator. The composite calls each in
    turn and weighs them per ``CompositeValidator.weights``."""

    name: str

    async def evaluate(
        self,
        *,
        job: JobType,
        response: AdapterResponse,
    ) -> tuple[float, bool, str]:
        """Returns ``(axis_score 0-100, passed_baseline, reason)``.

        ``passed_baseline=False`` means a hard fail — composite will
        floor the overall score to 0 regardless of other axes.
        """
        ...


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


# ---- LengthValidator ------------------------------------------------------


class LengthValidator:
    """Penalize responses that are too short or absurdly long.

    Score curve:
      * 0-min_chars → linear from 0 to 100 (suspicious shortness)
      * min_chars-soft_max → 100 (sweet spot)
      * soft_max-hard_max → linear from 100 to 50 (verbose but ok)
      * > hard_max → 30 (likely off-task ramble)

    ``passed_baseline`` is False only for empty responses — we never
    *block* on length alone, just penalize.
    """

    name: str = "length"

    def __init__(
        self,
        *,
        min_chars: int = 5,
        soft_max: int = 4000,
        hard_max: int = 12000,
    ):
        if not (0 < min_chars < soft_max < hard_max):
            raise ValueError(
                "require 0 < min_chars < soft_max < hard_max"
            )
        self._min = min_chars
        self._soft = soft_max
        self._hard = hard_max

    async def evaluate(
        self,
        *,
        job: JobType,
        response: AdapterResponse,
    ) -> tuple[float, bool, str]:
        text = (response.text or "").strip()
        n = len(text)
        if n == 0:
            return 0.0, False, "empty response"
        if n < self._min:
            ratio = n / self._min
            return _clamp(100.0 * ratio), True, f"short ({n} chars)"
        if n <= self._soft:
            return 100.0, True, ""
        if n <= self._hard:
            # Linear 100 → 50 from soft to hard.
            t = (n - self._soft) / (self._hard - self._soft)
            return _clamp(100.0 - 50.0 * t), True, f"verbose ({n} chars)"
        return 30.0, True, f"too long ({n} chars)"


# ---- StructuralValidator --------------------------------------------------


class StructuralValidator:
    """For job_types that must produce JSON. Scores by required-field
    presence (same logic as bench's StructuralScorer, adapted)."""

    name: str = "structural"

    _CODE_FENCE_RE = re.compile(
        r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$",
        re.DOTALL,
    )

    def __init__(self, required_fields: tuple[str, ...] = ()):
        self._required = required_fields

    async def evaluate(
        self,
        *,
        job: JobType,
        response: AdapterResponse,
    ) -> tuple[float, bool, str]:
        text = (response.text or "").strip()
        if not text:
            return 0.0, False, "empty response"

        m = self._CODE_FENCE_RE.match(text)
        if m:
            text = m.group("body").strip()

        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            return 0.0, False, f"JSONDecodeError: {e.msg}"

        if not self._required:
            return 100.0, True, ""

        check_obj = obj[0] if isinstance(obj, list) and obj else obj
        if not isinstance(check_obj, dict):
            return 0.0, False, f"expected dict, got {type(check_obj).__name__}"

        present = sum(1 for f in self._required if f in check_obj)
        ratio = present / len(self._required)
        if ratio == 1.0:
            return 100.0, True, ""
        return (
            _clamp(100.0 * ratio),
            False,  # missing required fields = baseline fail
            f"missing required fields ({present}/{len(self._required)})",
        )


# ---- LLMJudgeValidator ----------------------------------------------------


class LLMJudgeValidator:
    """Score by asking GPT-4o-mini to grade the response per a rubric.

    The rubric is built from JobType context (e.g., "this is a
    schedule_logging response — does it look like a useful activity log
    summary?"). Caller passes a function that maps job_type → rubric,
    so different jobs get different criteria.
    """

    name: str = "llm_judge"

    _SCORE_RE = re.compile(r"SCORE\s*[:=]\s*(?P<n>\d{1,3})", re.IGNORECASE)

    def __init__(
        self,
        judge_adapter: LLMAdapter,
        rubric_fn: Callable[[JobType], str],
        *,
        timeout_s: float = 15.0,
    ):
        self._adapter = judge_adapter
        self._rubric_fn = rubric_fn
        self._timeout_s = timeout_s

    async def evaluate(
        self,
        *,
        job: JobType,
        response: AdapterResponse,
    ) -> tuple[float, bool, str]:
        text = (response.text or "").strip()
        if not text:
            return 0.0, False, "empty response"

        rubric = self._rubric_fn(job)
        prompt = (
            "당신은 LLM 응답 평가자다. 다음 형식으로만 출력하라:\n\n"
            "SCORE: <0~100>\n"
            "REASON: <한 줄>\n\n"
            f"## 채점 기준\n{rubric}\n\n"
            f"## 평가할 응답\n{text}\n"
        )
        try:
            judgement = await self._adapter.generate(
                AdapterRequest(
                    messages=[ChatMessage(role="user", content=prompt)],
                    max_tokens=128,
                    temperature=0.0,
                    timeout_s=self._timeout_s,
                )
            )
        except Exception as e:  # noqa: BLE001
            log.warning("validator.judge_failed", extra={"err": str(e)})
            # Judge failure ≠ response failure — fall back to neutral 50.
            return 50.0, True, f"judge unavailable: {type(e).__name__}"

        m = self._SCORE_RE.search(judgement.text)
        if not m:
            return 50.0, True, "judge output unparseable"
        score = _clamp(float(m.group("n")))
        return (
            score,
            score >= 40.0,  # judge says < 40 means baseline-fail
            f"judge score {score:.0f}",
        )


# ---- CompositeValidator ---------------------------------------------------


@dataclass
class CompositeValidator:
    """Combine multiple ResponseValidators with per-axis weights.

    The dispatcher uses one CompositeValidator per process. The mapping
    from job_type to which axes apply (and their weights) lives in the
    ``per_job_overrides`` dict — empty dict ⇒ same axes for all jobs.
    """

    axes: list[ResponseValidator]
    weights: dict[str, float]
    per_job_overrides: dict[str, dict[str, float]] = field(default_factory=dict)

    def _weights_for(self, job: JobType) -> dict[str, float]:
        return self.per_job_overrides.get(job.name, self.weights)

    async def evaluate(
        self,
        *,
        job: JobType,
        response: AdapterResponse,
    ) -> ValidationResult:
        weights = self._weights_for(job)
        # Normalize weights (defensive).
        total_w = sum(weights.values())
        if total_w <= 0:
            # No applicable axes for this job — neutral.
            return ValidationResult(
                score=50.0,
                passed_baseline=True,
                breakdown={},
                reason="no validators configured for this job_type",
            )

        breakdown: dict[str, float] = {}
        baseline_ok = True
        weighted_sum = 0.0
        weight_used = 0.0
        reasons: list[str] = []

        for axis in self.axes:
            w = weights.get(axis.name, 0.0)
            if w <= 0:
                continue
            try:
                score, ok, reason = await axis.evaluate(
                    job=job, response=response,
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "validator.axis_failed",
                    extra={"axis": axis.name, "err": str(e)},
                )
                score, ok, reason = 0.0, False, f"axis error: {e}"
            breakdown[axis.name] = score
            weighted_sum += score * w
            weight_used += w
            if not ok:
                baseline_ok = False
            if reason:
                reasons.append(f"{axis.name}={reason}")

        composite = (weighted_sum / weight_used) if weight_used > 0 else 0.0
        # Hard floor: any baseline-fail axis drops the score to 0 so the
        # dispatcher won't accept the response. (We still report the
        # arithmetic composite in breakdown so the ledger shows what
        # each axis thought.)
        final = composite if baseline_ok else 0.0

        return ValidationResult(
            score=final,
            passed_baseline=baseline_ok,
            breakdown=breakdown,
            reason="; ".join(reasons),
        )


# ---- Adapter to dispatcher's ValidatorFn shape ---------------------------


def make_dispatcher_validator(
    composite: CompositeValidator,
) -> Callable[[JobType, AdapterResponse], Awaitable[tuple[float, bool]]]:
    """Wrap a CompositeValidator in the (sync-looking) callable the
    dispatcher expects. Returns an async function so dispatcher can
    await it — Phase 6 also updates the dispatcher to await its
    validator (it was previously sync)."""

    async def _validator(
        job: JobType, response: AdapterResponse,
    ) -> tuple[float, bool]:
        result = await composite.evaluate(job=job, response=response)
        return result.score, result.passed_baseline

    return _validator


# ---- Standard rubric helpers ---------------------------------------------


def default_rubric(job: JobType) -> str:
    """Generic rubric used when no per-job rubric is configured.

    Asks the judge to score on usefulness + relevance + faithfulness."""
    return (
        f"이 응답은 '{job.name}' 작업의 결과다. 다음 기준으로 채점하라:\n"
        "- 유용성: 사용자에게 실제로 도움이 되는가?\n"
        "- 관련성: 사용자 요청에 직접 답하는가?\n"
        "- 충실성: 거짓·추측·환각이 없는가?\n"
        "100=훌륭, 70=쓸 만함, 40=부족, 0=완전 빗나감.\n"
    )
