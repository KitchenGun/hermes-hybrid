"""Tests for src/job_factory/builder.py.

Verify component assembly: each helper produces the right adapters
under the right Settings, and the all-in-one
:func:`build_job_factory_dispatcher` returns a wired dispatcher.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Settings
from src.job_factory.builder import (
    build_classifier_adapter,
    build_cloud_adapters,
    build_job_factory_dispatcher,
    build_local_adapters,
    build_validator,
)
from src.job_factory.dispatcher import JobFactoryDispatcher
from src.job_factory.registry import (
    ClassifierConfig,
    DiscoveryConfig,
    JobType,
    JobTypeRegistry,
    ModelEntry,
    ModelRegistry,
)
from src.llm.adapters.claude_cli import ClaudeCLIAdapter
from src.llm.adapters.ollama import OllamaAdapter
from src.llm.adapters.openai import OpenAIAdapter


# ---- fixtures -------------------------------------------------------------


def _settings(
    *, ollama_enabled: bool = True, openai_key: str = "sk-test",
) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        discord_bot_token="",
        require_allowlist=False,
        openai_api_key=openai_key,
        ollama_enabled=ollama_enabled,
        ollama_base_url="http://localhost:11434",
    )


def _model_registry(
    *,
    local: list[tuple[str, str]] | None = None,
    cloud: list[tuple[str, str]] | None = None,
) -> ModelRegistry:
    return ModelRegistry(
        local=tuple(
            ModelEntry(provider=p, name=n) for p, n in (local or [])
        ),
        cloud=tuple(
            ModelEntry(provider=p, name=n) for p, n in (cloud or [])
        ),
        discovery=DiscoveryConfig(),
    )


def _registry(jts: list[JobType]) -> JobTypeRegistry:
    return JobTypeRegistry(
        job_types={j.name: j for j in jts},
        classifier=ClassifierConfig(fallback_job_type=jts[0].name),
    )


# ---- build_local_adapters -------------------------------------------------


def test_build_local_adapters_returns_ollama_per_entry():
    settings = _settings()
    models = _model_registry(local=[
        ("ollama", "qwen2.5:7b"),
        ("ollama", "qwen2.5:14b"),
    ])
    adapters = build_local_adapters(settings, models)
    assert set(adapters.keys()) == {"ollama/qwen2.5:7b", "ollama/qwen2.5:14b"}
    assert all(isinstance(a, OllamaAdapter) for a in adapters.values())


def test_build_local_adapters_skips_unknown_provider(caplog):
    settings = _settings()
    models = _model_registry(local=[
        ("ollama", "qwen2.5:7b"),
        ("custom_local", "weird-model"),     # unknown
    ])
    adapters = build_local_adapters(settings, models)
    assert "ollama/qwen2.5:7b" in adapters
    assert "custom_local/weird-model" not in adapters


# ---- build_cloud_adapters -------------------------------------------------


def test_build_cloud_adapters_with_openai_key():
    settings = _settings(openai_key="sk-test")
    models = _model_registry(cloud=[
        ("openai", "gpt-4o-mini"),
        ("openai", "gpt-4o"),
    ])
    adapters = build_cloud_adapters(settings, models)
    assert "openai/gpt-4o-mini" in adapters
    assert "openai/gpt-4o" in adapters
    assert all(isinstance(a, OpenAIAdapter) for a in adapters.values())


def test_build_cloud_adapters_skips_openai_when_no_key():
    settings = _settings(openai_key="")
    models = _model_registry(cloud=[("openai", "gpt-4o-mini")])
    adapters = build_cloud_adapters(settings, models)
    assert adapters == {}


def test_build_cloud_adapters_skips_claude_when_no_adapter():
    settings = _settings()
    models = _model_registry(cloud=[("claude_cli", "sonnet")])
    adapters = build_cloud_adapters(settings, models, claude_adapter=None)
    assert adapters == {}


def test_build_cloud_adapters_with_claude_adapter():
    settings = _settings()
    models = _model_registry(cloud=[
        ("claude_cli", "haiku"),
        ("claude_cli", "sonnet"),
    ])
    # Stub claude adapter — only needs to be passable to ClaudeCLIAdapter.
    class _StubClaude:
        async def run(self, **kwargs):
            raise NotImplementedError
    adapters = build_cloud_adapters(
        settings, models, claude_adapter=_StubClaude(),
    )
    assert "claude_cli/haiku" in adapters
    assert "claude_cli/sonnet" in adapters
    assert all(isinstance(a, ClaudeCLIAdapter) for a in adapters.values())


def test_build_cloud_adapters_mixed_providers():
    settings = _settings(openai_key="sk-test")
    models = _model_registry(cloud=[
        ("openai", "gpt-4o-mini"),
        ("claude_cli", "haiku"),
    ])
    class _StubClaude: pass
    adapters = build_cloud_adapters(
        settings, models, claude_adapter=_StubClaude(),
    )
    assert isinstance(adapters["openai/gpt-4o-mini"], OpenAIAdapter)
    assert isinstance(adapters["claude_cli/haiku"], ClaudeCLIAdapter)


# ---- build_classifier_adapter --------------------------------------------


def test_classifier_adapter_built_when_ollama_enabled():
    settings = _settings(ollama_enabled=True)
    jobs = _registry([JobType(name="simple_chat")])
    adapter = build_classifier_adapter(settings, jobs)
    assert adapter is not None
    assert isinstance(adapter, OllamaAdapter)


def test_classifier_adapter_none_when_ollama_disabled():
    settings = _settings(ollama_enabled=False)
    jobs = _registry([JobType(name="simple_chat")])
    adapter = build_classifier_adapter(settings, jobs)
    assert adapter is None


# ---- build_validator ------------------------------------------------------


def test_build_validator_with_judge():
    """default bench_judge_backend=claude_cli falls back to ollama when no
    claude_adapter is supplied; with ollama_enabled=True that yields an
    Ollama-based judge axis."""
    settings = _settings(openai_key="sk-test")
    jobs = _registry([JobType(name="simple_chat")])
    v = build_validator(settings, jobs)
    axis_names = {a.name for a in v.axes}
    assert "length" in axis_names
    assert "structural" in axis_names
    assert "llm_judge" in axis_names


def test_build_validator_no_judge_when_openai_backend_and_no_key():
    """bench_judge_backend='openai' + empty key → judge axis dropped."""
    settings = _settings(openai_key="").model_copy(
        update={"bench_judge_backend": "openai"},
    )
    jobs = _registry([JobType(name="simple_chat")])
    v = build_validator(settings, jobs)
    axis_names = {a.name for a in v.axes}
    assert "llm_judge" not in axis_names


def test_build_validator_no_judge_when_ollama_disabled_and_no_claude():
    """claude_cli backend + no adapter → falls back to ollama; with
    ollama_enabled=False the fallback also returns None and the judge
    axis is dropped silently."""
    settings = _settings(ollama_enabled=False)
    jobs = _registry([JobType(name="simple_chat")])
    v = build_validator(settings, jobs)  # no claude_adapter passed
    axis_names = {a.name for a in v.axes}
    assert "llm_judge" not in axis_names


def test_build_validator_per_job_overrides_present():
    settings = _settings()
    jobs = _registry([JobType(name="simple_chat")])
    v = build_validator(settings, jobs)
    # Spot-check: schedule_logging and code_generation have overrides.
    assert "schedule_logging" in v.per_job_overrides
    assert "code_generation" in v.per_job_overrides
    # schedule_logging should weight structural heavily (24-field JSON).
    sl = v.per_job_overrides["schedule_logging"]
    assert sl["structural"] >= 0.5


# ---- build_job_factory_dispatcher ----------------------------------------


def test_build_dispatcher_end_to_end_with_real_configs(tmp_path):
    """Smoke test: builder loads real checked-in config files and
    returns a usable dispatcher."""
    settings = _settings()
    settings = settings.model_copy(update={
        # Override matrix path to a tmp location so we don't pollute.
        "score_matrix_path": tmp_path / "matrix.json",
    })
    dispatcher = build_job_factory_dispatcher(settings)
    assert isinstance(dispatcher, JobFactoryDispatcher)
    # Local adapters built from real model_registry.yaml.
    assert len(dispatcher._local_adapters) > 0
    # Cloud adapters: openai built (we set a fake key); claude not (no adapter).
    assert any(
        k.startswith("openai/") for k in dispatcher._cloud_adapters
    )
    assert not any(
        k.startswith("claude_cli/") for k in dispatcher._cloud_adapters
    )


def test_build_dispatcher_with_overrides(tmp_path):
    """Override paths point to test-specific configs."""
    import yaml

    job_yaml = tmp_path / "jobs.yaml"
    job_yaml.write_text(yaml.safe_dump({
        "job_types": [{"name": "simple_chat"}],
    }), encoding="utf-8")

    model_yaml = tmp_path / "models.yaml"
    model_yaml.write_text(yaml.safe_dump({
        "local": [{"provider": "ollama", "name": "qwen2.5:7b"}],
    }), encoding="utf-8")

    settings = _settings()
    dispatcher = build_job_factory_dispatcher(
        settings,
        job_factory_yaml=job_yaml,
        model_registry_yaml=model_yaml,
        cloud_policy_yaml=tmp_path / "nope.yaml",  # missing → defaults
        score_matrix_path=tmp_path / "m.json",
    )
    assert isinstance(dispatcher, JobFactoryDispatcher)
    # Only the model we specified is built.
    assert list(dispatcher._local_adapters.keys()) == ["ollama/qwen2.5:7b"]


def test_build_dispatcher_ollama_disabled_classifier_none(tmp_path):
    """ollama_enabled=False → classifier has no LLM fallback."""
    settings = _settings(ollama_enabled=False)
    settings = settings.model_copy(update={
        "score_matrix_path": tmp_path / "m.json",
    })
    dispatcher = build_job_factory_dispatcher(settings)
    # Classifier still works via keyword fast path; LLM stage just None.
    assert dispatcher._classifier._llm is None
