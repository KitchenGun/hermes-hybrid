"""SQLite-backed Kanban store (Phase 2-A, Nous Hermes Agent 정렬).

WAL mode for concurrent read while a writer holds BEGIN IMMEDIATE.
``atomic_claim_one_ready`` uses an explicit BEGIN IMMEDIATE so dispatcher
tick races stay safe even though Phase 2-A only runs a single dispatcher
inside the gateway.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from src.core.kanban.models import (
    KanbanBoard,
    KanbanComment,
    KanbanEvent,
    KanbanRun,
    KanbanStatus,
    KanbanTask,
    RunOutcome,
)
from src.core.kanban.workspace import (
    ensure_scratch_workspace,
    materialize_workspace,
)


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class InvalidSlugError(ValueError):
    pass


def normalize_board_slug(raw: str) -> str:
    """Lowercase + validate. Rejects slashes, dots, spaces, and ``..``."""
    if not raw:
        raise InvalidSlugError("slug is required")
    slug = raw.strip().lower()
    if not _SLUG_RE.match(slug):
        raise InvalidSlugError(
            f"invalid slug {raw!r}: must be 1-64 chars, "
            "lowercase alphanumerics + hyphens/underscores, starting alphanumeric"
        )
    return slug


_SCHEMA_VERSION = 2  # v0.13 Tenacity: per-task retry budget columns
_BOARD_DEFAULT = "default"

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS boards (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    icon TEXT,
    description TEXT,
    created_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    board_id TEXT NOT NULL DEFAULT 'default',
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    assignee TEXT,
    tenant TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    workspace_kind TEXT NOT NULL DEFAULT 'scratch',
    workspace_path TEXT,
    idempotency_key TEXT,
    scheduled_at TEXT,
    max_runtime_seconds INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    current_run_id TEXT,
    spawn_failure_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_board ON tasks(status, board_id);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee);
CREATE INDEX IF NOT EXISTS idx_tasks_scheduled_at ON tasks(scheduled_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_idem
    ON tasks(board_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL
      AND status NOT IN ('done', 'archived');

CREATE TABLE IF NOT EXISTS task_links (
    parent_id TEXT NOT NULL,
    child_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);

CREATE INDEX IF NOT EXISTS idx_links_child ON task_links(child_id);

CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    outcome TEXT,
    summary TEXT,
    metadata_json TEXT,
    pid INTEGER,
    workspace_path TEXT,
    claim_expires_at TEXT NOT NULL,
    last_heartbeat_at TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_task ON task_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_runs_active ON task_runs(claim_expires_at)
    WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_comments_task ON task_comments(task_id);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT,
    actor TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, id);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat(timespec="seconds")


def _short_id(prefix: str) -> str:
    return prefix + secrets.token_hex(4)


def _row_to_task(row) -> KanbanTask:
    def _safe(name, default=None):
        try:
            return row[name]
        except (KeyError, IndexError):
            return default

    skills_raw = _safe("skills_json")
    skills: list[str] = []
    if skills_raw:
        try:
            parsed = json.loads(skills_raw)
            if isinstance(parsed, list):
                skills = [str(s) for s in parsed]
        except (ValueError, TypeError):
            skills = []
    return KanbanTask(
        id=row["id"],
        board_id=row["board_id"],
        title=row["title"],
        body=row["body"],
        status=row["status"],
        assignee=row["assignee"],
        tenant=row["tenant"],
        priority=row["priority"],
        workspace_kind=row["workspace_kind"],
        workspace_path=row["workspace_path"],
        idempotency_key=row["idempotency_key"],
        scheduled_at=row["scheduled_at"],
        max_runtime_seconds=row["max_runtime_seconds"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        created_by=row["created_by"],
        current_run_id=row["current_run_id"],
        spawn_failure_count=row["spawn_failure_count"],
        skills=skills,
        max_retries=_safe("max_retries", 3) or 3,
        retry_count=_safe("retry_count", 0) or 0,
    )


def _row_to_board(row) -> KanbanBoard:
    return KanbanBoard(
        id=row["id"],
        name=row["name"],
        icon=row["icon"],
        description=row["description"],
        created_at=row["created_at"],
        archived_at=row["archived_at"],
    )


def _row_to_run(row) -> KanbanRun:
    return KanbanRun(
        id=row["id"],
        task_id=row["task_id"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        outcome=row["outcome"],
        summary=row["summary"],
        metadata=json.loads(row["metadata_json"]) if row["metadata_json"] else {},
        pid=row["pid"],
        workspace_path=row["workspace_path"],
        claim_expires_at=row["claim_expires_at"],
        last_heartbeat_at=row["last_heartbeat_at"],
        error=row["error"],
    )


class KanbanDB:
    def __init__(self, db_path: Path, *, workspaces_root: Path):
        self.db_path = Path(db_path)
        self.workspaces_root = Path(workspaces_root)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspaces_root.mkdir(parents=True, exist_ok=True)

    async def migrate(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.executescript(_SCHEMA)
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (_SCHEMA_VERSION,),
            )
            await db.execute(
                "INSERT OR IGNORE INTO boards(id, name, created_at) VALUES (?, ?, ?)",
                (_BOARD_DEFAULT, "default", _utc_now_iso()),
            )
            # SQLite has no `IF NOT EXISTS` for ADD COLUMN, so probe each.
            async with db.execute("PRAGMA table_info(tasks)") as cur:
                cols = [row["name"] for row in await cur.fetchall()]
            # Phase 2-A v1.1: per-task skill loadout
            if "skills_json" not in cols:
                await db.execute(
                    "ALTER TABLE tasks ADD COLUMN skills_json TEXT"
                )
            # v0.13 "Tenacity": per-task retry budget + tracker
            if "max_retries" not in cols:
                await db.execute(
                    "ALTER TABLE tasks ADD COLUMN max_retries INTEGER "
                    "NOT NULL DEFAULT 3"
                )
            if "retry_count" not in cols:
                await db.execute(
                    "ALTER TABLE tasks ADD COLUMN retry_count INTEGER "
                    "NOT NULL DEFAULT 0"
                )
            await db.commit()

    # ---- boards ----------------------------------------------------

    @property
    def _current_pointer_path(self) -> Path:
        return self.db_path.parent / "current_board"

    def get_current_board(self) -> str:
        try:
            return self._current_pointer_path.read_text(encoding="utf-8").strip() or _BOARD_DEFAULT
        except OSError:
            return _BOARD_DEFAULT

    def set_current_board(self, slug: str) -> None:
        path = self._current_pointer_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(normalize_board_slug(slug), encoding="utf-8")

    async def create_board(
        self,
        slug: str,
        *,
        name: str | None = None,
        icon: str | None = None,
        description: str | None = None,
    ) -> KanbanBoard:
        slug = normalize_board_slug(slug)
        existing = await self.get_board(slug)
        if existing is not None:
            raise ValueError(f"board {slug!r} already exists")
        now = _utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "INSERT INTO boards(id, name, icon, description, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (slug, name or slug, icon, description, now),
            )
            await db.commit()
            async with db.execute(
                "SELECT * FROM boards WHERE id=?", (slug,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_board(row)

    async def list_boards(
        self, *, include_archived: bool = False
    ) -> list[KanbanBoard]:
        sql = "SELECT * FROM boards"
        if not include_archived:
            sql += " WHERE archived_at IS NULL"
        sql += " ORDER BY created_at ASC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql) as cur:
                rows = await cur.fetchall()
        return [_row_to_board(r) for r in rows]

    async def get_board(self, slug: str) -> KanbanBoard | None:
        slug = normalize_board_slug(slug)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM boards WHERE id=?", (slug,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_board(row) if row else None

    async def rename_board(
        self, slug: str, name: str
    ) -> KanbanBoard | None:
        slug = normalize_board_slug(slug)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "UPDATE boards SET name=? WHERE id=?", (name, slug)
            )
            await db.commit()
            async with db.execute(
                "SELECT * FROM boards WHERE id=?", (slug,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_board(row) if row else None

    async def archive_board(self, slug: str) -> bool:
        slug = normalize_board_slug(slug)
        if slug == _BOARD_DEFAULT:
            return False  # default board can't be archived
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE boards SET archived_at=? "
                "WHERE id=? AND archived_at IS NULL",
                (_utc_now_iso(), slug),
            )
            await db.commit()
        return cur.rowcount > 0

    async def hard_delete_board(self, slug: str) -> bool:
        """Permanent delete — removes board, its tasks, runs, comments, links, events.
        Default board can't be hard-deleted.
        """
        slug = normalize_board_slug(slug)
        if slug == _BOARD_DEFAULT:
            return False
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                # Cascade delete by board's tasks
                async with db.execute(
                    "SELECT id FROM tasks WHERE board_id=?", (slug,)
                ) as cur:
                    task_ids = [r[0] for r in await cur.fetchall()]
                if task_ids:
                    placeholders = ",".join("?" * len(task_ids))
                    for tbl in (
                        "task_events", "task_comments", "task_runs",
                        "task_links", "tasks",
                    ):
                        if tbl == "task_links":
                            await db.execute(
                                f"DELETE FROM {tbl} "
                                f"WHERE parent_id IN ({placeholders}) "
                                f"OR child_id IN ({placeholders})",
                                task_ids + task_ids,
                            )
                        elif tbl == "tasks":
                            await db.execute(
                                f"DELETE FROM {tbl} WHERE id IN ({placeholders})",
                                task_ids,
                            )
                        else:
                            await db.execute(
                                f"DELETE FROM {tbl} "
                                f"WHERE task_id IN ({placeholders})",
                                task_ids,
                            )
                cur = await db.execute(
                    "DELETE FROM boards WHERE id=?", (slug,)
                )
                deleted = cur.rowcount > 0
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise
        return deleted

    # ---- task CRUD --------------------------------------------------

    async def create_task(
        self,
        *,
        title: str,
        assignee: str | None,
        body: str = "",
        status: KanbanStatus = "todo",
        tenant: str | None = None,
        priority: int = 0,
        scheduled_at: str | None = None,
        max_runtime_seconds: int | None = None,
        created_by: str = "",
        idempotency_key: str | None = None,
        parents: list[str] | None = None,
        skills: list[str] | None = None,
        board_id: str = _BOARD_DEFAULT,
        workspace_kind: str = "scratch",
        workspace_path: str | None = None,
        max_retries: int = 3,
    ) -> KanbanTask:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if idempotency_key:
                async with db.execute(
                    "SELECT * FROM tasks WHERE board_id=? AND idempotency_key=? "
                    "AND status NOT IN ('done', 'archived') LIMIT 1",
                    (board_id, idempotency_key),
                ) as cur:
                    existing = await cur.fetchone()
                if existing:
                    return _row_to_task(existing)

            tid = _short_id("t_")
            now = _utc_now_iso()
            skills_json = json.dumps(list(skills or []))
            await db.execute(
                "INSERT INTO tasks(id, board_id, title, body, status, "
                "assignee, tenant, priority, workspace_kind, workspace_path, "
                "scheduled_at, max_runtime_seconds, created_at, updated_at, "
                "created_by, idempotency_key, skills_json, max_retries) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, board_id, title, body, status, assignee, tenant,
                 priority, workspace_kind, workspace_path,
                 scheduled_at, max_runtime_seconds, now, now,
                 created_by, idempotency_key, skills_json, max_retries),
            )
            for pid in (parents or []):
                await db.execute(
                    "INSERT INTO task_links(parent_id, child_id, created_at) VALUES(?,?,?)",
                    (pid, tid, now),
                )
            await self._record_event(
                db, tid, "created",
                {"assignee": assignee, "tenant": tenant, "status": status,
                 "skills": list(skills or []),
                 "workspace_kind": workspace_kind,
                 "workspace_path": workspace_path,
                 "max_retries": max_retries},
                actor=created_by or "human", at=now,
            )
            await db.commit()
            return await self._fetch_task(db, tid)

    async def _fetch_task(self, db, task_id: str) -> KanbanTask | None:
        async with db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_task(row) if row else None

    async def get_task(self, task_id: str) -> KanbanTask | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            return await self._fetch_task(db, task_id)

    async def list_tasks(
        self,
        *,
        status: KanbanStatus | None = None,
        assignee: str | None = None,
        tenant: str | None = None,
        board_id: str | None = None,
        include_archived: bool = False,
    ) -> list[KanbanTask]:
        """List tasks. ``board_id=None`` (default) returns all boards;
        pass an explicit slug (e.g. ``'default'``) to filter."""
        sql = "SELECT * FROM tasks"
        where: list[str] = []
        params: list = []
        if board_id is not None:
            where.append("board_id=?")
            params.append(board_id)
        if status:
            where.append("status=?")
            params.append(status)
        elif not include_archived:
            where.append("status != 'archived'")
        if assignee:
            where.append("assignee=?")
            params.append(assignee)
        if tenant:
            where.append("tenant=?")
            params.append(tenant)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY priority DESC, created_at ASC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def list_tasks_due(self, now_iso: str) -> list[KanbanTask]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM tasks WHERE scheduled_at IS NOT NULL "
                "AND scheduled_at <= ? AND status IN ('triage', 'todo') "
                "ORDER BY priority DESC, scheduled_at ASC",
                (now_iso,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def parents_of(self, child_id: str) -> list[KanbanTask]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT t.* FROM tasks t JOIN task_links l ON l.parent_id=t.id "
                "WHERE l.child_id=?",
                (child_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def children_of(self, parent_id: str) -> list[KanbanTask]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT t.* FROM tasks t JOIN task_links l ON l.child_id=t.id "
                "WHERE l.parent_id=?",
                (parent_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def add_link(self, *, parent_id: str, child_id: str) -> bool:
        if parent_id == child_id:
            return False
        if await self._reachable(child_id, parent_id):
            return False
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO task_links(parent_id, child_id, created_at) "
                    "VALUES(?,?,?)",
                    (parent_id, child_id, _utc_now_iso()),
                )
                await self._record_event(
                    db, child_id, "linked",
                    {"parent_id": parent_id},
                    actor="human", at=_utc_now_iso(),
                )
                await db.commit()
        except aiosqlite.IntegrityError:
            return False
        return True

    async def remove_link(self, *, parent_id: str, child_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM task_links WHERE parent_id=? AND child_id=?",
                (parent_id, child_id),
            )
            removed = cur.rowcount > 0
            if removed:
                await self._record_event(
                    db, child_id, "unlinked",
                    {"parent_id": parent_id},
                    actor="human", at=_utc_now_iso(),
                )
            await db.commit()
        return removed

    async def set_assignee(
        self, task_id: str, assignee: str | None, *, actor: str = "human"
    ) -> KanbanTask | None:
        now = _utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "UPDATE tasks SET assignee=?, updated_at=? WHERE id=?",
                (assignee, now, task_id),
            )
            await self._record_event(
                db, task_id, "assigned",
                {"assignee": assignee}, actor=actor, at=now,
            )
            await db.commit()
            return await self._fetch_task(db, task_id)

    async def _reachable(self, src: str, dst: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            queue = [src]
            seen = {src}
            while queue:
                node = queue.pop(0)
                if node == dst:
                    return True
                async with db.execute(
                    "SELECT child_id FROM task_links WHERE parent_id=?",
                    (node,),
                ) as cur:
                    rows = await cur.fetchall()
                for r in rows:
                    cid = r[0]
                    if cid not in seen:
                        seen.add(cid)
                        queue.append(cid)
        return False

    async def set_status(
        self,
        task_id: str,
        status: KanbanStatus,
        *,
        actor: str = "human",
        reason: str | None = None,
    ) -> KanbanTask | None:
        now = _utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                (status, now, task_id),
            )
            payload: dict = {"to": status}
            if reason:
                payload["reason"] = reason
            await self._record_event(
                db, task_id, "status_changed", payload, actor=actor, at=now
            )
            await db.commit()
            return await self._fetch_task(db, task_id)

    async def add_comment(
        self, task_id: str, *, author: str, body: str
    ) -> KanbanComment:
        now = _utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "INSERT INTO task_comments(task_id, author, body, created_at) "
                "VALUES(?,?,?,?)",
                (task_id, author, body, now),
            )
            await db.execute(
                "UPDATE tasks SET updated_at=? WHERE id=?", (now, task_id)
            )
            comment_id = cur.lastrowid
            await db.commit()
        return KanbanComment(
            id=comment_id, task_id=task_id, author=author,
            body=body, created_at=now,
        )

    async def list_comments(self, task_id: str) -> list[KanbanComment]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM task_comments WHERE task_id=? ORDER BY id ASC",
                (task_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [
            KanbanComment(
                id=r["id"], task_id=r["task_id"], author=r["author"],
                body=r["body"], created_at=r["created_at"],
            )
            for r in rows
        ]

    # ---- atomic claim ----------------------------------------------

    async def atomic_claim_one_ready(
        self,
        *,
        claim_ttl_seconds: int,
        exclude_ids: tuple[str, ...] | list[str] = (),
    ) -> KanbanTask | None:
        """Pick the highest-priority ready task and mark it running.

        Race-free via SQLite ``BEGIN IMMEDIATE``. Returns None if no ready
        task is available. ``exclude_ids`` skips tasks the dispatcher
        already touched this tick (reclaimed / spawn_failed) so it can't
        immediately re-claim them.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                sql = "SELECT * FROM tasks WHERE status='ready'"
                params: list = []
                if exclude_ids:
                    placeholders = ",".join("?" * len(exclude_ids))
                    sql += f" AND id NOT IN ({placeholders})"
                    params.extend(exclude_ids)
                sql += " ORDER BY priority DESC, created_at ASC LIMIT 1"
                async with db.execute(sql, params) as cur:
                    row = await cur.fetchone()
                if row is None:
                    await db.execute("ROLLBACK")
                    return None
                task_id = row["id"]
                run_id = _short_id("r_")
                now_dt = _utc_now()
                now = now_dt.isoformat(timespec="seconds")
                ttl = (now_dt + timedelta(seconds=claim_ttl_seconds)).isoformat(
                    timespec="seconds"
                )
                # Resolve workspace per task kind (scratch | dir).
                ws_str = materialize_workspace(
                    kind=row["workspace_kind"] or "scratch",
                    workspace_path=row["workspace_path"],
                    scratch_root=self.workspaces_root,
                    task_id=task_id,
                )
                await db.execute(
                    "INSERT INTO task_runs(id, task_id, started_at, "
                    "claim_expires_at, workspace_path) VALUES(?,?,?,?,?)",
                    (run_id, task_id, now, ttl, ws_str),
                )
                await db.execute(
                    "UPDATE tasks SET status='running', current_run_id=?, "
                    "workspace_path=?, updated_at=? WHERE id=?",
                    (run_id, ws_str, now, task_id),
                )
                await self._record_event(
                    db, task_id, "run_started",
                    {"run_id": run_id, "workspace": ws_str},
                    actor="dispatcher", at=now,
                )
                await db.execute("COMMIT")
                return await self._fetch_task(db, task_id)
            except Exception:
                await db.execute("ROLLBACK")
                raise

    # ---- runs -------------------------------------------------------

    async def get_run(self, run_id: str) -> KanbanRun | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM task_runs WHERE id=?", (run_id,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_run(row) if row else None

    async def list_runs(self, task_id: str) -> list[KanbanRun]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM task_runs WHERE task_id=? ORDER BY started_at ASC",
                (task_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_run(r) for r in rows]

    async def list_active_runs(self) -> list[KanbanRun]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM task_runs WHERE ended_at IS NULL"
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_run(r) for r in rows]

    async def attach_pid(self, run_id: str, pid: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE task_runs SET pid=? WHERE id=?", (pid, run_id)
            )
            await db.commit()

    async def heartbeat(
        self, run_id: str, *, ttl_seconds: int, note: str = ""
    ) -> bool:
        now_dt = _utc_now()
        now = now_dt.isoformat(timespec="seconds")
        new_ttl = (now_dt + timedelta(seconds=ttl_seconds)).isoformat(
            timespec="seconds"
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE task_runs SET claim_expires_at=?, last_heartbeat_at=? "
                "WHERE id=?",
                (new_ttl, now, run_id),
            )
            async with db.execute(
                "SELECT task_id FROM task_runs WHERE id=?", (run_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                await db.commit()
                return False
            await self._record_event(
                db, row[0], "heartbeat",
                {"note": note} if note else {},
                actor="worker", at=now,
            )
            await db.commit()
        return True

    async def end_run(
        self,
        run_id: str,
        *,
        outcome: RunOutcome,
        summary: str | None = None,
        metadata: dict | None = None,
        error: str | None = None,
    ) -> None:
        now = _utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE task_runs SET ended_at=?, outcome=?, summary=?, "
                "metadata_json=?, error=? WHERE id=?",
                (now, outcome, summary,
                 json.dumps(metadata) if metadata else None, error, run_id),
            )
            async with db.execute(
                "SELECT task_id FROM task_runs WHERE id=?", (run_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is not None:
                await self._record_event(
                    db, row[0], "run_ended",
                    {"run_id": run_id, "outcome": outcome,
                     "summary": summary, "error": error},
                    actor="worker", at=now,
                )
            await db.commit()

    async def bump_spawn_failure(self, task_id: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE tasks SET spawn_failure_count=spawn_failure_count+1, "
                "updated_at=? WHERE id=?",
                (_utc_now_iso(), task_id),
            )
            await db.commit()
            async with db.execute(
                "SELECT spawn_failure_count FROM tasks WHERE id=?", (task_id,)
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0

    async def reset_spawn_failure(self, task_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE tasks SET spawn_failure_count=0 WHERE id=?", (task_id,)
            )
            await db.commit()

    async def bump_retry_count(self, task_id: str) -> int:
        """Increment retry_count and return the new value.

        v0.13 Tenacity: dispatcher reclaims an incomplete-exit run by
        bumping retry_count. When retry_count >= max_retries, the
        dispatcher auto-blocks instead of cycling the task forever.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE tasks SET retry_count=retry_count+1, updated_at=? "
                "WHERE id=?",
                (_utc_now_iso(), task_id),
            )
            await db.commit()
            async with db.execute(
                "SELECT retry_count FROM tasks WHERE id=?", (task_id,)
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0

    async def reset_retry_count(self, task_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE tasks SET retry_count=0 WHERE id=?", (task_id,)
            )
            await db.commit()

    # ---- events -----------------------------------------------------

    async def _record_event(
        self,
        db,
        task_id: str,
        kind: str,
        payload: dict,
        *,
        actor: str | None = None,
        at: str | None = None,
    ) -> None:
        await db.execute(
            "INSERT INTO task_events(task_id, kind, payload_json, actor, "
            "created_at) VALUES(?,?,?,?,?)",
            (task_id, kind, json.dumps(payload) if payload else None,
             actor, at or _utc_now_iso()),
        )

    async def record_event(
        self,
        task_id: str,
        kind: str,
        payload: dict | None = None,
        *,
        actor: str | None = None,
    ) -> None:
        """Public single-shot event writer. Used by tools (e.g.
        ``hallucination_rejected``) that need to record an audit event
        outside an existing transaction."""
        async with aiosqlite.connect(self.db_path) as db:
            await self._record_event(
                db, task_id, kind, payload or {}, actor=actor,
            )
            await db.commit()

    async def list_events(self, task_id: str) -> list[KanbanEvent]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM task_events WHERE task_id=? ORDER BY id ASC",
                (task_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [
            KanbanEvent(
                id=r["id"], task_id=r["task_id"], kind=r["kind"],
                payload=json.loads(r["payload_json"]) if r["payload_json"] else {},
                actor=r["actor"], created_at=r["created_at"],
            )
            for r in rows
        ]

    async def list_events_since(
        self,
        since_id: int,
        *,
        task_id: str | None = None,
        limit: int = 200,
    ) -> list[KanbanEvent]:
        """Tail-friendly query: events with id > since_id, optionally
        filtered by task. Use the largest returned id as the next ``since_id``.
        """
        sql = "SELECT * FROM task_events WHERE id > ?"
        params: list = [since_id]
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [
            KanbanEvent(
                id=r["id"], task_id=r["task_id"], kind=r["kind"],
                payload=json.loads(r["payload_json"]) if r["payload_json"] else {},
                actor=r["actor"], created_at=r["created_at"],
            )
            for r in rows
        ]

    async def latest_event_id(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COALESCE(MAX(id), 0) FROM task_events"
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0


__all__ = ["KanbanDB"]
