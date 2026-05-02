"""Tests for src/job_factory/bench/loader.py.

Verifies:
  - Valid YAML round-trips into the right dataclasses.
  - Missing/invalid files raise BenchConfigError (no silent failures).
  - JobTypeWeights normalization sums to 1.0.
  - The actual checked-in config (config/benchmark_prompts.yaml) loads
    cleanly — guards against accidental config breakage.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.job_factory.bench.loader import (
    BenchConfigError,
    load_dimensions,
    load_job_type_weights,
    load_prompts_file,
)
from src.job_factory.bench.types import BenchPrompt


# ---- load_prompts_file ----------------------------------------------------


def _write_yaml(path: Path, data) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_load_prompts_file_minimal(tmp_path):
    p = tmp_path / "x.yaml"
    _write_yaml(p, {
        "prompts": [
            {"id": "p1", "prompt": "hello"},
        ],
    })
    out = load_prompts_file(p)
    assert len(out) == 1
    assert out[0].id == "p1"
    assert out[0].prompt == "hello"


def test_load_prompts_file_full_fields(tmp_path):
    p = tmp_path / "x.yaml"
    _write_yaml(p, {
        "prompts": [
            {
                "id": "p1",
                "prompt": "do thing",
                "rubric": "is it good",
                "expected": "label_x",
                "unit_test": "tests/foo.py",
                "required_fields": ["a", "b", "c"],
                "metadata": {"src": "manual"},
            },
        ],
    })
    out = load_prompts_file(p)
    p0 = out[0]
    assert p0.rubric == "is it good"
    assert p0.expected == "label_x"
    assert p0.unit_test == "tests/foo.py"
    assert p0.required_fields == ("a", "b", "c")
    assert p0.metadata == {"src": "manual"}


def test_load_prompts_file_missing_raises(tmp_path):
    with pytest.raises(BenchConfigError, match="not found"):
        load_prompts_file(tmp_path / "nope.yaml")


def test_load_prompts_file_invalid_yaml_raises(tmp_path):
    p = tmp_path / "x.yaml"
    p.write_text("[\nunclosed", encoding="utf-8")
    with pytest.raises(BenchConfigError, match="YAML parse"):
        load_prompts_file(p)


def test_load_prompts_file_missing_id_raises(tmp_path):
    p = tmp_path / "x.yaml"
    _write_yaml(p, {"prompts": [{"prompt": "no id here"}]})
    with pytest.raises(BenchConfigError, match="id"):
        load_prompts_file(p)


def test_load_prompts_file_required_fields_must_be_list(tmp_path):
    p = tmp_path / "x.yaml"
    _write_yaml(p, {"prompts": [
        {"id": "p", "prompt": "hi", "required_fields": "not_a_list"},
    ]})
    with pytest.raises(BenchConfigError, match="required_fields"):
        load_prompts_file(p)


def test_load_prompts_file_top_must_be_dict(tmp_path):
    p = tmp_path / "x.yaml"
    p.write_text("- just\n- a\n- list", encoding="utf-8")
    with pytest.raises(BenchConfigError, match="dict"):
        load_prompts_file(p)


# ---- load_dimensions ------------------------------------------------------


def test_load_dimensions_with_inline_prompts(tmp_path):
    p = tmp_path / "config.yaml"
    _write_yaml(p, {
        "dimensions": {
            "json": {
                "weight": 0.2,
                "judge": "structural",
                "prompts": [
                    {"id": "j1", "prompt": "make json"},
                ],
            },
        },
    })
    dims = load_dimensions(p)
    assert len(dims) == 1
    d = dims[0]
    assert d.name == "json"
    assert d.judge == "structural"
    assert d.weight == 0.2
    assert len(d.prompts) == 1


def test_load_dimensions_with_external_prompts_file(tmp_path):
    prompts_file = tmp_path / "korean.yaml"
    _write_yaml(prompts_file, {
        "prompts": [{"id": "k1", "prompt": "한국어"}],
    })
    config = tmp_path / "config.yaml"
    _write_yaml(config, {
        "dimensions": {
            "korean": {
                "weight": 0.15,
                "judge": "llm_judge",
                "prompts_file": "korean.yaml",
            },
        },
    })
    dims = load_dimensions(config)
    assert len(dims) == 1
    assert dims[0].prompts[0].id == "k1"


def test_load_dimensions_unknown_judge_raises(tmp_path):
    p = tmp_path / "config.yaml"
    _write_yaml(p, {
        "dimensions": {"x": {"weight": 0.1, "judge": "made_up"}},
    })
    with pytest.raises(BenchConfigError, match="judge"):
        load_dimensions(p)


def test_load_dimensions_latency_target_tps(tmp_path):
    p = tmp_path / "config.yaml"
    _write_yaml(p, {
        "dimensions": {
            "speed": {
                "weight": 0.1,
                "judge": "latency",
                "target_tokens_per_sec": 50.0,
            },
        },
    })
    dims = load_dimensions(p)
    assert dims[0].target_tokens_per_sec == 50.0


def test_load_dimensions_empty_returns_empty(tmp_path):
    p = tmp_path / "config.yaml"
    _write_yaml(p, {"dimensions": {}})
    assert load_dimensions(p) == []


# ---- load_job_type_weights ------------------------------------------------


def test_load_job_type_weights_basic(tmp_path):
    p = tmp_path / "config.yaml"
    _write_yaml(p, {
        "job_type_to_dimension_weights": {
            "simple_chat": {"korean": 0.5, "speed": 0.5},
            "code_generation": {"code_gen": 1.0},
        },
    })
    weights = load_job_type_weights(p)
    assert "simple_chat" in weights
    assert weights["simple_chat"].weights == {"korean": 0.5, "speed": 0.5}
    assert weights["code_generation"].weights == {"code_gen": 1.0}


def test_load_job_type_weights_normalization():
    from src.job_factory.bench.types import JobTypeWeights

    raw = JobTypeWeights(
        job_type="x",
        weights={"a": 2.0, "b": 8.0},  # sum 10
    )
    n = raw.normalized()
    assert n.weights["a"] == pytest.approx(0.2)
    assert n.weights["b"] == pytest.approx(0.8)
    assert sum(n.weights.values()) == pytest.approx(1.0)


def test_load_job_type_weights_zero_sum_returns_self():
    """Zero-sum weights → don't divide-by-zero, just return as-is."""
    from src.job_factory.bench.types import JobTypeWeights

    raw = JobTypeWeights(job_type="x", weights={"a": 0.0, "b": 0.0})
    n = raw.normalized()
    assert n.weights == {"a": 0.0, "b": 0.0}


