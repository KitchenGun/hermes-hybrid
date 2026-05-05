"""SkillLibrary — index of profile-level SKILL.md files.

The registry is the runtime / curator entry point for two questions:
  1. "What skills exist across profiles right now?"
  2. "How well is each one performing?"  (filled in later by the
     curator job once ExperienceLog data accrues)

Today the index is a static snapshot of frontmatter from every
``profiles/*/skills/**/SKILL.md`` plus stub fields for run statistics.
The Reflection / Curator jobs (P1.5 / P5) will mutate the stat fields
as data flows in.

Design choices
--------------
* The library lives in ``src/core`` (where the growth-loop primitives
  go) but the *output* file lives at the repo root (``skills/registry.yaml``)
  so it can be diffed in PRs and reviewed without diving into ``src``.
* Class is **read-only / write-once** per call — no in-process cache.
  Curator job will write its own registry; the build CLI is for static
  scans.
* Frontmatter is parsed directly (not via python-frontmatter) to avoid a
  new dependency. The repo's pyyaml is already pulled in elsewhere.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


_FM_HEAD = "---\n"
# Match closing ``---`` that's on its own line. Frontmatter is the chunk
# between the first occurrence of ``---\n`` and the next ``---\n``.
_FM_TAIL_RE = re.compile(r"\n---\s*\n", re.MULTILINE)


class SkillEntry(BaseModel):
    """One skill row in the registry."""

    id: str                                 # "{profile}/{category}/{name}"
    profile: str
    category: str
    name: str

    description: str = ""
    version: str = ""
    skill_md_path: str = ""                 # repo-relative POSIX path
    tags: list[str] = Field(default_factory=list)
    requires_toolsets: list[str] = Field(default_factory=list)
    required_env_vars: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)

    # Stats — filled by curator/reflection. Defaults for first scan.
    runs: int = 0
    successes: int = 0
    failure_rate: float | None = None
    last_used: str | None = None

    # Lifecycle
    status: str = "active"                  # active | shadow | archived
    source: str = "hand-written"            # hand-written | auto-promoted
    promoted_at: str | None = None
    promoted_by: str | None = None


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Extract the YAML frontmatter dict from a SKILL.md file's text.

    Returns ``{}`` when the file has no frontmatter at all — many
    hand-written SKILL.md files do, and the scanner should still index
    them with derived defaults.
    """
    if not text.startswith(_FM_HEAD):
        return {}
    after_head = text[len(_FM_HEAD):]
    m = _FM_TAIL_RE.search(after_head)
    if m is None:
        return {}
    raw = after_head[: m.start()]
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _entry_from_skill_md(
    md_path: Path,
    *,
    profiles_root: Path,
    repo_root: Path,
) -> SkillEntry | None:
    """Build one SkillEntry from a SKILL.md path.

    Path layout assumed:
        profiles_root / {profile} / skills / {category} / {name} / SKILL.md
    Anything that doesn't match returns ``None`` so callers can skip
    quietly (rather than raising on unfamiliar layouts).
    """
    rel = md_path.relative_to(profiles_root)
    parts = rel.parts
    # Expect: ('{profile}', 'skills', '{category}', '{name}', 'SKILL.md')
    if len(parts) < 5 or parts[1] != "skills" or parts[-1] != "SKILL.md":
        return None
    profile = parts[0]
    category = parts[2]
    name = parts[3]

    text = md_path.read_text(encoding="utf-8", errors="replace")
    fm = _parse_frontmatter(text)
    metadata = (fm.get("metadata") or {}) if isinstance(fm.get("metadata"), dict) else {}
    hermes_md = metadata.get("hermes") or {} if isinstance(metadata.get("hermes"), dict) else {}

    tags = hermes_md.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    requires_toolsets = hermes_md.get("requires_toolsets") or []
    if not isinstance(requires_toolsets, list):
        requires_toolsets = []

    env_decls = hermes_md.get("required_environment_variables") or []
    env_names: list[str] = []
    if isinstance(env_decls, list):
        for entry in env_decls:
            if isinstance(entry, dict) and entry.get("name"):
                env_names.append(str(entry["name"]))
            elif isinstance(entry, str):
                env_names.append(entry)

    platforms = fm.get("platforms") or []
    if not isinstance(platforms, list):
        platforms = []

    # repo-relative POSIX path so the YAML is portable across Linux / Windows.
    try:
        repo_rel = md_path.resolve().relative_to(repo_root.resolve())
        skill_md_path = repo_rel.as_posix()
    except ValueError:
        skill_md_path = md_path.as_posix()

    return SkillEntry(
        id=f"{profile}/{category}/{name}",
        profile=profile,
        category=category,
        name=name,
        description=str(fm.get("description") or "").strip(),
        version=str(fm.get("version") or "").strip(),
        skill_md_path=skill_md_path,
        tags=[str(t) for t in tags],
        requires_toolsets=[str(t) for t in requires_toolsets],
        required_env_vars=env_names,
        platforms=[str(p) for p in platforms],
    )


class SkillLibrary:
    """Scans ``profiles_root`` for SKILL.md files and produces a registry."""

    def __init__(self, profiles_root: Path, *, repo_root: Path | None = None):
        self.profiles_root = Path(profiles_root)
        # Used to make ``skill_md_path`` repo-relative. Falls back to the
        # profiles_root's parent (the repo root by convention).
        self.repo_root = Path(repo_root) if repo_root else self.profiles_root.parent

    def scan(self) -> list[SkillEntry]:
        """Return all skills, sorted by ``id`` for stable output."""
        if not self.profiles_root.exists():
            return []
        entries: list[SkillEntry] = []
        for md in sorted(self.profiles_root.rglob("SKILL.md")):
            entry = _entry_from_skill_md(
                md, profiles_root=self.profiles_root, repo_root=self.repo_root
            )
            if entry is not None:
                entries.append(entry)
        entries.sort(key=lambda e: e.id)
        return entries

    def build_registry(self) -> dict[str, Any]:
        """Materialize the full registry dict (ready for yaml.safe_dump)."""
        entries = self.scan()
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "skill_count": len(entries),
            "skills": [e.model_dump(mode="json") for e in entries],
        }

    def write_registry(self, output_path: Path) -> dict[str, Any]:
        """Write the registry to ``output_path`` and return the dict."""
        registry = self.build_registry()
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(
                registry,
                f,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )
        return registry
