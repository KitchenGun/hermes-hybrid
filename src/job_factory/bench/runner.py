"""BenchRunner — orchestrate the (model × dimension × prompt) grid.

This is the workhorse: given a set of LLM adapters and dimension/prompt
definitions, it runs every combination, scores the responses, and:

  1. Returns a structured :class:`BenchReport` (for the report file).
  2. Optionally feeds per-(job_type, model) scores into the live
     :class:`ScoreMatrix` so live traffic and bench observations share
     the same Welford accumulators.

Concurrency model:
  * **Per-model**: prompts run *sequentially* — most local LLM hosts
    (Ollama in particular) load weights into GPU memory and serialize
    requests anyway. Parallel prompts to the same model save no time.
  * **Across models**: capped by the GPU semaphore (default 1). Two
    32B models loading simultaneously is the textbook OOM case.

The runner never raises on individual prompt failures — they're recorded
in the report with ``passed=False`` so the benchmark always completes.
A single broken model doesn't take down the whole run.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable

from src.job_factory.bench.scorers import Scorer, make_scorer
from src.job_factory.bench.types import (
    BenchOutcome,
    BenchPromptResult,
    BenchReport,
    Dimension,
    DimensionScore,
    JobTypeWeights,
    ModelBenchResult,
)
from src.job_factory.score_matrix import ScoreMatrix
from src.llm.adapters.base import (
    AdapterRequest,
    AdapterResponse,
    ChatMessage,
    LLMAdapter,
)

log = logging.getLogger(__name__)

# Default per-prompt timeout. Big code-generation prompts on slow models
# can overrun even this; the LLMAdapter's own timeout takes precedence
# when it's tighter.
DEFAULT_PROMPT_TIMEOUT_S = 120.0


class BenchRunner:
    """Run benchmarks and aggregate results.

    Args:
        adapters: model_id → :class:`LLMAdapter`. The model_id is the
            ScoreMatrix key for live traffic too — keep it stable.
        dimensions: List of :class:`Dimension` to evaluate.
        job_type_weights: ``job_type → JobTypeWeights`` mapping. Determines
            how dimension scores combine into per-job_type scores. Empty
            dict ⇒ no per-job_type aggregation, only raw dimension scores
            in the report.
        score_matrix: Optional :class:`ScoreMatrix` to update with the
            derived per-job_type scores. Pass ``None`` for "report-only"
            runs (e.g., ad-hoc operator queries).
        scorers: Override map ``judge_kind → Scorer``. When omitted the
            runner builds default scorers via :func:`make_scorer`.
            Required override: ``"llm_judge"`` needs an explicit Scorer
            built with the project's judge LLMAdapter.
        gpu_concurrency: Max number of models running simultaneously.
            Default 1 (Ollama-friendly).
        per_prompt_timeout_s: Timeout per LLM call.
    """

    def __init__(
        self,
        *,
        adapters: dict[str, LLMAdapter],
        dimensions: list[Dimension],
        job_type_weights: dict[str, JobTypeWeights] | None = None,
        score_matrix: ScoreMatrix | None = None,
        scorers: dict[str, Scorer] | None = None,
        gpu_concurrency: int = 1,
        per_prompt_timeout_s: float = DEFAULT_PROMPT_TIMEOUT_S,
    ):
        if gpu_concurrency < 1:
            raise ValueError("gpu_concurrency must be >= 1")
        self._adapters = adapters
        self._dimensions = dimensions
        self._job_type_weights = job_type_weights or {}
        self._score_matrix = score_matrix
        self._timeout_s = per_prompt_timeout_s
        self._gpu_sem = asyncio.Semaphore(gpu_concurrency)
        self._scorers = self._build_scorer_map(scorers)

    def _build_scorer_map(
        self,
        overrides: dict[str, Scorer] | None,
    ) -> dict[str, Scorer]:
        """Construct the judge → Scorer map, honoring caller overrides.

        Defaults are constructed eagerly for deterministic judges
        (structural / execution / latency / label_match). The
        ``llm_judge`` default is *not* built because it requires an
        adapter — callers MUST pre-populate it via ``overrides`` if any
        dimension uses ``llm_judge``.
        """
        scorers: dict[str, Scorer] = {
            "structural": make_scorer("structural"),
            "execution": make_scorer("execution"),
            "label_match": make_scorer("label_match"),
            "latency": make_scorer("latency"),
        }
        if overrides:
            scorers.update(overrides)
        return scorers

    # ---- public API -------------------------------------------------------

    async def run(
        self,
        target_models: Iterable[str] | None = None,
    ) -> BenchReport:
        """Bench every model × dimension × prompt cell.

        Args:
            target_models: Iterable of model_ids to bench. ``None`` ⇒
                all adapters. Unknown ids are silently dropped (with a
                warning log) so callers can pass "all known models" lists
                that may be stale.
        """
        if target_models is None:
            models = list(self._adapters.keys())
        else:
            models = []
            for m in target_models:
                if m in self._adapters:
                    models.append(m)
                else:
                    log.warning(
                        "bench.unknown_model_skipped", extra={"model": m}
                    )

        report = BenchReport(target_models=list(models))
        # Run models concurrently up to gpu_concurrency. Per-model logic
        # is sequential inside _bench_model.
        await asyncio.gather(*(
            self._bench_model(m, report) for m in models
        ))

        # After all per-prompt results are collected, derive per-job_type
        # scores per model and (optionally) update the live ScoreMatrix.
        for model_id, mres in report.results.items():
            self._compute_job_type_scores(mres)
            if self._score_matrix is not None:
                await self._update_score_matrix(model_id, mres)
        return report

    # ---- per-model ---------------------------------------------------------

    async def _bench_model(
        self,
        model_id: str,
        report: BenchReport,
    ) -> None:
        adapter = self._adapters[model_id]
        # Use the adapter's stable provider/model identity for the report.
        provider = adapter.provider
        # ScoreMatrix key uses the model_id passed in (caller convention),
        # so the report's ``model`` mirrors that for lookup symmetry.
        mres = ModelBenchResult(model=model_id, provider=provider)
        report.results[model_id] = mres

        async with self._gpu_sem:
            for dim in self._dimensions:
                dscore = await self._bench_dimension(
                    model_id=model_id,
                    adapter=adapter,
                    dim=dim,
                    report=report,
                )
                mres.dimensions[dim.name] = dscore

    async def _bench_dimension(
        self,
        *,
        model_id: str,
        adapter: LLMAdapter,
        dim: Dimension,
        report: BenchReport,
    ) -> DimensionScore:
        scorer = self._scorers.get(dim.judge)
        if scorer is None:
            log.warning(
                "bench.scorer_missing",
                extra={"dimension": dim.name, "judge": dim.judge},
            )
            return DimensionScore()

        outcomes: list[BenchOutcome] = []
        for prompt in dim.prompts:
            outcome = await self._bench_one_prompt(
                adapter=adapter, prompt=prompt, scorer=scorer,
            )
            outcomes.append(outcome)
            report.add_prompt_result(BenchPromptResult(
                model=model_id,
                provider=adapter.provider,
                dimension=dim.name,
                prompt_id=prompt.id,
                outcome=outcome,
            ))

        return _aggregate_dimension(outcomes)

    async def _bench_one_prompt(
        self,
        *,
        adapter: LLMAdapter,
        prompt,
        scorer: Scorer,
    ) -> BenchOutcome:
        """Send one prompt, score the response. Failures → BenchOutcome
        with passed=False."""
        request = AdapterRequest(
            messages=[ChatMessage(role="user", content=prompt.prompt)],
            timeout_s=self._timeout_s,
        )
        start = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                adapter.generate(request),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError:
            return BenchOutcome(
                score=0.0,
                passed=False,
                latency_ms=int((time.perf_counter() - start) * 1000),
                error=f"prompt timeout after {self._timeout_s}s",
            )
        except Exception as e:  # noqa: BLE001
            return BenchOutcome(
                score=0.0,
                passed=False,
                latency_ms=int((time.perf_counter() - start) * 1000),
                error=f"adapter error: {type(e).__name__}: {e}",
            )

        try:
            outcome = await scorer.score(prompt=prompt, response=response)
        except Exception as e:  # noqa: BLE001
            return BenchOutcome(
                score=0.0,
                passed=False,
                latency_ms=response.duration_ms,
                error=f"scorer error: {type(e).__name__}: {e}",
            )
        return outcome

    # ---- aggregation ------------------------------------------------------

    def _compute_job_type_scores(self, mres: ModelBenchResult) -> None:
        """Collapse dimension scores into per-job_type scores using
        :class:`JobTypeWeights`. Stored back onto ``mres.job_type_scores``.
        """
        for job_type, jtw in self._job_type_weights.items():
            normalized = jtw.normalized()
            total = 0.0
            total_weight = 0.0
            for dim_name, w in normalized.weights.items():
                if dim_name not in mres.dimensions:
                    continue
                dim_score = mres.dimensions[dim_name].mean_score
                total += dim_score * w
                total_weight += w
            # If none of the requested dimensions actually ran for this
            # model, leave the job_type score absent (rather than 0.0
            # which would look like "tested but bad").
            if total_weight > 0:
                mres.job_type_scores[job_type] = total / total_weight

    async def _update_score_matrix(
        self,
        model_id: str,
        mres: ModelBenchResult,
    ) -> None:
        for job_type, score in mres.job_type_scores.items():
            await self._score_matrix.update(
                job_type=job_type,
                model=model_id,
                score=max(0.0, min(100.0, score)),
            )


def _aggregate_dimension(outcomes: list[BenchOutcome]) -> DimensionScore:
    if not outcomes:
        return DimensionScore()
    n = len(outcomes)
    n_passed = sum(1 for o in outcomes if o.passed)
    mean_score = sum(o.score for o in outcomes) / n
    successful = [o for o in outcomes if o.passed]
    if successful:
        mean_latency = sum(o.latency_ms for o in successful) / len(successful)
        with_tps = [o.tokens_per_sec for o in successful if o.tokens_per_sec > 0]
        mean_tps = sum(with_tps) / len(with_tps) if with_tps else 0.0
    else:
        mean_latency = 0.0
        mean_tps = 0.0
    return DimensionScore(
        n=n,
        n_passed=n_passed,
        mean_score=mean_score,
        mean_latency_ms=mean_latency,
        mean_tokens_per_sec=mean_tps,
    )
