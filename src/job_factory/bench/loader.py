"""YAML loaders for the bench config.

Two files form the bench config:

  1. ``config/benchmark_prompts.yaml`` — top-level: dimension definitions
     (judge type + weight + path to prompts file) and the
     ``job_type_to_dimension_weights`` table.

  2. ``data/bench/<dimension>.yaml`` — per-dimension prompt list. Kept
     in a separate file per dimension because they grow and operators
     edit them independently.

This loader is **pure** — it returns dataclasses from
:mod:`src.job_factory.bench.types` and never invokes scorers or LLMs.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from src.job_factory.bench.types import (
    BenchPrompt,
    Dimension,
    JobTypeWeights,
    JudgeKind,
)

log = logging.getLogger(__name__)


class BenchConfigError(ValueError):
    """Raised when YAML structure is invalid (missing required key,
    wrong type). Wraps the original key path for easy debugging."""


def _yaml_load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise BenchConfigError(f"file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise BenchConfigError(f"YAML parse error in {path}: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise BenchConfigError(
            f"expected dict at top of {path}, got {type(data).__name__}"
        )
    return data


def load_prompts_file(path: Path) -> list[BenchPrompt]:
    """Load a per-dimension prompts YAML.

    Schema:
        prompts:
          - id: <unique-within-dim>
            prompt: <text>
            rubric: <optional, for llm_judge>
            expected: <optional, for label_match>
            unit_test: <optional path, for execution>
            required_fields: [list, of, field, names]   # for structural
            metadata: {free-form key-values}
    """
    data = _yaml_load(path)
    raw_prompts = data.get("prompts", [])
    if not isinstance(raw_prompts, list):
        raise BenchConfigError(
            f"prompts must be a list in {path}, got {type(raw_prompts).__name__}"
        )

    out: list[BenchPrompt] = []
    for i, raw in enumerate(raw_prompts):
        if not isinstance(raw, dict):
            raise BenchConfigError(
                f"prompts[{i}] in {path} must be a dict, got {type(raw).__name__}"
            )
        try:
            out.append(_parse_prompt(raw))
        except (KeyError, TypeError) as e:
            raise BenchConfigError(
                f"prompts[{i}] in {path}: {e}"
            ) from e
    return out


def _parse_prompt(raw: dict[str, Any]) -> BenchPrompt:
    pid = raw.get("id")
    if not pid or not isinstance(pid, str):
        raise BenchConfigError(f"prompt missing 'id': {raw}")
    prompt_text = raw.get("prompt", "")
    if not prompt_text or not isinstance(prompt_text, str):
        raise BenchConfigError(f"prompt {pid!r} missing 'prompt' text")

    required_fields = raw.get("required_fields", [])
    if not isinstance(required_fields, list):
        raise BenchConfigError(
            f"prompt {pid!r}: required_fields must be a list"
        )

    metadata = raw.get("metadata", {})
    if not isinstance(metadata, dict):
        raise BenchConfigError(f"prompt {pid!r}: metadata must be a dict")

    return BenchPrompt(
        id=pid,
        prompt=prompt_text,
        rubric=str(raw.get("rubric") or ""),
        expected=str(raw.get("expected") or ""),
        unit_test=str(raw.get("unit_test") or ""),
        required_fields=tuple(str(f) for f in required_fields),
        metadata=metadata,
    )


def load_dimensions(
    config_path: Path,
    *,
    base_dir: Path | None = None,
) -> list[Dimension]:
    """Load dimension definitions + their prompt files.

    ``base_dir`` is the directory used to resolve ``prompts_file`` paths
    when they are relative. Defaults to ``config_path.parent``.

    Schema (top-level):
        dimensions:
          <name>:
            weight: <float>
            judge: <structural|execution|llm_judge|label_match|latency>
            prompts_file: <relative or absolute path>
            target_tokens_per_sec: <float, only for latency>
    """
    data = _yaml_load(config_path)
    raw_dims = data.get("dimensions", {})
    if not isinstance(raw_dims, dict):
        raise BenchConfigError(
            f"'dimensions' must be a dict in {config_path}, "
            f"got {type(raw_dims).__name__}"
        )

    base = base_dir or config_path.parent
    out: list[Dimension] = []
    for name, raw in raw_dims.items():
        if not isinstance(raw, dict):
            raise BenchConfigError(
                f"dimensions.{name} must be a dict, got {type(raw).__name__}"
            )
        weight = raw.get("weight", 1.0)
        if not isinstance(weight, (int, float)):
            raise BenchConfigError(
                f"dimensions.{name}.weight must be numeric"
            )
        judge = raw.get("judge")
        if judge not in (
            "structural", "execution", "llm_judge",
            "label_match", "latency",
        ):
            raise BenchConfigError(
                f"dimensions.{name}.judge must be one of "
                f"structural/execution/llm_judge/label_match/latency, "
                f"got {judge!r}"
            )
        prompts_file = raw.get("prompts_file")
        if prompts_file:
            pf_path = Path(prompts_file)
            if not pf_path.is_absolute():
                pf_path = (base / pf_path).resolve()
            prompts = load_prompts_file(pf_path)
        else:
            # Inline prompts under this dimension.
            inline = raw.get("prompts", [])
            if isinstance(inline, list):
                prompts = [_parse_prompt(p) for p in inline]
            else:
                prompts = []
        target_tps = float(raw.get("target_tokens_per_sec", 30.0))

        out.append(
            Dimension(
                name=name,
                weight=float(weight),
                judge=judge,  # type: ignore[arg-type]
                prompts=prompts,
                target_tokens_per_sec=target_tps,
            )
        )
    return out


def load_job_type_weights(
    config_path: Path,
) -> dict[str, JobTypeWeights]:
    """Load the ``job_type_to_dimension_weights`` table.

    Schema:
        job_type_to_dimension_weights:
          simple_chat:
            korean: 0.4
            speed: 0.3
            json: 0.0
          code_generation:
            code_gen: 0.5
            ...
    """
    data = _yaml_load(config_path)
    raw_table = data.get("job_type_to_dimension_weights", {})
    if not isinstance(raw_table, dict):
        raise BenchConfigError(
            f"'job_type_to_dimension_weights' must be a dict in {config_path}"
        )

    out: dict[str, JobTypeWeights] = {}
    for job_type, raw_weights in raw_table.items():
        if not isinstance(raw_weights, dict):
            raise BenchConfigError(
                f"job_type_to_dimension_weights.{job_type} must be a dict"
            )
        weights: dict[str, float] = {}
        for dim_name, w in raw_weights.items():
            if not isinstance(w, (int, float)):
                raise BenchConfigError(
                    f"weights[{job_type}][{dim_name}] must be numeric"
                )
            weights[str(dim_name)] = float(w)
        out[str(job_type)] = JobTypeWeights(
            job_type=str(job_type),
            weights=weights,
        )
    return out
