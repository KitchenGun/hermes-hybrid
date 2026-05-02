"""Job_type and model registries — config-driven, no prior matrix.

Two registries, both backed by YAML:

  * :class:`JobTypeRegistry`  loaded from ``config/job_factory.yaml``.
    Each :class:`JobType` carries POLICY booleans (cloud_allowed,
    claude_allowed, requires_user_approval, max_attempts, etc.) — never
    "preferred models" or "fallback chain". Empirical routing is the
    point of v2.

  * :class:`ModelRegistry`    loaded from ``config/model_registry.yaml``.
    Each entry is a stable (provider, name) pair plus optional cost
    metadata (cloud only). No ``role`` labels — those are prior. The
    :class:`EpsilonGreedySelector` is the only mechanism that picks a
    model.

The registries are read-only after load. Re-load by calling
:meth:`JobTypeRegistry.from_yaml` again (e.g., on SIGHUP) — concurrency
is the orchestrator's job.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


# ---- exceptions -----------------------------------------------------------


class RegistryConfigError(ValueError):
    """Raised on malformed config (missing required field, invalid type)."""


# ---- JobType --------------------------------------------------------------


@dataclass(frozen=True)
class JobType:
    """Policy-only definition of a job_type.

    No model preferences here — that's the bandit's job. This dataclass
    only carries the *boolean* policy bits needed to gate cloud/Claude
    use and to bound dispatcher behavior (retries, timeout, threshold).

    Attributes:
        name: Stable identifier — used as ScoreMatrix's primary key.
        keywords_ko: Korean keywords for fast-path classification.
        keywords_en: English keywords for fast-path classification.
        max_attempts: Cap on local-model retries before escalating to
            cloud. The bandit picks a different local model per attempt.
        quality_threshold: Validator score (0–100) above which the
            dispatcher considers the response "good enough" to return.
        cloud_allowed: If False, OpenAI escalation is blocked entirely.
        claude_allowed: If False, Claude CLI escalation is blocked.
        requires_user_approval: If True, every cloud/Claude step needs
            explicit user approval (Discord buttons via existing HITL).
        timeout_seconds: Per-attempt LLM timeout.
    """

    name: str
    keywords_ko: tuple[str, ...] = ()
    keywords_en: tuple[str, ...] = ()
    max_attempts: int = 2
    quality_threshold: int = 60
    cloud_allowed: bool = True
    claude_allowed: bool = False
    requires_user_approval: bool = False
    timeout_seconds: int = 60


@dataclass(frozen=True)
class ClassifierConfig:
    """Classifier-level config (YAML 'classifier' block)."""

    fast_keyword_path: bool = True
    llm_model: str = "qwen2.5:3b-instruct"
    llm_timeout_seconds: int = 5
    fallback_job_type: str = "simple_chat"


@dataclass(frozen=True)
class JobTypeRegistry:
    """All known job_types + the classifier config.

    Lookup-only — :meth:`get` raises KeyError for unknown names so
    typos in config or stale dispatcher code surface loudly.
    """

    job_types: dict[str, JobType]
    classifier: ClassifierConfig

    def get(self, name: str) -> JobType:
        if name not in self.job_types:
            raise KeyError(f"unknown job_type: {name!r}")
        return self.job_types[name]

    def has(self, name: str) -> bool:
        return name in self.job_types

    def names(self) -> list[str]:
        return sorted(self.job_types.keys())

    @classmethod
    def from_yaml(cls, path: Path) -> "JobTypeRegistry":
        data = _load_yaml_dict(path)

        # classifier block — optional, all fields default.
        cls_raw = data.get("classifier", {})
        if not isinstance(cls_raw, dict):
            raise RegistryConfigError(
                f"'classifier' must be a dict in {path}"
            )
        classifier = ClassifierConfig(
            fast_keyword_path=bool(
                cls_raw.get("fast_keyword_path", True)
            ),
            llm_model=str(
                cls_raw.get("llm_model", "qwen2.5:3b-instruct")
            ),
            llm_timeout_seconds=int(
                cls_raw.get("llm_timeout_seconds", 5)
            ),
            fallback_job_type=str(
                cls_raw.get("fallback_job_type", "simple_chat")
            ),
        )

        # job_types block — required, list of dicts.
        raw_list = data.get("job_types", [])
        if not isinstance(raw_list, list):
            raise RegistryConfigError(
                f"'job_types' must be a list in {path}"
            )
        if not raw_list:
            raise RegistryConfigError(
                f"'job_types' must be non-empty in {path}"
            )

        job_types: dict[str, JobType] = {}
        for i, raw in enumerate(raw_list):
            if not isinstance(raw, dict):
                raise RegistryConfigError(
                    f"job_types[{i}] must be a dict in {path}"
                )
            jt = _parse_job_type(raw, where=f"job_types[{i}]", path=path)
            if jt.name in job_types:
                raise RegistryConfigError(
                    f"duplicate job_type name {jt.name!r} in {path}"
                )
            job_types[jt.name] = jt

        # fallback_job_type must exist in the registry.
        if classifier.fallback_job_type not in job_types:
            raise RegistryConfigError(
                f"classifier.fallback_job_type {classifier.fallback_job_type!r} "
                f"not defined in job_types"
            )

        return cls(job_types=job_types, classifier=classifier)


def _parse_job_type(
    raw: dict[str, Any], *, where: str, path: Path,
) -> JobType:
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise RegistryConfigError(f"{where} missing 'name' (in {path})")

    def _strs(key: str) -> tuple[str, ...]:
        val = raw.get(key, [])
        if not isinstance(val, list):
            raise RegistryConfigError(
                f"{where}.{key} must be a list of strings (in {path})"
            )
        return tuple(str(v) for v in val)

    return JobType(
        name=str(name),
        keywords_ko=_strs("keywords_ko"),
        keywords_en=_strs("keywords_en"),
        max_attempts=int(raw.get("max_attempts", 2)),
        quality_threshold=int(raw.get("quality_threshold", 60)),
        cloud_allowed=bool(raw.get("cloud_allowed", True)),
        claude_allowed=bool(raw.get("claude_allowed", False)),
        requires_user_approval=bool(
            raw.get("requires_user_approval", False)
        ),
        timeout_seconds=int(raw.get("timeout_seconds", 60)),
    )


# ---- ModelRegistry --------------------------------------------------------


@dataclass(frozen=True)
class ModelEntry:
    """One entry in the model registry.

    The (provider, name) tuple doubles as the ScoreMatrix arm key
    (``f"{provider}/{name}"``) so the registry is the source of truth
    for "what arms exist".

    For cloud entries, ``cost_input_per_1m`` / ``cost_output_per_1m``
    are populated from the YAML; the dispatcher uses them for cost
    estimation in the cloud-policy gate (Phase 6). Local entries leave
    them at 0.
    """

    provider: str
    name: str
    cost_input_per_1m: float = 0.0
    cost_output_per_1m: float = 0.0

    @property
    def matrix_key(self) -> str:
        """Stable key used by ScoreMatrix (provider/name)."""
        return f"{self.provider}/{self.name}"


@dataclass(frozen=True)
class DiscoveryConfig:
    ollama_poll_interval_seconds: int = 300
    auto_bench_on_new_model: bool = True


@dataclass(frozen=True)
class ModelRegistry:
    """All known model arms + discovery settings.

    Local entries are read from ``local:`` block; cloud from ``cloud:``.
    The dispatcher constructs adapters as it needs them; the registry
    only holds metadata.
    """

    local: tuple[ModelEntry, ...]
    cloud: tuple[ModelEntry, ...]
    discovery: DiscoveryConfig

    def all_entries(self) -> list[ModelEntry]:
        return list(self.local) + list(self.cloud)

    def local_keys(self) -> list[str]:
        return [e.matrix_key for e in self.local]

    def cloud_keys(self) -> list[str]:
        return [e.matrix_key for e in self.cloud]

    def find(self, matrix_key: str) -> ModelEntry | None:
        for e in self.all_entries():
            if e.matrix_key == matrix_key:
                return e
        return None

    @classmethod
    def from_yaml(cls, path: Path) -> "ModelRegistry":
        data = _load_yaml_dict(path)

        local = tuple(_parse_model_list(
            data.get("local", []), where="local", path=path,
        ))
        cloud = tuple(_parse_model_list(
            data.get("cloud", []), where="cloud", path=path,
        ))

        # Cross-block uniqueness on matrix_key — prevents accidental
        # collision between e.g., a local 'gpt-4o-mini' and a cloud one.
        seen: set[str] = set()
        for e in (*local, *cloud):
            if e.matrix_key in seen:
                raise RegistryConfigError(
                    f"duplicate matrix_key {e.matrix_key!r} in {path}"
                )
            seen.add(e.matrix_key)

        disc_raw = data.get("discovery", {})
        if not isinstance(disc_raw, dict):
            raise RegistryConfigError(
                f"'discovery' must be a dict in {path}"
            )
        discovery = DiscoveryConfig(
            ollama_poll_interval_seconds=int(
                disc_raw.get("ollama_poll_interval_seconds", 300)
            ),
            auto_bench_on_new_model=bool(
                disc_raw.get("auto_bench_on_new_model", True)
            ),
        )
        return cls(local=local, cloud=cloud, discovery=discovery)


def _parse_model_list(
    raw: Any, *, where: str, path: Path,
) -> list[ModelEntry]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RegistryConfigError(
            f"'{where}' must be a list in {path}"
        )
    out: list[ModelEntry] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RegistryConfigError(
                f"{where}[{i}] must be a dict in {path}"
            )
        provider = item.get("provider")
        name = item.get("name")
        if not provider or not isinstance(provider, str):
            raise RegistryConfigError(
                f"{where}[{i}] missing 'provider' string in {path}"
            )
        if not name or not isinstance(name, str):
            raise RegistryConfigError(
                f"{where}[{i}] missing 'name' string in {path}"
            )
        out.append(ModelEntry(
            provider=str(provider),
            name=str(name),
            cost_input_per_1m=float(item.get("cost_input_per_1m", 0.0)),
            cost_output_per_1m=float(item.get("cost_output_per_1m", 0.0)),
        ))
    return out


# ---- helper ---------------------------------------------------------------


def _load_yaml_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RegistryConfigError(f"file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise RegistryConfigError(f"YAML parse error in {path}: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RegistryConfigError(
            f"expected dict at top of {path}, got {type(data).__name__}"
        )
    return data
