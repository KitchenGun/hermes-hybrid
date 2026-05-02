"""EpsilonGreedySelector — multi-armed bandit model picker for Job Factory v2.

Given a job_type and a set of available local models, decide which model
to invoke. The decision is **purely empirical** — driven by ScoreMatrix
observations, not by hand-tuned mappings.

Decision priority:
  1. **Cold-start**: if any model has fewer than ``warmup_n`` samples for
     this job_type, pick one of those models uniformly at random. This
     guarantees every (job_type, model) cell gets at least ``warmup_n``
     observations before exploitation kicks in.
  2. **Exploration**: with probability ``epsilon``, pick uniformly from
     all available models (regardless of past score). Lets the system
     re-evaluate stale cells and discover model improvements.
  3. **Exploitation**: pick the model with the highest empirical mean
     score for this job_type. Ties broken by lower observation count
     (prefer the less-tested arm — mild exploration).

References:
  - Sutton & Barto, "Reinforcement Learning: An Introduction" §2.3
    (epsilon-greedy action selection).
  - The cold-start / warmup phase is a domain-specific addition for the
    case where the matrix starts empty and we want every arm to have
    *some* observations before greedy picks dominate.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Literal

from src.job_factory.score_matrix import ScoreMatrix, ScoreStats

log = logging.getLogger(__name__)

SelectionReason = Literal[
    "cold_start",
    "exploration",
    "exploitation",
    "escalation",       # cloud/Claude step picked from a filtered candidate
                        # set after local attempts exhausted (Phase 6).
]


@dataclass(frozen=True)
class Selection:
    """Result of a selector decision.

    Attributes:
        model: The chosen model identifier.
        reason: Why it was chosen — drives observability/ledger.
        stats: A snapshot of the chosen model's stats for this job_type
            *at the moment of selection*. Useful for logging the
            selector's view of the world.
        candidates: List of (model, mean_score, n) tuples for every
            available model — for debug/observability. Ordered by
            descending mean. Empty if all candidates were unobserved
            (cold-start with no prior data).
    """

    model: str
    reason: SelectionReason
    stats: ScoreStats
    candidates: list[tuple[str, float, int]]


class EpsilonGreedySelector:
    """Epsilon-greedy bandit over a ScoreMatrix.

    Args:
        matrix: The ScoreMatrix to read from (does not mutate).
        epsilon: Exploration probability (0–1). Default 0.1.
        warmup_n: Minimum observations per (job_type, model) cell before
            cold-start gives way to epsilon-greedy. Default 5.
        rng: Optional seeded ``random.Random`` for reproducible tests.
            Default is a fresh ``random.Random()``.

    Concurrency: read-only on the matrix, no internal state — safe to
    share across coroutines.
    """

    def __init__(
        self,
        matrix: ScoreMatrix,
        *,
        epsilon: float = 0.1,
        warmup_n: int = 5,
        rng: random.Random | None = None,
    ):
        if not 0.0 <= epsilon <= 1.0:
            raise ValueError(f"epsilon must be in [0, 1], got {epsilon}")
        if warmup_n < 0:
            raise ValueError(f"warmup_n must be >= 0, got {warmup_n}")
        self.matrix = matrix
        self.epsilon = epsilon
        self.warmup_n = warmup_n
        self._rng = rng or random.Random()

    def select(
        self,
        *,
        job_type: str,
        available_models: list[str],
    ) -> Selection:
        """Pick a model for this job_type.

        Raises:
            ValueError: if ``available_models`` is empty.
        """
        if not available_models:
            raise ValueError(
                "available_models must contain at least one model"
            )

        # 1. Cold-start: any model below warmup_n? Prefer those.
        cold = [
            m
            for m in available_models
            if self.matrix.get(job_type, m).n < self.warmup_n
        ]
        if cold:
            chosen = self._rng.choice(cold)
            return Selection(
                model=chosen,
                reason="cold_start",
                stats=self.matrix.get(job_type, chosen),
                candidates=self._snapshot(job_type, available_models),
            )

        # 2. Exploration: with probability epsilon, pick uniformly.
        if self._rng.random() < self.epsilon:
            chosen = self._rng.choice(available_models)
            return Selection(
                model=chosen,
                reason="exploration",
                stats=self.matrix.get(job_type, chosen),
                candidates=self._snapshot(job_type, available_models),
            )

        # 3. Exploitation: argmax over mean. Tie-break: prefer fewer
        #    observations (gentle exploration of less-sampled arms).
        scored = [
            (m, self.matrix.get(job_type, m))
            for m in available_models
        ]
        scored.sort(key=lambda t: (-t[1].mean, t[1].n))
        chosen, stats = scored[0]
        return Selection(
            model=chosen,
            reason="exploitation",
            stats=stats,
            candidates=self._snapshot(job_type, available_models),
        )

    def _snapshot(
        self,
        job_type: str,
        models: list[str],
    ) -> list[tuple[str, float, int]]:
        """Compact (model, mean, n) tuples for observability."""
        snap = [
            (m, self.matrix.get(job_type, m).mean, self.matrix.get(job_type, m).n)
            for m in models
        ]
        snap.sort(key=lambda t: -t[1])
        return snap
