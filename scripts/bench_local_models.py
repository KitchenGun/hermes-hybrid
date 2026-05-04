#!/usr/bin/env python3
"""bench_local_models.py — CLI for the Job Factory v2 bench harness.

Usage:
    # Bench every Ollama model installed locally (default).
    python scripts/bench_local_models.py

    # Bench only specific models.
    python scripts/bench_local_models.py \\
        --model qwen2.5:14b-instruct --model qwen2.5-coder:7b-instruct

    # Use a different config file.
    python scripts/bench_local_models.py \\
        --config config/benchmark_prompts.yaml \\
        --output data/benchmarks/manual.json

    # Don't update ScoreMatrix (report-only mode for ad-hoc analysis).
    python scripts/bench_local_models.py --no-update-matrix

Exit codes:
    0  bench completed successfully
    1  config error / invalid CLI args
    2  no models to bench (Ollama unreachable or empty model list)
    3  bench raised an unexpected exception
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

# Allow `python scripts/bench_local_models.py` from project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import get_settings  # noqa: E402
from src.job_factory.bench.loader import (  # noqa: E402
    BenchConfigError,
    load_dimensions,
    load_job_type_weights,
)
from src.job_factory.bench.runner import BenchRunner  # noqa: E402
from src.job_factory.bench.scorers import LLMJudgeScorer  # noqa: E402
from src.job_factory.bench.types import BenchReport  # noqa: E402
from src.job_factory.score_matrix import ScoreMatrix  # noqa: E402
from src.llm.adapters.ollama import OllamaAdapter  # noqa: E402
from src.llm.ollama_client import OllamaClient  # noqa: E402

log = logging.getLogger("bench_local_models")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--config",
        type=Path,
        default=_PROJECT_ROOT / "config" / "benchmark_prompts.yaml",
        help="Path to bench config YAML (default: config/benchmark_prompts.yaml)",
    )
    p.add_argument(
        "--score-matrix",
        type=Path,
        default=_PROJECT_ROOT / "data" / "job_factory" / "score_matrix.json",
        help="Path to ScoreMatrix JSON (default: data/job_factory/score_matrix.json)",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write the BenchReport here. Default: data/benchmarks/<timestamp>.json",
    )
    p.add_argument(
        "--model",
        action="append",
        default=None,
        metavar="MODEL",
        help="Bench this specific Ollama model (repeatable). Default: all installed.",
    )
    p.add_argument(
        "--claude-model",
        action="append",
        default=None,
        metavar="MODEL",
        help=(
            "Bench this Claude CLI model (haiku/sonnet/opus, repeatable). "
            "Uses Max OAuth via ClaudeCodeAdapter — counts against the "
            "session quota. Can be combined with --model to bench Ollama "
            "and Claude family in one sweep."
        ),
    )
    p.add_argument(
        "--no-update-matrix",
        action="store_true",
        help="Don't update ScoreMatrix — report only.",
    )
    p.add_argument(
        "--gpu-concurrency",
        type=int,
        default=1,
        help="Max simultaneous models on GPU (default: 1).",
    )
    p.add_argument(
        "--per-prompt-timeout",
        type=float,
        default=120.0,
        help="Per-prompt timeout in seconds (default: 120).",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="DEBUG-level logging.",
    )
    return p.parse_args()


async def _discover_ollama_models(settings) -> list[str]:
    """Ask the local Ollama server for installed model tags.

    Returns the empty list if Ollama is unreachable so the CLI can exit
    with a clear "no models to bench" message instead of crashing.
    """
    import urllib.error
    import urllib.request

    url = f"{settings.ollama_base_url.rstrip('/')}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        log.warning("ollama unreachable at %s: %s", url, e)
        return []
    return [m["name"] for m in data.get("models", []) if "name" in m]


def _build_judge_adapter(settings):
    """LLMJudgeScorer needs an adapter. Reuses the unified judge resolver
    in src.job_factory.builder so the CLI runner and the dispatcher build
    the SAME judge backend (driven by ``settings.bench_judge_backend``).

    For ``bench_judge_backend="claude_cli"`` we instantiate a ClaudeCodeAdapter
    here — the CLI runner has no orchestrator to share with — so the bench
    sweep can grade through Max OAuth without burning OpenAI dollars.
    Returns None when no backend resolves (matches legacy behavior).
    """
    from src.claude_adapter.adapter import ClaudeCodeAdapter
    from src.job_factory.builder import _build_judge_adapter as _resolve_judge

    claude_adapter = None
    if settings.bench_judge_backend == "claude_cli":
        try:
            claude_adapter = ClaudeCodeAdapter(settings)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "claude adapter init failed (%s); judge will fall back to ollama/openai",
                e,
            )

    return _resolve_judge(settings, claude_adapter=claude_adapter)


async def _amain() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # 1. Load config.
    try:
        dimensions = load_dimensions(args.config)
        job_type_weights = load_job_type_weights(args.config)
    except BenchConfigError as e:
        log.error("config error: %s", e)
        return 1

    if not dimensions:
        log.error("no dimensions defined in %s", args.config)
        return 1

    settings = get_settings()

    # 2. Resolve target models — Ollama (discovered or --model) plus
    # any --claude-model entries. Both can be specified in one sweep so
    # a single ScoreMatrix grid covers local + cloud arms.
    ollama_models: list[str] = []
    if args.model:
        ollama_models = list(args.model)
    elif not args.claude_model:
        # No --model and no --claude-model — fall back to discovery so
        # the legacy "bench every installed Ollama model" default holds.
        ollama_models = await _discover_ollama_models(settings)
        if not ollama_models:
            log.error(
                "no Ollama models found (server unreachable at %s "
                "or no models pulled). Set --model or --claude-model "
                "explicitly, or run `ollama list` to verify.",
                settings.ollama_base_url,
            )
            return 2

    claude_models: list[str] = list(args.claude_model or [])
    target_models = ollama_models + claude_models

    if not target_models:
        log.error("no target models — pass --model and/or --claude-model")
        return 2

    log.info(
        "benching %d model(s): ollama=%s claude_cli=%s",
        len(target_models), ollama_models, claude_models,
    )

    # 3. Build adapters per target model.
    adapters = {}
    for model_id in ollama_models:
        client = OllamaClient(settings.ollama_base_url, model_id)
        adapters[model_id] = OllamaAdapter(client)

    if claude_models:
        # Single ClaudeCodeAdapter shared across all Claude arms — the
        # subprocess concurrency cap (settings.c1_claude_code_concurrency)
        # then gates Max OAuth quota usage globally for this sweep,
        # rather than per-model.
        from src.claude_adapter.adapter import ClaudeCodeAdapter
        from src.llm.adapters.claude_cli import ClaudeCLIAdapter
        claude_base = ClaudeCodeAdapter(
            settings, concurrency=settings.c1_claude_code_concurrency
        )
        for model_id in claude_models:
            adapters[model_id] = ClaudeCLIAdapter(claude_base, model=model_id)

    # 4. Load score matrix (unless --no-update-matrix).
    matrix = None
    if not args.no_update_matrix:
        matrix = ScoreMatrix.load(args.score_matrix)
        log.info(
            "loaded ScoreMatrix from %s (%d cells existing)",
            args.score_matrix, len(matrix.cells),
        )

    # 5. Build judge adapter for llm_judge dimensions.
    judge = _build_judge_adapter(settings)
    scorer_overrides = {}
    if judge is not None:
        scorer_overrides["llm_judge"] = LLMJudgeScorer(judge)
        log.info("llm_judge available via %s/%s", judge.provider, judge.model)
    else:
        log.warning(
            "OPENAI_API_KEY not set — llm_judge dimensions will report "
            "scorer_missing for all prompts."
        )

    # 6. Run.
    runner = BenchRunner(
        adapters=adapters,
        dimensions=dimensions,
        job_type_weights=job_type_weights,
        score_matrix=matrix,
        scorers=scorer_overrides,
        gpu_concurrency=args.gpu_concurrency,
        per_prompt_timeout_s=args.per_prompt_timeout,
    )

    try:
        report = await runner.run(target_models=target_models)
    except Exception as e:  # noqa: BLE001
        log.exception("bench raised: %s", e)
        return 3

    # 7. Persist matrix (if updated).
    if matrix is not None:
        await matrix.persist()
        log.info("ScoreMatrix saved to %s (%d cells)",
                 args.score_matrix, len(matrix.cells))

    # 8. Write report.
    output_path = args.output or _default_report_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(_report_to_dict(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("report written to %s", output_path)

    _print_summary(report)
    return 0


def _default_report_path() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return _PROJECT_ROOT / "data" / "benchmarks" / f"{ts}.json"


def _report_to_dict(report: BenchReport) -> dict:
    """Custom serializer — dataclasses-asdict mostly works but datetimes
    need ISO conversion."""
    out = {
        "ran_at": report.ran_at.isoformat(),
        "target_models": list(report.target_models),
        "results": {
            model_id: {
                "model": mres.model,
                "provider": mres.provider,
                "dimensions": {
                    name: asdict(score)
                    for name, score in mres.dimensions.items()
                },
                "job_type_scores": dict(mres.job_type_scores),
            }
            for model_id, mres in report.results.items()
        },
        "prompt_results": [
            {
                "model": pr.model,
                "provider": pr.provider,
                "dimension": pr.dimension,
                "prompt_id": pr.prompt_id,
                "outcome": asdict(pr.outcome),
            }
            for pr in report.prompt_results
        ],
    }
    return out


def _print_summary(report: BenchReport) -> None:
    print()
    print("=" * 60)
    print(f"Bench completed at {report.ran_at.isoformat()}")
    print(f"Models: {len(report.results)}")
    print("=" * 60)
    for model_id, mres in report.results.items():
        print(f"\n[{model_id}] ({mres.provider})")
        for dim_name, score in mres.dimensions.items():
            print(
                f"  {dim_name:16s}  mean={score.mean_score:5.1f}  "
                f"pass={score.n_passed}/{score.n}  "
                f"lat={score.mean_latency_ms:6.0f}ms  "
                f"tps={score.mean_tokens_per_sec:5.1f}"
            )
        if mres.job_type_scores:
            print("  job_type scores:")
            for jt, sc in sorted(
                mres.job_type_scores.items(), key=lambda x: -x[1]
            ):
                print(f"    {jt:24s}  {sc:5.1f}")


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
