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

-- Watcher dedup state. account is empty for non-mail watchers; for
-- mail_poll, it is the account name from accounts.yaml so each mailbox
-- gets its own last_message_id row.
CREATE TABLE IF NOT EXISTS watcher_state (
    profile_id     TEXT NOT NULL,
    watcher_name   TEXT NOT NULL,
    account        TEXT NOT NULL DEFAULT '',
    last_dedup_key TEXT,
    last_run_at    TEXT,
    PRIMARY KEY (profile_id, watcher_name, account)
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

    async def list_awaiting_confirmations(self) -> list[TaskState]:
        """Return all tasks persisted in the ``awaiting_confirmation`` state.

        Used on bot startup to notify users of pending confirmations whose
        Discord buttons were orphaned by the restart.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT state_json FROM tasks WHERE status=? ORDER BY updated_at DESC",
                ("awaiting_confirmation",),
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

    # ---- watcher state ----

    async def get_watcher_state(
        self, profile_id: str, watcher_name: str, account: str = ""
    ) -> str | None:
        """Return last_dedup_key for the given watcher+account, or None."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT last_dedup_key FROM watcher_state
                WHERE profile_id=? AND watcher_name=? AND account=?
                """,
                (profile_id, watcher_name, account),
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row and row[0] is not None else None

    async def update_watcher_state(
        self,
        profile_id: str,
        watcher_name: str,
        last_dedup_key: str,
        account: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO watcher_state(profile_id, watcher_name, account, last_dedup_key, last_run_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(profile_id, watcher_name, account) DO UPDATE SET
                  last_dedup_key=excluded.last_dedup_key,
                  last_run_at=excluded.last_run_at
                """,
                (profile_id, watcher_name, account, last_dedup_key, now),
            )
            await db.commit()

    async def get_watcher_last_run(
        self, profile_id: str, watcher_name: str, account: str = ""
    ) -> datetime | None:
        """Return the polling high-water timestamp as tz-aware datetime.

        Calendar watchers store the window end as ISO into the
        ``last_dedup_key`` column (mail watchers store message ids there
        — same column, different semantic per watcher type). We can't
        reuse ``last_run_at`` because :meth:`update_watcher_state` rewrites
        it to ``now`` on every save, which would defeat retry logic when
        a tick fails before delivery.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT last_dedup_key FROM watcher_state
                WHERE profile_id=? AND watcher_name=? AND account=?
                """,
                (profile_id, watcher_name, account),
            ) as cur:
                row = await cur.fetchone()
        if not row or not row[0]:
            return None
        try:
            return datetime.fromisoformat(row[0])
        except ValueError:
            # last_dedup_key isn't an ISO timestamp (mail watcher row, or
            # corrupt data). Treat as no high-water yet.
            return None
