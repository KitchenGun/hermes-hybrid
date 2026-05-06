"""Discord gateway. Thin shell — delegates all work to the Orchestrator.

R12: If REQUIRE_ALLOWLIST=true and ALLOWED_USER_IDS is empty, the bot
refuses to start. If the allowlist is present, only listed users' messages
are processed.

Phase 8 (2026-05-06): forced_profile / journal_channel_id / HITL / watcher
모두 폐기.
Phase 11 (2026-05-06): !heavy prefix 폐기 — master = single lane.
Phase 2-A Kanban (2026-05-07): setup_hook 에서 KanbanDispatcher 백그라운드
loop 시작. close 시 graceful 종료.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

import discord
from discord.ext import commands

from src.config import Settings
from src.core.kanban import KanbanDB
from src.core.kanban.dispatcher import KanbanDispatcher
from src.core.kanban.worker_runner import spawn_master_worker
from src.memory import SqliteMemory
from src.obs import bind_task_id, get_logger
from src.orchestrator import Orchestrator
from src.state import Repository

log = get_logger(__name__)
DISCORD_MAX = 2000
SLOW_THRESHOLD_S = 3.0


class DiscordBot(commands.Bot):
    def __init__(self, settings: Settings, repo: Repository):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.repo = repo
        # Phase 3: use the persistent SqliteMemory backend so `/memo` notes
        # survive bot restarts. Shares the Repository's SQLite file.
        self.memory = SqliteMemory(settings.state_db_path)
        self.orchestrator = Orchestrator(settings, repo=repo, memory=self.memory)
        self._sessions: dict[int, str] = {}
        self._history: dict[int, list[dict[str, str]]] = defaultdict(list)
        # Phase 2-A Kanban — populated in setup_hook so async migrate() can run.
        self.kanban_db: KanbanDB | None = None
        self.kanban_dispatcher: KanbanDispatcher | None = None
        self._kanban_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:  # pragma: no cover
        """Discord.py 2.0+ async init hook — start KanbanDispatcher loop here."""
        if not self.settings.kanban_dispatcher_enabled:
            return
        self.kanban_db = KanbanDB(
            self.settings.kanban_db_path,
            workspaces_root=self.settings.kanban_workspaces_root,
        )
        await self.kanban_db.migrate()
        self.kanban_dispatcher = KanbanDispatcher(
            self.kanban_db,
            spawn_runner=lambda task: spawn_master_worker(task, self.settings),
            poll_seconds=self.settings.kanban_dispatcher_poll_seconds,
            claim_ttl_seconds=self.settings.kanban_claim_ttl_seconds,
            spawn_failure_limit=self.settings.kanban_spawn_failure_limit,
            notify=self._make_kanban_notify(),
        )
        self._kanban_task = asyncio.create_task(self.kanban_dispatcher.run())

    async def close(self) -> None:  # pragma: no cover
        if self.kanban_dispatcher is not None:
            await self.kanban_dispatcher.stop()
        if self._kanban_task is not None:
            try:
                await asyncio.wait_for(self._kanban_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        await super().close()

    def _make_kanban_notify(self):  # pragma: no cover
        """Build an async notify callback. Returns None when opt-out."""
        cid = self.settings.kanban_notify_channel_id
        if cid <= 0:
            return None

        async def _notify(kind: str, task_id: str) -> None:
            try:
                channel = self.get_channel(cid)
                if channel is None:
                    return
                await channel.send(f"🔄 kanban: {kind} → `{task_id[:8]}`")
            except discord.HTTPException:
                log.warning("kanban.notify_send_failed", task=task_id, kind=kind)

        return _notify

    async def on_ready(self) -> None:  # pragma: no cover
        log.info("discord.ready", user=str(self.user), id=getattr(self.user, "id", 0))

    async def on_message(self, message: discord.Message) -> None:  # pragma: no cover
        if message.author.bot:
            return

        # R12: fail-closed allowlist
        allow = self.settings.allowed_user_ids
        if self.settings.require_allowlist:
            if not allow or message.author.id not in allow:
                return
        elif allow and message.author.id not in allow:
            # allowlist set explicitly even when not required → still enforce
            return

        content = message.content.strip()
        if not content:
            return

        user_id = message.author.id
        session_id = self._sessions.get(user_id)
        placeholder = await message.channel.send("⏳ processing…")

        try:
            start = asyncio.get_event_loop().time()
            result = await self.orchestrator.handle(
                content,
                user_id=str(user_id),
                session_id=session_id,
                history=self._history[user_id][-8:],
            )
            elapsed = asyncio.get_event_loop().time() - start

            self._sessions[user_id] = result.task.session_id
            self._history[user_id].append({"role": "user", "content": content})
            self._history[user_id].append({"role": "assistant", "content": result.response})
            self._history[user_id] = self._history[user_id][-16:]

            with bind_task_id(result.task.task_id, str(user_id)):
                log.info(
                    "discord.replied",
                    handled_by=result.handled_by,
                    tier=result.task.current_tier,
                    retries=result.task.retry_count,
                    cloud_calls=result.task.cloud_call_count,
                    elapsed_ms=int(elapsed * 1000),
                )

            if elapsed < SLOW_THRESHOLD_S:
                try:
                    await placeholder.delete()
                except discord.HTTPException:
                    pass
                await self._send_chunks(message.channel, result.response)
            else:
                first, rest = self._split(result.response)
                await placeholder.edit(content=first or "(empty)")
                for chunk in rest:
                    await message.channel.send(chunk)

        except Exception as e:  # noqa: BLE001
            log.exception("discord.unhandled")
            try:
                await placeholder.edit(content=f"❌ internal error: `{type(e).__name__}`")
            except discord.HTTPException:
                pass

    async def _send_chunks(self, channel: discord.abc.Messageable, text: str) -> None:
        first, rest = self._split(text or "(empty)")
        await channel.send(first)
        for c in rest:
            await channel.send(c)

    @staticmethod
    def _split(text: str) -> tuple[str, list[str]]:
        if len(text) <= DISCORD_MAX:
            return text, []
        parts: list[str] = []
        remainder = text
        while len(remainder) > DISCORD_MAX:
            cut = remainder.rfind("\n", 0, DISCORD_MAX)
            if cut == -1:
                cut = DISCORD_MAX
            parts.append(remainder[:cut])
            remainder = remainder[cut:].lstrip("\n")
        parts.append(remainder)
        return parts[0], parts[1:]


def run_bot(settings: Settings, repo: Repository) -> None:
    if not settings.discord_bot_token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set")
    if settings.require_allowlist and not settings.allowed_user_ids:
        raise SystemExit(
            "REQUIRE_ALLOWLIST=true but DISCORD_ALLOWED_USER_IDS is empty. "
            "Refusing to start (fail-closed). Set allowlist or flip REQUIRE_ALLOWLIST=false."
        )
    bot = DiscordBot(settings, repo)
    bot.run(settings.discord_bot_token, log_handler=None)
