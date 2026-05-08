"""W1 — Idempotent schema migration: ALTER TABLE memos ADD COLUMN source.

`SqliteMemory` (src/memory/sqlite.py:47) does not expose ALTER. We open a
raw aiosqlite connection.

Usage:
    python scripts/migrate_memos_add_source.py --check
    python scripts/migrate_memos_add_source.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import aiosqlite


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "memory" / "memos.db"


async def _has_source_column(db_path: Path) -> bool:
    if not db_path.exists():
        return False
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("PRAGMA table_info(memos)") as cur:
            cols = await cur.fetchall()
    return any(c[1] == "source" for c in cols)


async def _ensure_table(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memos (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                text       TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memos_user_created
                ON memos(user_id, created_at);
            """
        )
        await db.commit()


async def _add_source_column(db_path: Path) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "ALTER TABLE memos ADD COLUMN source TEXT DEFAULT 'manual'"
        )
        await db.commit()


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = p.parse_args()

    if not args.check and not args.apply:
        args.check = True

    await _ensure_table(args.db)
    has = await _has_source_column(args.db)

    if args.check:
        print(f"db: {args.db}")
        print(f"source column present: {has}")
        return 0 if has else 1

    if has:
        print(f"already migrated: {args.db}")
        return 0

    await _add_source_column(args.db)
    print(f"migrated: {args.db} (added source TEXT DEFAULT 'manual')")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
