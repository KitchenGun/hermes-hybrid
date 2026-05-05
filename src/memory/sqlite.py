"""SQLite-backed ``MemoryBackend`` — durable across bot restarts.

Drop-in replacement for :class:`InMemoryMemory` once Phase 2b ships. Same
interface, same validation rules, same 2k-char cap; the only difference
is that notes survive process restarts.

Storage lives alongside the existing Repository's SQLite DB — one shared
file, separate ``memos`` table. We open a fresh aiosqlite connection per
operation (same pattern as ``Repository``) to avoid threading a single
connection through the async loop and to keep crash recovery simple.

Scope caveats (inherited from :mod:`memory.base`):
  - short text only (≤ 2000 chars; ``MemoryTooLarge`` on overflow)
  - per-user keyspace (rows are partitioned by ``user_id``)
  - ``list_memos`` returns insertion order, tail-limited to ``limit``
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .base import Memo, MemoryBackend, _validate


_SCHEMA = """
CREATE TABLE IF NOT EXISTS memos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memos_user_created ON memos(user_id, created_at);
"""


class SqliteMemory(MemoryBackend):
    """Durable memo store. Shares the Repository's SQLite file by default.

    The schema is self-contained (``CREATE TABLE IF NOT EXISTS``) so this
    can co-exist with the existing ``tasks`` / ``budget_daily`` tables —
    callers can pass ``settings.state_db_path`` to unify storage, or a
    dedicated path for test isolation.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        """Create the ``memos`` table if missing. Idempotent."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def save(self, user_id: str, text: str) -> Memo:
        clean = _validate(text)
        created = datetime.now(timezone.utc)
        async with aiosqlite.connect(self.db_path) as db:
            # Defensive: make sure the table exists even if the caller
            # forgot to call init() explicitly. This keeps the
            # MemoryBackend Protocol contract drop-in compatible with
            # InMemoryMemory, which needs no init step.
            await db.executescript(_SCHEMA)
            await db.execute(
                "INSERT INTO memos(user_id, text, created_at) VALUES(?,?,?)",
                (user_id, clean, created.isoformat()),
            )
            await db.commit()
        return Memo(user_id=user_id, text=clean, created_at=created)

    async def list_memos(self, user_id: str, limit: int = 20) -> list[Memo]:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            async with db.execute(
                # Fetch the most recent `limit` rows (ORDER BY created_at DESC),
                # then reverse so the caller sees insertion order — matches
                # InMemoryMemory's tail-slice semantics.
                "SELECT text, created_at FROM memos "
                "WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        rows = list(reversed(rows))
        return [
            Memo(
                user_id=user_id,
                text=row[0],
                created_at=_parse_iso(row[1]),
            )
            for row in rows
        ]

    async def clear(self, user_id: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            cur = await db.execute(
                "DELETE FROM memos WHERE user_id=?",
                (user_id,),
            )
            await db.commit()
            return cur.rowcount or 0

    async def search(
        self, user_id: str, query: str, k: int = 5
    ) -> list[Memo]:
        """LIKE-based substring search.

        Korean tokenization via FTS5 unicode61 is whitespace-only, so a
        bare FTS5 index would miss substring matches inside a token (which
        is the common case for short journal-style memos in Korean).
        LIKE works for both languages without an index pass — fine at the
        memo volumes this stores. Upgrade to FTS5 when corpus crosses
        ~10k notes per user, not before.
        """
        q = (query or "").strip()
        if not q:
            return []
        # Tokenize on whitespace — match if ANY token is a substring.
        # Mirror the InMemoryMemory semantics so the two backends are
        # interchangeable in tests.
        tokens = [t for t in q.split() if t]
        if not tokens:
            return []
        # Escape SQL LIKE wildcards in user input so a query containing
        # ``%`` or ``_`` only matches literally. Without this, a query
        # of ``%`` would match every row via ``%%%`` and effectively
        # exfiltrate the user's memo list.
        escaped = [
            t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            for t in tokens
        ]
        patterns = [f"%{t}%" for t in escaped]
        clause = " OR ".join(["text LIKE ? ESCAPE '\\'"] * len(patterns))
        sql = (
            f"SELECT text, created_at FROM memos "
            f"WHERE user_id=? AND ({clause}) ORDER BY id DESC LIMIT ?"
        )
        params: list = [user_id] + patterns + [k]
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [
            Memo(user_id=user_id, text=row[0], created_at=_parse_iso(row[1]))
            for row in rows
        ]


def _parse_iso(s: str) -> datetime:
    # aiosqlite returns text as-is; we wrote UTC isoformat so fromisoformat
    # round-trips cleanly on Py 3.11+. Older stored values without tz info
    # get stamped UTC rather than failing — notes are low-stakes.
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
