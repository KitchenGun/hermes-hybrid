"""Builder — assemble a fully-wired JobFactoryDispatcher.

This is the only place that knows how to combine all 11 components:

  1. JobTypeRegistry        ← config/job_factory.yaml
  2. ModelRegistry          ← config/model_registry.yaml
  3. ScoreMatrix            ← data/job_factory/score_matrix.json
  4. EpsilonGreedySelector  ← over the matrix
  5. Local LLM adapters     ← OllamaClient per local entry
  6. Cloud LLM adapters     ← ClaudeCodeAdapter per cloud entry
  7. Classifier LLM adapter ← tiny Ollama model (qwen2.5:3b by default)
  8. JobClassifier          ← keyword + LLM fallback
  9. CompositeValidator     ← Length + Structural + (LLMJudge if openai key)
 10. CloudPolicy            ← config/cloud_policy.yaml (or defaults)
 11. ActionRunner           ← optional, with caller-supplied ToolRegistry

Callers (orchestrator) use :func:`build_job_factory_dispatcher`. Tests
can call individual ``_build_*`` helpers to inject mocks at any layer.

The builder takes the project's ``Settings`` and an optional already-
constructed :class:`ClaudeCodeAdapter` (the orchestrator's existing
heavy-path adapter is reused so we don't spin up a second subprocess
pool). Cloud adapters are *only* registered when the necessary auth/
service is available — missing OpenAI key just means OpenAI arms aren't
in the cloud_adapters map, and dispatcher.policy gates the rest.
"""
from __future__ import annotations

import logging
from pathlib import Path

from src.claude_adapter.adapter import ClaudeCodeAdapter
from src.config import Settings
from src.job_factory.classifier import JobClassifier
from src.job_factory.dispatcher import JobFactoryDispatcher
from src.job_factory.policy import CloudPolicy, CloudPolicyConfig
from src.job_factory.registry import (
    JobTypeRegistry,
    ModelEntry,
    ModelRegistry,
)
from src.job_factory.runner import ActionRunner, ToolRegistry
from src.job_factory.score_matrix import ScoreMatrix
from src.job_factory.selector import EpsilonGreedySelector
from src.job_factory.validator import (
    CompositeValidator,
    LengthValidator,
    LLMJudgeValidator,
    StructuralValidator,
    default_rubric,
    make_dispatcher_validator,
)
from src.llm.adapters.base import LLMAdapter
from src.llm.adapters.claude_cli import ClaudeCLIAdapter
from src.llm.adapters.ollama import OllamaAdapter
from src.llm.ollama_client import OllamaClient

log = logging.getLogger(__name__)

# Project root — used to resolve config/ and data/ paths when the
# Settings object holds relative defaults.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ---- public entry ---------------------------------------------------------


