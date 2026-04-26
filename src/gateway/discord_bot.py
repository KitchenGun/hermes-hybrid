"""Discord gateway. Thin shell — delegates all work to the Orchestrator.

R12: If REQUIRE_ALLOWLIST=true and ALLOWED_USER_IDS is empty, the bot
refuses to start. If the allowlist is present, only listed users' messages
are processed.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

import discord
from discord.ext import commands

from src.config import Settings
from src.gateway.confirm_view import ConfirmView, build_preview_embed
from src.memory import SqliteMemory
from src.obs import bind_task_id, get_logger
from src.orchestrator import Orchestrator
from src.state import ConfirmationContext, Repository, TaskState
from src.watcher import WatcherRunner

log = get_logger(__name__)
DISCORD_MAX = 2000
SLOW_THRESHOLD_S = 3.0

# Opt-in "heavy" path: explicit user prefix that routes the message to the
# Claude Code CLI via Max subscription. Skips router + tier escalation.
_HEAVY_PREFIX = "!heavy"


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
        self.watcher_runner: WatcherRunner | None = None

    async def on_ready(self) -> None:  # pragma: no cover
        log.info("discord.ready", user=str(self.user), id=getattr(self.user, "id", 0))
        # HITL restart recovery: any task left in ``awaiting_confirmation``
        # when the bot died has an orphaned button message — the view state
        # lives in memory only, so clicks now no-op. We ping the owner with
        # a text fallback referring to ``/confirm <id> yes|no`` so they can
        # still resolve the gate without re-triggering the whole job.
        if self.settings.hitl_enabled and self.settings.hitl_fallback_to_text_command:
            try:
                pending = await self.orchestrator.list_pending_confirmations()
            except Exception as e:  # noqa: BLE001
                log.warning("hitl.recovery.list_failed", err=str(e))
            else:
                for task in pending:
                    await self._notify_recovery(task)

        if self.settings.watcher_enabled and self.watcher_runner is None:
            try:
                self.watcher_runner = WatcherRunner(
                    settings=self.settings,
                    repo=self.repo,
                    profile_loader=self.orchestrator.profile_loader,
                    profiles_dir=self.settings.profiles_dir,
                )
                await self.watcher_runner.start()
            except Exception as e:  # noqa: BLE001
                log.error("watcher_runner.start_failed", err=str(e))
                self.watcher_runner = None

    async def close(self) -> None:  # pragma: no cover
        if self.watcher_runner is not None:
            try:
                await self.watcher_runner.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("watcher_runner.stop_failed", err=str(e))
        await super().close()

    async def _notify_recovery(self, task) -> None:  # pragma: no cover
        """Best-effort DM the owner of a task stranded in awaiting_confirmation.

        We don't try to re-post the button view — the old message_id is
        invalid for state-tracking after restart, and re-rendering buttons
        would risk double-execution if the original message also fires
        later. The text command `/confirm` is deterministic instead.
        """
        ctx = task.confirmation_context
        if ctx is None:
            return
        if ctx.is_expired():
            # Clean up: treat as timeout so it doesn't linger in the list.
            try:
                await self.orchestrator.resume_after_confirmation(
                    task.task_id, decision="timeout", actor_user_id=task.user_id
                )
            except Exception as e:  # noqa: BLE001
                log.warning("hitl.recovery.cleanup_failed", task_id=task.task_id, err=str(e))
            return
        try:
            user = await self.fetch_user(int(task.user_id))
        except (discord.HTTPException, ValueError) as e:
            log.info("hitl.recovery.fetch_user_failed", task_id=task.task_id, err=str(e))
            return
        text = (
            f"🔁 봇이 재시작되어 이전 확인 요청의 버튼이 만료됐습니다.\n"
            f"Task `{task.task_id}` — {ctx.preview_title}\n"
            f"`/confirm {task.task_id} yes` 또는 `/confirm {task.task_id} no` "
            f"로 응답해주세요."
        )
        try:
            await user.send(text)
            log.info("hitl.recovery.notified", task_id=task.task_id, user_id=task.user_id)
        except discord.HTTPException as e:
            log.info("hitl.recovery.dm_failed", task_id=task.task_id, err=str(e))

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

        # Heavy path opt-in: `!heavy <message>` → route to Claude Code CLI.
        # We strip the prefix here so the orchestrator sees the user's actual
        # prompt, and pass heavy=True so it skips rule/router/tier logic.
        heavy = False
        low = content.lower()
        if low == _HEAVY_PREFIX or low.startswith(_HEAVY_PREFIX + " "):
            heavy = True
            content = content[len(_HEAVY_PREFIX):].strip()
            if not content:
                await message.channel.send(
                    "Usage: `!heavy <your message>` — routes to Claude via Max subscription."
                )
                return

        user_id = message.author.id
        session_id = self._sessions.get(user_id)
        placeholder_text = "🔧 heavy (Claude)…" if heavy else "⏳ processing…"
        placeholder = await message.channel.send(placeholder_text)

        try:
            start = asyncio.get_event_loop().time()
            result = await self.orchestrator.handle(
                content,
                user_id=str(user_id),
                session_id=session_id,
                history=self._history[user_id][-8:],
                heavy=heavy,
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
                    heavy=heavy,
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

    # ---- HITL ---------------------------------------------------------

    async def send_confirmation(
        self,
        channel: discord.abc.Messageable,
        task: TaskState,
        ctx: ConfirmationContext,
        *,
        on_approve=None,
        on_decline=None,
    ) -> discord.Message:
        """Post the HITL preview embed with a :class:`ConfirmView` attached.

        Returns the posted message so the caller can await the view's
        ``wait()`` (``view.approved`` reveals the outcome) or just keep a
        handle for later edits. Persists the Discord message id back onto
        the task so a bot restart can still locate the gate.
        """
        embed = build_preview_embed(
            title=ctx.preview_title,
            body=ctx.preview_body,
            color=ctx.preview_color,
            task_id=task.task_id,
        )
        timeout_seconds = max(
            1.0,
            (ctx.expires_at.timestamp() - discord.utils.utcnow().timestamp()),
        )
        view = ConfirmView(
            task_id=task.task_id,
            owner_user_id=int(task.user_id),
            orchestrator=self.orchestrator,
            timeout_seconds=timeout_seconds,
            on_approve=on_approve,
            on_decline=on_decline,
        )
        message = await channel.send(embed=embed, view=view)
        view.bind_message(message)
        await self.orchestrator.record_confirmation_message(
            task.task_id,
            message_id=message.id,
            channel_id=getattr(channel, "id", 0) or 0,
        )
        log.info(
            "hitl.posted",
            task_id=task.task_id,
            message_id=message.id,
            channel_id=getattr(channel, "id", 0),
        )
        return message

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