def test_load_job_type_weights_non_numeric_raises(tmp_path):
    p = tmp_path / "config.yaml"
    _write_yaml(p, {
        "job_type_to_dimension_weights": {
            "x": {"korean": "high"},
        },
    })
    with pytest.raises(BenchConfigError, match="numeric"):
        load_job_type_weights(p)


# ---- end-to-end: real checked-in config -----------------------------------


def test_checked_in_config_loads_cleanly():
    """The actual config/benchmark_prompts.yaml in this repo must always
    parse — this test guards against accidentally breaking it."""
    project_root = Path(__file__).resolve().parent.parent
    config = project_root / "config" / "benchmark_prompts.yaml"
    assert config.exists(), f"expected {config} to exist"

    dims = load_dimensions(config)
    weights = load_job_type_weights(config)

    # All 8 dimensions present.
    dim_names = {d.name for d in dims}
    assert {
        "korean", "json", "code_gen", "code_review",
        "summarize", "routing", "long_context", "speed",
    } <= dim_names

    # Each dimension has at least one prompt.
    for d in dims:
        assert d.prompts, f"dimension {d.name} has no prompts"

    # Every prompt has a non-empty id and prompt text.
    for d in dims:
        for p in d.prompts:
            assert p.id
            assert p.prompt

    # 10 job_types defined.
    assert len(weights) >= 10
    for jt, jtw in weights.items():
        # Dimensions referenced should exist.
        for dim_name in jtw.weights:
            assert dim_name in dim_names, \
                f"job_type {jt} references unknown dim {dim_name}"
