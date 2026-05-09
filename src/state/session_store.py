"""SQLite-backed Discord session store (P2 — auto-resume).

Persists per-user/channel Discord session state so the bot can pick up
the last task / profile / context across restarts. Coexists with
``Repository`` in the same SQLite DB (``settings.state_db_path``) but
manages its own table so callers don't need to thread a Repository
handle just to read sessions.

Design:
  * ``session_key`` is the stable identity. For DMs we prefix with
    ``dm:`` to avoid colliding with a guild text channel that happens
    to share the same numeric id.
  * ``context_json`` carries small, non-secret extras (e.g. master
    session id, last_task_id reference). Never store secrets, tokens,
    or raw conversation transcripts here.
  * Read paths tolerate corrupt ``context_json`` (returns empty dict)
    and missing optional columns — the bot never crashes because of a
    bad row.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite


_SCHEMA = """
CREATE TABLE IF NOT EXISTS discord_sessions (
    session_key    TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    channel_id     TEXT NOT NULL,
    guild_id       TEXT,
    profile        TEXT,
    forced_profile TEXT,
    last_task_id   TEXT,
    context_json   TEXT,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dsess_user
    ON discord_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_dsess_channel
    ON discord_sessions(channel_id);
CREATE INDEX IF NOT EXISTS idx_dsess_updated
    ON discord_sessions(updated_at);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_session_key(
    *,
    user_id: str,
    channel_id: str,
    guild_id: str | None = None,
) -> str:
    """Stable composite key.

    DMs (``guild_id=None``) get a ``dm:`` prefix so they cannot collide
    with a guild text channel that has the same numeric id.
    """
    if not user_id or not channel_id:
        raise ValueError("user_id and channel_id are required")
    if guild_id is None or str(guild_id).strip() == "":
        return f"dm:{channel_id}:{user_id}"
    return f"{guild_id}:{channel_id}:{user_id}"


@dataclass
class DiscordSession:
    session_key: str
    user_id: str
    channel_id: str
    guild_id: str | None = None
    profile: str | None = None
    forced_profile: str | None = None
    last_task_id: str | None = None
    context: dict = field(default_factory=dict)
    updated_at: str | None = None


def _safe_get(row, name: str, default=None):
    try:
        return row[name]
    except (KeyError, IndexError):
        return default


def _row_to_session(row) -> DiscordSession:
    raw_ctx = _safe_get(row, "context_json")
    ctx: dict = {}
    if raw_ctx:
        try:
            parsed = json.loads(raw_ctx)
            if isinstance(parsed, dict):
                ctx = parsed
        except (ValueError, TypeError):
            ctx = {}
    return DiscordSession(
        session_key=row["session_key"],
        user_id=row["user_id"],
        channel_id=row["channel_id"],
        guild_id=_safe_get(row, "guild_id"),
        profile=_safe_get(row, "profile"),
        forced_profile=_safe_get(row, "forced_profile"),
        last_task_id=_safe_get(row, "last_task_id"),
        context=ctx,
        updated_at=row["updated_at"],
    )


class SessionStore:
    """Async SQLite-backed Discord session persistence.

    Shares ``settings.state_db_path`` with :class:`src.state.Repository`
    but owns its own table. Safe to call ``init()`` multiple times.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def save(self, session: DiscordSession) -> DiscordSession:
        """Upsert a session. Sets ``updated_at`` if not provided."""
        if not session.session_key:
            raise ValueError("session_key is required")
        if not session.user_id or not session.channel_id:
            raise ValueError("user_id and channel_id are required")
        ts = session.updated_at or _utc_now_iso()
        ctx_payload = json.dumps(session.context or {}, ensure_ascii=False)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO discord_sessions(
                    session_key, user_id, channel_id, guild_id,
                    profile, forced_profile, last_task_id,
                    context_json, updated_at
                )
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_key) DO UPDATE SET
                    user_id=excluded.user_id,
                    channel_id=excluded.channel_id,
                    guild_id=excluded.guild_id,
                    profile=excluded.profile,
                    forced_profile=excluded.forced_profile,
                    last_task_id=excluded.last_task_id,
                    context_json=excluded.context_json,
                    updated_at=excluded.updated_at
                """,
                (
                    session.session_key,
                    session.user_id,
                    session.channel_id,
                    session.guild_id,
                    session.profile,
                    session.forced_profile,
                    session.last_task_id,
                    ctx_payload,
                    ts,
                ),
            )
            await db.commit()
        session.updated_at = ts
        return session

    async def get(self, session_key: str) -> DiscordSession | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM discord_sessions WHERE session_key=?",
                (session_key,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_session(row) if row else None

    async def list_all(self) -> list[DiscordSession]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM discord_sessions ORDER BY updated_at DESC"
            ) as cur:
                rows = await cur.fetchall()
        out: list[DiscordSession] = []
        for r in rows:
            try:
                out.append(_row_to_session(r))
            except Exception:
                # Skip rows that can't be hydrated — don't crash startup
                # because of a single bad record.
                continue
        return out

    async def list_for_user(self, user_id: str) -> list[DiscordSession]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM discord_sessions WHERE user_id=? "
                "ORDER BY updated_at DESC",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_session(r) for r in rows]

    async def delete(self, session_key: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM discord_sessions WHERE session_key=?",
                (session_key,),
            )
            await db.commit()
        return cur.rowcount > 0

    async def prune_older_than(self, *, days: int) -> int:
        """Optional TTL cleanup. Returns number of rows removed."""
        if days < 0:
            raise ValueError("days must be >= 0")
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).isoformat(timespec="seconds")
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM discord_sessions WHERE updated_at < ?",
                (cutoff,),
            )
            await db.commit()
        return cur.rowcount

    # ---- hydration helper ---------------------------------------------

    async def hydrate_user_session_map(self) -> dict[int, str]:
        """Build the legacy ``DiscordBot._sessions`` shape from disk.

        Returns ``{user_id_int: session_id}`` for the most recent row per
        user (rows are sorted DESC by ``updated_at``). Rows whose
        ``user_id`` isn't an int, or whose ``context_json`` doesn't
        carry a ``session_id``, are skipped silently — corrupt data
        never blocks startup.

        ``session_id`` is read from ``context['session_id']`` to keep
        the master Orchestrator's identifier (which is what gets
        threaded into ``Orchestrator.handle(session_id=...)``) decoupled
        from our composite ``session_key``.
        """
        out: dict[int, str] = {}
        for s in await self.list_all():
            try:
                uid_int = int(s.user_id)
            except (TypeError, ValueError):
                continue
            sid = (s.context or {}).get("session_id")
            if not isinstance(sid, str) or not sid:
                continue
            # First write wins — list_all() is DESC by updated_at, so the
            # newest session for each user is set first.
            if uid_int not in out:
                out[uid_int] = sid
        return out


__all__ = [
    "DiscordSession",
    "SessionStore",
    "make_session_key",
]
