#!/usr/bin/env python3
"""1회용: hermes-hybrid `data/state.db` `memos` → 공식 hermes MEMORY.md/USER.md.

사용자 §0 지침 (2026-05-11):
- 자수 제한 (MEMORY.md ≤ 2200, USER.md ≤ 1375)
- 자수 초과 시 truncate (LLM 호출 없이 안전)
- raw text 그대로 보존 — 공식 hermes의 sessions가 자체 privacy 처리

분기 규칙:
- source 가 'user_feedback_style_*' 또는 'user_*' / 'feedback_*' → USER.md
- source 가 'generated_candidates' / 'claude_import_*' / 'chatgpt_import_*' → MEMORY.md
- 그 외 → MEMORY.md (기본)

옵션:
  --src PATH       state.db 경로 (e.g. /mnt/e/hermes-hybrid/data/state.db)
  --dst DIR        memories/ 디렉터리 (e.g. ~/.hermes/memories)
  --max-user N     USER.md 자수 제한 (default 1375)
  --max-memory N   MEMORY.md 자수 제한 (default 2200)
  --dry-run        실제 쓰지 않고 출력만
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path


def classify_source(source: str | None) -> str:
    if not source:
        return "memory"
    s = source.lower()
    if s.startswith("user") or "feedback" in s:
        return "user"
    return "memory"


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    # 단순 truncate + 절단 표시 (1회용 스크립트, LLM 호출 없음)
    truncated = text[: limit - 64]
    last_nl = truncated.rfind("\n")
    if last_nl > limit // 2:
        truncated = truncated[:last_nl]
    return truncated.rstrip() + "\n\n<!-- … truncated to fit hermes limit -->\n"


def build_doc(rows: list[tuple], header: str, *, limit: int) -> str:
    lines: list[str] = [header, ""]
    seen: set[str] = set()
    for row_text, source, created_at in rows:
        cleaned = (row_text or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        ts = created_at.split("T")[0] if created_at else ""
        prefix = f"- [{ts}] " if ts else "- "
        lines.append(f"{prefix}{cleaned}")
    body = "\n".join(lines).rstrip() + "\n"
    return truncate(body, limit)


def write_doc(path: Path, content: str, *, dry_run: bool) -> None:
    print(f"  → {path}: {len(content)} chars")
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--dst", required=True, type=Path,
                    help="memories/ 디렉터리 (e.g. ~/.hermes/memories)")
    ap.add_argument("--max-user", type=int, default=1375)
    ap.add_argument("--max-memory", type=int, default=2200)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = args.src.expanduser().resolve()
    dst = args.dst.expanduser().resolve()

    if not src.is_file():
        print(f"!! state.db not found: {src}")
        return 2

    print(f"== migrate_memos_to_hermes_official ==")
    print(f"  src: {src}")
    print(f"  dst: {dst}")
    print(f"  max-user: {args.max_user}")
    print(f"  max-memory: {args.max_memory}")
    print(f"  mode: {'DRY-RUN' if args.dry_run else 'APPLY'}")

    conn = sqlite3.connect(str(src))
    rows = conn.execute(
        "SELECT text, source, created_at FROM memos ORDER BY created_at"
    ).fetchall()
    conn.close()

    user_rows: list[tuple] = []
    memory_rows: list[tuple] = []
    for row in rows:
        text, source, created_at = row
        kind = classify_source(source)
        (user_rows if kind == "user" else memory_rows).append(row)

    print(f"  rows: {len(rows)} total ({len(user_rows)} user, {len(memory_rows)} memory)")
    print()

    today = datetime.utcnow().strftime("%Y-%m-%d")
    user_doc = build_doc(
        user_rows,
        f"# User Profile\n\n<!-- migrated from hermes-hybrid memos on {today} -->",
        limit=args.max_user,
    )
    memory_doc = build_doc(
        memory_rows,
        f"# Hermes Memory\n\n<!-- migrated from hermes-hybrid memos on {today} -->",
        limit=args.max_memory,
    )

    write_doc(dst / "USER.md", user_doc, dry_run=args.dry_run)
    write_doc(dst / "MEMORY.md", memory_doc, dry_run=args.dry_run)

    print()
    print(f"OK ({len(rows)} rows{'  [DRY-RUN]' if args.dry_run else ''})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
