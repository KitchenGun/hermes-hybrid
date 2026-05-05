"""Tests for SkillLibrary — frontmatter scanner + registry writer.

The library is the curator's read path, so the contract is:
  * malformed / missing frontmatter is tolerated (returns defaults)
  * directory layout outside ``profile/skills/category/name/SKILL.md``
    is silently skipped (no crash on unfamiliar trees)
  * every entry has a stable id; output ordering is deterministic
  * registry.yaml round-trips through pyyaml without losing fields
"""
from __future__ import annotations

from pathlib import Path

import yaml

from src.core import SkillLibrary


def _make_skill(
    profiles_root: Path,
    *,
    profile: str,
    category: str,
    name: str,
    body: str,
) -> Path:
    skill_dir = profiles_root / profile / "skills" / category / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = skill_dir / "SKILL.md"
    md.write_text(body, encoding="utf-8")
    return md


_FULL_FRONTMATTER = """---
name: discord_notify
description: Send a notification to Discord via webhook.
version: 1.2.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [messaging, discord]
    category: messaging
    requires_toolsets: [terminal]
    required_environment_variables:
      - name: DISCORD_WEBHOOK_URL
        description: webhook
      - name: DISCORD_USERNAME
        optional: true
---

# Body
Some markdown here.
"""

_NO_FRONTMATTER = """# Plain Markdown

No frontmatter at all.
"""

_BROKEN_FRONTMATTER = """---
name: broken
description: this isn't closed properly
version: 0.0.1
"""


def test_scan_empty_profiles_root_is_empty(tmp_path: Path):
    library = SkillLibrary(tmp_path)
    assert library.scan() == []


def test_scan_skips_layouts_that_dont_match_convention(tmp_path: Path):
    # SKILL.md at the wrong depth — too few path parts
    odd = tmp_path / "calendar_ops" / "SKILL.md"
    odd.parent.mkdir(parents=True)
    odd.write_text(_FULL_FRONTMATTER, encoding="utf-8")

    # Also a valid one — should still be picked up
    _make_skill(
        tmp_path,
        profile="calendar_ops",
        category="messaging",
        name="discord_notify",
        body=_FULL_FRONTMATTER,
    )

    library = SkillLibrary(tmp_path)
    entries = library.scan()
    assert len(entries) == 1
    assert entries[0].id == "calendar_ops/messaging/discord_notify"


def test_full_frontmatter_round_trip(tmp_path: Path):
    _make_skill(
        tmp_path,
        profile="calendar_ops",
        category="messaging",
        name="discord_notify",
        body=_FULL_FRONTMATTER,
    )
    library = SkillLibrary(tmp_path)
    [entry] = library.scan()

    assert entry.id == "calendar_ops/messaging/discord_notify"
    assert entry.profile == "calendar_ops"
    assert entry.category == "messaging"
    assert entry.name == "discord_notify"
    assert entry.description.startswith("Send a notification")
    assert entry.version == "1.2.0"
    assert "linux" in entry.platforms
    assert "messaging" in entry.tags
    assert "terminal" in entry.requires_toolsets
    assert "DISCORD_WEBHOOK_URL" in entry.required_env_vars
    assert "DISCORD_USERNAME" in entry.required_env_vars
    assert entry.skill_md_path.endswith("SKILL.md")
    assert entry.status == "active"
    assert entry.source == "hand-written"
    assert entry.runs == 0


def test_no_frontmatter_yields_entry_with_defaults(tmp_path: Path):
    _make_skill(
        tmp_path,
        profile="kk_job",
        category="research",
        name="web_search",
        body=_NO_FRONTMATTER,
    )
    library = SkillLibrary(tmp_path)
    [entry] = library.scan()
    assert entry.id == "kk_job/research/web_search"
    assert entry.description == ""
    assert entry.version == ""
    assert entry.tags == []


def test_broken_frontmatter_does_not_crash(tmp_path: Path):
    _make_skill(
        tmp_path,
        profile="advisor_ops",
        category="analysis",
        name="job_inventory",
        body=_BROKEN_FRONTMATTER,
    )
    library = SkillLibrary(tmp_path)
    [entry] = library.scan()
    # Frontmatter wasn't closed → treated as no-frontmatter.
    assert entry.id == "advisor_ops/analysis/job_inventory"
    assert entry.description == ""


def test_scan_results_are_sorted_for_stable_output(tmp_path: Path):
    _make_skill(
        tmp_path,
        profile="kk_job",
        category="storage",
        name="sheets_append",
        body=_FULL_FRONTMATTER,
    )
    _make_skill(
        tmp_path,
        profile="calendar_ops",
        category="messaging",
        name="discord_notify",
        body=_FULL_FRONTMATTER,
    )
    library = SkillLibrary(tmp_path)
    ids = [e.id for e in library.scan()]
    assert ids == sorted(ids)
    assert ids[0] == "calendar_ops/messaging/discord_notify"


def test_write_registry_yaml_is_round_trip_loadable(tmp_path: Path):
    _make_skill(
        tmp_path,
        profile="calendar_ops",
        category="messaging",
        name="discord_notify",
        body=_FULL_FRONTMATTER,
    )
    library = SkillLibrary(tmp_path)
    out = tmp_path / "registry.yaml"
    library.write_registry(out)

    loaded = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert loaded["skill_count"] == 1
    assert "generated_at" in loaded
    [skill] = loaded["skills"]
    assert skill["id"] == "calendar_ops/messaging/discord_notify"
    assert skill["status"] == "active"


def test_real_repo_profiles_scan_at_least_one_skill(tmp_path: Path):
    """Sanity: the actual repo's profiles directory should have skills.

    Catches regressions where a refactor renames the SKILL.md layout
    convention without updating the scanner.
    """
    repo_root = Path(__file__).resolve().parent.parent
    profiles = repo_root / "profiles"
    if not profiles.exists():
        # Skip when running outside the repo (tarball, sdist).
        return
    library = SkillLibrary(profiles, repo_root=repo_root)
    entries = library.scan()
    assert len(entries) > 0
    # All entries must have ids that include exactly two slashes.
    for e in entries:
        assert e.id.count("/") == 2
