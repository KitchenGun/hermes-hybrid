"""Extract SKILL.md candidates from processed_memory (P3, candidates only).

Plan v4.2 names this module ``src/memory/skill_promoter.py``. Renamed
to ``skill_candidate_extractor.py`` here to avoid collision with the
existing :mod:`src.jobs.skill_promoter` (which already has a W11
marker block on main and clusters ExperienceRecord patterns into
SKILL drafts under ``logs/curator/auto_skills``). The two modules
have orthogonal jobs:

- :mod:`src.jobs.skill_promoter` — production weekly schedule that
  reads ExperienceLog and may auto-install drafts. Untouched by P3.
- this module — generates candidates from ``data/processed_memory/``
  (failure_patterns / prompt_library / decision_log) and returns
  :class:`SkillCandidate` objects. **Never writes SKILL.md by
  itself.** The companion script
  ``scripts/promote_memory_to_skill.py`` is the only entry point that
  may write to disk, and only with ``--apply`` plus an explicit
  user-supplied target.

The four-section SKILL.md schema follows plan v4.2:

- ``When to Use``
- ``Procedure``
- ``Pitfalls``
- ``Verification``

Each candidate also carries the source item_ids it came from so a
human reviewer can trace lineage back to the originating
processed_memory rows.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .ingestion.writer import (
    MemoryItem,
    TYPE_TO_FILE,
    parse_processed_file,
    slugify,
)


_SKILL_ID_PREFIX = "hermes-"
_SKILL_VERSION_INITIAL = 1


@dataclass(frozen=True, slots=True)
class SkillCandidate:
    skill_id: str
    title: str
    when_to_use: str
    procedure: str
    pitfalls: str
    verification: str
    source_item_ids: tuple[str, ...]
    profile: str = "default"
    skill_version: int = _SKILL_VERSION_INITIAL

    def to_skill_markdown(self) -> str:
        """Render the SKILL.md body. Frontmatter is added at write time
        because skill_sha16 needs the body content first."""
        return (
            f"# {self.title}\n\n"
            f"## When to Use\n{self.when_to_use.strip()}\n\n"
            f"## Procedure\n{self.procedure.strip()}\n\n"
            f"## Pitfalls\n{self.pitfalls.strip()}\n\n"
            f"## Verification\n{self.verification.strip()}\n"
        )


class SkillCandidateExtractor:
    """Pure rule-based candidate generation. No LLM, no disk writes."""

    def __init__(self, processed_memory_root: Path | str) -> None:
        self.processed_memory_root = Path(processed_memory_root)

    def extract(self) -> list[SkillCandidate]:
        items = self._load_active(
            ["failure_pattern", "prompt_template", "decision"]
        )
        out: list[SkillCandidate] = []
        seen: set[str] = set()
        for it in items:
            cand = self._candidate_for(it)
            if cand is None or cand.skill_id in seen:
                continue
            seen.add(cand.skill_id)
            out.append(cand)
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _load_active(self, types: Sequence[str]) -> list[MemoryItem]:
        items: list[MemoryItem] = []
        for type_ in types:
            fname = TYPE_TO_FILE.get(type_)
            if not fname:
                continue
            path = self.processed_memory_root / fname
            if not path.exists():
                continue
            for it in parse_processed_file(path.read_text(encoding="utf-8")):
                if it.status != "active":
                    continue
                if it.pii_candidate or it.security_severity in ("medium", "high"):
                    continue
                if it.type != type_:
                    # parse_processed_file is type-agnostic — re-check.
                    continue
                items.append(it)
        return items

    def _candidate_for(self, item: MemoryItem) -> SkillCandidate | None:
        if item.type == "failure_pattern":
            return self._from_failure_pattern(item)
        if item.type == "prompt_template":
            return self._from_prompt_template(item)
        if item.type == "decision":
            return self._from_decision(item)
        return None

    @staticmethod
    def _from_failure_pattern(item: MemoryItem) -> SkillCandidate:
        slug = slugify(item.title)
        skill_id = _SKILL_ID_PREFIX + slug
        when = (
            "When the agent encounters a request similar to "
            f'"{item.title}" or recognises the failure pattern.'
        )
        procedure = (
            "Investigate the underlying assumption. The original failure "
            "was:\n\n"
            f"{item.body.strip()}\n\n"
            "Steps:\n"
            "1. Re-read the live state (config / file / log) before acting.\n"
            "2. Confirm the assumption against ground truth.\n"
            "3. Only then proceed with the change."
        )
        pitfalls = (
            "Do NOT rely on memorised values for state that may have "
            "drifted. Re-validate every time.\n"
            f"Source pattern: {item.title}"
        )
        verification = (
            "Add or update a regression test that fails when the assumption "
            "drifts. Tag the test with the originating item_id so future "
            "edits stay traceable."
        )
        return SkillCandidate(
            skill_id=skill_id,
            title=f"Hermes recovery: {item.title}",
            when_to_use=when,
            procedure=procedure,
            pitfalls=pitfalls,
            verification=verification,
            source_item_ids=(item.item_id,),
            profile=item.profile,
        )

    @staticmethod
    def _from_prompt_template(item: MemoryItem) -> SkillCandidate:
        slug = slugify(item.title)
        skill_id = _SKILL_ID_PREFIX + "prompt-" + slug
        when = (
            f"Use this prompt when the request matches the pattern: "
            f"{item.title}."
        )
        procedure = (
            "Substitute the placeholders below into the prompt and submit:\n\n"
            f"```\n{item.body.strip()}\n```"
        )
        pitfalls = (
            "Verify all variable placeholders are filled before sending. "
            "If unsure of a value, ask the user before proceeding."
        )
        verification = (
            "Sample the response against the original use case the prompt "
            "was created for; if quality drops, revisit the template body."
        )
        return SkillCandidate(
            skill_id=skill_id,
            title=f"Prompt: {item.title}",
            when_to_use=when,
            procedure=procedure,
            pitfalls=pitfalls,
            verification=verification,
            source_item_ids=(item.item_id,),
            profile=item.profile,
        )

    @staticmethod
    def _from_decision(item: MemoryItem) -> SkillCandidate:
        slug = slugify(item.title)
        skill_id = _SKILL_ID_PREFIX + "policy-" + slug
        when = (
            "When a request would conflict with the recorded decision: "
            f"{item.title}."
        )
        procedure = (
            f"Apply the decision: {item.body.strip()}\n\n"
            "If the request asks for the opposite, surface the decision and "
            "ask for explicit override. Do not silently revert."
        )
        pitfalls = (
            "Decisions can be re-litigated — never assume permanence. "
            "Always check decision_log.md for an updated entry first."
        )
        verification = (
            "Cross-reference any code change with decision_log.md; the entry "
            "should still be the freshest active item before acting on it."
        )
        return SkillCandidate(
            skill_id=skill_id,
            title=f"Policy: {item.title}",
            when_to_use=when,
            procedure=procedure,
            pitfalls=pitfalls,
            verification=verification,
            source_item_ids=(item.item_id,),
            profile=item.profile,
        )


__all__ = [
    "SkillCandidate",
    "SkillCandidateExtractor",
]
