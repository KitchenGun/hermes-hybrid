"""Discord gateway. Thin shell — delegates all work to the Orchestrator.

R12: If REQUIRE_ALLOWLIST=true and ALLOWED_USER_IDS is empty, the bot
refuses to start. If the allowlist is present, only listed users' messages
are processed.

Phase 8 (2026-05-06): forced_profile / HITL / watcher 폐기.
Phase 11 (2026-05-06): !heavy prefix 폐기 — master = single lane.
Phase 2-A Kanban (2026-05-07): setup_hook 에서 KanbanDispatcher 백그라운드
loop 시작. close 시 graceful 종료.
Phase 22 (2026-05-07): journal pipeline 재도입 — ``JOURNAL_CHANNEL_ID`` 채널
메시지는 Orchestrator 우회하고 ``JournalPipeline.handle()`` 직행.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

import discord
from discord.ext import commands

from src.claude_adapter import ClaudeCodeAdapter
from src.config import Settings
from src.core import ExperienceLogger
from src.core.feedback_keywords import match_text as _feedback_match_text
from src.core.kanban import KanbanDB
from src.core.kanban.dispatcher import KanbanDispatcher
from src.core.kanban.worker_runner import spawn_master_worker
from src.gateway.feedback_router import FeedbackRouter
from src.memory import SqliteMemory
from src.obs import bind_task_id, get_logger
from src.orchestrator import Orchestrator
from src.skills.journal.pipeline import JournalPipeline
from src.state import (
    DiscordSession,
    Repository,
    SessionStore,
    make_session_key,
)

log = get_logger(__name__)
DISCORD_MAX = 2000
SLOW_THRESHOLD_S = 3.0


_POSITIVE_EMOJIS = {"👍", "❤️", "🎉", "✅", "💯"}
_NEGATIVE_EMOJIS = {"👎", "❌", "🛑"}


def _emoji_polarity(emoji: str) -> str | None:
    if emoji in _NEGATIVE_EMOJIS:
        return "negative"
    if emoji in _POSITIVE_EMOJIS:
        return "positive"
    return None


class DiscordBot(commands.Bot):
    def __init__(self, settings: Settings, repo: Repository):
        intents = discord.Intents.default()
        intents.message_content = True
        # Phase 20 — reaction add/remove events.
        intents.reactions = True
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
        # P2 — Discord session auto-resume. Same SQLite file as Repository
        # (state_db_path) but a dedicated table. Hydrated in setup_hook,
        # written best-effort after each successful on_message turn.
        self.session_store = SessionStore(settings.state_db_path)

        # Phase 20 — feedback wiring. ExperienceLogger reused from
        # orchestrator so reaction patches land in the same root.
        self.feedback_router = FeedbackRouter(
            max_entries=settings.feedback_lru_max,
            ttl_seconds=settings.feedback_lru_ttl_seconds,
        )
        self._feedback_logger: ExperienceLogger | None = (
            getattr(self.orchestrator, "experience_logger", None)
        )

        # Phase 22 — journal pipeline. 채널 ID + webhook 둘 다 채워졌을 때만
        # 활성화. Orchestrator 의 lazy adapter 와 분리된 ClaudeCodeAdapter 를
        # 사용 (master_concurrency semaphore 는 인스턴스 단위라 일기 lane 의
        # 단발 호출과 master lane 은 사실상 독립).
        self.journal_pipeline: JournalPipeline | None = None
        if (
            settings.journal_enabled
            and settings.journal_channel_id > 0
            and settings.google_sheets_webhook_url
        ):
            self.journal_pipeline = JournalPipeline(
                settings=settings,
                adapter=ClaudeCodeAdapter(settings),
            )

    async def setup_hook(self) -> None:  # pragma: no cover
        """Discord.py 2.0+ async init hook — start KanbanDispatcher loop here."""
        # P2 — hydrate the legacy ``_sessions`` shape from SessionStore so
        # restart-survival is transparent to the rest of on_message().
        try:
            await self.session_store.init()
            hydrated = await self.session_store.hydrate_user_session_map()
            if hydrated:
                self._sessions.update(hydrated)
                log.info(
                    "discord.session_resume",
                    count=len(hydrated),
                )
        except Exception as e:  # noqa: BLE001
            log.warning("discord.session_hydrate_failed", err=str(e))

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

        # Phase 22 — journal lane: 지정 채널 메시지는 Orchestrator 우회하고
        # 24-필드 추출 → Apps Script append → 한국어 응답으로 직행. 시트
        # append 실패 시 ``JOURNAL_ALERT_WEBHOOK_URL`` (있다면) 로 빨간 embed.
        if (
            self.journal_pipeline is not None
            and message.channel.id == self.settings.journal_channel_id
        ):
            await self._handle_journal(message, content)
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

            # P2 — best-effort persist. Never block the user reply on a
            # save failure; just log and continue.
            try:
                await self._persist_session(message, result)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "discord.session_persist_failed",
                    user=user_id, err=str(e),
                )

            with bind_task_id(result.task.task_id, str(user_id)):
                log.info(
                    "discord.replied",
                    handled_by=result.handled_by,
                    tier=result.task.current_tier,
                    retries=result.task.retry_count,
                    cloud_calls=result.task.cloud_call_count,
                    elapsed_ms=int(elapsed * 1000),
                )

            sent_message_ids: list[int] = []
            if elapsed < SLOW_THRESHOLD_S:
                try:
                    await placeholder.delete()
                except discord.HTTPException:
                    pass
                sent_message_ids = await self._send_chunks(
                    message.channel, result.response,
                )
            else:
                first, rest = self._split(result.response)
                edited = await placeholder.edit(content=first or "(empty)")
                if edited is not None:
                    sent_message_ids.append(edited.id)
                for chunk in rest:
                    sent = await message.channel.send(chunk)
                    sent_message_ids.append(sent.id)

            # Phase 20 — register every bot message id → task_id so reactions
            # on any chunk land back on the same task.
            if self.settings.feedback_listener_enabled:
                for mid in sent_message_ids:
                    self.feedback_router.register(mid, result.task.task_id)

            # Phase 20 — text-keyword fallback (default OFF).
            if (
                self.settings.feedback_keyword_match_enabled
                and self._feedback_logger is not None
            ):
                polarity = _feedback_match_text(content)
                last_task = self._sessions.get(user_id)
                # Apply to the *previous* task — current input itself becomes
                # this turn's user_message and isn't yet "feedback on prior".
                # We approximate by tagging the just-finished task only when
                # the user wrote a follow-up containing a keyword.
                if polarity and last_task and last_task != result.task.session_id:
                    self._feedback_logger.append_feedback(
                        last_task,
                        feedback=polarity,
                        feedback_text=content[:160],
                    )

        except Exception as e:  # noqa: BLE001
            log.exception("discord.unhandled")
            try:
                await placeholder.edit(content=f"❌ internal error: `{type(e).__name__}`")
            except discord.HTTPException:
                pass

    async def _persist_session(  # pragma: no cover
        self, message: discord.Message, result,
    ) -> None:
        """Upsert the post-turn session into SessionStore.

        ``context_json`` carries only the master session id and the last
        task id — never the raw user message or response, never tokens.
        """
        guild = getattr(message, "guild", None)
        guild_id = str(guild.id) if guild is not None else None
        channel_id = str(message.channel.id)
        user_id = str(message.author.id)
        key = make_session_key(
            user_id=user_id, channel_id=channel_id, guild_id=guild_id,
        )
        await self.session_store.save(
            DiscordSession(
                session_key=key,
                user_id=user_id,
                channel_id=channel_id,
                guild_id=guild_id,
                last_task_id=getattr(result.task, "task_id", None),
                context={"session_id": result.task.session_id},
            )
        )

    async def _handle_journal(  # pragma: no cover
        self, message: discord.Message, content: str,
    ) -> None:
        """Journal lane — extract → append → reply. 실패 메시지도 항상 회신."""
        assert self.journal_pipeline is not None
        placeholder = await message.channel.send("⏳ 기록 중…")
        try:
            result = await self.journal_pipeline.handle(content)
        except Exception as e:  # noqa: BLE001
            log.exception("discord.journal_unhandled")
            try:
                await placeholder.edit(
                    content=f"❌ journal internal error: `{type(e).__name__}`",
                )
            except discord.HTTPException:
                pass
            return

        try:
            await placeholder.delete()
        except discord.HTTPException:
            pass
        await self._send_chunks(message.channel, result.response)
        log.info(
            "discord.journal_replied",
            ok=result.ok,
            rows_written=result.rows_written,
            rows_extracted=result.rows_extracted,
            extraction_ms=result.extraction_ms,
            error=result.error,
        )

    async def _send_chunks(
        self, channel: discord.abc.Messageable, text: str,
    ) -> list[int]:
        """Send chunks, return the discord message IDs of every chunk."""
        first, rest = self._split(text or "(empty)")
        ids: list[int] = []
        sent = await channel.send(first)
        ids.append(sent.id)
        for c in rest:
            sent = await channel.send(c)
            ids.append(sent.id)
        return ids

    # Phase 20 (2026-05-07) — reaction listener -----------------------

    async def on_reaction_add(  # pragma: no cover
        self, reaction: discord.Reaction, user: discord.abc.User,
    ) -> None:
        """Map 👍 / 👎 reactions to ExperienceLog feedback patches."""
        if user.bot:
            return
        if not self.settings.feedback_listener_enabled:
            return
        # R12 — only allowlisted users' reactions count if allowlist set.
        allow = self.settings.allowed_user_ids
        if allow and getattr(user, "id", None) not in allow:
            return

        polarity = _emoji_polarity(str(reaction.emoji))
        if polarity is None:
            return

        message_id = getattr(reaction.message, "id", None)
        if message_id is None:
            return
        task_id = self.feedback_router.lookup(int(message_id))
        if task_id is None:
            return

        if self._feedback_logger is None:
            return
        ok = self._feedback_logger.append_feedback(
            task_id,
            feedback=polarity,
            bot_message_id=int(message_id),
        )
        if ok:
            log.info(
                "discord.feedback_recorded",
                task_id=task_id, polarity=polarity,
            )

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
