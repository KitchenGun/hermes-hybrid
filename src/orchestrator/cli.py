"""CLI entry point for quick testing without Discord."""
from __future__ import annotations

import argparse
import asyncio
import sys

from src.config import get_settings
from src.obs import setup_logging
from src.orchestrator import Orchestrator
from src.preflight import run_preflight
from src.state import Repository


async def _run(message: str, user: str, session: str | None) -> str:
    settings = get_settings()
    setup_logging(settings.log_level, json=settings.log_json)
    # CLI doesn't need gateway-stopped (it's not a long-running Discord bot).
    report = await run_preflight(settings, require_gateway_stopped=False)
    if not report.ok:
        return "PREFLIGHT FAILED:\n  " + "\n  ".join(report.errors)

    repo = Repository(settings.state_db_path)
    await repo.init()
    orch = Orchestrator(settings, repo=repo)
    result = await orch.handle(message, user_id=user, session_id=session)
    return (
        f"[handled_by={result.handled_by} "
        f"tier={result.task.current_tier} "
        f"route={result.task.route} "
        f"retries={result.task.retry_count} "
        f"cloud_calls={result.task.cloud_call_count} "
        f"task_id={result.task.task_id}]\n\n{result.response}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="hermes-hybrid")
    parser.add_argument("message")
    parser.add_argument("--user", default="cli-user")
    parser.add_argument("--session", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    print(asyncio.run(_run(args.message, args.user, args.session)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
