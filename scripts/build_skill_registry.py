#!/usr/bin/env python3
"""Build skills/registry.yaml from every profiles/*/skills/**/SKILL.md.

Run this whenever a SKILL.md is added/edited so the index stays in sync.
The Curator job (P5) will eventually own the same write path; this CLI
is for ad-hoc / pre-commit use today.

Usage:
    python scripts/build_skill_registry.py
    python scripts/build_skill_registry.py --output some/other/path.yaml
    python scripts/build_skill_registry.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make ``src.core`` importable when this script is run directly.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from src.core.skill_library import SkillLibrary  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=_REPO / "skills" / "registry.yaml",
        help="Path for the generated registry.yaml",
    )
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        default=_REPO / "profiles",
        help="Profiles root to scan (default: <repo>/profiles)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and print summary only, don't write the file",
    )
    args = parser.parse_args()

    library = SkillLibrary(args.profiles_dir, repo_root=_REPO)
    entries = library.scan()
    print(f"Found {len(entries)} SKILL.md files under {args.profiles_dir}")
    for entry in entries:
        print(f"  - {entry.id}  ({entry.description[:60]})")

    if args.dry_run:
        return 0

    library.write_registry(args.output)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
