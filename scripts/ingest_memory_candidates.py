"""W1 — Ingest memory/memory_candidates.generated.yaml into data/memory/memos.db.

For each candidate with `should_store: true`:
  - call SqliteMemory.save(user_id, text)
  - then UPDATE memos SET source=<source> WHERE id=lastrowid

Audit log: data/memory/ingest.log

Usage:
    python scripts/ingest_memory_candidates.py --dry-run
    python scripts/ingest_memory_candidates.py --apply --user-id kang9
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.memory.sqlite import SqliteMemory  # noqa: E402

DEFAULT_DB = REPO_ROOT / "data" / "memory" / "memos.db"
DEFAULT_YAML = REPO_ROOT / "memory" / "memory_candidates.generated.yaml"
DEFAULT_LOG = REPO_ROOT / "data" / "memory" / "ingest.log"


def _load_candidates(yaml_path: Path) -> list[dict]:
    with yaml_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    items = data.get("candidates") or []
    return [c for c in items if c.get("should_store")]


def _detect_user_id(default: str = "local_default") -> str:
    log_root = REPO_ROOT / "logs" / "experience"
    if not log_root.exists():
        return default
    seen: dict[str, int] = {}
    for f in sorted(log_root.glob("*.jsonl")):
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                uid = row.get("user_id") or ""
                if uid:
                    seen[uid] = seen.get(uid, 0) + 1
        except OSError:
            continue
    if not seen:
        return default
    return max(seen.items(), key=lambda kv: kv[1])[0]


async def _stamp_source(db_path: Path, memo_id: int, source: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE memos SET source=? WHERE id=?",
            (source, memo_id),
        )
        await db.commit()


async def _last_id(db_path: Path) -> int:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT id FROM memos ORDER BY id DESC LIMIT 1") as cur:
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--yaml", type=Path, default=DEFAULT_YAML)
    p.add_argument("--user-id", default=None)
    p.add_argument("--source", default="generated_candidates")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True)
    g.add_argument("--apply", action="store_true")
    p.add_argument("--log", type=Path, default=DEFAULT_LOG)
    args = p.parse_args()

    if args.apply:
        args.dry_run = False

    if not args.yaml.exists():
        print(f"yaml missing: {args.yaml}", file=sys.stderr)
        return 1

    candidates = _load_candidates(args.yaml)
    user_id = args.user_id or _detect_user_id()

    print(f"yaml: {args.yaml}")
    print(f"db:   {args.db}")
    print(f"user_id: {user_id}")
    print(f"source: {args.source}")
    print(f"candidates (should_store=true): {len(candidates)}")
    print(f"mode: {'apply' if args.apply else 'dry-run'}")

    if args.dry_run:
        for c in candidates[:5]:
            print(f"  - id={c.get('id')} type={c.get('type')} content={(c.get('content') or '')[:60]}")
        if len(candidates) > 5:
            print(f"  ... +{len(candidates) - 5} more")
        return 0

    args.db.parent.mkdir(parents=True, exist_ok=True)
    args.log.parent.mkdir(parents=True, exist_ok=True)

    backend = SqliteMemory(args.db)
    await backend.init()

    inserted = 0
    insert_ids: list[int] = []
    failures: list[str] = []

    for c in candidates:
        text = (c.get("content") or "").strip()
        if not text:
            continue
        try:
            await backend.save(user_id, text)
            new_id = await _last_id(args.db)
            await _stamp_source(args.db, new_id, args.source)
            insert_ids.append(new_id)
            inserted += 1
        except Exception as e:  # noqa: BLE001
            failures.append(f"{c.get('id')}: {type(e).__name__} {e}")

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with args.log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": ts,
            "yaml": str(args.yaml),
            "user_id": user_id,
            "source": args.source,
            "inserted": inserted,
            "ids": insert_ids,
            "failures": failures,
        }, ensure_ascii=False) + "\n")

    print(f"inserted: {inserted}")
    print(f"ids: {insert_ids[:10]}{'...' if len(insert_ids) > 10 else ''}")
    if failures:
        print(f"failures: {len(failures)}")
        for f_ in failures[:5]:
            print(f"  - {f_}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
