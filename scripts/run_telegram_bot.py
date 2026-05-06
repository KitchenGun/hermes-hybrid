#!/usr/bin/env python3
"""Run the Telegram gateway alongside (or instead of) the Discord bot.

Both gateways share the same Orchestrator + Settings. Run this in its
own process — they don't conflict with each other.

Usage:
    python scripts/run_telegram_bot.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from src.config import Settings  # noqa: E402
from src.gateway.telegram_bot import TelegramBot  # noqa: E402
from src.orchestrator.orchestrator import Orchestrator  # noqa: E402
from src.state.repository import Repository  # noqa: E402


async def _main() -> int:
    settings = Settings()
    if not settings.telegram_bot_token:
        print("⚠️ TELEGRAM_BOT_TOKEN not set in .env — nothing to do.")
        return 1

    repo = Repository(settings.state_db_path)
    await repo.init()

    orch = Orchestrator(settings, repo=repo)
    bot = TelegramBot(settings, orch)

    print(
        f"telegram bot starting "
        f"(allowlist={len(settings.telegram_allowlist)} users)"
    )
    try:
        await bot.run()
    except KeyboardInterrupt:
        bot.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
