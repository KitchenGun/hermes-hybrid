"""Phase 7 — sub-agent registry (6 categories / 17 agents).

The 6-category taxonomy is the user's stated direction (2026-05-06):

    RESEARCH         PLANNING         IMPLEMENTATION
    @finder          @architect       @coder
    @analyst         @planner         @editor
    @researcher                       @fixer
                                      @refactorer

    QUALITY          DOCUMENTATION    INFRASTRUCTURE
    @reviewer        @documenter      @devops
    @tester          @commenter       @optimizer
    @debugger
    @security

Each agent is a SKILL.md under ``agents/{category}/{name}/SKILL.md``.
The Hermes Master Orchestrator consults this registry to:
  1. resolve ``@coder`` / ``@reviewer`` mentions in user input
  2. inject the relevant agent's SKILL.md as a system snippet when the
     master decides a sub-agent is the right tool

This module is intentionally read-only and stateless — same shape as
:class:`SkillLibrary`. Phase 8 adds invocation (parallel delegation).
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel, Field

from src.core.skill_library import _entry_from_skill_md
from src.core import SkillEntry


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_AGENTS_ROOT = _REPO_ROOT / "agents"


_CATEGORIES = (
    "research",
    "planning",
    "implementation",
    "quality",
    "documentation",
    "infrastructure",
)


class AgentEntry(BaseModel):
    """One sub-agent — projection over its SKILL.md frontmatter."""

    handle: str                       # "@coder"
    name: str                         # "coder"
    category: str                     # "implementation"
    role: str = ""
    description: str = ""
    when_to_use: list[str] = Field(default_factory=list)
    not_for: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    skill_md_path: str = ""           # repo-relative POSIX
    primary_tools: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class AgentRegistry:
    """Read-only loader for ``agents/{category}/{name}/SKILL.md``."""

    def __init__(
        self,
        agents_root: Path | None = None,
        *,
        repo_root: Path | None = None,
    ):
        self.agents_root = (
            Path(agents_root) if agents_root is not None else _AGENTS_ROOT
        )
        self.repo_root = (
            Path(repo_root) if repo_root is not None else self.agents_root.parent
        )
        self._entries: list[AgentEntry] | None = None
        self._by_handle: dict[str, AgentEntry] | None = None

    # ---- public lookups ---------------------------------------------

    def all(self) -> list[AgentEntry]:
        if self._entries is None:
            self._scan()
        assert self._entries is not None
        return list(self._entries)

    def by_handle(self, handle: str) -> AgentEntry | None:
        if self._by_handle is None:
            self._scan()
        assert self._by_handle is not None
        norm = self._normalize_handle(handle)
        return self._by_handle.get(norm)

    def by_category(self, category: str) -> list[AgentEntry]:
        return [e for e in self.all() if e.category == category]

    def categories(self) -> dict[str, list[AgentEntry]]:
        out: dict[str, list[AgentEntry]] = defaultdict(list)
        for e in self.all():
            out[e.category].append(e)
        return dict(out)

    def summary(self) -> dict[str, int]:
        return {cat: len(self.by_category(cat)) for cat in _CATEGORIES}

    # ---- scan ------------------------------------------------------

    def _scan(self) -> None:
        self._entries = []
        self._by_handle = {}
        if not self.agents_root.exists():
            return

        for cat_dir in sorted(p for p in self.agents_root.iterdir() if p.is_dir()):
            category = cat_dir.name
            for agent_dir in sorted(
                p for p in cat_dir.iterdir() if p.is_dir()
            ):
                md = agent_dir / "SKILL.md"
                if not md.exists():
                    continue
                entry = self._build_entry(md, category=category)
                if entry is None:
                    continue
                self._entries.append(entry)
                self._by_handle[entry.handle.lower()] = entry

    def _build_entry(
        self, md_path: Path, *, category: str
    ) -> AgentEntry | None:
        # Reuse the SkillLibrary parser for the YAML frontmatter so the
        # two registries see the same fields the same way.
        skill = _entry_from_skill_md(
            md_path,
            profiles_root=self.agents_root.parent,  # bogus but unused
            repo_root=self.repo_root,
        )
        # SkillLibrary's path heuristic expects ``profiles/{p}/skills/...``,
        # which agents/ won't satisfy. So parse the agent-specific bits
        # directly from the frontmatter instead.
        import yaml
        text = md_path.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("---\n"):
            return None
        end = text.find("\n---", 4)
        if end == -1:
            return None
        try:
            fm = yaml.safe_load(text[4:end]) or {}
        except yaml.YAMLError:
            return None
        if not isinstance(fm, dict):
            return None

        name = str(fm.get("name") or md_path.parent.name).strip()
        handle = str(fm.get("agent_handle") or f"@{name}").strip()
        if not handle.startswith("@"):
            handle = f"@{handle}"

        try:
            rel = md_path.resolve().relative_to(self.repo_root.resolve())
            skill_md_path = rel.as_posix()
        except ValueError:
            skill_md_path = md_path.as_posix()

        metadata = (fm.get("metadata") or {}) if isinstance(fm.get("metadata"), dict) else {}
        hermes_md = (metadata.get("hermes") or {}) if isinstance(metadata.get("hermes"), dict) else {}

        return AgentEntry(
            handle=handle,
            name=name,
            category=str(fm.get("category") or category).lower().strip(),
            role=str(fm.get("role") or "").strip(),
            description=str(fm.get("description") or "").strip(),
            when_to_use=_as_str_list(fm.get("when_to_use")),
            not_for=_as_str_list(fm.get("not_for")),
            inputs=_as_str_list(fm.get("inputs")),
            outputs=_as_str_list(fm.get("outputs")),
            skill_md_path=skill_md_path,
            primary_tools=_as_str_list(hermes_md.get("primary_tools")),
            tags=_as_str_list(hermes_md.get("tags")),
        )

    @staticmethod
    def _normalize_handle(handle: str) -> str:
        h = (handle or "").strip().lower()
        if not h:
            return ""
        if not h.startswith("@"):
            h = "@" + h
        return h


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if v is not None]


__all__ = ["AgentEntry", "AgentRegistry", "_CATEGORIES"]
