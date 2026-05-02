"""Pure data types for the bench harness.

These describe the *what* of a benchmark run (which prompts, which
dimensions, which scoring kind) and the *result shape* (per-prompt
outcome, per-dimension aggregate, per-model aggregate, full report).

No I/O here — :mod:`runner` and :mod:`scorers` consume these.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

JudgeKind = Literal[
    "structural",  # JSON parse + schema check (auto, deterministic)
    "execution",   # exec generated code + run unit tests (auto)
    "llm_judge",   # GPT-4o-mini scores against rubric (LLM-judged)
    "label_match", # categorical ground truth (e.g., routing classification)
    "latency",     # latency + tokens/sec (no quality dimension)
]

DimensionName = str    # e.g., "korean", "json", "code_gen"
JobTypeName = str
ModelName = str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class BenchPrompt:
    """One prompt in a single benchmark dimension.

    Attributes:
        id: Stable identifier within the dimension (e.g., "ko_summary_1").
        prompt: The text actually sent to the model.
        rubric: Free-text scoring guidance (used by llm_judge scorer).
        expected: Ground truth (label_match scorer) or expected JSON
            schema name (structural). Empty for dimensions that don't
            need it.
        unit_test: Path to a pytest file (execution scorer). Empty for
            non-execution dimensions.
        required_fields: For structural scoring, fields that must appear
            in parsed JSON (subset of full schema validation).
        metadata: Free-form per-prompt metadata (e.g., text_file path,
            schema_file path). Scorers consume what they understand.
    """

    id: str
    prompt: str
    rubric: str = ""
    expected: str = ""
    unit_test: str = ""
    required_fields: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Dimension:
    """One evaluation axis (e.g., Korean understanding, JSON adherence).

    Attributes:
        name: Stable identifier — used as the key in the
            job_type_to_dimension_weights table.
        weight: Default weight when used as a *raw* dimension score
            (most callers go through job_type weights instead, which
            override this).
        judge: Which Scorer to use.
        prompts: All BenchPrompts in this dimension.
        target_tokens_per_sec: For ``judge == "latency"``, the threshold
            at which a model gets full marks. Below this, score scales
            linearly toward 0.
    """

    name: str
    weight: float
    judge: JudgeKind
    prompts: list[BenchPrompt] = field(default_factory=list)
    target_tokens_per_sec: float = 30.0


@dataclass(frozen=True)
class JobTypeWeights:
    """How dimension scores combine for one job_type.

    Stored as a (job_type → {dimension → weight}) lookup. Sum of weights
    per job_type SHOULD equal 1.0 but isn't enforced — the runner
    normalizes if not.

    Example:
        ``code_generation``: {"code_gen": 0.5, "code_review": 0.2,
                              "json": 0.1, "speed": 0.2}
    """

    job_type: str
    weights: dict[DimensionName, float]

    def normalized(self) -> "JobTypeWeights":
        """Return a copy with weights summed to 1.0 (defensive)."""
        total = sum(self.weights.values())
        if total <= 0:
            return self
        return JobTypeWeights(
            job_type=self.job_type,
            weights={k: v / total for k, v in self.weights.items()},
        )


@dataclass(frozen=True)
class BenchOutcome:
    """One scored prompt: 0–100 score plus the raw response/timing.

    Attributes:
        score: 0–100. Scorers MUST clamp; the runner trusts this.
        passed: True if the response was at least minimally usable
            (e.g., produced valid JSON for structural; finished within
            timeout for any). A failing outcome still has a score (often
            0), but is also tracked separately in ``failure_rate``.
        latency_ms: Wall-clock for the LLM call.
        tokens_per_sec: Generation speed; 0 if unavailable.
        response_text: First N chars of the model's response (truncated
            to keep the report compact).
        error: One-line error description if ``passed`` is False.
    """

    score: float
    passed: bool
    latency_ms: int
    tokens_per_sec: float = 0.0
    response_text: str = ""
    error: str = ""


@dataclass(frozen=True)
class BenchPromptResult:
    """A single (model, dimension, prompt) tuple's full record."""

    model: str
    provider: str
    dimension: str
    prompt_id: str
    outcome: BenchOutcome


@dataclass
class DimensionScore:
    """Aggregated score for one (model, dimension) pair.

    Attributes:
        n: Number of prompts run.
        n_passed: How many ``passed=True``.
        mean_score: Arithmetic mean of 0–100 scores (passed AND failed).
        mean_latency_ms: Mean latency over successful runs.
        mean_tokens_per_sec: Mean throughput over successful runs.
    """

    n: int = 0
    n_passed: int = 0
    mean_score: float = 0.0
    mean_latency_ms: float = 0.0
    mean_tokens_per_sec: float = 0.0

    @property
    def failure_rate(self) -> float:
        return 0.0 if self.n == 0 else 1.0 - (self.n_passed / self.n)


@dataclass
class ModelBenchResult:
    """All dimensions for one model.

    Attributes:
        model: Model identifier (provider-prefixed, e.g.,
            ``"ollama/qwen2.5:14b-instruct"``). Matches ScoreMatrix
            key prefix convention.
        provider: Provider id ("ollama", "openai", etc.).
        dimensions: dimension_name → DimensionScore.
        job_type_scores: job_type_name → derived score after applying
            JobTypeWeights to ``dimensions``. This is what feeds into
            ScoreMatrix.update().
    """

    model: str
    provider: str
    dimensions: dict[str, DimensionScore] = field(default_factory=dict)
    job_type_scores: dict[str, float] = field(default_factory=dict)


@dataclass
class BenchReport:
    """Top-level bench artifact.

    Persisted to ``data/benchmarks/<timestamp>.json``. Read by the
    BenchScheduler to detect new vs already-benchmarked models, and by
    operators for post-mortem analysis.
    """

    ran_at: datetime = field(default_factory=_utcnow)
    target_models: list[str] = field(default_factory=list)
    results: dict[str, ModelBenchResult] = field(default_factory=dict)
    prompt_results: list[BenchPromptResult] = field(default_factory=list)

    def add_prompt_result(self, r: BenchPromptResult) -> None:
        self.prompt_results.append(r)
