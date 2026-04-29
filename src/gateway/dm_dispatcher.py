"""Send Discord DMs from non-bot contexts (e.g. WatcherRunner).

The watcher runtime lives in :mod:`src.watcher.runner` and must not import
the Discord bot directly — that would create a cycle (bot → orchestrator →
profile_loader → watcher → bot). Instead, when the bot is ready it builds a
:class:`DmDispatcher` around itself and hands it to the runner. The runner
calls ``send_dm`` without knowing anything about ``discord.Client``.

Calendar watchers use ``delivery.channel: dm`` + ``target_env:
DISCORD_DM_USER_ID``; the ``_dispatch_dm`` helper in the runner reads that
env var, converts to ``int``, and calls into here.
"""
from __future__ import annotations

from datetime import datetime, timezone

import discord

from src.obs import get_logger

log = get_logger(__name__)

_EMBED_COLOR_DM = 0xFEE75C  # yellow — distinguishes DM alerts from briefing webhooks


class DmDispatchError(RuntimeError):
    pass


class DmDispatcher:
    def __init__(self, bot: discord.Client):
        self._bot = bot

    async def send_dm(
        self,
        user_id: int,
        *,
        title: str,
        body: str,
        footer: str = "",
    ) -> None:
        try:
            user = await self._bot.fetch_user(user_id)
        except (discord.HTTPException, ValueError) as e:
            raise DmDispatchError(f"fetch_user({user_id}) failed: {e}") from e

        embed = discord.Embed(
            title=title[:256],
            description=body[:4000],
            color=_EMBED_COLOR_DM,
            timestamp=datetime.now(timezone.utc),
        )
        if footer:
            embed.set_footer(text=footer[:2048])

        try:
            await user.send(embed=embed)
        except discord.HTTPException as e:
            raise DmDispatchError(f"user.send failed: {e}") from e
        log.info("dm_dispatcher.sent", user_id=user_id, title=title[:60])
