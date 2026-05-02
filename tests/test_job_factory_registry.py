"""Tests for src/job_factory/registry.py."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.job_factory.registry import (
    DiscoveryConfig,
    JobType,
    JobTypeRegistry,
    ModelEntry,
    ModelRegistry,
    RegistryConfigError,
)


def _write_yaml(path: Path, data) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# ---- JobTypeRegistry -------------------------------------------------------


def test_jobtyperegistry_minimal(tmp_path):
    p = tmp_path / "jf.yaml"
    _write_yaml(p, {
        "job_types": [
            {"name": "simple_chat"},
        ],
    })
    reg = JobTypeRegistry.from_yaml(p)
    jt = reg.get("simple_chat")
    assert jt.name == "simple_chat"
    # Defaults.
    assert jt.max_attempts == 2
    assert jt.cloud_allowed is True
    assert jt.claude_allowed is False
    # Classifier default.
    assert reg.classifier.fast_keyword_path is True
    assert reg.classifier.fallback_job_type == "simple_chat"


def test_jobtyperegistry_full_fields(tmp_path):
    p = tmp_path / "jf.yaml"
    _write_yaml(p, {
        "classifier": {
            "fast_keyword_path": False,
            "llm_model": "qwen2.5:7b-instruct",
            "llm_timeout_seconds": 10,
            "fallback_job_type": "simple_chat",
        },
        "job_types": [
            {
                "name": "simple_chat",
                "keywords_ko": ["안녕"],
                "keywords_en": ["hi"],
                "max_attempts": 3,
                "quality_threshold": 70,
                "cloud_allowed": True,
                "claude_allowed": True,
                "requires_user_approval": True,
                "timeout_seconds": 90,
            },
        ],
    })
    reg = JobTypeRegistry.from_yaml(p)
    jt = reg.get("simple_chat")
    assert jt.keywords_ko == ("안녕",)
    assert jt.keywords_en == ("hi",)
    assert jt.max_attempts == 3
    assert jt.requires_user_approval is True
    assert jt.timeout_seconds == 90
    assert reg.classifier.fast_keyword_path is False
    assert reg.classifier.llm_model == "qwen2.5:7b-instruct"


def test_jobtyperegistry_get_unknown_raises(tmp_path):
    p = tmp_path / "jf.yaml"
    _write_yaml(p, {"job_types": [{"name": "simple_chat"}]})
    reg = JobTypeRegistry.from_yaml(p)
    with pytest.raises(KeyError):
        reg.get("not_a_real_job")


def test_jobtyperegistry_duplicate_name_raises(tmp_path):
    p = tmp_path / "jf.yaml"
    _write_yaml(p, {
        "job_types": [
            {"name": "x"},
            {"name": "x"},
        ],
    })
    with pytest.raises(RegistryConfigError, match="duplicate"):
        JobTypeRegistry.from_yaml(p)


def test_jobtyperegistry_missing_name_raises(tmp_path):
    p = tmp_path / "jf.yaml"
    _write_yaml(p, {
        "job_types": [{"keywords_ko": ["x"]}],
    })
    with pytest.raises(RegistryConfigError, match="name"):
        JobTypeRegistry.from_yaml(p)


def test_jobtyperegistry_empty_job_types_raises(tmp_path):
    p = tmp_path / "jf.yaml"
    _write_yaml(p, {"job_types": []})
    with pytest.raises(RegistryConfigError, match="non-empty"):
        JobTypeRegistry.from_yaml(p)


def test_jobtyperegistry_unknown_fallback_raises(tmp_path):
    p = tmp_path / "jf.yaml"
    _write_yaml(p, {
        "classifier": {"fallback_job_type": "no_such_job"},
        "job_types": [{"name": "simple_chat"}],
    })
    with pytest.raises(RegistryConfigError, match="fallback_job_type"):
        JobTypeRegistry.from_yaml(p)


def test_jobtyperegistry_keywords_must_be_list(tmp_path):
    p = tmp_path / "jf.yaml"
    _write_yaml(p, {
        "job_types": [
            {"name": "x", "keywords_ko": "not a list"},
        ],
    })
    with pytest.raises(RegistryConfigError, match="list of strings"):
        JobTypeRegistry.from_yaml(p)


def test_jobtyperegistry_missing_file_raises(tmp_path):
    with pytest.raises(RegistryConfigError, match="not found"):
        JobTypeRegistry.from_yaml(tmp_path / "nope.yaml")


def test_jobtyperegistry_invalid_yaml_raises(tmp_path):
    p = tmp_path / "jf.yaml"
    p.write_text("[\nunclosed", encoding="utf-8")
    with pytest.raises(RegistryConfigError, match="YAML parse"):
        JobTypeRegistry.from_yaml(p)


# ---- ModelRegistry --------------------------------------------------------


def test_modelregistry_minimal(tmp_path):
    p = tmp_path / "mr.yaml"
    _write_yaml(p, {
        "local": [
            {"provider": "ollama", "name": "qwen2.5:7b"},
        ],
    })
    reg = ModelRegistry.from_yaml(p)
    assert len(reg.local) == 1
    assert reg.local[0].provider == "ollama"
    assert reg.local[0].name == "qwen2.5:7b"
    assert reg.local[0].matrix_key == "ollama/qwen2.5:7b"


def test_modelregistry_with_cloud(tmp_path):
    p = tmp_path / "mr.yaml"
    _write_yaml(p, {
        "local": [{"provider": "ollama", "name": "qwen2.5:7b"}],
        "cloud": [
            {
                "provider": "openai",
                "name": "gpt-4o-mini",
                "cost_input_per_1m": 0.15,
                "cost_output_per_1m": 0.60,
            },
        ],
    })
    reg = ModelRegistry.from_yaml(p)
    assert len(reg.local) == 1
    assert len(reg.cloud) == 1
    assert reg.cloud[0].cost_input_per_1m == pytest.approx(0.15)
    assert reg.cloud[0].cost_output_per_1m == pytest.approx(0.60)


def test_modelregistry_find_returns_entry(tmp_path):
    p = tmp_path / "mr.yaml"
    _write_yaml(p, {
        "local": [{"provider": "ollama", "name": "x"}],
        "cloud": [{"provider": "openai", "name": "gpt-4o"}],
    })
    reg = ModelRegistry.from_yaml(p)
    entry = reg.find("openai/gpt-4o")
    assert entry is not None
    assert entry.provider == "openai"
    assert reg.find("not/here") is None


def test_modelregistry_local_keys_cloud_keys(tmp_path):
    p = tmp_path / "mr.yaml"
    _write_yaml(p, {
        "local": [{"provider": "ollama", "name": "a"}],
        "cloud": [
            {"provider": "openai", "name": "x"},
            {"provider": "claude_cli", "name": "haiku"},
        ],
    })
    reg = ModelRegistry.from_yaml(p)
    assert reg.local_keys() == ["ollama/a"]
    assert set(reg.cloud_keys()) == {"openai/x", "claude_cli/haiku"}


def test_modelregistry_duplicate_matrix_key_raises(tmp_path):
    p = tmp_path / "mr.yaml"
    _write_yaml(p, {
        "local": [{"provider": "ollama", "name": "x"}],
        "cloud": [{"provider": "ollama", "name": "x"}],  # collides
    })
    with pytest.raises(RegistryConfigError, match="duplicate"):
        ModelRegistry.from_yaml(p)


def test_modelregistry_missing_provider_raises(tmp_path):
    p = tmp_path / "mr.yaml"
    _write_yaml(p, {"local": [{"name": "x"}]})
    with pytest.raises(RegistryConfigError, match="provider"):
        ModelRegistry.from_yaml(p)


def test_modelregistry_missing_name_raises(tmp_path):
    p = tmp_path / "mr.yaml"
    _write_yaml(p, {"local": [{"provider": "ollama"}]})
    with pytest.raises(RegistryConfigError, match="name"):
        ModelRegistry.from_yaml(p)


def test_modelregistry_discovery_defaults(tmp_path):
    p = tmp_path / "mr.yaml"
    _write_yaml(p, {"local": [{"provider": "ollama", "name": "x"}]})
    reg = ModelRegistry.from_yaml(p)
    assert reg.discovery.ollama_poll_interval_seconds == 300
    assert reg.discovery.auto_bench_on_new_model is True


def test_modelregistry_discovery_custom(tmp_path):
    p = tmp_path / "mr.yaml"
    _write_yaml(p, {
        "local": [{"provider": "ollama", "name": "x"}],
        "discovery": {
            "ollama_poll_interval_seconds": 60,
            "auto_bench_on_new_model": False,
        },
    })
    reg = ModelRegistry.from_yaml(p)
    assert reg.discovery.ollama_poll_interval_seconds == 60
    assert reg.discovery.auto_bench_on_new_model is False


# ---- end-to-end: real checked-in configs load ----------------------------


def test_real_job_factory_yaml_loads():
    project_root = Path(__file__).resolve().parent.parent
    p = project_root / "config" / "job_factory.yaml"
    assert p.exists()
    reg = JobTypeRegistry.from_yaml(p)
    # Sanity: 10 job_types from the v2 design.
    assert len(reg.job_types) == 10
    assert reg.has("simple_chat")
    assert reg.has("heavy_project_task")
    # heavy_project_task should require approval per the YAML.
    heavy = reg.get("heavy_project_task")
    assert heavy.requires_user_approval is True
    assert heavy.claude_allowed is True


def test_real_model_registry_yaml_loads():
    project_root = Path(__file__).resolve().parent.parent
    p = project_root / "config" / "model_registry.yaml"
    assert p.exists()
    reg = ModelRegistry.from_yaml(p)
    # Sanity: at least the qwen 5 + openai 2 + claude 3 entries.
    assert len(reg.local) >= 3
    assert len(reg.cloud) >= 5
    # Cost fields populated for cloud.
    gpt_mini = reg.find("openai/gpt-4o-mini")
    assert gpt_mini is not None
    assert gpt_mini.cost_input_per_1m > 0
