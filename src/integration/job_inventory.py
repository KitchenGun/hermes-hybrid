"""Job Inventory — agent-only runtime view (Phase 8, 2026-05-06).

Phase 8 폐기 후 inventory 의 책임은 단순:

  * scans ``agents/{category}/{name}/SKILL.md`` for AgentEntry
  * exposes lookup methods: ``agents()``, ``agent_by_handle(handle)``,
    ``agents_by_category(category)``
  * 옛 profile/job/skill scan 은 없어짐 (profile 자체가 폐기됨)

class 이름은 호환을 위해 ``JobInventory`` 그대로 유지. 향후
``AgentInventory`` 로 rename 가능. ``profiles()`` / ``jobs()`` /
``skills()`` 는 빈 결과를 반환하는 stub 으로만 남겨 호출 측 호환 유지
(차차 호출자 정리 후 제거).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.agents import AgentEntry, AgentRegistry


class JobInventory:
    """Agent-only inventory wrapper (Phase 8)."""

    def __init__(
        self,
        profiles_root: Path | None = None,
        *,
        skills_root: Path | None = None,
        agents_root: Path | None = None,
        repo_root: Path | None = None,
    ):
        # profiles_root 는 호환을 위해 받지만 사용 X.
        self._profiles_root_compat = (
            Path(profiles_root) if profiles_root is not None else None
        )
        if repo_root is not None:
            self.repo_root = Path(repo_root)
        elif self._profiles_root_compat is not None:
            self.repo_root = self._profiles_root_compat.parent
        else:
            self.repo_root = Path.cwd()
        # Phase 7+: agents/ root holds the 17 sub-agents (6 categories).
        # Default points to ``<repo_root>/agents``.
        self.agents_root = (
            Path(agents_root)
            if agents_root is not None
            else self.repo_root / "agents"
        )

        self._agents: AgentRegistry | None = None

    # ---- legacy stubs (always empty after Phase 8) -------------------

    def profiles(self) -> dict[str, Any]:
        return {}

    def jobs(
        self,
        *,
        profile_id: str | None = None,
        trigger_type: str | None = None,
    ) -> list[Any]:
        return []

    def find_job(self, job_id: str) -> Any | None:
        return None

    def skills(self) -> list[Any]:
        return []

    def skills_for(self, profile_id: str) -> list[Any]:
        return []

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
        return {
            "profile_count": 0,
            "profile_ids": [],
            "job_count": 0,
            "jobs_by_trigger": {
                "cron": 0,
                "on_demand": 0,
                "watcher_event": 0,
                "watcher_poll": 0,
            },
            "skill_count": 0,
            "agent_count": len(self.agents()),
            "agents_by_category": self._agent_registry().summary(),
        }


__all__ = ["JobInventory"]
