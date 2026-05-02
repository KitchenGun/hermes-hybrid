"""Empirical benchmark harness for the Job Factory v2 ScoreMatrix.

The bench:
  1. Runs each (model, dimension, prompt) once.
  2. Scores the response with a dimension-appropriate Scorer.
  3. Combines dimension scores into per-job_type scores via the
     ``job_type_to_dimension_weights`` table.
  4. Persists the results into ScoreMatrix (using the same Welford-
     accumulation update path that production traffic uses, so bench
     observations and live observations are interchangeable).

Subpackage layout:
  - ``types``    — pure data classes shared across runner/scorers.
  - ``scorers``  — per-dimension judges (structural, execution,
    llm_judge, label_match, latency).
  - ``runner``   — orchestrates the model × dimension × prompt grid
    (added in Phase 3d).
"""
from src.job_factory.bench.types import (
    BenchOutcome,
    BenchPrompt,
    BenchPromptResult,
    BenchReport,
    Dimension,
    DimensionScore,
    JudgeKind,
    JobTypeWeights,
    ModelBenchResult,
)

__all__ = [
    "BenchOutcome",
    "BenchPrompt",
    "BenchPromptResult",
    "BenchReport",
    "Dimension",
    "DimensionScore",
    "JudgeKind",
    "JobTypeWeights",
    "ModelBenchResult",
]
