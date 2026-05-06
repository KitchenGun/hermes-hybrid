"""Tests for AgentRegistry — Phase 7 sub-agent taxonomy.

Locks down:
  * scan finds all 17 agents in 6 categories
  * by_handle('@coder') resolves; case-insensitive; @-optional
  * by_category returns only that bucket
  * frontmatter projection: name / role / description / when_to_use /
    primary_tools / tags
  * malformed frontmatter doesn't crash the scan
  * unknown handle returns None
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.agents import AgentEntry, AgentRegistry, _CATEGORIES


# ---- real repo agents/ ---------------------------------------------------


def _real_registry() -> AgentRegistry | None:
    repo = Path(__file__).resolve().parent.parent
    agents_root = repo / "agents"
    if not agents_root.exists():
        return None
    return AgentRegistry(agents_root, repo_root=repo)


def test_real_repo_has_seventeen_agents_across_six_categories():
    reg = _real_registry()
    if reg is None:
        pytest.skip("agents/ root not present (running outside repo)")
    entries = reg.all()
    assert len(entries) == 17

    summary = reg.summary()
    # 6 categories — exact counts as defined in Phase 7 plan
    assert summary == {
        "research": 3,
        "planning": 2,
        "implementation": 4,
        "quality": 4,
        "documentation": 2,
        "infrastructure": 2,
    }


def test_real_repo_by_handle_case_insensitive():
    reg = _real_registry()
    if reg is None:
        pytest.skip("agents/ root not present")
    coder = reg.by_handle("@coder")
    assert coder is not None
    assert coder.name == "coder"
    assert coder.category == "implementation"
    # Case-insensitive + @-optional both work
    assert reg.by_handle("@CODER") is coder
    assert reg.by_handle("coder") is coder


def test_real_repo_unknown_handle_returns_none():
    reg = _real_registry()
    if reg is None:
        pytest.skip("agents/ root not present")
    assert reg.by_handle("@does_not_exist") is None
    assert reg.by_handle("") is None


def test_real_repo_by_category_filters_correctly():
    reg = _real_registry()
    if reg is None:
        pytest.skip("agents/ root not present")
    research = reg.by_category("research")
    assert sorted(e.name for e in research) == ["analyst", "finder", "researcher"]
    quality = reg.by_category("quality")
    assert sorted(e.name for e in quality) == [
        "debugger", "reviewer", "security", "tester",
    ]


def test_real_repo_frontmatter_projection():
    reg = _real_registry()
    if reg is None:
        pytest.skip("agents/ root not present")
    coder = reg.by_handle("@coder")
    assert coder.role
    assert coder.description
    assert coder.when_to_use, "when_to_use list must be non-empty"
    assert coder.not_for
    # primary_tools comes from metadata.hermes.primary_tools
    assert "write" in coder.primary_tools or "edit" in coder.primary_tools


# ---- synthetic tmp_path agents tree --------------------------------------


_GOOD_FM = """\
---
name: tmp_agent
agent_handle: "@tmp"
category: implementation
role: test_role
description: A synthetic agent for unit tests.
when_to_use:
  - "writing tests"
not_for:
  - "production"
inputs:
  - "x"
outputs:
  - "y"
metadata:
  hermes:
    tags: [test, synthetic]
    primary_tools: [edit]
---

Body content.
"""


_BROKEN_FM = """\
---
name: broken
this is not yaml
---
"""


def _build_synth(root: Path) -> None:
    (root / "implementation" / "tmp_agent").mkdir(parents=True)
    (root / "implementation" / "tmp_agent" / "SKILL.md").write_text(
        _GOOD_FM, encoding="utf-8"
    )
    (root / "implementation" / "broken").mkdir(parents=True)
    (root / "implementation" / "broken" / "SKILL.md").write_text(
        _BROKEN_FM, encoding="utf-8"
    )


def test_synthetic_scan_includes_well_formed_skips_broken(tmp_path):
    _build_synth(tmp_path)
    reg = AgentRegistry(tmp_path)
    entries = reg.all()
    # broken frontmatter is skipped (no name parsed → entry None) but
    # we don't assert exact count — the well-formed agent must show up.
    handles = [e.handle for e in entries]
    assert "@tmp" in handles
    tmp = reg.by_handle("@tmp")
    assert tmp is not None
    assert tmp.role == "test_role"
    assert tmp.tags == ["test", "synthetic"]
    assert tmp.primary_tools == ["edit"]
    assert tmp.when_to_use == ["writing tests"]


def test_synthetic_categories_summary(tmp_path):
    _build_synth(tmp_path)
    reg = AgentRegistry(tmp_path)
    summary = reg.summary()
    # All 6 categories surface as keys (count=0 for missing buckets)
    assert set(summary.keys()) == set(_CATEGORIES)
    assert summary["implementation"] >= 1
    assert summary["research"] == 0


def test_missing_agents_root_returns_empty(tmp_path):
    reg = AgentRegistry(tmp_path / "does_not_exist")
    assert reg.all() == []
    assert reg.by_handle("@coder") is None
    assert reg.summary() == {cat: 0 for cat in _CATEGORIES}