def build_job_factory_dispatcher(
    settings: Settings,
    *,
    job_factory_yaml: Path | None = None,
    model_registry_yaml: Path | None = None,
    cloud_policy_yaml: Path | None = None,
    score_matrix_path: Path | None = None,
    claude_adapter: ClaudeCodeAdapter | None = None,
    tool_registry: ToolRegistry | None = None,
    epsilon: float = 0.1,
    warmup_n: int = 5,
    system_prompt: str | None = None,
) -> JobFactoryDispatcher:
    """Construct a fully-wired JobFactoryDispatcher.

    Args:
        settings: Hermes-hybrid Settings (env-loaded).
        job_factory_yaml: Override path. Default: config/job_factory.yaml.
        model_registry_yaml: Override path. Default: config/model_registry.yaml.
        cloud_policy_yaml: Override path. Default: config/cloud_policy.yaml.
            File is optional — defaults are used when absent.
        score_matrix_path: Override path. Default: ``settings.score_matrix_path``.
        claude_adapter: Reuse the orchestrator's heavy-path Claude
            adapter so we share the session-quota semaphore. Pass None
            to disable Claude routing entirely.
        tool_registry: Pre-populated tool registry. ``None`` ⇒ runner
            has no tools, so any ``action.tool != "respond_to_user"``
            gets denied (safe default).
        epsilon: Selector exploration rate.
        warmup_n: Selector cold-start threshold.
        system_prompt: Optional system message prepended to every turn.
    """
    # Resolve paths.
    jobs_path = job_factory_yaml or _PROJECT_ROOT / "config" / "job_factory.yaml"
    models_path = model_registry_yaml or _PROJECT_ROOT / "config" / "model_registry.yaml"
    policy_path = cloud_policy_yaml or _PROJECT_ROOT / "config" / "cloud_policy.yaml"
    matrix_path = (
        score_matrix_path or _resolve(settings.score_matrix_path)
    )

    # Load registries.
    jobs = JobTypeRegistry.from_yaml(jobs_path)
    models = ModelRegistry.from_yaml(models_path)
    log.info(
        "jf.builder.registries_loaded",
        extra={
            "job_types": len(jobs.job_types),
            "local_models": len(models.local),
            "cloud_models": len(models.cloud),
        },
    )

    # ScoreMatrix.
    matrix = ScoreMatrix.load(matrix_path)
    log.info(
        "jf.builder.matrix_loaded",
        extra={"path": str(matrix_path), "cells": len(matrix.cells)},
    )

    # Selector.
    selector = EpsilonGreedySelector(
        matrix,
        epsilon=epsilon,
        warmup_n=warmup_n,
    )

    # Adapters.
    local_adapters = build_local_adapters(settings, models)
    cloud_adapters = build_cloud_adapters(
        settings, models, claude_adapter=claude_adapter,
    )
    log.info(
        "jf.builder.adapters_built",
        extra={
            "local": len(local_adapters),
            "cloud": len(cloud_adapters),
        },
    )

    # Classifier (uses tiny ollama model from registry config).
    classifier_adapter = build_classifier_adapter(settings, jobs)
    classifier = JobClassifier(jobs, llm_adapter=classifier_adapter)

    # Validator. Pass claude_adapter so the judge can route through
    # Max OAuth (bench_judge_backend="claude_cli") instead of OpenAI.
    validator = build_validator(settings, jobs, claude_adapter=claude_adapter)
    dispatcher_validator = make_dispatcher_validator(validator)

    # Cloud policy.
    policy_config = CloudPolicyConfig.from_yaml(policy_path)
    cloud_policy = CloudPolicy(config=policy_config)
    log.info(
        "jf.builder.policy_loaded",
        extra={"path": str(policy_path)},
    )

    # Runner — only constructed when a tool registry is supplied; without
    # tools, the runner just does respond_only / respond_to_user, which
    # the dispatcher handles either way.
    runner: ActionRunner | None = None
    if tool_registry is not None:
        runner = ActionRunner(tool_registry)

    return JobFactoryDispatcher(
        classifier=classifier,
        job_registry=jobs,
        model_registry=models,
        selector=selector,
        score_matrix=matrix,
        local_adapters=local_adapters,
        cloud_adapters=cloud_adapters,
        runner=runner,
        validator=dispatcher_validator,
        cloud_policy=cloud_policy,
        system_prompt=system_prompt,
    )


# ---- adapter builders (exposed for tests) --------------------------------


def build_local_adapters(
    settings: Settings,
    models: ModelRegistry,
) -> dict[str, LLMAdapter]:
    """Construct OllamaAdapter for each local model in the registry."""
    out: dict[str, LLMAdapter] = {}
    for entry in models.local:
        if entry.provider == "ollama":
            client = OllamaClient(settings.ollama_base_url, entry.name)
            out[entry.matrix_key] = OllamaAdapter(client)
        else:
            log.warning(
                "jf.builder.unknown_local_provider",
                extra={"matrix_key": entry.matrix_key, "provider": entry.provider},
            )
    return out


def build_cloud_adapters(
    settings: Settings,
    models: ModelRegistry,
    *,
    claude_adapter: ClaudeCodeAdapter | None = None,
) -> dict[str, LLMAdapter]:
    """Construct cloud adapters per the registry, skipping any whose
    adapter isn't configured.

    2026-05-04: OpenAI legacy removed. Claude CLI is the only cloud lane.
    Entries are skipped if ``claude_adapter`` is None (i.e., orchestrator
    didn't wire one in).
    """
    out: dict[str, LLMAdapter] = {}
    for entry in models.cloud:
        if entry.provider == "claude_cli":
            if claude_adapter is None:
                log.info(
                    "jf.builder.claude_skipped",
                    extra={"matrix_key": entry.matrix_key, "reason": "no adapter"},
                )
                continue
            out[entry.matrix_key] = ClaudeCLIAdapter(claude_adapter, entry.name)
        else:
            log.warning(
                "jf.builder.unknown_cloud_provider",
                extra={"matrix_key": entry.matrix_key, "provider": entry.provider},
            )
    return out


def build_classifier_adapter(
    settings: Settings,
    jobs: JobTypeRegistry,
) -> LLMAdapter | None:
    """Tiny LLM for the JobClassifier fallback path. Uses the model
    named in ``classifier.llm_model`` (default ``qwen2.5:3b-instruct``).

    Returns None if Ollama isn't enabled — keyword fast path still works,
    fallback to ``classifier.fallback_job_type`` covers the rest."""
    if not settings.ollama_routable:
        return None
    client = OllamaClient(
        settings.ollama_base_url,
        jobs.classifier.llm_model,
    )
    return OllamaAdapter(client)


# ---- validator builder ----------------------------------------------------


