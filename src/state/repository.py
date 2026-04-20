"""SQLite-backed persistence for TaskState and daily cloud-token budget (R4).

All data is serialized as JSON. Keep this layer thin — we only need:
  - upsert/get a TaskState by task_id
  - list recent tasks by user
  - record & query daily cloud-token usage for budget gating
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import aiosqlite

from src.state.task_state import TaskState


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    state_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_user_created ON tasks(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);

CREATE TABLE IF NOT EXISTS budget_daily (
    user_id  TEXT NOT NULL,
    day      TEXT NOT NULL,
    tokens   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
"""


def _today() -> str:
    return date.today().isoformat()


class Repository:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    # ---- tasks ----

    async def save_task(self, state: TaskState) -> None:
        payload = state.model_dump_json()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO tasks(task_id, session_id, user_id, status, created_at, updated_at, state_json)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET
                  status=excluded.status,
                  updated_at=excluded.updated_at,
                  state_json=excluded.state_json
                """,
                (
                    state.task_id,
                    state.session_id,
                    state.user_id,
                    state.status,
                    state.created_at.isoformat(),
                    state.updated_at.isoformat(),
                    payload,
                ),
            )
            await db.commit()

    async def get_task(self, task_id: str) -> TaskState | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT state_json FROM tasks WHERE task_id=?", (task_id,)
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return TaskState.model_validate_json(row[0])

    async def list_user_tasks(self, user_id: str, limit: int = 20) -> list[TaskState]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT state_json FROM tasks WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [TaskState.model_validate_json(r[0]) for r in rows]

    # ---- budget ----

    async def used_tokens_today(self, user_id: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT tokens FROM budget_daily WHERE user_id=? AND day=?",
                (user_id, _today()),
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def add_tokens(self, user_id: str, tokens: int) -> int:
        if tokens <= 0:
            return await self.used_tokens_today(user_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO budget_daily(user_id, day, tokens) VALUES(?,?,?)
                ON CONFLICT(user_id, day) DO UPDATE SET tokens = tokens + excluded.tokens
                """,
                (user_id, _today(), tokens),
            )
            await db.commit()
        return await self.used_tokens_today(user_id)
