#!/usr/bin/env python3
"""1회용: opencode-hermes-multiagent .md → 공식 hermes profile SOUL.md 변환.

사용자 §0 지침 (2026-05-11):
- frontmatter (mode:/model:/tools:/...) 전체 strip — 본문만 SOUL.md로
- model 가상값 (gpt-5.2-high 등) 무시 — hermes default model 그대로
- opencode.json/package.json/MCP 서버 무시
- ~/.config/opencode/ 디렉터리 만들지 않음

옵션:
  --src DIR        opencode-hermes-multiagent clone root (e.g. /tmp/ohma)
  --dst DIR        hermes profiles root (e.g. ~/.hermes/profiles)
  --default-dst    default profile SOUL.md 별도 위치 (e.g. ~/.hermes/SOUL.md)
                   미지정 시 ~/.hermes/profiles/default/SOUL.md 사용
  --dry-run        실제 쓰지 않고 출력만

기본 매핑:
  agent/core/hermes.md                       → <dst>/default/SOUL.md
  agent/subagents/{cat}/{name}.md            → <dst>/{name}/SOUL.md
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

CATEGORIES = ("research", "planning", "implementation", "quality",
              "documentation", "infrastructure")


def strip_frontmatter(text: str) -> str:
    """YAML frontmatter (--- ... ---)를 통째로 제거. 본문만 반환.
    frontmatter 없으면 원문 그대로."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return text.lstrip("\n")
    return text[m.end():].lstrip("\n")


def collect(src_root: Path) -> list[tuple[str, Path]]:
    """(profile_name, source_path) 목록 반환."""
    items: list[tuple[str, Path]] = []
    core = src_root / "agent" / "core" / "hermes.md"
    if core.is_file():
        items.append(("default", core))
    sub = src_root / "agent" / "subagents"
    if sub.is_dir():
        for cat in CATEGORIES:
            cat_dir = sub / cat
            if not cat_dir.is_dir():
                continue
            for md in sorted(cat_dir.glob("*.md")):
                items.append((md.stem, md))
    return items


def write_soul(dst_path: Path, body: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] would write {dst_path} ({len(body)} chars)")
        return
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_path.write_text(body, encoding="utf-8")
    print(f"  wrote {dst_path} ({len(body)} chars)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, type=Path,
                    help="opencode-hermes-multiagent clone root")
    ap.add_argument("--dst", required=True, type=Path,
                    help="hermes profiles root (e.g. ~/.hermes/profiles)")
    ap.add_argument("--default-dst", type=Path, default=None,
                    help="default profile SOUL.md 별도 위치 (e.g. ~/.hermes/SOUL.md). "
                         "미지정 시 <dst>/default/SOUL.md")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = args.src.expanduser().resolve()
    dst = args.dst.expanduser().resolve()
    default_dst = args.default_dst.expanduser().resolve() if args.default_dst else None

    items = collect(src)
    if not items:
        print(f"!! no .md files found under {src}/agent/{{core,subagents}}/")
        return 2

    print(f"== migrate_opencode_agents_to_hermes_profiles ==")
    print(f"  src: {src}")
    print(f"  dst: {dst}")
    print(f"  default-dst: {default_dst or f'{dst}/default/SOUL.md'}")
    print(f"  mode: {'DRY-RUN' if args.dry_run else 'APPLY'}")
    print(f"  found {len(items)} .md files")
    print()

    for name, src_path in items:
        body = strip_frontmatter(src_path.read_text(encoding="utf-8"))
        if name == "default" and default_dst is not None:
            target = default_dst
        else:
            target = dst / name / "SOUL.md"
        print(f"- {name}: {src_path.name}")
        write_soul(target, body, dry_run=args.dry_run)

    print()
    print(f"OK ({len(items)} files{'  [DRY-RUN]' if args.dry_run else ''})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