def build_validator(
    settings: Settings,
    jobs: JobTypeRegistry,
    *,
    claude_adapter: ClaudeCodeAdapter | None = None,
) -> CompositeValidator:
    """Default Phase 6 validator wiring.

    Three axes:
      * ``length``     — always on (cheap heuristic).
      * ``structural`` — JSON parse, used heavily for schedule_logging.
      * ``llm_judge``  — graded by ``settings.bench_judge_backend``
                         (claude_cli / ollama / openai). Skipped when no
                         backend is available.

    ``per_job_overrides`` defines weights per job_type — see the table
    below. These are sensible defaults; operators can subclass or
    monkey-patch the result for tighter control.
    """
    axes = [LengthValidator(), StructuralValidator()]
    judge_adapter = _build_judge_adapter(settings, claude_adapter=claude_adapter)
    if judge_adapter is not None:
        axes.append(LLMJudgeValidator(judge_adapter, default_rubric))

    have_judge = judge_adapter is not None

    # Default weights — used when a job_type isn't in the override table.
    weights = {
        "length": 0.4,
        "structural": 0.3,
        "llm_judge": 0.3 if have_judge else 0.0,
    }

    # Per-job overrides. The 10 job_types from config/job_factory.yaml
    # get tuned weights; any job_type not listed here falls back to
    # ``weights`` above.
    per_job: dict[str, dict[str, float]] = {
        # Conversational: length matters; structure doesn't.
        "simple_chat": {"length": 0.7, "structural": 0.0,
                        "llm_judge": 0.3 if have_judge else 0.0},
        # Summary needs to be coherent + right length.
        "summarize": {"length": 0.4, "structural": 0.0,
                      "llm_judge": 0.6 if have_judge else 0.0},
        # Code review: judge dominates (judges actually find issues).
        "code_review": {"length": 0.2, "structural": 0.0,
                        "llm_judge": 0.8 if have_judge else 0.0},
        # Code generation: judge dominates (does the code make sense?).
        "code_generation": {"length": 0.2, "structural": 0.0,
                            "llm_judge": 0.8 if have_judge else 0.0},
        # Architecture: long, judge-heavy.
        "architecture_design": {"length": 0.3, "structural": 0.0,
                                "llm_judge": 0.7 if have_judge else 0.0},
        # Web research: judge for relevance, length for thoroughness.
        "web_research": {"length": 0.4, "structural": 0.1,
                         "llm_judge": 0.5 if have_judge else 0.0},
        # Document transform: structure matters most (often JSON/YAML).
        "document_transform": {"length": 0.2, "structural": 0.6,
                               "llm_judge": 0.2 if have_judge else 0.0},
        # Schedule logging: structural is critical (24-field JSON).
        "schedule_logging": {"length": 0.2, "structural": 0.8,
                             "llm_judge": 0.0},
        # Image asset request: routing label, structure, judge.
        "image_asset_request": {"length": 0.3, "structural": 0.4,
                                "llm_judge": 0.3 if have_judge else 0.0},
        # Heavy project: judge dominates (long-form quality matters).
        "heavy_project_task": {"length": 0.2, "structural": 0.0,
                               "llm_judge": 0.8 if have_judge else 0.0},
    }

    return CompositeValidator(
        axes=axes,
        weights=weights,
        per_job_overrides=per_job,
    )


def _build_judge_adapter(
    settings: Settings,
    *,
    claude_adapter: ClaudeCodeAdapter | None = None,
) -> LLMAdapter | None:
    """Judge adapter for LLMJudgeValidator.

    Resolution order is driven by ``settings.bench_judge_backend``:

      * ``"claude_cli"`` (default) — use ClaudeCLIAdapter with the C1 alias
        (haiku) for cheap+fast grading. Falls back to ``"ollama"`` if the
        caller didn't supply a ``claude_adapter`` (orchestrator integration
        path) so misconfiguration degrades, not crashes.
      * ``"ollama"`` — use OllamaAdapter with ``ollama_judge_model``. Free,
        unlimited, lower quality than Claude. The ONLY safe choice for the
        auto BenchScheduler — Claude Max quota would otherwise drain.

    2026-05-04: ``"openai"`` backend removed when API legacy was purged.

    Returns None when no backend is available; CompositeValidator drops
    the llm_judge axis silently in that case.
    """
    backend = settings.bench_judge_backend

    if backend == "claude_cli":
        if claude_adapter is None:
            log.warning(
                "jf.builder.judge_claude_no_adapter",
                extra={"fallback": "ollama"},
            )
            backend = "ollama"
        else:
            return ClaudeCLIAdapter(claude_adapter, settings.c1_claude_code_model)

    if backend == "ollama":
        if not settings.ollama_routable:
            log.warning(
                "jf.builder.judge_ollama_disabled",
                extra={"reason": "ollama_enabled=False and local_first_mode=False"},
            )
            return None
        client = OllamaClient(settings.ollama_base_url, settings.ollama_judge_model)
        return OllamaAdapter(client)

    return None


# ---- helpers -------------------------------------------------------------


def _resolve(p: Path) -> Path:
    """If ``p`` is relative, resolve against project root. Absolute
    paths pass through unchanged."""
    if p.is_absolute():
        return p
    return (_PROJECT_ROOT / p).resolve()
