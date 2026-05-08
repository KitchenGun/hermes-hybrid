"""Promote memory candidates into SKILL.md artifacts (P3).

Default mode is ``--dry-run``: print every candidate the extractor
generated and exit without writing. ``--apply`` is the only path that
creates SKILL.md files, and it requires the operator to choose the
target with ``--target-root``. When the resolved skill storage mode
is ``hermes_profile`` and ``--target-root`` is not given, the script
refuses to write — the implicit ``~/.hermes/skills/`` write is
deliberately gated behind the operator's explicit choice. Skills can
also be exported to the shared registry with ``--share``, but only
together with ``--apply``.

Frontmatter schema (plan v4.2):

    ---
    schema_version: 1
    skill_id: ...
    skill_version: ...
    skill_sha16: <sha16(SKILL.md body)>
    source_item_id: ...     # comma-joined when multi-source
    source_sha16: ...       # first item's sha16
    created_at: ISO8601
    approved_by: user
    status: active
    profile: default
    ---

The extractor is rule-based; each candidate's body alone is what gets
sha16'd. Renames re-bump skill_version (P3 follow-up — out of scope).
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.memory.skill_candidate_extractor import (
    SkillCandidate,
    SkillCandidateExtractor,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Promote rule-extracted memory candidates into SKILL.md "
            "files. Dry-run by default; --apply requires --target-root."
        ),
    )
    p.add_argument(
        "--processed-root",
        type=Path,
        default=Path("./data/processed_memory"),
    )
    p.add_argument(
        "--target-root",
        type=Path,
        default=None,
        help=(
            "Destination directory for SKILL.md files. Required with "
            "--apply. Common choices: ./data/profiles/default/skills "
            "(project_local) or ~/.hermes/skills (hermes_profile)."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually write SKILL.md files. Default is dry-run.",
    )
    p.add_argument(
        "--profile",
        default="default",
        help="Profile name stamped onto frontmatter.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="If >0, only emit the first N candidates.",
    )
    return p


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def render_skill_md(cand: SkillCandidate, *, profile: str) -> str:
    body = cand.to_skill_markdown()
    skill_sha16 = _sha16(body)
    source_ids = ",".join(cand.source_item_ids) or "<none>"
    source_sha16 = (
        cand.source_item_ids[0].split(":", 2)[1]
        if cand.source_item_ids
        else ""
    )
    front = (
        "---\n"
        f"schema_version: 1\n"
        f"skill_id: {cand.skill_id}\n"
        f"skill_version: {cand.skill_version}\n"
        f"skill_sha16: {skill_sha16}\n"
        f"source_item_id: {source_ids}\n"
        f"source_sha16: {source_sha16}\n"
        f"created_at: {_utc_now_iso()}\n"
        f"approved_by: user\n"
        f"status: active\n"
        f"profile: {profile}\n"
        "---\n\n"
    )
    return front + body


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    extractor = SkillCandidateExtractor(args.processed_root)
    candidates = extractor.extract()
    if args.limit > 0:
        candidates = candidates[: args.limit]
    print(f"[promote-memory] candidates: {len(candidates)}")

    if not args.apply:
        for c in candidates:
            print(f"  - {c.skill_id}: {c.title}")
        print("[promote-memory] dry-run; pass --apply --target-root to write.")
        return 0

    if args.target_root is None:
        print(
            "[promote-memory] --apply requires --target-root.",
            file=sys.stderr,
        )
        return 2

    target = args.target_root.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    written = 0
    for c in candidates:
        skill_dir = target / c.skill_id
        skill_dir.mkdir(parents=True, exist_ok=True)
        path = skill_dir / "SKILL.md"
        if path.exists():
            print(f"  - skip (exists): {path}")
            continue
        path.write_text(
            render_skill_md(c, profile=args.profile),
            encoding="utf-8",
        )
        written += 1
        print(f"  + wrote: {path}")
    print(f"[promote-memory] wrote {written} SKILL.md files under {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
