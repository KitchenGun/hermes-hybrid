"""Entry point for the Discord gateway.

Performs preflight (R6+R15), initializes persistence (R4), starts the bot.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import get_settings  # noqa: E402
from src.gateway import run_bot  # noqa: E402
from src.obs import get_logger, setup_logging  # noqa: E402
from src.preflight import run_preflight  # noqa: E402
from src.state import Repository  # noqa: E402


async def _startup() -> tuple[bool, Repository]:
    settings = get_settings()
    setup_logging(settings.log_level, json=settings.log_json)
    log = get_logger(__name__)

    report = await run_preflight(settings, require_gateway_stopped=True)
    for w in report.warnings:
        log.warning("preflight.warning", msg=w)
    for e in report.errors:
        log.error("preflight.error", msg=e)
    if not report.ok:
        return False, None  # type: ignore[return-value]

    repo = Repository(settings.state_db_path)
    await repo.init()
    log.info("repo.ready", path=str(settings.state_db_path))
    return True, repo


def main() -> int:
    ok, repo = asyncio.run(_startup())
    if not ok:
        return 2
    settings = get_settings()
    run_bot(settings, repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
