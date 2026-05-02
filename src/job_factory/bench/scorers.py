"""Per-dimension scorers — turn an LLM response into a 0–100 score.

Five judges, one per :class:`JudgeKind`:

  * ``StructuralScorer``  — JSON parse + required-field check (deterministic).
  * ``ExecutionScorer``   — exec generated code + run a pytest file (deterministic).
  * ``LLMJudgeScorer``    — GPT-4o-mini scores against a rubric (LLM-judged).
  * ``LabelMatchScorer``  — exact-match against a ground-truth label.
  * ``LatencyScorer``     — tokens/sec relative to a target throughput.

All scorers return :class:`BenchOutcome` with a clamped score in [0, 100].
The runner only calls ``score`` — never inspects internals — so adding a
new judge later is a matter of implementing the same protocol.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from src.job_factory.bench.types import BenchOutcome, BenchPrompt
from src.llm.adapters.base import (
    AdapterRequest,
    AdapterResponse,
    ChatMessage,
    LLMAdapter,
)

log = logging.getLogger(__name__)

# Compact preview length stored in BenchOutcome.response_text — keeps
# the report tractable when persisted to disk.
RESPONSE_PREVIEW_CHARS = 200

# Maximum execution time for execution-scored unit tests (sec). Above
# this, the prompt is considered failed (timeout = 0 score).
EXECUTION_TIMEOUT_SEC = 30


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _preview(text: str, n: int = RESPONSE_PREVIEW_CHARS) -> str:
    if len(text) <= n:
        return text
    return text[:n] + "…"


@runtime_checkable
class Scorer(Protocol):
    """Common scoring interface."""

    judge: str

    async def score(
        self,
        *,
        prompt: BenchPrompt,
        response: AdapterResponse,
    ) -> BenchOutcome: ...


# ---- StructuralScorer -----------------------------------------------------


class StructuralScorer:
    """Parse the response as JSON and verify required fields are present.

    Score formula:
      * 0 if not valid JSON.
      * Otherwise score = 100 * (fraction of required_fields present).
      * passed = score == 100.

    The scorer is forgiving about code-fence wrappers (\\`\\`\\`json ... \\`\\`\\`)
    because many local models add them despite "no code fence" prompts.
    """

    judge: str = "structural"

    _CODE_FENCE_RE = re.compile(
        r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$",
        re.DOTALL,
    )

    async def score(
        self,
        *,
        prompt: BenchPrompt,
        response: AdapterResponse,
    ) -> BenchOutcome:
        text = response.text.strip()
        # Strip code fences if present.
        m = self._CODE_FENCE_RE.match(text)
        if m:
            text = m.group("body").strip()

        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            return BenchOutcome(
                score=0.0,
                passed=False,
                latency_ms=response.duration_ms,
                response_text=_preview(response.text),
                error=f"JSONDecodeError: {e.msg}",
            )

        if not prompt.required_fields:
            # Just JSON-parseable is enough — full marks.
            return BenchOutcome(
                score=100.0,
                passed=True,
                latency_ms=response.duration_ms,
                response_text=_preview(response.text),
            )

        # Walk required fields. If response is an array, check the FIRST
        # item (that's the journal_ops convention — single object or
        # array of same-shaped objects).
        check_obj = obj[0] if isinstance(obj, list) and obj else obj
        if not isinstance(check_obj, dict):
            return BenchOutcome(
                score=0.0,
                passed=False,
                latency_ms=response.duration_ms,
                response_text=_preview(response.text),
                error=f"expected dict, got {type(check_obj).__name__}",
            )

        present = sum(1 for f in prompt.required_fields if f in check_obj)
        ratio = present / len(prompt.required_fields)
        score = _clamp(100.0 * ratio)
        return BenchOutcome(
            score=score,
            passed=ratio == 1.0,
            latency_ms=response.duration_ms,
            response_text=_preview(response.text),
            error="" if ratio == 1.0 else f"missing fields ({present}/{len(prompt.required_fields)})",
        )


# ---- ExecutionScorer ------------------------------------------------------


class ExecutionScorer:
    """Extract code from response, run a pytest file against it.

    Procedure:
      1. Pull the first code block from the response (\\`\\`\\`python ... \\`\\`\\`).
      2. Write it to a temp file ``solution.py``.
      3. Copy ``prompt.unit_test`` next to it as ``test_solution.py``.
      4. Run ``pytest`` on the directory.
      5. Score = 100 if all tests pass, else 0 (binary). passed = score==100.

    No partial credit on purpose — code that doesn't pass tests is, for
    benchmarking purposes, a failure. Future refinement could grade by
    fraction of passing tests.
    """

    judge: str = "execution"

    _CODE_BLOCK_RE = re.compile(
        r"```(?:python|py)?\s*\n(?P<body>.*?)\n```",
        re.DOTALL,
    )

    async def score(
        self,
        *,
        prompt: BenchPrompt,
        response: AdapterResponse,
    ) -> BenchOutcome:
        # Extract code block. If none found, treat the whole response as
        # code (some models reply with raw code without fences).
        m = self._CODE_BLOCK_RE.search(response.text)
        code = m.group("body") if m else response.text

        if not code.strip():
            return BenchOutcome(
                score=0.0,
                passed=False,
                latency_ms=response.duration_ms,
                response_text=_preview(response.text),
                error="empty code",
            )

        if not prompt.unit_test:
            return BenchOutcome(
                score=0.0,
                passed=False,
                latency_ms=response.duration_ms,
                response_text=_preview(response.text),
                error="prompt.unit_test missing — execution scorer needs it",
            )

        unit_test_path = Path(prompt.unit_test)
        if not unit_test_path.exists():
            return BenchOutcome(
                score=0.0,
                passed=False,
                latency_ms=response.duration_ms,
                response_text=_preview(response.text),
                error=f"unit_test file not found: {unit_test_path}",
            )

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "solution.py").write_text(code, encoding="utf-8")
            (td_path / "test_solution.py").write_text(
                unit_test_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, "-m", "pytest", str(td_path), "-q"],
                    capture_output=True,
                    text=True,
                    timeout=EXECUTION_TIMEOUT_SEC,
                )
            except subprocess.TimeoutExpired:
                return BenchOutcome(
                    score=0.0,
                    passed=False,
                    latency_ms=response.duration_ms,
                    response_text=_preview(response.text),
                    error=f"pytest timeout after {EXECUTION_TIMEOUT_SEC}s",
                )
            except Exception as e:  # noqa: BLE001
                return BenchOutcome(
                    score=0.0,
                    passed=False,
                    latency_ms=response.duration_ms,
                    response_text=_preview(response.text),
                    error=f"pytest invocation error: {type(e).__name__}: {e}",
                )

        if proc.returncode == 0:
            return BenchOutcome(
                score=100.0,
                passed=True,
                latency_ms=response.duration_ms,
                response_text=_preview(response.text),
            )
        # Tail of pytest output for the report.
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
        return BenchOutcome(
            score=0.0,
            passed=False,
            latency_ms=response.duration_ms,
            response_text=_preview(response.text),
            error="pytest failed: " + " | ".join(tail),
        )


# ---- LLMJudgeScorer -------------------------------------------------------


class LLMJudgeScorer:
    """Score by asking GPT-4o-mini (or any LLMAdapter) to grade against
    a free-text rubric.

    Judge prompt is fixed-format so the response is parseable:

        SCORE: <0-100>
        REASON: <one line>

    Score is the integer after "SCORE:". If parsing fails, score=0,
    passed=False.
    """

    judge: str = "llm_judge"

    _SCORE_RE = re.compile(r"SCORE\s*[:=]\s*(?P<n>\d{1,3})", re.IGNORECASE)

    def __init__(self, judge_adapter: LLMAdapter):
        self._adapter = judge_adapter

    async def score(
        self,
        *,
        prompt: BenchPrompt,
        response: AdapterResponse,
    ) -> BenchOutcome:
        rubric = prompt.rubric or "응답이 정확하고 자연스러운가?"
        judge_prompt = (
            "당신은 LLM 응답을 채점하는 평가자다. "
            "다음 형식으로만 출력하라:\n\n"
            "SCORE: <0~100 정수>\n"
            "REASON: <한 줄 평가>\n\n"
            f"## 채점 기준 (Rubric)\n{rubric}\n\n"
            f"## 원본 프롬프트\n{prompt.prompt}\n\n"
            f"## 평가할 응답\n{response.text}\n"
        )
        try:
            judgement = await self._adapter.generate(
                AdapterRequest(
                    messages=[ChatMessage(role="user", content=judge_prompt)],
                    max_tokens=256,
                    temperature=0.0,
                )
            )
        except Exception as e:  # noqa: BLE001
            return BenchOutcome(
                score=0.0,
                passed=False,
                latency_ms=response.duration_ms,
                response_text=_preview(response.text),
                error=f"judge call failed: {type(e).__name__}: {e}",
            )

        m = self._SCORE_RE.search(judgement.text)
        if not m:
            return BenchOutcome(
                score=0.0,
                passed=False,
                latency_ms=response.duration_ms,
                response_text=_preview(response.text),
                error=f"judge output unparseable: {_preview(judgement.text, 80)}",
            )
        score = _clamp(float(m.group("n")))
        return BenchOutcome(
            score=score,
            passed=score >= 60.0,  # judge convention: 60+ is "useful"
            latency_ms=response.duration_ms,
            response_text=_preview(response.text),
        )


# ---- LabelMatchScorer -----------------------------------------------------


class LabelMatchScorer:
    """Exact match against ``prompt.expected``.

    Used for routing classification (where the response should be a
    single label like "schedule_logging"). Strips whitespace and
    trailing punctuation before comparing. Case-insensitive.

    Score: 100 if match, else 0.
    """

    judge: str = "label_match"

    def __init__(self, *, allow_substring: bool = False):
        """``allow_substring=True`` accepts label found anywhere in the
        response (useful for chatty models that say "the answer is X")."""
        self._allow_substring = allow_substring

    async def score(
        self,
        *,
        prompt: BenchPrompt,
        response: AdapterResponse,
    ) -> BenchOutcome:
        if not prompt.expected:
            return BenchOutcome(
                score=0.0,
                passed=False,
                latency_ms=response.duration_ms,
                response_text=_preview(response.text),
                error="prompt.expected missing — label_match scorer needs it",
            )

        actual = response.text.strip().rstrip(".!?").lower()
        expected = prompt.expected.strip().lower()

        if self._allow_substring:
            matched = expected in actual
        else:
            matched = actual == expected

        return BenchOutcome(
            score=100.0 if matched else 0.0,
            passed=matched,
            latency_ms=response.duration_ms,
            response_text=_preview(response.text),
            error=""
            if matched
            else f"expected '{expected}', got '{_preview(actual, 40)}'",
        )


# ---- LatencyScorer --------------------------------------------------------


class LatencyScorer:
    """Score by tokens/second relative to a target throughput.

    Score = clamp(100 * (actual_tps / target_tps), 0, 100).

    No quality dimension — fast responses with garbage content still
    get full marks. Latency is meaningful only when combined with a
    quality dimension via ``JobTypeWeights``.
    """

    judge: str = "latency"

    def __init__(self, target_tokens_per_sec: float = 30.0):
        if target_tokens_per_sec <= 0:
            raise ValueError("target_tokens_per_sec must be positive")
        self._target = target_tokens_per_sec

    async def score(
        self,
        *,
        prompt: BenchPrompt,
        response: AdapterResponse,
    ) -> BenchOutcome:
        ms = response.duration_ms
        if ms <= 0 or response.completion_tokens <= 0:
            return BenchOutcome(
                score=0.0,
                passed=False,
                latency_ms=ms,
                response_text=_preview(response.text),
                error="missing duration or completion_tokens",
            )
        tps = (response.completion_tokens / ms) * 1000
        score = _clamp(100.0 * tps / self._target)
        return BenchOutcome(
            score=score,
            passed=tps >= self._target * 0.5,  # half-target = "passable"
            latency_ms=ms,
            tokens_per_sec=tps,
            response_text=_preview(response.text),
        )


# ---- Registry / factory ---------------------------------------------------


def make_scorer(
    judge: str,
    *,
    judge_adapter: LLMAdapter | None = None,
    target_tokens_per_sec: float = 30.0,
    allow_substring: bool = False,
) -> Scorer:
    """Construct a scorer by judge kind. Raises on unknown judge.

    ``judge_adapter`` is required for ``judge='llm_judge'``.
    """
    if judge == "structural":
        return StructuralScorer()
    if judge == "execution":
        return ExecutionScorer()
    if judge == "llm_judge":
        if judge_adapter is None:
            raise ValueError("llm_judge scorer requires judge_adapter")
        return LLMJudgeScorer(judge_adapter)
    if judge == "label_match":
        return LabelMatchScorer(allow_substring=allow_substring)
    if judge == "latency":
        return LatencyScorer(target_tokens_per_sec=target_tokens_per_sec)
    raise ValueError(f"unknown judge: {judge!r}")
