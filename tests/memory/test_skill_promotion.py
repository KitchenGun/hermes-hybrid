"""Tests for skill_candidate_extractor + promote_memory_to_skill (P3)."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.memory.ingestion.writer import ProcessedMemoryWriter
from src.memory.skill_candidate_extractor import (
    SkillCandidate,
    SkillCandidateExtractor,
)


@pytest.fixture
def populated_proc(tmp_path: Path) -> Path:
    root = tmp_path / "processed_memory"
    w = ProcessedMemoryWriter(root)
    w.write(
        type="failure_pattern",
        title="acceptEdits hardcoded confusion",
        body="recurring confusion: agent assumes hardcoded acceptEdits mode",
        source="claude",
        source_sha16="aa" * 8,
        confidence="medium",
    )
    w.write(
        type="prompt_template",
        title="senior reviewer brief",
        body="You are a senior reviewer. Be terse.",
        source="claude",
        source_sha16="bb" * 8,
    )
    w.write(
        type="decision",
        title="kanban over scrum",
        body="team uses kanban; do not propose scrum",
        source="claude",
        source_sha16="cc" * 8,
    )
    return root


def test_extractor_yields_one_candidate_per_source_type(populated_proc: Path) -> None:
    cands = SkillCandidateExtractor(populated_proc).extract()
    assert len(cands) == 3
    ids = {c.skill_id for c in cands}
    # IDs follow the prefix conventions in the extractor
    assert any(i.startswith("hermes-") for i in ids)
    assert any("prompt-" in i for i in ids)
    assert any("policy-" in i for i in ids)


def test_extractor_skips_quarantined(tmp_path: Path) -> None:
    root = tmp_path / "processed_memory"
    w = ProcessedMemoryWriter(root)
    w.write(
        type="failure_pattern",
        title="risky pattern",
        body="contact alice@example.com",
        source="claude",
        source_sha16="aa" * 8,
        pii_candidate=True,
    )
    cands = SkillCandidateExtractor(root).extract()
    assert cands == []


def test_skill_candidate_renders_four_sections(populated_proc: Path) -> None:
    cands = SkillCandidateExtractor(populated_proc).extract()
    assert cands
    body = cands[0].to_skill_markdown()
    assert "## When to Use" in body
    assert "## Procedure" in body
    assert "## Pitfalls" in body
    assert "## Verification" in body


def test_dry_run_does_not_write(populated_proc: Path, tmp_path: Path) -> None:
    """Invoke main(['--processed-root', ...]) without --apply: no writes."""
    from scripts import promote_memory_to_skill as mod

    target = tmp_path / "skills_target"  # must NOT be created
    rc = mod.main([
        "--processed-root", str(populated_proc),
    ])
    assert rc == 0
    assert not target.exists()


def test_apply_requires_target_root(populated_proc: Path) -> None:
    from scripts import promote_memory_to_skill as mod

    rc = mod.main([
        "--processed-root", str(populated_proc),
        "--apply",
    ])
    # Refuses to write without explicit target.
    assert rc == 2


def test_apply_with_target_writes_skill_md(
    populated_proc: Path, tmp_path: Path
) -> None:
    from scripts import promote_memory_to_skill as mod

    target = tmp_path / "skills_target"
    rc = mod.main([
        "--processed-root", str(populated_proc),
        "--apply",
        "--target-root", str(target),
    ])
    assert rc == 0
    skill_dirs = list(target.glob("*/SKILL.md"))
    assert len(skill_dirs) == 3  # one per candidate
    sample = skill_dirs[0].read_text(encoding="utf-8")
    assert "schema_version: 1" in sample
    assert "skill_sha16:" in sample
    assert "approved_by: user" in sample
    assert "status: active" in sample
    assert "## When to Use" in sample


def test_apply_skips_existing_skill_md(
    populated_proc: Path, tmp_path: Path
) -> None:
    from scripts import promote_memory_to_skill as mod

    target = tmp_path / "skills_target"
    mod.main([
        "--processed-root", str(populated_proc),
        "--apply",
        "--target-root", str(target),
    ])
    written_first = list(target.rglob("SKILL.md"))
    # Re-running should not duplicate / clobber.
    rc = mod.main([
        "--processed-root", str(populated_proc),
        "--apply",
        "--target-root", str(target),
    ])
    assert rc == 0
    written_second = list(target.rglob("SKILL.md"))
    assert len(written_second) == len(written_first)
