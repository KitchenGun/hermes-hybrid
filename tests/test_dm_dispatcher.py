"""Tests for DmDispatcher — sends Discord DMs from the watcher runtime.

We avoid spinning up a real ``discord.Client``; the dispatcher only calls
``fetch_user`` then ``user.send``, so a minimal AsyncMock-based fake
suffices to verify the embed it builds and the error-translation contract.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.gateway.dm_dispatcher import DmDispatcher, DmDispatchError


@pytest.mark.asyncio
async def test_dm_dispatcher_sends_embed_via_user():
    fake_user = MagicMock()
    fake_user.send = AsyncMock()
    fake_bot = MagicMock()
    fake_bot.fetch_user = AsyncMock(return_value=fake_user)

    d = DmDispatcher(fake_bot)
    await d.send_dm(123456, title="t", body="b", footer="f")

    fake_bot.fetch_user.assert_awaited_once_with(123456)
    fake_user.send.assert_awaited_once()
    embed = fake_user.send.await_args.kwargs["embed"]
    assert embed.title == "t"
    assert embed.description == "b"
    assert embed.footer.text == "f"


@pytest.mark.asyncio
async def test_dm_dispatcher_truncates_long_fields():
    """Discord rejects oversize embeds; the dispatcher must clamp before
    sending so a verbose hermes response can't break the alert path."""
    fake_user = MagicMock()
    fake_user.send = AsyncMock()
    fake_bot = MagicMock()
    fake_bot.fetch_user = AsyncMock(return_value=fake_user)

    d = DmDispatcher(fake_bot)
    long_title = "T" * 500
    long_body = "B" * 5000
    long_footer = "F" * 3000
    await d.send_dm(1, title=long_title, body=long_body, footer=long_footer)

    embed = fake_user.send.await_args.kwargs["embed"]
    assert len(embed.title) == 256
    assert len(embed.description) == 4000
    assert len(embed.footer.text) == 2048


@pytest.mark.asyncio
async def test_dm_dispatcher_translates_fetch_user_failure_to_dispatch_error():
    """A ValueError from ``fetch_user`` must surface as DmDispatchError so
    the watcher runner's try/except can log + leave the watermark intact."""
    fake_bot = MagicMock()
    fake_bot.fetch_user = AsyncMock(side_effect=ValueError("bad id"))

    d = DmDispatcher(fake_bot)
    with pytest.raises(DmDispatchError, match="fetch_user"):
        await d.send_dm(1, title="t", body="b")
