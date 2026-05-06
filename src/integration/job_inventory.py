"""Job Inventory — runtime view of profiles / jobs / skills.

The diagram pictures Job Inventory as a sibling of Intent Router /
Policy Gate / Session Importer. Its role: when the master needs to
*decide* which profile + job + skill to invoke for a free-text request,
it asks the inventory for the available specs.

Today the inventory is read-only:
  * scans ``profiles/*/config.yaml`` for ProfileSpec
  * scans ``profiles/*/{cron,on_demand,watchers}/**.yaml`` for JobSpec
  * cross-references ``profiles/*/skills/**/SKILL.md`` (current
    profile-local layout) and the global skills/ root (for the future
    6-category agent layout)
  * exposes lookup methods: ``profiles()``, ``jobs(profile_id=...)``,
    ``find_job(job_id)``, ``skills_for(profile_id)``

Mutations (registering a new job at runtime, etc.) are out of scope —
the existing ``hermes`` CLI cron register flow stays the source of
truth for that.

Caching: a single in-process scan per JobInventory instance (no
TTL refresh). Master orchestrator builds an inventory at startup; if
profile yaml changes mid-run the operator restarts the bot — same
contract as ProfileLoader.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from src.agents import AgentEntry, AgentRegistry
from src.core.skill_library import SkillEntry, SkillLibrary


_JOB_TRIGGER_DIRS = ("cron", "on_demand", "watchers")


class ProfileSpec(BaseModel):
    """One row of profile metadata — what the master needs to know
    to compose a request for this profile."""

    profile_id: str
    soul_excerpt: str = ""           # first ~10 lines of SOUL.md
    model_provider: str | None = None
    model_name: str | None = None
    tier_prefer: str | None = None
    tier_max: str | None = None
    budget_cap_usd_per_day: float | None = None
    approvals_mode: str | None = None
    auto_load_skills: list[str] = Field(default_factory=list)
    has_cron: bool = False
    has_on_demand: bool = False
    has_watchers: bool = False
    has_memories: bool = False


class JobSpec(BaseModel):
    """One row of job metadata — yaml-derived."""

    job_id: str                       # yaml `name:` field
    profile_id: str
    trigger_type: str                 # cron / on_demand / watcher_event / watcher_poll
    category: str | None = None       # read / write / analyze / monitor / watcher
    description: str = ""
    schedule: str | None = None       # cron expr or polling interval label
    tier_max: str | None = None
    tier_prefer: str | None = None
    budget_usd_per_run_cap: float | None = None
    skills: list[str] = Field(default_factory=list)
    delivery_channel: str | None = None
    delivery_target_env: str | None = None
    requires_confirmation: bool = False
    yaml_path: str = ""               # repo-relative POSIX
    prompt_excerpt: str = ""          # first ~5 lines of prompt for context


class JobInventory:
    """Read-only profile/job/skill view for the orchestrator."""

    def __init__(
        self,
        profiles_root: Path,
        *,
        skills_root: Path | None = None,
        agents_root: Path | None = None,
        repo_root: Path | None = None,
    ):
        self.profiles_root = Path(profiles_root)
        self.repo_root = Path(repo_root) if repo_root else self.profiles_root.parent
        # When skills_root is provided we scan that *in addition to* the
        # profile-local ones. Today nothing is materialized there yet
        # (Phase 7 work), but the interface accepts it so the master
        # doesn't change when the 6-category agent layout lands.
        self.skills_root = Path(skills_root) if skills_root else None
        # Phase 7: agents/ root holds the 17 sub-agents (6 categories).
        # Default points to ``<repo_root>/agents`` so the master can
        # consult them by handle (@coder etc.).
        self.agents_root = (
            Path(agents_root)
            if agents_root is not None
            else self.repo_root / "agents"
        )

        self._profiles: dict[str, ProfileSpec] | None = None
        self._jobs: dict[str, JobSpec] | None = None
        self._skills: list[SkillEntry] | None = None
        self._agents: AgentRegistry | None = None

    # ---- public lookups ---------------------------------------------

    def profiles(self) -> dict[str, ProfileSpec]:
        if self._profiles is None:
            self._scan()
        assert self._profiles is not None
        return self._profiles

    def jobs(
        self,
        *,
        profile_id: str | None = None,
        trigger_type: str | None = None,
    ) -> list[JobSpec]:
        if self._jobs is None:
            self._scan()
        assert self._jobs is not None
        out = list(self._jobs.values())
        if profile_id is not None:
            out = [j for j in out if j.profile_id == profile_id]
        if trigger_type is not None:
            out = [j for j in out if j.trigger_type == trigger_type]
        return out

    def find_job(self, job_id: str) -> JobSpec | None:
        if self._jobs is None:
            self._scan()
        assert self._jobs is not None
        return self._jobs.get(job_id)

    def skills(self) -> list[SkillEntry]:
        if self._skills is None:
            self._scan()
        assert self._skills is not None
        return list(self._skills)

    def skills_for(self, profile_id: str) -> list[SkillEntry]:
        return [s for s in self.skills() if s.profile == profile_id]

    # ---- Phase 7 — agents (6 categories / 17 handles) -----------------

    def agents(self) -> list[AgentEntry]:
        return self._agent_registry().all()

    def agent_by_handle(self, handle: str) -> AgentEntry | None:
        return self._agent_registry().by_handle(handle)

    def agents_by_category(self, category: str) -> list[AgentEntry]:
        return self._agent_registry().by_category(category)

    def _agent_registry(self) -> AgentRegistry:
        if self._agents is None:
            self._agents = AgentRegistry(
                self.agents_root, repo_root=self.repo_root
            )
        return self._agents

    # ---- summary ----------------------------------------------------

    def summary(self) -> dict[str, Any]:
        ps = self.profiles()
        js = self.jobs()
        return {
            "profile_count": len(ps),
            "profile_ids": sorted(ps.keys()),
            "job_count": len(js),
            "jobs_by_trigger": {
                t: sum(1 for j in js if j.trigger_type == t)
                for t in (
                    "cron",
                    "on_demand",
                    "watcher_event",
                    "watcher_poll",
                )
            },
            "skill_count": len(self.skills()),
            "agent_count": len(self.agents()),
            "agents_by_category": self._agent_registry().summary(),
        }

    # ---- internal scan ----------------------------------------------

    def _scan(self) -> None:
        self._profiles = {}
        self._jobs = {}

        if not self.profiles_root.exists():
            self._skills = []
            return

        for profile_dir in sorted(p for p in self.profiles_root.iterdir() if p.is_dir()):
            spec = self._scan_profile(profile_dir)
            if spec is None:
                continue
            self._profiles[spec.profile_id] = spec
            for job in self._scan_jobs(profile_dir):
                # First-write-wins on duplicate job_id (yaml authors
                # should keep names unique across profiles, but we
                # guard rather than overwrite silently).
                self._jobs.setdefault(job.job_id, job)

        # Skills: profile-local layout via SkillLibrary.
        try:
            library = SkillLibrary(
                self.profiles_root, repo_root=self.repo_root
            )
            self._skills = library.scan()
        except Exception:  # noqa: BLE001
            self._skills = []

        # Future: also scan self.skills_root for the 6-category agent
        # layout. Today that root is None / unused.

    def _scan_profile(self, profile_dir: Path) -> ProfileSpec | None:
        config_path = profile_dir / "config.yaml"
        if not config_path.exists():
            return None
        try:
            cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            return None
        if not isinstance(cfg, dict):
            return None

        soul_excerpt = ""
        soul_path = profile_dir / "SOUL.md"
        if soul_path.exists():
            try:
                lines = soul_path.read_text(encoding="utf-8").splitlines()
                soul_excerpt = "\n".join(lines[:10]).strip()
            except OSError:
                pass

        model = cfg.get("model") or {}
        tier_policy = (
            (cfg.get("x-hermes-hybrid") or {}).get("tier_policy") or {}
        )
        budget = (cfg.get("x-hermes-hybrid") or {}).get("budget") or {}
        approvals = cfg.get("approvals") or {}
        skills_block = cfg.get("skills") or {}

        return ProfileSpec(
            profile_id=profile_dir.name,
            soul_excerpt=soul_excerpt,
            model_provider=str(model.get("provider") or "") or None,
            model_name=str(model.get("model") or "") or None,
            tier_prefer=tier_policy.get("prefer_tier"),
            tier_max=tier_policy.get("max_tier"),
            budget_cap_usd_per_day=(
                float(budget["cap_usd_per_day"])
                if "cap_usd_per_day" in budget
                else None
            ),
            approvals_mode=approvals.get("mode"),
            auto_load_skills=list(skills_block.get("auto_load") or []),
            has_cron=(profile_dir / "cron").exists(),
            has_on_demand=(profile_dir / "on_demand").exists(),
            has_watchers=(profile_dir / "watchers").exists(),
            has_memories=(profile_dir / "memories").exists(),
        )

    def _scan_jobs(self, profile_dir: Path) -> list[JobSpec]:
        out: list[JobSpec] = []
        for sub in _JOB_TRIGGER_DIRS:
            tdir = profile_dir / sub
            if not tdir.exists():
                continue
            for yaml_path in sorted(tdir.rglob("*.yaml")):
                spec = self._scan_job_yaml(
                    yaml_path,
                    profile_id=profile_dir.name,
                    container=sub,
                )
                if spec is not None:
                    out.append(spec)
        return out

    def _scan_job_yaml(
        self,
        yaml_path: Path,
        *,
        profile_id: str,
        container: str,
    ) -> JobSpec | None:
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            return None

        trigger = data.get("trigger") or {}
        ttype = str(trigger.get("type") or container)
        # watcher 안에서 event vs poll 분리 — 다이어그램 카운트와 일치.
        if ttype == "watcher":
            src = trigger.get("source")
            if isinstance(src, dict) and src.get("type") in (
                "rss_poll", "mail_poll", "interval"
            ):
                ttype = "watcher_poll"
            elif "interval_seconds" in trigger:
                ttype = "watcher_poll"
            else:
                ttype = "watcher_event"

        tier = data.get("tier") or {}
        budget = data.get("budget") or {}
        delivery = data.get("delivery") or {}
        safety = data.get("safety") or {}

        prompt = data.get("prompt") or ""
        prompt_excerpt = "\n".join(str(prompt).splitlines()[:5]).strip()

        try:
            rel = yaml_path.resolve().relative_to(self.repo_root.resolve())
            posix = rel.as_posix()
        except ValueError:
            posix = yaml_path.as_posix()

        return JobSpec(
            job_id=name.strip(),
            profile_id=profile_id,
            trigger_type=ttype,
            category=data.get("category"),
            description=str(data.get("description") or "").strip(),
            schedule=str(trigger.get("schedule") or "") or None,
            tier_max=tier.get("max"),
            tier_prefer=tier.get("prefer"),
            budget_usd_per_run_cap=(
                float(budget["usd_per_run_cap"])
                if "usd_per_run_cap" in budget
                else None
            ),
            skills=list(data.get("skills") or []),
            delivery_channel=delivery.get("channel"),
            delivery_target_env=delivery.get("target_env"),
            requires_confirmation=bool(safety.get("requires_confirmation", False)),
            yaml_path=posix,
            prompt_excerpt=prompt_excerpt,
        )


__all__ = ["JobInventory", "JobSpec", "ProfileSpec"]
