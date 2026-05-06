"""Discord gateway. Thin shell — delegates all work to the Orchestrator.

R12: If REQUIRE_ALLOWLIST=true and ALLOWED_USER_IDS is empty, the bot
refuses to start. If the allowlist is present, only listed users' messages
are processed.

Phase 8 (2026-05-06) 후 단순화:
  * forced_profile / journal_channel_id / 채널 핀 라우팅 폐기
  * HITL confirmation 흐름 폐기 (profile yaml 의존)
  * watcher runner 폐기 (cron / poll watcher 폐기)
  * !heavy 만 prefix 라우팅으로 잔존
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

import discord
from discord.ext import commands

from src.config import Settings
from src.memory import SqliteMemory
from src.obs import bind_task_id, get_logger
from src.orchestrator import Orchestrator
from src.state import Repository

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
