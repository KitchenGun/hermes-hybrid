"""Profile YAML metadata loader — HITL safety section lookups.

Focused on ``on_demand/<job>.yaml`` files: the orchestrator asks
"does this job require confirmation?" and this loader answers in O(1)
from an in-memory cache refreshed every 30s. Parsing/IO mirrors
:class:`src.job_factory.factory.JobFactory._load_trigger_patterns`.

Public surface:
  - ``get_job_safety(profile_id, job_name)`` → ``JobSafety`` or ``None``
  - ``get_job_meta(profile_id, job_name)`` → full ``JobMeta`` (includes
    safety + category + delivery hints used to build the HITL preview)

The cache is deliberately coarse: a single timestamp per process. Profiles
change rarely and a stale 30s view is safe — worst case the user sees one
turn without confirmation after editing the YAML, which is indistinguishable
from editing it mid-turn.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.obs import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class JobSafety:
    requires_confirmation: bool = False
    idempotent: bool = True
    max_retries: int = 0
    rollback_on_failure: bool = False


@dataclass(frozen=True)
class JobMeta:
    profile_id: str
    job_name: str
    category: str = ""            # "read" | "write" | "compose" | ...
    description: str = ""
    safety: JobSafety = field(default_factory=JobSafety)
    # Raw YAML dict for callers needing fields we didn't model (e.g. prompt).
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WatcherMeta:
    """Subset of a watcher YAML that the runtime needs to schedule polls.

    The full YAML is kept under ``raw`` for callers that need fields we
    haven't modeled yet (e.g., per-watcher tier hints).
    """

    profile_id: str
    name: str
    description: str = ""
    interval_seconds: int = 300
    source_type: str = ""        # "mail_poll" | "rss_poll" | "internal.*"
    source: dict[str, Any] = field(default_factory=dict)
    skills: tuple[str, ...] = ()
    delivery: dict[str, Any] = field(default_factory=dict)
    prompt: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class ProfileLoader:
    """30s TTL cache over every profile's ``on_demand/*.yaml`` and
    ``watchers/*.yaml`` files. Cron jobs are still owned by the Hermes
    CLI scheduler and are not loaded here.
    """

    def __init__(self, profiles_dir: Path, cache_ttl_seconds: float = 30.0):
        self.profiles_dir = Path(profiles_dir)
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[tuple[str, str], JobMeta] = {}
        self._watchers: dict[tuple[str, str], WatcherMeta] = {}
        self._cached_at: float = 0.0

    def get_job_meta(self, profile_id: str, job_name: str) -> JobMeta | None:
        self._ensure_fresh()
        return self._cache.get((profile_id, job_name))

    def get_job_safety(self, profile_id: str, job_name: str) -> JobSafety | None:
        meta = self.get_job_meta(profile_id, job_name)
        return meta.safety if meta is not None else None

    def requires_confirmation(self, profile_id: str, job_name: str) -> bool:
        safety = self.get_job_safety(profile_id, job_name)
        return bool(safety and safety.requires_confirmation)

    def invalidate(self) -> None:
        self._cached_at = 0.0

    def iter_watchers(self) -> list[WatcherMeta]:
        self._ensure_fresh()
        return list(self._watchers.values())

    def _ensure_fresh(self) -> None:
        now = time.monotonic()
        if self._cache and (now - self._cached_at) <= self.cache_ttl_seconds:
            return
        self._cache = self._build_cache()
        self._watchers = self._build_watcher_cache()
        self._cached_at = now

    def _build_watcher_cache(self) -> dict[tuple[str, str], WatcherMeta]:
        out: dict[tuple[str, str], WatcherMeta] = {}
        if not self.profiles_dir.exists():
            return out
        for pdir in sorted(self.profiles_dir.iterdir()):
            if not pdir.is_dir() or not (pdir / "config.yaml").exists():
                continue
            wdir = pdir / "watchers"
            if not wdir.exists():
                continue
            for yml in sorted(wdir.glob("*.yaml")):
                try:
                    data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
                except (OSError, yaml.YAMLError) as e:
                    log.info(
                        "profile_loader.watcher_yaml_failed",
                        path=str(yml),
                        err=str(e),
                    )
                    continue
                if not isinstance(data, dict):
                    continue
                trigger = data.get("trigger") or {}
                if str(trigger.get("type") or "").lower() != "watcher":
                    continue
                source = trigger.get("source") or {}
                if isinstance(source, str):
                    source_type = source
                    source = {"type": source}
                else:
                    source_type = str(source.get("type") or "")
                interval = trigger.get("interval_seconds") or source.get("interval_seconds") or 0
                try:
                    interval_int = int(interval) if interval else 0
                except (TypeError, ValueError):
                    interval_int = 0
                skills_raw = data.get("skills") or []
                if not isinstance(skills_raw, list):
                    skills_raw = []
                name = str(data.get("name") or yml.stem)
                out[(pdir.name, name)] = WatcherMeta(
                    profile_id=pdir.name,
                    name=name,
                    description=str(data.get("description") or ""),
                    interval_seconds=interval_int,
                    source_type=source_type,
                    source=dict(source) if isinstance(source, dict) else {},
                    skills=tuple(str(s) for s in skills_raw),
                    delivery=dict(data.get("delivery") or {}),
                    prompt=str(data.get("prompt") or "").strip(),
                    raw=data,
                )
        return out

    def _build_cache(self) -> dict[tuple[str, str], JobMeta]:
        out: dict[tuple[str, str], JobMeta] = {}
        if not self.profiles_dir.exists():
            return out
        for pdir in sorted(self.profiles_dir.iterdir()):
            if not pdir.is_dir() or not (pdir / "config.yaml").exists():
                continue
            od_dir = pdir / "on_demand"
            if not od_dir.exists():
                continue
            for yml in sorted(od_dir.glob("*.yaml")):
                try:
                    data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
                except (OSError, yaml.YAMLError) as e:
                    log.info(
                        "profile_loader.yaml_failed",
                        path=str(yml),
                        err=str(e),
                    )
                    continue
                if not isinstance(data, dict):
                    continue
                job_name = str(data.get("name") or yml.stem)
                safety_raw = data.get("safety") or {}
                safety = JobSafety(
                    requires_confirmation=bool(
                        safety_raw.get("requires_confirmation", False)
                    ),
                    idempotent=bool(safety_raw.get("idempotent", True)),
                    max_retries=int(safety_raw.get("max_retries", 0) or 0),
                    rollback_on_failure=bool(
                        safety_raw.get("rollback_on_failure", False)
                    ),
                )
                out[(pdir.name, job_name)] = JobMeta(
                    profile_id=pdir.name,
                    job_name=job_name,
                    category=str(data.get("category") or ""),
                    description=str(data.get("description") or ""),
                    safety=safety,
                    raw=data,
                )
        return out
